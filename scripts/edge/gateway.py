#!/usr/bin/env python3
"""Reconcile the home gateway (UniFi) with the edge's desired state (ADR 0040).

    python3 scripts/edge/gateway.py plan  [--only dhcp|dns|forward] [--adopt KEY] [--forget KEY]
    python3 scripts/edge/gateway.py apply --plan-hash HASH [same options as the plan]
    python3 scripts/edge/gateway.py fingerprint      # the controller certificate's SHA-256

Desired state: kubernetes/edge/gateway.yaml (the gateway contract) and kubernetes/edge/hostnames.yaml
(the hosts with lan_dns: true, the VIP). Owned objects: kubernetes/edge/gateway-state.yaml.

Guarantees (docs/standards/edge-ingress.md section 6, plans/2026-10-27-edge-ingress-plan.md section 3):
  - Ownership: only objects whose IDs are in the state file are updated or deleted. An unowned object
    in the way (a static DNS key, a forward name, any other WAN 443 forward) aborts the run; --adopt
    takes one over explicitly. An owned object that vanished aborts; --forget drops it from the state.
  - Fail closed: every listing is validated (classic `meta.rc == "ok"` + list, v2 list); anything
    partial or unexpected aborts before a plan exists, so "absent" is never inferred from a bad read.
  - Plan binding: the hash covers the whole inventory read, the operations and every option, and apply
    recomputes it and aborts on any difference. Each mutation re-reads its object first and checks it
    is unchanged since the plan, and is verified by a read afterwards.
  - Crash safety: a create writes an intent to the state file BEFORE the API call and replaces it
    with the returned ID after; an intent left behind stops the next run until it is adopted.
  - TLS is checked against the pinned certificate fingerprint, never skipped.
  - DDNS is checked, never changed. Secrets (x_* fields, the API key) are never printed or stored.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import http.client
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
        # the pinned fingerprint, checked below before a single request byte is sent.
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

    def _v2(self, rest):
        return f"/proxy/network/v2/api/site/{self.site}/{rest}"

    @staticmethod
    def _classic_list(status, payload, what):
        if status != 200 or not isinstance(payload, dict) or (payload.get("meta") or {}).get("rc") != "ok" \
                or not isinstance(payload.get("data"), list):
            raise GatewayError(f"{what}: unexpected answer (HTTP {status}); refusing to plan on a partial read")
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
            "port_forward": None, "static_dns": {}, "intents": []}


def load_state(g, path=STATE) -> dict:
    if not path.exists():
        return empty_state(g)
    with open(path, encoding="utf-8") as f:
        s = yaml.safe_load(f) or {}
    c = g["controller"]
    if s.get("version") != 1 or s.get("controller") != {"url": c["url"], "site": c["site"], "cert_sha256": c["cert_sha256"]}:
        raise GatewayError(f"{path} belongs to another controller, site or certificate than gateway.yaml; "
                           "refusing to act on its IDs")
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


def make_plan(g, reg, state, inv, only=None, adopt=(), forget=()) -> dict:
    """Pure: compute the operations from the desired state, the state file and the inventory."""
    only = set(only or KINDS)
    errors, warnings, ops = [], [], []
    # An intent is a create whose outcome is unknown. It stops every run until the operator names it:
    # --adopt <key> takes the object the create may have made, --forget <key> drops the intent (and the
    # create is planned again if the object is still wanted and absent).
    pending = [i for i in state["intents"] if i["key"] not in adopt and i["key"] not in forget]
    if pending:
        keys = ", ".join(f"{i['kind']}:{i['key']}" for i in pending)
        errors.append(f"interrupted create(s) recorded in the state file ({keys}): find the object in the "
                      "controller and re-run with --adopt <key>, or delete it there and --forget <key>")
    intent_keys = {(i["kind"], i["key"]) for i in state["intents"]}
    vip = ipaddress.ip_address(reg["vip"])
    lan = g["lan"]
    net = ipaddress.ip_interface(lan["subnet"]).network
    start, stop = ipaddress.ip_address(lan["dhcp_start"]), ipaddress.ip_address(lan["dhcp_stop"])
    if vip not in net:
        errors.append(f"vip {vip} is not on {net}")
    if start <= vip <= stop:
        errors.append(f"vip {vip} lies inside the desired DHCP range {start}-{stop}")

    # ---- dhcp
    if "dhcp" in only:
        homes = [n for n in inv["networkconf"] if n["_id"] == lan["network_id"]]
        if len(homes) != 1:
            errors.append(f"network {lan['network_id']} not found")
        else:
            h = homes[0]
            if h.get("name") != lan["network_name"] or h.get("ip_subnet") != lan["subnet"]:
                errors.append(f"network {lan['network_id']} is {h.get('name')!r} {h.get('ip_subnet')!r}, "
                              f"not {lan['network_name']!r} {lan['subnet']!r}")
            elif (h.get("dhcpd_start"), h.get("dhcpd_stop")) != (lan["dhcp_start"], lan["dhcp_stop"]):
                ops.append({"kind": "dhcp", "action": "update", "key": lan["network_name"], "id": h["_id"],
                            "before": digest(h), "fields": {"dhcpd_start": lan["dhcp_start"],
                                                            "dhcpd_stop": lan["dhcp_stop"]},
                            "show": f"{h.get('dhcpd_start')}-{h.get('dhcpd_stop')} -> "
                                    f"{lan['dhcp_start']}-{lan['dhcp_stop']}"})

    # ---- static dns
    if "dns" in only:
        want = desired_dns(reg)
        by_id = {r["_id"]: r for r in inv["static_dns"]}
        by_key = {}
        for r in inv["static_dns"]:
            by_key.setdefault(str(r.get("key", "")).lower(), []).append(r)
        owned = dict(state["static_dns"])
        for key in forget:
            if key in owned and owned[key] not in by_id:
                ops.append({"kind": "dns", "action": "forget", "key": key, "id": owned.pop(key)})
            elif ("dns", key) in intent_keys and key not in owned:
                ops.append({"kind": "dns", "action": "forget", "key": key, "id": None})
        for key, oid in sorted(owned.items()):
            if oid not in by_id:
                errors.append(f"owned DNS record {key} ({oid}) is gone from the controller: --forget {key}")
            elif str(by_id[oid].get("key", "")).lower() != key:
                errors.append(f"owned DNS record {oid} now has key {by_id[oid].get('key')!r}, not {key}")
        for key, value in sorted(want.items()):
            fields = {"key": key, "value": value, "record_type": "A", "enabled": True, "ttl": g["local_dns"]["ttl"]}
            if key in owned and owned[key] in by_id:
                r = by_id[owned[key]]
                if any(r.get(k) != v for k, v in fields.items()):
                    ops.append({"kind": "dns", "action": "update", "key": key, "id": r["_id"],
                                "before": digest(r), "fields": fields})
                continue
            existing = by_key.get(key, [])
            if existing and key in adopt:
                if len(existing) != 1:
                    errors.append(f"--adopt {key}: {len(existing)} records carry that key; resolve by hand")
                    continue
                r = existing[0]
                ops.append({"kind": "dns", "action": "adopt", "key": key, "id": r["_id"], "before": digest(r)})
                if any(r.get(k) != v for k, v in fields.items()):
                    ops.append({"kind": "dns", "action": "update", "key": key, "id": r["_id"],
                                "before": digest(r), "fields": fields})
            elif existing:
                errors.append(f"DNS record {key} exists and is not ours: inspect it, then --adopt {key}")
            else:
                ops.append({"kind": "dns", "action": "create", "key": key, "fields": fields})
        for key, oid in sorted(owned.items()):
            if key not in want and oid in by_id:
                ops.append({"kind": "dns", "action": "delete", "key": key, "id": oid, "before": digest(by_id[oid])})

    # ---- port forward
    if "forward" in only:
        pf = g["port_forward"]
        fields = forward_fields(g, reg)
        by_id = {f["_id"]: f for f in inv["portforward"]}
        owned = state.get("port_forward")
        if owned and pf["name"] in forget and owned["id"] not in by_id:
            ops.append({"kind": "forward", "action": "forget", "key": pf["name"], "id": owned["id"]})
            owned = None
        elif not owned and pf["name"] in forget and ("forward", pf["name"]) in intent_keys:
            ops.append({"kind": "forward", "action": "forget", "key": pf["name"], "id": None})
        if owned and owned["id"] not in by_id:
            errors.append(f"owned forward {owned['name']} ({owned['id']}) is gone from the controller: "
                          f"--forget {owned['name']}")
            owned = None
        same_name = [f for f in inv["portforward"] if f.get("name") == pf["name"]
                     and (not owned or f["_id"] != owned["id"])]
        conflicts = [f for f in inv["portforward"] if (not owned or f["_id"] != owned["id"])
                     and f.get("enabled", True) and f.get("pfwd_interface", "wan") in (pf["wan_interface"], "both")
                     and _forward_covers(f, pf["wan_port"])]
        if pf["enabled"]:
            if owned:
                cur = by_id[owned["id"]]
                if any(cur.get(k) != v for k, v in fields.items()):
                    ops.append({"kind": "forward", "action": "update", "key": pf["name"], "id": cur["_id"],
                                "before": digest(cur), "fields": fields})
            elif len(same_name) == 1 and pf["name"] in adopt:
                cur = same_name[0]
                ops.append({"kind": "forward", "action": "adopt", "key": pf["name"], "id": cur["_id"],
                            "before": digest(cur)})
                if any(cur.get(k) != v for k, v in fields.items()):
                    ops.append({"kind": "forward", "action": "update", "key": pf["name"], "id": cur["_id"],
                                "before": digest(cur), "fields": fields})
            elif same_name:
                errors.append(f"a forward named {pf['name']} exists and is not ours: inspect it, then --adopt {pf['name']}")
            elif conflicts:
                errors.append("another forward already takes WAN port "
                              f"{pf['wan_port']}: {[c.get('name') for c in conflicts]}")
            else:
                ops.append({"kind": "forward", "action": "create", "key": pf["name"], "fields": fields})
        elif owned:
            ops.append({"kind": "forward", "action": "delete", "key": pf["name"], "id": owned["id"],
                        "before": digest(by_id[owned["id"]])})

    # ---- ddns: checked, never changed
    for want in g["ddns"]:
        hits = [d for d in inv["dynamicdns"] if all(d.get(k) == v for k, v in want.items())]
        if not hits:
            warnings.append(f"DDNS entry missing on the gateway: {want} (recreate by hand: runbook)")

    # Order: DHCP first; DNS before the forward when it is created, the forward first when it goes.
    def rank(op):
        if op["kind"] == "dhcp":
            return 0
        if op["kind"] == "forward" and op["action"] == "delete":
            return 1
        if op["kind"] == "dns":
            return 2
        return 3
    # Within a key: bookkeeping (forget, adopt) before any API change, deletes before writes.
    action_rank = {"forget": 0, "adopt": 1, "delete": 2, "update": 3, "create": 4}
    ops.sort(key=lambda o: (rank(o), o["key"], action_rank[o["action"]]))
    body = {"controller": g["controller"]["url"], "site": g["controller"]["site"],
            "inventory": {k: digest(v) for k, v in sorted(inv.items())},
            "ops": ops, "options": {"only": sorted(only), "adopt": sorted(adopt), "forget": sorted(forget)}}
    plan_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:20]
    return {"ops": ops, "errors": errors, "warnings": warnings, "hash": plan_hash}


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


def vip_answers(vip: str, name: str) -> bool:
    """The VIP must answer HTTPS for the canary before the WAN is pointed at it."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((vip, 443), timeout=10) as s:
            with ctx.wrap_socket(s, server_hostname=name) as t:
                t.sendall(f"GET / HTTP/1.1\r\nHost: {name}\r\nConnection: close\r\n\r\n".encode())
                return t.recv(64).startswith(b"HTTP/1.1 200")
    except (OSError, ssl.SSLError):
        return False


