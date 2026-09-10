# Agent credentials in OpenBao + Helm-capable isolated test namespaces

## Codex Review

- TokenRequest needs an enforced lifetime/refresh budget and protected ServiceAccounts; requesting 30 days does not guarantee 29 days of outage margin.
- The sync policy lacks permissions required by its own validation and write workflow, and concurrent first writes can still erase fields.
- Serial rollout helps contain failures, but the gate, restart verification, persistent writer ownership, and disabled-OpenBao cleanup path need correction.
- The corrected quota keys are valid; the 60-object budgets need workload evidence. The restricted RBAC supports ordinary Helm operations but needs a broader chart compatibility contract.
- Offline escrow breaks the backup-key DR dependency, while its online placement conflicts with the stated Tier C rule. The convergence concern is narrowed to isolated testing of the new bootstrap dependencies.

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
pods: "30"
services: "20"             services.loadbalancers: "0"   services.nodeports: "0"
count/secrets: "60"        count/configmaps: "60"
count/jobs.batch: "30"     count/cronjobs.batch: "10"
count/deployments.apps: "20"  count/statefulsets.apps: "10"
```

<!-- codex: `pods: 30` excludes Succeeded/Failed Pods, so retained standalone Helm test Pods can accumulate beyond the advertised object bound; directly writable ReplicaSets and ServiceAccounts also have no count cap here. Add `count/pods` and appropriate bounds for the remaining writable object kinds if the quota is intended to contain etcd object growth. [Resource quotas](https://kubernetes.io/docs/concepts/policy/resource-quotas/) -->

The object counts are not padding: Helm keeps one release Secret **per revision**, so an agent
looping `helm upgrade` is an unbounded Secret generator against etcd. `docs/runbooks/helmtest.md`
tells agents to run with `--history-max 3`.

<!-- codex: The corrected Service keys and `count/secrets`/`count/configmaps` are valid literal object limits, but 60 is not justified by a declared largest chart or concurrent-release limit: twenty releases retaining three revisions already consume all sixty Secrets before application or hook Secrets. Measure install/upgrade/rollback/test peaks for the supported workload, and correct the unbounded-upgrade claim because the pinned Helm 3.16.3 defaults upgrade history to ten unless overridden. [Resource quotas](https://kubernetes.io/docs/concepts/policy/resource-quotas/), [Helm upgrade implementation](https://raw.githubusercontent.com/helm/helm/v3.16.3/cmd/helm/upgrade.go) -->

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
calls the `TokenRequest` API (`serviceaccounts/token`) for a bound token with a 30-day TTL and
re-issues it daily, leaving ~29 days of outage margin. This also gives automatic rotation and makes
the credential unrevocable-by-chart.

<!-- codex: The API server can shorten a requested lifetime, so a 24h grant would leave no overlap for a daily sync plus Agent's default five-minute KV refresh, and the 36h alert would fire after expiry. Validate every returned `status.expirationTimestamp`, budget retries/rendering/outage margin inside the actual lifetime, and verify the cap on every API server before claiming 29 days of headroom. [API-server expiration cap](https://kubernetes.io/docs/reference/command-line-tools-reference/kube-apiserver/), [Agent refresh behavior](https://openbao.org/docs/2.5.x/agent-and-proxy/agent/template/) -->

<!-- codex: The Role still permits deleting the worker's ServiceAccount, which invalidates both old and newly minted tokens; deleting its platform Role or RoleBinding similarly removes access. Protect those reserved identities and RBAC objects with admission policy, because removing the token Secret alone does not make this credential immune to chart collisions. [ServiceAccount token lifecycle](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/) -->

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

<!-- codex: Retaining Role CRUD does not guarantee chart RBAC can be installed: a namespaced Role granting event creation/patching, Endpoint reads, or leader-election Lease access is rejected because this deployer lacks those permissions. Extend the supported-chart profile to inspect rendered resources, hooks, lookups, and Role rules, with explicit settings to disable unsupported components or reviewed narrow additions; none of the five dropped resources is universally required by Helm itself. [RBAC escalation checks](https://kubernetes.io/docs/reference/access-authn-authz/rbac/), [Helm test implementation](https://raw.githubusercontent.com/helm/helm/v3.16.3/pkg/action/release_testing.go) -->

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
carrying a new policy that grants `create`+`update` on exactly
`af/data/dev-workers/dev-worker-{1..6}` and nothing else — no read of `common`, no estate, no
delete. Declarative, so it is restored by the same daily Job after a wipe.

<!-- codex: `create`+`update` cannot execute the specified `kv get`/`kv patch` workflow: the existence probe requires `read`, HTTP PATCH requires `patch`, and the CLI fallback also needs `read`. Grant the required capabilities on the six exact data paths and explicitly select the write method, or redesign the workflow to avoid reads; the current policy prevents any successful sync. [OpenBao KV-v2 ACLs and patch behavior](https://openbao.org/docs/secrets/kv/kv-v2/) -->

**How it writes.** `bao kv patch` per path, never `put`: patch preserves any unrelated field in that
worker's subtree, `put` would erase it. A genuinely absent path is created with `put` **only** when
a `kv get` returns a real not-found; an authorization or network error must abort the run, never be
treated as absence. The two owned fields are written together so a partial run cannot leave a worker
with a fresh tep kubeconfig and a stale helmtest one.

<!-- codex: A genuine not-found followed by an unconditional `put` still races the seed provisioner: both can observe absence, then the second write replaces the first document. Use create-only CAS (`cas=0`) with conflict retry into the merge path in both writers, and fix the existing provisioner's any-read-error-to-`put` branch; CronJob `Forbid` does not serialize it with the separate provisioner Job. -->

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

<!-- codex: The named TokenRequest restriction is valid, but these namespace Roles do not authorize the planned TokenReview validation. Add a narrowly scoped ClusterRole and binding granting `create` on `authentication.k8s.io/tokenreviews`, or use a different authenticated validation method; otherwise every validation attempt is forbidden and nothing is published. [TokenReview request scope](https://www.kubernetes.dev/resources/keps/1040/) -->

**Scheduling and recovery.** Daily is the steady-state cadence, but daily alone would mean up to a
day of outage after a wipe. So: `concurrencyPolicy: Forbid`, `startingDeadlineSeconds`,
`backoffLimit` with `activeDeadlineSeconds`, a Flux-triggered immediate run on manifest change, and
a Prometheus alert on "no successful sync in 36h" wired into `testpool-rules.yaml`'s sibling. The
runbook documents the one-liner to force a run.

<!-- codex: Applying or updating a CronJob does not itself execute its job template, so the promised Flux-triggered immediate run needs an explicit mechanism and manifest. Define that trigger and its retry behavior when bootstrap dependencies arrive after the initial Job exhausts its deadline, including recovery without a manifest change. -->

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

```bash
for n in 1 2 3 4 5 6; do
  for f in tep_kubeconfig helmtest_kubeconfig; do
    ssh c4@192.168.0.$((7+n)) "cred get dev-worker-$n $f | python3 -c \
      'import sys,yaml; d=yaml.safe_load(sys.stdin); assert d[\"current-context\"]; print(\"ok\")'" \
      || { echo "GATE FAILED: dev-worker-$n/$f"; exit 1; }
  done
