# ADR 0021 — The agent credential plane: helm-capable test namespaces + sync-owned kubeconfigs

**Status:** PROPOSED (2026-09-10). Implementation on `feat/openbao-agent-creds-and-helm-testns`.
Phase 3 (the `bao agent` cutover) ships **opt-in per host** (`dev_worker_openbao_kubeconfig_cutover:
false`) so the cluster side can land and be verified before any worker changes.
**Relates to:** ADR 0020 (per-VM AppRole + agent-rendered files — this extends that plane to
Kubernetes credentials and amends its durability claim), 0019 (OpenBao + ESO as the estate secret
store; the k8s auth mount this reuses), 0018 (the dev-worker agents that consume these), 0007 (k8s
exposure), 0006 (Talos/Cilium/Flux).

## Context

Two findings from the 2026-09-10 credential audit, with one shared root cause.

**An agent cannot deploy anything.** `testpool` is where agents run tests, and its `tep-worker` Role
grants sandboxclaim CRUD, read-only pods/sandboxes, and exec/attach/portforward — nothing else.
Verified live: `kubectl auth can-i create secrets --as=system:serviceaccount:testpool:tep-dw1` → **no**,
so `helm install` fails at release-state creation before it looks at a chart. The documented
escalation hook (`claude-grant-write` → `/etc/claude-agent/kube-rw-config`) is a dead end on these
hosts: nothing in this repo ever provisioned that file — it came from the *homelab* repo's
`platform/main.tf`, for claude-worker VMs — so it only ever printed "not provisioned" and exited 1.

**The credentials agents do have are distributed by a second channel.** `af/dev-workers/common`
carries exactly `gitea_pat` and `proxmox_ssh_key`. The tep kubeconfig — a real Kubernetes bearer
token — lives instead in `ansible/secrets/tep-tokens.sops.yaml`, is extracted by a hand-run script
(`scripts/tep-render-kubeconfigs.py`), and is distributed by `just dev-workers`. That is a second
durable home for a credential, in git, rotated by a three-step ceremony. ADR 0020 built a credential
plane precisely to stop this shape, and this credential was never moved onto it.

The shared root cause: `testpool` is a **lease** namespace, not a **deploy** namespace, and there is
no third thing. So there is nowhere to point a deploy credential at, and the one k8s credential that
does exist predates the vault plane.

## Decision

### 1. A deploy namespace per worker, not a wider lease namespace

Six namespaces `helmtest-dw1..6`, one per dev-worker. Widening `tep-worker` was rejected: granting
pod-create in `testpool` lets an agent schedule a plain pod alongside the Kata-isolated leases and
bypass the boundary the pool exists to provide. Per-worker rather than one shared namespace because
six agents run concurrently — a shared namespace is a shared release-name collision domain and a
shared quota, and "isolated" was the actual requirement.

The controls are the decision, not the namespace:

- **PSA `enforce: restricted`.** This is what makes `create pods` safe at all; without it, pod-create
  in a namespace is a node-compromise primitive (hostPath, privileged, hostNetwork). Note it is the
  *opposite* of `testpool`'s `enforce: privileged`, which is correct there because DinD needs
  privileged and Kata confines it. Nothing here runs under Kata, so nothing here may be privileged.
- **Explicit RBAC verbs, no wildcards.** `verbs: ["*"]` matches `escalate` and `bind`. A wildcard on
  `roles` would therefore grant exactly the escalation the design excludes, and would let a worker
  manufacture broader permissions and then delete the quota and network policy containing it.
- **Quota keys that actually enforce.** `services.loadbalancers` / `services.nodeports` are
  first-class quota keys; `count/services.loadbalancers` is not one and silently enforces nothing.
  And `pods` counts only non-terminal pods, so completed `helm test` hook pods accumulate against
  etcd without ever touching it — `count/pods` is the bound that matters.
- **Kyverno protects the reserved objects.** `helmtest-deployer` must be able to delete
  `serviceaccounts` for `helm uninstall` to work, and RBAC cannot express "all ServiceAccounts except
  this one". So an ordinary release collision could delete the worker's own identity and invalidate
  every token ever minted for it. A ClusterPolicy denies DELETE/UPDATE on the reserved SA, Role,
  RoleBinding, ResourceQuota, LimitRange and NetworkPolicies from any non-Flux principal. This is
  also what makes the quota and network boundary *non-removable* rather than merely un-granted.

