#!/usr/bin/env python3
"""Reconcile the home gateway (UniFi) with the edge's desired state (ADR 0040).

    python3 scripts/edge/gateway.py plan  [--only dhcp|dns|forward] [--adopt KEY] [--forget KEY]
                                          [--skip-vip-check] [--ack-dhcp-leases]
    python3 scripts/edge/gateway.py apply --plan-hash HASH [same options as the plan]
    python3 scripts/edge/gateway.py fingerprint      # the controller certificate's SHA-256

Desired state: kubernetes/edge/gateway.yaml (the gateway contract) and kubernetes/edge/hostnames.yaml
(the hosts with lan_dns: true, the VIP). Owned objects: kubernetes/edge/gateway-state.yaml.

Guarantees (docs/standards/edge-ingress.md section 6, plans/2026-10-27-edge-ingress-plan.md section 3):
  - Ownership: only objects whose IDs are in the state file are changed, and only while they still carry
    the identity recorded with the ID (a DNS record's key, the forward's name, the network named in
    gateway.yaml): a reused or renamed ID stops the run. The DHCP network is owned like the rest. An
    unowned object in the way aborts; --adopt takes one over explicitly. An owned object that vanished
    or changed identity aborts; --forget drops it from the state.
  - Collisions: a duplicate DNS key, a second forward with our name, or any other enabled forward on
    the WAN port aborts, owned or not, at plan time and again from a fresh read before each mutation.
  - Fail closed: every listing is validated (classic `meta.rc == "ok"`, no paging metadata, a list; v2
    a list); anything partial or unexpected aborts before a plan exists, so "absent" is never inferred
    from a bad read.
  - Plan binding: the hash covers the whole inventory read, the operations and every option (including
    --skip-vip-check and --ack-dhcp-leases), and apply recomputes it and aborts on any difference. Each
    mutation re-reads its object first and checks it is unchanged since the plan, and is verified by a
    read afterwards.
  - The WAN: any change that leaves an enabled forward to the VIP needs the VIP to serve the canary's
    own answer first (a CA-valid certificate for edge-canary and its fixed response, not just any 200).
  - DHCP: a range change that would take back an address an active client holds (not as a fixed IP)
    is refused unless --ack-dhcp-leases; the check runs at plan time and again before the write.
  - Crash safety: a create writes an intent (with the fields it sends) to the state file BEFORE the API
    call and replaces it with the returned ID after; an intent left behind stops the next run until it
    is adopted (only an object holding those fields) or forgotten.
  - TLS is checked against the pinned certificate fingerprint, never skipped.
  - DDNS is checked, never changed. Secrets (x_* fields, the API key) are never printed or stored.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import http.client
import io
import ipaddress
import json
import os
import pathlib
import socket
import ssl
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import render  # noqa: E402
import yaml  # noqa: E402  (render.py already requires PyYAML)

ROOT = pathlib.Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "kubernetes/edge/gateway.yaml"
STATE = ROOT / "kubernetes/edge/gateway-state.yaml"
BACKUPS = pathlib.Path.home() / ".ailab-edge" / "backups"
KINDS = ("dhcp", "dns", "forward")


class GatewayError(Exception):
    pass


# --------------------------------------------------------------------------------------------
# the controller API


def _scrub(obj):
    """Drop secret-bearing fields (UniFi prefixes them x_) before hashing, printing or backing up."""
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items() if not k.startswith("x_") and k != "login"}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def digest(obj) -> str:
    return hashlib.sha256(json.dumps(_scrub(obj), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class UnifiClient:
    """UniFi Network application API over TLS pinned to one certificate."""

    def __init__(self, url: str, site: str, cert_sha256: str, key: str, timeout: int = 20):
        if not url.startswith("https://"):
            raise GatewayError("controller.url must be https")
        self.host = url[len("https://"):].rstrip("/")
        self.site = site
        self.pin = cert_sha256.lower()
        self.key = key
        self.timeout = timeout

    def _connect(self):
        ctx = ssl.create_default_context()
        # The Cloud Key's certificate is self-signed by UniFi OS: no CA can vouch for it, so trust is
        # the pinned fingerprint, checked below before a single request byte is sent. Every request
        # opens its own connection (and checks the pin); redirects are never followed.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        host, _, port = self.host.partition(":")
        conn = http.client.HTTPSConnection(host, int(port or 443), timeout=self.timeout, context=ctx)
        conn.connect()
        got = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
        if got != self.pin:
            conn.close()
            raise GatewayError(f"controller certificate {got} does not match the pinned {self.pin}")
        return conn

    def request(self, method: str, path: str, body=None):
        conn = self._connect()
        try:
            headers = {"X-API-KEY": self.key, "Accept": "application/json"}
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            conn.close()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return resp.status, payload

    # ---- paths
    def _classic(self, rest):
        return f"/proxy/network/api/s/{self.site}/rest/{rest}"

    def _stat(self, rest):
        return f"/proxy/network/api/s/{self.site}/stat/{rest}"

    def _v2(self, rest):
        return f"/proxy/network/v2/api/site/{self.site}/{rest}"

    @staticmethod
    def _classic_list(status, payload, what):
        meta = payload.get("meta") if isinstance(payload, dict) else None
        if status != 200 or not isinstance(meta, dict) or meta.get("rc") != "ok" \
                or not isinstance(payload.get("data"), list):
            raise GatewayError(f"{what}: unexpected answer (HTTP {status}); refusing to plan on a partial read")
        # The classic API answers whole collections ({"meta": {"rc": "ok"}, "data": [...]}, seen live
        # 2026-10-27). Anything else (a count, an offset, a cursor) would mean a page, and a page can make
        # an owned object look absent, so it is refused rather than interpreted.
        extra = sorted((set(meta) - {"rc", "msg"}) | (set(payload) - {"meta", "data"}))
        if extra:
            raise GatewayError(f"{what}: unexpected metadata {extra} (a paged answer?); refusing a possibly "
                               "partial list")
        return payload["data"]

    @staticmethod
    def _v2_list(status, payload, what):
        if status != 200 or not isinstance(payload, list):
            raise GatewayError(f"{what}: unexpected answer (HTTP {status}); refusing to plan on a partial read")
        return payload

    def inventory(self) -> dict:
        inv = {
            "portforward": self._classic_list(*self.request("GET", self._classic("portforward")), "port forwards"),
            "networkconf": self._classic_list(*self.request("GET", self._classic("networkconf")), "networks"),
            "dynamicdns": self._classic_list(*self.request("GET", self._classic("dynamicdns")), "dynamic DNS"),
            "static_dns": self._v2_list(*self.request("GET", self._v2("static-dns")), "static DNS"),
        }
        for kind, rows in inv.items():
            for r in rows:
                if not isinstance(r, dict) or not r.get("_id"):
                    raise GatewayError(f"{kind}: an entry without an _id; refusing to plan")
        return inv

    def active_clients(self) -> list:
        """Connected clients (ip, network_id, use_fixedip, fixed_ip). Volatile, so never hashed: read
        for the DHCP lease check at plan time and again right before the write."""
        return self._classic_list(*self.request("GET", self._stat("sta")), "active clients")

    # ---- mutations; each returns the object the controller now holds
    def create_portforward(self, fields):
        st, p = self.request("POST", self._classic("portforward"), fields)
        rows = self._classic_list(st, p, "create port forward")
        if len(rows) != 1:
            raise GatewayError("create port forward: no object returned")
        return rows[0]

    def update_portforward(self, oid, obj):
        st, p = self.request("PUT", self._classic(f"portforward/{oid}"), obj)
        return self._classic_list(st, p, "update port forward")[0]

    def delete_portforward(self, oid):
        st, p = self.request("DELETE", self._classic(f"portforward/{oid}"))
        self._classic_list(st, p, "delete port forward")

    def update_network(self, oid, obj):
        st, p = self.request("PUT", self._classic(f"networkconf/{oid}"), obj)
        return self._classic_list(st, p, "update network")[0]

    def create_dns(self, fields):
        st, p = self.request("POST", self._v2("static-dns"), fields)
        if st != 200 or not isinstance(p, dict) or not p.get("_id"):
            raise GatewayError(f"create static DNS: unexpected answer (HTTP {st})")
        return p

    def update_dns(self, oid, obj):
        st, p = self.request("PUT", self._v2(f"static-dns/{oid}"), obj)
        if st != 200 or not isinstance(p, dict):
            raise GatewayError(f"update static DNS: unexpected answer (HTTP {st})")
        return p

    def delete_dns(self, oid):
        st, _ = self.request("DELETE", self._v2(f"static-dns/{oid}"))
        if st not in (200, 204):
            raise GatewayError(f"delete static DNS: unexpected answer (HTTP {st})")


def fingerprint(url: str) -> str:
    host, _, port = url[len("https://"):].rstrip("/").partition(":")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, int(port or 443)), timeout=10) as s:
        with ctx.wrap_socket(s, server_hostname=host) as t:
            return hashlib.sha256(t.getpeercert(binary_form=True)).hexdigest()


def api_key() -> str:
    """UNIFI_API_KEY, else the OpenBao operator ceremony (af/estate/unifi, field api_token), else the
    SOPS estate seeds (offline: works with OpenBao or the cluster down). Never printed."""
    key = os.environ.get("UNIFI_API_KEY", "").strip()
    if key:
        return key
    try:
        enc = subprocess.run(["kubectl", "--context", "admin@ai", "-n", "openbao", "get", "secret",
                              "openbao-breakglass-token", "-o", "jsonpath={.data.root_token}"],
                             capture_output=True, check=True, timeout=30).stdout
        token = base64.b64decode(enc)
        out = subprocess.run(
            ["kubectl", "--context", "admin@ai", "-n", "openbao", "exec", "-i", "openbao-0", "-c", "openbao", "--",
             "sh", "-c", 'read -r T; BAO_TOKEN="$T" BAO_TLS_SERVER_NAME=openbao.openbao.svc '
                         'bao kv get -mount=af -field=api_token estate/unifi'],
            input=token + b"\n", capture_output=True, check=True, timeout=60).stdout.decode().strip()
        if out:
            return out
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    seeds = ROOT / "kubernetes/apps/infrastructure/security/openbao/estate-seeds.sops.yaml"
    try:
        doc = yaml.safe_load(subprocess.run(["sops", "-d", str(seeds)], capture_output=True, check=True,
                                            timeout=60).stdout)
        val = json.loads(doc["stringData"]["unifi.json"])["api_token"]
        if val:
            return val.strip()
    except (subprocess.SubprocessError, OSError, KeyError, ValueError, TypeError):
        pass
    raise GatewayError("no UniFi API key: set UNIFI_API_KEY, or reach OpenBao (kubectl admin@ai), "
                       "or decrypt the estate seeds with sops")


# --------------------------------------------------------------------------------------------
# desired state, the state file, the plan


def load_gateway(path=GATEWAY) -> dict:
    with open(path, encoding="utf-8") as f:
        g = yaml.safe_load(f)
    need = {"version", "vendor", "controller", "lan", "port_forward", "local_dns", "ddns"}
    if not isinstance(g, dict) or set(g) != need or g["version"] != 1 or g["vendor"] != "unifi":
        raise GatewayError(f"{path}: expected exactly {sorted(need)} with version 1, vendor unifi")
    return g


def empty_state(g) -> dict:
    c = g["controller"]
    return {"version": 1, "controller": {"url": c["url"], "site": c["site"], "cert_sha256": c["cert_sha256"]},
            "dhcp_network": None, "port_forward": None, "static_dns": {}, "intents": []}


def load_state(g, path=STATE) -> dict:
    if not path.exists():
        return empty_state(g)
    with open(path, encoding="utf-8") as f:
        s = yaml.safe_load(f) or {}
    c = g["controller"]
    if s.get("version") != 1 or s.get("controller") != {"url": c["url"], "site": c["site"], "cert_sha256": c["cert_sha256"]}:
        raise GatewayError(f"{path} belongs to another controller, site or certificate than gateway.yaml; "
                           "refusing to act on its IDs")
    s.setdefault("dhcp_network", None)
    s.setdefault("static_dns", {})
    s.setdefault("intents", [])
    return s


def save_state(state, path=STATE):
    text = ("# Written by scripts/edge/gateway.py apply: the UniFi objects the edge reconciler owns.\n"
            "# Commit after every apply. Never edit IDs by hand: use --adopt / --forget.\n"
            + yaml.safe_dump(state, sort_keys=True))
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def desired_dns(reg) -> dict:
    return {f"{n}.{reg['zone']}": reg["vip"] for n, h in sorted(reg["hosts"].items()) if h["lan_dns"]}


def forward_fields(g, reg) -> dict:
    pf = g["port_forward"]
    return {"name": pf["name"], "enabled": True, "pfwd_interface": pf["wan_interface"], "src": "any",
            "destination_ip": "any", "dst_port": str(pf["wan_port"]), "fwd": reg["vip"],
            "fwd_port": str(pf["target_port"]), "proto": pf["proto"], "log": False}


def _forward_covers(f, port: int) -> bool:
    for part in str(f.get("dst_port", "")).split(","):
        part = part.strip()
        if "-" in part:
            lo, _, hi = part.partition("-")
            if lo.isdigit() and hi.isdigit() and int(lo) <= port <= int(hi):
                return True
        elif part.isdigit() and int(part) == port:
            return True
    return False


def collisions(g, kind, key, rows, own_id) -> list:
    """What else on the controller would clash with our object (own_id excluded; None for a create).
    Checked when planning and again from a fresh read right before the mutation."""
    if kind == "dns":
        others = [r for r in rows if str(r.get("key", "")).lower() == key and r["_id"] != own_id]
        return [f"DNS record {key}: {len(others)} other record(s) carry this key"] if others else []
    pf = g["port_forward"]
    out = []
    if any(f.get("name") == pf["name"] and f["_id"] != own_id for f in rows):
        out.append(f"another forward is named {pf['name']}")
    taken = [f.get("name") for f in rows if f["_id"] != own_id and f.get("enabled", True)
             and f.get("pfwd_interface", "wan") in (pf["wan_interface"], "both") and _forward_covers(f, pf["wan_port"])]
    if taken:
        out.append(f"another forward already takes WAN port {pf['wan_port']}: {taken}")
    return out


def lease_conflicts(clients, network_id, old, new) -> list:
    """Active clients holding an address the range change gives back (in the old range, not in the new
    one) that is not their fixed IP. A client of another network is skipped; one whose row names no
    network counts (conservatively). Only addresses of the range's own family are compared."""
    o0, o1 = (ipaddress.ip_address(x) for x in old)
    n0, n1 = (ipaddress.ip_address(x) for x in new)
    hits = []
    for c in clients:
        if not isinstance(c, dict) or not c.get("ip") or c.get("network_id") not in (network_id, None, ""):
            continue
        try:
            ip = ipaddress.ip_address(c["ip"])
        except ValueError:
            continue
        if ip.version != o0.version:
            continue
        if o0 <= ip <= o1 and not n0 <= ip <= n1 and not (c.get("use_fixedip") and c.get("fixed_ip") == c["ip"]):
            hits.append(f"{c['ip']} ({c.get('hostname') or c.get('name') or c.get('mac', '?')})")
    return sorted(hits)


