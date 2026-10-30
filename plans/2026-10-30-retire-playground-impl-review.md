# Implementation review — retire-playground — round 1 (Phase 1a)

<!-- codex-impl-review-status: pending -->

## Summary

- The three runtime changes match Phase 1a. No blocker or missing runtime change found; credentials, network policies, RBAC and storage remain available while pods terminate.
- KEDA 2.20.1 honours `"0"`, including changing an already-paused pool from `"1"`: the annotation change triggers reconciliation, stops scaling, deletes the generated HPA and updates the target replica count. The cron trigger cannot override this pause. See the [versioned controller implementation](https://github.com/kedacore/keda/blob/v2.20.1/controllers/keda/scaledobject_controller.go#L315).
- No competing replica declaration, autoscaler or applicable PDB was found. Flux applies the dispatcher/conductor zeros; the worker correctly omits `spec.replicas`. Platform-dev retains its independent polling, credentials and permissions; the dispatcher's sole forwarding target is playground, and the conductor's observe tier performs no writes.
- No unconditional alert on these pods disappearing was found: scrape targets disappear, coverage rules require existing pods, and `ForgeScaleOracleMissing` requires a running dispatcher. Historical counter windows and the retained conductor ExternalSecret still require the planned pre-flight and post-merge checks.
- All 22 selected worker-probe, alert-contract and real-repository manifest-path tests passed; `git diff --check` passed. Kustomize rendering was blocked by filesystem access denial. Live convergence, alert observation and platform-dev baselines were not verified. The only finding is stale commentary.

## Findings

### Update comments that still describe running workloads

**Location:** kubernetes/apps/infrastructure/agentforge-workers/kustomization.yaml:56; kubernetes/apps/infrastructure/agentforge-workers/scaledobject.yaml:43  
**Severity:** nit  
<!-- codex: The resource list still describes the conductor as “replicas 1” and the worker/dispatcher as “listed and live” (kustomization.yaml:60), while scaledobject.yaml:43–51 and kustomization.yaml:68 retain instructions to hold one replica and leave removal exclusively to engine#186. Mark those instructions as historical and add the current Phase 1a parked state, making retirement the controlling reason for retaining the pause until Phase 1b. -->

## Diff stat

```text
 .../agentforge-workers/conductor-deployment.yaml               |  6 +++++-
 .../agentforge-workers/dispatcher-deployment.yaml              | 10 ++++++----
 .../apps/infrastructure/agentforge-workers/scaledobject.yaml   |  7 ++++++-
 3 files changed, 17 insertions(+), 6 deletions(-)
```