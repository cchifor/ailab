# Retire AgentForge and every deployment that exists only for it

## Codex Review

- Reviewed against worktree commit `d31a32e5`; the owner’s authorization covers AgentForge retirement. Keeping OpenBao bootstrap/unseal, ESO, runtime classes, agent nodes, LiteLLM and shared reviewer/CI services is justified.
- Changes are needed before execution: CI updates must accompany each removal, OpenBao cleanup currently assumes a revoked identity and can be undone by reconciliation, and backup/rollback coverage is incomplete.
- Keeping the provisioner and tenant reconciler SA/RBAC through teardown is correct. The checked DELETE admission guards do not prohibit the intended platform-dev teardown or deletion by the main Flux identity.
- `scripts/check-inline-hashes.py` and `scripts/manifest-paths.py` pass on current main. Live inventories, registry availability, token capabilities and policies implemented inside the external AgentForge image were not independently verified.

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
  <!-- codex: Preserve their supporting objects explicitly: the first three identities in unseal-rbac.yaml, openbao-server-auth-delegator, TLS, openbao-keys, openbao-state, the operator-provisioner token and seed Secret, and the independent non-AgentForge provisioning/sync Jobs. Several carry app.kubernetes.io/part-of: agentforge, so a label-based cleanup would remove shared infrastructure. -->
  <!-- codex: The Zot policy retains recent/pulled tags rather than permanently protecting this exact digest, and HTTP 200 is not a recoverable backup. Record a durable copy or explicit retention protection for the orchestrator manifest and blobs before retiring its build repositories. -->
  - Replacing them with plain `bao` Jobs is a separate follow-up.
- **Workbricks** uses the kata/gVisor RuntimeClasses, so `agentforge-runtimeclasses` stays (renaming it is a follow-up).
- **The three Talos agent nodes** run llm-router, llm-router-edge and relay (including relay-postgres), so they stay, with their `dedicated=agent` taint and Talos extensions.
- **litellm-local** serves Strive and the dev-worker diag key. AgentForge was only one consumer.
- **Not AgentForge:** dsh's own conductor, `agent-sandbox/` (ADR 0037), and kro. kro is not touched here, because external repos (platform, workbricks) may use it.

## Approach

Six PRs in order, each verified live before the next. Owner-level steps run with one-shot owner credentials, under explicit owner approval. Data deletions come last, after backups. Flux deletes child Kustomizations concurrently and ignores dependsOn, so each ordering point is its own PR.

<!-- codex: The schedule contradicts “data deletions come last”: PR-2 deletes the cache LUN, the storage ceremony can erase NFS before PR-2, and live KV cleanup is allowed after PR-3 but before PR-4 removes its seeds. Put each backup or explicit disposable-data exception before its actual destructive step and make those steps release gates. -->
<!-- codex: Every deleted resource file must also leave its owning kustomization.yaml in the same commit, especially monitoring/, security/openbao/ and infrastructure/sources/. The shared infrastructure, apps and databases units use wait:true; build/apply failures can block unrelated dependents, and openbao's wait:false does not prevent apply failures from blocking external-secrets. -->

### PR-0: preparation (deletes nothing)
1. **Move the full `scripts/tests` unittest step out of `.gitea/workflows/broker-inventory.yaml`** into a new `.gitea/workflows/script-tests.yaml`.
   - Same Python bootstrap, path-filtered on `scripts/**` and `.gitea/workflows/script-tests.yaml`, plus a required context if contexts files demand it.
   <!-- codex: That filter reduces existing coverage: broker-inventory currently runs on every push/PR, while this suite also checks Kubernetes manifests, Ansible reviewer code/defaults and registry configuration. Keep the existing trigger coverage or include all tested inputs; required-contexts-ailab.txt currently declares no required contexts. -->
   - Otherwise deleting broker-inventory would silently stop every script test.
