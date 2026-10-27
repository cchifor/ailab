"""Unit tests for scripts/edge/render.py and the edge registry (kubernetes/edge/hostnames.yaml).

    python3 -m unittest scripts.tests.test_edge_render

What Caddy then DOES with the rendered config is tested by scripts/tests/edge_runtime.py (docker).
"""
import copy
import pathlib
import sys
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "edge"))
import render  # noqa: E402

FROZEN = ROOT / "scripts/tests/fixtures/edge/cloudflared-ingress-2026-10-27.yaml"

# Differences from the frozen cloudflared routing that are intended, each with its reason.
INTENDED = {
    # dw terminals: cloudflared sent Host: <IP>; the edge keeps Host and SNI dwN.chifor.me, which the
    # dev-worker gate answers and its WebSocket Origin check expects (role dev_worker, Caddyfile.j2).
    **{f"dw{i}": "host-sni" for i in range(1, 5)},
    # vault /admin: gated by a Cloudflare Access PATH app today, not a cloudflared rule; the edge
    # restricts it to trusted sources on the direct listener.
    "vault": "restricted-path",
    # new: the outside probe's target, a fixed answer naming the peer.
    "edge-canary": "new",
}
# cloudflared hostnames the registry deliberately does not carry.
DROPPED = {
    "strive.place": "vestigial: strive.place rides cloudflared-strive on another Cloudflare account",
    "id.strive.place": "vestigial: never live (the real host is auth.strive.place, another tunnel)",
}


def committed():
    return render.load()


def raw():
    with open(render.REGISTRY, encoding="utf-8") as f:
        return yaml.safe_load(f)


class RegistrySchema(unittest.TestCase):
    def test_committed_registry_is_valid(self):
        self.assertEqual(render.validate(raw()), [])

    def assert_rejected(self, mutate, fragment):
        reg = raw()
        mutate(reg)
        errors = render.validate(reg)
        self.assertTrue(any(fragment in e for e in errors), f"expected {fragment!r} in {errors}")

    def test_unknown_top_key(self):
        self.assert_rejected(lambda r: r.update(extra=1), "unknown key 'extra'")

    def test_unknown_host_key(self):
        self.assert_rejected(lambda r: r["hosts"]["router"].update(timeout=5), "unknown key 'timeout'")

    def test_missing_security_field(self):
        self.assert_rejected(lambda r: r["hosts"]["router"].pop("auth"), "missing 'auth'")
        self.assert_rejected(lambda r: r["hosts"]["router"].pop("exposure"), "missing 'exposure'")

    def test_bad_values(self):
        self.assert_rejected(lambda r: r["hosts"]["router"].update(exposure="internal"), "public or private")
        self.assert_rejected(lambda r: r["hosts"]["router"].update(auth="maybe"), "native, authelia or none")
        self.assert_rejected(lambda r: r.update(edge_default="cloudflare"), "edge_default")

    def test_private_host_never_in_tunnel(self):
        def m(r):
            r["hosts"]["proxmox"].update(exposure="private", access_gated=False)
        self.assert_rejected(m, "private hosts are never in the tunnel")

    def test_access_gated_cannot_go_direct_without_a_gate(self):
        self.assert_rejected(lambda r: r["hosts"]["proxmox"].update(edge="direct"), "access_gated host cannot be direct")
        self.assert_rejected(lambda r: r["hosts"]["dw1"].update(edge="default") or r.update(edge_default="direct"),
                             "access_gated host cannot be direct")

    def test_access_gated_authelia_host_may_go_direct(self):
        reg = raw()
        reg["hosts"]["prometheus"]["edge"] = "direct"
        self.assertEqual(render.validate(reg), [])

    def test_authelia_needs_group(self):
        self.assert_rejected(lambda r: r["hosts"]["dsh"].pop("authelia_group"), "needs authelia_group")

    def test_auth_none_only_for_public_respond(self):
        self.assert_rejected(lambda r: r["hosts"]["edge-canary"].update(auth="native"), "must be auth none")

    def test_backend_url_shape(self):
        self.assert_rejected(lambda r: r["hosts"]["router"]["backend"].update(url="http://x/path"), "scheme://host[:port] only")
        self.assert_rejected(lambda r: r["hosts"]["router"]["backend"].update(tls_server_name="x"), "tls_* options need an https url")

    def test_bad_cidr(self):
        self.assert_rejected(lambda r: r["trusted_sources"]["allow"].append("192.168.0.1/23"), "is not a CIDR")

    def test_bad_regex_and_backticks(self):
        self.assert_rejected(lambda r: r["hosts"]["id"]["routes"][0]["match"].update(path_regexp="(unclosed"), "bad regex")
        self.assert_rejected(lambda r: r["hosts"]["id"]["routes"][0]["match"].update(path_regexp="a`b"), "backticks")

    def test_routes_default_required(self):
        self.assert_rejected(lambda r: r["hosts"]["id"].pop("routes_default"), "routes_default: backend or 404")

    def test_invalid_host_label(self):
        self.assert_rejected(lambda r: r["hosts"].update({"Bad_Name": copy.deepcopy(r["hosts"]["router"])}), "invalid DNS label")


