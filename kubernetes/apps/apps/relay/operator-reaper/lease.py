"""lease.py take|guard|release: the relay-operator-reaper's side of `relay-operator-jit`.

The same protocol as scripts/relay_operator_session.py: annotation `chifor.me/expires-at` (epoch seconds) plus
spec.holderIdentity, every write conditioned on metadata.resourceVersion. The reaper NEVER touches a live Lease, so a
running `session` or `call` excludes it, and while the reaper holds the Lease (for at most HOLD_S) a new run is refused.

    take     acquire a free Lease; write its exact readback identity and original expiry to /work/go
    guard    after the DB container requests work, revalidate that identity and issue one deadline permit
    release  if /work/go exists and the reaper still holds the Lease, release it (holder "")

Exit 0 in every expected case (including "skipped: a live holder"); non-zero only on an unexpected API failure.
stdlib only; talks to the API server with the pod's service-account token.
"""
import json
import math
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
HOLD_S = 180
MIN_REMAINING_S = 45
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
        value = float(((lease.get("metadata") or {}).get("annotations") or {}).get("chifor.me/expires-at", "0"))
        if not math.isfinite(value) or value < 0 or value != int(value):
            raise ValueError("invalid Lease expiry")
        return int(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("invalid Lease expiry") from None


def holder(lease):
    return (lease.get("spec") or {}).get("holderIdentity") or ""


def log(**kv):
    print(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kv}), flush=True)


def take():
    if os.path.exists(GO):
        os.unlink(GO)
    st, cur = api("GET", f"{LEASE_URL}/{NAME}")
    now = time.time()
    if st == 404:
        st, _ = api("POST", LEASE_URL, doc(HOLDER, now + HOLD_S))
    elif st == 200:
        if holder(cur) and expires(cur) <= 0:
            raise ValueError("held Lease has no expiry")
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
    if expires(got) < time.time() + MIN_REMAINING_S or expires(got) != int(now + HOLD_S):
        log(event="error", stage="acquisition-expiry")
        return 1
    record = {"holder": HOLDER, "resourceVersion": got["metadata"]["resourceVersion"],
              "expiresAt": expires(got)}
    with open(GO, "x") as stream:
        json.dump(record, stream)
    log(event="taken", holder=HOLDER, hold_s=HOLD_S)
    return 0


def permit(record):
    """Fresh read only: no renewal, reacquisition, or deadline extension."""
    st, cur = api("GET", f"{LEASE_URL}/{NAME}")
    if (st != 200 or record.get("holder") != HOLDER or holder(cur) != HOLDER
            or cur["metadata"].get("resourceVersion") != record.get("resourceVersion")
            or expires(cur) != record.get("expiresAt")
            or expires(cur) < time.time() + MIN_REMAINING_S):
        raise ValueError("Lease permit refused")
    return expires(cur)


def guard():
    if not os.path.exists(GO):
        log(event="guard", skipped=True)
        return 0
    with open(GO) as stream:
        record = json.loads(stream.read(4096))
    root = os.path.dirname(GO)
    stop = time.monotonic() + 110
    while not os.path.exists(os.path.join(root, "request")):
        if time.monotonic() >= stop or time.time() + MIN_REMAINING_S > record["expiresAt"]:
            raise ValueError("DB request deadline elapsed")
        time.sleep(0.1)
    deadline = permit(record)
    path = os.path.join(root, "permit")
    with open(path + ".tmp", "x") as stream:
        stream.write(str(deadline) + "\n")
    os.replace(path + ".tmp", path)
    # A failed/lost SQL reply is not proof the backend stopped. Never release on that path.
    while not os.path.exists(os.path.join(root, "done")):
        if time.monotonic() >= stop or time.time() >= deadline:
            raise ValueError("DB completion unobserved; keep original Lease expiry")
        time.sleep(0.1)
    return release()


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
    try:
        sys.exit({"take": take, "guard": guard, "release": release}[sys.argv[1]]())
    except (OSError, ValueError, KeyError, TypeError):
        log(event="error", stage="lease-protocol")
        sys.exit(1)
