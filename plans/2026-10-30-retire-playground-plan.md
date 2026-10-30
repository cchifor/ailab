# Retire the AgentForge tenant-zero/playground workspace

## Codex Review

- The staged retirement, preservation of platform-dev grants, deferred NFS deletion, and CAS-based revocation are sound directions.
- Execution blockers remain: removing admission rule 16 weakens the reservation, deleting all playground broker ingress breaks the retained canary, and scaling to zero does not drain active work.
- The reaper does not discover jobs across the whole namespace, and parking the conductor removes monitoring beyond playground.
- Seed-coverage regeneration, alert-dependent tests, actual Flux verification targets, credential cleanup, and broker-by-broker revocation checks need corrections.
- This was a read-only source review against ailab and agentforge `origin/main`; live-state assertions and deployed-image equivalence still require pre-flight evidence.

## Context

The owner decided on 2026-10-30 to retire the playground workspace (org `tenant-zero`, ws `playground`).

The trigger is `ForgeProvisionerSeatEntitlementDrift`, which has fired since 2026-09-07:
- The legacy, hand-seeded capability kid `tz-213512e80fde7c84` is granted in the broker kid registries for anthropic/claude-max-1, anthropic/claude-max-2 and openai/codex-pro (`af/operator/broker/<p>/<a>/kids`).
- `security/openbao/capability-kids-configmap.yaml` cannot declare it:
  - Kids there are always derived as `cap-<sha256(org/ws)[:32]>`.
  - `CapabilityKidEntry.pool` holds one pool, and `tz-` has five.
