# 2026-10-10 — retire the test-env pool (testpool / agent-sandbox / tep / talos-env-node-1)

## Context

The owner asked whether the leasable test-env pool is worth keeping. Decision (owner go,
2026-10-10): **remove it.** Evidence, measured 2026-10-10:

- **Off since 2026-09-26.** `SandboxWarmPool env-std-pool` is at `replicas: 0` (#880, after the
  shared golden-clone iSCSI target died). None of the restore steps in #972 has been started.
- **Unused before that.** There were zero leases from 09-12 to 09-25, while agents ran about 250
  Playwright and 300 compose sessions on the dev-workers (#865). In 15 days of Prometheus
  (`agent_sandbox_claim_creation_total`) there is one claim, the 09-26 test lease.
- **It never fit its workload.** The platform e2e stack declares about 17 GiB of limits for about 32
  long-running containers. An env is 8 Gi dind (an ~11 GiB Kata guest) on one 16 GiB node, and
  the platform's own plan says it "cannot host hatchet+workers". The platform repo never routed
  agents to it: `feat/e2e-pool-routing` was never merged.
- **It hurt shared infrastructure.** Its clone churn killed iSCSI target 11 (09-26). On 09-27 it
  left the dangling LUN 9 that broke **all** new `qnap-iscsi` provisioning, which blocked the
  `strive-pg` replica re-clone. Kata guest freezes wedged the env node's kubelet twice (09-20,
  09-24); that root cause is still open.
- **It still costs.** The env node takes 16 GiB fixed RAM and 8 vCPU on ai-node2 (about 24 GiB
  available there). It holds orphaned LUNs on the NAS, and each NAS attach/detach sweep is
  O(LUNs²) (`docs/runbooks/qnap-storage-setup.md` §9). It runs a controller, the env-reaper,
  cri-log-relay at containerd `[debug]`, alerts and dashboard rows. Every Talos upgrade carries the
  env node.
- **No isolation gain.** The testpool README calls it a "trusted-code-only pool, same posture as a
  dev-worker".

ADR 0037 (this branch) records the decision.

### Inventory (read-only, 2026-10-10; NAS config backups in `kubernetes/infra/_out/qnap-*.20261010-testpool-retire-inventory`)

| Layer | Object | State |
|---|---|---|
| k8s | ns `testpool`: SandboxTemplate `env-std`, SandboxWarmPool `env-std-pool` (0), SAs + token Secrets `tep-dw1..4`, DS `env-image-prepull`, NetworkPolicy, RBAC | Flux ks `testpool` (prune) |
| k8s | `agent-sandbox-system` controller + 4 CRDs | Flux ks `agent-sandbox` (prune). Nothing else uses the CRDs: no Sandbox, Template or Claim outside `testpool` |
| k8s | VolumeSnapshot `testpool/golden-v1` → VSC `snapcontent-c004c88c…` | `deletionPolicy: Delete`. Finalizers include a stale `volumesnapshot-as-source-protection` |
| k8s | 8 Released PVs, `testpool-iscsi` class, all `Retain` | `pvc-1aacdbdc`, `3e4f4cbc`, `43285e38`, `48c73270`, `743aa279`, `842a2790`, `864c27c9`, `f75344b3` |
| k8s | DS `kube-system/env-reaper`, Deploy `monitoring/cri-log-relay` (+ SOPS talosconfig), `testpool-rules`, `env-node-rules`, Alloy redaction stage for the relay, KSM warm-pool metrics, dashboard row | in the monitoring / testpool trees |
| k8s | StorageClass `testpool-iscsi`, RuntimeClass `kata-env` | testpool tree. **`kata` RuntimeClass and the kata schematic stay**: agent-node-3 uses them |
| Trident | 9 TridentVolumes (the 8 above + source `pvc-8e290587`, state `deleting`) + 1 TridentSnapshot | No publications or transactions. No log activity in 24 h: **inert** |
| NAS | LUNs **11** (`pvc-8e290587`, zfs284), **4** (`864c27c9`, zfs277), **43** (`f75344b3`, zfs316), **44** (`842a2790`, zfs317) | All 60 G thin, about 5 GiB used each, **unmapped** (no target). SCST devices present. Target 11 no longer exists |
| NAS | zvol `zpool1/orphan_placeholder_from_lun9_20260927` (+ `@tmpdel`, `@scratch`) | No LUN, no SCST device. Leftover of the 09-27 LUN-9 repair |
| NAS | 5 of the Released PVs (`1aacdbdc`, `3e4f4cbc`, `43285e38`, `48c73270`, `743aa279`) | **no LUN and no zvol**: the backend is already gone |
| VM | 4401 `talos-env-node-1`, ai-node2, `.37`, tofu `kubernetes/infra/env-pool` (state in the ops checkout) | Ready; DaemonSets only |
| dev-workers | `/usr/local/bin/tep`, `~/.tep/kubeconfig` (openbao-agent template), KV field `tep_kubeconfig`, the managed CLAUDE.md block, the feature-implementation/review-pr skill rule "lease tep" | Ansible `dev_worker`; daily converge 06:35 from `main` |
| sync | `openbao-k8stoken-sync` mints `tep_kubeconfig` for 4 slots, plus a mint Role in `testpool` | It mints ALL targets before writing any: a missing SA fails the whole run |