2. **Fix #1513** (`test_reviewbot.py` leaks a fake `subprocess.run`), using `mock.patch.object`. A one-line, self-contained change that runs in the new workflow.
   <!-- codex: test_reviewbot.py contains numerous direct assignments to the shared subprocess.run, and its _REAL_RUN comment explicitly documents leakage across tests. Audit and restore every relevant mutation rather than assuming one patched assignment fixes isolation, then run the full discovery command in one process. -->
3. **Stop the AgentForge workloads.** Set `replicas: 0` for:
   - `agentforge-platform` (the CP, which also stops its tenants reconciler);
   - all five brokers;
   - the reaper;
   - npm-cache and pypi-cache.
   <!-- codex: There is no separate pypi-cache Deployment in this tree: npm-cache.yaml serves /pypi/ through the proxy sidecar in the npm-cache Deployment. Scale that Deployment once. -->

   **Keep the provisioner running:** it owns the finalizers on the tenant namespace and BrokerSeats.

   The platform-dev pool lives in the `agentforge-tenants` repo and goes in PR-1.

**Verify:** the pods are gone, and alerts are only the expected AgentForge target-down noise, silenced for the change window, or the rules are deleted in PR-2. Everything is reversible by revert.

### PR-1: tenants
- Delete `clusters/ai/agentforge-tenants.yaml` and `agentforge-tenant-platform-dev.yaml`, and the `agentforge-tenants` GitRepository if it is separate.
<!-- codex: The separate source is definitely clusters/ai/agentforge-tenants-source.yaml; also remove the now-unused infrastructure/agentforge-tenant-platform-dev/ directory. Preserve the tenant repository revision and live Flux inventory before deletion, since this checkout does not contain the CP-rendered tenant resources. -->
<!-- codex: Update scripts/tests/test_manifest_paths.py in PR-1, not first in PR-2: its exact local-path set and GitRepository assertions immediately fail after these removals. Also remove agentforge-tenants from scripts/manifest-paths.py's EXPECTED_EXTERNAL_SOURCES and adapt the external-source fixtures, then update the path expectations again with PR-2 and PR-3. -->
- Keep `agentforge-tenants-git-auth`: the tenants Kustomization impersonates the SA it creates, and Flux GC needs it.
<!-- codex: Gate PR-2 on deletion of both tenant Kustomization objects and completion of their recorded inventory cleanup, not only disappearance of the namespace. Retain the reconciler's ClusterRole/ClusterRoleBinding as well as its SA until Flux finalization finishes; its bootstrap directory is kubernetes/apps/agentforge-tenants-bootstrap/. -->

**Verify:**
- namespace `af-tenant-tenant-zero-platform-dev` is gone;
- the provisioner logged its teardown (role and policy deleted);
- `get ns af-tenant-*` is empty;
<!-- codex: kubectl does not expand af-tenant-* as a namespace-name wildcard; enumerate namespaces and filter their names or use the actual tenant labels. Treat authorization/list failures as failures rather than evidence of an empty inventory. -->
- no BrokerSeat exists (already none).
<!-- codex: Recheck BrokerSeats after the CP has stopped, and if any exist, delete them and wait for controller teardown before PR-2 removes brokerseat-rbac.yaml, the broker namespace and the CRD. A running provisioner alone is insufficient once its namespace-local permissions disappear; do not bypass its finalizers to satisfy this gate. -->

