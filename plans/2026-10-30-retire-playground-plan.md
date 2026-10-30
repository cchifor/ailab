# Retire the AgentForge tenant-zero/playground workspace

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

The playground has been idle since PRs #41-43 on 2026-08-22. Two other workloads exist only for it:
- **The dispatcher** (the KEDA scale oracle and webhook fan-out): the playground ScaledObject is its only consumer.
- **The conductor**: its `agentforge-config` binds only `cchifor/agentforge-playground`. Its startup check (agentforge `main.py:1042-1047`) refuses a config that binds no repos. Its estate lane reads `agentforge-workers/scaledobject.yaml` (`conductor/estate_lane.py:108`) for two detectors, TOPOLOGY_DRIFT and SCALE_ORACLE_DEAD, that only ever covered this pool.

Nothing re-creates the playground once it is gone:
- `agentforge-tenants` has only `tenants/tenant-zero/platform-dev/`.
- The CP DB has a single workspace, platform-dev.
- The provisioner republishes only declared kids (`capability_kid.py:503-543`).
- The kids docs are not seeded (absent from `declaredSeedPaths`).

There is no prior workspace retirement and no decommission runbook. This plan is the first.

## Approach

### Decision: park the conductor (owner to confirm at hand-off)
- Unlist `conductor-deployment.yaml`, `conductor-podmonitor.yaml` and `conductor-externalsecret.yaml` from `agentforge-workers/kustomization.yaml`. That restores the pre-arming (pre-2026-09-13) state the file headers already describe.
- Keep `conductor-serviceaccount.yaml` and `conductor-netpol.yaml` listed. They are inert, and they keep the Flux unit non-empty: an empty-path Kustomization is a known Flux trap.
- Keep the `operator/conductor/forge` seed and the conductor-bot principal, so re-arming is one list edit.
- Its rules in `monitoring/agentforge-conductor-rules.yaml` are all per-pod or forge_conduct_* reads. With no pod they are empty vectors, the same as today's T1 rules, and none fires.
- Re-arming needs an agentforge change: tolerate a CP-only estate and drop the ScaledObject sensor and its two rows. That is a follow-up issue, not this plan.
- Alternative (not recommended): keep it running. That needs the agentforge change, a new image and a pin bump BEFORE the retirement. Otherwise ForgeConductDetectorBlind fires on the two blind rows.

### Phase 0: pre-flight (no changes)
- Zero in-flight playground work:
  - no sandbox Jobs or Leases labelled workspace=playground in `agentforge-sandbox`;
  - no claimed or in-progress issues in `cchifor/agentforge-playground`;
  - the dispatcher's `forge_pending` is 0 for the playground.
- Baseline:
  - the provisioner log shows `3 finding(s) across 5 seat(s)`;
  - broker pods are Ready with their restart counts;
  - `af-orch-platform-dev-delivery` is 1/1;
  - the kid NAMES in each of the three kids docs (names only, never values);
  - the playground NFS directory's size.
- If any work is in flight, set `autoscaling.keda.sh/paused-replicas: "0"` in its own small PR and wait for it to drain.

### Phase 1: one ailab PR, "retire the playground workspace"

All edits go on a feature branch. Flux prunes the deleted objects (`prune: true`).

1. **`agentforge-workers/`**
   - Delete the worker files: `worker-*.yaml` (deployment, externalsecret, secretstore, serviceaccount, staging-pvc, webhook-service, openbao-ca-cert, podmonitor), plus `scaledobject.yaml` and `cilium-egress.yaml`.
   - Delete the dispatcher files: `dispatcher-*.yaml`.
   - Park the conductor as above.
   - Rewrite the kustomization header so it says what is left and why.