def _intent_mismatch(intent, obj) -> bool:
    """An interrupted create may only be adopted as an object holding the fields it sent."""
    fields = intent.get("fields")
    return not isinstance(fields, dict) or any(obj.get(k) != v for k, v in fields.items())


# The order operations run in: bookkeeping first, then everything that withdraws or narrows exposure,
# then the network, then additions, and the WAN forward last of all.
PHASES = {("forward", "delete"): 2, ("dns", "delete"): 3, ("dhcp", "update"): 4, ("dns", "update"): 5,
          ("dns", "create"): 6, ("forward", "update"): 7, ("forward", "create"): 7}


def phase(op) -> int:
    return {"forget": 0, "adopt": 1}.get(op["action"]) if op["action"] in ("forget", "adopt") \
        else PHASES[(op["kind"], op["action"])]


def make_plan(g, reg, state, inv, only=None, adopt=(), forget=(), skip_vip_check=False,
              ack_dhcp_leases=False) -> dict:
    """Pure: compute the operations from the desired state, the state file and the inventory."""
    only = set(only or KINDS)
    errors, warnings, ops = [], [], []
    # An intent is a create whose outcome is unknown. It stops every run until the operator names it:
    # --adopt <key> takes the object the create may have made (only if it holds the fields the create
    # sent), --forget <key> drops the intent (the create is planned again if still wanted and absent).
    pending = [i for i in state["intents"] if i["key"] not in adopt and i["key"] not in forget]
    if pending:
        keys = ", ".join(f"{i['kind']}:{i['key']}" for i in pending)
        errors.append(f"interrupted create(s) recorded in the state file ({keys}): find the object in the "
                      "controller and re-run with --adopt <key>, or delete it there and --forget <key>")
    intents = {(i["kind"], i["key"]): i for i in state["intents"]}
    vip = ipaddress.ip_address(reg["vip"])
    lan = g["lan"]
    net = ipaddress.ip_interface(lan["subnet"]).network
    start, stop = ipaddress.ip_address(lan["dhcp_start"]), ipaddress.ip_address(lan["dhcp_stop"])
    if vip not in net:
        errors.append(f"vip {vip} is not on {net}")
    if start <= vip <= stop:
        errors.append(f"vip {vip} lies inside the desired DHCP range {start}-{stop}")

    # ---- dhcp: the network named in gateway.yaml, owned through the state file like everything else
    if "dhcp" in only:
        owned = state.get("dhcp_network")
        nets = {n["_id"]: n for n in inv["networkconf"]}
        name = lan["network_name"]
        if owned and owned["name"] in forget and (owned["id"] not in nets or owned["id"] != lan["network_id"]
                                                  or nets[owned["id"]].get("name") != owned["name"]):
            ops.append({"kind": "dhcp", "action": "forget", "key": owned["name"], "id": owned["id"]})
            owned = None
        h = nets.get(lan["network_id"])
        if owned and (owned["id"] != lan["network_id"] or owned["name"] != name):
            errors.append(f"the state owns network {owned['id']} ({owned['name']}) but gateway.yaml names "
                          f"{lan['network_id']} ({name}): fix gateway.yaml, or --forget {owned['name']}")
        elif h is None:
            errors.append(f"network {lan['network_id']} not found")
        elif h.get("name") != name or h.get("ip_subnet") != lan["subnet"]:
            errors.append(f"network {lan['network_id']} is {h.get('name')!r} {h.get('ip_subnet')!r}, "
                          f"not {name!r} {lan['subnet']!r}")
        else:
            if not owned and name in adopt:
                ops.append({"kind": "dhcp", "action": "adopt", "key": name, "id": h["_id"], "before": digest(h)})
                owned = {"id": h["_id"], "name": name}
            cur = (h.get("dhcpd_start"), h.get("dhcpd_stop"))
            want = (lan["dhcp_start"], lan["dhcp_stop"])
            if cur != want and not owned:
                errors.append(f"the DHCP range of {name} is {cur[0]}-{cur[1]}, not {want[0]}-{want[1]}, and the "
                              f"network is not ours: inspect it, then --adopt {name}")
            elif cur != want:
                if not all(cur):
                    errors.append(f"network {name} has no DHCP range to compare; set it by hand first")
                else:
                    ops.append({"kind": "dhcp", "action": "update", "key": name, "id": h["_id"],
                                "before": digest(h), "from": list(cur), "to": list(want),
                                "fields": {"dhcpd_start": want[0], "dhcpd_stop": want[1]},
                                "show": f"{cur[0]}-{cur[1]} -> {want[0]}-{want[1]}"})

    # ---- static dns
    if "dns" in only:
        want = desired_dns(reg)
        by_id = {r["_id"]: r for r in inv["static_dns"]}
        by_key = {}
        for r in inv["static_dns"]:
            by_key.setdefault(str(r.get("key", "")).lower(), []).append(r)
        owned = dict(state["static_dns"])
        for key in forget:
            if key in owned and (owned[key] not in by_id or str(by_id[owned[key]].get("key", "")).lower() != key):
                ops.append({"kind": "dns", "action": "forget", "key": key, "id": owned.pop(key)})
            elif ("dns", key) in intents and key not in owned:
                ops.append({"kind": "dns", "action": "forget", "key": key, "id": None})
        for key, oid in sorted(owned.items()):
            if oid not in by_id:
                errors.append(f"owned DNS record {key} ({oid}) is gone from the controller: --forget {key}")
            elif str(by_id[oid].get("key", "")).lower() != key:
                errors.append(f"owned DNS record {oid} now has key {by_id[oid].get('key')!r}, not {key}: "
                              f"inspect it, then --forget {key}")
        for key, value in sorted(want.items()):
            fields = {"key": key, "value": value, "record_type": "A", "enabled": True, "ttl": g["local_dns"]["ttl"]}
            if key in owned and owned[key] in by_id:
                r = by_id[owned[key]]
                if str(r.get("key", "")).lower() != key:
                    continue  # identity error above
                clash = collisions(g, "dns", key, inv["static_dns"], r["_id"])
                if clash:
                    errors.extend(clash)
                elif any(r.get(k) != v for k, v in fields.items()):
                    ops.append({"kind": "dns", "action": "update", "key": key, "id": r["_id"],
                                "before": digest(r), "fields": fields})
                continue
            existing = by_key.get(key, [])
            if existing and key in adopt:
                if len(existing) != 1:
                    errors.append(f"--adopt {key}: {len(existing)} records carry that key; resolve by hand")
                    continue
                r = existing[0]
                if ("dns", key) in intents and _intent_mismatch(intents[("dns", key)], r):
                    errors.append(f"--adopt {key}: the record does not hold the fields the interrupted create "
                                  f"sent, so it is not that create's: inspect it, delete it there and --forget {key}")
                    continue
                ops.append({"kind": "dns", "action": "adopt", "key": key, "id": r["_id"], "before": digest(r)})
                if any(r.get(k) != v for k, v in fields.items()):
                    ops.append({"kind": "dns", "action": "update", "key": key, "id": r["_id"],
                                "before": digest(r), "fields": fields})
            elif existing:
                errors.append(f"DNS record {key} exists and is not ours: inspect it, then --adopt {key}")
            else:
                ops.append({"kind": "dns", "action": "create", "key": key, "fields": fields})
        for key, oid in sorted(owned.items()):
            if key not in want and oid in by_id and str(by_id[oid].get("key", "")).lower() == key:
                ops.append({"kind": "dns", "action": "delete", "key": key, "id": oid, "before": digest(by_id[oid])})

    # ---- port forward
    if "forward" in only:
        pf = g["port_forward"]
        fields = forward_fields(g, reg)
        by_id = {f["_id"]: f for f in inv["portforward"]}
        owned = state.get("port_forward")
        intent = intents.get(("forward", pf["name"]))
        if owned and owned["name"] in forget and (owned["id"] not in by_id or owned["name"] != pf["name"]
                                                  or by_id[owned["id"]].get("name") != owned["name"]):
            ops.append({"kind": "forward", "action": "forget", "key": owned["name"], "id": owned["id"]})
            owned = None
        elif not owned and pf["name"] in forget and intent:
            ops.append({"kind": "forward", "action": "forget", "key": pf["name"], "id": None})
        blocked = False
        if owned and owned["id"] not in by_id:
            errors.append(f"owned forward {owned['name']} ({owned['id']}) is gone from the controller: "
                          f"--forget {owned['name']}")
            owned, blocked = None, True
        elif owned and (by_id[owned["id"]].get("name") != owned["name"] or owned["name"] != pf["name"]):
            errors.append(f"owned forward {owned['id']} is now named {by_id[owned['id']].get('name')!r}, not "
                          f"{owned['name']!r} (gateway.yaml: {pf['name']!r}): the ID may have been reused; "
                          f"inspect it, then --forget {owned['name']} (and --adopt it if it is ours)")
            owned, blocked = None, True
        own_id = owned["id"] if owned else None
        if not owned and not blocked and pf["name"] in adopt:
            named = [f for f in inv["portforward"] if f.get("name") == pf["name"]]
            if len(named) != 1:
                errors.append(f"--adopt {pf['name']}: {len(named)} forwards carry that name; resolve by hand")
                blocked = True
            elif intent and _intent_mismatch(intent, named[0]):
                errors.append(f"--adopt {pf['name']}: the forward does not hold the fields the interrupted create "
                              f"sent, so it is not that create's: inspect it, delete it there and --forget {pf['name']}")
                blocked = True
            else:
                ops.append({"kind": "forward", "action": "adopt", "key": pf["name"], "id": named[0]["_id"],
                            "before": digest(named[0])})
                own_id = named[0]["_id"]
        if not blocked:
            if pf["enabled"]:
                if not own_id and any(f.get("name") == pf["name"] for f in inv["portforward"]):
                    errors.append(f"a forward named {pf['name']} exists and is not ours: inspect it, then "
                                  f"--adopt {pf['name']}")
                else:
                    clash = collisions(g, "forward", pf["name"], inv["portforward"], own_id)
                    if clash:
                        errors.extend(clash)
                    elif own_id:
                        cur = by_id[own_id]
                        if any(cur.get(k) != v for k, v in fields.items()):
                            ops.append({"kind": "forward", "action": "update", "key": pf["name"], "id": own_id,
                                        "before": digest(cur), "fields": fields})
                    else:
                        ops.append({"kind": "forward", "action": "create", "key": pf["name"], "fields": fields})
            elif own_id:
                ops.append({"kind": "forward", "action": "delete", "key": pf["name"], "id": own_id,
                            "before": digest(by_id[own_id])})

    # ---- ddns: checked, never changed
    for want_dd in g["ddns"]:
        hits = [d for d in inv["dynamicdns"] if all(d.get(k) == v for k, v in want_dd.items())]
        if not hits:
            warnings.append(f"DDNS entry missing on the gateway: {want_dd} (recreate by hand: runbook)")

    if skip_vip_check:
        warnings.append("--skip-vip-check: this plan exposes the WAN forward WITHOUT proving the VIP serves the canary")
    ops.sort(key=lambda o: (phase(o), o["kind"], o["key"]))
    options = {"only": sorted(only), "adopt": sorted(adopt), "forget": sorted(forget),
               "skip_vip_check": bool(skip_vip_check), "ack_dhcp_leases": bool(ack_dhcp_leases)}
    body = {"controller": g["controller"]["url"], "site": g["controller"]["site"],
            "inventory": {k: digest(v) for k, v in sorted(inv.items())}, "ops": ops, "options": options}
    plan_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:20]
    return {"ops": ops, "errors": errors, "warnings": warnings, "options": options, "hash": plan_hash}


