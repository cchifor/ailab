# Agent credentials in OpenBao + Helm-capable isolated test namespaces

## Context

Two gaps found in the 2026-09-10 credential audit, with one shared root cause.

**Gap 1 — credentials that agents need are not in OpenBao.** The `bao agent` on each dev-worker
exposes exactly two fields (`af/dev-workers/common`: `gitea_pat`, `proxmox_ssh_key`). Everything
else an agent might need is distributed by a *second*, parallel channel — ansible + SOPS — or sits
in plaintext on the operator workstation. Concretely, verified on disk:

- `ansible/secrets/tep-tokens.sops.yaml` → rendered to `~/.tep/kubeconfig` by
  `roles/dev_worker/tasks/tep.yml`. A k8s bearer token with a second home, rotated by a hand-run
  script (`scripts/tep-render-kubeconfigs.py`) plus a `just dev-workers` run.
- Candidate workstation-only credentials: `~/work/keys/talos-backup-age.key`,
  `nextcloud-restic-password.txt`, `rclone-crypt-escrow.txt`, `platform.env` (Hatchet keysets,
  SendGrid, OpenAI + Anthropic API keys), `~/work/keys.txt` (gcloud + GitHub OAuth). **"Only" is
  unproven for several of these** — Phase 4 opens with a reconciliation step, because
  `kubernetes/apps/backup/backup-offsite/rclone-config.sops.yaml`,
  `kubernetes/apps/apps/ai/litellm-cloud-keys.sops.yaml` and the offline
  `kubernetes/infra/_out/talos-backup-age.key` (cited by `apps/backup/talos-backup/cronjob.yaml`)
  all exist and may already hold the same values.
- Duplicated key material outside any git repo, mode 0644: `~/work/keys/kubeconfig.txt` is a
  byte-identical copy of the `admin@ai` **cluster-admin** kubeconfig (sha256 `eb9213e4…`), and
  `~/work/keys/age.agekey` is a byte-identical copy of the SOPS master key (sha256 `0274ff70…`).

**Gap 2 — no kubeconfig can run a Helm deployment.** Verified live against the cluster with
`kubectl auth can-i --as=system:serviceaccount:testpool:tep-dw1`: `create secrets` → **no**,
so `helm install` fails at release-state creation before touching a chart. Role `tep-worker` grants
sandboxclaim CRUD, read-only pods/sandboxes, and exec/attach/portforward — nothing else. The
documented escalation hook (`claude-grant-write` → `/etc/claude-agent/kube-rw-config`) is a dead
end on these hosts: nothing in this repo provisions that file; it came from the *homelab* repo's
`platform/main.tf` for claude-worker VMs.

The shared root cause is that `testpool` is a *lease* namespace, not a *deploy* namespace, and
there is no third thing. This plan adds the deploy namespace and puts its credential — plus the
existing tep credential — into OpenBao, retiring the SOPS side-channel.

### The tiering constraint that shapes everything below

"Put the credentials in OpenBao so agents can read them" cannot be applied uniformly. Three tiers,
and the boundary is load-bearing:

- **Tier A — agent-readable** (`af/dev-workers/*`, read by the per-worker AppRole via `cred`).
  Namespace-scoped k8s tokens, Gitea PATs.
  **This tier gets wider in this plan and that must be said plainly.** A worker today can lease and
  exec into sandboxes; after Phase 1 it can also *deploy* into a namespace. What it does **not**
  gain is reach into another worker's namespace, any estate credential, or anything cluster-scoped.
  Note also that per-worker Helm namespaces do **not** by themselves make the six workers
  independent end to end: `af/dev-workers/common` still holds one shared `gitea_pat` and one shared
  `proxmox_ssh_key` (root on three hypervisors, unattributable across the six), and every tep token
  reaches every pod in the shared `testpool`. Those are pre-existing and out of scope here; ADR 0020's
  per-worker-PAT follow-up remains the fix.
- **Tier B — operator escrow only** (`af/estate/*`; no vault policy grants read to any AppRole or
  ESO). **The vault policy is not the whole boundary.** Flux decrypts `estate-seeds.sops.yaml` into
  a live `openbao-estate-seeds` Secret in ns `openbao`, so anyone who can read Secrets in that
  namespace — or schedule a workload that mounts one — holds every Tier B value regardless of what
  the vault policy says. The real Tier B boundary is *the union of* the vault policy, Secret-read
  RBAC in `openbao`, and the root-capable vault logins (breakglass token, the undocumented
  `auth/userpass` `root` user the estate runbook already flags). Phase 4 states this and adds an
  audit of who currently holds Secret read in `openbao`; it does not attempt to fix it.
- **Tier C — must never enter the vault at all.** The SOPS age master key, the unseal key, the
  breakglass token. The age key in particular is chicken-and-egg: it decrypts the seed files that
  populate the vault *and* `ansible/secrets/dev-worker.sops.yaml`, which holds every worker's
  AppRole `secret_id`.

**Disposition of the `admin@ai` cluster-admin kubeconfig:** Tier B, never Tier A — putting it where
a dev-worker AppRole can read it would convert any single-worker compromise into full cluster-admin.
It is **not escrowed to `af/estate/*` either**, and that is deliberate: it is reproducible from
Talos (`talosctl kubeconfig`) given the Talos secrets bundle, so escrowing it would add a standing
copy of cluster-admin without adding recoverability. Phase 5 therefore deletes the redundant copy
and tightens the mode on the canonical one; the recovery path is "regenerate from Talos", recorded
in `docs/runbooks/openbao-recovery.md`.

## Approach

Five phases. Phases 1–3 are independent of 4–5 and can land as separate PRs.

### Phase 1 — `helmtest-dw<N>`: six isolated, Helm-capable namespaces

New tree `kubernetes/apps/infrastructure/helmtest/`, one namespace per worker so concurrent agents
cannot collide on release names and a runaway chart is contained to one worker's quota.

Per namespace `helmtest-dw1` … `helmtest-dw6`:

**Namespace** with Pod Security Admission enforced at `restricted`
(`pod-security.kubernetes.io/enforce: restricted`, `enforce-version: v1.30`, plus `warn`/`audit`).
`create pods` in a namespace without PSA is a node-compromise primitive (hostPath, privileged,
hostNetwork), so this is the control that makes the whole phase safe.

`restricted` rejects far more than privileged pods: any container — **init containers and Helm hook
pods included** — that omits `runAsNonRoot: true`, `allowPrivilegeEscalation: false`,
`capabilities.drop: ["ALL"]`, or `seccompProfile.type: RuntimeDefault` is refused. So this phase
ships a **supported-chart profile**, not just a label:

- `docs/runbooks/helmtest.md` documents the required `values.yaml` shape and the four settings every
  container needs, with a worked example for a Bitnami-style chart.
- `kubernetes/apps/infrastructure/helmtest/hack/values-restricted.yaml` is a committed, reusable
  values overlay that sets them.
- Charts that cannot meet `restricted` are **out of scope for `helmtest`**. The fallback is to run
  them inside a leased Kata sandbox against a nested cluster (k3d in the DinD daemon). That workflow
  does not exist today and this plan does not build it — it is named as a follow-up
  (`docs/runbooks/helmtest.md` § "Charts this namespace cannot run") so the gap is explicit rather
  than implied.

**ResourceQuota.** Corrected keys — `services.loadbalancers` and `services.nodeports` are
first-class quota keys; `count/services.loadbalancers` is *not* and would silently enforce nothing:

```yaml
requests.cpu: "4"          limits.cpu: "8"
requests.memory: 8Gi       limits.memory: 16Gi
requests.storage: 50Gi     persistentvolumeclaims: "10"
requests.ephemeral-storage: 8Gi   limits.ephemeral-storage: 16Gi
pods: "30"                 count/pods: "60"
services: "20"             services.loadbalancers: "0"   services.nodeports: "0"
count/secrets: "120"       count/configmaps: "60"
count/jobs.batch: "30"     count/cronjobs.batch: "10"
count/deployments.apps: "20"  count/statefulsets.apps: "10"
count/replicasets.apps: "60"
count/serviceaccounts: "20"
count/roles.rbac.authorization.k8s.io: "20"
count/rolebindings.rbac.authorization.k8s.io: "20"
```

`pods` and `count/pods` are **different limits and both are needed**: the `pods` quota counts only
non-terminal pods, so completed `helm test` hook pods (which a `hook-succeeded` policy may not
remove — see Verification) accumulate against etcd without ever touching it. `count/pods` bounds the
total. `replicasets`, `serviceaccounts`, `roles` and `rolebindings` are all directly writable by the
Role below and were previously uncapped.

**Correction to the earlier draft:** it claimed a `helm upgrade` loop is an *unbounded* Secret
generator. It is not — the pinned Helm 3.16.3 defaults `--history-max` to **10**, so a single release
is self-limiting at ten revision Secrets. The real bound is *concurrency*: the budget must be
`max_concurrent_releases × history_max` plus each chart's own Secrets. `count/secrets: 120` is sized
for a declared ceiling of **10 concurrent releases × 3 revisions** (`--history-max 3`, which the
runbook instructs and the smoke test exercises) plus 90 chart/hook Secrets of headroom. That ceiling
goes in `docs/runbooks/helmtest.md` as the supported limit, and the smoke test measures actual
install/upgrade/rollback/test peaks to confirm the number before it is treated as final.

**LimitRange** with default requests/limits (CPU, memory, ephemeral-storage) so a chart that omits
them cannot evade the quota.

**Network isolation, owned by the platform and not writable by the worker.** A default-deny
`NetworkPolicy` (both policyTypes, no rules) plus a CiliumNetworkPolicy that:

- allows **same-namespace** ingress and egress — without this, `helm test` hooks cannot reach the
  Service they are testing and no multi-component chart works;
- allows DNS to kube-dns **L7-locked** to `*.<ns>.svc.cluster.local` and `*.svc.cluster.local`,
  mirroring the `sandbox-agent-egress` pattern. An unrestricted kube-dns allowance is an
  exfiltration channel even with world egress denied, because CoreDNS forwards upstream;
- `egressDeny`s `world`, `host`, `remote-node`, `169.254.169.254/32`, `169.254.0.0/16`, `::/0`,
  **and the OpenBao ClusterIP** — the previous draft denied the NodePort path but left
  `openbao.openbao.svc:8200` reachable;
- selects pods by a namespace-wide empty selector, **not** by a label a chart controls.

Registry correction: `registry.chifor.me` is a **LAN LXC at 192.168.0.36**
(`kubernetes/infra/registry/README.md`), not an in-cluster endpoint. Image pulls are performed by
containerd on the node and are unaffected by pod egress policy; `helm pull` runs on the worker, also
outside these policies. So **no pod-level registry allowance is added**, and the LAN deny stands
unqualified.

**Credential: TokenRequest, not a standing token Secret.** Reversing the earlier draft. A
`kubernetes.io/service-account-token` Secret in the namespace would be deletable by the very Role
below (RBAC cannot express "all Secrets except this one"), so an ordinary chart collision could
destroy the credential being synced. Instead the SA has **no token Secret at all**; the Phase 2 sync
calls the `TokenRequest` API (`serviceaccounts/token`) for a bound token and re-issues it daily. This
also gives automatic rotation and removes the standing object a chart could delete.

**The requested TTL is a request, not a guarantee.** The API server caps it at
`--service-account-max-token-expiration` and silently returns something shorter, so "30 days
requested" is not "29 days of margin". The sync therefore:

- reads `status.expirationTimestamp` off **every** minted token and computes the *actual* lifetime;
- fails the run if that lifetime is under a floor of **72h** — enough for two missed daily runs plus
  the `bao agent` KV refresh interval — rather than publishing a credential that expires before the
  next sync;
- exports the minimum remaining lifetime across all twelve as a metric, and the alert fires on
  *remaining lifetime*, not on "no sync in 36h", so a shortened cap surfaces as a warning instead of
  an outage.

The effective cap is measured on this cluster's API servers as a Phase 2 prerequisite and recorded in
`docs/runbooks/helmtest.md`; the requested TTL is then set from the measured cap, not assumed.

**Reserved objects are protected by admission, because RBAC cannot protect them.** Removing the token
Secret does *not* make the credential immune to a chart collision: the Role below can `delete`
`serviceaccounts`, and deleting `helmtest-dw<N>` invalidates every token ever minted for it — old and
new. Deleting the `helmtest-deployer` Role or its RoleBinding does the same to authorization. RBAC
cannot express "all ServiceAccounts except this one" for `delete`, so a Kyverno `ClusterPolicy`
(Kyverno is already running here) denies `DELETE` and `UPDATE` from any non-platform principal on the
reserved objects in every `helmtest-dw<N>` namespace: the `helmtest-dw<N>` ServiceAccount, the
`helmtest-deployer` Role and RoleBinding, the ResourceQuota, the LimitRange, and both NetworkPolicies.
This is the same mechanism that lets `ingresses` be re-granted later, and it is what actually makes
the quota and the network boundary non-removable rather than merely un-granted.