done
```

<!-- codex: This still reports only Python's pipeline status, and a YAML document containing merely `current-context: anything` passes without being a usable kubeconfig. Assert the `cred` exit status in the remote shell and validate context references, expected namespace/server, CA, bearer token and remaining lifetime, then repeat the host's checks immediately before its cutover. -->

*Rollout.* Serially, one worker at a time: apply → `systemctl is-active openbao-agent` stable across
a 2-minute window with an unchanged `NRestarts` counter → both files present at 0600 → next worker.
On failure, roll that host back to the previous `agent.hcl` (kept as `.bak` by the role) and stop.

<!-- codex: Make the serial boundary include the actual restart and completed renders: the current role queues its restart handler, so checks inside the role can inspect the old healthy process unless handlers are flushed first. Require the applied process/config generation and both rendered kubeconfigs to match the validated KV values, with correct ownership, before advancing; file existence and mode alone can accept the old SOPS token. [Ansible handler timing](https://docs.ansible.com/projects/ansible/latest/playbook_guide/playbooks_handlers.html) -->

*Rehearsal, on dev-worker-1 only, before touching the other five:* delete one field from KV, confirm
the agent exits and systemd restarts it, restore the field, confirm it recovers unattended. The gate
proves the fields exist *now*; the rehearsal proves the failure mode is survivable *later*.

**Retiring the old channel — per host, not at the end.** The moment a host's `bao agent` owns
`~/.tep/kubeconfig`, the ansible writer for that host must stop, or two writers can restore different
token generations. `tep.yml`'s render task therefore gains
`when: not dev_worker_openbao_renders_kubeconfigs` (a new per-host flag flipped by the same play that
installs the stanzas). Hosts with `dev_worker_enable_openbao: false` keep the ansible path
unchanged and must keep working — that combination is asserted in the role's idempotency run.

<!-- codex: A flag flipped only as an in-play fact is not durable ownership: a later full or tag-limited run can restore the SOPS writer while the running agent still owns the file. Persist one per-host ownership state, use it consistently for both template stanzas and the SOPS guard, assert its compatibility with `dev_worker_enable_openbao`, and restore ownership plus restart the previous config on rollback. -->

Only after all six are green: delete `ansible/secrets/tep-tokens.sops.yaml`,
`scripts/tep-render-kubeconfigs.py`, `templates/tep-kubeconfig.j2`, and the flag itself.

<!-- codex: Deleting the SOPS input/template and then revoking legacy tokens contradicts the promise that `dev_worker_enable_openbao: false` continues to provision working kubeconfigs. Define a retained alternative for that mode, or explicitly retire its kubeconfig support and change the convergence acceptance criterion before removing these files and the ownership guard. -->

**Burning the old tep tokens.** The legacy `tep-dw<N>-token` Secrets are what the SOPS file and six
disks held, so they must die — but deleting one invalidates it *before* its replacement lands. Per
worker, in order: confirm the TokenRequest-minted kubeconfig is rendered and works
(`kubectl --kubeconfig ~/.tep/kubeconfig get sandboxclaims`), *then* delete that worker's legacy
Secret, then confirm the old token is rejected. One worker at a time; no window where a worker has
neither.

<!-- codex: The six legacy Secrets remain declared in `kubernetes/apps/infrastructure/testpool/tep-access.yaml`, which is missing from the change list, so deleting only live objects lets Flux recreate the standing credential source. Remove each declaration only after its worker's verified cutover, retain the ServiceAccounts, and check revocation after reconciliation. -->

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
| `af/estate/talos-backup` | `age_key` | `~/work/keys/talos-backup-age.key` |
| `af/estate/restic` | `nextcloud_password` | `~/work/keys/nextcloud-restic-password.txt` |
| `af/estate/rclone` | `crypt_escrow` | `~/work/keys/rclone-crypt-escrow.txt` |
| `af/estate/platform` | `hatchet_encryption_master_keyset`, `hatchet_jwt_public_keyset`, `hatchet_jwt_private_keyset`, `hatchet_client_token`, `sendgrid_key`, `openai_api_key`, `anthropic_api_key` | `~/work/keys/platform.env` |
| `af/estate/oauth` | `gcloud_client_secret`, `github_client_secret` | `~/work/keys.txt` |

**The talos-backup key is a transitive Tier C credential.** It decrypts etcd snapshots, and etcd
holds every k8s Secret in the cluster — including `openbao-breakglass-token` and
`openbao-estate-seeds`. So escrowing it *into* OpenBao creates a loop: the vault holds a key that
decrypts a backup that contains the vault's own root token. It is escrowed anyway (the alternative
is one workstation copy), but it is labelled in the runbook as a Tier-C-equivalent, and its
authoritative recovery copy stays **offline and outside both the cluster and the vault** — DR must
work with neither running. This is recorded in `openbao-recovery.md` alongside the unseal key.

<!-- codex: An independently recoverable offline copy breaks the DR dependency, provided snapshot retrieval credentials and decryption prerequisites are also available without the cluster/vault; escrowing an additional copy is therefore not inherently a recovery deadlock. However, online escrow still contradicts the stated Tier C exclusion and exposes historical etcd secrets to that online boundary, so keep the key offline-only or explicitly justify an exception in the tiering rule rather than treating a runbook label as equivalent protection. -->

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
# each must be NO — enumerated individually; a wildcard query returning "no" proves nothing:
for c in "escalate roles" "bind rolebindings" "create networkpolicies" "create ingresses" \
         "create daemonsets" "create endpoints" "delete resourcequotas" "patch resourcequotas" \
         "delete limitranges" "create pods --namespace=helmtest-dw2" \
         "create pods --namespace=testpool" "create namespaces" \
         "create customresourcedefinitions" "get secrets --namespace=openbao"; do
  echo "$c: $($K auth can-i $c -n helmtest-dw1 --as=$SA "${G[@]}")"             # all no
done
```

