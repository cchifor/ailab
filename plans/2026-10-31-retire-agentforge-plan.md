# Retire AgentForge and every deployment that exists only for it

## Context

The owner decided on 2026-10-31 to remove AgentForge and all its dependent deployments ("Remove the AgentForge and all the dependent deployments"; "Address all the issues yourself. I approve!").

The playground was retired the day before (`plans/2026-10-30-retire-playground-plan.md`, #1505). What remains:
- the control plane `agentforge-platform`;
- five brokers;
- the provisioner;
- the reaper;
- the sandbox and its canary;
- one CP-rendered tenant, `tenant-zero/platform-dev`. Its pool runs on GC'd images (#1519), and it has created no sandbox Job since 2026-08-17;
- the parked conductor;
- codex-refresh (suspended);
- pkgcache: one Deployment, `npm-cache`, whose proxy also serves `/pypi/`;
- `agentforge-ci-runners` (dormant: its ScaledJob was never listed);
- KEDA, with zero ScaledObjects or ScaledJobs.

The dev workers have no AgentForge units installed (0 on all four, checked 2026-10-31).

**Kept, because something outside AgentForge needs it** (measured 2026-10-31). Each is listed explicitly; never select objects by the `app.kubernetes.io/part-of: agentforge` label, which shared objects also carry.
- **OpenBao runs AgentForge code.** `openbao-init`, `openbao-unsealer` and `openbao-provision` use `agentforge/orchestrator@sha256:ed8c94fa…`, and `agentforge openbao-provision` creates the `af` KV mount, k8s auth and canary. Kept:
  - those three workloads;
  - `unseal-rbac.yaml` identities 1–3 and `openbao-server-auth-delegator`;
  - OpenBao TLS, the Secrets `openbao-keys` and `openbao-operator-provisioner-token`, the ConfigMap `openbao-state`, and the `openbao-operator-seeds` Secret (mounted non-optionally; it may become empty, but it must stay valid);
  - the non-AgentForge provisioning and sync Jobs;
  - `openbao-canary` and the role `af-operator-canary`;
  - every `af-app-*` role and the `af` mount itself (estate, dev-workers, litellm and dsh paths live there).
- **The image is kept,** along with its zot `agentforge/**` retention policy and the Renovate disable rule. Replacing OpenBao's dependency with plain `bao` Jobs is a separate follow-up.
- **`agentforge-runtimeclasses`:** Workbricks uses kata and gVisor. Renaming it is a follow-up; never prune and recreate it in one step.
- **The three Talos agent nodes:** llm-router, llm-router-edge and relay with relay-postgres run there.
- **litellm-local:** Strive and the dev-worker diag key use it.
- **Misnamed shared tooling:** `af-verify-litellm`, `af-gen-litellm`, `gen-litellm-consumers.py` and its tests; `pin-image-digests.py` (the manifests workflow's immutable-Job guard); `check-inline-hashes.py`'s three LiteLLM checks; `af-verify-hashes`; `pin-bootstrap`; `openbao-status`.
- **Not AgentForge:** dsh's conductor; `agent-sandbox/` (ADR 0037); kro. External repos may use kro, so it is not touched here.
- **The reviewer webhooks** (org hooks 38 and 39) and both reviewer identities.

**Dormant, deliberately deferred** (they stay inert):
- the operator k8s-auth roles and policies that `openbao-provision`'s `run_operator_reconcile` re-applies from image code on every run. They bind service accounts in namespaces this plan deletes, so they can authenticate nothing. Removing them durably needs the image change from the follow-up;
- `CODEX_AUTH_GRANT` and the reviewer-1/2 AppRoles in `devworker-provision-job.yaml`. Current host configs don't render them; that is a separate review.

**Disposable data:**
- the `/pve-nfs/agentforge-sandbox` tree: job scratch copies (all reaped), the `.deps` caches and the empty canary dirs. The repos live in Gitea, and the PVs are velero-excluded by design;
- npm-cache's 10 GiB LUN (a cache).

**Backed up before deletion:**
- the two infra-pg databases;
- the live inventories (Flux, KV leaf names, roles, hooks, tokens).

**Credentials:** the rollback is reissuing them, not restoring them.

## Approach

Each PR is verified live before the next; PR-2 and PR-3 are split, as described in their sections. Every deleted file leaves its `kustomization.yaml` in the same commit, and every deleted script or rule leaves together with its tests and checks, so `wait: true` layers and CI never see a dangling reference.

Owner-level live steps use a one-shot owner credential (approved), in two ways:
- **Gitea:** a token minted in the gitea pod, passed via stdin, deleted from the DB, and proven dead with a 401.
- **OpenBao:** the break-glass token, used only for an exact, printed list of deletions.

Destructive steps are release gates, each placed after its own backup or disposable-data record.

### PR-0: preparation (deletes nothing)
1. **`.gitea/workflows/script-tests.yaml`** runs the full `unittest discover -s scripts/tests` on **every push and PR**, the same triggers as broker-inventory today, with the same Python bootstrap. broker-inventory then keeps only its generator check.
2. **#1513:** the audit found 77 direct assignments of the shared `subprocess.run` in `test_reviewbot.py` and no other mutated subprocess attribute. The cross-module leak is the defect, so a module-level `tearDownModule` restores the real function, bound after the imports. Within-module ordering is unchanged; each test sets its own fake first.
   - Proven with a probe module: run after it, the probe gets the canned JSON on main and a real subprocess with the fix.
3. **Remove the AgentForge alerts and status checks of the services this PR stops,** so the stop is not alert noise:
   - every AgentForge rule file except the provisioner's, which stays until PR-2b, with its promtool tests;
   - the dormant `monitoring/agentforge.yaml`;
   - the two script tests that read `agentforge-rules.yaml`. Their generic promrule-spec and rules-lint classes move verbatim to `test_promrule_spec.py`;
   - the gatus check and the homepage tile.
4. **Scale to 0, in git:** `agentforge-platform` (the CP and its tenants reconciler), the five brokers, the reaper, `npm-cache`, and the conductor (already 0). The provisioner stays up.
5. **Before merge:**
   - **Durable image copy:** copy `agentforge/orchestrator@sha256:ed8c94fa…` to `registry.chifor.me/infra/openbao-bootstrap`. That is outside `agentforge/**`, so the catch-all keep-everything rule applies. Verify both paths serve the same digest.
   - **Baseline:** save the Flux status of every unit and the live inventory: all Kustomizations' `.status.inventory` for the AgentForge units, the tenants repo revision, BrokerSeats, TriggerAuthentications, ClusterTriggerAuthentications, HPAs on external metrics, the PVs, the Gitea hooks and tokens, and the OpenBao KV leaf names.

**Verify:** the pods are gone; the provisioner is Running; `get bseat -A` is empty with the CP stopped.

### PR-1: tenants
- Delete `clusters/ai/agentforge-tenants.yaml`, `agentforge-tenants-source.yaml` and `agentforge-tenant-platform-dev.yaml`, and the dir `infrastructure/agentforge-tenant-platform-dev/`.
- Keep `agentforge-tenants-git-auth`: `agentforge-tenants-bootstrap/` holds the impersonated SA and the reconciler's ClusterRole and Binding, which Flux GC needs.
- In the same commit:
  - update `scripts/tests/test_manifest_paths.py` (local paths, GitRepository assertions);
  - drop `agentforge-tenants` from `scripts/manifest-paths.py`'s `EXPECTED_EXTERNAL_SOURCES`;
  - adapt its fixtures.

**Gate before PR-2:**
- both tenant Kustomization objects are gone, and every object in their recorded inventory is gone;
- no namespace name starts with `af-tenant-` (list all namespaces and filter; a list error is a failure);
- the provisioner logged its teardown;
- no BrokerSeat exists.

### PR-2a … PR-2e: the AgentForge objects, in five PRs

The deletions total about 1 MB, but the reviewers skip any diff over `pr_reviewer_max_diff_bytes` (400 KB). So this phase runs as five PRs, **in sequence**, each one verified before the next. Each edits `test_manifest_paths.py`'s exact path set together with its own deletions.

Every replica change in this plan is made **in git**. Flux would revert a `kubectl scale` on a manifest with explicit replicas; PR-0 already set them to 0.

**Preflight for every deletion PR:** no Flux Kustomization or HelmRelease being deleted has `spec.suspend: true`. Flux skips garbage collection and Helm uninstall for a suspended object, which would orphan its inventory. Checked 2026-10-31: none is suspended. codex-refresh's suspension is its CronJob's own `spec.suspend`, a workload field, not the Flux object's.

**PR-2a. CI checks only; nothing live changes.**
- Delete `check-seat-guard-cel.py`, `check-tenant-guard-cel.py` and their workflow `tenant-guard-cel.yaml`.
- Delete `gen-broker-inventory.py` and its workflow `broker-inventory.yaml`. Its unit-test step already moved to `script-tests.yaml` in PR-0.
- Delete the canary harness `verify-sandbox-boundary.sh`.
- Delete their tests: `_brokerseat_support.py`, `test_brokerseat_{admission,crd,rbac}.py`, `test_gen_broker_inventory.py`, `test_verify_sandbox_boundary.py`, and `test_provisioner_podmonitor.py`, which imports the generator.
- Remove the justfile recipes that ran them (af-verify-tenant-guard, af-verify-brokers, af-gen-brokers, af-record-seed-coverage).
- **Why first:** the checks only keep the soon-deleted objects consistent, and removing a check before its subject is always safe. It is about 295 KB.

**PR-2b. The provisioner, before the brokers.**
- **Why:** its BrokerSeat controller watches the `brokerseats` CRD, which PR-2c deletes. After PR-1's gate it has no duties left (the tenant namespace is gone, and no BrokerSeat exists).
- Delete `provisioner-deploy.yaml` (with its CiliumNetworkPolicy and its namespaces ClusterRole/Binding), `provisioner-podmonitor.yaml` and `capability-kids-configmap.yaml`, with their entries in `security/openbao/kustomization.yaml`.
- Delete the provisioner identity block (4) in `unseal-rbac.yaml`.
- Delete `agentforge-provisioner-rules.yaml` with its test and its monitoring kustomization entry.
- Delete `af-revoke-legacy-kid.sh` and `af_revoke_legacy_kid.py` with their test, and `check_capability_kids_checksum` in `check-inline-hashes.py`.
- **After merge:** revoke the provisioner's OpenBao token and delete the Secret `openbao-provisioner-token`. Keep `openbao-operator-provisioner-token`.
- **Verify:** OpenBao is unsealed, the canary is green, and an ESO refresh succeeds outside AgentForge.
- **Deferred to a follow-up**, not this plan: `devworker-provision-job.yaml`'s codex-hosts-publisher, `CODEX_AUTH_GRANT` and the reviewer AppRoles. All three are dormant, and changing that Job is a review of its own.

**PR-2c. The brokers and codex-refresh.**
- Delete the Flux units `agentforge-broker` (five seats, the `brokerseats` CRD, its admission and RBAC, the inventory) and `agentforge-codex-refresh`, which dependsOn it, with their directories.
- **Verify:** the namespace `agentforge-broker` and the CRD are gone, and its VAPs and bindings are gone.

**PR-2d. Sandbox, workers, pkgcache, ci-runners.**
- **Gates before merge:**
  - a one-shot cleanup Job on the parent-export PVC erases `/export/tenant-zero/*` (disposable data, as in Context);
  - npm-cache's PV is patched to `persistentVolumeReclaimPolicy: Delete`, so Trident deletes the LUN.
- Delete the Flux units `agentforge-sandbox`, `-workers` (which dependsOn the sandbox and KEDA), `-pkgcache` and `-ci-runners` (which dependsOn KEDA), with their directories. Remove `check_platform_dev_job_suffix` from `check-inline-hashes.py`.
- **Verify:**
  - the namespaces `agentforge-sandbox`, `agentforge-pkgcache` and `agentforge-ci` are gone, with their VAPs;
  - the Retain NFS PVs are gone. Their data was already erased; the empty export root is a noted QNAP leftover;
  - the npm-cache LUN is gone from Trident and the QNAP.

**PR-2e. The CP and the tenants bootstrap, plus the shared-file edits.**
- Delete `apps/agentforge/` (the CP, the admission VAPs, `agentforge-infra-bot.sops.yaml`) and drop it from `apps/kustomization.yaml`.
- Delete `agentforge-tenants-git-auth.yaml` and `agentforge-tenants-bootstrap/`. Gate: PR-1's GC has finished.
- Delete `af-db.sh`, `af-cp-smoke.sh`, `test_cp_env.py` and their recipes.
- Edge, auth and status:
  - remove the Authelia `agentforge` client and its `agentforge_id_token` claims policy, plus any `af:tenant-zero:*` groups in the users DB (a SOPS edit, asserting `ENC[`);
  - remove the edge hostname, then `just edge-render`, and add it to `DROPPED`; delete the UniFi DNS record via `gateway.py` after merge, and apply Cloudflare with `tofu` by hand;
  - remove the cloudflared ingress.
- Repo lists: ci-rerun-watchdog `REPOS` and `ci-queue-stats` `DEFAULT_REPOS`, with their tests.
- The dev_worker role's host-mode AgentForge feature.
- **Verify:** the namespace `agentforge` is gone with its VAPs and bindings; every surviving ailab unit is at the merge revision; external units match their baseline.

### PR-3a and PR-3b: KEDA, uninstalled before its namespace goes

**PR-3a. Uninstall only.**
- **Preflight:** no ScaledObjects, ScaledJobs, TriggerAuthentications or ClusterTriggerAuthentications, and no HPA on external metrics. Read the installed chart's CRD behaviour.
- Remove only the HelmRelease from `autoscaling/keda/kustomization.yaml`. The namespace, the Flux Kustomization and the HelmRepository stay.
- **After merge:**
  - helm-controller has uninstalled the release while the namespace still exists;
  - then remove any residual KEDA CRDs and the `v1beta1.external.metrics.k8s.io` APIService (a dangling one hangs namespace deletion cluster-wide), and confirm both are gone.

**PR-3b. The rest.** Delete `clusters/ai/keda.yaml`, `autoscaling/keda/` and `sources/keda.yaml`, and drop `keda` from the `platform-rules.yaml` regex and its test.
- **Verify:** the namespace `keda` is gone, no `keda.sh` CRD remains, and OpenBao, ESO and workbricks are healthy.

### PR-4: data and identity
1. **infra-pg**, all on `--context admin@ai -n databases`:
   - **Retirement dump:**
     - create the named Job `infra-pg-dump-retire-agentforge` from the CronJob and wait for it to complete;
     - verify `COMPLETE` and `SHA256SUMS`;
     - copy that generation outside the 7-generation rotation, onto the dump volume under `retired/agentforge-2026-10-31/`;
     - restore both databases into scratch databases, checking table counts and ownership, then drop the scratch databases.

     Role passwords are not kept; a rollback recreates them.
   - Then, by hand, `DROP DATABASE agentforge_platform, agentforge_broker`.
   - In the PR:
     - set all five roles to `ensure: absent`;
     - remove **every** AgentForge postInitSQL statement (both CREATE DATABASE statements and all role statements);
     - remove the passwordSecret references and delete `infra-pg-afp-{admin,app}.sops.yaml`;
     - edit the kustomization;
     - all in one commit.
   - **Verify:** the roles are gone from `pg_roles` and from the CNPG managed-role status, and nothing they own remains.
2. **OpenBao seeds**, merged **before** any live KV delete. Remove the AgentForge paths from `seeds.json`, leaving a valid document, even if it is empty. Edit through the sops shim and assert `ENC[`. Update the seed table in `openbao-recovery.md`.
3. **Gitea admin token `af-ci-scaler-2941`:** delete it first, then the inventory SQL allowlist, the alert text and its test.

### Live owner steps (each after the PR that frees it)
- **OpenBao**, after PR-2b and the PR-4 seeds:
  - Enumerate the exact KV-v2 leaves (names only) under `operator/{broker,reaper,ci,conductor,dispatcher,provisioner}` and `tenants/`, plus `dev-workers/codex-auth`. Keep `operator/canary`.
  - Print the list, then **permanently** delete each leaf (`metadata delete`), using the break-glass token only after confirming it exists and resolves.
  - Rollback is credential reissuance, recorded.
  - After the next `openbao-provision` run, verify the paths stay absent.
- **Gitea, one-shot owner token:**
  1. Re-read every hook's scope and target, then delete only those that target AgentForge (today 1–4).
  2. Close pin PRs #1518 and #1521 as obsolete.
  3. Revoke these tokens: the AgentForge bot PATs (planner, tester, impl, reviewer, ops, conductor, agentforge-ci-bot), the CP's `agentforge-infra-bot`, the tenants-repo deploy key and `af-ci-scaler-2941`. Resolve each owner and ID first, and leave the reviewer and runner credentials alone.
  4. Archive `agentforge`, `agentforge-platform`, `agentforge-tenants`, `agentforge-config` and `agentforge-playground`. Archive `primes-lab` only if nothing but AgentForge used it.
  5. **Before archiving:** a `pr_reviewer` role PR that removes those repos from `pr_reviewer_repos` and `agentforge-ci-bot` from the pin/merge-author lists, with the `test_reviewbot.py` updates, deployed to both reviewer hosts.

### PR-5: docs and close-out
- A new ADR, "AgentForge retired", superseding 0018 and 0019 (keep their text).
- Move the OpenBao disaster notes into `openbao-recovery.md`, then delete the AgentForge runbooks.
- Update CLAUDE.md, README, `network-plan.md`, `infra-pg.md`, `owner-credentials.md` and `s2s-identity.md`.
- Close #1504, #1506, #1512, #1519 and agentforge#422.
- File the follow-ups: OpenBao off the AgentForge image; the dormant image-owned roles; the RuntimeClasses rename; `CODEX_AUTH_GRANT`; the empty NFS export root.

## Critical files
Per PR, as listed above. The full delete/edit/keep classification (2026-10-31) is attached to the review record `plans/2026-10-31-retire-agentforge-classification.md`.

## Verification
- **Baseline first.** Each PR: its Verify lines; surviving ailab units at the merge revision, and external units against their baseline (workbricks-c1 is suspended by design); no new alert for 30 minutes.
- **End state:**
  - no namespace named `agentforge*`, `af-tenant-*` or `keda`;
  - no `agentforge.io` or `keda.sh` CRDs, and no external-metrics APIService;
  - OpenBao unsealed, with the unsealer Secret readable and the canary green;
  - an ESO refresh outside AgentForge;
  - a LiteLLM diag request;
  - a dev-worker credential refresh;
  - both reviewer personas review a PR;
  - Workbricks still resolves `kata` and `gvisor`;
  - llm-router and relay healthy;
  - the retired KV and seeds stay absent after a provision run.

**Rollback.**
- PR-0 to PR-3b revert. Recreated Retain PVs need their `claimRef` UIDs cleared before they rebind.
- The databases restore from the retirement dump, with the roles recreated.
- The NFS data and the LUN are disposable.
- Permanently deleted KV, revoked PATs and deploy keys are reissued, not restored.
- The Gitea archives can be unarchived.

<!-- codex-review-status: finalized -->