**Role `helmtest-deployer` + RoleBinding.** Explicit verbs — **no wildcards**, because `["*"]`
includes `escalate` and `bind`, which is exactly the escalation the earlier draft claimed to
exclude:

| API group | Resources | Verbs |
|---|---|---|
| `""` | `secrets`, `configmaps`, `services`, `serviceaccounts`, `persistentvolumeclaims`, `pods` | `get,list,watch,create,update,patch,delete` |
| `""` | `pods/log`, `pods/status`, `events` | `get,list,watch` *(read-only)* |
| `""` | `pods/exec`, `pods/portforward` | `create,get` *(debugging a failed release)* |
| `apps` | `deployments`, `statefulsets`, `replicasets` | `get,list,watch,create,update,patch,delete` |
| `batch` | `jobs`, `cronjobs` | `get,list,watch,create,update,patch,delete` |
| `policy` | `poddisruptionbudgets` | `get,list,watch,create,update,patch,delete` |
| `autoscaling` | `horizontalpodautoscalers` | `get,list,watch,create,update,patch,delete` |
| `rbac.authorization.k8s.io` | `roles`, `rolebindings` | `get,list,watch,create,update,patch,delete` — **never `escalate` or `bind`** |

Dropped from the earlier draft, each for a reason worth recording:

- **`networkpolicies`** — a worker able to write NetworkPolicies can delete the default-deny baseline
  or add a permissive rule, which is the isolation boundary deleting itself. Platform-owned only.
- **`ingresses`** — creating an Ingress reconfigures the shared Traefik controller and can collide
  with or shadow an estate hostname, which zero-NodePort/LoadBalancer quota does not prevent.
  Kyverno is already running in this cluster (`clusterpolicy.kyverno.io/airlock-sandbox-exec-boundary`),
  so a later policy constraining `ingressClassName` and host suffix is the way to add this back.
- **`daemonsets`** — a DaemonSet places a pod on every node; nothing about a namespaced test needs it.
- **`endpoints`** — writable Endpoints let a pod hijack the address a Service resolves to.
- **`pods/attach`** — `exec` covers the debugging need.

`roles`/`rolebindings` are kept because Helm charts routinely ship them, and the API server's
built-in escalation check confines them to permissions `helmtest-deployer` itself holds — a
guarantee that is only true because the wildcard is gone.

**But that escalation check cuts both ways, and the supported-chart profile has to say so.** A chart
whose Role grants something this deployer does *not* hold is **rejected**, not silently narrowed. The
common cases are exactly the resources dropped above: a chart Role granting `create`/`patch` on
`events`, `get` on `endpoints`, or `create`/`update` on `coordination.k8s.io/leases` for leader
election will fail to install even though the chart is otherwise `restricted`-compatible. That is a
deliberate trade — none of the five dropped resources is required by Helm *itself*, only by
particular charts — but it must be discoverable before an agent burns a debugging cycle on it.

So `docs/runbooks/helmtest.md` § "Checking a chart before you install it" documents a
`helm template … | <linter>` pre-flight that inspects rendered resources, hooks, `lookup` calls and
**every rule in every rendered Role**, and reports which of them this deployer cannot grant, with the
values that disable the offending component where one exists. Anything genuinely needed becomes a
reviewed, narrow addition to the table above — not a wildcard.

**Two documented non-grants**, in `docs/runbooks/helmtest.md` where an agent will read them: no
cluster-scoped rights at all — so charts with a `crds/` directory, or that ship ClusterRoles,
ClusterRoleBindings, or Namespace objects, will fail — and no grant on any custom resource. A chart
needing a pre-provisioned CRD's namespaced CRs requires an explicit, reviewed addition to the Role.

### Phase 2 — an in-cluster token sync, replacing the operator ceremony

New CronJob `openbao-k8stoken-sync` (ns `openbao`) that mints bound tokens and writes a **fully
rendered kubeconfig** into each worker's own KV path:

```
af/dev-workers/dev-worker-<N>
  tep_kubeconfig       # ns testpool,       SA tep-dw<N>
  helmtest_kubeconfig  # ns helmtest-dw<N>, SA helmtest-dw<N>
```

The per-worker subtree is where ADR 0020 already says per-worker material belongs, and the existing
per-worker policy already grants `read` on `af/data/dev-workers/<host>` and `/*` — **no dev-worker
policy change is required**.