### PR-2: AgentForge workloads, CP, admission, monitoring, CI
1. **Delete these Flux Kustomizations and their dirs:** `agentforge-broker`, `-sandbox`, `-workers`, `-pkgcache`, `-codex-refresh`, `-ci-runners`, `-tenants-git-auth`.
2. **Delete `apps/agentforge/`** (the CP and the admission VAPs), and drop it from `apps/kustomization.yaml`.
3. **Monitoring:** delete the AgentForge rule files together with their promtool tests.
<!-- codex: Include monitoring/agentforge.yaml and its commented kustomization entry: it is an unlisted, obsolete Service/Endpoints/ServiceMonitor artifact rather than a rule file. Keep shared platform, OpenBao, reviewer and dev-worker monitoring even where comments mention AgentForge. -->
4. **Scripts, tests and the justfile:** delete the AgentForge scripts and their tests together (the classification list), and the af-* recipes. Edit `check-inline-hashes.py` and `test_manifest_paths.py`.
<!-- codex: af-verify-litellm and af-gen-litellm are shared LiteLLM maintenance recipes despite their names; preserve or rename them, along with gen-litellm-consumers.py and its tests. Keep pin-image-digests.py's shared immutable-Job guard used by manifests.yaml, and retain check-inline-hashes.py's three LiteLLM checks. -->
<!-- codex: Stage the remaining test dependencies explicitly: test_provisioner_podmonitor.py imports gen-broker-inventory.py and reads the provisioner rules, so it must leave with those PR-2 deletions or keep those dependencies until PR-3. Remove check_platform_dev_job_suffix in PR-2 and check_capability_kids_checksum with the PR-3 provisioner files; otherwise the surviving manifests workflow fails on missing files. -->
5. **Workflows:** delete `.gitea/workflows/tenant-guard-cel.yaml`, and `broker-inventory.yaml` (its tests moved in PR-0).
6. **Other surfaces:**
   - gatus and homepage entries;
   - the Authelia `agentforge` OIDC client and its claims policy;
   <!-- codex: These live in apps/auth/configuration.yml; remove only agentforge_id_token, leaving the other clients and claims policies intact. Also inspect the encrypted Authelia user database for obsolete af:tenant-zero:* memberships, which deleting the client does not remove. -->
   - the cloudflared ingress;
   - the edge hostname entry, then `just edge-render`, and add the hostname to `test_edge_render.py`'s `DROPPED`. Cloudflare DNS (tofu) is applied by hand after merge.
   <!-- codex: Cloudflare cleanup does not remove LAN DNS: kubernetes/edge/gateway-state.yaml records an owned UniFi static_dns entry for agentforge.chifor.me. Reconcile DNS with scripts/edge/gateway.py against the updated registry and commit its resulting state; do not simply erase the ownership entry before deleting the live record. -->
   <!-- codex: The host-mode AgentForge implementation is missing from the execution steps: dev_worker still contains tasks/agentforge.yml, agentforge-update.sh, service/timer/env templates, handlers, defaults and firewall rules. Verify installed units on every dev worker, stop/disable any service and updater before revoking credentials or archiving agentforge-config, then remove only this feature's code and secrets while preserving the workers and shared OpenBao agent. -->
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
<!-- codex: Removing the Kubernetes Deployment and RBAC does not revoke the generated openbao-provisioner-token or remove its Kubernetes Secret; inventory these for explicit retirement after finalizer cleanup. Distinguish that token from openbao-operator-provisioner-token, which the retained openbao-provision Job still reads. -->
<!-- codex: devworker-provision-job.yaml also retains CODEX_AUTH_GRANT in all worker policies and creates reviewer-1/reviewer-2 AppRoles solely for the old projection. Current defaults use host-owned worker logins and disable reviewer OpenBao rendering, but verify effective host configs before retiring these obsolete grants/roles, and preserve reviewer-2's independent dsh-codex-publisher and af/litellm/chatgpt path. -->
2. **KEDA:** first confirm that `get scaledobjects,scaledjobs -A` is empty, then delete `clusters/ai/keda.yaml`, `autoscaling/keda/` and `sources/keda.yaml`.
<!-- codex: Empty ScaledObjects/ScaledJobs are not the entire uninstall preflight: enumerate TriggerAuthentications, ClusterTriggerAuthentications and any HPAs consuming external metrics, including resources from external Flux sources. Make HelmRelease uninstall a verified step while its namespace still exists, inspect the installed chart's CRD retention behavior, then remove residual CRDs/APIService and the namespace; crds.install: true alone does not prove uninstall removes them. -->
3. **Monitoring:** drop `keda` from the `platform-rules.yaml` regex, and swap the series in its test.