- So the finding clears only when `tz-` is revoked (ailab PR #287, agentforge#123).
- Revoking it is safe only once nothing signs with it, and only the playground orchestrator does: `af-creds-playground-planner`, record `allowed_workspaces: ['playground']`.
- platform-dev signs with `cap-b5663179affeeaba5366c3ffd232681d` (verified live).
<!-- codex: Treat the signer inventory as a fresh pre-flight gate: allowed_workspaces constrains authorization but does not prove who holds a private key, and the checked-in evidence is dated (kubernetes/apps/infrastructure/security/openbao/capability-kids-configmap.yaml:47). Record current secret key names/kids without values and map deployed image digests to source revisions before relying on origin/main behavior; the worker and provisioner use different pinned images (kubernetes/apps/infrastructure/agentforge-workers/worker-deployment.yaml:126; kubernetes/apps/infrastructure/security/openbao/provisioner-deploy.yaml:46). -->

The playground has been idle since PRs #41-43 on 2026-08-22. Two other workloads exist only for it:
- **The dispatcher** (the KEDA scale oracle and webhook fan-out): the playground ScaledObject is its only consumer.
- **The conductor**: its `agentforge-config` binds only `cchifor/agentforge-playground`. Its startup check (agentforge `main.py:1042-1047`) refuses a config that binds no repos. Its estate lane reads `agentforge-workers/scaledobject.yaml` (`conductor/estate_lane.py:108`) for two detectors, TOPOLOGY_DRIFT and SCALE_ORACLE_DEAD, that only ever covered this pool.
<!-- codex: The conductor does not exist only for playground: its estate lane reads CP tenant deployments and runs broker, CP-backup, and alert-channel detectors, while its deployment also enables platform/deploy inspection (agentforge src/agentforge/conductor/estate_lane.py:105,142; kubernetes/apps/infrastructure/agentforge-workers/conductor-deployment.yaml:158,167). Make the loss of this fleet coverage, including platform-dev, an explicit part of the parking decision. -->

Nothing re-creates the playground once it is gone:
- `agentforge-tenants` has only `tenants/tenant-zero/platform-dev/`.
- The CP DB has a single workspace, platform-dev.
- The provisioner republishes only declared kids (`capability_kid.py:503-543`).
- The kids docs are not seeded (absent from `declaredSeedPaths`).
<!-- codex: The tenant repository and CP database assertions cannot be established from this checkout: agentforge-tenants reconciles a separate GitRepository (kubernetes/apps/clusters/ai/agentforge-tenants.yaml:24). Capture its current revision/tree and the CP workspace inventory before deletion, and keep the namespace reservation intact rather than relying on today's inventory alone. -->

There is no prior workspace retirement and no decommission runbook. This plan is the first.

## Approach

### Decision: park the conductor (owner to confirm at hand-off)
- Unlist `conductor-deployment.yaml`, `conductor-podmonitor.yaml` and `conductor-externalsecret.yaml` from `agentforge-workers/kustomization.yaml`. That restores the pre-arming (pre-2026-09-13) state the file headers already describe.
- Keep `conductor-serviceaccount.yaml` and `conductor-netpol.yaml` listed. They are inert, and they keep the Flux unit non-empty: an empty-path Kustomization is a known Flux trap.
- Keep the `operator/conductor/forge` seed and the conductor-bot principal, so re-arming is one list edit.
<!-- codex: Keeping the principal does not require keeping its PAT live indefinitely: the conductor identity has forge grants outside playground, and its seed preserves that credential (kubernetes/apps/infrastructure/agentforge-workers/conductor-externalsecret.yaml:25,57,148). Specify whether that authority is deliberately retained or revoked, and include credential/config validation in re-arming rather than describing it as only a list edit. -->
- Its rules in `monitoring/agentforge-conductor-rules.yaml` are all per-pod or forge_conduct_* reads. With no pod they are empty vectors, the same as today's T1 rules, and none fires.
<!-- codex: This is not true for every rule: ForgeConductSecretNotReady reads ESO status, and ForgeConductWakesUnanswered retains historical range samples after pods disappear (kubernetes/apps/infrastructure/monitoring/agentforge-conductor-rules.yaml:486,689). Verify the ExternalSecret is actually removed and evaluate shutdown behavior against the current tier/history; immediate pod absence alone is insufficient evidence. -->
- Re-arming needs an agentforge change: tolerate a CP-only estate and drop the ScaledObject sensor and its two rows. That is a follow-up issue, not this plan.
- Alternative (not recommended): keep it running. That needs the agentforge change, a new image and a pin bump BEFORE the retirement. Otherwise ForgeConductDetectorBlind fires on the two blind rows.

### Phase 0: pre-flight (no changes)
- Zero in-flight playground work:
  - no sandbox Jobs or Leases labelled workspace=playground in `agentforge-sandbox`;
<!-- codex: Use agentforge.io/workspace=playground for Jobs/Pods, but do not filter Leases that way: KubernetesLeaseClient creates them without labels (agentforge src/agentforge/adapters/exec/kube.py:375; src/agentforge/adapters/exec/sandbox.py:1211). Correlate af-sbx job IDs, staging directories, and lease holders, including staging/import activity without a remaining Job, so an empty label query cannot falsely prove drainage. -->
  - no claimed or in-progress issues in `cchifor/agentforge-playground`;
  - the dispatcher's `forge_pending` is 0 for the playground.
- Baseline:
  - the provisioner log shows `3 finding(s) across 5 seat(s)`;
  - broker pods are Ready with their restart counts;
  - `af-orch-platform-dev-delivery` is 1/1;
  - the kid NAMES in each of the three kids docs (names only, never values);
<!-- codex: Inventory all five declared audiences before choosing the three write targets, and stop on unexpected grants or missing platform-dev entries; the inventory includes max-3 and max-4 as well (kubernetes/apps/infrastructure/agentforge-broker/broker-inventory.yaml:113). This also establishes that the final all-seats revocation check covers the complete current estate. -->
  - the playground NFS directory's size.
- If any work is in flight, set `autoscaling.keda.sh/paused-replicas: "0"` in its own small PR and wait for it to drain.
<!-- codex: Scaling to zero terminates workers and cancels in-flight tasks; it is not an intake-only drain (agentforge src/agentforge/app/orchestrator.py:1241,1377). First pause new claims through the scoped config, wait for work/imports to finish, then persist zero replicas even if the initial inventory was empty, since the committed ScaledObject currently holds one replica and otherwise permits new work between pre-flight and prune (src/agentforge/app/orchestrator.py:3114; kubernetes/apps/infrastructure/agentforge-workers/scaledobject.yaml:52). -->

### Phase 1: one ailab PR, "retire the playground workspace"

All edits go on a feature branch. Flux prunes the deleted objects (`prune: true`).
<!-- codex: One commit does not provide teardown ordering: workers depend on sandbox, both use wait:false, and the resource list explicitly disclaims apply sequencing (kubernetes/apps/clusters/ai/agentforge-workers.yaml:28,38; kubernetes/apps/clusters/ai/agentforge-sandbox.yaml:27; kubernetes/apps/infrastructure/agentforge-workers/kustomization.yaml:30). Establish and verify quiescence first, and retain network policies until their pods have exited; pruning the worker/dispatcher CNPs first can remove their default-deny boundary while credentials remain in running processes (kubernetes/apps/infrastructure/agentforge-workers/cilium-egress.yaml:35; dispatcher-netpol.yaml:1). -->

1. **`agentforge-workers/`**
   - Delete the worker files: `worker-*.yaml` (deployment, externalsecret, secretstore, serviceaccount, staging-pvc, webhook-service, openbao-ca-cert, podmonitor), plus `scaledobject.yaml` and `cilium-egress.yaml`.
   - Delete the dispatcher files: `dispatcher-*.yaml`.
   - Park the conductor as above.
   - Rewrite the kustomization header so it says what is left and why.
<!-- codex: Explicitly remove every deleted resource entry from both workers and sandbox kustomizations in the same commit, not just their headers; they currently reference the files being deleted (kubernetes/apps/infrastructure/agentforge-workers/kustomization.yaml:14; kubernetes/apps/infrastructure/agentforge-sandbox/kustomization.yaml:20). Otherwise the build fails before Flux can perform any prune. -->
2. **`agentforge-sandbox/`**
   - Delete `tenant-namespace.yaml` (namespace `af-tenant-tenant-zero-playground`), `tenant-resourcequota.yaml` and `staging-pv.yaml` (both playground-only).
   - Remove the playground subject from `orchestrator-rbac.yaml`. Keep the platform-dev subject.
   - KEEP `reaper-storage.yaml` (the `af-sbx-reaper-tenant-zero-playground` PV and PVC in ns `agentforge`). The reaper's Job and Pod GC covers the whole sandbox namespace, which platform-dev's sandbox Jobs use too. Only its orphan-directory scan reads this mount, and pruning the PVC would break the shared reaper. Re-pointing it at platform-dev's staging directory is a follow-up: it first needs proof that platform-dev's orchestrator CAS-renews `af-sbx-<job-id>` Leases, or the reaper could rmtree live staging dirs.
<!-- codex: The namespace-wide GC claim is contradicted by the implementation: Reaper.run_once iterates only backend.list_job_ids(), which enumerates directories under its staging mount before checking/deleting matching Jobs and Pods (agentforge src/agentforge/adapters/exec/reaper.py:91; src/agentforge/adapters/exec/kube.py:530,570). Retaining this mount avoids breaking the existing deployment, but does not establish platform-dev GC coverage; record that existing gap and verify the deployed image before deciding the follow-up. -->
   - KEEP `workspace-pvc.yaml` (the `af-sbx-ws-tenant-zero-playground` PV and PVC in ns `agentforge-sandbox`). It is the only target of the ADR 0019 boundary canary (`egress-canary.yaml`, `test-egress-canary.yaml`, `scripts/verify-sandbox-boundary.sh`). Re-targeting the canary is a follow-up.
   - Header comments on both kept files say they now serve the reaper and the canary, not a workspace.
3. **`agentforge-broker/`**
   - Delete the workspace=playground ingress rule in `broker-anthropic-max1.yaml`, `-max2`, `-openai-codex`, `-claude-max-3` and `-claude-max-4`.
<!-- codex: This breaks the canary retained above: its agent pod uses playground/planner labels, and the harness requires a successful broker connection as its positive control (kubernetes/apps/infrastructure/agentforge-sandbox/egress-canary.yaml:55; scripts/verify-sandbox-boundary.sh:261; kubernetes/apps/infrastructure/agentforge-broker/broker-anthropic-max1.yaml:344). Preserve the necessary canary admission until a disposable replacement is working, or move that replacement into this phase and rerun the full matrix; do not weaken the positive control or point it at platform-dev's real workspace. -->
   - Remove the playground subject from `orchestrator-endpoints-rbac.yaml`.
   - Same in `agentforge-pkgcache/orchestrator-endpoints-rbac.yaml`.
4. **`security/openbao/operator-seeds.sops.yaml`**
   - `sops edit` it to remove the `tenants/tenant-zero/playground/orchestrator` and `operator/dispatcher/forge` fragments.
<!-- codex: Seed removal does not delete live KV data or retire operator auth: the dispatcher also has operator/dispatcher/webhook, and af-operator-dispatcher remains in DEFAULT_OPERATOR_ROLES and is recreated by operator reconciliation (kubernetes/apps/infrastructure/agentforge-workers/dispatcher-externalsecret.yaml:153; agentforge src/agentforge/provisioner/bootstrap.py:341,1106). Add an explicit disposition for both dispatcher KV documents, its role/policy, and outstanding credentials, including removal of the declarative role source if permanent retirement is intended; the normal operator token has no delete authority (src/agentforge/provisioner/bootstrap.py:1315). -->
   - Then run `gen-broker-inventory.py --write`, so `broker-inventory.yaml` `declaredSeedPaths` and `seedsDocumentSha256` match in the same commit.
<!-- codex: --write deliberately preserves the old coverage record and only warns about its stale digest, so this command leaves CI failing (scripts/gen-broker-inventory.py:727,795; kubernetes/apps/infrastructure/agentforge-broker/broker-inventory.yaml:99). Run just af-record-seed-coverage, which decrypts the edited file's seeds.json into --refresh-seed-coverage, then run --check; do not stamp paths from the live cluster Secret (justfile:248). -->
   - Assert that every remaining value is still `ENC[`.
5. **`apps/agentforge/admission/`**
   - Remove the playground entry from `tenant-map.yaml`, and rule 16 from `tenant-guard.yaml` if it exists only for the retired namespace.
   - KEEP the `tenant-reserved-guard.yaml` reservation, so the CP cannot mint a new `playground` workspace over leftover NFS data or OpenBao paths.
<!-- codex: Keep rule 16 as well: tenant-reserved-guard matches DELETE only, while rule 16 is the CREATE/UPDATE prohibition, so the proposed removal permits namespace recreation over retained paths (kubernetes/apps/apps/agentforge/admission/tenant-reserved-guard.yaml:23; tenant-guard.yaml:321). Removing the tenant-map entry does not replace that namespace-level reservation; verify denied create/update/delete for the tenant reconciler and allowed platform-dev operations. -->
   - Update the `scripts/check-tenant-guard-cel.py` fixtures to match.
6. **`monitoring/`**
   - Delete the rules that can only ever read the retired worker or dispatcher (`job="agentforge-worker"` / `agentforge-dispatcher` and `forge_pending`) from `agentforge-rules.yaml`: ForgeTargetDown, ForgeReconcileStalled, ForgeNeedsHumanBacklogNotDraining, ForgeEngineAuthDegraded, ForgeReconcileDriftHigh, ForgeDispatchStalled, ForgeUnexplainedEscalations, ForgeOperatorServiceFailing and ForgeScaleOracleMissing.
   - If the file empties, drop it from the monitoring kustomization.
<!-- codex: The current file will not empty: ForgeBrokerTargetDown and ForgeBrokerSeatReplicasUnavailable protect the shared broker fleet and must remain, together with their tests (kubernetes/apps/infrastructure/monitoring/agentforge-rules.yaml:548,599; scripts/tests/test_broker_servicemonitor.py:493). Make retaining these two rules an explicit invariant. -->
   - Check `agentforge-coverage-rules.yaml` ForgeOrchestratorUnscraped against the CP-pool half, which it must keep covering.
   - Update `agentforge-provisioner-rules.yaml:379,405` and its `.test.yaml:317` playground references.
   - Run `scripts/rules-lint.sh` (promtool).
7. **Tests and scripts.** Update or remove `scripts/tests/test_worker_probes.py`, which reads `worker-deployment.yaml`.
<!-- codex: Also update AgentforgeRulesCarryTheP0EngineAlerts in scripts/tests/test_broker_refresh_intervals.py:181: it explicitly requires three alerts being deleted, including ForgeDispatchStalled (line 203). test_worker_probes.py reads the dispatcher manifest too (scripts/tests/test_worker_probes.py:45), so cover both removals while retaining the unrelated broker and rule-extractor tests. -->
8. **Docs.**
   - Add a short "Retiring a workspace" section to `docs/runbooks/agentforge.md`, written from this run.
   - Update the playground references in `agentforge.md`, `agentforge-v3-cutover.md` and `sandbox-boundary-canary.md`.
   - Add a dated status line to ADR 0019 (and 0018 if it lists the playground as live).
   - Fix the stale `agentforge-tenant-platform-dev/externalsecret.yaml:11-13` comment, which says platform-dev uses `tz-`.
<!-- codex: Also date or correct the capability ConfigMap's unresolved-decision/live-playground commentary, which currently tells operators that this kid signs today and still needs a decision (kubernetes/apps/infrastructure/security/openbao/capability-kids-configmap.yaml:46,55,77). Leave its platform-dev policy data unchanged. -->
   - Leave `plans/` untouched; those are historical records.

Local gates before pushing:
- `kustomize build` of every touched path, and `scripts/manifest-lint.sh`;
- `scripts/rules-lint.sh`;
- `python -m pytest scripts/tests`;
<!-- codex: Run the actual CI command too: python -m unittest discover -s scripts/tests -p "test_*.py" -v (.gitea/workflows/broker-inventory.yaml:107). Keeping the workers Flux unit preserves the existing manifest-path expectation; if that choice changes, update the closed path list and Flux wiring together (scripts/tests/test_manifest_paths.py:54,64). -->
- `git grep -n playground` reviewed line by line, so that every remaining hit is a deliberate keep (reaper storage, canary, reservation, historical docs).
<!-- codex: Expand the reference audit to af-orch-playground, agentforge-dispatcher, af-dispatcher, operator/dispatcher, tz-213512e80fde7c84, and forge_pending in both repos; several operative references contain no playground string (agentforge src/agentforge/provisioner/bootstrap.py:343; kubernetes/apps/infrastructure/agentforge-workers/dispatcher-externalsecret.yaml:153). Include consumers of deleted filenames and alert names, not only workspace-name matches. -->

### Phase 2: verify the prune (after merge)
- Flux Kustomizations `agentforge-workers`, `agentforge-sandbox`, `agentforge-broker`, `apps`, `monitoring` and `openbao` are all Ready.
<!-- codex: There is no separate monitoring Flux Kustomization here: monitoring is included by infrastructure; also check the changed agentforge-pkgcache unit and the surviving agentforge-tenants/agentforge-tenant-platform-dev units (kubernetes/apps/infrastructure/kustomization.yaml:10; kubernetes/apps/clusters/ai/agentforge-pkgcache.yaml:4; agentforge-tenants.yaml:16). Require the relevant applied revision and current observed generation, because Ready from an older reconciliation, or wait:false, does not prove this prune completed (kubernetes/apps/clusters/ai/agentforge-workers.yaml:38). -->
- Namespace `af-tenant-tenant-zero-playground` is deleted, not stuck Terminating. Its `agentforge.io/provisioner` finalizer teardown deletes the OpenBao role and policy `af-tenant-tenant-zero-playground`; confirm in the provisioner log.
<!-- codex: Teardown skips unowned/foreign objects and token revocation can fall back to TTL, so namespace disappearance and a generic teardown log do not prove authority was removed (agentforge src/agentforge/provisioner/controller.py:463,478). Verify the actual role/policy are absent, inspect ownership/revocation outcomes and the TTL fallback, and do not force-remove the finalizer to bypass a cleanup failure. -->
- The dispatcher, worker and conductor pods are gone. The reaper is still Running.
<!-- codex: Check the rest of the object inventory as well: Deployments/ScaledObject and generated HPA, Services/monitors, ESO stores/auth SAs, generated Secrets, retired RBAC subjects, and the staging PV/PVC; pod absence alone permits dormant credentials or recreatable workloads to remain (kubernetes/apps/infrastructure/agentforge-workers/kustomization.yaml:14,20,26; dispatcher-externalsecret.yaml:87,135). Verify the deliberately retained PVCs remain Bound and the boundary canary still passes. -->
- `af-orch-platform-dev-delivery` and the brokers are unchanged.
<!-- codex: Ready replicas do not exercise the shared permissions being edited: verify platform-dev can resolve broker/pkgcache Endpoints, run a sandbox task through an entitled broker, and complete/import its result (kubernetes/apps/infrastructure/agentforge-broker/orchestrator-endpoints-rbac.yaml:46; agentforge-pkgcache/orchestrator-endpoints-rbac.yaml:47; agentforge-sandbox/orchestrator-rbac.yaml:65). Capture this baseline before retirement and repeat after revocation, including credential sync and metrics coverage. -->
- No new alerts after 30 minutes.

### Phase 3: revoke `tz-213512e80fde7c84` (after Phase 2)
- Codify the procedure as `scripts/openbao-revoke-kid.sh <kid> <aud>...`, reviewed in the Phase 1 PR, and run it. For each `af/operator/broker/{anthropic/claude-max-1,anthropic/claude-max-2,openai/codex-pro}/kids`:
<!-- codex: Keep this helper narrowly scoped and test its destructive boundaries: validate audience path components and the expected retired identity/public-key fingerprint, handle already-absent entries safely on a resumed partial run, and fail or freshly re-read/re-merge after CAS conflicts. The existing lifecycle already demonstrates these safeguards, so avoid inventing a second generic revocation framework or broadening OpenBao privileges (agentforge src/agentforge/provisioner/identity.py:67; src/agentforge/provisioner/capability_kid.py:546,1128). -->
  - read it in-pod (`openbao-0`, loopback, the `openbao-operator-provisioner-token` passed on stdin per `agentforge-v3-cutover.md:221-245`);
  - parse the `registry.json` string;
  - assert the kid is present and is not the only entry;
<!-- codex: “Not the only entry” does not establish platform-dev safety: explicitly require its expected kid and unchanged authorization record on each target, and reject unexpected scope on the retired entry (agentforge src/agentforge/provisioner/capability_kid.py:476; kubernetes/apps/infrastructure/security/openbao/capability-kids-configmap.yaml:115). Also make absence distinguishable from a failed/unreadable read so retries cannot turn an error into success. -->
  - remove `kids["tz-213512e80fde7c84"]`, keeping every other field byte-identical;
<!-- codex: bao kv put replaces the complete KV data object, so preserve outer KV siblings as well as unknown registry fields and all other kid records; changing only registry.json must not discard other properties (agentforge src/agentforge/provisioner/capability_kid.py:508,529). Require equality of untouched parsed values rather than unnecessary byte-preserving JSON surgery, and never replay a stale full document after a CAS conflict. -->
  - re-parse and validate the result;
<!-- codex: JSON syntax validation is insufficient: run the broker's parse_kid_registry validator over the complete result, including public keys and required policy ceilings, before any write (agentforge src/agentforge/broker/config.py:369,393). Add focused helper tests for malformed registries, preserved siblings/platform-dev records, CAS conflict, and partial-run recovery. -->
  - write it with `bao kv put -cas=<read version>`.
- The script prints kid NAMES only and supports `--dry-run`, which shows the before and after key sets.
- A bad `registry.json` is the worst case: a restarting broker fails `SEAT_FINDING_INVALID`. Validate before writing, and do one aud at a time, checking broker health between them.
<!-- codex: SEAT_FINDING_INVALID is a provisioner audit classification, not the broker startup error; a running broker with an invalid reload retains its last-good registry and can continue authorizing tz- while Ready (agentforge src/agentforge/provisioner/capability_kid.py:1511; src/agentforge/broker/config.py:494). Treat continued authorization from stale replicas as a failure even without restarts. -->
- Verify:
  - ESO syncs `broker-*-kids` within 1 minute;
<!-- codex: ESO's 1m refresh is only one propagation stage: Secret-volume delivery and the broker's 60s reload follow it (kubernetes/apps/infrastructure/agentforge-broker/broker-anthropic-max1.yaml:276; configmap.yaml:46). Poll every serving replica by pod endpoint until /internal/kid/tz-213512e80fde7c84 reports served:false and platform-dev's kid still reports its expected digest, with a bounded timeout; a load-balanced Service probe is insufficient (agentforge src/agentforge/broker/app.py:502). -->
  - the provisioner audit logs `0 finding(s)`;
  - `ForgeProvisionerSeatEntitlementDrift` resolves;
  - the brokers have no restarts and no `cap-b566…` auth failures;
  - platform-dev keeps claiming work.

### Phase 4: owner actions (forge UI; workstation-bot cannot do these)
- Archive `cchifor/agentforge-playground`. It has 19 open issues and 4 open PRs, which stay readable. Remove the bot collaborators from it.
<!-- codex: Removing repo collaborators does not revoke PATs, HMACs, or other credentials retained in KV; inventory credential ownership and revoke dedicated retired credentials while preserving those shared with platform-dev. The dispatcher projects an AF_BOT_TOKENS map covering all five bot principals, and both tenant documents carry that map, so deleting bot users or bulk-rotating their tokens would risk the surviving tenant (kubernetes/apps/infrastructure/agentforge-workers/dispatcher-externalsecret.yaml:114; docs/runbooks/agentforge-v3-cutover.md:243). -->
- If a Gitea org or repo webhook pointed at the dispatcher, delete it (UNVERIFIED: workstation-bot gets a 403 on hooks).
<!-- codex: Resolve the hook inventory before removing the receiver and record the exact URLs/IDs to disable; bootstrap creates org-level hooks by default, so archiving playground alone does not establish that deliveries stop (agentforge src/agentforge/bootstrap.py:849). Remove only dispatcher-targeting hooks and verify any platform-dev delivery path remains functional. -->

### Follow-ups (issues, not this plan)
1. agentforge:
   - drop the `PRIVILEGE_HARDENING_EXEMPT_REPO` (`domain/config.py:63`), `estate.py:1251` and `bootstrap.py:845` playground references, and the playground ingress rule in `provisioner/seat_templates/{anthropic,openai}.yaml`;
<!-- codex: Template cleanup needs an explicit dependency on any subsequent seat provisioning, or new brokers will restore the retired ingress grant (agentforge src/agentforge/provisioner/seat_templates/anthropic.yaml:346; openai.yaml:349). Removing the privilege exception must preserve the requirement for v1.1 on every remaining repo, not remove the validator that enforces it (src/agentforge/domain/config.py:654,707). -->
   - make the conductor CP-only capable (no-repos config and the ScaledObject sensor) before re-arming;
   - have provisioner teardown delete the tenant KV doc.
2. Delete the dormant KV doc `af/tenants/tenant-zero/playground/orchestrator`. Its only reader policy goes with the teardown, and only the provisioner identity can delete under `tenants/*`.
<!-- codex: It is not left without readers: the operator-provisioner policy also grants read on af/data/tenants/* (agentforge src/agentforge/provisioner/bootstrap.py:1347). Give retained credential material an explicit cleanup owner/deadline and distinguish recoverable KV-v2 deletion from removal of historical versions; removing the tenant role or seed is not credential revocation. -->
3. Re-point the reaper's directory scan at platform-dev, and re-target the boundary canary. Then delete `reaper-storage.yaml`, `workspace-pvc.yaml` and the NFS directory `/pve-nfs/agentforge-sandbox/tenant-zero/playground`.
<!-- codex: Create and verify replacement mounts/resources before deleting these files; editing their existing objects and then pruning the files would delete the replacement too (kubernetes/apps/infrastructure/agentforge-sandbox/reaper-deployment.yaml:126; reaper-storage.yaml:60). Keep the canary on disposable storage, and make NFS deletion a separate irreversible step after checking all mounts and closing the rollback window, since multiple Retain PVs share this same export directory (workspace-pvc.yaml:51; staging-pv.yaml:39,46). -->
4. Archive `cchifor/agentforge-config`, which has no reader once the conductor is parked.
<!-- codex: The repo still has a declared host-mode consumer: the Ansible runtime and updater use agentforge-config, although installation defaults to disabled (ansible/roles/dev_worker/defaults/main.yml:289,292; tasks/agentforge.yml:5). Verify no previously installed services/timers or other consumers remain before asserting “no reader,” and include the conductor's future config source in its re-arming prerequisites. -->

## Critical files
- `kubernetes/apps/infrastructure/agentforge-workers/*`: retire the worker and dispatcher; park the conductor.
- `kubernetes/apps/infrastructure/agentforge-sandbox/{kustomization,tenant-namespace,tenant-resourcequota,staging-pv,orchestrator-rbac,reaper-storage,workspace-pvc}.yaml`
- `kubernetes/apps/infrastructure/agentforge-broker/broker-*.yaml`, `orchestrator-endpoints-rbac.yaml`, `broker-inventory.yaml`; `agentforge-pkgcache/orchestrator-endpoints-rbac.yaml`
- `kubernetes/apps/infrastructure/security/openbao/operator-seeds.sops.yaml` (encrypted; protected, so an owner merge)
- `kubernetes/apps/apps/agentforge/admission/{tenant-map,tenant-guard}.yaml`, `scripts/check-tenant-guard-cel.py`
- `kubernetes/apps/infrastructure/monitoring/agentforge-{rules,coverage-rules,provisioner-rules}.yaml` (+ `.test.yaml`), `monitoring/kustomization.yaml`
- `scripts/openbao-revoke-kid.sh` (new), `scripts/tests/test_worker_probes.py`
- `docs/runbooks/{agentforge,agentforge-v3-cutover,sandbox-boundary-canary}.md`, `docs/decisions/0019-*.md`, `agentforge-tenant-platform-dev/externalsecret.yaml` (comment)

## Verification
- Pre-merge: kustomize build and `manifest-lint.sh` on every touched path; `rules-lint.sh`; `pytest scripts/tests`; `check-tenant-guard-cel.py`; `gen-broker-inventory.py --check`; the sops `ENC[` assertion; the `git grep playground` audit; CI green; both reviewer bots.
- Post-merge (Phase 2): the Flux, namespace, finalizer, OpenBao role/policy, reaper and platform-dev checks above.
- Post-revocation (Phase 3): audit at 0 findings, the alert resolved, brokers stable, platform-dev unaffected, kids docs carry only declared kids.
- Rollback:
  - Phase 1 is a git revert. Flux re-creates everything; the seeds fragment comes back with the revert, and the KV doc was never deleted.
<!-- codex: A revert is not a complete rollback after later phases: restore and verify broker authorization before restarting playground, then wait for recreated tenant roles/ESO credentials and storage bindings before resuming claims (agentforge src/agentforge/provisioner/controller.py:567; kubernetes/apps/infrastructure/agentforge-workers/worker-secretstore.yaml:65; worker-staging-pvc.yaml:23). Repo archival, collaborator removal, webhook deletion, and any credential revocation also require explicit inverse owner actions and are not undone by git. -->
  - Phase 3: re-add the kid entry from the dry-run's captured before-state (kid metadata only, no secret material) with another CAS write.
<!-- codex: The specified dry-run prints only names, which cannot reconstruct the removed public key and authorization ceilings; capture each complete removed public record plus its audience/version in a controlled rollback artifact before writing (agentforge src/agentforge/broker/config.py:372,393). Roll back by merging that record into a freshly read document with CAS, preserving concurrent platform-dev changes, then verify every replica loaded it before resuming the retired signer (src/agentforge/provisioner/capability_kid.py:508,534; src/agentforge/broker/app.py:502). -->

<!-- codex-review-status: complete -->
