# Trident attach timeout (IaC), CNPG required anti-affinity, sandbox recycle path

## Context

2026-09-27, after the QNAP LUN-9 repair (ailab#865): the QNAP CSI driver (qnap-csi v1.6.0, Trident
fork) serves every ControllerPublish/Unpublish/Delete with a full `Qvolume List` + one `Get Volume`
per LUN (~2.5–3 s each; each is one NAS `disk_manage.cgi` call that walks all volume labels, so the
sweep is O(n²)). At 45 LUNs a sweep is ≈110–135 s, above the stock `csi-attacher --timeout=60s`, so
every attach and detach ends in `DeadlineExceeded` and retries re-queue sweeps until the controller
is saturated. Observed: a publish that ran 09:29:37→09:34:58Z, then `client rate limiter Wait
returned an error: context canceled`; two `strive-pg-9` detaches stuck for 3 h.

Imperative state left in the cluster today (the debt this plan retires):
- `deploy/trident-controller` container `csi-attacher` args `--timeout=60s` → `--timeout=600s`, by
  hand;
- `deploy/trident-operator` scaled to 0 replicas, because the operator (qnap-csi-operator v1.6.0,
  `TridentOrchestrator` exposes only `debug/namespace/tridentImage`; the chart hard-codes
  `replicas: 1` for the operator and the sidecar args live inside the operator image) reverted the
  patch within 30 s;
- 8 Released `testpool` PVs flipped to `Retain` (annotated `ailab.io/retain-reason`) to stop their
  delete loops — that part stays; their NAS cleanup is ailab#880.

