"""Spike 3: clone-churn soak — N cycles of (PVC-from-snapshot -> attach via runc pod -> delete),
counting failures and leftover PVs. Stresses the Trident/QNAP clone+attach+delete path the
lease pool will exercise, using the pool's Delete-reclaim StorageClass."""
import json, subprocess, sys, time

KC = ["kubectl", "--context", "admin@ai", "-n", "testpool-spike"]
N = int(sys.argv[1]) if len(sys.argv) > 1 else 50

def pod_pvc(i):
    name = f"churn-{i}"
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": "testpool-spike"},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "nodeSelector": {"ailab.io/env-pool": "true"},
            "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "env", "effect": "NoSchedule"}],
            "containers": [{
                "name": "probe", "image": "docker.io/library/busybox:1.36",
                "command": ["sh", "-c", "test -b /dev/blk && echo BLOCK-OK"],
                "volumeDevices": [{"name": "vol", "devicePath": "/dev/blk"}],
                "resources": {"requests": {"cpu": "50m", "memory": "32Mi"},
                               "limits": {"cpu": "200m", "memory": "64Mi"}},
            }],
            "volumes": [{"name": "vol", "ephemeral": {"volumeClaimTemplate": {"spec": {
                "accessModes": ["ReadWriteOnce"], "volumeMode": "Block",
                "storageClassName": "testpool-spike-iscsi",
                "dataSource": {"apiGroup": "snapshot.storage.k8s.io", "kind": "VolumeSnapshot", "name": "golden-spike-v2"},
                "resources": {"requests": {"storage": "40Gi"}}}}}}],
        },
    }

results = {"ok": 0, "pod_fail": 0, "timeout": 0}
durs = []
for i in range(N):
    t0 = time.time()
    subprocess.run(KC + ["apply", "-f", "-"], input=json.dumps(pod_pvc(i)), capture_output=True, text=True)
    ok = False
    deadline = time.time() + 300
    while time.time() < deadline:
        r = subprocess.run(KC + ["get", "pod", f"churn-{i}", "-o", "jsonpath={.status.phase}"], capture_output=True, text=True)
        if r.stdout.strip() in ("Succeeded", "Failed"):
            ok = r.stdout.strip() == "Succeeded"
            break
        time.sleep(2)
    else:
        results["timeout"] += 1
    if ok:
        results["ok"] += 1
    elif results["timeout"] == 0 or time.time() < deadline:
        if not ok and time.time() < deadline:
            results["pod_fail"] += 1
    subprocess.run(KC + ["delete", "pod", f"churn-{i}", "--wait=true", "--timeout=120s"], capture_output=True, text=True)
    durs.append(round(time.time() - t0, 1))
    if (i + 1) % 5 == 0:
        print(f"cycle {i+1}/{N}: {results} last5={durs[-5:]}", flush=True)

# settle, then leftovers
time.sleep(90)
r = subprocess.run(["kubectl", "--context", "admin@ai", "get", "pv", "-o", "json"], capture_output=True, text=True)
d = json.loads(r.stdout)
left = [pv["metadata"]["name"] + ":" + pv["status"]["phase"] for pv in d["items"]
        if (pv["spec"].get("claimRef") or {}).get("namespace") == "testpool-spike"
        and "churn" in ((pv["spec"].get("claimRef") or {}).get("name") or "")]
r2 = subprocess.run(KC + ["get", "pvc", "-o", "name"], capture_output=True, text=True)
churn_pvcs = [x for x in r2.stdout.split() if "churn" in x]
durs_sorted = sorted(durs)
print("RESULTS:", results)
print("cycle seconds: p50", durs_sorted[len(durs)//2], "p95", durs_sorted[int(len(durs)*0.95)], "max", durs_sorted[-1])
print("leftover churn PVs:", left or "NONE")
print("leftover churn PVCs:", churn_pvcs or "NONE")