## Approach

Four PRs plus out-of-band steps, strictly in order. Each gate must hold before the next step:
reviewer bots auto-merge on approval (`reviewbot-auto-merge`), so a dependent PR is opened only
after its prerequisite is live.

### Step 1 — PR-A: the dev-worker side (ansible + agent guidance) + this plan + ADR 0037

- `tasks/tep.yml` becomes a removal task: `/usr/local/bin/tep` and `~<user>/.tep` absent for
  every `dev_worker_users` entry. Drop `dev_worker_tep_enabled` and `dev_worker_tep_namespace`.
  `dev_worker_tep_server` is used by the openbao kubeconfig check, so rename it to a neutral
  `dev_worker_kube_server` (same value).
- `tasks/openbao.yml` + `templates/openbao-agent.hcl.j2`: drop the tep template stanza, the
  `tep_kubeconfig` field from the KV pre-flight, the `~/.tep` verify (`get sandboxclaims`), `.tep`
  from the destination-dir loop, and the tep lines of the managed CLAUDE.md block. helmtest and
  platform stay byte-identical in behaviour.
- Delete `templates/tep-kubeconfig.j2`, `templates/tep-kubeconfig.ctmpl.j2`, `files/tep` and
  `ansible/secrets/tep-tokens.sops.yaml` (dead since the ADR 0021 cutover), plus the `.sops.yaml`
  rule for it.
- **Agent guidance:** replace the estate safeguard "Playwright / heavy compose never run directly
  on a dev-worker — lease a pool env (`tep`)" in `files/feature-implementation-skill.md` and the
  tep routing in `files/review-pr-skill.md`. New rule: follow the target repo's "Where tests run"
  section. Without one, run lint/unit/integration locally and leave full-stack e2e to CI. One
  full compose stack per worker at a time (`docker compose ls` first), `down -v` when done. For PRs
  not authored by the operator or estate bots, there is no local execution (static review only).
- **Gate 1:** PR-A merged. Run the fleet converge once by hand (`scripts/fleet-converge-bootstrap.sh`
  in WSL, same as the 06:35 task); it must exit 0. On dw1..dw4: `tep` absent, `~/.tep` absent,
  `systemctl is-active openbao-agent` active, `~/.helmtest/kubeconfig` and `~/.platform/kubeconfig`
  re-rendered and `kubectl auth can-i` passes with each. The reviewers' converge is unaffected
  (the role is not applied there).

### Step 2 — out-of-band, NAS-inert: retire the golden snapshot objects

The VolumeSnapshot is not in Flux's inventory: `hack/golden-refresh.sh` created it. Deleting the
namespace with it in place would call CSI DeleteSnapshot through a Trident path that loops on
NAS-side "not found". Each loop iteration is an O(LUNs²) sweep, and a stuck finalizer would leave
the namespace `Terminating`.

```sh
K="kubectl --context admin@ai"
$K patch volumesnapshotcontent snapcontent-c004c88c-5f06-4b70-9fd8-a828a44220f0 --type merge -p '{"spec":{"deletionPolicy":"Retain"}}'
$K -n testpool delete volumesnapshot golden-v1 --wait=false
# if still present after 2 min (stale as-source-protection; no PVC uses it as a source):
$K -n testpool patch volumesnapshot golden-v1 --type json -p '[{"op":"remove","path":"/metadata/finalizers"}]'
$K delete volumesnapshotcontent snapcontent-c004c88c-5f06-4b70-9fd8-a828a44220f0   # Retain: no CSI call
```

**Gate 2:** both objects are gone. Trident logs no `DeleteSnapshot` for `snapshot-c004c88c`.

### Step 3 — PR-B: the cluster side (opened only after Gate 1 and Gate 2)

- Delete `kubernetes/apps/clusters/ai/{testpool,agent-sandbox}.yaml` and the trees
  `kubernetes/apps/infrastructure/{testpool,agent-sandbox}/`. Flux deletes both Kustomizations and
  garbage-collects their inventory: namespace, CRDs, controller, StorageClass `testpool-iscsi`,
  RuntimeClass `kata-env`, `env-reaper`. No CR carries a finalizer.
