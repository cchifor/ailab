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

    def test_private_auth_none_only_for_a_fixed_answer(self):
        # the canary must be able to rehearse every mode, private included
        reg = raw()
        reg["hosts"]["edge-canary"]["exposure"] = "private"
        reg["hosts"]["edge-canary"].pop("edge")
        self.assertEqual(render.validate(reg), [])
        body = "\n".join(render._host_body(reg, "edge-canary", "direct", None))
        self.assertLess(body.index("respond @not_admin 403"), body.index('respond "edge-canary ok'))

        def private_upstream(r):
            r["hosts"]["status"].update(exposure="private", auth="none")
            r["hosts"]["status"].pop("edge", None)
        self.assert_rejected(private_upstream, "auth none on a private host is only for a fixed respond backend")

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

    def test_access_gated_needs_real_auth(self):
        self.assert_rejected(lambda r: r["hosts"]["proxmox"].update(auth="none"), "access_gated host needs authelia or native")

    def test_security_options_are_typed(self):
        self.assert_rejected(lambda r: r["hosts"]["proxmox"]["backend"].update(tls_insecure_skip_verify="false"),
                             "must be a bool")
        self.assert_rejected(lambda r: r["hosts"]["dw1"]["backend"].update(tls_server_name="dw1.chifor.me }"),
                             "is not a plain token")
        self.assert_rejected(lambda r: r["hosts"]["edge-canary"]["backend"].update(respond='ok" 200\n}'),
                             "words, '=' and {remote_host} only")
        self.assert_rejected(lambda r: r["hosts"]["router"]["smoke"].update(status="200"), "statuses must be integers")

    def test_duplicate_keys_are_refused_by_load(self):
        text = render.REGISTRY.read_text(encoding="utf-8")
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            dup_host = pathlib.Path(d) / "dup-host.yaml"
            i = text.index("  router:\n")
            j = text.index("\n  relay:\n")
            dup_host.write_text(text + "\n" + text[i:j].replace("edge: default", "edge: direct") + "\n", encoding="utf-8")
            with self.assertRaises(render.RegistryError):
                render.load(dup_host)
            dup_field = pathlib.Path(d) / "dup-field.yaml"
            dup_field.write_text(text.replace("  router:\n    exposure: public\n",
                                              "  router:\n    exposure: private\n    exposure: public\n"), encoding="utf-8")
            with self.assertRaises(render.RegistryError):
                render.load(dup_field)

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