**Verify:**
- the KEDA CRDs are gone;
- the `v1beta1.external.metrics.k8s.io` APIService is gone, since a dangling one hangs namespace deletion cluster-wide;
- the `keda` namespace is gone;
- OpenBao is unsealed and healthy, and `openbao-canary` is green.

### PR-4: data and identity (irreversible parts, after backups)
1. **infra-pg:**
   - Take a fresh dump: `kubectl create job --from=cronjob/infra-pg-dump`, and verify it contains `agentforge_platform` and `agentforge_broker`.
   <!-- codex: This command lacks the required Job name and the databases namespace; use an explicitly named Job with --context admin@ai -n databases and wait for successful completion. Keep all destructive database operations pinned to that cluster and namespace as well. -->
   <!-- codex: The dump publishes COMPLETE, SHA256SUMS, globals.sql and per-database archives, but retains only seven complete generations and omits role passwords. Preserve the retirement generation outside that rotation, verify checksums and a restore of both databases with ownership/ACLs/RLS, and retain encrypted role credentials if rollback is promised. -->
   - Then `DROP DATABASE` both by hand.
   - Then, in the PR: set the five roles to `ensure: absent`; remove the afp_* postInitSQL and the passwordSecret references; delete `infra-pg-afp-{admin,app}.sops.yaml`; edit the kustomization. The databases Kustomization is `wait: true`, so all of this lands in one commit.
   <!-- codex: Remove all AgentForge postInitSQL, including CREATE DATABASE agentforge_platform and the three agentforge_broker_admin/agentforge_broker/agentforge_reaper role statements plus CREATE DATABASE agentforge_broker; removing only afp_* statements recreates retired objects on a rebuild. After dropping the databases, check for remaining ownership/privilege dependencies and verify all five roles actually disappear from pg_roles and CNPG managed-role status, rather than relying solely on Flux Ready. -->
2. **OpenBao seeds:** remove the AgentForge paths from `operator-seeds.sops.yaml` (`seeds.json`), so the daily provision run cannot recreate them.
   <!-- codex: This must reconcile before live KV deletion, not after the PR-3 cleanup window: provision-job.yaml documents create-if-absent seeding on every provision run. Preserve the mounted Secret with valid seeds.json even if the remaining document is empty, and verify a subsequent provision run cannot repopulate the retired paths. -->
   - Edit through sops with the editor shim, and assert `ENC[` afterwards.
   - Update the `openbao-recovery.md` seed table.
3. **Gitea admin token `af-ci-scaler-2941`:** delete it in Gitea first; then the credential-inventory allowlist SQL, the alert text and its test, in the same PR.