Deliberately not granted, each for a stated reason: `networkpolicies` (the boundary deleting
itself), `ingresses` (reconfigures the shared Traefik controller and can shadow an estate hostname —
re-grant once a Kyverno policy constrains class and host suffix), `daemonsets`, `endpoints`,
`pods/attach`, and anything cluster-scoped.

### 2. TokenRequest, not standing token Secrets

`tep-access.yaml` uses long-lived `kubernetes.io/service-account-token` Secrets. Repeating that shape
for `helmtest` does not work: the Role must be able to delete Secrets, so the credential would sit in
a namespace where an ordinary chart collision can destroy it. The sync calls the **TokenRequest API**
instead — no standing object to delete, and re-issuing daily gives rotation for free.

The requested TTL is a request, not a guarantee: the API server caps `expirationSeconds` and silently
returns something shorter. So the sync reads `status.expirationTimestamp` off every minted token and
**fails below a 72h floor** rather than publishing a credential that expires before the next run.
*Measured 2026-09-10: this cluster grants a 720h request in full*, so the daily re-issue leaves ~29
days of margin — re-measure after a Talos/k8s upgrade rather than assuming it holds.

This reverses ADR 0020's reasoning about bound tokens ("they expire and nothing on a worker renews
them"). That objection assumed worker-side renewal. The sync *is* the renewal point, in-cluster, so
no broker is needed.

### 3. The kubeconfigs are sync-owned KV, and deliberately NOT seeded

`af/dev-workers/<host>` gains `tep_kubeconfig` and `helmtest_kubeconfig`, written by
`openbao-k8stoken-sync` and rendered onto each worker by `bao agent`. The per-worker AppRole policy
already grants read on that subtree, so **no dev-worker policy change was required**.

They are **not** added to `devworker-seeds.sops.yaml`. That subtree's contract is *seed-wins on every
daily run*, so a seeded copy would fight the sync and revert to a stale bearer token. This introduces
a **fourth precedence** in a repo that already has three seeders, and the honest consequence is that
these fields are **absent after a wipe until a successful sync** — not that recovery is free.
`docs/runbooks/openbao-recovery.md` carries the *sync-owned* path class and the recovery ordering;
ADR 0020 and `openbao-dev-workers.md` carry the exception to their blanket durability language.

The sync authenticates with the **Kubernetes auth mount**, not the breakglass token — a daily
credential-writer must not hold root. Its policy grants `read`, `create`, `update`, **`patch`** on
exactly the six data paths. `patch` is not an extra: KV-v2's patch is an HTTP PATCH and the probe
that decides patch-vs-create needs `read`, so a create+update-only policy looks tighter and makes
every sync fail.

Writes use **create-only CAS (`cas=0`)**, not check-then-write. `openbao-devworker-provision` writes
the same paths, and both jobs can observe absence at once; the loser of a bare-`put` race replaces
the winner's document and erases its fields. `concurrencyPolicy: Forbid` does not help — it
serialises the CronJob against itself, not against a separate Job. The same bug existed in
`devworker-provision-job.yaml` (any read error → full-document `put`) and is fixed there too.

### 4. The cutover is gated on durable per-host state

Adding the template stanzas is the one genuinely dangerous step: `error_on_missing_key = true` plus
`exit_on_retry_failure = true` mean a stanza pointing at an unpopulated KV field **exits the whole
agent**, taking `~/.git-credentials` rendering down with it on every host that receives the config.

So ownership is a durable marker file, `/etc/openbao-agent/renders-kubeconfigs`, written only after
that host reads both fields *under its own AppRole identity* and confirms each parses as a kubeconfig
pointing at the expected server with >72h of token life. Both the agent config template and
`tep.yml`'s SOPS writer read that same marker, so the two writers of `~/.tep/kubeconfig` are mutually
exclusive **by construction** — an in-play ansible fact would be re-derived by a later tag-limited run
and could hand the file back while the agent still owns it.