def _current(client, kind, oid):
    inv = client.inventory()
    rows = {"dns": inv["static_dns"], "forward": inv["portforward"], "dhcp": inv["networkconf"]}[kind]
    hits = [r for r in rows if r["_id"] == oid]
    return hits[0] if hits else None, inv


def apply(client, g, reg, state, plan, state_path=STATE, skip_vip_check=False, log=print):
    for op in plan["ops"]:
        kind, action, key = op["kind"], op["action"], op["key"]
        if action == "forget":
            # id None: only an interrupted create's intent is dropped; an owned ID is never touched then
            if op["id"] is not None:
                if kind == "dns":
                    state["static_dns"].pop(key, None)
                else:
                    state["port_forward"] = None
            state["intents"] = [i for i in state["intents"] if not (i["kind"] == kind and i["key"] == key)]
            save_state(state, state_path)
            log(f"forgot  {kind} {key}")
            continue
        if action in ("update", "delete", "adopt"):
            cur, _ = _current(client, kind, op["id"])
            if cur is None or digest(cur) != op["before"]:
                raise GatewayError(f"{kind} {key} changed on the controller since the plan; re-plan")
        if action == "adopt":
            if kind == "dns":
                state["static_dns"][key] = op["id"]
            else:
                state["port_forward"] = {"id": op["id"], "name": key}
            state["intents"] = [i for i in state["intents"] if not (i["kind"] == kind and i["key"] == key)]
            save_state(state, state_path)
            log(f"adopted {kind} {key} ({op['id']})")
            continue
        if kind == "forward" and action == "create" and not skip_vip_check:
            canary = f"edge-canary.{reg['zone']}"
            if not vip_answers(reg["vip"], canary):
                raise GatewayError(f"the VIP {reg['vip']} does not answer {canary}; not pointing the WAN at it "
                                   "(--skip-vip-check overrides)")
        if action == "create":
            _, inv = _current(client, kind, "")
            rows = inv["static_dns"] if kind == "dns" else inv["portforward"]
            field = "key" if kind == "dns" else "name"
            if any(str(r.get(field, "")).lower() == key.lower() for r in rows):
                raise GatewayError(f"{kind} {key} appeared on the controller since the plan; re-plan")
            state["intents"].append({"kind": kind, "key": key, "at": int(time.time())})
            save_state(state, state_path)
            obj = client.create_dns(op["fields"]) if kind == "dns" else client.create_portforward(op["fields"])
            if kind == "dns":
                state["static_dns"][key] = obj["_id"]
            else:
                state["port_forward"] = {"id": obj["_id"], "name": key}
            state["intents"] = [i for i in state["intents"] if not (i["kind"] == kind and i["key"] == key)]
            save_state(state, state_path)
            oid = obj["_id"]
        elif action == "update":
            cur, _ = _current(client, kind, op["id"])
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
            if kind == "dns":
                state["static_dns"].pop(key, None)
            else:
                state["port_forward"] = None
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
    ap.add_argument("--skip-vip-check", action="store_true")
    args = ap.parse_args(argv)
    try:
        g = load_gateway()
        if args.command == "fingerprint":
            print(fingerprint(g["controller"]["url"]))
            return 0
        reg = render.load()
        state = load_state(g)
        client = UnifiClient(g["controller"]["url"], g["controller"]["site"], g["controller"]["cert_sha256"], api_key())
        plan = make_plan(g, reg, state, client.inventory(), args.only, args.adopt, args.forget)
        print(show(plan))
        if plan["errors"]:
            return 1
        if args.command == "plan":
            return 0
        if args.plan_hash != plan["hash"]:
            print(f"refusing: --plan-hash {args.plan_hash!r} is not this plan ({plan['hash']}); re-plan and review",
                  file=sys.stderr)
            return 1
        apply(client, g, reg, state, plan, skip_vip_check=args.skip_vip_check)
        print("applied; commit kubernetes/edge/gateway-state.yaml")
        return 0
    except (GatewayError, render.RegistryError) as e:
        print(f"edge gateway: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
