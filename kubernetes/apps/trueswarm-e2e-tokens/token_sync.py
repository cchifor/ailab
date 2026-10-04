#!/usr/bin/env python3
"""trueswarm-e2e-token-sync — per-slot Trueswarm e2e bearer tokens (ADR 0035).

For every LIVE dev-worker slot and each Trueswarm app it mints a bearer token, publishes the token to
the worker's own OpenBao document (af/dev-workers/dev-worker-<N>, field `<app>_e2e_token`) and the
token's SHA-256 — never the token — into Secret `trueswarm-e2e-tokens` in the app's namespace, which
the app mounts (optional) and reads on every `POST /auth/e2e`.

ORDER IS THE CONTRACT. Secrets first, then (after kubelet has had time to refresh the mounted file)
OpenBao. A rotated slot's previous hash stays accepted for OVERLAP_SECONDS, so a worker holding the
old token keeps working while the new one propagates, and a worker never reads a token its app does
not accept yet. A run that dies between the two writes leaves OpenBao on the old token, which the
next run sees is no longer the slot's CURRENT hash and rotates again: self-healing, no operator step.

NEVER prints a token or a hash: only slot names, app names, counts and dates. Fail-closed: any error
before the OpenBao patch leaves every published token exactly as it was.
"""
import base64
import hashlib
import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request

SECRET_NAME = "trueswarm-e2e-tokens"
SECRET_KEY = "tokens.json"
FORMAT_VERSION = 1

# app key -> (namespace, KV field prefix, token prefix, admin?). The token prefix makes a token for one
# app useless at the other even if the hash files were ever swapped: each app only accepts its own.
APPS = {
    "trueswarm": ("trueswarm", "trueswarm", "tse2e", False),
    "trueswarm-admin": ("trueswarm-admin", "trueswarm_admin", "tsadmine2e", True),
}
# The highest role this sync may grant in trueswarm-admin. The admin app independently refuses
# anything above operator for an e2e principal, and machine sessions can never satisfy the fresh-MFA
# check, so sensitive operations stay human-only whatever this says.
ADMIN_ROLES = ("viewer", "moderator", "operator")


