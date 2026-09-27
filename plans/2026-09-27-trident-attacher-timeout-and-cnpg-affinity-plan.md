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

## Approach

### 1. Make the attacher timeout GitOps state: a Kyverno mutate policy (ailab)

Kyverno 1.13.4 is installed cluster-wide and ailab already ships a ClusterPolicy through Flux
(`kubernetes/apps/infrastructure/helmtest/kyverno-protect-reserved.yaml`). Add
`kubernetes/apps/infrastructure/storage/kyverno-trident-attacher-timeout.yaml` (listed in the storage
`kustomization.yaml`, next to `iscsi-recovery-tmo.yaml`, which fixes the driver's other timeout bug):

- `ClusterPolicy trident-attacher-timeout`, `background: false`, admission-time mutate on
  CREATE/UPDATE of `Deployment` `trident-controller` in namespace `trident`.
- `foreach` over `request.object.spec.template.spec.containers` with precondition
  `{{element.name}} == csi-attacher`, nested `foreach` over `element.args` replacing the element that
  matches `^--timeout=` with `--timeout=600s` (same value as the provisioner sidecar; a detach uses
  the same flag). No fixed indices, so an operator upgrade that reorders containers/args still gets
  mutated; if the sidecar is renamed the policy is a no-op and the verification step catches it.
- Why 600 s and not "fix the sweep": the sweep cost is inside QNAP's `storage-api-server`; only 3 of
  the 45 LUNs are removable orphans (LUNs 4, 43, 44; the rest are live or intentionally retained), so
  the count cannot drop under the ~20 that a 60 s budget would need.
- Why admission-only (no `mutateExistingOnPolicyUpdate`): it needs no extra RBAC for the background
  controller (the helmtest policy's header warns against widening Kyverno's grants), and the operator
  re-applies the Deployment on every start, so restoring the operator is the trigger.
- Operator interaction: the operator's re-apply is mutated at admission to the same object it already
  is, so the API server records no change and no rollout happens; the operator does not fight a
  no-op. Verified in the verification step, not assumed.

### 2. Restore the operator and remove the hand patch (ailab, operational)

After Flux reports the policy Ready: `kubectl -n trident scale deploy trident-operator --replicas=1`
(that is the chart's declared state, so this is un-doing the imperative drift, not new drift). Watch
`deploy/trident-controller` args stay `--timeout=600s`, `tridentorchestrator/trident` status
`Installed`, and run a real attach test (step 5).

### 3. Runbook + issue closure (ailab)

Add a section to `docs/runbooks/qnap-storage-setup.md`: the O(n²) sweep, the attacher timeout policy,
stale `tridenttransactions` wedging bootstrap (`CSI driver probe failed: Trident initialization
failed; … Resource was not found` → check PV/PVC/tridentvolume, then delete), and the
`Retain`-flip for orphan PVs pending NAS cleanup. Post the closure on ailab#865.

### 4. CNPG required anti-affinity (platform repo, separate PR)

`deploy/components/cnpg-cluster/cluster.yaml` on `C:/Users/chifo/work/platform` (remote `gitea`):
replace header lines 15–17 ("Anti-affinity stays the CNPG DEFAULT `preferred` (NOT required…") with
one line pointing at the affinity block, and add after `primaryUpdateMethod: switchover`:

```yaml
  affinity:
    enablePodAntiAffinity: true
    podAntiAffinityType: required
    topologyKey: kubernetes.io/hostname
```

with the comment from the verified #1672 analysis (why `required` with 3 instances on 3 CP nodes,
what a drain does now: the evicted instance stays Pending until its node returns, 2/3 keep serving,
roll ONE CP at a time — which is already the rule in CLAUDE.md). CNPG 1.24.1 treats the affinity
change as a PodSpec diff and rolls once (replicas, then a switchover); each recreation
detaches/attaches two volumes, which is why step 1–2 must be live first. Open the PR on Gitea with
the sign-off note for the documented-decision reversal (`strive.io/owner: data-eng`), let the
reviewbot review; if it stalls on the `E2E Tests / smoke*` required-check glob (deploy-only PRs get
no smoke status — see platform#1672), merge with `force_merge` at the reviewed head, and record the
switchover minute on the PR.

### 5. Sandbox recycle: codify the platform path, hand the credential step to the operator

Add `scripts/airlock-recycle-sandbox.sh` (bash + curl + jq; POSIX, LF): for one `app_id`, POST
`/api/airlock/v1/apps/<id>/teardown`, poll `/api/airlock/v1/app-operations/<op>` until terminal,
POST `/deploy`, poll again, then print the new pod's `requests.cpu/limits.cpu/qosClass` via
`kubectl --context admin@ai -n strive-sandboxes-ailab`. Inputs: `AIRLOCK_BASE_URL`, `AIRLOCK_TOKEN`
(Bearer, from a tenant member's session — never stored, never echoed), the app id. Refuses to run
without a token; never deletes pods. The operator runs it three times (`ryan`, `billing-desk`,
`smart-demo`) with their own session; the script is the reusable, reviewable part. Expected result:
each pod `100m / 1 / Burstable`, control-plane requested CPU −2.7 cores.

## Critical files

- `kubernetes/apps/infrastructure/storage/kyverno-trident-attacher-timeout.yaml` — new ClusterPolicy.
- `kubernetes/apps/infrastructure/storage/kustomization.yaml` — add the policy.
- `docs/runbooks/qnap-storage-setup.md` — operations section (timeout, sweep, transactions).
- `scripts/airlock-recycle-sandbox.sh` — the recycle wrapper.
- platform `deploy/components/cnpg-cluster/cluster.yaml` — affinity block + header comment (own PR).

## Verification

1. `kubectl --context admin@ai get clusterpolicy trident-attacher-timeout` → `READY True`; Flux
   `infrastructure` Kustomization reconciled at the merge sha.
2. `kubectl -n trident scale deploy trident-operator --replicas=1`; within 2 min:
   `kubectl -n trident get deploy trident-controller -o jsonpath='{.spec.template.spec.containers[?(@.name=="csi-attacher")].args}'`
   contains `--timeout=600s`; `kubectl -n trident get tridentorchestrator trident -o jsonpath='{.status.status}'`
   is `Installed`; the controller's `generation` does not keep incrementing (no fight).
3. Attach test: a 1Gi `qnap-iscsi` PVC + busybox pod in a scratch namespace → pod Running within
   10 min, no `FailedAttachVolume DeadlineExceeded` events; delete both; PV gone within 10 min.
4. Affinity PR: after merge, `kubectl -n strive-ailab get pods -l cnpg.io/cluster=strive-pg -o wide`
   shows one instance per CP node, the rendered pod affinity has
   `requiredDuringSchedulingIgnoredDuringExecution` with `topologyKey: kubernetes.io/hostname`,
   cluster 3/3 healthy, two streaming replicas; switchover minute recorded on the PR.
5. Recycle script: `bash -n`, `shellcheck` clean, and a dry-run against a missing token exits non-zero
   without calling the API. Real run by the operator: the three sandbox pods report `100m / 1 /
   Burstable`, `kubectl describe node talos-cp{1,2}` show ~2.7 cores less requested.

<!-- codex-review-status: pending -->
