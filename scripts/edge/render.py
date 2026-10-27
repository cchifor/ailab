#!/usr/bin/env python3
"""Validate the edge registry and render the edge proxy's Caddyfile from it.

    python3 scripts/edge/render.py            # write the Caddyfile and kubernetes/edge/dns-records.json
    python3 scripts/edge/render.py --check    # fail if either committed output is stale (CI)
    python3 scripts/edge/render.py --validate # schema only

The registry (kubernetes/edge/hostnames.yaml) and the standard (docs/standards/edge-ingress.md)
define the semantics; this file is their one implementation. The proxy listens on:

  :8443  direct  TLS; the VIP's :443 (LAN + the gateway's WAN forward). Client = the TCP peer.
  :8080  tunnel  plain HTTP; only cloudflared reaches it. Client = CF-Connecting-IP.
  :8081  redirect to https, trusted sources only (the VIP's :80).
  :8082  /healthz (503 while /config/drain exists).  :9180 /metrics.

--test-profile FILE renders the same rules with the backends, the auth service and the source
ranges swapped for a docker fixture (scripts/tests/edge_runtime.sh); production never uses it.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import pathlib
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - the runners and the workstation have PyYAML
    sys.exit("render.py needs PyYAML (python3 -m pip install pyyaml)")

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "kubernetes/edge/hostnames.yaml"
OUTPUT = ROOT / "kubernetes/apps/apps/edge/proxy/Caddyfile"
DNS_OUTPUT = ROOT / "kubernetes/edge/dns-records.json"  # consumed by kubernetes/infra/cloudflare/dns.tf

AUTHELIA = "authelia.auth.svc.cluster.local:9091"
CERT = "/certs/tls.crt"
KEY = "/certs/tls.key"
PROM_TEXT = "text/plain; version=0.0.4; charset=utf-8"  # Prometheus text exposition format

TOP_KEYS = {"version", "zone", "edge_default", "force_tunnel", "vip", "tunnel_id", "trusted_sources", "admin_sources",
            "tunnel_trusted_proxies", "hosts"}
HOST_KEYS = {"exposure", "edge", "auth", "authelia_group", "access_gated", "lan_dns", "backend",
             "uri_guard", "routes", "routes_default", "restricted_paths", "smoke"}
REQUIRED_HOST_KEYS = {"exposure", "auth", "access_gated", "lan_dns", "backend", "smoke"}
BACKEND_KEYS = {"url", "respond", "tls_insecure_skip_verify", "tls_ca", "tls_server_name", "host_header"}
SMOKE_KEYS = {"status", "status_any", "location_contains"}
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
# Values interpolated into the Caddyfile: no whitespace, quotes, braces or backticks.
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._:/-]+$")
# A fixed response body: words, '=', and the {remote_host} placeholder only.
SAFE_RESPOND = re.compile(r"^[A-Za-z0-9 =._:-]*(\{remote_host\}[A-Za-z0-9 =._:-]*)?$")

# Refused on hosts with uri_guard, matched on the RAW request target before the query string:
# dot segments, ';' and percent-encoded dots, slashes, backslashes, semicolons and percents (double
# encoding), any case. Caddy matches paths after decoding, the backends may normalise differently;
# refusing the ambiguous forms outright removes the question (id: Keycloak realms, vault: /admin).
URI_GUARD = r"(?i)^[^?]*(\.\.|/\./|/\.$|;|%2e|%2f|%5c|%25|%3b|\\)"
# The tunnel listener's client address comes from CF-Connecting-IP. Caddy parses that header itself
# (client_ip_headers) and falls back to the TCP peer when it is missing or not a valid address, so
# "the client IP equals the peer" is exactly "no valid CF-Connecting-IP" (a visitor can never BE the
# cloudflared pod, so refusing that one address is no loss). Caddy's parser also strips a port or a
# zone, so the raw value must first have the bare shape of one address: a dotted quad, or IPv6
# characters with at least two colons; no port, zone, brackets or list. A comma anywhere in the joined
# header value (several values, or several header lines) is refused separately.
CF_IP_SHAPE = r"^(([0-9]{1,3}\.){3}[0-9]{1,3}|[0-9A-Fa-f]*:[0-9A-Fa-f]*:[0-9A-Fa-f:.]*)$"

# Removed from every request before routing, on both listeners: identity and forwarding headers
# only this proxy may set. The direct listener also drops every Cf-* header (no Cloudflare there).
STRIP_ALWAYS = ["Forwarded", "X-Forwarded-*", "X-Real-Ip", "True-Client-Ip", "Cdn-Loop", "Remote-*"]
STRIP_DIRECT = ["Cf-*"]
# Never logged even though Caddy's default redaction (Authorization, Cookie) does not cover them.
LOG_REDACT = ["Cf-Access-Jwt-Assertion", "Cf-Access-Client-Id", "Cf-Access-Client-Secret",
              "Cf-Access-Authenticated-User-Email", "X-Api-Key"]


class RegistryError(Exception):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    """safe_load, except a key repeated in one mapping is an error instead of last-one-wins: a
    second `router:` entry or a second `exposure:` must never silently replace the first."""


def _construct_unique_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise RegistryError(f"duplicate key {key!r} (line {key_node.start_mark.line + 1})")
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return load_yaml_text(f.read())


def load_yaml_text(text: str):
    return yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass


# --------------------------------------------------------------------------------------------
# validation


def _cidr(value, where, errors):
    try:
        return ipaddress.ip_network(value, strict=True)
    except (ValueError, TypeError):
        errors.append(f"{where}: {value!r} is not a CIDR")
        return None


def validate(reg: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(reg, dict):
        return ["registry: not a mapping"]
    for key in sorted(set(reg) - TOP_KEYS):
        errors.append(f"registry: unknown key {key!r}")
    for key in sorted(TOP_KEYS - set(reg)):
        errors.append(f"registry: missing key {key!r}")
    if errors:
        return errors
    if reg["version"] != 1:
        errors.append("registry: version must be 1")
    if reg["edge_default"] not in ("direct", "tunnel"):
        errors.append("edge_default: must be direct or tunnel")
    if not isinstance(reg["force_tunnel"], bool):
        errors.append("force_tunnel: must be a bool")
    try:
        vip = ipaddress.ip_address(reg["vip"])
    except ValueError:
        errors.append(f"vip: {reg['vip']!r} is not an address")
        vip = None
    ts = reg["trusted_sources"] or {}
    if set(ts) != {"allow", "exclude"}:
        errors.append("trusted_sources: needs exactly allow and exclude")
    allow = [_cidr(c, "trusted_sources.allow", errors) for c in ts.get("allow") or []]
    for c in ts.get("exclude") or []:
        _cidr(c, "trusted_sources.exclude", errors)
    if not allow:
        errors.append("trusted_sources.allow: empty")
    if vip and any(n and vip in n for n in [ipaddress.ip_network(c) for c in ts.get("exclude") or []
                                             if _ok_cidr(c)]):
        errors.append("vip: must not be an excluded source")
    adm = reg["admin_sources"] or {}
    if set(adm) != {"exclude", "include"}:
        errors.append("admin_sources: needs exactly exclude and include")
    for c in (adm.get("exclude") or []) + (adm.get("include") or []):
        _cidr(c, "admin_sources", errors)
    if not errors:
        lan, admin = source_tiers(reg)
        if not admin:
            errors.append("admin_sources: the admin tier is empty")
        for c in adm.get("include") or []:
            if not any(ipaddress.ip_network(c).subnet_of(n) for n in lan):
                errors.append(f"admin_sources.include: {c} is not inside the LAN tier")
    for c in reg["tunnel_trusted_proxies"] or []:
        _cidr(c, "tunnel_trusted_proxies", errors)
    if not reg["tunnel_trusted_proxies"]:
        errors.append("tunnel_trusted_proxies: empty")

    hosts = reg["hosts"]
    if not isinstance(hosts, dict) or not hosts:
        return errors + ["hosts: empty or not a mapping"]
    for name, h in hosts.items():
        where = f"hosts.{name}"
        if not isinstance(name, str) or not NAME_RE.match(name):
            errors.append(f"{where}: invalid DNS label")
            continue
        if not isinstance(h, dict):
            errors.append(f"{where}: not a mapping")
            continue
        for key in sorted(set(h) - HOST_KEYS):
            errors.append(f"{where}: unknown key {key!r}")
        for key in sorted(REQUIRED_HOST_KEYS - set(h)):
            errors.append(f"{where}: missing {key!r}")
        if REQUIRED_HOST_KEYS - set(h):
            continue
        if h["exposure"] not in ("public", "private"):
            errors.append(f"{where}.exposure: public or private")
        if h["exposure"] == "public" and h.get("edge") not in ("default", "direct", "tunnel"):
            errors.append(f"{where}.edge: default, direct or tunnel (required for public hosts)")
        if h["exposure"] == "private" and "edge" in h:
            errors.append(f"{where}.edge: private hosts are never in the tunnel; drop edge")
        if h["auth"] not in ("native", "authelia", "none"):
            errors.append(f"{where}.auth: native, authelia or none")
        if h["auth"] == "authelia" and not h.get("authelia_group"):
            errors.append(f"{where}: auth authelia needs authelia_group")
        if h["auth"] != "authelia" and "authelia_group" in h:
            errors.append(f"{where}: authelia_group without auth authelia")
        if not isinstance(h["access_gated"], bool) or not isinstance(h["lan_dns"], bool):
            errors.append(f"{where}: access_gated and lan_dns must be bools")
        if h["exposure"] == "private" and h["access_gated"]:
            errors.append(f"{where}: a private host is not access_gated")
        if (h["exposure"] == "public" and h["access_gated"] and h["auth"] != "authelia"
                and effective_mode(reg, name) == "direct"):
            errors.append(f"{where}: access_gated host cannot be direct without auth authelia "
                          "(Cloudflare Access is its only per-person gate)")
        b = h["backend"]
        if not isinstance(b, dict):
            errors.append(f"{where}.backend: not a mapping")
        else:
            for key in sorted(set(b) - BACKEND_KEYS):
                errors.append(f"{where}.backend: unknown key {key!r}")
            if ("url" in b) == ("respond" in b):
                errors.append(f"{where}.backend: exactly one of url or respond")
            if "url" in b and not re.match(r"^https?://[A-Za-z0-9.-]+(:[0-9]+)?$", str(b["url"])):
                errors.append(f"{where}.backend.url: scheme://host[:port] only, got {b['url']!r}")
            if "respond" in b and h["auth"] != "none":
                errors.append(f"{where}: a fixed respond backend must be auth none")
            if "respond" in b and not SAFE_RESPOND.match(str(b["respond"])):
                errors.append(f"{where}.backend.respond: words, '=' and {{remote_host}} only")
            if "tls_insecure_skip_verify" in b and not isinstance(b["tls_insecure_skip_verify"], bool):
                errors.append(f"{where}.backend.tls_insecure_skip_verify: must be a bool, not {b['tls_insecure_skip_verify']!r}")
            for k in ("tls_ca", "tls_server_name", "host_header"):
                if k in b and not (isinstance(b[k], str) and SAFE_TOKEN.match(b[k])):
                    errors.append(f"{where}.backend.{k}: {b[k]!r} is not a plain token")
            if any(k.startswith("tls_") for k in b) and not str(b.get("url", "")).startswith("https://"):
                errors.append(f"{where}.backend: tls_* options need an https url")
        if h["auth"] == "none" and h["exposure"] == "private" and not (isinstance(b, dict) and "respond" in b):
            errors.append(f"{where}: auth none on a private host is only for a fixed respond backend")
        if h["auth"] == "none" and h["access_gated"]:
            errors.append(f"{where}: an access_gated host needs authelia or native auth: in tunnel mode the "
                          "direct listener serves it to every trusted source with no Access in front")
        routes = h.get("routes")
        if routes is not None:
            if not isinstance(routes, list) or not routes:
                errors.append(f"{where}.routes: non-empty list")
            else:
                for i, r in enumerate(routes):
                    if set(r) != {"match", "to"} or set(r["match"]) != {"path_regexp"}:
                        errors.append(f"{where}.routes[{i}]: {{match: {{path_regexp}}, to}}")
                        continue
                    _regex(r["match"]["path_regexp"], f"{where}.routes[{i}]", errors)
                    if r["to"] != "default" and not re.match(r"^https?://[A-Za-z0-9.-]+(:[0-9]+)?$", str(r["to"])):
                        errors.append(f"{where}.routes[{i}].to: default or scheme://host[:port]")
            if h.get("routes_default") not in ("backend", "404"):
                errors.append(f"{where}.routes_default: backend or 404")
        elif "routes_default" in h:
            errors.append(f"{where}: routes_default without routes")
        for i, rp in enumerate(h.get("restricted_paths") or []):
            if set(rp) != {"path_regexp", "exposure"} or rp["exposure"] not in ("access_gated", "private"):
                errors.append(f"{where}.restricted_paths[{i}]: {{path_regexp, exposure: access_gated|private}}")
                continue
            _regex(rp["path_regexp"], f"{where}.restricted_paths[{i}]", errors)
        if "uri_guard" in h and not isinstance(h["uri_guard"], bool):
            errors.append(f"{where}.uri_guard: bool")
        s = h["smoke"]
        if not isinstance(s, dict) or set(s) - SMOKE_KEYS or ("status" in s) == ("status_any" in s):
            errors.append(f"{where}.smoke: status or status_any, optional location_contains")
        elif "status" in s and not isinstance(s["status"], int) or                 "status_any" in s and not all(isinstance(x, int) for x in s["status_any"]):
            errors.append(f"{where}.smoke: statuses must be integers")
    return errors


def _ok_cidr(c) -> bool:
    try:
        ipaddress.ip_network(c, strict=True)
        return True
    except (ValueError, TypeError):
        return False


def _regex(value, where, errors):
    try:
        re.compile(value)
    except (re.error, TypeError) as e:
        errors.append(f"{where}: bad regex {value!r}: {e}")
    if "`" in str(value):
        errors.append(f"{where}: backticks are not allowed in regexes")


def _subtract(nets, minus):
    """CIDRs covering `nets` without `minus`. CIDRs either nest or are disjoint, so each removal
    either drops a network, splits it (address_exclude), or leaves it alone."""
    out = list(nets)
    for m in minus:
        nxt = []
        for n in out:
            if n.version != m.version or not n.overlaps(m):
                nxt.append(n)
            elif m.supernet_of(n):
                continue
            else:
                nxt.extend(n.address_exclude(m))
        out = nxt
    return out


def source_tiers(reg, profile=None):
    """The two source tiers as explicit CIDR lists (docs/standards/edge-ingress.md 5.3):
    LAN   = trusted_sources.allow - trusted_sources.exclude
    admin = LAN - admin_sources.exclude + admin_sources.include"""
    if profile:
        allow, exclude = profile["allow"], profile["exclude"]
        admin_ex, admin_in = profile["admin_exclude"], profile.get("admin_include", [])
    else:
        allow, exclude = reg["trusted_sources"]["allow"], reg["trusted_sources"]["exclude"]
        admin_ex, admin_in = reg["admin_sources"]["exclude"], reg["admin_sources"]["include"]
    net = lambda xs: [ipaddress.ip_network(x) for x in xs]  # noqa: E731
    lan = sorted(ipaddress.collapse_addresses(_subtract(net(allow), net(exclude))))
    admin = sorted(ipaddress.collapse_addresses(_subtract(lan, net(admin_ex)) + net(admin_in)))
    return lan, admin


def effective_mode(reg: dict, name: str) -> str:
    """direct | tunnel for public hosts, private for private ones."""
    h = reg["hosts"][name]
    if h["exposure"] == "private":
        return "private"
    if reg["force_tunnel"]:
        return "tunnel"
    return reg["edge_default"] if h.get("edge", "default") == "default" else h["edge"]


def load(path: pathlib.Path = REGISTRY, text: str | None = None) -> dict:
    """The validated registry. With `text`, that exact text (main() hashes the same snapshot)."""
    reg = load_yaml_text(text) if text is not None else load_yaml(path)
    errors = validate(reg)
    if errors:
        raise RegistryError("\n".join(errors))
    return reg


def registry_digest(text: str) -> str:
    """sha256 of the registry text with CRs removed. dns.tf computes the same value with
    sha256(replace(file(...), "\\r", "")) and refuses records rendered from any other registry."""
    return hashlib.sha256(text.replace("\r", "").encode("utf-8")).hexdigest()


def dns_records(reg: dict) -> dict:
    """The DNS record each host's effective mode needs (docs/standards/edge-ingress.md section 4).
    dns.tf creates exactly these; it does not re-derive modes, so the proxy and DNS cannot disagree."""
    tunnel = f"{reg['tunnel_id']}.cfargotunnel.com"
    out = {}
    for name in sorted(reg["hosts"]):
        mode = effective_mode(reg, name)
        if mode == "private":
            rec = {"type": "A", "content": reg["vip"], "proxied": False, "ttl": 60}
        elif mode == "tunnel":
            rec = {"type": "CNAME", "content": tunnel, "proxied": True, "ttl": 1}  # 1 = automatic, required while proxied
        else:
            rec = {"type": "CNAME", "content": reg["zone"], "proxied": False, "ttl": 60}
        out[name] = {"mode": mode, **rec}
    return out


def records_binding(registry_text: str, records: dict) -> str:
    """sha256 over the registry (CRs removed) and the records, which dns.tf recomputes as
    sha256("${registry}\n${jsonencode(records)}"): a record edited, added or dropped by hand, with the
    registry hash left intact, no longer matches. jsonencode writes compact JSON with sorted keys and
    escapes <, > and & (Go's encoder); these values never carry those, which is asserted."""
    canon = json.dumps(records, sort_keys=True, separators=(",", ":"))
    if any(c in canon for c in "<>&"):
        raise RegistryError("dns records contain <, > or &, which jsonencode would escape differently")
    return hashlib.sha256((registry_text.replace("\r", "") + "\n" + canon).encode("utf-8")).hexdigest()


def render_dns(reg: dict, registry_text: str) -> str:
    records = dns_records(reg)
    doc = {
        "_generated": "by scripts/edge/render.py from kubernetes/edge/hostnames.yaml; do not edit, run `just edge-render`",
        "registry_sha256": registry_digest(registry_text),
        "binding_sha256": records_binding(registry_text, records),
        "zone": reg["zone"],
        "tunnel_id": reg["tunnel_id"],
        "vip": reg["vip"],
        "records": records,
    }
    return json.dumps(doc, indent=2) + "\n"


# --------------------------------------------------------------------------------------------
# rendering


def _fqdn(reg, name):
    return f"{name}.{reg['zone']}"


def _peer_matchers(lan, admin):
    return [
        "\t@not_lan not remote_ip " + " ".join(str(n) for n in lan),
        "\t@not_admin not remote_ip " + " ".join(str(n) for n in admin),
    ]


def _proxy(url, backend, profile, indent):
    """A reverse_proxy block for one upstream, with the header policy."""
    pad = "\t" * indent
    target = url
    if profile:
        target = profile["echo"]
    lines = [f"{pad}reverse_proxy {target} {{", f"{pad}\timport edge_upstream"]
    if profile:
        lines.append(f"{pad}\theader_up X-Test-Backend {url}")
    if backend.get("host_header"):
        lines.append(f"{pad}\theader_up Host {backend['host_header']}")
    elif url.startswith("https://"):
        # Caddy rewrites Host to the upstream's address for HTTPS upstreams unless told otherwise; the
        # backends expect the public name, as cloudflared sent it (and the dw gate requires it).
        lines.append(f"{pad}\theader_up Host {{host}}")
    tls = [k for k in ("tls_insecure_skip_verify", "tls_ca", "tls_server_name") if backend.get(k)]
    if profile and url.startswith("https://"):
        # the fixture's HTTPS echo: the same Host/SNI semantics, only the address and trust swapped
        lines[0] = f"{pad}reverse_proxy {profile['echo_tls']} {{"
        lines.append(f"{pad}\ttransport http {{")
        lines.append(f"{pad}\t\ttls_insecure_skip_verify")
        if backend.get("tls_server_name"):
            lines.append(f"{pad}\t\ttls_server_name {backend['tls_server_name']}")
        lines.append(f"{pad}\t}}")
    elif tls and not profile and url == backend.get("url"):
        lines.append(f"{pad}\ttransport http {{")
        if backend.get("tls_insecure_skip_verify"):
            lines.append(f"{pad}\t\ttls_insecure_skip_verify")
        if backend.get("tls_ca"):
            lines.append(f"{pad}\t\ttls_trust_pool file {backend['tls_ca']}")
        if backend.get("tls_server_name"):
            lines.append(f"{pad}\t\ttls_server_name {backend['tls_server_name']}")
        lines.append(f"{pad}\t}}")
    lines.append(f"{pad}}}")
    return lines


def _host_body(reg, name, listener, profile):
    """The ordered handlers for one host on one listener (inside its handle block)."""
    h = reg["hosts"][name]
    mode = effective_mode(reg, name)
    b = h["backend"]
    out = ["\t\t\troute {"]
    add = out.append

    # 1. who may reach the host at all on this listener: admin surfaces (private hosts, Access-gated
    #    hosts still in the tunnel) the admin tier; other tunnel-mode hosts the LAN tier
    if listener == "direct" and (mode == "private" or (mode == "tunnel" and h["access_gated"])):
        add("\t\t\t\trespond @not_admin 403")
    elif listener == "direct" and mode == "tunnel":
        add("\t\t\t\trespond @not_lan 403")
    # 2. the raw-URI guard
    if h.get("uri_guard"):
        add("\t\t\t\trespond @uri_unsafe 404")
    # 3. restricted paths, before the public route
    for i, rp in enumerate(h.get("restricted_paths") or []):
        m = f"@rp_{_ident(name)}_{i}"
        if listener == "direct":
            add(f"\t\t\t\trespond {m}_not_admin 403")
        elif rp["exposure"] == "private":
            add(f"\t\t\t\trespond {m} 404")
        # tunnel + access_gated: Cloudflare Access's path application gates it at the edge
    # 4. authentication, before any proxying
    if h["auth"] == "authelia":
        add("\t\t\t\timport edge_authelia")
    # 5. the backend(s)
    if "respond" in b:
        add(f"\t\t\t\trespond \"{b['respond']}\" 200")
    elif h.get("routes"):
        for i, r in enumerate(h["routes"]):
            url = b["url"] if r["to"] == "default" else r["to"]
            be = b if r["to"] == "default" else {"url": url}
            add(f"\t\t\t\thandle @rt_{_ident(name)}_{i} {{")
            out.extend(_proxy(url, be, profile, 5))
            add("\t\t\t\t}")
        add("\t\t\t\thandle {")
        if h["routes_default"] == "404":
            add("\t\t\t\t\trespond 404")
        else:
            out.extend(_proxy(b["url"], b, profile, 5))
        add("\t\t\t\t}")
    else:
        out.extend(_proxy(b["url"], b, profile, 4))
    add("\t\t\t}")
    return out


def _ident(name):
    return name.replace("-", "_")


def _site_matchers(reg, names, lan, admin):
    lines = _peer_matchers(lan, admin)
    lines.append(f"\t@uri_unsafe vars_regexp {{http.request.orig_uri}} `{URI_GUARD}`")
    for name in names:
        h = reg["hosts"][name]
        lines.append(f"\t@h_{_ident(name)} host {_fqdn(reg, name)}")
        for i, r in enumerate(h.get("routes") or []):
            lines.append(f"\t@rt_{_ident(name)}_{i} path_regexp `{r['match']['path_regexp']}`")
        for i, rp in enumerate(h.get("restricted_paths") or []):
            m = f"@rp_{_ident(name)}_{i}"
            rx = f"(?i){rp['path_regexp']}" if not rp["path_regexp"].startswith("(?i)") else rp["path_regexp"]
            lines.append(f"\t{m} path_regexp `{rx}`")
            lines.append(f"\t{m}_not_admin {{")
            lines.append(f"\t\tpath_regexp `{rx}`")
            lines.append("\t\tnot remote_ip " + " ".join(str(n) for n in admin))
            lines.append("\t}")
    return lines


def render(reg: dict, profile: dict | None = None) -> str:
    """The Caddyfile for the registry. `profile` swaps backends/ranges for the docker fixture."""
    lan, admin = source_tiers(reg, profile)
    tunnel_trusted = list(reg["tunnel_trusted_proxies"])
    authelia = AUTHELIA
    if profile:
        tunnel_trusted, authelia = profile["tunnel_trusted"], profile["authelia"]
    names = sorted(reg["hosts"])
    public = [n for n in names if reg["hosts"][n]["exposure"] == "public"]

    o: list[str] = []
    w = o.append
    w("# GENERATED by scripts/edge/render.py from kubernetes/edge/hostnames.yaml. Do not edit:")
    w("# change the registry and run `just edge-render`. Semantics: docs/standards/edge-ingress.md.")
    if profile:
        w("# TEST PROFILE: backends, auth and source ranges replaced for the docker fixture.")
    w("{")
    w("\tadmin localhost:2019")
    w("\tlog default {")
    w("\t\toutput stdout")
    w("\t\tformat filter {")
    w("\t\t\twrap json")
    w("\t\t\tfields {")
    w("\t\t\t\trequest>uri regexp `\\?.*$` `?REDACTED`")
    for hname in LOG_REDACT:
        w(f"\t\t\t\trequest>headers>{hname} delete")
    w("\t\t\t}")
    w("\t\t}")
    w("\t}")
    w("\tauto_https off")
    w("\tgrace_period 90s")
    w("\tmetrics {")
    w("\t\tper_host")
    w("\t}")
    w("\tservers :8443 {")
    w("\t\tname direct")
    w("\t\tprotocols h1 h2")
    w("\t\ttimeouts {")
    w("\t\t\tread_header 10s")
    w("\t\t\tidle 5m")
    w("\t\t}")
    w("\t\tmax_header_size 64KB")
    w("\t}")
    w("\tservers :8080 {")
    w("\t\tname tunnel")
    w("\t\tprotocols h1")
    w("\t\ttrusted_proxies static " + " ".join(tunnel_trusted))
    w("\t\tclient_ip_headers CF-Connecting-IP")
    w("\t\ttimeouts {")
    w("\t\t\tread_header 10s")
    w("\t\t\tidle 5m")
    w("\t\t}")
    w("\t\tmax_header_size 64KB")
    w("\t}")
    w("\tservers :8081 {")
    w("\t\tname redirect")
    w("\t\tprotocols h1")
    w("\t\ttimeouts {")
    w("\t\t\tread_header 10s")
    w("\t\t\tidle 1m")
    w("\t\t}")
    w("\t}")
    w("}")
    w("")
    # snippets
    w("(edge_log) {")
    w("\tlog {")
    w("\t\toutput stdout")
    w("\t\tformat filter {")
    w("\t\t\twrap json")
    w("\t\t\tfields {")
    w("\t\t\t\trequest>uri regexp `\\?.*$` `?REDACTED`")
    for hname in LOG_REDACT:
        w(f"\t\t\t\trequest>headers>{hname} delete")
    w("\t\t\t}")
    w("\t\t}")
    w("\t}")
    w("}")
    w("")
    w("(edge_upstream) {")
    w("\theader_up CF-Connecting-IP {client_ip}")
    w("\theader_up X-Real-IP {client_ip}")
    w("\theader_up X-Forwarded-For {client_ip}")
    w("\theader_up X-Forwarded-Proto https")
    w("\theader_up X-Forwarded-Host {host}")
    w("\tstream_close_delay 5m")
    w("}")
    w("")
    w("(edge_authelia) {")
    w(f"\tforward_auth {authelia} {{")
    w("\t\turi /api/authz/forward-auth")
    w("\t\tcopy_headers Remote-User Remote-Groups Remote-Email Remote-Name")
    w("\t\theader_up CF-Connecting-IP {client_ip}")
    w("\t\theader_up X-Forwarded-Proto https")
    w("\t\theader_up X-Forwarded-Host {host}")
    w("\t\theader_up X-Forwarded-For {client_ip}")
    w("\t\theader_up X-Real-IP {client_ip}")
    w("\t}")
    w("}")
    w("")

    # :8443 direct
    w("https://:8443 {")
    w(f"\ttls {CERT} {KEY}")
    w("\timport edge_log")
    o.extend(_site_matchers(reg, names, lan, admin))
    w("\troute {")
    for hdr in STRIP_ALWAYS + STRIP_DIRECT:
        w(f"\t\trequest_header -{hdr}")
    w("\t\theader -Server")
    for name in names:
        w(f"\t\thandle @h_{_ident(name)} {{")
        o.extend(_host_body(reg, name, "direct", profile))
        w("\t\t}")
    w("\t\thandle {")
    w("\t\t\trespond 404")
    w("\t\t}")
    w("\t}")
    w("}")
    w("")

    # :8080 tunnel (public hosts only)
    w("http://:8080 {")
    w("\timport edge_log")
    o.extend(_site_matchers(reg, public, lan, admin))
    w("\t@unknown_host not host " + " ".join(_fqdn(reg, n) for n in public))
    w("\t@cf_client_bad expression `{client_ip} == {remote_host}`")
    w(f"\t@cf_client_shape not vars_regexp {{http.request.header.CF-Connecting-IP}} `{CF_IP_SHAPE}`")
    w("\t@cf_client_multi vars_regexp {http.request.header.CF-Connecting-IP} `,`")
    w("\t@cf_visitor_missing not header Cf-Visitor *")
    w("\t@cf_visitor_http header_regexp Cf-Visitor `\"scheme\"\\s*:\\s*\"http\"`")
    w("\troute {")
    w("\t\trespond @unknown_host 404")
    w("\t\trespond @cf_client_shape 400")
    w("\t\trespond @cf_client_bad 400")
    w("\t\trespond @cf_client_multi 400")
    w("\t\trespond @cf_visitor_missing 400")
    w("\t\tredir @cf_visitor_http https://{host}{uri} 308")
    for hdr in STRIP_ALWAYS:
        w(f"\t\trequest_header -{hdr}")
    w("\t\theader -Server")
    for name in public:
        w(f"\t\thandle @h_{_ident(name)} {{")
        o.extend(_host_body(reg, name, "tunnel", profile))
        w("\t\t}")
    w("\t\thandle {")
    w("\t\t\trespond 404")
    w("\t\t}")
    w("\t}")
    w("}")
    w("")

    # :8081 redirect (trusted sources only)
    w("http://:8081 {")
    w("\timport edge_log")
    o.extend(_peer_matchers(lan, admin))
    w("\t@unknown_host not host " + " ".join(_fqdn(reg, n) for n in names))
    w("\troute {")
    w("\t\trespond @unknown_host 404")
    w("\t\trespond @not_lan 403")
    w("\t\tredir https://{host}{uri} 308")
    w("\t}")
    w("}")
    w("")
    w("http://:8082 {")
    w("\t@draining file {")
    w("\t\troot /config")
    w("\t\ttry_files drain shutdown")
    w("\t}")
    w("\troute {")
    w("\t\trespond @draining \"draining\" 503")
    w("\t\trespond /healthz \"ok\" 200")
    w("\t\trespond 404")
    w("\t}")
    w("}")
    w("")
    # The sidecars' metrics are files; file_server sends no Content-Type for a name without an
    # extension, and Prometheus 3 refuses such a scrape, so set the text exposition type.
    w("http://:9180 {")
    w("\troute {")
    w("\t\thandle /reloader-metrics {")
    w("\t\t\troot * /config/reloader")
    w("\t\t\trewrite * /metrics")
    w(f"\t\t\theader Content-Type \"{PROM_TEXT}\"")
    w("\t\t\tfile_server")
    w("\t\t}")
    w("\t\thandle /probe-metrics {")
    w("\t\t\troot * /config/reloader")
    w("\t\t\trewrite * /probe")
    w(f"\t\t\theader Content-Type \"{PROM_TEXT}\"")
    w("\t\t\tfile_server")
    w("\t\t}")
    w("\t\tmetrics /metrics")
    w("\t}")
    w("}")
    return "\n".join(o) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--registry", type=pathlib.Path, default=REGISTRY)
    ap.add_argument("--out", type=pathlib.Path, default=OUTPUT)
    ap.add_argument("--dns-out", type=pathlib.Path, default=DNS_OUTPUT)
    ap.add_argument("--check", action="store_true", help="fail if --out or --dns-out differs from the render")
    ap.add_argument("--validate", action="store_true", help="validate the registry only")
    ap.add_argument("--test-profile", type=pathlib.Path, help="docker fixture overrides (JSON)")
    args = ap.parse_args(argv)
    try:
        # ONE read: dns-records.json carries the hash of exactly the text that was validated and
        # rendered, so an editor saving the file mid-render cannot pair old records with a new hash.
        registry_text = args.registry.read_bytes().decode("utf-8")
        reg = load(args.registry, registry_text)
    except RegistryError as e:
        print(f"edge registry invalid:\n{e}", file=sys.stderr)
        return 1
    if args.validate:
        print(f"edge registry ok: {len(reg['hosts'])} hosts")
        return 0
    profile = None
    if args.test_profile is not None:
        profile = json.loads(args.test_profile.read_text())
        if not isinstance(profile, dict) or not profile:
            print(f"{args.test_profile}: a test profile must be a non-empty JSON object", file=sys.stderr)
            return 1
    outputs = [(args.out, render(reg, profile))]
    if args.test_profile is None:  # a fixture render never touches the production DNS file
        outputs.append((args.dns_out, render_dns(reg, registry_text)))
    if args.check:
        stale = [p for p, text in outputs if (p.read_text(encoding="utf-8") if p.exists() else "") != text]
        for p in stale:
            print(f"{p} is stale: run `just edge-render`", file=sys.stderr)
        if stale:
            return 1
        for p, _ in outputs:
            print(f"{p} is up to date")
        return 0
    for p, text in outputs:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
