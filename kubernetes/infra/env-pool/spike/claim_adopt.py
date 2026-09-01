import json, subprocess, sys, time, datetime

KC = ["kubectl", "--context", "admin@ai", "-n", "testpool-spike"]
name = sys.argv[1] if len(sys.argv) > 1 else "lease-1"

shutdown = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
claim = {
    "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
    "kind": "SandboxClaim",
    "metadata": {"name": name, "namespace": "testpool-spike"},
    "spec": {
        "warmPoolRef": {"name": "env-std-pool"},
        "lifecycle": {"shutdownTime": shutdown, "shutdownPolicy": "Delete"},
    },
}

t0 = time.time()
r = subprocess.run(KC + ["apply", "-f", "-"], input=json.dumps(claim), capture_output=True, text=True)
if r.returncode != 0:
    print("APPLY FAILED:", r.stderr[:400]); sys.exit(1)

sandbox = None
t_bound = t_ready = None
deadline = time.time() + 300
while time.time() < deadline:
    r = subprocess.run(KC + ["get", "sandboxclaim", name, "-o", "json"], capture_output=True, text=True)
    if r.returncode == 0:
        c = json.loads(r.stdout)
        st = c.get("status") or {}
        sb = st.get("sandbox") or {}
        if sb.get("name") and t_bound is None:
            t_bound = time.time(); sandbox = sb.get("name")
        conds = {x["type"]: x["status"] for x in (st.get("conditions") or [])}
        if conds.get("Ready") == "True":
            t_ready = time.time(); break
    time.sleep(0.05)

print(f"claim={name} sandbox={sandbox}")
print(f"t_create->bound: {None if t_bound is None else round(t_bound-t0,3)}s")
print(f"t_create->Ready: {None if t_ready is None else round(t_ready-t0,3)}s")