2. **`agentforge-sandbox/`**
   - Delete `tenant-namespace.yaml` (namespace `af-tenant-tenant-zero-playground`), `tenant-resourcequota.yaml` and `staging-pv.yaml` (both playground-only).
   - Remove the playground subject from `orchestrator-rbac.yaml`. Keep the platform-dev subject.
   - KEEP `reaper-storage.yaml` (the `af-sbx-reaper-tenant-zero-playground` PV and PVC in ns `agentforge`). The reaper's Job and Pod GC covers the whole sandbox namespace, which platform-dev's sandbox Jobs use too. Only its orphan-directory scan reads this mount, and pruning the PVC would break the shared reaper. Re-pointing it at platform-dev's staging directory is a follow-up: it first needs proof that platform-dev's orchestrator CAS-renews `af-sbx-<job-id>` Leases, or the reaper could rmtree live staging dirs.
   - KEEP `workspace-pvc.yaml` (the `af-sbx-ws-tenant-zero-playground` PV and PVC in ns `agentforge-sandbox`). It is the only target of the ADR 0019 boundary canary (`egress-canary.yaml`, `test-egress-canary.yaml`, `scripts/verify-sandbox-boundary.sh`). Re-targeting the canary is a follow-up.
   - Header comments on both kept files say they now serve the reaper and the canary, not a workspace.
3. **`agentforge-broker/`**
   - Delete the workspace=playground ingress rule in `broker-anthropic-max1.yaml`, `-max2`, `-openai-codex`, `-claude-max-3` and `-claude-max-4`.
   - Remove the playground subject from `orchestrator-endpoints-rbac.yaml`.
   - Same in `agentforge-pkgcache/orchestrator-endpoints-rbac.yaml`.
4. **`security/openbao/operator-seeds.sops.yaml`**
   - `sops edit` it to remove the `tenants/tenant-zero/playground/orchestrator` and `operator/dispatcher/forge` fragments.
   - Then run `gen-broker-inventory.py --write`, so `broker-inventory.yaml` `declaredSeedPaths` and `seedsDocumentSha256` match in the same commit.
   - Assert that every remaining value is still `ENC[`.
5. **`apps/agentforge/admission/`**
   - Remove the playground entry from `tenant-map.yaml`, and rule 16 from `tenant-guard.yaml` if it exists only for the retired namespace.
   - KEEP the `tenant-reserved-guard.yaml` reservation, so the CP cannot mint a new `playground` workspace over leftover NFS data or OpenBao paths.
   - Update the `scripts/check-tenant-guard-cel.py` fixtures to match.
6. **`monitoring/`**
   - Delete the rules that can only ever read the retired worker or dispatcher (`job="agentforge-worker"` / `agentforge-dispatcher` and `forge_pending`) from `agentforge-rules.yaml`: ForgeTargetDown, ForgeReconcileStalled, ForgeNeedsHumanBacklogNotDraining, ForgeEngineAuthDegraded, ForgeReconcileDriftHigh, ForgeDispatchStalled, ForgeUnexplainedEscalations, ForgeOperatorServiceFailing and ForgeScaleOracleMissing.
   - If the file empties, drop it from the monitoring kustomization.
   - Check `agentforge-coverage-rules.yaml` ForgeOrchestratorUnscraped against the CP-pool half, which it must keep covering.
   - Update `agentforge-provisioner-rules.yaml:379,405` and its `.test.yaml:317` playground references.
   - Run `scripts/rules-lint.sh` (promtool).
7. **Tests and scripts.** Update or remove `scripts/tests/test_worker_probes.py`, which reads `worker-deployment.yaml`.
8. **Docs.**
   - Add a short "Retiring a workspace" section to `docs/runbooks/agentforge.md`, written from this run.
   - Update the playground references in `agentforge.md`, `agentforge-v3-cutover.md` and `sandbox-boundary-canary.md`.
   - Add a dated status line to ADR 0019 (and 0018 if it lists the playground as live).
   - Fix the stale `agentforge-tenant-platform-dev/externalsecret.yaml:11-13` comment, which says platform-dev uses `tz-`.
   - Leave `plans/` untouched; those are historical records.