def check_leases(plan, clients) -> None:
    """Add a DHCP range change's lease conflicts to the plan: an error, or with --ack-dhcp-leases a
    warning. Kept out of make_plan (and out of the hash): client lists change by the second."""
    for op in plan["ops"]:
        if op["kind"] == "dhcp" and op["action"] == "update":
            hits = lease_conflicts(clients, op["id"], op["from"], op["to"])
            if hits and plan["options"]["ack_dhcp_leases"]:
                plan["warnings"].append(f"DHCP {op['show']} takes back addresses active clients hold: {hits}")
            elif hits:
                plan["errors"].append(f"DHCP {op['show']} would take back addresses active clients hold: {hits}; "
                                      "move or reserve them first, or re-plan with --ack-dhcp-leases")


def show(plan) -> str:
    lines = []
    for e in plan["errors"]:
        lines.append(f"ERROR  {e}")
    for w in plan["warnings"]:
        lines.append(f"WARN   {w}")
    for op in plan["ops"]:
        what = op.get("show") or json.dumps(op.get("fields", {}), sort_keys=True)
        lines.append(f"{op['action']:7s}{op['kind']:8s}{op['key']}  {what if op['action'] in ('create', 'update') else ''}")
    if not plan["ops"] and not plan["errors"]:
        lines.append("no changes")
    lines.append(f"plan hash: {plan['hash']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# apply


def canary_marker(reg) -> bytes:
    """The fixed start of edge-canary's answer (`edge-canary ok peer=`): a 200 from anything else is
    not proof that the VIP serves the edge proxy."""
    respond = reg["hosts"]["edge-canary"]["backend"]["respond"]
    marker = respond.split("{remote_host}")[0].strip()
    if len(marker) < 8:
        raise GatewayError("edge-canary's fixed answer has no distinctive text to check")
    return marker.encode()


def vip_answers(vip: str, name: str, marker: bytes) -> bool:
    """The VIP must serve the canary (a CA-valid certificate for its name, a 200 carrying its fixed
    answer) before the WAN is pointed at it. The TLS socket asks for HTTP/1.1 only (no ALPN)."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((vip, 443), timeout=10) as s:
            with ctx.wrap_socket(s, server_hostname=name) as t:
                t.sendall(f"GET / HTTP/1.1\r\nHost: {name}\r\nConnection: close\r\n\r\n".encode())
                data = b""
                while len(data) <= CANARY_LIMIT:
                    chunk = t.recv(4096)
                    if not chunk:
                        break
                    data += chunk
    except (OSError, ssl.SSLError):
        return False
    return canary_ok(data, marker)


CANARY_LIMIT = 65536


class _Raw:
    """Lets http.client parse bytes already read from the verified TLS socket."""

    def __init__(self, data: bytes):
        self._f = io.BytesIO(data)

    def makefile(self, *_args, **_kw):
        return self._f


def canary_ok(response: bytes, marker: bytes) -> bool:
    """The raw answer, parsed as HTTP/1.1 with its framing: exactly 200, a complete body (Content-Length
    or chunked), and the marker in the decoded body. A truncated body, a marker only in headers or
    trailers, an oversized answer, anything unparsable: False."""
    if len(response) > CANARY_LIMIT:
        return False
    resp = http.client.HTTPResponse(_Raw(response), method="GET")
    try:
        resp.begin()
        if resp.status != 200:
            return False
        body = resp.read()
    except (http.client.HTTPException, ValueError, OSError):
        return False
    return marker in body


def _current(client, kind, oid):
    inv = client.inventory()
    rows = {"dns": inv["static_dns"], "forward": inv["portforward"], "dhcp": inv["networkconf"]}[kind]
    hits = [r for r in rows if r["_id"] == oid]
    return hits[0] if hits else None, inv


def _drop_intent(state, kind, key):
    state["intents"] = [i for i in state["intents"] if not (i["kind"] == kind and i["key"] == key)]


def _own(state, kind, key, oid):
    if kind == "dns":
        state["static_dns"][key] = oid
    elif kind == "forward":
        state["port_forward"] = {"id": oid, "name": key}
    else:
        state["dhcp_network"] = {"id": oid, "name": key}


def _disown(state, kind, key):
    if kind == "dns":
        state["static_dns"].pop(key, None)
    elif kind == "forward":
        state["port_forward"] = None
    else:
        state["dhcp_network"] = None


def _unowned_snapshot(inv, state, plan) -> dict:
    """{(kind, id): digest} of every listed object the reconciler neither owns nor is about to touch:
    the Nextcloud forwards, the *.lan records, the other networks. They must be unchanged after apply."""
    mine = set(state["static_dns"].values())
    mine |= {o["id"] for o in plan["ops"] if o.get("id")}
    for owned in (state.get("port_forward"), state.get("dhcp_network")):
        if owned:
            mine.add(owned["id"])
    return {(kind, r["_id"]): digest(r) for kind in ("portforward", "static_dns", "networkconf")
            for r in inv[kind] if r["_id"] not in mine}


def _check_unowned(before: dict, inv: dict, created: set) -> list:
    now = {(kind, r["_id"]): digest(r) for kind in ("portforward", "static_dns", "networkconf")
           for r in inv[kind] if r["_id"] not in created}
    return sorted(f"{kind} {oid}: {'gone' if (kind, oid) not in now else 'changed'}"
                  for (kind, oid), d in before.items() if now.get((kind, oid)) != d)


def apply(client, g, reg, state, plan, state_path=STATE, log=print):
    options = plan["options"]
    baseline = _unowned_snapshot(client.inventory(), state, plan)
    created = set()
    _apply_ops(client, g, reg, state, plan, options, state_path, log, created)
    drift = _check_unowned(baseline, client.inventory(), created)
    if drift:
        raise GatewayError("objects the reconciler does not own changed or vanished during the apply "
                           f"(check them in the UniFi UI now): {drift}")


def _apply_ops(client, g, reg, state, plan, options, state_path, log, created):
    for op in plan["ops"]:
        kind, action, key = op["kind"], op["action"], op["key"]
        if action == "forget":
            # id None: only an interrupted create's intent is dropped; an owned ID is never touched then
            if op["id"] is not None:
                _disown(state, kind, key)
            _drop_intent(state, kind, key)
            save_state(state, state_path)
            log(f"forgot  {kind} {key}")
            continue
        if action in ("delete", "adopt"):
            cur, _ = _current(client, kind, op["id"])
            if cur is None or digest(cur) != op["before"]:
                raise GatewayError(f"{kind} {key} changed on the controller since the plan; re-plan")
        if action == "adopt":
            _own(state, kind, key, op["id"])
            _drop_intent(state, kind, key)
            save_state(state, state_path)
            log(f"adopted {kind} {key} ({op['id']})")
            continue
        # the gates, which take time, come BEFORE the final read below
        if kind == "forward" and action in ("create", "update") and op["fields"].get("enabled") \
                and not options["skip_vip_check"]:
            canary = f"edge-canary.{reg['zone']}"
            if not vip_answers(reg["vip"], canary, canary_marker(reg)):
                raise GatewayError(f"the VIP {reg['vip']} does not serve {canary}'s answer; not pointing the WAN "
                                   "at it (--skip-vip-check, in the plan, overrides)")
        if kind == "dhcp" and action == "update":
            hits = lease_conflicts(client.active_clients(), op["id"], op["from"], op["to"])
            if hits and not options["ack_dhcp_leases"]:
                raise GatewayError(f"DHCP {op['show']}: active clients now hold addresses it takes back: {hits}; "
                                   "re-plan")
        if action in ("create", "update"):
            # ONE final read, right before the mutation: the object is still the planned one, and nothing
            # has appeared in the way since (also not while a gate above was running)
            cur, inv = _current(client, kind, op.get("id") or "")
            if action == "update" and (cur is None or digest(cur) != op["before"]):
                raise GatewayError(f"{kind} {key} changed on the controller since the plan; re-plan")
            if kind in ("dns", "forward"):
                rows = inv["static_dns"] if kind == "dns" else inv["portforward"]
                clash = collisions(g, kind, key, rows, op.get("id"))
                if clash:
                    raise GatewayError(f"{kind} {key}: {'; '.join(clash)} (since the plan); re-plan")
        if action == "create":
            state["intents"].append({"kind": kind, "key": key, "at": int(time.time()), "fields": op["fields"]})
            save_state(state, state_path)
            obj = client.create_dns(op["fields"]) if kind == "dns" else client.create_portforward(op["fields"])
            created.add(obj["_id"])
            _own(state, kind, key, obj["_id"])
            _drop_intent(state, kind, key)
            save_state(state, state_path)
            oid = obj["_id"]
        elif action == "update":
            if kind == "dhcp":
                BACKUPS.mkdir(parents=True, exist_ok=True)
                bk = BACKUPS / f"networkconf-{op['id']}-{int(time.time())}.json"
                bk.write_text(json.dumps(_scrub(cur), indent=1))
                log(f"backup  {bk}")
            new = copy.deepcopy(cur)
            new.update(op["fields"])
            {"dns": client.update_dns, "forward": client.update_portforward, "dhcp": client.update_network}[kind](op["id"], new)
            oid = op["id"]
        elif action == "delete":
            {"dns": client.delete_dns, "forward": client.delete_portforward}[kind](op["id"])
            _disown(state, kind, key)
            save_state(state, state_path)
            cur, _ = _current(client, kind, op["id"])
            if cur is not None:
                raise GatewayError(f"{kind} {key}: still present after delete")
            log(f"deleted {kind} {key}")
            continue
        # verify the object now holds the desired fields
        cur, _ = _current(client, kind, oid)
        if cur is None or any(cur.get(k) != v for k, v in op["fields"].items()):
            raise GatewayError(f"{kind} {key}: the controller does not hold the desired fields after {action}")
        log(f"{action:7s} {kind} {key} ok")


# --------------------------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["plan", "apply", "fingerprint"])
    ap.add_argument("--only", action="append", choices=KINDS)
    ap.add_argument("--adopt", action="append", default=[], metavar="KEY")
    ap.add_argument("--forget", action="append", default=[], metavar="KEY")
    ap.add_argument("--plan-hash")
    ap.add_argument("--skip-vip-check", action="store_true",
                    help="expose the WAN forward without the canary check (part of the plan hash)")
    ap.add_argument("--ack-dhcp-leases", action="store_true",
                    help="accept a DHCP range change under active clients (part of the plan hash)")
    args = ap.parse_args(argv)
    try:
        g = load_gateway()
        if args.command == "fingerprint":
            print(fingerprint(g["controller"]["url"]))
            return 0
        reg = render.load()
        state = load_state(g)
        client = UnifiClient(g["controller"]["url"], g["controller"]["site"], g["controller"]["cert_sha256"], api_key())
        plan = make_plan(g, reg, state, client.inventory(), args.only, args.adopt, args.forget,
                         skip_vip_check=args.skip_vip_check, ack_dhcp_leases=args.ack_dhcp_leases)
        if any(o["kind"] == "dhcp" and o["action"] == "update" for o in plan["ops"]):
            check_leases(plan, client.active_clients())
        print(show(plan))
        if plan["errors"]:
            return 1
        if args.command == "plan":
            return 0
        if args.plan_hash != plan["hash"]:
            print(f"refusing: --plan-hash {args.plan_hash!r} is not this plan ({plan['hash']}); re-plan and review",
                  file=sys.stderr)
            return 1
        apply(client, g, reg, state, plan)
        print("applied; commit kubernetes/edge/gateway-state.yaml")
        return 0
    except (GatewayError, render.RegistryError) as e:
        print(f"edge gateway: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