### Manual steps (after the PR that frees each)
- **OpenBao live cleanup** (key names only in any output), after PR-3:
  - delete the KV paths `af/operator/{broker/*,reaper,ci,conductor,dispatcher}/*`, `af/tenants/*` and `af/dev-workers/codex-auth`;
  <!-- codex: These are inventory patterns, not recursive OpenBao delete commands: enumerate exact KV-v2 leaves and specify whether retirement means soft deletion or permanent metadata/all-version deletion. Include an inventory of operator/provisioner/* and other controller-created artifacts, and preserve an encrypted recovery copy or explicitly record credential reissuance as the rollback path before permanent deletion. -->
  - delete the AgentForge k8s-auth roles and policies (af-operator-{broker,ci,reaper,conductor,dispatcher}, af-codex-refresher, codex-hosts-publisher, af-tenant-*, af-dispatcher-eso, the provisioner's scoped policy);
  <!-- codex: Removing seeds does not stop role reconciliation: unseal-rbac.yaml explicitly documents the retained openbao-provision Job calling run_operator_reconcile to reapply operator roles on an already-provisioned vault. The role definitions are in the external image, so verify and change that reconciliation before claiming durable removal, or explicitly defer the image-owned roles and verify their remaining scope. -->
  - keep `af/operator/canary` and every non-AgentForge role.

  These run with the least-privileged identity that can delete each item. The `openbao-provision` bootstrap token covers policies and roles. Before using a root-equivalent, read which token can delete `operator/*` KV.
  <!-- codex: The bootstrap-token assumption is wrong in steady state: provision-job.yaml says bootstrap revokes the root token and deletes openbao-bootstrap-token, while unseal-rbac.yaml only proves which Kubernetes Secrets each SA may read. The ACLs of openbao-provisioner-token and openbao-operator-provisioner-token are implemented in the external AgentForge image, so this repo cannot establish their policy/role/KV delete capabilities. -->
  <!-- codex: devworker-provision-job.yaml and the other shared seeders reference a separate openbao-breakglass-token Secret, key root_token, documented as root-equivalent and deliberately absent from Git. Verify its live existence/validity and exact capabilities of any narrower candidate through the already-authorized owner path; neither the unseal-key-only identity nor Kubernetes Secret-read RBAC proves OpenBao administrative authority, and cleanup must not reset or wipe the shared vault. -->
- **Gitea, with a one-shot owner token** (minted in the gitea pod, used via stdin, deleted from the DB, then proven dead with a 401):
  1. Close the pin PRs #1518 and #1521 as obsolete.
  2. Delete webhooks 1–4 (all target AgentForge).
  <!-- codex: Record each hook's organization/repository scope and re-read its target before deletion; this checkout cannot establish that IDs 1–4 still identify those AgentForge hooks. Preserve the reviewer org webhooks consumed and asserted by ansible/roles/pr_reviewer. -->
  3. Remove the playground bot collaborators.
  4. Revoke the AgentForge bot PATs (planner/tester/impl/reviewer/ops/conductor-bot, agentforge-ci-bot) and `af-ci-scaler-2941`.
  <!-- codex: Include the CP's agentforge-infra-bot credential from apps/agentforge/agentforge-infra-bot.sops.yaml and the tenant repository deploy key from agentforge-tenants-bootstrap in the live credential inventory; deleting their Secrets or archiving a repo is not revocation. Resolve actual token owners/IDs and preserve the independent reviewer-claude/reviewer-codex identities and shared runner credentials. -->
  5. Archive `agentforge`, `agentforge-platform`, `agentforge-tenants`, `agentforge-config`, `agentforge-playground` and `primes-lab` (an AgentForge demo target; confirm it has nothing else).
  6. Before archiving, apply the `pr_reviewer` role change that removes those repos from `pr_reviewer_repos`, so `ReviewbotReconcileRepoFailing` does not fire.
  <!-- codex: Make this a named repo change with matching scripts/tests/test_reviewbot.py updates: AllowlistDefaultsTest hard-codes agentforge and agentforge-platform, and the role also retains agentforge-ci-bot in pin/merge-author lists. Deploy the updated configuration to both reviewer hosts before archiving, while retaining their router seats and review service. -->
  <!-- codex: Also update apps/ci-rerun-watchdog/deployment.yaml's REPOS and scripts/ci-queue-stats.py's DEFAULT_REPOS, both of which explicitly include agentforge and agentforge-platform. Preserve these shared tools and adjust any affected tests rather than deleting them as AgentForge-only. -->
- **Storage:** delete `/pve-nfs/agentforge-sandbox`, using a one-shot Job on the parent-export PV before PR-2 deletes it, or a short-lived Job after. Then the remaining Retain NFS PVs go with their manifests.
<!-- codex: The sandbox NFS PV/PVC manifests explicitly exclude themselves from Velero backup, so no backup of platform-dev workspace data is established here. Inventory and back up the retained data before erasure, or record its confirmed disposable status separately from npm-cache's stated cache exception. -->
<!-- codex: The “parent-export” PV mounts /pve-nfs/agentforge-sandbox itself, so a Job using it can erase contents but cannot unlink that export root through the mount. Finish and remove the cleanup Job before PVC/namespace deletion, prevent the still-managed NFS provisioner Job from recreating subdirectories, and explicitly handle export-root removal and Retain PV protection/binding cleanup after all consumers are gone. -->
- **Cloudflare:** `tofu apply` of the edge DNS change.

### PR-5: docs
- A new ADR, "AgentForge retired", superseding 0018 and 0019 (keep their text).
- Delete the AgentForge runbooks. Move the OpenBao disaster notes from `agentforge-activation.md` to `openbao-recovery.md` first.
- Update CLAUDE.md (the inventory row and the "agent nodes" wording), README, and `network-plan.md`.
- Fix the remaining doc references from the classification.
- Close the now-moot issues: #1504, #1506, #1512, #1519, agentforge#422, and pin PRs #1518 and #1521.

## Critical files
See the classification (buckets A and B) in the review record. In short: `kubernetes/apps/clusters/ai/agentforge-*.yaml` and `keda.yaml`; `kubernetes/apps/infrastructure/agentforge-*` and `autoscaling/keda`; `kubernetes/apps/apps/agentforge/`; `security/openbao/{provisioner-*,capability-kids-configmap,unseal-rbac,devworker-provision-job}.yaml`; `databases/infra-pg*.yaml`; `monitoring/agentforge-*`; `scripts/` and `scripts/tests/`; `.gitea/workflows/`; the edge, auth, gatus and homepage files; `justfile`; the `dev_worker` and `pr_reviewer` Ansible roles; docs.

<!-- codex: The classification is neither included nor linked, yet several deletion steps depend on it; attach a concrete per-PR delete/edit/keep inventory before implementation. These broad patterns include preserved runtimeclasses and shared database/OpenBao resources, and do not by themselves capture the tenant bootstrap directory, dormant monitoring/agentforge.yaml or shared-file edits identified above. -->

## Verification
- Each PR: the "Verify" lines above, plus every Flux Kustomization Ready at the merge revision and no new alert for 30 minutes.
<!-- codex: “Every Flux Kustomization at the merge revision” cannot apply literally: external sources have their own revisions, and workbricks-c1 is explicitly suspended. Record a baseline, require affected surviving ailab units to reconcile the merged revision, verify deleted units and their inventories are gone, and check unrelated/external units against their own expected state. -->
- At the end:
  - `kubectl get ns | grep -E 'agentforge|af-tenant|keda'` is empty;
  - no `agentforge` CRD and no external-metrics APIService remain;
  - OpenBao is unsealed and the canary is green;
  - an ESO-synced secret in a non-AgentForge namespace refreshes;
  - llm-router and relay are healthy on the agent nodes;
  - workbricks still finds `kata`/`gvisor`.

<!-- codex: Add verification of the retained init/provision rerun and unsealer Secret access, since an already-unsealed vault can hide broken restart recovery; also verify retired KV/roles stay absent after the next provision run where durable removal is claimed. Exercise a LiteLLM diagnostic request, dev-worker credential/kubeconfig refresh, both reviewer personas and Workbricks runtime scheduling, which namespace absence and Flux readiness alone do not establish. -->

**Rollback:**
- PR-0 to PR-3 are reverts, except for the dropped databases (restored from the dump), the deleted KV (not restorable; accepted, since these are credentials of a retired system) and the deleted LUN (a cache).
<!-- codex: This understates rollback: database drops occur in PR-4, NFS erasure is missing entirely, and reverted manifests cannot restore revoked PATs/deploy keys or deleted live credentials. Document credential reissuance and restore ordering, including Retain PV claimRef UIDs after namespace/PVC recreation, and distinguish reversible KV soft deletion from permanent metadata deletion rather than declaring every KV deletion unrestorable. -->
- Gitea archives can be unarchived.

<!-- codex-review-status: complete -->