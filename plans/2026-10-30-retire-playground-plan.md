# Retire the AgentForge tenant-zero/playground workspace

## Context

The owner decided on 2026-10-30 to retire the playground workspace (org `tenant-zero`, ws `playground`).

The trigger is `ForgeProvisionerSeatEntitlementDrift`, which has fired since 2026-09-07:
- The legacy, hand-seeded capability kid `tz-213512e80fde7c84` is granted in the broker kid registries for anthropic/claude-max-1, anthropic/claude-max-2 and openai/codex-pro (`af/operator/broker/<p>/<a>/kids`).
- `security/openbao/capability-kids-configmap.yaml` cannot declare it:
  - Kids there are always derived as `cap-<sha256(org/ws)[:32]>`.
  - `CapabilityKidEntry.pool` holds one pool, and `tz-` has five.
- So the finding clears only when `tz-` is revoked (ailab PR #287, agentforge#123).
- Revoking it is safe only once nothing signs with it:
  - The playground orchestrator signs with it (`af-creds-playground-planner`, record `allowed_workspaces: ['playground']`).
  - platform-dev signs with `cap-b5663179affeeaba5366c3ffd232681d` (live, 2026-10-30).
  - `allowed_workspaces` limits authorization; it does not prove who holds the private key. Phase 0 re-establishes the signer inventory from live Secrets (key names and kid values only).

The playground has been idle since PRs #41-43 on 2026-08-22: 0 claims in 7 days, 0 sandbox Jobs or Pods, issues last updated 2026-08-22. Its 4 `forge_pending` planner items are the permanently parked backlog the rule comments describe.

Two workloads exist only for it:
- **The dispatcher** (the KEDA scale oracle and webhook fan-out). The playground ScaledObject is its only consumer.
- **The reaper.** `Reaper.run_once` iterates only the job IDs found under its staging mount, which is the playground's (agentforge `adapters/exec/reaper.py:91`, `adapters/exec/kube.py:530,570`). So it has never covered platform-dev; the 7 Leases still held by 74-day-old platform-dev pods are that gap. It is kept running here (see Phase 1b), and the gap is a follow-up.

The conductor is broader: its estate lane also reads CP tenant Deployments and runs broker, CP-backup and alert-channel detectors. But:
- Its `agentforge-config` binds only `cchifor/agentforge-playground`, and its startup check refuses a config that binds no repos (agentforge `main.py:1042-1047`).
- Two of its detectors, TOPOLOGY_DRIFT and SCALE_ORACLE_DEAD, read `agentforge-workers/scaledobject.yaml` (`conductor/estate_lane.py:108`) and only ever covered this pool.

What could re-create the playground (captured again in Phase 0, not assumed):
- `agentforge-tenants` (a separate GitRepository) has only `tenants/tenant-zero/platform-dev/`.
- The CP DB has a single workspace, platform-dev.
- The provisioner republishes only declared kids (`capability_kid.py:503-543`).
- The kids docs are not seeded (absent from `declaredSeedPaths`).
- Admission rule 16 and the reservation stay, so the namespace cannot be re-created regardless.

There is no prior workspace retirement and no decommission runbook; this plan is the first.

## Approach

### Decision: park the conductor (owner to confirm at hand-off)

**Evidence that parking loses nothing a human sees today.** The conductor runs at T0 (`AF_CONDUCT_TIER=observe`):
- The write door refuses every filing.
- Every rule in `monitoring/agentforge-conductor-rules.yaml` reads the conductor's own health or meta series (`forge_conduct_{writes,passes,last_pass_timestamp,detector_unknown,sensor_incomplete,tier_info,...}`). None reads a finding.
- So its broker, CP-backup and alert-channel findings reach no one now. Parking removes that latent coverage, including platform-dev's, until re-arming. That is the trade the owner confirms.

**Park:**
- Phase 1a sets the Deployment to `replicas: 0`.
- Phase 1b unlists `conductor-deployment.yaml`, `conductor-podmonitor.yaml` and `conductor-externalsecret.yaml`. That restores the documented pre-arming (pre-2026-09-13) state.
- Keep `conductor-serviceaccount.yaml` and `conductor-netpol.yaml` listed. They are inert, and they keep the `agentforge-workers` Flux unit and its manifest-path expectation (`scripts/tests/test_manifest_paths.py:54,64`) intact; an empty-path unit is a known Flux trap.

**Credential, deliberately retained:**
- The conductor-bot principal and its PAT stay: the `operator/conductor/forge` seed, and grants on the playground repo, `agentforge-config` and the filing repo.
- The re-arm follow-up issue owns it: either re-arm, or revoke the PAT and grants, by a stated date.

**Rules after parking:**
- ForgeConductSecretNotReady reads ESO status; it has no series once the ExternalSecret is pruned. Verify the ES is actually gone.
- ForgeConductWakesUnanswered reads range samples. Before Phase 1a, query whether `forge_conduct_wake_writes_total` / `forge_conduct_diagnoses_total` have any series at T0, and after 1a watch the rule for its full 2h window plus offset.

**Re-arming prerequisites** (follow-up):
- an agentforge change for a CP-only estate (a no-repos config, a config source other than `agentforge-config`, and dropping the ScaledObject sensor and its two rows);
- then credential and config validation.

**Alternative (not recommended):** keep it running. That needs that agentforge change, a release and a pin bump BEFORE the retirement. Otherwise ForgeConductDetectorBlind fires on the two blind rows.

### Phase 0: pre-flight (read-only; re-run immediately before each merge)

**In-flight work:**
- No Jobs or Pods labelled `agentforge.io/workspace=playground` in `agentforge-sandbox`.
- Leases are unlabelled (`adapters/exec/kube.py:375`), so correlate every `af-sbx-<job-id>` Lease holder with a live pod; today all 7 are platform-dev's, from dead pods.
- List the playground staging directory for job dirs modified after 2026-08-22.
- No claimed or in-progress issues in `cchifor/agentforge-playground`.
- No increase in `forge_claims_total{namespace="af-tenant-tenant-zero-playground"}` over 7 days.

**Signer inventory** (key names and kid values only; never signing keys or tokens):
- `AF_CAPABILITY_KID` in every Secret cluster-wide that carries one: expect exactly `af-creds-playground-planner` = `tz-…` and `af-creds-platform-dev-delivery` = `cap-b566…`.
- The deployed image digests of the worker, brokers and provisioner.

**Kids inventory for all five declared auds** (claude-max-1..4, codex-pro):
- kid names per doc and the KV version;
- `tz-` is expected only on max-1, max-2 and codex-pro, and `cap-b566…` where the ConfigMap declares it;
- stop on any unexpected grant.

**Re-creation sources:**
- the `agentforge-tenants` GitRepository revision and its `tenants/` tree;
- the CP workspace list (read-only SELECT).

**Baseline:**
- the provisioner audit line (`3 finding(s) across 5 seat(s)`);
- broker pods (10, Ready, restarts=21);
- `af-orch-platform-dev-delivery` 1/1;
- the playground NFS directory's size.

**Platform-dev permission baseline**, for comparison after each phase:
- `kubectl auth can-i --as=system:serviceaccount:af-tenant-tenant-zero-platform-dev:<orch-sa>` for get/list Endpoints in `agentforge-broker` and `agentforge-pkgcache`, and create Jobs, Leases and Pods in `agentforge-sandbox`;
- each broker replica's `/internal/kid/cap-b5663179…` (`broker/app.py:502`) reports served with its digest.

### Phase 1a: ailab PR "stop the playground workloads" (scale to zero, nothing deleted)

Changes:
- `scaledobject.yaml`: `autoscaling.keda.sh/paused-replicas: "0"`.
- `dispatcher-deployment.yaml`: `replicas: 0`.
- `conductor-deployment.yaml`: `replicas: 0`.

Why this goes first:
- Pods exit while their CNPs, SecretStores and RBAC still exist. Pruning everything in one apply would otherwise drop the worker and dispatcher default-deny policies while credential-holding processes are still terminating. Both units are `wait: false`, and the resource list disclaims ordering (`clusters/ai/agentforge-workers.yaml:28,38`).
- It is a clean rollback point.

<!-- codex: Scaling to zero terminates workers and cancels in-flight tasks; it is not an intake-only drain (agentforge src/agentforge/app/orchestrator.py:1241,1377). First pause new claims through the scoped config, wait for work/imports to finish, then persist zero replicas even if the initial inventory was empty, since the committed ScaledObject currently holds one replica and otherwise permits new work between pre-flight and prune (src/agentforge/app/orchestrator.py:3114; kubernetes/apps/infrastructure/agentforge-workers/scaledobject.yaml:52). -->
<!-- opus-pushback: Accepted in part: zero replicas are persisted in git (this phase) even though the inventory is empty. A separate claim-pause step in agentforge-config buys nothing here. Nothing has been claimed since 2026-08-22, the in-flight gate is re-run minutes before this merge, and a task cancelled on a repo being retired has no consequence. -->

After merge:
- the three pods are gone;
- the generated HPA reports 0 desired;
- no new alerts for 2h (this covers ForgeConductWakesUnanswered's window);
- the platform-dev permission baseline is unchanged.

### Phase 1b: ailab PR "retire the playground workspace"

Flux prunes deleted objects (`prune: true`). Every deleted file is also removed from its kustomization's `resources:` list in the same commit, so `kustomize build` stays green.

1. **`agentforge-workers/`**
   - Delete the worker: `worker-*.yaml` (deployment, externalsecret, secretstore, serviceaccount, staging-pvc, webhook-service, openbao-ca-cert, podmonitor), plus `scaledobject.yaml` and `cilium-egress.yaml`.
   - Delete the dispatcher: `dispatcher-*.yaml`.
   - Unlist the three conductor files.
   - Rewrite the kustomization header to say what remains and why.
2. **`agentforge-sandbox/`**
   - Delete `tenant-namespace.yaml` (namespace `af-tenant-tenant-zero-playground`), `tenant-resourcequota.yaml` and `staging-pv.yaml`.
   - Remove the playground subject from `orchestrator-rbac.yaml`, keeping platform-dev's.
   - **KEEP the reaper** (`reaper-deployment.yaml`, `reaper-storage.yaml` = PV/PVC `af-sbx-reaper-tenant-zero-playground`, and its RBAC, netpol and ledger). It reaps only playground job dirs, so after this it idles harmlessly. Re-pointing it at platform-dev is a follow-up, because it closes an existing platform-dev GC gap.
   - **KEEP the boundary canary's target**, `workspace-pvc.yaml` (PV/PVC `af-sbx-ws-tenant-zero-playground` in `agentforge-sandbox`). Its header is rewritten to say it now serves the ADR 0019 canary, not a workspace.
   - Two retained PVs share the export directory with `staging-pv.yaml`: deleting that PV object is fine (Retain), the directory stays.
3. **`agentforge-broker/`**
   - **KEEP** the workspace=playground ingress rule in the five broker CNPs. It is the canary's positive control: the agent pod carries playground/planner labels, and `verify-sandbox-boundary.sh:261` requires a successful broker connection. With `tz-` revoked, a connection there can no longer authorize.
   - Re-targeting the canary to a disposable identity, then removing the rule, is a follow-up. The canary must never point at platform-dev's real workspace.
   - Remove the playground subject from `orchestrator-endpoints-rbac.yaml`, and the same in `agentforge-pkgcache/orchestrator-endpoints-rbac.yaml`.
4. **Seeds**
   - `sops edit security/openbao/operator-seeds.sops.yaml` to remove the `tenants/tenant-zero/playground/orchestrator` and `operator/dispatcher/forge` fragments.
   - Then run `just af-record-seed-coverage` (decrypts the edited document's `seeds.json` into `--refresh-seed-coverage`; `--write` alone preserves the stale record, `scripts/gen-broker-inventory.py:727,795`), then `gen-broker-inventory.py --check`.
   - Assert every value is still `ENC[`.
   - Never stamp paths from the live cluster Secret.
5. **Admission**
   - Remove the playground entry from `tenant-map.yaml`. It only correlates ESO objects in tenant namespaces; the canary runs in `agentforge-sandbox` and does not read it.
   - **KEEP rule 16** in `tenant-guard.yaml` (the CREATE/UPDATE prohibition) **and** `tenant-reserved-guard.yaml` (DELETE), so the namespace cannot be re-created over retained paths.
   - Update the `scripts/check-tenant-guard-cel.py` fixtures, and verify there that create/update/delete of the playground namespace is denied and platform-dev operations are allowed.
6. **`monitoring/agentforge-rules.yaml`**
   - Delete the rules that can only read the retired worker or dispatcher: ForgeTargetDown, ForgeReconcileStalled, ForgeNeedsHumanBacklogNotDraining, ForgeEngineAuthDegraded, ForgeReconcileDriftHigh, ForgeDispatchStalled, ForgeUnexplainedEscalations, ForgeOperatorServiceFailing and ForgeScaleOracleMissing.
   - **Invariant:** ForgeBrokerTargetDown and ForgeBrokerSeatReplicasUnavailable stay, with their tests (`scripts/tests/test_broker_servicemonitor.py:493`).
   - Check `agentforge-coverage-rules.yaml` ForgeOrchestratorUnscraped still covers the CP-pool half.
   - Update `agentforge-provisioner-rules.yaml:379,405` and `.test.yaml:317`.
   - Run `scripts/rules-lint.sh`.
7. **Tests**
   - `scripts/tests/test_worker_probes.py` reads the worker and dispatcher manifests (`:45`, `:115`): drop those cases, keep the rest.
   - `scripts/tests/test_broker_refresh_intervals.py:181` `AgentforgeRulesCarryTheP0EngineAlerts` requires deleted alerts (`:203`): narrow it to the surviving broker rules.
   - Add the kid-revoke helper's tests (Phase 3).
8. **Phase 3 tooling, reviewed here**
   - `scripts/af_kid_revoke.py`: a pure transform with validation, plus unit tests.
   - `scripts/openbao-revoke-kid.sh`: the I/O wrapper.
9. **Docs**
   - Add a "Retiring a workspace" section to `docs/runbooks/agentforge.md`, written from this run.
   - Update the playground references in `agentforge.md`, `agentforge-v3-cutover.md` and `sandbox-boundary-canary.md`.
   - Add a dated status line to ADR 0019 (and 0018 if it lists the playground as live).
   - Date-correct the `capability-kids-configmap.yaml:46,55,77` commentary that says `tz-` signs today and awaits a decision; leave its platform-dev data unchanged.
   - Fix the stale `agentforge-tenant-platform-dev/externalsecret.yaml:11-13` comment.
   - Leave `plans/` untouched.

**Local gates:**
- `kustomize build` of every touched path, and `scripts/manifest-lint.sh`;
- `scripts/rules-lint.sh`;
- `python -m unittest discover -s scripts/tests -p "test_*.py" -v` (the CI command, `.gitea/workflows/broker-inventory.yaml:107`);
- `check-tenant-guard-cel.py`;
- `gen-broker-inventory.py --check`;
- the `ENC[` assertion.

**Reference audit.** Across ailab and agentforge, grep `playground`, `af-orch-playground`, `agentforge-dispatcher`, `af-dispatcher`, `operator/dispatcher`, `tz-213512e80fde7c84`, `forge_pending`, plus the deleted file names and alert names. Every remaining hit must be a deliberate keep (reaper, canary, broker canary rule, rule 16 and the reservation, historical docs, agentforge follow-ups).

### Credential and authority disposition (explicit)

| Item | Disposition |
|---|---|
| Bot PATs in `AF_BOT_TOKENS` (planner/tester/impl/reviewer/ops) | **Not rotated or revoked.** Shared with platform-dev (`agentforge-v3-cutover.md:243`). Removing the playground's ESO copy is all this plan does. |
| `tz-` signing key (in the playground Secret and KV doc) | Useless after Phase 3 revokes its public record; the Secret is pruned in 1b. |
| KV `af/tenants/tenant-zero/playground/orchestrator` | Kept for now as a dormant copy, readable only by the operator-provisioner and provisioner policies (`bootstrap.py:1347`). Seed removed in 1b so DR does not restore it. Follow-up: the provisioner identity deletes it (KV-v2 delete, then a metadata delete for history), by a stated date. |
| KV `af/operator/dispatcher/{forge,webhook}` | Same as above. The seed for `forge` is removed in 1b; `webhook` was never seeded. Follow-up deletes both. |
| OpenBao role/policy `af-tenant-tenant-zero-playground` | Removed by the provisioner's namespace teardown. Phase 2 verifies they are actually absent; never force-remove the finalizer. |
| OpenBao role `af-operator-dispatcher` | Stays: it is in `DEFAULT_OPERATOR_ROLES` and re-created by reconciliation (`bootstrap.py:341,1106`). Follow-up: remove it from the declarative source in agentforge. |
| conductor-bot PAT and grants | Retained deliberately (see the Decision). Owned by the re-arm follow-up. |

### Phase 2: verify the prune (after 1b merges)

**Flux.** For each Kustomization, the Ready condition must carry the merge revision and a current `observedGeneration` (several are `wait: false`, so an old Ready proves nothing):
- `agentforge-workers`, `agentforge-sandbox`, `agentforge-broker`, `agentforge-pkgcache`, `apps`, `infrastructure` (which carries monitoring), `openbao`;
- plus the surviving `agentforge-tenants` and `agentforge-tenant-platform-dev`.

**Objects gone:**
- Deployments (worker, dispatcher, conductor), the ScaledObject and its generated HPA;
- Services, ServiceMonitor and PodMonitors;
- SecretStores, ESO auth SAs, ExternalSecrets and their generated Secrets (`af-creds-playground-planner`, the dispatcher and conductor ones);
- CNPs, the retired RBAC subjects, and the playground staging PV and PVC;
- the namespace itself, deleted rather than stuck Terminating.

**OpenBao.** The role and policy `af-tenant-tenant-zero-playground` are absent: read them with the operator token where its policy permits, and otherwise use the provisioner's teardown outcome (ownership skips, token revocation versus the TTL fallback, `controller.py:463,478`).

**Kept objects.** The reaper is Running and its PVC Bound; `workspace-pvc` is Bound; run `scripts/verify-sandbox-boundary.sh` and get the full matrix green.

**platform-dev.** The permission baseline and each broker replica's `cap-b566…` kid endpoint are unchanged, and `af-orch-platform-dev-delivery` is 1/1.

<!-- codex: Ready replicas do not exercise the shared permissions being edited: verify platform-dev can resolve broker/pkgcache Endpoints, run a sandbox task through an entitled broker, and complete/import its result (kubernetes/apps/infrastructure/agentforge-broker/orchestrator-endpoints-rbac.yaml:46; agentforge-pkgcache/orchestrator-endpoints-rbac.yaml:47; agentforge-sandbox/orchestrator-rbac.yaml:65). Capture this baseline before retirement and repeat after revocation, including credential sync and metrics coverage. -->
<!-- opus-pushback: Accepted in part: the exact RBAC edges this PR touches are checked with `auth can-i` against platform-dev's SA (Endpoints in broker and pkgcache, Jobs/Leases/Pods in the sandbox), plus every broker replica's kid endpoint, all against the Phase 0 baseline. A real sandbox task would mean filing synthetic work into the platform repo, whose pool has claimed nothing in 7 days, and it tests no edge this PR changes beyond those. -->

**Alerts.** No new alerts for 30 minutes.

### Phase 3: revoke `tz-213512e80fde7c84` (after Phase 2)

`scripts/openbao-revoke-kid.sh --kid tz-213512e80fde7c84 --aud <p>/<a> [--dry-run]`, one aud at a time: claude-max-1, then claude-max-2, then codex-pro. It is narrowly scoped; no generic revocation framework and no new OpenBao privilege.

**Read.**
- In-pod read of `af/operator/broker/<p>/<a>/kids` on `openbao-0` (loopback; the `openbao-operator-provisioner-token` on stdin, per `agentforge-v3-cutover.md:221-245`) yields the whole KV data object plus its version.
- An unreadable read is an error, distinct from "kid absent".

**Transform** (`af_kid_revoke.py`, pure):
- Parse `registry.json`.
- Require the retired kid's record to match the expected identity: kid, public-key fingerprint, and scope (`allowed_workspaces == ['playground']`).
- Require the expected platform-dev kid (`cap-b566…`) present wherever Phase 0 found it, with its record unchanged.
- Remove only `kids["tz-…"]`.
- Preserve every outer KV sibling and every other registry field and kid record. This is checked as parsed-value equality, because `bao kv put` replaces the whole data object.
- If the kid is already absent (a resumed run), report it and write nothing.

**Validate.** Pipe the complete candidate into the DEPLOYED parser, `kubectl exec <broker pod> -- python -c '...parse_kid_registry...'` (`broker/config.py:369,393`), so it is checked with the broker's own code and ceilings. Fail closed.

**Rollback artifact.** Before writing, save the complete removed public record plus the aud and KV version to an out-of-repo scratch file. It is public-key material, no secrets.

**Write.**
- `bao kv put -cas=<read version>` with the full data object.
- On a CAS conflict: re-read, re-run the transform and validation on the fresh document, and never replay the stale one.

**Output.** Kid names, the version, and before/after key sets only.

**Tests** (in 1b): malformed registry, preserved siblings and platform-dev record, identity mismatch, already-absent, CAS-conflict re-read.

**Verify after each aud,** with a bounded timeout. ESO (1m), the Secret volume and the broker's 60s reload are separate propagation stages, so poll EVERY serving replica by pod endpoint until:
- `/internal/kid/tz-213512e80fde7c84` reports `served:false`;
- `cap-b566…` still reports its expected digest.

A Ready replica still serving `tz-` is a failure: a broker with an invalid reload keeps its last-good registry. Then move to the next aud.

**After all three:**
- the provisioner audit logs `0 finding(s)`, and `ForgeProvisionerSeatEntitlementDrift` resolves;
- the brokers show no restarts and no `cap-b566…` auth failures;
- the platform-dev permission baseline and the canary still pass.

### Phase 4: owner actions (forge UI; workstation-bot cannot do these)
- Inventory the Gitea org and repo hooks (bootstrap creates org-level hooks by default, agentforge `bootstrap.py:849`). Record the IDs and URLs, and delete only those that target the dispatcher. Confirm with the owner that no platform-dev delivery depends on them; the dispatcher's only forward target was the playground worker (`dispatcher-deployment.yaml:119-120`).
- Archive `cchifor/agentforge-playground`. Its 19 open issues and 4 open PRs stay readable. Remove the bot collaborators from it; their PATs are shared and stay.

### Follow-ups (issues, filed with the PR)
1. **agentforge cleanup:**
   - drop the playground references (`domain/config.py:63` `PRIVILEGE_HARDENING_EXEMPT_REPO`, keeping the v1.1 validator for every remaining repo at `:654,707`; `estate.py:1251`; `bootstrap.py:845`);
   - remove the playground ingress rule from `provisioner/seat_templates/{anthropic,openai}.yaml` BEFORE any new seat is provisioned, or new brokers re-grant it;
   - remove `af-operator-dispatcher` from `DEFAULT_OPERATOR_ROLES`;
   - have provisioner teardown delete the tenant KV doc.
2. **Conductor re-arm or retire,** by a stated date: the CP-only changes above, its future config source, then credential and config validation. Otherwise revoke the conductor-bot PAT and grants.
3. **Delete the dormant KV docs** (the playground orchestrator, `operator/dispatcher/{forge,webhook}`) via the provisioner identity, including metadata and history, by a stated date.
4. **Re-point the reaper** at platform-dev: create and verify a new PV/PVC first, then switch the mount (closes the existing platform-dev GC gap; the 7 stale Leases). **Re-target the canary** to a disposable identity and storage, then remove the broker playground ingress rule.
5. **Delete storage last,** only after 4: the old PVs and PVCs, then, as a separate irreversible step after checking all mounts and closing the rollback window, the NFS directory `/pve-nfs/agentforge-sandbox/tenant-zero/playground`.
6. **Archive `cchifor/agentforge-config`** once no reader remains. It still has a host-mode consumer in Ansible (`dev_worker` defaults `:289,292`, disabled by default): verify no installed units on any dev-worker, and decide the conductor's config source first.

## Critical files
- `kubernetes/apps/infrastructure/agentforge-workers/*`: 1a scale to zero; 1b delete the worker and dispatcher, unlist the conductor.
- `kubernetes/apps/infrastructure/agentforge-sandbox/{kustomization,tenant-namespace,tenant-resourcequota,staging-pv,orchestrator-rbac,workspace-pvc}.yaml`
- `kubernetes/apps/infrastructure/agentforge-broker/{orchestrator-endpoints-rbac,broker-inventory}.yaml`; `agentforge-pkgcache/orchestrator-endpoints-rbac.yaml`
- `kubernetes/apps/infrastructure/security/openbao/operator-seeds.sops.yaml` (encrypted; protected, so an owner merge) and `capability-kids-configmap.yaml` (comments only)
- `kubernetes/apps/apps/agentforge/admission/tenant-map.yaml`, `scripts/check-tenant-guard-cel.py`
- `kubernetes/apps/infrastructure/monitoring/agentforge-{rules,coverage-rules,provisioner-rules}.yaml` (+ `.test.yaml`)
- `scripts/af_kid_revoke.py`, `scripts/openbao-revoke-kid.sh` (new); `scripts/tests/{test_worker_probes,test_broker_refresh_intervals,test_af_kid_revoke}.py`
- `docs/runbooks/{agentforge,agentforge-v3-cutover,sandbox-boundary-canary}.md`, `docs/decisions/0019-*.md`, `agentforge-tenant-platform-dev/externalsecret.yaml` (comment)

## Verification
- **Pre-merge:** the local gates and reference audit above; CI green; both reviewer bots.
- **Phase 1a:** the three pods are gone and the HPA is at 0; no new alerts for 2h; the platform-dev baseline is unchanged.
- **Phase 2:** the Flux revision and generation checks, the object-absence inventory, the OpenBao role/policy absence, the kept-object checks with the canary green, and the platform-dev baseline.
- **Phase 3:** per-aud, per-replica kid endpoints; audit at 0 findings; the alert resolved; brokers stable; the platform-dev baseline and canary green.

**Rollback:**
- **1a:** revert. The pods return.
- **1b:** revert, then in order:
  1. If Phase 3 already ran, restore `tz-` first (below) and verify every broker replica serves it.
  2. Wait for the provisioner to re-create the tenant role and policy, ESO to sync `af-creds-playground-planner`, and the staging PV and PVC to bind.
  3. Only then revert 1a to start the pods.
- The reverted seeds fragment returns with the git revert. The KV doc was never deleted.
- **Phase 3:** merge the saved record into a FRESHLY read document (preserving any concurrent platform-dev change), validate it with the deployed parser, CAS-write it, and poll every replica until `tz-` is served.
- **Phase 4** is not undone by git. Each step needs its inverse owner act: un-archive, re-add collaborators, re-create the hooks from the recorded IDs and URLs.

<!-- codex-review-status: complete -->