class EffectiveMode(unittest.TestCase):
    def test_default_follows_edge_default(self):
        reg = committed()
        self.assertEqual(render.effective_mode(reg, "router"), reg["edge_default"])
        reg["edge_default"] = "direct"
        self.assertEqual(render.effective_mode(reg, "router"), "direct")

    def test_pins_and_force_tunnel(self):
        reg = committed()
        reg["edge_default"] = "direct"
        self.assertEqual(render.effective_mode(reg, "dsh"), "tunnel")  # pinned
        reg["hosts"]["router"]["edge"] = "direct"
        reg["force_tunnel"] = True
        self.assertEqual(render.effective_mode(reg, "router"), "tunnel")  # force beats every pin


class Parity(unittest.TestCase):
    """Every registry backend equals the routing it replaces, except the INTENDED differences."""

    def setUp(self):
        with open(FROZEN, encoding="utf-8") as f:
            self.rules = yaml.safe_load(f)["ingress"]
        self.reg = committed()

    def by_host(self):
        out = {}
        for r in self.rules:
            if "hostname" in r:
                out.setdefault(r["hostname"], []).append(r)
        return out

    def test_same_hostnames(self):
        frozen = set(self.by_host())
        ours = {f"{n}.chifor.me" for n in self.reg["hosts"]}
        self.assertEqual(sorted(frozen - ours - set(DROPPED)), [], "cloudflared hosts missing from the registry")
        new = {h.split(".")[0] for h in ours - frozen}
        self.assertEqual(sorted(new - {k for k, v in INTENDED.items() if v == "new"}), [],
                         "registry hosts with no cloudflared counterpart")

    def test_backends_match(self):
        for host, rules in self.by_host().items():
            if host in DROPPED:
                continue
            name = host.split(".")[0]
            h = self.reg["hosts"][name]
            default = [r for r in rules if "path" not in r]
            self.assertEqual(len(default), 1, host)
            want = default[0]["service"]
            if want.startswith("http_status:404"):
                self.assertEqual(h.get("routes_default"), "404", f"{host}: cloudflared answered 404 by default")
            else:
                self.assertEqual(h["backend"]["url"], want, f"{host}: backend")
            origin = default[0].get("originRequest") or {}
            self.assertEqual(bool(origin.get("noTLSVerify")), bool(h["backend"].get("tls_insecure_skip_verify")),
                             f"{host}: TLS verification")
            if "caPool" in origin:
                self.assertEqual(h["backend"].get("tls_server_name"), origin["originServerName"], host)
                self.assertTrue(h["backend"].get("tls_ca"), f"{host}: CA pinning kept")
            if "httpHostHeader" in origin:
                self.assertEqual(INTENDED.get(name), "host-sni", f"{host}: Host header change not listed")
            for r in rules:
                if "path" not in r:
                    continue
                if r["service"].startswith("http_status"):
                    # cloudflared refused these paths outright; the raw-URI guard does that here
                    self.assertTrue(h.get("uri_guard"), f"{host}: deny rule {r['path']} needs uri_guard")
                    continue
                matching = [rt for rt in h.get("routes") or [] if rt["match"]["path_regexp"] == r["path"]]
                self.assertEqual(len(matching), 1, f"{host}: path rule {r['path']} carried over")
                to = matching[0]["to"]
                self.assertEqual(h["backend"]["url"] if to == "default" else to, r["service"], f"{host} {r['path']}")

    def test_access_gated_set(self):
        """The hosts Cloudflare Access gates today (access.tf) are exactly the access_gated ones."""
        gated = {"dw1", "dw2", "dw3", "dw4", "k8s", "hubble", "dsh", "proxmox", "qnap", "prometheus",
                 "alertmanager", "openbao"}
        ours = {n for n, h in self.reg["hosts"].items() if h["access_gated"]}
        self.assertEqual(ours, gated)