Local gates before pushing:
- `kustomize build` of every touched path, and `scripts/manifest-lint.sh`;
- `scripts/rules-lint.sh`;
- `python -m pytest scripts/tests`;
- `git grep -n playground` reviewed line by line, so that every remaining hit is a deliberate keep (reaper storage, canary, reservation, historical docs).

### Phase 2: verify the prune (after merge)
- Flux Kustomizations `agentforge-workers`, `agentforge-sandbox`, `agentforge-broker`, `apps`, `monitoring` and `openbao` are all Ready.
- Namespace `af-tenant-tenant-zero-playground` is deleted, not stuck Terminating. Its `agentforge.io/provisioner` finalizer teardown deletes the OpenBao role and policy `af-tenant-tenant-zero-playground`; confirm in the provisioner log.
- The dispatcher, worker and conductor pods are gone. The reaper is still Running.
- `af-orch-platform-dev-delivery` and the brokers are unchanged.
- No new alerts after 30 minutes.

### Phase 3: revoke `tz-213512e80fde7c84` (after Phase 2)
- Codify the procedure as `scripts/openbao-revoke-kid.sh <kid> <aud>...`, reviewed in the Phase 1 PR, and run it. For each `af/operator/broker/{anthropic/claude-max-1,anthropic/claude-max-2,openai/codex-pro}/kids`:
  - read it in-pod (`openbao-0`, loopback, the `openbao-operator-provisioner-token` passed on stdin per `agentforge-v3-cutover.md:221-245`);
  - parse the `registry.json` string;
  - assert the kid is present and is not the only entry;
  - remove `kids["tz-213512e80fde7c84"]`, keeping every other field byte-identical;
  - re-parse and validate the result;
  - write it with `bao kv put -cas=<read version>`.
- The script prints kid NAMES only and supports `--dry-run`, which shows the before and after key sets.
- A bad `registry.json` is the worst case: a restarting broker fails `SEAT_FINDING_INVALID`. Validate before writing, and do one aud at a time, checking broker health between them.
- Verify:
  - ESO syncs `broker-*-kids` within 1 minute;
  - the provisioner audit logs `0 finding(s)`;
  - `ForgeProvisionerSeatEntitlementDrift` resolves;
  - the brokers have no restarts and no `cap-b566…` auth failures;
  - platform-dev keeps claiming work.

### Phase 4: owner actions (forge UI; workstation-bot cannot do these)
- Archive `cchifor/agentforge-playground`. It has 19 open issues and 4 open PRs, which stay readable. Remove the bot collaborators from it.
- If a Gitea org or repo webhook pointed at the dispatcher, delete it (UNVERIFIED: workstation-bot gets a 403 on hooks).

### Follow-ups (issues, not this plan)
1. agentforge:
   - drop the `PRIVILEGE_HARDENING_EXEMPT_REPO` (`domain/config.py:63`), `estate.py:1251` and `bootstrap.py:845` playground references, and the playground ingress rule in `provisioner/seat_templates/{anthropic,openai}.yaml`;
   - make the conductor CP-only capable (no-repos config and the ScaledObject sensor) before re-arming;
   - have provisioner teardown delete the tenant KV doc.
2. Delete the dormant KV doc `af/tenants/tenant-zero/playground/orchestrator`. Its only reader policy goes with the teardown, and only the provisioner identity can delete under `tenants/*`.
3. Re-point the reaper's directory scan at platform-dev, and re-target the boundary canary. Then delete `reaper-storage.yaml`, `workspace-pvc.yaml` and the NFS directory `/pve-nfs/agentforge-sandbox/tenant-zero/playground`.
4. Archive `cchifor/agentforge-config`, which has no reader once the conductor is parked.

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
  - Phase 3: re-add the kid entry from the dry-run's captured before-state (kid metadata only, no secret material) with another CAS write.

<!-- codex-review-status: pending -->
