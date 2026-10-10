# 2026-10-10 — retire the test-env pool (testpool / agent-sandbox / tep / talos-env-node-1)

## Context

The owner asked whether the leasable test-env pool is worth keeping. Decision (owner go,
2026-10-10): **remove it.** ADR 0037 records it. Evidence, measured 2026-10-10:

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
- **It still costs.**
  - The env node takes 16 GiB fixed RAM and 8 vCPU on ai-node2 (about 24 GiB available there).
  - Its LUNs sit on the NAS, and every attach/detach sweep is O(LUNs²) (`qnap-storage-setup.md` §9).
  - The controller, env-reaper, cri-log-relay, alerts and dashboard rows keep running.
    (`kata_debug` has defaulted to false since 2026-10-09, so the relay now ships ordinary-level
    lines.)
  - Every Talos upgrade carries the env node.
- **No isolation gain.** The testpool README calls it a "trusted-code-only pool, same posture as a
  dev-worker".

### Inventory (read-only, 2026-10-10; NAS config backups in `kubernetes/infra/_out/qnap-*.20261010-testpool-retire-inventory`)

| Layer | Object | State |
|---|---|---|
| k8s | ns `testpool`: SandboxTemplate `env-std`, SandboxWarmPool `env-std-pool` (0), SAs + token Secrets `tep-dw1..4`, DS `env-image-prepull`, NetworkPolicy, RBAC | Flux ks `testpool` (prune). No finalizers on the CRs |
| k8s | `agent-sandbox-system` controller + 4 CRDs | Flux ks `agent-sandbox` (prune) |
| k8s | VolumeSnapshot `testpool/golden-v1` → VSC `snapcontent-c004c88c…` | `deletionPolicy: Delete`. Finalizers include a stale `volumesnapshot-as-source-protection`. The only snapshot/VSC in the cluster |
| k8s | 8 Released PVs, `testpool-iscsi` class, all `Retain` | `pvc-1aacdbdc`, `3e4f4cbc`, `43285e38`, `48c73270`, `743aa279`, `842a2790`, `864c27c9`, `f75344b3`. No VolumeAttachment names any of them |
| k8s | DS `kube-system/env-reaper`, Deploy `monitoring/cri-log-relay` (+ SOPS talosconfig), `testpool-rules`, `env-node-rules`, Alloy redaction stage for the relay, KSM warm-pool metrics + RBAC, dashboard row + Estate Health "Test Envs" tile | monitoring / testpool trees |
| k8s | StorageClass `testpool-iscsi`, RuntimeClass `kata-env` | testpool tree. **`kata` RuntimeClass and the kata schematic stay**: agent-node-3 uses them |
| k8s | spike leftovers (`testpool-spike`, `kata-env-spike`, `testpool-spike-iscsi`, `golden-spike-v2`) | **gone** (checked) |
| Trident | 9 TridentVolumes (the 8 above + source `pvc-8e290587`, state `deleting`) + TridentSnapshot `pvc-8e290587-snapshot-c004c88c…`; TridentNode `talos-env-node-1` | No publications, no transactions, no log activity in 24 h: **inert** |
| NAS | LUNs **4** (`864c27c9`, zfs277), **43** (`f75344b3`, zfs316), **44** (`842a2790`, zfs317), **11** (`pvc-8e290587`, zfs284, the golden source) | All 60 G thin, ~5 GiB used, **unmapped**, pool 1, real zvols, SCST devices present. The clone zvols have `origin -`: full copies, not ZFS clones. Target 11 no longer exists |
| NAS | zvol `zpool1/orphan_placeholder_from_lun9_20260927` (+ `@tmpdel`, `@scratch`) | No LUN, no SCST device. Leftover of the 09-27 LUN-9 repair |
| NAS | 5 of the Released PVs (`1aacdbdc`, `3e4f4cbc`, `43285e38`, `48c73270`, `743aa279`) | **no LUN and no zvol** |
| VM | 4401 `talos-env-node-1`, ai-node2, `.37`, tofu `kubernetes/infra/env-pool` (state + tfvars in the ops checkout) | Ready; DaemonSets only |
| dev-workers | `/usr/local/bin/tep`, `~/.tep/kubeconfig` (openbao-agent template), KV field `tep_kubeconfig`, managed CLAUDE.md block, the skill rule "lease tep" | Ansible `dev_worker`; daily converge 06:35 from `main` |
| sync | `openbao-k8stoken-sync` mints `tep_kubeconfig` for 4 slots + a mint Role in `testpool` | Mints ALL targets before writing any |
| outside ailab | `agentforge` (`dashboard/e2e-pool.sh`) and `agentforge-platform` (`webapp/e2e-pool.sh` + a unit test that requires Playwright inside `tep run`) wrap `tep` | Already failing since 09-26 (pool at 0). AgentForge is out of scope (owner 2026-10-08): **flagged, not changed** |
| legacy tokens | `serviceaccount_legacy_tokens_total` on cp1 rose 3× on 09-26 (the #877 live test) and 3× on 10-03 ~19Z | The client cannot be identified (apiserver audit logs rotate within hours). Its next attempt gets a 401 from a pool that has been off since 09-26. Accepted |
| platform | `platform-access/rbac.yaml` grants read on `agents.x-k8s.io` kinds ("App sandboxes") | Platform main never references agent-sandbox (grep, 2026-10-10); airlock uses plain pods. The RBAC rule becomes inert and is removed in PR-B |

## Approach

Four ailab PRs plus out-of-band steps, strictly in order. Each gate must hold before the next
step. The reviewer bots auto-merge on approval (`reviewbot-auto-merge`), so a dependent PR is opened
only after its prerequisite is live. Every live change is watched at the time, never left to the
06:35 converge.

### Step 1 — PR-A: the worker side, the sync side, this plan and ADR 0037

- **`tasks/tep.yml` becomes guarded removal tasks,** imported **unconditionally** after
  `openbao.yml` in `main.yml`. It removes `/usr/local/bin/tep`, then `~<user>/.tep` and
  `/etc/openbao-agent/tep-kubeconfig.ctmpl` for every `dev_worker_users` entry, **only if**:
  - `agent.hcl` has no `tep-kubeconfig` stanza, and
  - the running agent started after `agent.hcl` was written.

  Otherwise the agent could re-create `~/.tep` as root, or a rescued `openbao.yml` (which restores
  the old `agent.hcl`) could lose its template source. `dev_worker_tep_enabled` and
  `dev_worker_tep_namespace` go; `dev_worker_tep_server` becomes `dev_worker_kube_server` (same
  value).
- **`openbao.yml` + `openbao-agent.hcl.j2`:**
  - drop the tep template stanza and the `.tep` destination dir;
  - stop laying down the tep `.ctmpl` (the file itself is removed by `tep.yml`, after the verify);
  - drop `tep_kubeconfig` from the KV pre-flight and the `get sandboxclaims` verify;
  - rewrite the texts that describe the SOPS writer: the rescue comment, the fail message, the
    marker content, and the CLAUDE.md block, including "cannot deploy into `testpool`".
- **Deletions:**
  - `files/tep`, `templates/tep-kubeconfig{.j2,.ctmpl.j2}`;
  - `ansible/secrets/tep-tokens.sops.yaml` with its `.sops.yaml` rule, **and**
    `scripts/tep-render-kubeconfigs.py`. Leaving the script behind its deleted rule would open a
    plaintext window;
  - move the `.ctmpl` contract text into `helmtest-kubeconfig.ctmpl.j2`.
- **The sync side moves here from PR-B.** `openbao` dependsOn `infrastructure` (wait:true), so a
  single PR-B commit would not be atomic: flux-system can prune the tep SAs well before the new
  script applies.
  - `k8stoken-sync.yaml` drops the tep TARGETS and its mint Role/RoleBinding in `testpool`. That
    is safe while the SAs still exist.
  - The sync PATCHes, so `tep_kubeconfig` stays in KV and a worker that has not converged keeps
    rendering it.
- **Agent guidance:**
  - The feature-implementation estate safeguard and the review-pr execution rule now defer to the
    target repo's "Where tests run" section. Without one: lint/unit/in-memory integration run
    locally and full-stack e2e runs in CI on the PR.
  - At most **one** compose stack per worker at a time (`docker compose ls` first, `down -v` when
    done), whatever a repo says. A repo section may narrow this, never widen it.
  - The review skill reads conventions from the BASE commit (it already does) and never re-runs
    full-stack/Playwright tiers locally.
- **Gate 1:**
  - PR-A merged; Flux `openbao` `lastAppliedRevision` = the merge SHA.
  - A manual `create job --from=cronjob/openbao-k8stoken-sync` completes with `validated 8/8`.
  - Then a hand-run fleet converge: `converge.log` shows the PR-A SHA as `source:` and four
    dev-worker PLAY RECAPs with `failed=0`. Exit 0 alone proves nothing; the lock path also exits 0.
  - On dw1..dw4:
    - `grep -c tep-kubeconfig /etc/openbao-agent/agent.hcl` = 0;
    - `tep` and `~/.tep` absent;
    - `openbao-agent` active;
    - `~/.helmtest/kubeconfig` and `~/.platform/kubeconfig` pass a live `kubectl` call.

### Step 2 — out-of-band, NAS-inert: retire the golden snapshot objects

The VolumeSnapshot is not in Flux's inventory: `golden-refresh.sh` created it. If the namespace is
deleted with it in place, CSI DeleteSnapshot runs through Trident's QNAP not-found loop.

**Pre-checks:**
- the VSC has no `deletionTimestamp` and no `volumesnapshot-being-deleted` annotation;
- `kubectl -n testpool get pvc` is empty.

```sh
K="kubectl --context admin@ai"
$K patch volumesnapshotcontent snapcontent-c004c88c-5f06-4b70-9fd8-a828a44220f0 --type merge -p '{"spec":{"deletionPolicy":"Retain"}}'
$K -n testpool delete volumesnapshot golden-v1 --wait=false
# if still present after 2 min (stale as-source-protection; no PVC uses it as a source):
$K -n testpool patch volumesnapshot golden-v1 --type json -p '[{"op":"remove","path":"/metadata/finalizers"}]'
$K delete volumesnapshotcontent snapcontent-c004c88c-5f06-4b70-9fd8-a828a44220f0   # Retain: no CSI call
```

The TridentSnapshot **stays**: it keeps the source volume parked in `deleting`. Deleting it alone
would let Trident delete the parent and re-enter the not-found loop (ADR 0037).

**Gate 2:** both objects are gone. Neither Trident's `trident-main` log nor the `csi-snapshotter`
sidecar log shows a `DeleteSnapshot` for `snapshot-c004c88c`.

### Step 3 — PR-B: the cluster side (opened after Gates 1 and 2)

- **Delete the trees.** Remove `clusters/ai/{testpool,agent-sandbox}.yaml` and
  `infrastructure/{testpool,agent-sandbox}/`. Flux garbage-collects namespace, CRDs, controller,
  StorageClass `testpool-iscsi`, RuntimeClass `kata-env` and `env-reaper`.
  **Pre-gate:**
  - both Kustomizations are Ready and **not suspended** (deleting a suspended one skips GC);
  - their `.status.inventory` lists the Namespace, SC, RuntimeClass and CRDs;
  - every object in `testpool`, and every `*.agents.x-k8s.io` CR, has empty finalizers. The CRDs
    may then go concurrently with the CRs; the vendored manifest has no webhooks.
- **Monitoring:**
  - remove `testpool-rules{,.test}`, `env-node-rules{,.test}`, `cri-log-relay` and its talosconfig;
  - remove the KSM SandboxWarmPool custom-resource metrics **and** their `rbac.extraRules`;
  - drop the dashboard's Test Env Pool row **and** the Estate Health "Test Envs" tile; the three
    remaining tiles share the row.
  - The Alloy redaction stage stays until PR-C, so it outlives the relay.
  - The relay's talosconfig cannot be revoked: the os:reader cert expires about 2026-12-20, and
    the ADR records it.
- **CI and scripts:**
  - remove `env-image.yaml` and `env-image/`, plus the unittest and busybox steps in
    `manifests.yaml`;
  - remove `env-pool-soak.py` and its test, `test_ready_watchdog.py` and `test-env-reaper.sh`;
  - remove the testpool paths from `test_manifest_paths.py`, `check-slot-enumerations.py` (+ its
    test) and `manifest-lint.sh`;
  - drop the talosctl Renovate bound, whose only consumer was the relay;
  - drop the agent-sandbox read rules in `platform-access/rbac.yaml`.
- **Deliberately not changed (no rollout for nothing):**
  - the velero `excludedNamespaces: testpool` entry; only its comments change. YAML comments never
    reach the API, so the HelmRelease does not upgrade;
  - the `dedicated=env` tolerations on Alloy, velero node-agent and storage-fabric-probe (comments
    only);
  - the Zot `testpool` sync prefix.
- **Gate 3:**
  - All Kustomizations are Ready.
  - `ns testpool`, `ns agent-sandbox-system`, `sc testpool-iscsi`, `runtimeclass kata-env` and
    `crd *.agents.x-k8s.io` are all NotFound, with nothing `Terminating`.
  - The token sync and KSM logs are clean.

### Step 4 — out-of-band, NAS-inert: delete the 8 Released PV objects

Immediately before deleting, check that:
- `reclaimPolicy: Retain`;
- the finalizers hold only `kubernetes.io/pv-protection` (no `external-attacher/…`);
- no VolumeAttachment names the PV or `talos-env-node-1`.

Delete one PV and watch the Trident and attacher logs for 5 min, then delete the rest.
**Gate 4:** no `testpool-iscsi` PV, and no new Trident or attacher activity.

### Step 5 — NAS: remove LUNs 4, 43, 44, then 11, and the placeholder zvol

Hand-editing `/etc/config/hero_*.json` or reloading the iSCSI service is an escalation, not part of
this plan; 09-27 showed that `qcli_iscsi -r` can print "ok" and do nothing.

1. **Pre-gates:**
   - a quiet window: no Pending PVC, every VolumeAttachment attached, no CNPG re-clone;
   - `tridenttransactions` empty;
   - last night's Velero and CNPG backups completed;
   - no iSCSI errors in `talosctl dmesg` on the CPs.

   Back up the config files again (`_out/qnap-*.20261010-before-testpool-lun-removal`), plus the
   full `qcli_iscsi -l` / `-L` output.
2. **For each LUN, one at a time.** The clones go first; the source (11) goes last.
   - Resolve its `lunID` **from the expected `trident-pvc-<uuid>` name** right before the call.
   - Re-verify: unmapped, pool 1, zvol `blkdev_name` as inventoried, no PV references it.
   - Remove it with `qcli_iscsi -r lunID=<n>` in a qcli session opened through
     `scripts/qnap-ssh.py`, and log the session out afterwards.
   - Never retry by ID alone.
3. **Verify after each removal:**
   - the full LUN and target-mapping lists, diffed against the backup: **only** that entry is gone;
   - its zvol and SCST device are gone;
   - `iscsi_lun_setting.cgi … lunList` returns `result 0`;
   - no new `Lun is not ready` in `storage-api-server`;
   - no iSCSI errors on the nodes.

   **Stop at the first anomaly.**
4. **After all four:**
   - Run a provisioning probe: a 1 Gi `qnap-iscsi` PVC + pod (bind, write). Patch its PV to
     `Delete` **before** deleting the PVC, so the class's Retain policy does not leak a new LUN,
     and confirm that its LUN disappears.
   - Destroy the placeholder: run `zfs get -r clones,origin` and `zfs destroy -nvr` on it first,
     check it was created 2026-09-27 with no LUN or SCST reference, then
     `zfs destroy -r zpool1/orphan_placeholder_from_lun9_20260927`.

**Trident records** (9 TridentVolumes, 1 TridentSnapshot, listed in ADR 0037) stay as inert
residue. Driving their deletion through Trident loops on NAS "not found", with an O(LUNs²) sweep
per attempt; deleting the CRs under a running controller is unsupported. They come out at the next
planned Trident/operator restart, with controller and operator scaled to 0.

**Gate 5:**
- exactly the four expected names are gone and every other LUN's name, ID and mapping is unchanged;
- the probe is green and its LUN is gone;
- `lunList` returns result 0.

### Step 6 — destroy the env node (tofu, by hand from Windows)

1. Back up the state to `_out/env-pool.tfstate.20261010-pre-destroy`.
2. From a main worktree, run `tofu init -reconfigure -backend-config=path=<ops>/…/terraform.tfstate`
   and `-var-file=<ops>/…/terraform.tfvars` (tfvars exists only in the ops checkout).
3. Run `tofu state rm` on `kubernetes_labels.env_pool` and `kubernetes_node_taint.env`, so the
   destroy cannot lift the taint and let non-tolerating DaemonSets land on the node.
4. `plan -destroy -out`: expect **2 to destroy** (the VM and the no-op talos apply) and nothing
   else.
5. Cordon and `drain --ignore-daemonsets` the node, then `apply` the plan. `stop_on_destroy`
   hard-stops the VM, and nothing on it needs saving.
6. `kubectl delete node talos-env-node-1`. Watch Trident during the delete.

**Gate 6:**
- `qm list` on ai-node2 has no 4401, and its disk is gone;
- the CiliumNode and TridentNode for the node are gone, and no VolumeAttachment names it;
- no lasting node alerts;
- `.37` no longer answers.

Then measure ai-node2's used % and dw2/dw4's balloon sizes. The headroom is real only below PVE's
~80% auto-balloon threshold. Report the numbers; this plan makes no sizing change.

Finally, age-encrypt the retired state file and the pre-destroy backup (they embed the cluster
machine secrets via remote state) and shred the plaintext copies.

### Step 7 — PR-C: infra code, docs, and the Alloy stage (opened after Gate 6)

- **Delete:**
  - `kubernetes/infra/env-pool/`;
  - the `env-pool-*` justfile recipes;
  - `docs/runbooks/env-pool.md`; ADR 0037 summarises its Kata lessons and links to the last
    commit that had it.
- **Remove the Alloy `cri-log-relay` redaction stage.** The relay is gone by now.
- **Edits:**
  - `CLAUDE.md` (modules, recipes, IPAM line, inventory row);
  - `docs/network-plan.md`: `.37` free, `.39` released;
  - `talos-upgrade.md`: no env node in the kubelet roll, `talos_version` in two modules, the kata
    smoke on agent-node-3;
  - `node-maintenance.md`;
  - comments that describe the env node as current, in `talos-schematics.yaml`,
    `infra/variables.tf`, `agent-nodes/variables.tf`, `dev-workers/variables.tf` and `inventory`.
- **Not touched:**
  - `machine-config/controlplane.yaml.tftpl`: it is rendered into the CP config, so even a comment
    edit would show in the CP plan;
  - history sections, ADRs other than 0021 (a "superseded in part by 0037" line), `plans/` and
    `docs/superpowers/`.
- **Gate 7:** merged; `just --list` has no env-pool recipe; tofu plans of `infra/` and
  `agent-nodes/` are `No changes`.

### Step 8 — issues and the platform side (after Gate 6)

- #972: close with the outcome. #835's branch: note that it is superseded.
- #865: check every item first. It is also cited for the attacher timeout (`qnap-storage-setup.md`
  §9, fixed by #908) and for the registry pull identity (fixed by #875/#877). Close it only if
  nothing is left open; otherwise comment.
- Update `qnap-storage-setup.md` §9's orphan-LUN paragraph.
- Report the AgentForge `e2e-pool.sh` dependency to the owner (out of scope for changes).
- Platform PR: the "Where tests run" section in `CLAUDE.md`, and "8-shard" → "4-shard". Lands after
  Gate 6, so the slim-stack guidance arrives once node2's RAM is back. No AI attribution in that
  repo.

## Critical files

`ansible/roles/dev_worker/{tasks/tep.yml,tasks/openbao.yml,tasks/main.yml,defaults/main.yml,templates/openbao-agent.hcl.j2,files/*-skill.md}`,
`kubernetes/apps/infrastructure/security/openbao/k8stoken-sync.yaml`, `kubernetes/apps/clusters/ai/{testpool,agent-sandbox}.yaml`,
`kubernetes/apps/infrastructure/{testpool,agent-sandbox}/`, `kubernetes/apps/infrastructure/monitoring/*`,
`kubernetes/infra/env-pool/`, `scripts/{check-slot-enumerations.py,gen-reporting-dashboard.py}`, `CLAUDE.md`,
`docs/network-plan.md`, `docs/decisions/0037-retire-the-test-env-pool.md`; platform `CLAUDE.md`.

## Verification

Each gate above, plus, at the end:
- All Flux Kustomizations are Ready, and no `Testpool*` / `EnvNode*` series appear in `ALERTS`.
- Nothing pool-related remains:
  - no `testpool-iscsi` PV, and `tridenttransactions` empty;
  - no TridentNode or VolumeAttachment for `talos-env-node-1`;
  - none of the spike names;
  - no `tep-kubeconfig` in any worker's `agent.hcl`.
- The NAS LUN set equals the pre-removal set minus the four, `lunList` returns result 0, and a
  `qnap-iscsi` provision probe is green.
- The 06:35 converge the next morning shows `failed=0` for all four workers.

## Rollback

- **Steps 1–3:** revert PR-B **before** PR-A. The restored verify block needs the tep SAs and CRDs
  back first. Flux recreates the trees; the golden snapshot would need rebuilding (#972 required
  that anyway).
- **Step 6 is not a re-apply.** A new VM needs `apply_mode = "auto"`, no `imports.tf`, and a
  v1.14.2 agent image staged on ai-node2. That is new provisioning, accepted.
- **Step 5 is irreversible.** That is accepted: the four LUNs hold only a rebuildable docker cache.

## Review trail

**Round 1 (2026-10-10).** Codex was unavailable: the LiteLLM route returned 429 and the native seat
is capped until 10-14. An independent Claude Plan agent reviewed the draft (`90a80218`) instead,
against the repo. Dispositions:

- **Accepted, and folded in above:**
  - **Ordering:**
    - the token-sync change moves to PR-A (Flux non-atomicity);
    - Flux GC pre-gates;
    - the Alloy stage removal moves to PR-C;
    - the NAS order is clones first; LUN IDs are resolved by name;
    - `tofu state rm` of the label/taint before the destroy;
    - revert PR-B before PR-A;
    - the platform PR lands after Gate 6.
  - **Gates and checks:**
    - real converge evidence for Gate 1;
    - VSC pre-checks and the csi-snapshotter log;
    - PV finalizer and VolumeAttachment checks;
    - NAS quiet window, backups, set-based diff and node iSCSI health;
    - TridentNode check;
    - measuring balloon headroom instead of assuming it;
    - extra end-state checks.
  - **Code:**
    - the `tep.yml` guard and unconditional import;
    - remove the stale `.ctmpl` after the verify;
    - delete the render script in PR-A;
    - move the contract text;
    - rewrite the stale texts;
    - the KSM `extraRules`;
    - the Estate Health tile;
    - the platform-access agent-sandbox rules;
    - leave the velero value alone;
    - leave `controlplane.yaml.tftpl` alone.
  - **Facts:**
    - 2 destroys after the state rm, not 5;
    - `kata_debug` is off;
    - tfvars exist only in the ops checkout;
    - the probe PV must be patched to Delete;
    - the placeholder needs a dry-run destroy;
    - the talosconfig cert expires ~12-20;
    - the TridentSnapshot must stay;
    - the residue is listed in ADR 0037;
    - age-encrypt the retired state;
    - the Step 6 rollback is not a re-apply;
    - #865's wider scope;
    - grep the sibling repos (found the AgentForge wrappers);
    - spike leftovers (verified gone);
    - the legacy-token user (dated; not identifiable).
- **Pushed back:**
  - **Deleting `tep_kubeconfig` from KV after a soak.** After Gate 1 no worker's agent references
    the field (grep on every host), so a vault wipe cannot hurt anything. Deleting it would be a
    write into every worker's KV path for hygiene only. It is dropped from the recovery docs
    instead.
  - **Keeping `qcli -l` off the command line entirely.** It is partly accepted: the session goes
    through `qnap-ssh.py` (no shell history) and is logged out afterwards. The password still
    appears briefly in the NAS process table, as in `scripts/qnap-setup.sh`. The NAS has a single
    admin and no other interactive users.

<!-- codex-review-status: finalized -->

## Execution log

- **Step 1 (PR-A, #1217, merged as `658e5858`).** Flux `openbao` applied it, and a manual
  `openbao-k8stoken-sync` run printed `validated 8/8 fields`. The testpool mint Role was pruned.
  - Review rounds hardened `files/tep-retire-gate.sh`: nanosecond timestamps, and fail-closed short
    of a confirmed stop. They also fixed the stale-backup `find` (`read_whole_file`).
- **Gate 1 — deviation.** The hand-run converge was killed by the workstation's Claude Code harness
  (host memory pressure) before it touched any host; `agent.hcl` on dw1..dw4 is unchanged since
  09-30. A read-only check then showed that **no worker ever cut over to agent-rendered kubeconfigs**
  (ADR 0021 phase 3).
  - No host has `/etc/openbao-agent/renders-kubeconfigs`.
  - `agent.hcl` has no kubeconfig stanzas.
  - `~/.helmtest/kubeconfig` was never rendered.
  - `~/.tep/kubeconfig` is the SOPS-written file with the legacy `tep-dwN-token`. That is the
    unidentified legacy-token client.

  So the hazard Gate 1 guarded against cannot occur, because the converge's tep/helmtest verify
  only runs on cut-over hosts. PR-B proceeds on the sync evidence alone. Each worker's `tep`/`~/.tep`
  cleanup lands at its next converge: `tep-retire-gate.sh` reads `safe` there (no stanza; agent
  started 10-06, after the 09-30 config).
  - `converge.log`'s last entry is the 2026-10-08 06:35 run: the scheduled converge did not log
    on 10-09 or 10-10.
- **Step 2 (Gate 2 ✓).** VSC set to `Retain`. The VolumeSnapshot and VSC are gone. csi-snapshotter
  only removed its finalizer; no `DeleteSnapshot`.
- **Step 5 (Gate 5 ✓), done before Step 4 (both are k8s-independent).**
  - Pre-gates held: no Pending PVC, all VAs attached, no transactions, last night's Velero
    `Completed`, CNPG archiving healthy, no iSCSI errors.
  - A provisioning probe before the removals passed: bind, attach and write in 30 s; deleting it
    with `Delete` removed its LUN.
  - The `lunList` CGI answered `-1/-22` while provisioning was healthy, so it is not a health signal.
  - LUNs 4, 43, 44 and 11 were removed one at a time. Each removal left the full LUN and target
    lists unchanged except for that entry, with its zvol and SCST device gone. 55 → 51; set check:
    exactly the four expected names gone.
  - The placeholder zvol could not be destroyed (QuTS: "cannot destroy snapshots: permission
    denied", no holds). It is left, 84.8K and unreferenced.
- **Step 4 (Gate 4 ✓).** The 8 Released PVs are deleted. Each carried a stale
  `external-attacher/csi-trident-qnap-io` finalizer, which the attacher removed itself ("no VA
  found"). The provisioner made no `DeleteVolume`, and Trident and storage-api-server logged
  nothing about them.
- **Step 3 pre-gates ✓.** Both Kustomizations are Ready, not suspended, with prune on. Their
  inventories list the Namespace, SC, RuntimeClass and 4 CRDs. No finalizer exists in `testpool`
  or on any agent-sandbox CR.
  - `kubectl get sandboxes,sandboxclaims -A` is empty cluster-wide. The only CRs are testpool's
    template and warm pool, and the `strive-sandboxes-ailab` pods are plain pods with no owner.
    The platform-access comment "App sandboxes in strive-sandboxes-ailab" was stale.
- **Step 3 split (review, #1219).**
  - The review bots skip a PR whose reviewable diff exceeds 400 KB, and the vendored 425 KB
    agent-sandbox manifest alone exceeds it.
  - `dependsOn` orders reconciliation, not deletion, so pruning both Kustomizations in one commit
    could delete the CRDs before testpool's own GC prunes its CRs.
  - So PR-B removes **only `testpool`**. A follow-up removes the `agent-sandbox` Kustomization and
    deletes its source tree, in two review-sized halves.
  - Gate 3 adds "no Kustomization stuck in deletion". After PR-B, Gate 3 expects only
    `ns testpool`, `sc testpool-iscsi` and `runtimeclass kata-env` NotFound. `agent-sandbox-system`
    and the CRDs stay until the follow-up.
  - **Accepted gap:** `TestpoolOperatorDown` goes with the testpool rules in PR-B, while the
    agent-sandbox controller keeps running until the follow-up. It is unmonitored for that window,
    but it manages nothing (no CRs once testpool is pruned), so an outage there costs nothing.