class AutheliaRules(unittest.TestCase):
    """Every `auth: authelia` host has an allow rule for its group FOLLOWED by a deny rule in
    Authelia's access_control, so the global one_factor default never applies to it."""

    def test_rules_match_the_registry(self):
        with open(ROOT / "kubernetes/apps/apps/auth/configuration.yml", encoding="utf-8") as f:
            rules = yaml.safe_load(f)["access_control"]["rules"]
        reg = committed()
        for name, h in reg["hosts"].items():
            if h["auth"] != "authelia":
                continue
            fqdn = f"{name}.chifor.me"
            hits = [(i, r) for i, r in enumerate(rules) if fqdn in (r["domain"] if isinstance(r["domain"], list) else [r["domain"]])]
            self.assertEqual(len(hits), 2, f"{fqdn}: expected an allow and a deny rule, got {hits}")
            (i_allow, allow), (i_deny, deny) = hits
            self.assertLess(i_allow, i_deny)
            self.assertEqual(allow.get("subject"), [f"group:{h['authelia_group']}"], fqdn)
            self.assertEqual(allow["policy"], "one_factor", fqdn)
            self.assertEqual(deny["policy"], "deny", fqdn)
            self.assertNotIn("subject", deny, fqdn)
        listed = {d for r in rules for d in (r["domain"] if isinstance(r["domain"], list) else [r["domain"]])
                  if r.get("policy") == "deny"}
        ours = {f"{n}.chifor.me" for n, h in reg["hosts"].items() if h["auth"] == "authelia"}
        self.assertEqual(listed, ours, "deny rules for hosts that are not authelia hosts in the registry")


class Rendering(unittest.TestCase):
    def setUp(self):
        self.reg = committed()
        self.text = render.render(self.reg)

    def test_deterministic(self):
        self.assertEqual(self.text, render.render(committed()))

    def test_committed_caddyfile_is_current(self):
        self.assertEqual(render.OUTPUT.read_text(encoding="utf-8"), self.text,
                         "run `just edge-render` and commit the Caddyfile")

    def site(self, header):
        start = self.text.index(header)
        end = self.text.index("\n}\n", start)
        return self.text[start:end]

    def test_tunnel_listener_has_no_private_or_unknown_backends(self):
        reg = copy.deepcopy(self.reg)
        reg["hosts"]["secret"] = {"exposure": "private", "auth": "native", "access_gated": False, "lan_dns": False,
                                  "backend": {"url": "http://secret.svc:80"}, "smoke": {"status": 200}}
        text = render.render(reg)
        start = text.index("http://:8080 {")
        tunnel = text[start:text.index("\n}\n", start)]
        self.assertNotIn("secret.chifor.me", tunnel)
        self.assertNotIn("secret.svc", tunnel)
        self.assertIn("secret.chifor.me", text[text.index("https://:8443 {"):start])

    def test_direct_listener_strips_cloudflare_headers(self):
        direct = self.site("https://:8443 {")
        self.assertIn("request_header -Cf-*", direct)
        tunnel = self.site("http://:8080 {")
        self.assertNotIn("request_header -Cf-*", tunnel)
        for site in (direct, tunnel):
            for h in render.STRIP_ALWAYS:
                self.assertIn(f"request_header -{h}", site)

    def test_tunnel_client_address_checks_come_first(self):
        tunnel = self.site("http://:8080 {")
        route = tunnel[tunnel.index("\troute {"):]
        first = [ln.strip() for ln in route.splitlines()[1:4]]
        self.assertEqual(first, ["respond @cf_client_bad 400", "respond @cf_visitor_missing 400",
                                 "redir @cf_visitor_http https://{host}{uri} 308"])

    def test_authelia_runs_before_the_proxy(self):
        direct = self.site("https://:8443 {")
        block = direct[direct.index("handle @h_dsh {"):]
        self.assertLess(block.index("import edge_authelia"), block.index("reverse_proxy"))

    def test_trust_rules_use_the_tcp_peer(self):
        self.assertIn("@untrusted_peer not remote_ip 192.168.0.0/23", self.text)
        self.assertNotIn("client_ip ", self.text.replace("{client_ip}", ""))

    def test_log_redaction(self):
        for h in render.LOG_REDACT:
            self.assertIn(f"request>headers>{h} delete", self.text)


if __name__ == "__main__":
    unittest.main()
