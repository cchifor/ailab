# Retire AgentForge and every deployment that exists only for it

## Context

The owner decided on 2026-10-31 to remove AgentForge and all its dependent deployments ("Remove the AgentForge and all the dependent deployments"; "Address all the issues yourself. I approve!").

The playground was retired the day before (`plans/2026-10-30-retire-playground-plan.md`, #1505). What remains:
- the control plane `agentforge-platform`;
- five brokers;
- the provisioner;
- the reaper;
- the sandbox namespace and its boundary canary;
- one CP-rendered tenant workspace, `tenant-zero/platform-dev`. Its pool runs on GC'd images (#1519), and it has created no sandbox Job since 2026-08-17;
- the parked conductor;
- codex-refresh (suspended);
- pkgcache;
- `agentforge-ci-runners` (dormant: its ScaledJob was never listed);
- KEDA, which no longer has a single ScaledObject or ScaledJob.

**Kept, because something outside AgentForge needs it** (measured 2026-10-31):
- **OpenBao runs AgentForge code.** `openbao-init`, `openbao-unsealer` and `openbao-provision` all use `agentforge/orchestrator@sha256:ed8c94fa…`, and the `af` KV mount, k8s auth and canary are created by `agentforge openbao-provision`.
  - Removing the image would leave OpenBao sealed after its next restart, and ESO, dsh, litellm, strive, workbricks and the dev workers would break.
  - These three stay, as does the image (present, HTTP 200), its zot `agentforge/**` retention policy and the Renovate disable rule.
  - Replacing them with plain `bao` Jobs is a separate follow-up.
- **Workbricks** uses the kata/gVisor RuntimeClasses, so `agentforge-runtimeclasses` stays (renaming it is a follow-up).
- **The three Talos agent nodes** run llm-router, llm-router-edge and relay (including relay-postgres), so they stay, with their `dedicated=agent` taint and Talos extensions.
- **litellm-local** serves Strive and the dev-worker diag key. AgentForge was only one consumer.
- **Not AgentForge:** dsh's own conductor, `agent-sandbox/` (ADR 0037), and kro. kro is not touched here, because external repos (platform, workbricks) may use it.

## Approach

Six PRs in order, each verified live before the next. Owner-level steps run with one-shot owner credentials, under explicit owner approval. Data deletions come last, after backups. Flux deletes child Kustomizations concurrently and ignores dependsOn, so each ordering point is its own PR.

### PR-0: preparation (deletes nothing)
1. **Move the full `scripts/tests` unittest step out of `.gitea/workflows/broker-inventory.yaml`** into a new `.gitea/workflows/script-tests.yaml`.
   - Same Python bootstrap, path-filtered on `scripts/**` and `.gitea/workflows/script-tests.yaml`, plus a required context if contexts files demand it.
   - Otherwise deleting broker-inventory would silently stop every script test.
2. **Fix #1513** (`test_reviewbot.py` leaks a fake `subprocess.run`), using `mock.patch.object`. A one-line, self-contained change that runs in the new workflow.
3. **Stop the AgentForge workloads.** Set `replicas: 0` for:
   - `agentforge-platform` (the CP, which also stops its tenants reconciler);
   - all five brokers;
   - the reaper;
   - npm-cache and pypi-cache.

   **Keep the provisioner running:** it owns the finalizers on the tenant namespace and BrokerSeats.

   The platform-dev pool lives in the `agentforge-tenants` repo and goes in PR-1.

**Verify:** the pods are gone, and alerts are only the expected AgentForge target-down noise, silenced for the change window, or the rules are deleted in PR-2. Everything is reversible by revert.

### PR-1: tenants
- Delete `clusters/ai/agentforge-tenants.yaml` and `agentforge-tenant-platform-dev.yaml`, and the `agentforge-tenants` GitRepository if it is separate.
- Keep `agentforge-tenants-git-auth`: the tenants Kustomization impersonates the SA it creates, and Flux GC needs it.

**Verify:**
- namespace `af-tenant-tenant-zero-platform-dev` is gone;
- the provisioner logged its teardown (role and policy deleted);
- `get ns af-tenant-*` is empty;
- no BrokerSeat exists (already none).

### PR-2: AgentForge workloads, CP, admission, monitoring, CI
1. **Delete these Flux Kustomizations and their dirs:** `agentforge-broker`, `-sandbox`, `-workers`, `-pkgcache`, `-codex-refresh`, `-ci-runners`, `-tenants-git-auth`.
2. **Delete `apps/agentforge/`** (the CP and the admission VAPs), and drop it from `apps/kustomization.yaml`.
3. **Monitoring:** delete the AgentForge rule files together with their promtool tests.
4. **Scripts, tests and the justfile:** delete the AgentForge scripts and their tests together (the classification list), and the af-* recipes. Edit `check-inline-hashes.py` and `test_manifest_paths.py`.
5. **Workflows:** delete `.gitea/workflows/tenant-guard-cel.yaml`, and `broker-inventory.yaml` (its tests moved in PR-0).
6. **Other surfaces:**
   - gatus and homepage entries;
   - the Authelia `agentforge` OIDC client and its claims policy;
   - the cloudflared ingress;
   - the edge hostname entry, then `just edge-render`, and add the hostname to `test_edge_render.py`'s `DROPPED`. Cloudflare DNS (tofu) is applied by hand after merge.
7. **npm-cache's Retain iSCSI PV:** before the merge, patch it to `persistentVolumeReclaimPolicy: Delete`, so Trident deletes the LUN with the PVC. It is a disposable cache, and a Retain PV would leave a dangling QNAP LUN.

**Verify:**
- the namespaces `agentforge`, `agentforge-broker`, `agentforge-sandbox`, `agentforge-pkgcache` and `agentforge-ci` are gone;
- the cluster-scoped VAPs and bindings are gone;
- the `brokerseats` CRD is gone;
- the npm-cache LUN is deleted on the QNAP;
- every Flux unit is Ready;
- CI is green on the next PRs.

### PR-3: provisioner and KEDA
1. **OpenBao:** delete `provisioner-deploy.yaml`, `provisioner-podmonitor.yaml` and `capability-kids-configmap.yaml`, plus the provisioner SA/Role/RoleBinding in `unseal-rbac.yaml`. Remove the codex-hosts-publisher policy and role from `devworker-provision-job.yaml`.
2. **KEDA:** first confirm that `get scaledobjects,scaledjobs -A` is empty, then delete `clusters/ai/keda.yaml`, `autoscaling/keda/` and `sources/keda.yaml`.
3. **Monitoring:** drop `keda` from the `platform-rules.yaml` regex, and swap the series in its test.

**Verify:**
- the KEDA CRDs are gone;
- the `v1beta1.external.metrics.k8s.io` APIService is gone, since a dangling one hangs namespace deletion cluster-wide;
- the `keda` namespace is gone;
- OpenBao is unsealed and healthy, and `openbao-canary` is green.

### PR-4: data and identity (irreversible parts, after backups)
1. **infra-pg:**
   - Take a fresh dump: `kubectl create job --from=cronjob/infra-pg-dump`, and verify it contains `agentforge_platform` and `agentforge_broker`.
   - Then `DROP DATABASE` both by hand.
   - Then, in the PR: set the five roles to `ensure: absent`; remove the afp_* postInitSQL and the passwordSecret references; delete `infra-pg-afp-{admin,app}.sops.yaml`; edit the kustomization. The databases Kustomization is `wait: true`, so all of this lands in one commit.
2. **OpenBao seeds:** remove the AgentForge paths from `operator-seeds.sops.yaml` (`seeds.json`), so the daily provision run cannot recreate them.
   - Edit through sops with the editor shim, and assert `ENC[` afterwards.
   - Update the `openbao-recovery.md` seed table.
3. **Gitea admin token `af-ci-scaler-2941`:** delete it in Gitea first; then the credential-inventory allowlist SQL, the alert text and its test, in the same PR.

### Manual steps (after the PR that frees each)
- **OpenBao live cleanup** (key names only in any output), after PR-3:
  - delete the KV paths `af/operator/{broker/*,reaper,ci,conductor,dispatcher}/*`, `af/tenants/*` and `af/dev-workers/codex-auth`;
  - delete the AgentForge k8s-auth roles and policies (af-operator-{broker,ci,reaper,conductor,dispatcher}, af-codex-refresher, codex-hosts-publisher, af-tenant-*, af-dispatcher-eso, the provisioner's scoped policy);
  - keep `af/operator/canary` and every non-AgentForge role.

  These run with the least-privileged identity that can delete each item. The `openbao-provision` bootstrap token covers policies and roles. Before using a root-equivalent, read which token can delete `operator/*` KV.
- **Gitea, with a one-shot owner token** (minted in the gitea pod, used via stdin, deleted from the DB, then proven dead with a 401):
  1. Close the pin PRs #1518 and #1521 as obsolete.
  2. Delete webhooks 1–4 (all target AgentForge).
  3. Remove the playground bot collaborators.
  4. Revoke the AgentForge bot PATs (planner/tester/impl/reviewer/ops/conductor-bot, agentforge-ci-bot) and `af-ci-scaler-2941`.
  5. Archive `agentforge`, `agentforge-platform`, `agentforge-tenants`, `agentforge-config`, `agentforge-playground` and `primes-lab` (an AgentForge demo target; confirm it has nothing else).
  6. Before archiving, apply the `pr_reviewer` role change that removes those repos from `pr_reviewer_repos`, so `ReviewbotReconcileRepoFailing` does not fire.
- **Storage:** delete `/pve-nfs/agentforge-sandbox`, using a one-shot Job on the parent-export PV before PR-2 deletes it, or a short-lived Job after. Then the remaining Retain NFS PVs go with their manifests.
- **Cloudflare:** `tofu apply` of the edge DNS change.

### PR-5: docs
- A new ADR, "AgentForge retired", superseding 0018 and 0019 (keep their text).
- Delete the AgentForge runbooks. Move the OpenBao disaster notes from `agentforge-activation.md` to `openbao-recovery.md` first.
- Update CLAUDE.md (the inventory row and the "agent nodes" wording), README, and `network-plan.md`.
- Fix the remaining doc references from the classification.
- Close the now-moot issues: #1504, #1506, #1512, #1519, agentforge#422, and pin PRs #1518 and #1521.

## Critical files
See the classification (buckets A and B) in the review record. In short: `kubernetes/apps/clusters/ai/agentforge-*.yaml` and `keda.yaml`; `kubernetes/apps/infrastructure/agentforge-*` and `autoscaling/keda`; `kubernetes/apps/apps/agentforge/`; `security/openbao/{provisioner-*,capability-kids-configmap,unseal-rbac,devworker-provision-job}.yaml`; `databases/infra-pg*.yaml`; `monitoring/agentforge-*`; `scripts/` and `scripts/tests/`; `.gitea/workflows/`; the edge, auth, gatus and homepage files; `justfile`; the `dev_worker` and `pr_reviewer` Ansible roles; docs.

## Verification
- Each PR: the "Verify" lines above, plus every Flux Kustomization Ready at the merge revision and no new alert for 30 minutes.
- At the end:
  - `kubectl get ns | grep -E 'agentforge|af-tenant|keda'` is empty;
  - no `agentforge` CRD and no external-metrics APIService remain;
  - OpenBao is unsealed and the canary is green;
  - an ESO-synced secret in a non-AgentForge namespace refreshes;
  - llm-router and relay are healthy on the agent nodes;
  - workbricks still finds `kata`/`gvisor`.

**Rollback:**
- PR-0 to PR-3 are reverts, except for the dropped databases (restored from the dump), the deleted KV (not restorable; accepted, since these are credentials of a retired system) and the deleted LUN (a cache).
- Gitea archives can be unarchived.

<!-- codex-review-status: pending -->