def sha256_hex(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(prefix, slot_name):
    return f"{prefix}.{slot_name}.{secrets.token_urlsafe(32)}"


def plan_slot(published, published_until, current_entry, now, rotate_before, force):
    """Decide one (slot, app). Returns (rotate: bool, reason: str).

    `published` is the token in OpenBao (or None), `published_until` its recorded expiry (or None),
    `current_entry` the slot's entry in the app's live Secret (or None)."""
    if force:
        return True, "forced"
    if not published or not published_until:
        return True, "not published"
    if published_until < now + rotate_before:
        return True, "due"
    tokens = (current_entry or {}).get("tokens") or []
    if not tokens or tokens[0].get("sha256") != sha256_hex(published):
        # OpenBao and the app disagree about the CURRENT token (a run died between its two writes,
        # or the Secret was recreated). Mint fresh rather than guess which side is right.
        return True, "app does not hold the published token"
    if tokens[0].get("not_after", 0) < now + rotate_before:
        return True, "app copy due"
    return False, "current"


def build_entry(slot_name, current_token, current_until, previous_entry, rotated, now, overlap, admin, role,
                access_client_ids):
    """The slot's entry for the app Secret. tokens[0] is always the current token."""
    tokens = [{"sha256": sha256_hex(current_token), "not_after": current_until}]
    if previous_entry:
        prev = previous_entry.get("tokens") or []
        if rotated and prev:
            # The token being replaced stays valid for `overlap`, never longer than it already was.
            tokens.append({"sha256": prev[0]["sha256"],
                           "not_after": min(prev[0].get("not_after", 0), now + overlap)})
        # An overlap entry from an earlier rotation survives until it expires.
        for t in prev[1:]:
            if t.get("not_after", 0) > now and t["sha256"] != tokens[0]["sha256"]:
                tokens.append(t)
    tokens = [t for t in tokens if t["not_after"] > now]
    entry = {"name": slot_name, "tokens": tokens}
    if admin:
        entry["role"] = role
        entry["access_client_ids"] = sorted(access_client_ids)
    return entry


def build_document(entries):
    return {"version": FORMAT_VERSION, "principals": sorted(entries, key=lambda e: e["name"])}


def parse_document(raw):
    if not raw:
        return {}
    doc = json.loads(raw)
    if doc.get("version") != FORMAT_VERSION:
        raise RuntimeError(f"Secret {SECRET_NAME} has format version {doc.get('version')!r}, expected "
                           f"{FORMAT_VERSION}; refusing to overwrite a document this sync does not understand")
    return {p["name"]: p for p in doc.get("principals", [])}


def validate_document(doc, live_names, admin, role):
    """Shape check of what is about to be written (a malformed file disables e2e login for every slot)."""
    names = [p["name"] for p in doc["principals"]]
    assert sorted(names) == sorted(live_names), "principals must be exactly the live slots"
    for p in doc["principals"]:
        assert p["tokens"], f"{p['name']} has no token"
        for t in p["tokens"]:
            assert len(t["sha256"]) == 64 and int(t["sha256"], 16) >= 0
            assert isinstance(t["not_after"], int)
        if admin:
            assert p["role"] == role and role in ADMIN_ROLES
            assert isinstance(p["access_client_ids"], list)
        else:
            assert "role" not in p and "access_client_ids" not in p


# ---- I/O -------------------------------------------------------------------------------------------

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S = "https://kubernetes.default.svc"


def call(url, ctx, method="GET", body=None, token=None, ctype="application/json"):
    """Returns (status, parsed_json). Raises on transport errors; 4xx/5xx come back as status."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", ctype)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except ValueError:
            return exc.code, {"raw": "<unparseable>"}


def config_from_env(env):
    cfg = {
        "mount": env.get("BAO_KV_MOUNT", "af"),
        "live": [int(x) for x in env["LIVE_SLOTS"].split()],
        "validity": int(env.get("VALIDITY_SECONDS", str(14 * 86400))),
        "rotate_before": int(env.get("ROTATE_BEFORE_SECONDS", str(7 * 86400))),
        "overlap": int(env.get("OVERLAP_SECONDS", "3600")),
        "propagation": int(env.get("PROPAGATION_SECONDS", "120")),
        "force": env.get("FORCE_ROTATE", "0") == "1",
        "role": env.get("ADMIN_ROLE", "operator"),
        "access_ids": env.get("ADMIN_ACCESS_CLIENT_IDS", "").split(),
    }
    if not cfg["live"]:
        sys.exit("LIVE_SLOTS is empty; refusing to run")
    if cfg["role"] not in ADMIN_ROLES:
        sys.exit(f"ADMIN_ROLE {cfg['role']!r} is not one of {ADMIN_ROLES}; an e2e principal is never administrator")
    if cfg["rotate_before"] >= cfg["validity"] or cfg["overlap"] >= cfg["rotate_before"]:
        sys.exit("need OVERLAP_SECONDS < ROTATE_BEFORE_SECONDS < VALIDITY_SECONDS")
    return cfg


def read_secret(k8s, ns):
    """(resourceVersion or None, {slot name: entry})."""
    st, obj = k8s(f"/api/v1/namespaces/{ns}/secrets/{SECRET_NAME}")
    if st == 200:
        raw = base64.b64decode((obj.get("data") or {}).get(SECRET_KEY, "")).decode() or None
        return obj["metadata"]["resourceVersion"], parse_document(raw)
    if st == 404:
        return None, {}
    sys.exit(f"read of {ns}/{SECRET_NAME} returned HTTP {st}")


def sync(cfg, k8s, bao, sleep, now, log):
    """One run. `k8s(path, method, payload)` and `bao(path, method, payload, ctype)` return
    (status, json); they are injected so the tests can interleave two runs against one fake cluster."""
    mount, slots = cfg["mount"], [f"dev-worker-{n}" for n in cfg["live"]]

    # ---- Read everything first ------------------------------------------------------------------
    workers = {}
    for slot in slots:
        st, doc = bao(f"{mount}/data/dev-workers/{slot}")
        if st == 200:
            workers[slot] = doc["data"]["data"]
        elif st == 404:
            workers[slot] = {}
        else:
            sys.exit(f"read of {slot} returned HTTP {st}; refusing to guess what it holds")
    live_secrets = {app: read_secret(k8s, ns) for app, (ns, _, _, _) in APPS.items()}

    # ---- Plan -----------------------------------------------------------------------------------
    publish = {slot: {} for slot in slots}  # slot -> KV fields to patch (only rotated ones)
    fence = []  # (app, slot, sha256): must still be the app's CURRENT token when we publish
    documents = {}
    for app, (ns, field, prefix, admin) in APPS.items():
        _, current = live_secrets[app]
        entries = []
        for slot in slots:
            published = workers[slot].get(f"{field}_e2e_token")
            until_raw = workers[slot].get(f"{field}_e2e_valid_until")
            published_until = int(until_raw) if until_raw and str(until_raw).isdigit() else None
            rotate, reason = plan_slot(published, published_until, current.get(slot), now, cfg["rotate_before"],
                                       cfg["force"])
            if rotate:
                token, until = new_token(prefix, slot), now + cfg["validity"]
                publish[slot][f"{field}_e2e_token"] = token
                publish[slot][f"{field}_e2e_valid_until"] = str(until)
                fence.append((app, slot, sha256_hex(token)))
            else:
                token, until = published, published_until
            entries.append(build_entry(slot, token, until, current.get(slot), rotate, now, cfg["overlap"],
                                       admin, cfg["role"], cfg["access_ids"]))
            log(f"{app}/{slot}: {'rotate' if rotate else 'keep'} ({reason}); "
                f"valid until {time.strftime('%Y-%m-%d', time.gmtime(until))}")
        doc = build_document(entries)
        validate_document(doc, slots, admin, cfg["role"])
        documents[app] = doc
        dropped = sorted(set(current) - set(slots))
        if dropped:
            log(f"{app}: dropping non-live slot(s) {dropped}")

    # ---- Write the app Secrets (compare-and-swap on resourceVersion) ----------------------------
    for app, (ns, _, _, _) in APPS.items():
        version, current = live_secrets[app]
        rendered = json.dumps(documents[app], indent=2, sort_keys=True)
        if version is not None and current and build_document(list(current.values())) == documents[app]:
            log(f"{app}: Secret unchanged")
            continue
        obj = {
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": SECRET_NAME, "namespace": ns,
                         "labels": {"app.kubernetes.io/name": SECRET_NAME,
                                    "app.kubernetes.io/managed-by": "trueswarm-e2e-token-sync",
                                    "app.kubernetes.io/part-of": "agentforge"},
                         "annotations": {"ailab.chifor.me/adr": "0035"}},
            "data": {SECRET_KEY: base64.b64encode(rendered.encode()).decode()},
        }
        if version is None:
            st, _ = k8s(f"/api/v1/namespaces/{ns}/secrets", "POST", obj)
        else:
            # resourceVersion makes this a CAS against a run that wrote between our read and this write.
            obj["metadata"]["resourceVersion"] = version
            st, _ = k8s(f"/api/v1/namespaces/{ns}/secrets/{SECRET_NAME}", "PUT", obj)
        if st == 409:
            sys.exit(f"{ns}/{SECRET_NAME} changed under this run (concurrent sync); nothing published, retry")
        if st not in (200, 201):
            sys.exit(f"write of {ns}/{SECRET_NAME} failed: HTTP {st}; nothing published")
        log(f"{app}: Secret written ({len(documents[app]['principals'])} principals)")

    expected = len(slots) * len(APPS)
    if not fence:
        log(f"validated {expected}/{expected} slot tokens (0 rotated)")
        return

    # ---- Publish to OpenBao, only after the apps can accept the new tokens ----------------------
    log(f"waiting {cfg['propagation']}s for kubelet to refresh the mounted Secrets")
    sleep(cfg["propagation"])
    # PUBLICATION FENCE. The CAS above only orders the Secret WRITES. A run that wrote its Secrets and
    # paused here can be overtaken: a second run reads those Secrets while OpenBao still holds the old
    # tokens, rotates again and publishes. Publishing now would put OUR tokens in OpenBao while the
    # apps hold the other run's as current (ours only as a 1 h overlap). So re-read, and publish only
    # if every token this run minted is still its app's current one; otherwise the later run owns
    # publication and this one stops. The remaining window (another run writing its Secrets between
    # this check and the PATCH below) cannot leave a wrong final state: that run must itself sleep
    # `propagation` before publishing, so its tokens reach OpenBao after ours and match its Secrets.
    reread = {app: read_secret(k8s, ns)[1] for app, (ns, _, _, _) in APPS.items()}
    for app, slot, digest in fence:
        tokens = (reread[app].get(slot) or {}).get("tokens") or []
        if not tokens or tokens[0].get("sha256") != digest:
            sys.exit(f"{app}/{slot}: a later run replaced this run's token before publication; not publishing "
                     "(that run publishes its own)")
    for slot, fields in sorted(publish.items()):
        if not fields:
            continue
        path = f"{mount}/data/dev-workers/{slot}"
        if workers[slot]:
            st, _ = bao(path, "PATCH", {"data": fields}, "application/merge-patch+json")
        else:
            # Create-only CAS, exactly as openbao-k8stoken-sync: a bare write could replace a document
            # another sync created in the meantime and erase its fields.
            st, _ = bao(path, "POST", {"data": fields, "options": {"cas": 0}})
            if st in (400, 409):
                st, _ = bao(path, "PATCH", {"data": fields}, "application/merge-patch+json")
        if st not in (200, 204):
            sys.exit(f"publish to {slot} failed: HTTP {st}; the next run re-mints (self-healing)")
        log(f"published {slot}: {sorted(f for f in fields if f.endswith('_token'))}")
    log(f"validated {expected}/{expected} slot tokens ({len(fence)} rotated)")


def main():
    cfg = config_from_env(os.environ)
    bao_addr = os.environ["BAO_ADDR"]
    bao_role = os.environ.get("BAO_ROLE", "trueswarm-e2e-sync")
    k8s_ctx = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
    bao_ctx = ssl.create_default_context(cafile=os.environ["BAO_CACERT"])
    with open(f"{SA_DIR}/token", encoding="utf-8") as fh:
        sa_jwt = fh.read().strip()

    status, body = call(f"{bao_addr}/v1/auth/kubernetes/login", bao_ctx, "POST", {"role": bao_role, "jwt": sa_jwt})
    if status != 200:
        sys.exit(f"OpenBao k8s-auth login failed as role '{bao_role}': HTTP {status}. "
                 "Is the role created (devworker-provision-job) and the vault unsealed?")
    bao_token = body["auth"]["client_token"]

    def bao(path, method="GET", payload=None, ctype="application/json"):
        return call(f"{bao_addr}/v1/{path}", bao_ctx, method, payload, bao_token, ctype)

    def k8s(path, method="GET", payload=None):
        return call(f"{K8S}{path}", k8s_ctx, method, payload, sa_jwt)

    sync(cfg, k8s, bao, time.sleep, int(time.time()), lambda line: print(line, flush=True))


if __name__ == "__main__":
    main()
