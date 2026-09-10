# Implementation review — openbao-agent-creds-and-helm-testns — round 1

<!-- codex-impl-review-status: pending -->

## Findings

Codex returned **no defects**. All five areas it was asked to concentrate on came back clean, each
recorded below with the reasoning it gave. Its five entries are reproduced rather than summarised,
because "we looked here and found nothing" is the useful part of a clean review.

### Marker re-read ordering in openbao.yml

**Location:** `ansible/roles/dev_worker/tasks/openbao.yml:266-272`
**Severity:** nit — no issue found

> The "Re-read kubeconfig ownership" task sets `dev_worker_openbao_renders_kubeconfigs: true` only on
> a successful preflight, and updates the in-memory fact for subsequent tasks in the same play. The
> ordering is correct. The task name is mildly misleading — it is a forward update, not a re-read —
> but that does not affect behaviour.

### Kubeconfig validation is consistent across both implementations

**Location:** `ansible/roles/dev_worker/tasks/openbao.yml:224-235`
**Severity:** nit — no issue found

> The preflight Python validation matches the sync script's generation/validation logic precisely:
> current-context, namespace, cluster name, server address, CA format, token extraction, JWT expiry.
> JWT padding is handled correctly (`p += "=" * (-len(p) % 4)`).

### Seed validation grep anchors field names correctly

**Location:** `kubernetes/apps/infrastructure/security/openbao/estate-provision-job.yaml` (PASS 1)
**Severity:** nit — no issue found

> The leading `\"` anchors to a literal quote, so `client_id` cannot match inside `gcloud_client_id`.
> `[^\"]` requires at least one non-quote character, correctly rejecting empty strings. Works across
> both the pretty-printed and single-line seed shapes.

### Kyverno policy is correctly configured for 1.13.4

**Location:** `kubernetes/apps/infrastructure/helmtest/kyverno-protect-reserved.yaml:50-56`
**Severity:** nit — no issue found

> `helmtest-dw?` covers dw1–dw9, so all six workers. `deny: {}` with `failureAction: Enforce` denies
> matching requests. Flux controller exclusions are scoped to the two service accounts that manage
> these objects.

### CAS=0 create fallback handles the race correctly

**Location:** `kubernetes/apps/infrastructure/security/openbao/k8stoken-sync.yaml:372-387`
**Severity:** nit — no issue found

> Create-only CAS with a PATCH fallback on 400/409 correctly handles the race with
> `devworker-provision-job`; any other status exits. The "mint everything before writing anything"
> ordering prevents partial updates, and both fields per worker go in one patch, so there is no
> window for a fresh helmtest alongside a stale tep.

## Independent verification of the review's load-bearing claim

A clean review after two plan rounds that produced 66 findings warrants a check rather than
acceptance, so the one mechanical assertion the design leans on was verified directly against the
live CRD rather than taken on the reviewer's word:

```
kubectl --context admin@ai get crd clusterpolicies.kyverno.io -o json
  validate keys:  [... deny, failureAction, ...]
  deny type: object | required: (none) | deny props: ['conditions']
  failureAction present on rule.validate: True
```

So `deny: {}` is schema-valid with `conditions` optional — an omitted conditions block denies
unconditionally — and `failureAction` is a valid **per-rule** key in 1.13.4, which is the form used.
Both hold.

## What is NOT covered by this review

A clean review means "no defect was found by reading", not "this works". Three properties are
verified by reasoning and schema only, and are verified by *execution* nowhere. Each is already a
step in the plan's verification section, and each must be done before the rollout proceeds past the
first worker:

1. **The Kyverno policy has never actually denied anything.** It passed server-side dry-run, which
   proves the schema is valid — not that a `DELETE` of `serviceaccount/helmtest-dw1` by the worker
   credential is refused. Until that negative test runs, "the credential cannot destroy its own
   identity" is a design claim. Test: attempt the delete with the worker kubeconfig; it must fail
   with the policy's message, and the same delete of a chart-created SA must succeed.
2. **No sync failure path has been exercised against a real OpenBao.** The `cas=0` conflict fallback
   and the "probe returned neither 200 nor 404 → abort" branch are reasoned, not run. Test: an
   unpopulated ServiceAccount, a sealed vault, and a concurrent `openbao-devworker-provision` run —
   in each case existing KV fields must survive unchanged and no token may reach the logs.
3. **The ansible cutover has never run.** Template rendering is verified in both states (2 stanzas
   pre-cutover, 6 post), but the gate, the `flush_handlers` placement, and the block/rescue rollback
   are untested because ansible does not run from the Windows workstation. Test: the deliberate
   failure rehearsal on dev-worker-1 — delete a KV field, confirm the agent exits and systemd holds
   it, restore, confirm unattended recovery.

Nothing has been applied to the cluster and no workstation file has been deleted, so all three
remain open.

## Diff stat

```
 ansible/roles/dev_worker/defaults/main.yml         |  23 +
 ansible/roles/dev_worker/tasks/k8s_tools.yml       |  46 +-
 ansible/roles/dev_worker/tasks/openbao.yml         | 211 ++++++++
 ansible/roles/dev_worker/tasks/tep.yml             |  14 +
 .../templates/helmtest-kubeconfig.ctmpl.j2         |   4 +
 .../dev_worker/templates/openbao-agent.hcl.j2      |  31 ++
 .../dev_worker/templates/tep-kubeconfig.ctmpl.j2   |  11 +
 .../0020-dev-worker-openbao-credentials.md         |  10 +
 .../0021-agent-credential-plane-and-helm-testns.md | 185 +++++++
 docs/runbooks/helmtest.md                          | 170 +++++++
 docs/runbooks/openbao-dev-workers.md               |  89 +-
 docs/runbooks/openbao-estate-credentials.md        |  92 ++++
 docs/runbooks/openbao-recovery.md                  |   3 +-
 kubernetes/apps/clusters/ai/helmtest.yaml          |  26 +
 .../helmtest/hack/smoke-chart/Chart.yaml           |  10 +
 .../hack/smoke-chart/templates/deployment.yaml     |  67 +++
 .../hack/smoke-chart/templates/service.yaml        |  16 +
 .../templates/tests/test-connection.yaml           |  59 +++
 .../helmtest/hack/smoke-chart/values.yaml          |  18 +
 .../helmtest/hack/values-restricted.yaml           |  69 +++
 .../infrastructure/helmtest/kustomization.yaml     |  10 +
 .../helmtest/kyverno-protect-reserved.yaml         | 102 ++++
 .../apps/infrastructure/helmtest/namespaces.yaml   | 360 +++++++++++++
 .../infrastructure/helmtest/networkpolicy.yaml     | 292 +++++++++++
 kubernetes/apps/infrastructure/helmtest/rbac.yaml  | 335 ++++++++++++
 .../security/openbao/devworker-provision-job.yaml  |  46 +-
 .../security/openbao/estate-provision-job.yaml     |  44 +-
 .../security/openbao/estate-seeds.sops.yaml        |  47 +-
 .../security/openbao/k8stoken-sync.yaml            | 560 +++++++++++++++++++++
 .../security/openbao/kustomization.yaml            |   6 +
 kubernetes/apps/infrastructure/testpool/README.md  |  10 +
 31 files changed, 2915 insertions(+), 51 deletions(-)
```