- `security/openbao/k8stoken-sync.yaml`: drop the tep TARGETS and the mint Role/RoleBinding in
  `testpool`, **in the same commit** as the SA removal. A target whose SA is gone fails the whole
  sync run, and a Role in a deleted namespace fails the openbao Kustomization. The KV field
  `tep_kubeconfig` is left in place: a dead token, and no consumer after Gate 1.
- Monitoring: remove `testpool-rules{,.test}.yaml`, `env-node-rules{,.test}.yaml`,
  `cri-log-relay.yaml` + `cri-log-relay-talosconfig.sops.yaml`, the Alloy `cri-log-relay`
  redaction stage, and the KSM agent-sandbox custom-resource metrics. Remove the "Test Env Pool"
  row from `scripts/gen-reporting-dashboard.py` and regenerate `reporting-dashboard.yaml`.
- CI and scripts: `.gitea/workflows/env-image.yaml`, `env-image/`, the unittest/busybox steps in
  `manifests.yaml`, `scripts/{env-pool-soak.py,tep-render-kubeconfigs.py}` and their tests,
  `test_ready_watchdog.py`, `test-env-reaper.sh`, and the testpool paths in
  `test_manifest_paths.py`, `check-slot-enumerations.py` (+ its shell test) and
  `manifest-lint.sh`. Remove the velero `excludedNamespaces: testpool` entries and their
  recovery-contract comments, and the `renovate.json` mentions.
- **Deliberately left:** the `dedicated=env:NoSchedule` tolerations on velero node-agent, Alloy
  and storage-fabric-probe. They are harmless once no node has the taint, and removing them would
  roll three DaemonSets for nothing. Only their comments change. The Zot `testpool` sync prefix
  stays too: it needs a registry-LXC apply for no benefit, and its repos can be GC'd later.
- **Gate 3:** merged → GitHub mirror → Flux. All Kustomizations Ready. `testpool` and
  `agent-sandbox-system` gone, nothing stuck `Terminating`, `crd | grep agents.x-k8s.io` empty. A
  manual run of the token sync (`create job --from=cronjob/…`) completes, and the next daily
  converge is clean.

### Step 4 — out-of-band, NAS-inert: delete the 8 Released PV objects

All 8 are `Retain`, so `kubectl delete pv` makes no CSI call. Re-check `reclaimPolicy` immediately
before deleting. **Gate 4:** no `testpool-iscsi` PV remains.

### Step 5 — NAS: remove LUNs 11, 4, 43, 44 and the placeholder zvol (independent of k8s)

Constraints learned on 09-27: `qcli_iscsi -r` can print "Apply remove LUN ok" and do nothing.
Hand-editing `/etc/config/hero_*.json` or reloading the iSCSI service is an escalation, not part
of this plan.

1. Back up the config files again (`_out/qnap-*.20261010-before-testpool-lun-removal`).
2. For each LUN, **one at a time**: re-verify that its name is the expected `trident-pvc-<uuid>`,
   it has no target mapping (`hero_iscsi_scst.json`), no live PV/PVC references it, and its
   `blkdev_name` matches. Then remove it with the QuTS-native path: `qcli_iscsi -r lunID=<n>` from
   an authenticated qcli session (`qcli -l … saveauthsid=yes`), equivalent to the UI's remove.
3. Verify after each removal: the entry is gone from `hero_zfs_lun.json` and `qcli_iscsi -l`; the
   zvol and SCST device are gone; `iscsi_lun_setting.cgi?func=extra_get&lunList=1` returns
   `result 0` (the 09-27 estate-wide breakage showed up as `-22` here); Trident's
   `storage-api-server` logs no new `Lun is not ready`. **Stop at the first anomaly.**
4. After all four: confirm a fresh `qnap-iscsi` provision with a 1 Gi scratch PVC + pod (bind,
   write, delete). Then destroy the placeholder zvol: `zfs destroy -r
   zpool1/orphan_placeholder_from_lun9_20260927`, after checking its creation date is 2026-09-27
   and that no LUN or SCST device references it.

**Trident records** (9 TridentVolumes + 1 TridentSnapshot) **stay as inert residue**, documented
in ADR 0037. Driving their deletion through Trident calls the QNAP driver's delete, which loops on
NAS "not found" with an O(LUNs²) sweep per attempt (09-27 evidence). Deleting the CRs under a
running controller is unsupported. **Gate 5:** LUN count 55 → 51, provisioning probe green.

### Step 6 — destroy the env node (tofu, by hand from Windows)