The role flushes handlers before verifying (ansible queues them to end-of-play, so a check written
after the template task would inspect the old, healthy process and pass regardless), requires the
running process to be newer than `agent.hcl`, and requires each kubeconfig to *authenticate* — mode
and existence alone would accept the stale SOPS-written file at the same path. On failure it restores
the previous config, removes the marker so the SOPS writer resumes, and stops the rollout.

### 5. Credential tiering, applied

"Put the credentials in OpenBao so agents can read them" cannot be applied uniformly:

- **Tier A — agent-readable** (`af/dev-workers/*`). Namespace-scoped k8s tokens, Gitea PATs. This
  tier *widens* here: a worker gains deploy authority it did not have. It does not gain reach into
  another worker's namespace, any estate credential, or anything cluster-scoped.
- **Tier B — operator escrow only** (`af/estate/*`). The vault policy is not the whole boundary:
  Flux decrypts the seeds into a live Secret in ns `openbao`, so the real boundary is the union of
  the vault policy, Secret-read RBAC there, and the root-capable vault logins.
- **Tier C — never in the vault.** The SOPS age key, the unseal key, the breakglass token — and, new
  here, **the talos-backup age key**: it decrypts etcd snapshots, which contain this vault's own
  breakglass token and these very seeds. The general rule: *a credential that decrypts a backup of
  the boundary is classified by what the backup contains, not by what the credential is for.*

The `admin@ai` cluster-admin kubeconfig is Tier B and is **not escrowed at all** — it is reproducible
via `talosctl kubeconfig`, so escrowing it would add a standing copy of cluster-admin without adding
recoverability.

## Consequences

- **A dev-worker can now deploy.** Bounded to one namespace, under `restricted`, with no
  cluster-scoped rights and no escalation hatch. `claude-grant-write` is removed (`state: absent`,
  not merely un-installed, or it would linger on every existing worker).
- **Charts that are not `restricted`-compatible do not work here**, and neither do charts shipping
  CRDs, ClusterRoles, or a Role granting permissions the deployer lacks (`events`, `endpoints`,
  `leases` — the API server's escalation check rejects those outright). `docs/runbooks/helmtest.md`
  documents the supported profile and the pre-flight. The fallback — a nested cluster inside a leased
  Kata sandbox — **does not exist yet** and is named as a follow-up rather than implied.
- **`ansible/secrets/tep-tokens.sops.yaml` and `scripts/tep-render-kubeconfigs.py` are deleted** at
  the end of Phase 3, and the legacy `tep-dw<N>-token` Secrets are retired per worker **as a git
  change** to `tep-access.yaml` — deleting the live object alone lets Flux recreate it.
- **`dev_worker_enable_openbao: false` stops provisioning kubeconfigs at all** once Phase 3
  completes. The earlier draft promised that mode keeps working *and* deleted the SOPS assets it
  depends on; both cannot hold. The flag reverts to what ADR 0020 made it — a staged-rollout switch,
  not a supported steady state.
- **A fourth seed precedence exists** and post-wipe recovery gains an ordering constraint (vault +
  auth role → sync → AppRole re-mint → restart agents). A daily eventual retry is not sufficient for
  fail-exiting agents.
- **The estate escrow grew by three paths and shrank by three candidates.** The reconciliation that
  produced that also found `~/work/keys/talos-backup-age.key` is **orphaned** — its public key is not
  the recipient `talos-backup/cronjob.yaml` encrypts to. Treating it as the DR key would have been
  false confidence.

## Follow-ups

- **A nested-cluster path for charts `helmtest` cannot run** (k3d inside a leased Kata sandbox).
- **Re-grant `ingresses`** behind a Kyverno policy constraining `ingressClassName` and host suffix.
- **Per-worker Gitea PATs** (ADR 0020's outstanding follow-up). Until then `af/dev-workers/common`
  still holds one shared PAT and one shared hypervisor root key, so per-worker Helm namespaces do not
  by themselves make the six workers independent end to end.
- **Alerting on minimum remaining token lifetime**, not just on sync success: a sync that runs daily
  but mints short-lived tokens is a healthy-looking outage. The fail-closed floor covers the
  dangerous case today; a metric would surface it earlier.
- **Resolve the orphaned talos-backup key** — determine whether any retained snapshot was encrypted
  to it before deleting, and verify an offline copy of the live one.
