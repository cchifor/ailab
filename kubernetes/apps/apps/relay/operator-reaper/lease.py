"""lease.py take|release: the relay-operator-reaper's side of the fenced Lease `relay-operator-jit` (namespace relay).

The same protocol as scripts/relay_operator_session.py: annotation `chifor.me/expires-at` (epoch seconds) plus
spec.holderIdentity, every write conditioned on metadata.resourceVersion. The reaper NEVER touches a live Lease, so a
running `session` or `call` excludes it, and while the reaper holds the Lease (for at most HOLD_S) a new run is refused.

    take     acquire the Lease only if it is absent, released or expired; on success write /work/go as
             "<holder> <expires-at>": the reap passes that expires-at to ops.relay_operator_reap(fence), which
             refuses to delete anything unless at least 30 s of the hold remain (the database's own clock)
    release  if /work/go exists and the reaper still holds the Lease, release it (holder "")

Exit 0 in every expected case (including "skipped: a live holder"); non-zero only on an unexpected API failure.
stdlib only; talks to the API server with the pod's service-account token.
"""
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

API = "https://kubernetes.default.svc"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
LEASE_URL = API + "/apis/coordination.k8s.io/v1/namespaces/relay/leases"
NAME = "relay-operator-jit"
# Strictly longer than anything the Job can still run: activeDeadlineSeconds (120) + terminationGracePeriodSeconds
# (30) + margin. The reap's SQL additionally refuses unless >= 30 s of this hold remain (its fence).
HOLD_S = 180
GO = os.environ.get("REAPER_GO_FILE", "/work/go")
HOLDER = "reaper-" + socket.gethostname()


def api(method, url, body=None):
    token = open(os.path.join(SA, "token")).read().strip()
    ctx = ssl.create_default_context(cafile=os.path.join(SA, "ca.crt"))
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def doc(holder, expires_at, rv=None):
    meta = {"name": NAME, "namespace": "relay", "annotations": {"chifor.me/expires-at": str(int(expires_at))}}
    if rv:
        meta["resourceVersion"] = rv
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())
    return {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease", "metadata": meta,
            "spec": {"holderIdentity": holder, "acquireTime": ts, "renewTime": ts,
                     "leaseDurationSeconds": max(1, int(expires_at - time.time()))}}


def expires(lease):
    try:
        return float(((lease.get("metadata") or {}).get("annotations") or {}).get("chifor.me/expires-at", "0"))
    except ValueError:
        return 0.0


def holder(lease):
    return (lease.get("spec") or {}).get("holderIdentity") or ""


def log(**kv):
    print(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kv}), flush=True)


def take():
    st, cur = api("GET", f"{LEASE_URL}/{NAME}")
    now = time.time()
    if st == 404:
        st, _ = api("POST", LEASE_URL, doc(HOLDER, now + HOLD_S))
    elif st == 200:
        if holder(cur) and now < expires(cur):
            log(event="skip", reason="live lease", holder=holder(cur), expires_in=int(expires(cur) - now))
            return 0
        st, _ = api("PUT", f"{LEASE_URL}/{NAME}", doc(HOLDER, now + HOLD_S, cur["metadata"]["resourceVersion"]))
    else:
        log(event="error", stage="get", status=st)
        return 1
    if st == 409:
        log(event="skip", reason="lost the race (conflict)")
        return 0
    if st not in (200, 201):
        log(event="error", stage="write", status=st)
        return 1
    st, got = api("GET", f"{LEASE_URL}/{NAME}")
    if st != 200 or holder(got) != HOLDER:
        log(event="skip", reason="not the holder after the write")
        return 0
    fence = int(expires(got))  # the expires-at the API server now holds for US: the reap's SQL fence
    open(GO, "w").write(f"{HOLDER} {fence}")
    log(event="taken", holder=HOLDER, hold_s=HOLD_S, fence=fence)
    return 0


def release():
    if not os.path.exists(GO):
        log(event="release", skipped=True)
        return 0
    st, cur = api("GET", f"{LEASE_URL}/{NAME}")
    if st != 200 or holder(cur) != HOLDER:
        log(event="release", skipped=True, reason="no longer the holder")
        return 0
    st, _ = api("PUT", f"{LEASE_URL}/{NAME}", doc("", time.time(), cur["metadata"]["resourceVersion"]))
    log(event="release", status=st)
    return 0 if st in (200, 201, 409) else 1


if __name__ == "__main__":
    sys.exit({"take": take, "release": release}[sys.argv[1]]())