class DnsRecords(unittest.TestCase):
    """kubernetes/edge/dns-records.json: the records dns.tf creates, from render.py's effective_mode()."""

    def test_one_shape_per_mode(self):
        reg = committed()
        reg["edge_default"] = "direct"
        reg["hosts"]["edge-canary"]["edge"] = "tunnel"
        recs = render.dns_records(reg)
        tunnel = f"{reg['tunnel_id']}.cfargotunnel.com"
        self.assertEqual(recs["router"], {"mode": "direct", "type": "CNAME", "content": "chifor.me", "proxied": False, "ttl": 60})
        self.assertEqual(recs["edge-canary"], {"mode": "tunnel", "type": "CNAME", "content": tunnel, "proxied": True, "ttl": 1})
        reg["hosts"]["edge-canary"].update(exposure="private")
        reg["hosts"]["edge-canary"].pop("edge")
        self.assertEqual(render.dns_records(reg)["edge-canary"],
                         {"mode": "private", "type": "A", "content": reg["vip"], "proxied": False, "ttl": 60})

    def test_every_host_once_and_modes_agree_with_the_proxy(self):
        reg = committed()
        for default in ("tunnel", "direct"):
            for force in (False, True):
                reg["edge_default"], reg["force_tunnel"] = default, force
                recs = render.dns_records(reg)
                self.assertEqual(set(recs), set(reg["hosts"]))
                for name, rec in recs.items():
                    self.assertEqual(rec["mode"], render.effective_mode(reg, name), name)

    def test_force_tunnel_never_publishes_a_private_host(self):
        reg = committed()
        reg["force_tunnel"] = True
        reg["hosts"]["edge-canary"].update(exposure="private")
        reg["hosts"]["edge-canary"].pop("edge")
        self.assertEqual(render.dns_records(reg)["edge-canary"]["type"], "A")

    def test_digest_ignores_carriage_returns_only(self):
        text = render.REGISTRY.read_bytes().decode("utf-8")
        self.assertEqual(render.registry_digest(text), render.registry_digest(text.replace("\n", "\r\n")))
        self.assertNotEqual(render.registry_digest(text), render.registry_digest(text + "#\n"))

    def test_main_hashes_the_text_it_validated(self):
        # an editor saving the registry between validation and hashing must not pair the records
        # rendered from the old text with the new text's hash (codex impl-review P4 round 2)
        import json
        import tempfile
        from unittest import mock
        text = render.REGISTRY.read_bytes().decode("utf-8")
        real_validate = render.validate
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            reg = d / "hostnames.yaml"
            reg.write_text(text, encoding="utf-8", newline="")

            def validate_then_save(r):
                errors = real_validate(r)
                reg.write_text(text + "# saved by an editor mid-render\n", encoding="utf-8", newline="")
                return errors
            with mock.patch.object(render, "validate", validate_then_save):
                rc = render.main(["--registry", str(reg), "--out", str(d / "Caddyfile"), "--dns-out", str(d / "dns.json")])
            self.assertEqual(rc, 0)
            doc = json.loads((d / "dns.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["registry_sha256"], render.registry_digest(text))

    def test_a_test_profile_never_writes_dns(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            for bad in ("{}", "null", "[]"):
                (d / "p.json").write_text(bad, encoding="utf-8")
                rc = render.main(["--test-profile", str(d / "p.json"), "--out", str(d / "Caddyfile"),
                                  "--dns-out", str(d / "dns.json")])
                self.assertEqual(rc, 1, bad)
            (d / "p.json").write_text('{"allow": []}', encoding="utf-8")
            with mock.patch.object(render, "render", lambda reg, profile: "# fixture\n"):
                rc = render.main(["--test-profile", str(d / "p.json"), "--out", str(d / "Caddyfile"),
                                  "--dns-out", str(d / "dns.json")])
            self.assertEqual(rc, 0)
            self.assertTrue((d / "Caddyfile").exists())
            self.assertFalse((d / "dns.json").exists())

    def test_committed_file_is_current(self):
        text = render.render_dns(committed(), render.REGISTRY.read_bytes().decode("utf-8"))
        self.assertEqual(render.DNS_OUTPUT.read_text(encoding="utf-8"), text,
                         "run `just edge-render` and commit kubernetes/edge/dns-records.json")

    def test_binding_covers_every_record_and_the_registry(self):
        import json
        text = render.REGISTRY.read_bytes().decode("utf-8")
        doc = json.loads(render.render_dns(committed(), text))
        recs = doc["records"]
        self.assertEqual(doc["binding_sha256"], render.records_binding(text, recs))
        # a valid-shape substitution, a dropped record, an emptied map, another registry: all differ
        sub = json.loads(json.dumps(recs))
        sub["router"] = {"mode": "direct", "type": "CNAME", "content": "chifor.me", "proxied": False, "ttl": 60}
        dropped = {k: v for k, v in recs.items() if k != "router"}
        for other in (render.records_binding(text, sub), render.records_binding(text, dropped),
                      render.records_binding(text, {}), render.records_binding(text + "#\n", recs)):
            self.assertNotEqual(doc["binding_sha256"], other)

    def test_dns_tf_consumes_the_file_behind_blocking_guards(self):
        tf = (ROOT / "kubernetes/infra/cloudflare/dns.tf").read_text(encoding="utf-8")
        self.assertIn("dns-records.json", tf)
        self.assertIn('replace(file("${path.module}/../../edge/hostnames.yaml"), "\\r", "")', tf)
        # the artifact guards sit on an output, which every plan evaluates however many records exist
        guard = tf[tf.index('output "edge_dns_artifact"'):tf.index('resource "cloudflare_dns_record" "edge"')]
        self.assertIn("local.edge_dns.registry_sha256 == sha256(local.edge_registry)", guard)
        self.assertIn('local.edge_dns.binding_sha256 == sha256("${local.edge_registry}\\n${jsonencode(local.edge_dns.records)}")', guard)
        self.assertIn("setsubtract(keys(local.edge_dns.records), local.edge_registry_hosts)", guard)
        self.assertIn("setsubtract(local.edge_registry_hosts, keys(local.edge_dns.records))", guard)
        self.assertEqual(guard.count("precondition {"), 4)
        # the same four on every record too: a `-target` plan prunes an output with no resource ancestors
        resource = tf[tf.index('resource "cloudflare_dns_record" "edge"'):]
        for line in [ln.strip() for ln in guard.splitlines() if ln.strip().startswith(("condition", "&& length"))]:
            self.assertIn(line, resource)
        self.assertEqual(resource.count("precondition {"), 5)
        # no second implementation of the mode rule, and no warn-only check block
        for word in ("edge_default", "force_tunnel", "check \""):
            self.assertNotIn(word, tf)


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
            frozen_routes = []
            for r in rules:
                if "path" not in r:
                    continue
                if r["service"].startswith("http_status"):
                    # cloudflared refused these paths outright; the raw-URI guard does that here
                    self.assertTrue(h.get("uri_guard"), f"{host}: deny rule {r['path']} needs uri_guard")
                    continue
                frozen_routes.append((r["path"], r["service"]))
            ours = [(rt["match"]["path_regexp"], h["backend"]["url"] if rt["to"] == "default" else rt["to"])
                    for rt in h.get("routes") or []]
            # the SAME routes in the SAME order: an extra route (say ^/admin/ on id) is a failure
            self.assertEqual(ours, frozen_routes, f"{host}: path routes differ from cloudflared's")
            if not origin.get("httpHostHeader") and "originServerName" not in origin:
                self.assertNotIn("host_header", h["backend"], f"{host}: Host override not in cloudflared")
                if INTENDED.get(name) != "host-sni":
                    self.assertNotIn("tls_server_name", h["backend"], f"{host}: SNI override not in cloudflared")

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
        first = [ln.strip() for ln in route.splitlines()[1:7]]
        self.assertEqual(first, ["respond @unknown_host 404", "respond @cf_client_shape 400", "respond @cf_client_bad 400",
                                 "respond @cf_client_multi 400", "respond @cf_visitor_missing 400",
                                 "redir @cf_visitor_http https://{host}{uri} 308"])

    def test_cf_ip_shape(self):
        import re
        ok = ["203.0.113.7", "2001:db8::7", "::ffff:192.0.2.1", "::1"]
        bad = ["203.0.113.9:443", "fe80::1%eth0", "[2001:db8::1]", "1.2.3.4, 5.6.7.8", "not-an-ip", "1:2", ""]
        for v in ok:
            self.assertTrue(re.match(render.CF_IP_SHAPE, v), v)
        for v in bad:
            self.assertFalse(re.match(render.CF_IP_SHAPE, v), v)

    def test_authelia_runs_before_the_proxy(self):
        direct = self.site("https://:8443 {")
        block = direct[direct.index("handle @h_dsh {"):]
        self.assertLess(block.index("import edge_authelia"), block.index("reverse_proxy"))

    def test_trust_rules_use_the_tcp_peer(self):
        self.assertIn("@not_lan not remote_ip ", self.text)
        self.assertIn("@not_admin not remote_ip ", self.text)
        self.assertNotIn("client_ip ", self.text.replace("{client_ip}", ""))

    def test_source_tiers(self):
        import ipaddress
        lan, admin = render.source_tiers(self.reg)
        inside = lambda ip, nets: any(ipaddress.ip_address(ip) in n for n in nets)  # noqa: E731
        for ip, in_lan, in_admin in [
            ("192.168.0.86", True, True),     # the workstation (DHCP)
            ("192.168.1.200", True, True),    # a DHCP client in the upper half
            ("192.168.50.10", True, True),    # Trusted VLAN
            ("192.168.0.38", True, True),     # the owner's Windows VM, the named exception
            ("192.168.0.8", True, False),     # dev-worker-1: automation
            ("192.168.0.14", True, False),    # ci-runner-1
            ("192.168.0.25", True, False),    # reviewer-2
            ("192.168.0.51", True, False),    # the VIP itself
            ("192.168.0.1", False, False),    # the gateway (NAT loopback)
            ("192.168.0.41", False, False),   # talos-cp1
            ("192.168.2.10", False, False),   # Lab VLAN
            ("192.168.60.10", False, False),  # Guest VLAN
            ("8.8.8.8", False, False),
        ]:
            self.assertEqual(inside(ip, lan), in_lan, ip)
            self.assertEqual(inside(ip, admin), in_admin, ip)

    def test_admin_surfaces_use_the_admin_tier(self):
        direct = self.site("https://:8443 {")
        for name, matcher in (("dsh", "@not_admin"), ("dw1", "@not_admin"), ("proxmox", "@not_admin"),
                              ("router", "@not_lan"), ("git", "@not_lan")):
            block = direct[direct.index(f"handle @h_{name} {{"):]
            block = block[:block.index("\n\t\t}\n")]
            self.assertIn(f"respond {matcher} 403", block, name)

    def test_admin_include_must_be_inside_the_lan(self):
        reg = raw()
        reg["admin_sources"]["include"].append("10.1.2.0/24")
        self.assertTrue(any("not inside the LAN tier" in e for e in render.validate(reg)))

    def test_log_redaction(self):
        # twice each: the site access logs AND the default logger, which carries http.log.error
        for h in render.LOG_REDACT:
            self.assertGreaterEqual(self.text.count(f"request>headers>{h} delete"), 2, h)
        self.assertIn("\tlog default {", self.text)

    def test_https_upstreams_send_the_public_host(self):
        direct = self.site("https://:8443 {")
        for name in ("dw1", "proxmox", "qnap", "openbao"):
            block = direct[direct.index(f"handle @h_{name} {{"):]
            block = block[:block.index("\n\t\t}\n")]
            self.assertIn("header_up Host {host}", block, name)

    def test_sidecar_metrics_have_a_prometheus_content_type(self):
        # Prometheus 3 refuses a blank Content-Type ("non-compliant scrape target"): seen live on
        # 2026-10-27, both sidecar targets down while Caddy's own /metrics was up.
        block = self.text[self.text.index("http://:9180 {"):]
        for path in ("/reloader-metrics", "/probe-metrics"):
            handler = block[block.index(f"handle {path} {{"):]
            handler = handler[:handler.index("\n\t\t}")]
            self.assertIn(f'header Content-Type "{render.PROM_TEXT}"', handler, path)
            self.assertLess(handler.index("header Content-Type"), handler.index("file_server"), path)

    def test_metrics_are_bounded(self):
        # no per-host labels: on a TLS server Caddy would label any Host a WAN client sends
        glob = self.text[:self.text.index("\n}\n")]
        self.assertIn("\n\tmetrics\n", glob)
        self.assertNotIn("per_host", self.text)
        self.assertNotIn("observe_catchall_hosts", self.text)

    def test_forward_auth_gets_the_canonical_client(self):
        snippet = self.text[self.text.index("(edge_authelia) {"):]
        snippet = snippet[:snippet.index("\n}\n")]
        self.assertIn("header_up CF-Connecting-IP {client_ip}", snippet)


if __name__ == "__main__":
    unittest.main()
