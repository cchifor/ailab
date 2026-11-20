"""The nightly Talos node trim's manifests (kubernetes/apps/node-fstrim).

A trim is a discard burst on the NVMe that a Proxmox host's etcd member shares, so the trims take turns:
- every Talos node the tofu modules declare (the control planes, the agent nodes, the relay-worker pool) has
  exactly one CronJob, pinned to that node by hostname, and no CronJob names a node that does not exist;
- each CronJob's worst case (a start up to startingDeadlineSeconds late, the Job's activeDeadlineSeconds, then
  terminationGracePeriodSeconds for the chunk in flight) ends before the next node's slot starts, all of it
  between Velero (02:00, + 30 min) and the off-site sync (04:00);
- the script's own budget fits the Job: no chunk starts after RUN_MAX_S unless it can end by then, so
  RUN_MAX_S plus one chunk stays inside activeDeadlineSeconds, and the grace covers one chunk;
- the pod tolerates exactly the taints the tofu modules give the nodes, holds CAP_SYS_ADMIN and nothing else,
  mounts /var read-only, has no token, a read-only root and a digest-pinned image, and every setting it
  passes is one node-fstrim.sh reads;
- the namespace admits privileged pods and denies all traffic; Flux applies the tree.
Fixture cases mutate the parsed manifests to prove the slot check fails when it should.
"""

import copy
import pathlib
import re
import unittest

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
TREE = REPO / "kubernetes/apps/node-fstrim"
INFRA = REPO / "kubernetes/infra"
BAND = ("02:30", "04:00")