<!-- codex: `bind rolebindings` probes the wrong resource: binding authorization checks `bind` on the referenced Role or ClusterRole, and echoing `can-i` output does not assert the expected result. Check those resources explicitly and exercise rejection of an overprivileged Role and a RoleBinding to `cluster-admin`, with failures distinguished from connectivity errors. [Binding restrictions](https://kubernetes.io/docs/reference/access-authn-authz/rbac/) -->

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

```bash
ssh c4@192.168.0.8
KC=~/.helmtest/kubeconfig
helm --kubeconfig $KC install smoke ./smoke-chart -f values-restricted.yaml --wait --timeout 5m --history-max 3
helm --kubeconfig $KC test smoke --logs        # the chart ships a real test hook; must run AND pass
helm --kubeconfig $KC uninstall smoke
kubectl --kubeconfig $KC get pods -l 'helm.sh/hook'   # hook pods cleaned up (delete policy)
# the migrated tep path still works:
tep lease -t 10 && tep run -- true && tep release
```

<!-- codex: The pinned Helm v3.16.3 `install` command has no `--history-max` flag, so this smoke test stops before deploying anything. Use `upgrade --install --history-max 3` or omit that flag from install, then exercise actual upgrades past the retention limit so upgrade permissions and release-history pruning are verified. [Install flags](https://raw.githubusercontent.com/helm/helm/v3.16.3/cmd/helm/install.go), [History pruning](https://raw.githubusercontent.com/helm/helm/v3.16.3/pkg/storage/storage.go) -->

<!-- codex: In Helm 3.16.3, a `hook-succeeded` delete policy removes the test Pod before `helm test --logs` fetches its logs, while `helm.sh/hook` is normally an annotation and the label selector can falsely report no leftovers. Retain a named Pod hook until logs are collected, then explicitly delete and assert its absence using its name or a chart-defined label. [Hook deletion](https://raw.githubusercontent.com/helm/helm/v3.16.3/pkg/action/hooks.go), [Test log retrieval](https://raw.githubusercontent.com/helm/helm/v3.16.3/pkg/action/release_testing.go) -->

Agent health is checked by **rendering**, not by a git read — an anonymous `git ls-remote` can
succeed with no credential at all, and a stale credential file survives an agent crash:

```bash
systemctl show openbao-agent -p NRestarts -p ActiveState   # stable across a 2-min window
sudo journalctl -u openbao-agent --since -2m | grep -c 'template.*rendered'
sudo systemctl restart openbao-agent                        # forces a full re-render
ls -l ~/.git-credentials ~/.tep/kubeconfig ~/.helmtest/kubeconfig   # all three present, 0600
git ls-remote https://git.chifor.me/cchifor/ailab.git HEAD >/dev/null   # now meaningful
```

<!-- codex: Restart-based checks do not prove steady-state rotation: Agent polls these kubeconfigs as static KV values and does not renew or detect expiry of their embedded Kubernetes tokens. Run a second sync without restarting Agent, verify each user's files adopt the new validated generation within the configured refresh bound, and check remaining on-disk token lifetime independently of service health and sync success. [Agent static-secret refresh](https://openbao.org/docs/2.5.x/agent-and-proxy/agent/template/) -->

Repeated for every user in `dev_worker_users`, on all six workers.

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

**Convergence.** Re-run `just dev-workers` twice; the second reports near-zero `changed`, including
on a host with `dev_worker_enable_openbao: false`.
<!-- codex: round-2: Agreed that the live vault must not be wiped and the unchanged AppRole re-mint ceremony can remain covered by ADR 0020; the new sync auth role/policy and empty-path write branch are changed bootstrap dependencies that deleting fields from an already-configured vault does not test. Bootstrap those dependencies and reconstruct all twelve fields in a disposable isolated OpenBao instance, plus perform the scoped worker recovery test: the executable plan currently deletes/restores only one field manually, not the twelve-field reconstruction claimed in the pushback. -->
<!-- opus-pushback: A full vault wipe + re-bootstrap rehearsal is owned by openbao-recovery.md and would put the live estate's only vault through a destructive drill to validate a credential migration. The scoped substitute is in the plan: delete the twelve sync-owned fields from KV, prove the sync reconstructs and validates all twelve, and prove a worker's agent recovers unattended — which exercises every step of the recovery ordering except the parts (KV mount, AppRole re-mint) that ADR 0020 already covers and that this plan does not change. -->

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