1. Back up the state: `_out/env-pool.tfstate.20261010-pre-destroy`.
2. Run `tofu -chdir=kubernetes/infra/env-pool init -backend-config="path=…/home/ailab/…/terraform.tfstate"`,
   then `plan -destroy -out`. Expect exactly 5 destroys: VM, talos apply, labels, taint, and the
   import-only state (no other module touched).
3. `kubectl cordon`/`drain --ignore-daemonsets talos-env-node-1` (only DaemonSets), then
   `apply` the destroy plan. `stop_on_destroy` hard-stops the VM, and nothing on it needs saving.
4. `kubectl delete node talos-env-node-1`. Confirm the CiliumNode is gone and the node-exporter
   target has disappeared (no lasting `NodeExporterDown`/`KubeNodeNotReady`).

**Gate 6:** `qm list` on ai-node2 has no 4401 and its disk LV is gone. ai-node2 `MemAvailable` is up
by about 16 GiB. The dw2/dw4 balloons get that headroom automatically; no sizing change.

### Step 7 — PR-C: infra code + docs (opened after Gate 6)

- Delete `kubernetes/infra/env-pool/` (the state file stays in the ops checkout, gitignored and
  renamed `*.retired-20261010`), the `env-pool-*` justfile recipes, and `docs/runbooks/env-pool.md`.
- `CLAUDE.md`: modules/recipes lines and the inventory row. `docs/network-plan.md`: `.37` freed,
  the `.39` reservation released (#835 closed). Comments in `talos-schematics.yaml`,
  `infra/variables.tf`, `vms.tf`, `agent-nodes/{main,variables}.tf`, `reviewers/backend.tf`,
  `inventory/hosts.yml`, `dev-workers/variables.tf` (the "heavy stacks lease kata envs via tep"
  claim), `docs/runbooks/talos-upgrade.md` and the other runbooks that list the env node.
- ADR 0021/0028 and helmtest docs: drop the tep/testpool cross-references where they would now
  point at nothing. Historical `plans/` are not rewritten.
- **Gate 7:** merged; `just --list` has no env-pool recipe; a tofu plan of `infra/` and `agent-nodes/`
  is still `No changes` (comment-only edits).

### Step 8 — issues and the platform side

- Close #972 and #865 with the outcome. Leave a note on the `feat/env-node-2` branch (#835) that it
  is superseded.
- Platform PR (independent of 1–7): a "Where tests run" section in `CLAUDE.md`, taking the table
  from the unmerged `feat/e2e-pool-routing` without the pool.
  - Lint, unit and in-memory integration run locally.
  - The slim stack + targeted journeys run locally, one stack per worker.
  - The full journey matrix and the contract producer are CI-only.
  - k8s-only failure classes (netpol, Helm values, Kyverno, live drift) are caught by the
    helm-render guards and the post-deploy `ailab-journeys` lane.
  - While there, correct the stale "8-shard" line to 4. No AI attribution in that repo.

## Critical files

`ansible/roles/dev_worker/{tasks/tep.yml,tasks/openbao.yml,tasks/main.yml,defaults/main.yml,templates/openbao-agent.hcl.j2,files/*-skill.md}`,
`kubernetes/apps/clusters/ai/{testpool,agent-sandbox}.yaml`, `kubernetes/apps/infrastructure/{testpool,agent-sandbox}/`,
`kubernetes/apps/infrastructure/security/openbao/k8stoken-sync.yaml`, `kubernetes/apps/infrastructure/monitoring/*`,
`kubernetes/infra/env-pool/`, `scripts/{check-slot-enumerations.py,gen-reporting-dashboard.py}`, `CLAUDE.md`,
`docs/network-plan.md`, `docs/decisions/0037-retire-the-test-env-pool.md`; platform `CLAUDE.md`.

## Verification

Each gate above, plus, at the end:
- All Flux Kustomizations are Ready.
- `kubectl get pv | grep testpool` is empty, and the NAS LUN list holds 51 entries with
  `lunList` returning result 0.
- A `qnap-iscsi` provision probe is green.
- The 06:35 converge the next morning exits 0.
- ai-node2 `MemAvailable` is up by about 16 GiB.
- No `Testpool*` / `EnvNode*` series in `ALERTS`.

## Rollback

- **Steps 1–3** are git reverts, and Flux recreates the trees. The golden snapshot would need
  rebuilding (`golden-refresh.sh`), which #972 required anyway.
- **Step 6** is reversible by re-applying the module from git history: the VM is re-created from
  the staged image and joins as before.
- **Step 5** is not reversible. That is acceptable, because the four LUNs hold only a rebuildable
  docker cache.

<!-- codex-review-status: pending -->