def mins(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def tofu_nodes():
    """{k8s node name: (proxmox host, taints)} from the tofu modules."""
    out = {}
    cps = (INFRA / "variables.tf").read_text(encoding="utf-8")
    for key, host in re.findall(r'^\s*(cp\d+)\s*=\s*\{\s*host_node\s*=\s*"([^"]+)"', cps, re.M):
        out[f"talos-{key}"] = (host, set())
    agents = (INFRA / "agent-nodes/variables.tf").read_text(encoding="utf-8")
    labels = (INFRA / "agent-nodes/node-labels.tf").read_text(encoding="utf-8")
    m = re.search(r'kubernetes_node_taint" "agent".*?key\s*=\s*"([^"]+)".*?value\s*=\s*"([^"]+)".*?effect\s*=\s*"([^"]+)"',
                  labels, re.S)
    agent_taint = (m.group(1), m.group(2), m.group(3))
    for host, hostname in re.findall(r'node_name\s*=\s*"([^"]+)".*?hostname\s*=\s*"([^"]+)"\s*\}', agents):
        out[f"talos-{hostname}"] = (host, {agent_taint})
    # The relay workers' taint is Talos's own (machine.nodeTaints in their machine-config template).
    tpl = yaml.safe_load((INFRA / "agent-nodes/machine-config/relay-worker.yaml.tftpl").read_text(encoding="utf-8"))
    [(k, v)] = tpl["machine"]["nodeTaints"].items()
    rw_taint = (k, v.split(":")[0], v.split(":")[1])
    # The real relay-worker inventory is the gitignored relay-workers.auto.tfvars. What is committed is the
    # module's validation, which refuses any relay worker but the listed ones (relay-workers.tf), and the
    # example file; the two must agree, and the CronJobs follow them.
    allowed = dict(re.findall(r'k == "(relay-worker-\d+)" && v\.node_name == "(ai-node\d+)"',
                              (INFRA / "agent-nodes/relay-workers.tf").read_text(encoding="utf-8")))
    example = (INFRA / "agent-nodes/relay-workers.tfvars.example").read_text(encoding="utf-8")
    listed = dict(re.findall(r'^\s*(relay-worker-\d+)\s*=\s*\{\s*\n\s*node_name\s*=\s*"([^"]+)"', example, re.M))
    assert allowed and allowed == listed, f"relay-workers.tf allows {allowed}, the example lists {listed}"
    for name, host in allowed.items():
        out[name] = (host, {rw_taint})
    return out


def load_tree():
    base = yaml.safe_load((TREE / "base/cronjob.yaml").read_text(encoding="utf-8"))
    top = yaml.safe_load((TREE / "kustomization.yaml").read_text(encoding="utf-8"))
    overlays = {}
    for d in sorted((TREE / "nodes").iterdir()):
        k = yaml.safe_load((d / "kustomization.yaml").read_text(encoding="utf-8"))
        ops = {op["path"]: op["value"] for p in k["patches"] for op in yaml.safe_load(p["patch"])}
        overlays[d.name] = {
            "suffix": k["nameSuffix"],
            "resources": k["resources"],
            "schedule": ops["/spec/schedule"],
            "node": ops["/spec/jobTemplate/spec/template/spec/nodeSelector/kubernetes.io~1hostname"],
        }
    return base, top, overlays


def slot_problems(base, overlays):
    out, spans = [], []
    job = base["spec"]["jobTemplate"]["spec"]
    pod = job["template"]["spec"]
    worst = base["spec"]["startingDeadlineSeconds"] + job["activeDeadlineSeconds"] + pod["terminationGracePeriodSeconds"]
    for name, o in overlays.items():
        m = re.fullmatch(r"(\d{1,2}) (\d{1,2}) \* \* \*", o["schedule"])
        if not m:
            out.append(f"{name}: schedule {o['schedule']!r} is not a daily 'M H * * *'")
            continue
        s = int(m.group(2)) * 60 + int(m.group(1))
        e = s * 60 + worst
        spans.append((s * 60, e, name))
        if s < mins(BAND[0]) or e > mins(BAND[1]) * 60:
            out.append(f"{name}: {hm(s)} + {worst} s leaves the {BAND[0]}-{BAND[1]} UTC band")
    spans.sort()
    for a, b in zip(spans, spans[1:]):
        if b[0] < a[1]:
            out.append(f"{a[2]} (ends {hm(a[1] // 60)}:{a[1] % 60:02d}) overlaps {b[2]} (starts {hm(b[0] // 60)})")
    return out


class NodeFstrimManifests(unittest.TestCase):
    def setUp(self):
        self.base, self.top, self.overlays = load_tree()
        self.job = self.base["spec"]["jobTemplate"]["spec"]
        self.pod = self.job["template"]["spec"]
        self.ctr = self.pod["containers"][0]
        self.env = {e["name"]: e["value"] for e in self.ctr["env"]}

    def test_one_cronjob_per_talos_node(self):
        nodes = tofu_nodes()
        self.assertEqual(7, len(nodes), nodes)
        self.assertEqual(sorted(nodes), sorted(o["node"] for o in self.overlays.values()))
        for name, o in self.overlays.items():
            self.assertEqual(name, o["node"], "the overlay's directory names its node")
            self.assertEqual(f"-{name}", o["suffix"])
            self.assertEqual(["../../base"], o["resources"])
        self.assertEqual(sorted(f"nodes/{n}" for n in self.overlays),
                         sorted(r for r in self.top["resources"] if r.startswith("nodes/")))

    def test_the_slots_are_one_node_at_a_time_inside_the_band(self):
        self.assertEqual([], slot_problems(self.base, self.overlays))

    def test_the_script_budget_fits_the_job(self):
        chunk_s = int(self.env["CHUNK_GB"]) * int(self.env["SEC_PER_GB"])
        self.assertLessEqual(int(self.env["RUN_MAX_S"]) + chunk_s, self.job["activeDeadlineSeconds"])
        self.assertGreaterEqual(self.pod["terminationGracePeriodSeconds"], chunk_s)
        self.assertEqual(0, self.job["backoffLimit"], "a retry would run into the next node's slot")
        self.assertEqual("Forbid", self.base["spec"]["concurrencyPolicy"])
        self.assertEqual("Etc/UTC", self.base["spec"]["timeZone"])

    def test_tolerations_cover_every_node_taint_and_nothing_broader(self):
        tol = {(t["key"], t.get("value"), t["effect"]) for t in self.pod["tolerations"]}
        for name, (_host, taints) in tofu_nodes().items():
            for t in taints:
                self.assertIn(t, tol, f"{name} carries {t}")
        for t in self.pod["tolerations"]:
            self.assertEqual("NoSchedule", t["effect"])
            self.assertNotEqual("", t.get("key", ""), "a toleration without a key tolerates every taint")

    def test_the_pod_holds_only_what_fitrim_needs(self):
        sc = self.ctr["securityContext"]
        self.assertEqual({"drop": ["ALL"], "add": ["SYS_ADMIN"]}, sc["capabilities"])
        self.assertFalse(sc["privileged"])
        self.assertTrue(sc["readOnlyRootFilesystem"])
        self.assertEqual({"type": "RuntimeDefault"}, sc["seccompProfile"])
        self.assertFalse(self.pod["automountServiceAccountToken"])
        mounts = {m["name"]: m for m in self.ctr["volumeMounts"]}
        self.assertTrue(mounts["host-var"]["readOnly"])
        vols = {v["name"]: v for v in self.pod["volumes"]}
        self.assertEqual({"path": "/var", "type": "Directory"}, vols["host-var"]["hostPath"])
        # The only writable host path: the trim's own state directory, which STATE_DIR points at.
        writable = sorted(v["hostPath"]["path"] for v in self.pod["volumes"]
                          if "hostPath" in v and not mounts[v["name"]].get("readOnly"))
        self.assertEqual(["/var/lib/node-fstrim"], writable)
        self.assertEqual({"path": "/var/lib/node-fstrim", "type": "DirectoryOrCreate"}, vols["state"]["hostPath"])
        self.assertEqual(mounts["state"]["mountPath"], self.env["STATE_DIR"])
        self.assertEqual(["/bin/bash", "/opt/node-fstrim/node-fstrim.sh", "/host/var"], self.ctr["command"])
        self.assertRegex(self.ctr["image"], r"^[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$")

    def test_every_setting_is_one_the_script_reads(self):
        script = (TREE / "node-fstrim.sh").read_text(encoding="utf-8")
        for n in self.env:
            self.assertRegex(script, r'(?m)^' + n + r'="\$\{' + n + r':-', f"{n} is set but never read")

    def test_namespace_network_and_flux(self):
        ns = yaml.safe_load((TREE / "namespace.yaml").read_text(encoding="utf-8"))
        self.assertEqual("privileged", ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"])
        np = yaml.safe_load((TREE / "networkpolicy.yaml").read_text(encoding="utf-8"))
        self.assertEqual({}, np["spec"]["podSelector"])
        self.assertEqual(["Ingress", "Egress"], np["spec"]["policyTypes"])
        self.assertNotIn("ingress", np["spec"])
        self.assertNotIn("egress", np["spec"])
        self.assertEqual("node-fstrim", self.top["namespace"])
        flux = yaml.safe_load((REPO / "kubernetes/apps/clusters/ai/node-fstrim.yaml").read_text(encoding="utf-8"))
        self.assertEqual("./kubernetes/apps/node-fstrim", flux["spec"]["path"])
        self.assertTrue(flux["spec"]["prune"])

    # ---- the slot check bites --------------------------------------------------------------------
    def mutate(self, node, schedule):
        o = copy.deepcopy(self.overlays)
        o[node]["schedule"] = schedule
        return slot_problems(self.base, o)

    def test_two_nodes_in_one_slot_overlap(self):
        self.assertTrue(any("overlaps" in p for p in self.mutate("talos-cp3", "42 2 * * *")))

    def test_a_slot_too_close_to_the_next_overlaps(self):
        self.assertTrue(any("overlaps" in p for p in self.mutate("talos-cp3", "52 2 * * *")))

    def test_a_slot_into_the_offsite_sync_leaves_the_band(self):
        self.assertTrue(any("band" in p for p in self.mutate("relay-worker-1", "52 3 * * *")))

    def test_a_slot_during_velero_leaves_the_band(self):
        self.assertTrue(any("band" in p for p in self.mutate("talos-agent-node-1", "10 2 * * *")))

    def test_a_longer_job_deadline_overlaps(self):
        base = copy.deepcopy(self.base)
        base["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] = 600
        self.assertTrue(any("overlaps" in p for p in slot_problems(base, self.overlays)))


if __name__ == "__main__":
    unittest.main()
