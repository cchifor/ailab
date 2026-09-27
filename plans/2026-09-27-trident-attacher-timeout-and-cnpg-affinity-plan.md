# Trident attach timeout (IaC), CNPG required anti-affinity, sandbox recycle path

## Codex Review

- The Kyverno approach is viable, but the nested-loop patch needs explicit 1.13.4-compatible syntax and tests for indices, argument matching, and idempotence.
- Admission mutation can prevent timeout drift without stopping repeated operator reconciliation. The existing hand patch also makes the proposed verification vulnerable to a false pass.
- CNPG requests 250m CPU per instance, so ~500m spare CPU alone does not imply a stall. Node eligibility, memory, placement, storage delays, and recovery sequencing still need explicit gates.
- The recycle script needs stronger credential handling and tenant verification. Its asynchronous polling contract also conflicts with the inspected local Airlock source.
- Resolve the missing cluster contexts, rollout rollback procedures, and verification gaps before execution.

## Context

2026-09-27, after the QNAP LUN-9 repair (ailab#865): the QNAP CSI driver (qnap-csi v1.6.0, Trident
fork) serves every ControllerPublish/Unpublish/Delete with a full `Qvolume List` + one `Get Volume`
per LUN (~2.5–3 s each; each is one NAS `disk_manage.cgi` call that walks all volume labels, so the
sweep is O(n²)). At 45 LUNs a sweep is ≈110–135 s, above the stock `csi-attacher --timeout=60s`, so
every attach and detach ends in `DeadlineExceeded` and retries re-queue sweeps until the controller
is saturated. Observed: a publish that ran 09:29:37→09:34:58Z, then `client rate limiter Wait
returned an error: context canceled`; two `strive-pg-9` detaches stuck for 3 h.

Imperative state left in the cluster today (the debt this plan retires):
- `deploy/trident-controller` container 3 (`csi-attacher`) args[1] patched to `--timeout=600s`;
- `deploy/trident-operator` scaled to 0 replicas, because the operator (qnap-csi-operator v1.6.0,
  `TridentOrchestrator` exposes only `debug/namespace/tridentImage`; the chart hard-codes
  `replicas: 1` for the operator and the sidecar args live inside the operator image) reverted the
  patch within 30 s;
- 8 Released `testpool` PVs flipped to `Retain` (annotated `ailab.io/retain-reason`) to stop their
  delete loops — that part stays; their NAS cleanup is ailab#880.