Two related items from the same program: (a) `strive-pg`'s three instances had doubled up on
talos-cp3 since the 09-21 CP reboots because CNPG's default `preferred` anti-affinity is
IgnoredDuringExecution (verified analysis on platform#1672); the #1672 roll happened to re-spread them
one per CP (cp1/cp2/cp3 since 15:47Z) because there was headroom, not because of a rule — `required`
is also IgnoredDuringExecution, so it never moves a running pod either; what it changes is the
placement of every pod CNPG creates from now on (the roll it triggers, and every future drain/reboot
replacement). (b) The three `forever` sandboxes in `strive-sandboxes-ailab` each reserve a full
core (Guaranteed); platform#1644 (in the sweep build, airlock rolled 09:25:13Z) makes new sandboxes
request 100m, but pod resources are immutable, so they must be recycled through airlock's own
lifecycle (teardown → deploy), which requires a member of tenant `dbe45925-…` — the only members are
the two human accounts; `ops-admin@localhost` is a tenant admin of `…0001` and `_resolve_fleet_scope`
lets only `platform:support` act across tenants.

Every cluster command below is `kubectl --context admin@ai …` (the default context is a different
cluster; CLAUDE.md).

## Approach

### 1. Make the attacher timeout GitOps state: a Kyverno mutate policy (ailab)

**Where it lives.** Kyverno (v1.13.4) is delivered by the `platform-kyverno` Kustomization in
`kubernetes/apps/clusters/ai/platform.yaml`, which `dependsOn: infrastructure`. A ClusterPolicy in
`infrastructure/storage` would make the base layer depend on a CRD that is installed after it (a
cycle Flux would never resolve), so the policy gets its own tree and Kustomization:
`kubernetes/apps/storage-policies/{kustomization.yaml,trident-attacher-timeout.yaml}` +
`kubernetes/apps/clusters/ai/storage-policies.yaml` with `dependsOn: [{name: platform-kyverno}]`,
`wait: true`, `retryInterval: 1m` (the same CRD-ordering pattern as `qnap-storage.yaml`).

**Webhook coverage (checked live).** `autoUpdateWebhooks=true`; `kyverno-resource-mutating-webhook-cfg`
exists with 0 webhooks today because no mutate policy exists — Kyverno adds the Deployment rule when
the policy is created. The namespace selector excludes only `kube-system, kyverno, cert-manager,
external-secrets`; `trident` is covered. The policy sets `spec.failurePolicy: Ignore` on purpose: with
`Fail`, a Kyverno outage would block every update of `trident-controller`, including the operator's
own repair path; with `Ignore`, the worst case is one un-mutated write during an outage, which the
post-check in §3 (a `kubectl … get deploy … -o jsonpath` line in the runbook, and the same check in
the attach-latency alert's runbook) catches and the next operator write repairs.

**The rule (concrete, no fixed indices).** Outer `foreach` over
`request.object.spec.template.spec.containers` with `preconditions` `{{ element.name }} Equals
csi-attacher`; inner `foreach` over `element.args` with `preconditions`
`{{ starts_with(element, '--timeout=') }} Equals true` (a JMESPath function, not a regex);
`patchesJson6902` `replace /spec/template/spec/containers/{{elementIndex0}}/args/{{elementIndex1}}`
value `--timeout=600s`. Both loops iterate the ORIGINAL arrays, so the indices address the right
element; every other container and every non-matching argument is untouched by construction (a JSON
patch at one path, never a strategic merge of the list). Defined behaviour: missing `args`, a missing
`--timeout=` flag, or the two-token form `--timeout 60s` → the rule matches nothing and is a no-op
(documented; the §3 post-check exists for exactly this); a duplicate flag → every occurrence is set
to 600s; an already-600s value → identical output (idempotent). If Kyverno 1.13.4 rejects
`preconditions` on the inner loop, fall back to a single outer loop with a `patchStrategicMerge`
that anchors `(name): csi-attacher` and `args: [ "--timeout=600s" ]` is NOT acceptable (it replaces
the list); instead use the inner loop without preconditions and a JMESPath value expression
`{{ starts_with(element, '--timeout=') && '--timeout=600s' || element }}` — either shape is proven
by the tests below before it reaches the cluster.

**Why 600 s, and its limits.** The sweep cost is inside QNAP's `storage-api-server`; only 3 of the
45 LUNs are removable orphans (LUNs 4, 43, 44), so the count cannot drop under the ~20 that a 60 s
budget needs. 600 s is a mitigation, not a demonstrated bound: it is one RPC budget (publish or
unpublish), not the end-to-end pod-start budget, and it does not touch the provisioner (600 s already),
resizer or snapshotter (300 s) deadlines. Acceptance: attach and detach p95 under 5 min at ≤50 LUNs
with no `DeadlineExceeded` retries in `csi-attacher` logs over 24 h; the policy is removed when a
qnap-csi release exposes the sidecar timeout or fixes the per-LUN sweep (tracked in the runbook).

**Admission-only, and what triggers it.** No `mutateExistingOnPolicyUpdate`/`targets`, so the
background controller needs no new write grant (the helmtest policy header warns against widening
Kyverno's RBAC); `background: false` only disables scanning. Installing the policy does not touch
the live Deployment. The trigger is any UPDATE of the Deployment that passes admission: the operator
issues one on restart (observed today: it rewrote the args within 30 s of the hand patch), and the
proof step (§2) issues a server-side dry-run UPDATE first. Fallback trigger if the operator does not
write: a harmless metadata annotation update on the Deployment goes through the same webhook with
the full object and is mutated the same way.

**Operator interaction is verified, not assumed.** The operator's write carries 60s; admission turns
it into 600s, which equals the stored object, so the API server persists nothing and no ReplicaSet
rolls. Whether qnap-csi-operator v1.6.0 then keeps issuing updates (comparing its 60s intent with the
600s it reads back) is unknown — its reconcile loop is in the image, not in the chart. §2 observes
Deployment `generation`, `metadata.resourceVersion`, ReplicaSet and pod UIDs, and the operator's log
volume over ≥3 cycles and one operator restart. Rollback trigger: resourceVersion churn on
`trident-controller` or a fresh ReplicaSet → scale the operator to 0 again, confirm args are 600s,
and fix the policy before resuming; never delete the policy while the operator runs (that restores
60s within one reconcile).

### 2. Prove the mutation, then restore the operator (ailab, operational)

Order matters because the live Deployment is already at 600s, so simple value checks cannot tell a
working policy from a dead one:
1. Flux: `storage-policies` Kustomization Ready; `get clusterpolicy trident-attacher-timeout` READY.
2. Proof with the operator still at 0: fetch the Deployment, set the attacher arg to `--timeout=60s`
   in the copy, `kubectl replace --dry-run=server -o json` it, and assert the returned object carries
   `--timeout=600s`. Also assert the dry-run of the UNCHANGED object returns byte-identical args.
3. `scale deploy trident-operator --replicas=1`. The operator re-applies on start. Within 2 min: the
   Deployment template and the running controller pod both show `--timeout=600s`,
   `tridentorchestrator/trident` `.status.status == Installed`. Then watch 15 min: no new ReplicaSet,
   pod UID stable, resourceVersion stable, operator log free of repeating update errors. Then
   `rollout restart deploy/trident-operator` once and repeat the check (an operator restart is the
   realistic future event).
4. Attach test (Verification 3). Only after that: close the timeout incident on ailab#865 (the NAS
   orphan cleanup #880 and the driver performance question stay open, linked).

### 3. Runbook (ailab)

`docs/runbooks/qnap-storage-setup.md` gets an operations section: the O(n²) sweep and the 600s
policy (with the post-check one-liner and the removal condition); stale `tridenttransactions` —
precisely: the bootstrap error names ONE transaction; a transaction is stale only if `kubectl -n
trident get tridenttransaction <name> -o yaml` shows an `addVolume` whose volume has no PV, no PVC,
no `tridentvolume`, and `qcli_iscsi -l` on the NAS shows no LUN of that name; save the CR
(`-o yaml > _out/`) before deleting; a transaction with a live PV/PVC/tridentvolume is NOT stale and
must be left to Trident; the `Retain` flip for orphan PVs pending NAS cleanup (#880); the
airlock-recycle wrapper.

### 4. CNPG required anti-affinity (platform repo, separate PR)

`deploy/components/cnpg-cluster/cluster.yaml` on `C:/Users/chifo/work/platform` (remote `gitea`):
replace the anti-affinity sentence in header lines 15–17 with one line pointing at the affinity
block, **keeping "No off-cluster backup." and "Bootstrap is from-scratch initdb" intact** (they are
independent warnings), and add after `primaryUpdateMethod: switchover`:

```yaml
  affinity:
    enablePodAntiAffinity: true
    podAntiAffinityType: required
    topologyKey: kubernetes.io/hostname
```

Eligible nodes (checked live 2026-09-27): the Cluster sets no nodeSelector/tolerations; the agent
nodes carry `dedicated=agent:NoSchedule`, the env node `dedicated=env:NoSchedule`, so only
talos-cp1/2/3 are schedulable for the instances; the 6 PVs carry no `nodeAffinity` (Immediate
binding, iSCSI reachable from every CP). So `required` on `kubernetes.io/hostname` with 3 instances
means exactly one per CP.

**Pre-merge gate (GitOps starts the unsupervised roll at once).** The instances are already one per
node, so each recreated pod's only eligible node is its current node; it needs that node to keep
≥500m CPU and ≥2Gi memory free after its old pod is gone (the pod's own requests are released when
it terminates; CNPG deletes then recreates, no surge). Gate: for each CP, `Allocatable − (requested
by everything except that instance) ≥ 500m / 2Gi`, plus PDBs allow 1 disruption, replication lag < 1 s,
Trident policy live (§2 done), no cordoned CP. If any node fails the gate, do the sandbox recycle (§5)
first and re-check.

**During the roll (CNPG 1.24.1: affinity is a PodSpec diff → one rolling update: replicas first, then
a switchover of the primary):** watch `kubectl … get pods -l cnpg.io/cluster=strive-pg -o wide -w`,
`get events --field-selector reason=FailedScheduling|FailedAttachVolume`, and `get volumeattachments`.
Same-node recreation may or may not produce a fresh unpublish/publish pair; either way each RPC has the
600s budget, and a full instance replacement is expected to take 1–8 min. **Stop condition:** a
replacement Pending > 10 min → free CPU/memory on ITS node (scale a non-critical workload there; the
sandboxes and the trueswarm right-sizing are the known levers), never delete a second instance and
never flip the affinity back mid-roll (the Pending pod already carries `required`). After the roll:
three pods with `requiredDuringSchedulingIgnoredDuringExecution` + `topologyKey:
kubernetes.io/hostname` in their rendered affinity, one per CP, cluster 3/3 healthy, two `streaming`
replicas with lag < 1 s, an application round-trip through `strive-pg-rw` (a `SELECT 1` from a
platform pod via the service DNS name), switchover minute recorded on the PR.

**What a CP drain means now:** the evicted replica stays Pending until its node returns; a drained
primary is switched over first (brief write interruption); 2/3 serve only if the cluster was healthy
and spread before the drain — so the node-maintenance runbook's pre-checks gain "CNPG 3/3 healthy,
PDBs 1 allowed, lag < 1 s" and its post-checks "all three instances back and streaming" before the
next CP, alongside etcd 3/3.

PR mechanics: open on Gitea with the explicit sign-off line for reversing the documented decision
(`strive.io/owner: data-eng`, the operator approves on the PR), let the reviewbot review. `force_merge`
only if the reviewed head is unchanged, both reviewer verdicts are clean, every check that ran is
green and the only thing blocking is the absent `E2E Tests / smoke*` status of a deploy-only PR
(platform#1672 precedent); a failing check is never bypassed.

### 5. Sandbox recycle: codify the platform path, hand the credential step to the operator

**Contract (verified on the deployed build).** The running airlock is the sweep build (`0455699f2`,
pod created 09:25:13Z) with `APP__AIRLOCK__APP_ASYNC_LIFECYCLE_ENABLED=true`; on `gitea/main`,
`services/airlock/src/app/api/v1/endpoints/apps.py` returns **202 + `AppOperationOut`** from
`POST /apps/{app_id}/deploy` (L1152) and `POST /apps/{app_id}/teardown` (L1195) in that mode (204/
`AppOut` when the flag is off), and `api/v1/api.py:41` mounts `GET /app-operations/{id}` and
`/app-operations/latest` (`app_operations.py`, since d92d526fb 2026-08-15). The codex worktree read an
older platform checkout (`ed627012c`) that predates this. The script handles both contracts: 202 →
poll the operation until `succeeded` (any other terminal status is a failure, stop); 200/204 → poll
`GET /apps/{app_id}` until `status` is `STOPPED` (after teardown) / `ACTIVE` with a sandbox
(after deploy).

`scripts/airlock-recycle-sandbox.sh` (bash, LF; curl + jq):
- Inputs: `--app <id>`, `--tenant <uuid>` (required; the app's returned `tenant_id` must equal it, else
  abort before any write), `--base-url` fixed default `https://<platform edge host>` (must be
  `https://`, certificate verification on, `--max-redirs 0`, so no redirect can forward the bearer);
  a raw in-cluster airlock URL is refused (it bypasses Gatekeeper and its `X-Gatekeeper-Tenant`).
- Token: never an argument, never an exported env var, never a file. Read from a hidden prompt
  (`read -rs`) or `--token-fd N`; passed to curl through `--config` on stdin (`header = "Authorization:
  Bearer …"`), so it is in no argv, no history, no log; `set +x` enforced; error bodies are printed
  with the `Authorization` line stripped; the token variable is unset on exit.
- Flow per app: preflight `GET /apps/{id}` (auth, tenant, current status, sandbox present) →
  `POST teardown` → poll (bounded: 15 min, 10 s interval) → **only on success** `POST deploy` →
  poll → wait for the new pod (`kubectl --context admin@ai -n strive-sandboxes-ailab get pod -l
  airlock.strive.io/app-id=<id>`, new UID, Ready) → print every container's requests/limits and the
  pod QoS. Phases are printed as `PHASE=<name>` lines; a failure after teardown exits with a distinct
  code and prints the resume command (`--resume-deploy`) so an expired token does not leave the app
  stopped without a documented way back. 401/403 → stop, no retry; a POST that times out is NOT
  repeated automatically (the operation may have started) — the script re-reads app status and asks.
- Tests: `bash -n`; shellcheck (via `pip install shellcheck-py` into the job tmp, or skip with a
  note if unavailable); `tests/airlock-recycle-mock.py` — a stdlib `http.server` mock of the four
  endpoints that drives the script through: happy path (202 contract), 204 contract, wrong tenant
  (must not POST), 401 before teardown, 401 after teardown (resume path), teardown operation failed
  (must not deploy), deploy timeout, and an assertion that the mock never sees the token in a query
  string and that the process argv/env never contain it.
- Operator step: run it three times (`ryan`, `billing-desk`, `smart-demo`) with your own session
  token, one at a time; the expected result per pod is `100m / 1 / Burstable` (verify the deployed
  build sets those defaults first: `GET /apps/{id}` after deploy shows the sandbox, the pod's
  container resources show 100m). Aggregate effect −2.7 cores of requests, but it lands on whichever
  CPs the new pods schedule to; the §4 gate checks the specific nodes.

## Critical files

- `kubernetes/apps/storage-policies/kustomization.yaml`, `…/trident-attacher-timeout.yaml` — new tree + ClusterPolicy.
- `kubernetes/apps/clusters/ai/storage-policies.yaml` — Flux Kustomization, `dependsOn platform-kyverno`.
- `docs/runbooks/qnap-storage-setup.md` — operations section; `docs/runbooks/node-maintenance.md` — CNPG pre/post checks.
- `scripts/airlock-recycle-sandbox.sh`, `scripts/tests/airlock-recycle-mock.py` — the wrapper and its mock tests.
- platform `deploy/components/cnpg-cluster/cluster.yaml` — affinity block + header comment (own PR).

## Verification

0. Policy fixtures before merge: download the Kyverno CLI (v1.13.x) into the job tmp and run
   `kyverno apply` against fixture Deployments: CREATE and UPDATE, containers reordered, args
   reordered, `60s` input, already-`600s` input (identical output), duplicate flag (both replaced),
   absent flag and split `--timeout 60s` (no-op), sidecar renamed (no-op), a Deployment of another
   name/namespace (unmatched). Assert all other containers/args are byte-identical. The policy's
   match stays exact (`trident/trident-controller`), so the fixtures can only run offline with the
   CLI; the live server-side dry-run of the real Deployment (§2 step 2) is the integration proof. If
   the CLI cannot run on this workstation, run it on a dev-worker (Linux) — the fixtures are plain
   files.
1. Flux `storage-policies` Ready at the merge sha; `get clusterpolicy trident-attacher-timeout` READY;
   `get mutatingwebhookconfiguration kyverno-resource-mutating-webhook-cfg` now lists a Deployment rule.
2. §2 steps 2–3 exactly, with the operator observation window (15 min + one operator restart) and the
   rollback trigger recorded in the runbook.
3. Attach test in a scratch namespace: 1Gi `qnap-iscsi` PVC (reclaim Delete) + a busybox pod that
   writes a marker file; pod Running (stage deadlines: PVC Bound ≤ 10 min, attach ≤ 10 min); then
   delete the pod and recreate it pinned to a DIFFERENT CP (`nodeName`), verify the marker survives
   (real detach → publish on another node, the database-migration path); watch `volumeattachments`
   and `csi-attacher` logs for zero `DeadlineExceeded`; delete pod + PVC; PV gone within 10 min and
   the LUN gone from `qcli_iscsi -l` (the eight `Retain` PVs untouched).
4. Affinity PR: the §4 pre-merge gate output pasted on the PR; during the roll the watch commands
   above; afterwards the rendered affinity on all three pods, 3/3 healthy, two `streaming` replicas
   with lag < 1 s, `SELECT 1` through `strive-pg-rw` from a platform pod, switchover minute on the PR.
5. Recycle script: the mock test suite green; operator run: three new pod UIDs, each container
   `100m / 1`, QoS `Burstable`, `GET /apps/{id}` ACTIVE with a sandbox; per-node requested CPU before/
   after captured from `describe node talos-cp{1,2,3}` (the pods may land on different nodes than
   before, so compare the sum and each node).

<!-- codex-review-status: complete -->