**How the sync authenticates to OpenBao.** Not with the breakglass token. It uses the existing
Kubernetes auth mount (ADR 0019's k8s-auth provisioner): `devworker-provision-job.yaml` gains a role
`k8stoken-sync` bound to `serviceAccountName: openbao-k8stoken-sync` in namespace `openbao` only,
carrying a new policy on exactly `af/data/dev-workers/dev-worker-{1..6}` — no `common`, no estate,
no `delete`, and no wildcard. Declarative, so the same daily Job restores it after a wipe.

**The capability list must match the workflow or nothing syncs.** `create`+`update` alone is not
enough: the existence probe needs `read`, and KV-v2's patch is an HTTP `PATCH` that needs the
`patch` capability (the CLI's read-modify-write fallback needs `read` as well). The policy therefore
grants `read`, `create`, `update`, `patch` on those six data paths. This is a strictly larger grant
than the earlier draft claimed but still cannot reach `common`, cannot reach `af/estate/*`, and
cannot delete anything.

**How it writes — CAS, not check-then-write.** `bao kv patch` per path preserves unrelated fields;
`put` would erase them. But "probe, then `put` if absent" is a TOCTOU race with the
`openbao-devworker-provision` Job — both can observe absence and the second write replaces the
first's document. `concurrencyPolicy: Forbid` does not help, because that only serializes this
CronJob against *itself*, not against a separate Job. So:

- absent path → `put` with **`cas=0`** (create-only). On a CAS conflict, another writer won the race;
  retry into the patch path rather than overwriting.
- present path → `patch`, retried on version conflict.
- an authorization or network error is **never** treated as absence; it aborts the run.
- the same `cas=0` fix is applied to `devworker-provision-job.yaml`, whose current
  any-read-error → `put` branch has the identical bug and can erase a synced field.

The two owned fields are written in one patch, so a partial run cannot leave a worker with a fresh
tep kubeconfig and a stale helmtest one.

**What goes in the kubeconfig.** The `server:` is the **worker-reachable** endpoint
`https://192.168.0.40:6443` (matching `dev_worker_tep_server`), not the Job's in-cluster
`kubernetes.default.svc` — the workers are outside the cluster. Before publishing, the Job validates
that the CA data is non-empty and parses, that the minted token's `TokenReview` identity is the
expected SA, and that the rendered document parses as a kubeconfig with the intended
`current-context` and namespace. Any failure leaves the previous KV value untouched.

**Kubernetes RBAC for the sync.** A ServiceAccount in `openbao`, plus one Role+RoleBinding per
target namespace granting `create` on `serviceaccounts/token` with `resourceNames` limited to the
single SA in that namespace. The `testpool` Role enumerates all six `tep-dw<N>` names. No `list`,
no `watch`, no Secret access anywhere — the TokenRequest design removes the need for it.

`TokenReview` is **cluster-scoped**, so the namespace Roles above cannot authorize the validation
step and every validation would 403. The sync also gets a dedicated ClusterRole granting `create` on
`authentication.k8s.io/tokenreviews` and nothing else, bound to its ServiceAccount. That is the only
cluster-scoped grant in this plan, and it confers no read access to any object.

**Scheduling and recovery.** Daily is the steady-state cadence, but daily alone would mean up to a
day of outage after a wipe, and **applying a CronJob does not run it** — a Flux reconcile of the
manifest changes the schedule, not the state. So the tree ships two objects: the CronJob for steady
state, plus a **separate bootstrap `Job`** carrying
`kustomize.toolkit.fluxcd.io/force: "enabled"` (the same wedge-guard pattern
`devworker-provision-job.yaml` already uses) so any change to the sync script recreates and re-runs
it immediately, and `ttlSecondsAfterFinished` so Flux re-applies it on the next reconcile.

That handles the "dependencies arrived late" case without a manifest edit: if the bootstrap Job
exhausts `activeDeadlineSeconds` because the `helmtest` SAs or the vault auth role are not there yet,
it is reaped by TTL and Flux recreates it on its next reconcile interval — the retry is the
reconcile loop, not an operator. The runbook still documents the one-liner to force a run now.
`concurrencyPolicy: Forbid` and `startingDeadlineSeconds` remain on the CronJob; the CAS writes above
are what make a bootstrap Job and a scheduled run overlapping safe.

Alerting is on **minimum remaining token lifetime across the twelve fields**, not on sync age — a
sync that runs daily but mints 24h tokens is a healthy-looking outage, and an alert on "no sync in
36h" would fire only after expiry.

**These two fields are deliberately NOT added to `devworker-seeds.sops.yaml`.** The seed contract in
this subtree is *seed-wins on every daily run*, so a seeded copy would fight the sync and revert to a
stale token. They are cluster-derived state. But the honest consequence is that **they are absent
after a wipe until a successful sync**, not that recovery is free. `docs/runbooks/openbao-recovery.md`
gains a new path class — *sync-owned* — with the ordering that actually works:

1. Restore the vault + KV mount + the `k8stoken-sync` auth role and policy (daily provision Job).
2. Run the sync; confirm all twelve fields validated and written.
3. Re-mint every worker's AppRole secret-id (ADR 0020's ceremony — unavoidable after a wipe).
4. Only then restart the workers' agents, which otherwise exit on the missing fields.

ADR 0020 and `openbao-dev-workers.md` both carry blanket "the seed restores this subtree" language;
both gain the exception. A CI check asserts `devworker-seeds.sops.yaml` never gains a
`*_kubeconfig` key.

### Phase 3 — `bao agent` renders both kubeconfigs; retire the SOPS channel

Add two `template` stanzas per user to `roles/dev_worker/templates/openbao-agent.hcl.j2`, with
matching `.ctmpl` files, rendering to `{{ user.home }}/.tep/kubeconfig` and
`{{ user.home }}/.helmtest/kubeconfig` at 0600, chowned to the user.

`openbao.yml` must first deploy both `.ctmpl` sources **and** create both destination directories at
0700 for **every** entry in `dev_worker_users` — `tep.yml` today creates `~/.tep` for
`dev_worker_agent_user` only, while these stanzas cover all users. Directory creation must precede
any agent start or restart, including a tag-limited run.

**This step can brick all six workers and must be sequenced.** `error_on_missing_key = true` plus
`template_config.exit_on_retry_failure = true` means a template pointing at a not-yet-present KV
field makes the **whole agent exit**, taking `~/.git-credentials` rendering with it.

*Gate (before any worker gets the new config).* For **each** of the six workers, under **that
worker's own identity**, both fields must read back non-empty and parse:

A pipeline hides the failure that matters — `cred … | python3` reports *Python's* status, so a
`cred` permission error reads as success. The gate runs `set -o pipefail`, captures to a variable,
and validates the document is actually usable rather than merely parseable:

```bash
gate_one() {  # $1=worker-index  $2=field  $3=expected-namespace
  ssh "c4@192.168.0.$((7+$1))" "set -o pipefail
    kc=\$(cred get dev-worker-$1 $2) || exit 91          # cred's own status, not the pipe's
    printf '%s' \"\$kc\" | python3 - $3 <<'PY'
import sys, base64, json, yaml
d = yaml.safe_load(sys.stdin.read()); ns = sys.argv[1]
ctx = next(c for c in d['contexts'] if c['name'] == d['current-context'])['context']
assert ctx['namespace'] == ns, ctx
cl = next(c for c in d['clusters'] if c['name'] == ctx['cluster'])['cluster']
assert cl['server'] == 'https://192.168.0.40:6443', cl['server']
assert base64.b64decode(cl['certificate-authority-data']).startswith(b'-----BEGIN CERTIFICATE')
tok = next(u for u in d['users'] if u['name'] == ctx['user'])['user']['token']
exp = json.loads(base64.urlsafe_b64decode(tok.split('.')[1] + '=='))['exp']
import time; assert exp - time.time() > 72*3600, 'token lifetime under the 72h floor'
PY"
}
for n in 1 2 3 4 5 6; do
  gate_one $n tep_kubeconfig      testpool       || { echo "GATE FAILED dw$n/tep"; exit 1; }
  gate_one $n helmtest_kubeconfig "helmtest-dw$n" || { echo "GATE FAILED dw$n/helmtest"; exit 1; }
done
```

It checks what a broken kubeconfig would get wrong: the context actually resolves to a cluster and a
user, the server is the worker-reachable endpoint (not `kubernetes.default.svc`), the CA decodes to a
certificate, and the bearer token has more than the 72h floor left. **Re-run for a host immediately
before its own cutover**, not once for all six at the start — the fields can change underneath a
serial rollout.

*Rollout.* Serially, one worker at a time. The boundary must enclose the **restart and the completed
render**, which the naive version does not: ansible queues the restart handler until the end of the
play, so any check written after the template task inspects the *old, healthy* process and passes
regardless. The role therefore runs `meta: flush_handlers` immediately after writing `agent.hcl`, and
only then:

1. the running unit's `ExecMainStartTimestamp` is newer than the `agent.hcl` mtime — proving the
   restart actually happened and this is not the pre-change process;
2. `NRestarts` is unchanged across a 2-minute settle window and `ActiveState=active`;
3. both kubeconfigs exist at 0600, owned by the right user — **and their content hashes match the KV
   values the gate validated**. Existence and mode alone would accept the *old SOPS-written* file
   sitting at the same path, which is exactly the failure this migration can produce;
4. a live call with each file (`kubectl --kubeconfig … auth can-i create secrets`) succeeds.

Only then the next worker. On any failure: restore the previous `agent.hcl` (kept as `.bak`),
restore the previous ownership state (below), restart, re-verify, and stop the rollout.

*Rehearsal, on dev-worker-1 only, before touching the other five:* delete one field from KV, confirm
the agent exits and systemd restarts it, restore the field, confirm it recovers unattended. The gate
proves the fields exist *now*; the rehearsal proves the failure mode is survivable *later*.

**Retiring the old channel — per host, not at the end.** The moment a host's `bao agent` owns
`~/.tep/kubeconfig`, the ansible writer for that host must stop, or two writers restore different
token generations on alternate runs.

An in-play fact cannot express this: a later full or tag-limited run re-evaluates it, and can hand
the file back to the SOPS writer while the agent still owns it. Ownership is therefore **durable
on-host state** — a marker file `/etc/openbao-agent/renders-kubeconfigs`, written when a host cuts
over and read as a fact at the start of every run. One state, two consumers:

- the `template` stanzas in `openbao-agent.hcl.j2` are emitted **only** when the marker is present;
- `tep.yml`'s SOPS render task carries `when: not <marker fact>`.

Because both read the same fact, the two writers are mutually exclusive by construction rather than
by ordering. A pre-flight assertion fails the run if the marker is present while
`dev_worker_enable_openbao` is false — that combination means no vault and no SOPS writer, i.e. no
kubeconfig at all. Rollback removes the marker, restores `agent.hcl`, and restarts.

**Resolving the `dev_worker_enable_openbao: false` contradiction.** The earlier draft promised that
mode keeps working *and* deleted the SOPS assets it depends on. Both cannot hold. The decision: once
all six workers are cut over, **`dev_worker_enable_openbao: false` no longer provisions kubeconfigs
at all** — the flag reverts to what ADR 0020 made it, a staged-rollout switch for the vault
integration, not a supported steady state. All six workers are vault-enabled today, so nothing
regresses in practice.

The convergence criterion changes accordingly: instead of "a host with
`dev_worker_enable_openbao: false` still gets working kubeconfigs", it becomes "such a host converges
cleanly, installs the `tep` CLI, and provisions **no** kubeconfig — and the role says so rather than
failing obscurely". `docs/runbooks/dev-workers.md` records the mode change.

Only after all six are green, and after that criterion is asserted: delete
`ansible/secrets/tep-tokens.sops.yaml`, `scripts/tep-render-kubeconfigs.py`,
`templates/tep-kubeconfig.j2`, and the marker logic.

**Burning the old tep tokens.** The legacy `tep-dw<N>-token` Secrets are what the SOPS file and six
disks held, so they must die — but deleting one invalidates it *before* its replacement lands. Per
worker, in order: confirm the TokenRequest-minted kubeconfig is rendered and works
(`kubectl --kubeconfig ~/.tep/kubeconfig get sandboxclaims`), *then* retire that worker's legacy
Secret, then confirm the old token is rejected. One worker at a time; no window where a worker has
neither.

**Deleting the live Secret is not enough — Flux would put it straight back.** The six
`tep-dw<N>-token` Secrets are *declared* in
`kubernetes/apps/infrastructure/testpool/tep-access.yaml`, which the earlier draft omitted from the
change list entirely. A `kubectl delete` is undone on the next reconcile, and the standing credential
source silently returns. Retirement is therefore a git change, per worker, after that worker's
verified cutover: remove its Secret block from `tep-access.yaml`, merge, let Flux reconcile,
**then** confirm the object is gone and the old token is rejected. The six `ServiceAccount`
declarations stay — TokenRequest mints against them.

`tep-access.yaml` joins the change list below.

`claude-grant-write` is removed with an explicit `state: absent` task (not merely by deleting the
install task, which would leave the helper in place on every existing worker), and the role checks
for and removes any staged `/run/user/*/kube-rw-config`.

Update the managed `~/.claude/CLAUDE.md` block: `helm --kubeconfig ~/.helmtest/kubeconfig …`,
`--history-max 3`, the values overlay, and the two non-grants.

### Phase 4 — Tier B: escrow what is genuinely workstation-only

**Step 0, before any seeding — reconcile against existing homes.** For each candidate, determine
whether the value already lives in a Flux/SOPS secret, and if so whether it is the *same* value:
`backup-offsite/rclone-config.sops.yaml` vs `rclone-crypt-escrow.txt`;
`apps/ai/litellm-cloud-keys.sops.yaml` vs `platform.env`'s `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`;
`kubernetes/infra/_out/talos-backup-age.key` vs `~/work/keys/talos-backup-age.key`. The estate
runbook explicitly excludes duplicating Kubernetes-native credentials, so anything already owned by
Flux+SOPS is **dropped from this phase** and only recorded as a multi-home row. This step decides
the final table; the one below is the pre-reconciliation candidate list.

| Path | Fields | Candidate source |
|---|---|---|
| ~~`af/estate/talos-backup`~~ | — | **not escrowed — see below** |
| `af/estate/restic` | `nextcloud_password` | `~/work/keys/nextcloud-restic-password.txt` |
| `af/estate/rclone` | `crypt_escrow` | `~/work/keys/rclone-crypt-escrow.txt` |
| `af/estate/platform` | `hatchet_encryption_master_keyset`, `hatchet_jwt_public_keyset`, `hatchet_jwt_private_keyset`, `hatchet_client_token`, `sendgrid_key`, `openai_api_key`, `anthropic_api_key` | `~/work/keys/platform.env` |
| `af/estate/oauth` | `gcloud_client_secret`, `github_client_secret` | `~/work/keys.txt` |

**The talos-backup key is Tier C, so it is not escrowed at all.** It decrypts etcd snapshots, and
etcd holds every k8s Secret in the cluster — including `openbao-breakglass-token` and
`openbao-estate-seeds`. Putting it in the vault would place, inside the online boundary, a key that
decrypts historical copies of that same boundary's own root token and every estate seed: a Tier C
credential wearing a Tier B label.

The earlier draft escrowed it anyway and leaned on a runbook label to mark the risk. A label is not a
control, and the "otherwise it exists in one place only" argument does not hold either — the fix for
a single copy is a *second offline* copy, not an online one. So this key is **removed from the Phase
4 table** and joins the Tier C list beside the unseal key and the age key: authoritative copy offline,
a second offline copy verified as part of Phase 5, and neither inside the cluster nor the vault. DR
must work with both down, which also requires the snapshot-retrieval credential (`af/estate/rclone`,
or wherever Step 0 concludes it lives) to be reachable offline — recorded as a DR prerequisite in
`openbao-recovery.md`.

The tiering rule gains a sentence making this general rather than a one-off: **a credential that
decrypts a backup of the boundary is classified by what the backup contains, not by what the
credential is for.**

Every path is Tier B: no policy grant is added, so `cred` cannot reach them by design.

Two couplings this repo enforces, both in the same commit:

- Each new `path:field` goes into the **completeness matrix** in `estate-provision-job.yaml`. Note
  what that matrix does and does not prove: it probes *live KV after seeding*, so a stale live value
  masks a seed that never contained the field, and it passes on an empty-string value. Phase 4
  therefore also adds a seed-side assertion — the Job parses each `<name>.json` and fails if a
  matrix field is absent **or empty** in the seed itself, without printing values.
- The multi-home table in `docs/runbooks/openbao-estate-credentials.md` gains a row per path,
  including the "already owned by Flux+SOPS" rows that Step 0 excludes from seeding.

**Audit, not fix:** record in the runbook who currently holds Secret read in ns `openbao` (the real
Tier B boundary per the tiering note), and confirm or remove the undocumented `auth/userpass` `root`
user that runbook already flags.

### Phase 5 — workstation cleanup (operator ceremony, documented not automated)

Once Phase 4 is verified in the vault:

- **`~/work/keys/age.agekey`** — delete **only after** confirming a recoverable offline copy exists
  (removable media or password manager). `kubernetes/infra/_out/age.agekey` is on the *same
  workstation* and gitignored; `.gitignore` is not access control and a same-disk copy is not a
  backup. Then `chmod 600` the retained one. Same treatment, same order, for the offline
  talos-backup key.
- **`~/work/keys/kubeconfig.txt`** — delete; it is a redundant 0644 copy of cluster-admin, and
  `~/.kube/config` holds the canonical one. Recovery is `talosctl kubeconfig`, not this file.
- `chmod 600` on every retained credential home — `~/.kube/config`, `~/.kube/ailab.config`,
  `~/.git-credentials`, `~/.gitea_tok`, `~/.cc_gitea_issue_token`, **and the Phase 4 source files
  that stay in place** (`~/work/keys/*`, `ailab/.env`, the seven `terraform.tfvars`) — plus
  `chmod 700 ~/work/keys`. Escrow does not remove the originals, so it does not remove the need.
- Delete the stale scratch files **individually after confirming each has no consumer**:
  `~/.gitea_cred_tmp`, `~/.cutover_cookie`, `~/.cutover_sess_secret`, `~/.cutover_dump_name`.
- The two Gitea tokens in `~/.gitea_tok` and `~/.cc_gitea_issue_token` have **different hashes**,
  which proves only that they differ — not that either is redundant. Identify each one's owner,
  scopes and consumers in Gitea (and compare against the shared `dev_worker_gitea_token`) before
  revoking anything, and update every consumer first.
- Delete `.env`'s `SSO_PASSWORD` rather than escrowing it — the estate runbook already records that
  it has zero consumers; confirm with a repo-wide grep in the same change.

## Critical files

| Path | Role |
|---|---|
| `kubernetes/apps/infrastructure/helmtest/` *(new)* | Namespaces + PSA labels, quotas, LimitRanges, default-deny NetworkPolicy + CNP, SAs (no token Secrets), Role + RoleBinding |
| `kubernetes/apps/infrastructure/helmtest/hack/values-restricted.yaml` *(new)* | Reusable PSA-`restricted` values overlay |
| `kubernetes/apps/infrastructure/helmtest/hack/smoke-chart/` *(new)* | Pinned in-repo smoke chart with a real `helm test` hook |
| `kubernetes/apps/clusters/ai/helmtest.yaml` *(new)* | Flux Kustomization, mirroring `testpool.yaml` (`wait: false`) |
| `kubernetes/apps/infrastructure/security/openbao/k8stoken-sync.yaml` *(new)* | CronJob + SA + per-namespace Roles/RoleBindings (`serviceaccounts/token`, `resourceNames`-scoped) |
| `kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml` | Adds the `k8stoken-sync` k8s-auth role + its write-scoped policy |
| `kubernetes/apps/infrastructure/security/openbao/kustomization.yaml` | Wires the new sync manifest in |
| `ansible/roles/dev_worker/tasks/openbao.yml` | Deploys both `.ctmpl` sources; creates `~/.tep` + `~/.helmtest` 0700 for every `dev_worker_users` entry |
| `ansible/roles/dev_worker/templates/openbao-agent.hcl.j2` | Two new `template` stanzas per user |
| `ansible/roles/dev_worker/templates/{tep,helmtest}-kubeconfig.ctmpl.j2` *(new)* | Consul-template sources |
| `ansible/roles/dev_worker/tasks/tep.yml` | Render task gated on `dev_worker_openbao_renders_kubeconfigs`; CLI install stays |
| `ansible/roles/dev_worker/tasks/k8s_tools.yml` | `claude-grant-write` → `state: absent` |
| `kubernetes/apps/infrastructure/testpool/tep-access.yaml` | Legacy `tep-dw<N>-token` Secret blocks removed **per worker, after that worker's cutover**; the six ServiceAccounts stay |
| `kubernetes/apps/infrastructure/helmtest/kyverno-protect-reserved.yaml` *(new)* | ClusterPolicy denying delete/update of the reserved SA, Role, RoleBinding, quota, LimitRange and NetworkPolicies |
| `ansible/secrets/tep-tokens.sops.yaml`, `scripts/tep-render-kubeconfigs.py` | **Deleted** at the end of Phase 3 |
| `kubernetes/apps/infrastructure/security/openbao/estate-{seeds.sops,provision-job}.yaml` | New paths + matrix rows + the seed-side non-empty assertion |
| `docs/decisions/0021-agent-credential-plane-and-helm-testns.md` *(new)* | ADR: A/B/C tiering, per-worker namespaces, TokenRequest, sync-owned-not-seeded |
| `docs/runbooks/helmtest.md` *(new)* | Supported-chart profile, the two non-grants, `--history-max`, the out-of-scope fallback |
| `docs/runbooks/openbao-{dev-workers,estate-credentials,recovery}.md`, `docs/decisions/0020-*.md` | New fields, estate rows, the *sync-owned* path class + recovery ordering, the seed-exception |
| `kubernetes/apps/infrastructure/testpool/README.md` | Point at `helmtest` for deploys; `testpool` stays lease-only |

**Ordering, without a Flux cycle.** `helmtest` and `openbao` are separate Kustomizations, both
`wait: false`, so neither Kustomization's readiness proves the SAs exist or the sync ran. Rather than
adding a `dependsOn` cycle (`openbao` → `helmtest` → …), the **sync Job itself is the sequencer**: it
retries when a target SA does not yet exist and only reports success once all twelve fields validate.
Phase 3's gate consumes that success, so ordering is enforced by the gate, not by Flux.

## Verification

**Phase 1 — RBAC is exactly as wide as intended, and no wider.** Run with a real minted kubeconfig
*and* with impersonation including the SA's groups, since `--as` alone omits
`system:serviceaccounts*` group grants:

```bash
SA=system:serviceaccount:helmtest-dw1:helmtest-dw1
G=(--as-group=system:serviceaccounts --as-group=system:serviceaccounts:helmtest-dw1 --as-group=system:authenticated)
K="kubectl --context admin@ai"

for r in secrets deployments services configmaps pods jobs roles rolebindings; do
  echo "$r: $($K auth can-i create $r -n helmtest-dw1 --as=$SA "${G[@]}")"      # all yes
done
# each must be NO — enumerated individually, and ASSERTED, not echoed. `escalate`/`bind` are checked
# on the REFERENCED role, not on rolebindings: the API server authorizes a binding by asking whether
# the actor may `bind` the Role/ClusterRole being referenced.
deny() {  # asserts "no"; distinguishes a real deny from a connectivity failure
  out=$($K auth can-i $1 -n helmtest-dw1 --as=$SA "${G[@]}" 2>&1) || true
  case "$out" in
    no*) ;;
    yes*) echo "SECURITY FAIL: allowed: $1"; exit 1 ;;
    *)   echo "INCONCLUSIVE (not a deny): $1 -> $out"; exit 2 ;;
  esac
}
for c in "escalate roles.rbac.authorization.k8s.io" \
         "bind roles.rbac.authorization.k8s.io" \
         "bind clusterroles.rbac.authorization.k8s.io" \
         "create networkpolicies" "create ingresses" "create daemonsets" "create endpoints" \
         "delete resourcequotas" "patch resourcequotas" "delete limitranges" \
         "delete serviceaccounts" \
         "create pods --namespace=helmtest-dw2" "create pods --namespace=testpool" \
         "create namespaces" "create customresourcedefinitions" \
         "get secrets --namespace=openbao"; do deny "$c"; done
```

Authorization queries alone do not prove the escalation check fires, so two live attempts **with the
worker kubeconfig** must be rejected: creating a Role granting a permission the deployer lacks (e.g.
`create` on `events`), and creating a RoleBinding referencing `cluster-admin`. Note `delete
serviceaccounts` is expected to be **denied by the Kyverno policy** for the reserved SA specifically
while remaining allowed for chart-created ones — both cases are tested.

Admission and networking are not covered by authorization probes, so also, **using the worker
kubeconfig**: a `type: NodePort` and a `type: LoadBalancer` Service are both rejected by quota; a
valid `restricted` pod is admitted; a privileged variant is rejected with a `PodSecurity`-specific
message (not merely "an error"); a Deployment whose *pod template* violates `restricted` is accepted
as an object but produces no pods, with the rejection visible on the ReplicaSet — the case a naive
"the Deployment was created" check misses; a `helm test` hook pod reaches its own Service; a pod
cannot reach `helmtest-dw2`, `openbao.openbao.svc:8200`, `192.168.0.41:30820`, or `1.1.1.1`; and the
boundary probes are **repeated after** attempting to create a permissive NetworkPolicy (must be
denied) and after relabelling a pod (policy must still apply).

**Phase 2 — the sync populated KV without printing anything.**

```bash
kubectl --context admin@ai -n openbao create job --from=cronjob/openbao-k8stoken-sync sync-$(date +%s)
kubectl --context admin@ai -n openbao wait --for=condition=complete job/sync-<ts> --timeout=5m
kubectl --context admin@ai -n openbao logs job/sync-<ts> | grep -E '^validated 12/12 fields$'
```

Then the Phase 3 gate loop above (all twelve, per-identity, parsed, exit statuses asserted) — a bare
`cred get … | wc -c` reports the exit status of `wc`, not of `cred`. Failure paths are exercised
explicitly: an SA that does not exist yet, a sealed vault, and a concurrent
`openbao-devworker-provision` run — in each case existing KV fields must survive unchanged and no
token may appear in logs. Finally, run the seed provisioner and the sync back to back in both orders
and confirm seeded fields and sync-owned fields each survive the other.

**Phase 3 — end-to-end, the thing the user actually asked for.** Against the committed smoke chart,
not an upstream chart whose defaults drift (Bitnami's nginx defaults `service.type: LoadBalancer`,
which the corrected quota now correctly rejects):

`--history-max` is an **`upgrade` flag; `helm install` has no such flag** in the pinned 3.16.3 and
would exit before deploying anything. Use `upgrade --install`, and actually cross the retention limit
so pruning and upgrade permissions are exercised rather than assumed:

```bash
ssh c4@192.168.0.8
KC=~/.helmtest/kubeconfig
H="helm --kubeconfig $KC"
$H upgrade --install smoke ./smoke-chart -f values-restricted.yaml --wait --timeout 5m --history-max 3
for i in 2 3 4 5; do   # five revisions against a max of three: proves pruning, not just installing
  $H upgrade smoke ./smoke-chart -f values-restricted.yaml --set rev=$i --wait --history-max 3
done
[ "$($H history smoke -o json | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')" -le 3 ]
$H test smoke --logs           # a real hook: must RUN and PASS, and its logs must be retrievable
```

The smoke chart's test hook deliberately carries **no `hook-succeeded` delete policy**: in 3.16.3
that policy deletes the pod before `helm test --logs` can read it, so the logs come back empty and
the check silently proves nothing. The hook pod is named and deleted explicitly afterwards, and its
absence asserted **by name** — `-l 'helm.sh/hook'` is a label selector against what is normally an
*annotation*, so it matches nothing and would report "clean" regardless:

```bash
$H uninstall smoke
kubectl --kubeconfig $KC delete pod smoke-test --ignore-not-found
kubectl --kubeconfig $KC get pod smoke-test 2>&1 | grep -q NotFound
# the migrated tep path still works:
tep lease -t 10 && tep run -- true && tep release
```

Agent health is checked by **rendering**, not by a git read — an anonymous `git ls-remote` can
succeed with no credential at all, and a stale credential file survives an agent crash:

```bash
systemctl show openbao-agent -p NRestarts -p ActiveState   # stable across a 2-min window
sudo journalctl -u openbao-agent --since -2m | grep -c 'template.*rendered'
sudo systemctl restart openbao-agent                        # forces a full re-render
ls -l ~/.git-credentials ~/.tep/kubeconfig ~/.helmtest/kubeconfig   # all three present, 0600
git ls-remote https://git.chifor.me/cchifor/ailab.git HEAD >/dev/null   # now meaningful
```

Repeated for every user in `dev_worker_users`, on all six workers.

**Rotation adoption, without a restart.** A manual restart proves rendering works once; it does not
prove the steady state. `bao agent` treats these kubeconfigs as **static KV values** — it re-renders
when the KV value changes on its polling interval, but it has no idea the embedded k8s token has an
expiry and will never renew one on its own. Nothing in the restart check above would catch a worker
whose token quietly ages out. So:

```bash
# force a second sync, then WITHOUT touching the agent:
kubectl --context admin@ai -n openbao create job --from=cronjob/openbao-k8stoken-sync rot-$(date +%s)
# on the worker, within the agent's configured refresh bound:
sha256sum ~/.helmtest/kubeconfig    # must change to the new generation, unattended
# and independently of service health or sync success — the number that actually matters:
python3 - <<'PY' < ~/.helmtest/kubeconfig
import sys,yaml,json,base64,time
d=yaml.safe_load(sys.stdin); t=d['users'][0]['user']['token']
exp=json.loads(base64.urlsafe_b64decode(t.split('.')[1]+'=='))['exp']
print(f"{(exp-time.time())/3600:.1f}h remaining")   # must exceed the 72h floor
PY
```

That last figure is what the Phase 2 alert tracks. A green agent, a successful sync, and an expiring
token are three independent facts, and only the third one breaks the worker.

**Phase 4 — seeds and matrix agree, and Tier B stayed sealed.** The estate denial test must actually
address an estate path: `cred` prefixes every lookup with `dev-workers/`, so
`cred get estate/platform …` probes `af/dev-workers/estate/platform` and proves nothing. Use the sink
token directly and distinguish a 403 from a 404:

```bash
BAO_ADDR=https://openbao.lan.chifor.me:30820 BAO_TOKEN="$(cat /run/openbao-agent/token)" \
  bao kv get -mount=af -format=json estate/platform >/dev/null
echo "exit=$?"    # non-zero; stderr must say permission denied, NOT "no value found"
```

Plus: restore `estate-seeds.sops.yaml` into a disposable empty KV mount and confirm every matrix
field arrives non-empty — the live-KV probe alone cannot distinguish a good seed from a stale value.

**Convergence.** Re-run `just dev-workers` twice; the second reports near-zero `changed`. On a host
with `dev_worker_enable_openbao: false`, assert the revised criterion: converges cleanly, installs
the `tep` CLI, provisions **no** kubeconfig, and says so.

**Bootstrap rehearsal, in a disposable OpenBao — not the live vault.** Ansible idempotency proves
nothing about recovery, and the round-1 rehearsal (delete *one* field, restore it) tests a vault that
is already correctly configured. What this plan actually adds as new bootstrap dependencies is the
`k8stoken-sync` auth role, its policy, and the empty-path `cas=0` write branch — none of which a
single-field delete exercises at all, because they only run when the path or the auth mount is
*absent*.

So: stand up a throwaway OpenBao (a `bao server -dev` container, or a second release in a scratch
namespace), point a copy of the provision Job and the sync at it, and prove from empty:

1. the provision Job creates the k8s-auth role and the `k8stoken-sync` policy from scratch;
2. the sync authenticates against that fresh role — catching a policy that lacks `read`/`patch`,
   which is precisely the class of bug this round found and which a configured vault hides;
3. all **twelve** fields are reconstructed via the `cas=0` create-only path and validate;
4. the seed provisioner and the sync run in both orders against those empty paths without either
   erasing the other's fields.

Then, against the live vault, the scoped worker-recovery test only: delete the twelve sync-owned
fields, confirm the agents exit and systemd holds them in a restart loop, force a sync, and confirm
every worker recovers unattended with no operator action. The live vault is never wiped, the AppRole
re-mint ceremony stays ADR 0020's, and the parts this plan actually changes are still tested from
empty.

## Rejected

- **Widening `tep-worker` in `testpool`.** Granting pod-create there lets an agent schedule a plain
  pod alongside the Kata-isolated leases, bypassing the sandbox boundary the pool exists to provide.
- **One shared `helmtest` namespace.** Cheaper, but six agents share a release-name collision domain
  and one runaway chart's quota. Per-worker is the same manifest generated six times.
- **Provisioning `/etc/claude-agent/kube-rw-config`.** It hands out `edit` on the *whole cluster*
  behind a sudo prompt an agent can be talked into. Removed with `state: absent`, not merely
  un-installed.
- **Keeping the tep kubeconfig in SOPS and only adding helmtest to OpenBao.** Two credentials of the
  same kind on two channels is how rotation split-brains start.
- **Putting the age key or the `admin@ai` kubeconfig in `af/dev-workers/*`.** See the tiering note.
- **Standing `kubernetes.io/service-account-token` Secrets** (the earlier draft's choice). Reversed:
  the Role must be able to delete Secrets for Helm to work, RBAC cannot carve out one Secret by name
  for `delete`, so the credential would be destroyable by an ordinary chart collision. TokenRequest
  with daily re-issue removes the standing object, adds automatic rotation, and needs no worker-side
  renewal — the sync is the renewal point, so ADR 0020's "nothing renews it on the worker" objection
  does not apply here.
- **Granting `ingresses` now.** Deferred until a Kyverno policy constrains `ingressClassName` and
  host suffix; Kyverno is already running in this cluster.

<!-- codex-review-status: complete -->