Two related items from the same program: (a) `strive-pg`'s three instances had doubled up on
talos-cp3 since the 09-21 CP reboots because CNPG's default `preferred` anti-affinity is
IgnoredDuringExecution (verified analysis on platform#1672); the #1672 roll re-spread them by luck of
headroom, not by rule. (b) The three `forever` sandboxes in `strive-sandboxes-ailab` each reserve a
full core (Guaranteed); platform#1644 (in the sweep build, airlock rolled 09:25:13Z) makes new
sandboxes request 100m, but pod resources are immutable, so they must be recycled through airlock's
own lifecycle (teardown → deploy), which requires a member of tenant `dbe45925-…` — the only members
are the two human accounts; `ops-admin@localhost` is a tenant admin of `…0001` and
`_resolve_fleet_scope` lets only `platform:support` act across tenants.

<!-- codex: Required anti-affinity is also IgnoredDuringExecution: it constrains newly scheduled pods but does not itself evict or rebalance existing colocated instances. The redistribution claim depends on CNPG actually recreating the affected pods. -->

## Approach

### 1. Make the attacher timeout GitOps state: a Kyverno mutate policy (ailab)

Kyverno 1.13.4 is installed cluster-wide and ailab already ships a ClusterPolicy through Flux
(`kubernetes/apps/infrastructure/helmtest/kyverno-protect-reserved.yaml`). Add
`kubernetes/apps/infrastructure/storage/kyverno-trident-attacher-timeout.yaml` (listed in the storage
`kustomization.yaml`, next to `iscsi-recovery-tmo.yaml`, which fixes the driver's other timeout bug):

<!-- codex: The existing validation policy does not prove that Deployment mutation is covered by the admission webhook. Check webhook rules, namespace/resource exclusions, and Kyverno availability. Document the CRD/webhook bootstrap dependency and failurePolicy choice: Fail can block controller recovery during a Kyverno outage; Ignore can admit the original timeout. -->

- `ClusterPolicy trident-attacher-timeout`, `background: false`, admission-time mutate on
  CREATE/UPDATE of `Deployment` `trident-controller` in namespace `trident`.
- `foreach` over `request.object.spec.template.spec.containers` with precondition
  `{{element.name}} == csi-attacher`, nested `foreach` over `element.args` replacing the element that
  matches `^--timeout=` with `--timeout=600s` (same value as the provisioner sidecar; a detach uses
  the same flag). No fixed indices, so an operator upgrade that reorders containers/args still gets
  mutated; if the sidecar is renamed the policy is a no-op and the verification step catches it.

<!-- codex: Nested foreach is supported, but this prose is not yet a verifiable policy. Express the outer name check as Kyverno preconditions, then use patchesJson6902 with /spec/template/spec/containers/{{elementIndex0}}/args/{{elementIndex1}}. Inside the inner loop, element is the argument string. Kyverno 1.13 documents preconditions only at the top-level loop, so do not implement the timeout match as an inner-loop precondition; use a supported conditional replacement that preserves nonmatches. Equals does not interpret a regex, and filtering either list before using its loop index would address the wrong original array position. -->

<!-- codex: Define behavior for absent/null args, missing timeout, duplicate timeout flags, and the alternate two-argument form "--timeout", "60s". A replace-only rule silently misses absent flags. Preserve every unrelated argument and all five other containers, including provisioner 600s and resizer/snapshotter 300s; a strategic-merge patch containing one scalar args entry can replace the entire args list. Add a post-upgrade check because this one-time verification cannot detect a future rename or a higher upstream timeout being overwritten. -->

- Why 600 s and not "fix the sweep": the sweep cost is inside QNAP's `storage-api-server`; only 3 of
  the 45 LUNs are removable orphans (LUNs 4, 43, 44; the rest are live or intentionally retained), so
  the count cannot drop under the ~20 that a 60 s budget would need.

<!-- codex: Treat 600s as mitigation, not a demonstrated upper bound: queueing, concurrent operations, and LUN growth can consume it. Record attach/detach latency and retry/backlog acceptance criteria, plus a removal condition when the driver exposes a supported timeout or fixes the sweep. The attacher flag does not change provisioner DeleteVolume or other sidecar deadlines. -->

- Why admission-only (no `mutateExistingOnPolicyUpdate`): it needs no extra RBAC for the background
  controller (the helmtest policy's header warns against widening Kyverno's grants), and the operator
  re-applies the Deployment on every start, so restoring the operator is the trigger.

<!-- codex: Ordinary admission mutation needs no background write grant, but background:false controls scanning; omission of mutate-existing targets is what keeps this an admission-only rule. Installing the policy does not repair an existing Deployment. Prove the operator submits a matching admission request on restart, or define an explicit harmless Deployment update as the trigger. -->

- Operator interaction: the operator's re-apply is mutated at admission to the same object it already
  is, so the API server records no change and no rollout happens; the operator does not fight a
  no-op. Verified in the verification step, not assumed.

<!-- codex: This conclusion is too strong. The operator may repeatedly compare desired 60s with observed 600s and issue updates even when admission preserves the stored PodSpec. Other metadata changes or delete/recreate reconciliation can still cause churn. Inspect the v1.6.0 reconciliation path and observe requests/logs, Deployment UID/generation, ReplicaSets, and pod UIDs over multiple cycles; stable generation alone proves neither zero writes nor a quiet operator. -->

### 2. Restore the operator and remove the hand patch (ailab, operational)

After Flux reports the policy Ready: `kubectl -n trident scale deploy trident-operator --replicas=1`
(that is the chart's declared state, so this is un-doing the imperative drift, not new drift). Watch
`deploy/trident-controller` args stay `--timeout=600s`, `tridentorchestrator/trident` status
`Installed`, and run a real attach test (step 5).

<!-- codex: Add --context admin@ai to this command and every subsequent cluster command. CLAUDE.md explicitly says the default context is home-lab, a different k3s cluster. The attach test is Verification step 3, not step 5. -->

<!-- codex: Do not restore 60s merely to "remove" the hand patch: the intended transition is to retain 600s under policy enforcement. Before enabling the operator, prove mutation with a server-side dry-run UPDATE carrying 60s. Define rollback: stop a churning operator, verify/restore the known-good controller args, and revert or repair the policy before resuming. Removing the policy while the operator runs can immediately restore the failing timeout. -->

### 3. Runbook + issue closure (ailab)

Add a section to `docs/runbooks/qnap-storage-setup.md`: the O(n²) sweep, the attacher timeout policy,
stale `tridenttransactions` wedging bootstrap (`CSI driver probe failed: Trident initialization
failed; … Resource was not found` → check PV/PVC/tridentvolume, then delete), and the
`Retain`-flip for orphan PVs pending NAS cleanup. Post the closure on ailab#865.

<!-- codex: "Check ... then delete" is unsafe as a general transaction-recovery instruction. Specify the exact transaction, operation, backend/LUN identity, evidence that no live operation owns it, and a saved CR before deletion; that bootstrap error alone does not establish staleness. Close the timeout incident only after attach/detach verification, keeping NAS cleanup and any driver performance fix linked as separate work. -->

### 4. CNPG required anti-affinity (platform repo, separate PR)

`deploy/components/cnpg-cluster/cluster.yaml` on `C:/Users/chifo/work/platform` (remote `gitea`):
replace header lines 15–17 ("Anti-affinity stays the CNPG DEFAULT `preferred` (NOT required…") with
one line pointing at the affinity block, and add after `primaryUpdateMethod: switchover`:

<!-- codex: Line 17 also contains "No off-cluster backup." Preserve that independent warning when replacing the affinity explanation, and establish the available restore path before a storage-dependent database roll. -->

```yaml
  affinity:
    enablePodAntiAffinity: true
    podAntiAffinityType: required
    topologyKey: kubernetes.io/hostname
```

<!-- codex: These fields express hostname anti-affinity, not control-plane placement. The inspected component has no CP nodeSelector. Check the rendered Cluster and actual pod selectors, tolerations, hostname labels, and volume topology to establish which nodes are eligible; one instance per CP follows only if those three CPs are the eligible destinations. -->

with the comment from the verified #1672 analysis (why `required` with 3 instances on 3 CP nodes,
what a drain does now: the evicted instance stays Pending until its node returns, 2/3 keep serving,
roll ONE CP at a time — which is already the rule in CLAUDE.md). CNPG 1.24.1 treats the affinity
change as a PodSpec diff and rolls once (replicas, then a switchover); each recreation
detaches/attaches two volumes, which is why step 1–2 must be live first.

<!-- codex: Add a scheduling gate before merging. This manifest requests 250m CPU and 1Gi memory per instance, so ~500m spare CPU does not by itself prove a stall. Check effective requests of the rendered pods, including init containers/overhead, memory, and competing reservations on each eligible destination. Confirm CNPG's delete/recreate ordering rather than assuming Deployment-style surge capacity. If capacity is insufficient, recycle sandboxes and verify node-specific headroom before this step; the current ordering otherwise makes a capacity prerequisite arrive too late. -->

<!-- codex: Hard anti-affinity can leave a replacement Pending when its only usable node is cordoned, unavailable, full, or still occupied by a matching pod. CNPG can then stop the roll while waiting for a healthy replica. Define a bounded stop condition and recovery procedure for a stuck replacement; reverting the Cluster field may not immediately repair an already-created Pending pod. Do not delete a second instance to unblock the first. -->

<!-- codex: Confirm the 1.24.1 affinity-change rollout behavior and monitor it explicitly. Same-node recreation need not cause a fresh ControllerPublish/Unpublish pair; test an actual cross-node move. Where moves do occur, two volumes and several serial 2–5 minute operations can exceed a ten-minute end-to-end budget. -->

<!-- codex: "2/3 keep serving" assumes the cluster was healthy and already spread, and primary eviction can require failover with a service interruption. Check CNPG PDBs and replication lag before a drain, then wait for all three database instances to recover before the next CP, in addition to etcd health. Async replication and the shared NAS still limit the availability guarantee. -->

Open the PR on Gitea with
the sign-off note for the documented-decision reversal (`strive.io/owner: data-eng`), let the
reviewbot review; if it stalls on the `E2E Tests / smoke*` required-check glob (deploy-only PRs get
no smoke status — see platform#1672), merge with `force_merge` at the reviewed head, and record the
switchover minute on the PR.

<!-- codex: Treat force_merge as a narrowly evidenced exception for this PR, not an automatic consequence of the earlier issue. Require the documented decision sign-off, successful applicable render/schema checks, and confirmation that only the known absent smoke status blocks the unchanged reviewed head; never bypass an actual failing check. -->

### 5. Sandbox recycle: codify the platform path, hand the credential step to the operator

Add `scripts/airlock-recycle-sandbox.sh` (bash + curl + jq; POSIX, LF): for one `app_id`, POST
`/api/airlock/v1/apps/<id>/teardown`, poll `/api/airlock/v1/app-operations/<op>` until terminal,
POST `/deploy`, poll again, then print the new pod's `requests.cpu/limits.cpu/qosClass` via
`kubectl --context admin@ai -n strive-sandboxes-ailab`.

<!-- codex: The inspected platform checkout at ed627012c contradicts this API contract: services/airlock/src/app/api/v1/endpoints/apps.py:631 returns AppOut from deploy, teardown at :648 returns 204, and api/v1/api.py registers no app-operations router. Verify the deployed image/OpenAPI before implementation. If it uses this synchronous contract, poll the app's sandbox state rather than inventing operation IDs; otherwise document the newer deployed contract and its version dependency. -->

<!-- codex: Define HTTP error handling and bounded request/poll deadlines. Advance only after successful teardown, not any terminal state; handle 401/403, conflicts, malformed responses, and interrupted or ambiguous POST results without blindly repeating destructive steps. An expired token after teardown can leave the app stopped: report a non-secret recovery phase and support resuming deployment after reauthorization. Confirm state persistence and application readiness before recycling the next sandbox. -->

Inputs: `AIRLOCK_BASE_URL`, `AIRLOCK_TOKEN`
(Bearer, from a tenant member's session — never stored, never echoed), the app id. Refuses to run
without a token; never deletes pods.

<!-- codex: An environment variable plus "never echoed" is not a sufficient secret-handling design. Exported tokens are inherited by child processes, curl -H with an expanded token exposes it in argv, and shell history/xtrace can capture it. Specify hidden prompt or inherited-FD input, pass the header through stdin/FD rather than argv, disable tracing/verbose credential output, and avoid persistent credential files or raw error dumps. Do not promise the environment-based design never exposes or stores the token. -->

<!-- codex: Bind AIRLOCK_BASE_URL to a trusted HTTPS ingress origin with certificate verification and no credential-forwarding redirects; an arbitrary destination can receive the bearer. This route relies on Gatekeeper authentication and path rewriting, so a raw Airlock service URL is not interchangeable. Preflight the authenticated app and compare its returned tenant_id with an explicit expected tenant before teardown: membership in some tenant or a nonempty token is insufficient, and app IDs may repeat across tenants. -->

The operator runs it three times (`ryan`, `billing-desk`,
`smart-demo`) with their own session; the script is the reusable, reviewable part. Expected result:
each pod `100m / 1 / Burstable`, control-plane requested CPU −2.7 cores.

<!-- codex: Verify the deployed Airlock build actually supplies the new resource defaults before the first teardown. Identify replacements by tenant/app labels and a changed pod UID, wait for Ready and an application check, and inspect every container's resources before asserting QoS. The 2.7-core reduction is aggregate; it does not guarantee capacity on the particular node needed by CNPG. -->

## Critical files

- `kubernetes/apps/infrastructure/storage/kyverno-trident-attacher-timeout.yaml` — new ClusterPolicy.
- `kubernetes/apps/infrastructure/storage/kustomization.yaml` — add the policy.
- `docs/runbooks/qnap-storage-setup.md` — operations section (timeout, sweep, transactions).
- `scripts/airlock-recycle-sandbox.sh` — the recycle wrapper.
- platform `deploy/components/cnpg-cluster/cluster.yaml` — affinity block + header comment (own PR).

## Verification

<!-- codex: Before rollout, render the storage Kustomization and validate/test the concrete policy against Kyverno 1.13.4. Fixtures should cover CREATE and UPDATE, reordered containers/args, 60s and already-600s input, absent/duplicate/split timeout flags, a missing sidecar, and nonmatching names/namespaces. Assert preservation of unrelated fields and identical output on a second mutation. -->

1. `kubectl --context admin@ai get clusterpolicy trident-attacher-timeout` → `READY True`; Flux
   `infrastructure` Kustomization reconciled at the merge sha.

<!-- codex: READY and Flux reconciliation do not prove mutation. While the operator remains stopped, submit a server-side dry-run UPDATE of the actual Deployment with the attacher timeout set to 60s and assert the returned value is 600s. The already-patched live Deployment would otherwise let a nonmatching policy pass all simple value checks. -->

2. `kubectl -n trident scale deploy trident-operator --replicas=1`; within 2 min:
   `kubectl -n trident get deploy trident-controller -o jsonpath='{.spec.template.spec.containers[?(@.name=="csi-attacher")].args}'`
   contains `--timeout=600s`; `kubectl -n trident get tridentorchestrator trident -o jsonpath='{.status.status}'`
   is `Installed`; the controller's `generation` does not keep incrementing (no fight).

<!-- codex: Use --context admin@ai throughout and inspect the running controller pod args as well as the Deployment template. Two minutes is only an initial check: observe multiple reconciliations and an operator restart, checking reconcile/error logs and request activity alongside stable Deployment/ReplicaSet/pod identities. Record a rollback trigger for repeated updates or replacement. -->

3. Attach test: a 1Gi `qnap-iscsi` PVC + busybox pod in a scratch namespace → pod Running within
   10 min, no `FailedAttachVolume DeadlineExceeded` events; delete both; PV gone within 10 min.

<!-- codex: Add a write/read check, then move the same PVC between two eligible nodes sequentially and verify data after detach/reattach. A single initial attach and deletion do not exercise the database migration path. Observe VolumeAttachment state and controller/attacher logs; use stage-specific deadlines because 600s is one RPC budget, not the combined provisioning/attach/detach/delete budget. Verify this scratch PV has Delete reclaim policy and track its exact backend volume cleanup, leaving the eight retained PVs untouched. -->

4. Affinity PR: after merge, `kubectl -n strive-ailab get pods -l cnpg.io/cluster=strive-pg -o wide`
   shows one instance per CP node, the rendered pod affinity has
   `requiredDuringSchedulingIgnoredDuringExecution` with `topologyKey: kubernetes.io/hostname`,
   cluster 3/3 healthy, two streaming replicas; switchover minute recorded on the PR.

<!-- codex: Make the capacity/storage-health checks a pre-merge gate because GitOps can start the unsupervised roll immediately. During rollout inspect Pending/Terminating pods and scheduler/attachment events before the next replacement; afterward verify all three pods' affinity selectors, replication lag, and application connectivity through the primary service. Three scheduled pods and a switchover timestamp alone do not establish database recovery. -->

5. Recycle script: `bash -n`, `shellcheck` clean, and a dry-run against a missing token exits non-zero
   without calling the API. Real run by the operator: the three sandbox pods report `100m / 1 /
   Burstable`, `kubectl describe node talos-cp{1,2}` show ~2.7 cores less requested.

<!-- codex: Syntax checks and missing-token handling miss the dangerous branches. Add mocked tests for the verified API contract, wrong tenant, expired credentials before/after teardown, teardown failure, deployment failure, timeout/interruption recovery, and secret-free argv/logs. Verify node-request deltas across the actual before/after placements with --context admin@ai; do not assume only cp1/cp2 changed or confuse aggregate requested CPU with observed usage. -->

<!-- codex-review-status: complete -->
