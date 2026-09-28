#!/usr/bin/env python3
"""Build the throwaway Job (+ SA/Role/RoleBinding/ConfigMap) that drills cnpg-lost-slot-reclone
against the disposable `reclone-drill` cluster (cnpg-reclone-drill.yaml).

    python scripts/tests/fixtures/build-reclone-drill-job.py <suffix> <DRY_RUN true|false> > /tmp/drill-<suffix>.yaml
    kubectl --context admin@ai apply -f /tmp/drill-<suffix>.yaml
    kubectl --context admin@ai -n databases logs job/reclone-drill-<suffix> -c probe
    kubectl --context admin@ai -n databases logs job/reclone-drill-<suffix> -c reclone

It starts from the REAL CronJob's pod template (kubernetes/apps/databases/cnpg-lost-slot-reclone.yaml)
and the REAL script, and rewires only what the drill needs: the ServiceAccount (a Role scoped to the
drill Cluster), the ConfigMap holding the script, the probe's host / credentials / CA (the drill
cluster's), CLUSTERS, DRY_RUN and a short MIN_INTERVAL so the positive path can be repeated. Everything
it emits is named reclone-drill-* and labelled ailab.io/drill=reclone; nothing shares a name with a
Flux-managed object, so the drill cannot drift the real CronJob.
"""
import copy
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]
suffix, dry = sys.argv[1], sys.argv[2]
if dry not in ("true", "false"):
    sys.exit("DRY_RUN must be true or false")
docs = list(yaml.safe_load_all(open(ROOT / "kubernetes/apps/databases/cnpg-lost-slot-reclone.yaml", encoding="utf-8")))
cron = next(d for d in docs if d["kind"] == "CronJob")
script = open(ROOT / "kubernetes/apps/databases/cnpg-lost-slot-reclone.sh", encoding="utf-8").read()
labels = {"ailab.io/drill": "reclone"}


def meta(name):
    return {"name": name, "namespace": "databases", "labels": dict(labels)}


out = [
    {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta("reclone-drill-runner")},
    {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": meta("reclone-drill-runner"),
     "rules": [{"apiGroups": ["postgresql.cnpg.io"], "resources": ["clusters"], "resourceNames": ["reclone-drill"], "verbs": ["get", "patch"]},
               {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "delete"]},
               {"apiGroups": [""], "resources": ["persistentvolumeclaims"], "verbs": ["get", "list", "delete"]}]},
    {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": meta("reclone-drill-runner"),
     "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "reclone-drill-runner"},
     "subjects": [{"kind": "ServiceAccount", "name": "reclone-drill-runner", "namespace": "databases"}]},
    {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("reclone-drill-script"), "data": {"cnpg-lost-slot-reclone.sh": script}},
]

pod = copy.deepcopy(cron["spec"]["jobTemplate"]["spec"]["template"])
pod["spec"]["serviceAccountName"] = "reclone-drill-runner"
for v in pod["spec"]["volumes"]:
    if v["name"] == "script":
        v["configMap"]["name"] = "reclone-drill-script"
    if v["name"] == "ca":
        v["secret"]["secretName"] = "reclone-drill-ca"
probe = pod["spec"]["initContainers"][0]
for e in probe["env"]:
    if e["name"] == "PGHOST":
        e["value"] = "reclone-drill-rw.databases.svc.cluster.local"
    if e["name"] in ("PGUSER", "PGPASSWORD"):
        e["valueFrom"]["secretKeyRef"]["name"] = "reclone-drill-slotwatch"
main = pod["spec"]["containers"][0]
for e in main["env"]:
    if e["name"] == "CLUSTERS":
        e["value"] = "reclone-drill"
    if e["name"] == "DRY_RUN":
        e["value"] = dry
    if e["name"] == "MIN_INTERVAL_SECONDS":
        e["value"] = "60"  # the drill repeats the positive path quickly; production keeps 21600
out.append({"apiVersion": "batch/v1", "kind": "Job", "metadata": meta(f"reclone-drill-{suffix}"),
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 280, "template": pod}})
print(yaml.safe_dump_all(out, sort_keys=False))
