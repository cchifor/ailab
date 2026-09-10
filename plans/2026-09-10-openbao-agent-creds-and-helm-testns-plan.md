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
- Never escrowed anywhere: `~/work/keys/talos-backup-age.key`, `nextcloud-restic-password.txt`,
  `rclone-crypt-escrow.txt`, `platform.env` (Hatchet keysets, SendGrid, OpenAI + Anthropic API
  keys), `~/work/keys.txt` (gcloud + GitHub OAuth).
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
  Namespace-scoped k8s tokens, Gitea PATs. Blast radius of a worker compromise is already this set.
- **Tier B — operator escrow only** (`af/estate/*`; no policy grants read to any AppRole or ESO).
  The unescrowed workstation credentials above.
- **Tier C — must never enter the vault at all.** The SOPS age master key, the unseal key, the
  breakglass token. The age key in particular is chicken-and-egg: it decrypts the seed files that
  populate the vault *and* `ansible/secrets/dev-worker.sops.yaml`, which holds every worker's
  AppRole `secret_id`.

The `admin@ai` cluster-admin kubeconfig is Tier B, never Tier A. Putting it where a dev-worker
AppRole can read it would convert any single-worker compromise into full cluster-admin — on top of
the root-on-three-hypervisors that the shared `proxmox_ssh_key` already grants.

## Approach

Five phases. Phases 1–3 are independent of 4–5 and can land as separate PRs.

### Phase 1 — `helmtest-dw<N>`: six isolated, Helm-capable namespaces

New tree `kubernetes/apps/infrastructure/helmtest/`, one namespace per worker so concurrent agents
cannot collide on release names and a runaway chart is contained to one worker's quota.

Per namespace `helmtest-dw1` … `helmtest-dw6`:

- **Namespace** with Pod Security Admission **enforced at `restricted`**
  (`pod-security.kubernetes.io/enforce: restricted`, `enforce-version: v1.30`, plus `warn`/`audit`
  at the same level). This is the single most important control in the phase: `create pods` in a
  namespace without PSA is a node-compromise primitive (hostPath, privileged, hostNetwork). Charts
  that need more than `restricted` are out of scope — they belong in a leased Kata sandbox.
- **ResourceQuota** (starting point, tunable): `requests.cpu: "4"`, `requests.memory: 8Gi`,
  `limits.cpu: "8"`, `limits.memory: 16Gi`, `pods: "30"`, `count/persistentvolumeclaims: "10"`,
  `requests.storage: 50Gi`, `count/services.loadbalancers: "0"`, `count/services.nodeports: "0"`.
  The last two matter: a NodePort or LoadBalancer from a test chart is an estate-wide exposure
  change made without a git commit.
- **LimitRange** with default requests/limits, so a chart that omits them cannot evade the quota.
- **NetworkPolicy** default-deny both directions, plus a CiliumNetworkPolicy mirroring the
  `agentforge-sandbox` posture: allow kube-dns and the in-cluster registry, `egressDeny` on
  `world`, `host`, `remote-node`, `169.254.169.254/32`, `169.254.0.0/16`, `::/0`. A test deployment
  has no business reaching the LAN, the node IPs, or — critically — `openbao-lan:30820`.
- **ServiceAccount** `helmtest-dw<N>` + a `kubernetes.io/service-account-token` Secret
  `helmtest-dw<N>-token`, the same shape `tep-access.yaml` already uses.
- **Role** `helmtest-deployer` + RoleBinding, granting `["*"]` verbs **only** on the namespaced
  resources Helm charts actually create: `secrets` (Helm 3 release state), `configmaps`,
  `services`, `serviceaccounts`, `persistentvolumeclaims`, `pods`, `pods/log`, `pods/exec`,
  `endpoints`, plus `apps` (deployments, statefulsets, daemonsets, replicasets), `batch` (jobs,
  cronjobs), `networking.k8s.io` (ingresses, networkpolicies), `policy`
  (poddisruptionbudgets), `autoscaling` (horizontalpodautoscalers), and
  `rbac.authorization.k8s.io` (roles, rolebindings — namespaced only).

Two deliberate non-grants, both of which need to be documented where an agent will read them:

- **No cluster-scoped rights, so no CRD installation.** `helm install` of a chart with a `crds/`
  directory will fail. This is intentional — a CRD is a cluster-wide schema change.
- **No `escalate` / `bind` verb.** Kubernetes' built-in RBAC escalation check already confines
  `create rolebindings` to permissions the grantor holds, so the `rbac` grant above cannot exceed
  `helmtest-deployer` itself. Worth stating explicitly because it looks scarier than it is.

### Phase 2 — an in-cluster token sync, replacing the operator ceremony

New CronJob `openbao-k8stoken-sync` (ns `openbao`, daily, plus a Job for immediate first run). It
reads the twelve SA token Secrets (`testpool/tep-dw<N>-token`, `helmtest-dw<N>/helmtest-dw<N>-token`)
and writes a **fully rendered kubeconfig** into each worker's own KV path:

```
af/dev-workers/dev-worker-<N>
  tep_kubeconfig       # ns testpool,       SA tep-dw<N>
  helmtest_kubeconfig  # ns helmtest-dw<N>, SA helmtest-dw<N>
```

The per-worker subtree is where ADR 0020 already says per-worker material belongs, and the existing
per-worker policy already grants `read` on `af/data/dev-workers/<host>` and `/*` — **no policy
change is required**.

Storing the rendered kubeconfig rather than the raw token keeps the `bao agent` template a
one-liner and keeps the CA bundle next to the token it authenticates.

RBAC for the CronJob: a Role in `testpool` and one per `helmtest-dw<N>`, each granting `get` on
**only** the named token Secret — not `list`, not namespace-wide `get`.

**These two fields are deliberately NOT added to `devworker-seeds.sops.yaml`.** The seed contract
in this subtree is *seed-wins on every daily run*, so a seeded copy would fight the sync job and
revert to a stale token. They are cluster-derived state: after an OpenBao wipe the sync job
repopulates them on its next run with no operator action, which is strictly better than the seed
path. `docs/runbooks/openbao-recovery.md` § path classes gains a row saying so.

### Phase 3 — `bao agent` renders both kubeconfigs; retire the SOPS channel

Add two `template` stanzas per user to `roles/dev_worker/templates/openbao-agent.hcl.j2`, alongside
the existing `git-credentials` one, with matching `.ctmpl` files:

| ctmpl source | destination | perms |
|---|---|---|
| `tep-kubeconfig.ctmpl` | `{{ user.home }}/.tep/kubeconfig` | 0600, chowned to the user |
| `helmtest-kubeconfig.ctmpl` | `{{ user.home }}/.helmtest/kubeconfig` | 0600, chowned to the user |

**This is the hazardous step and it must be sequenced, not merged-and-hoped.** The agent config
sets `error_on_missing_key = true` and `template_config.exit_on_retry_failure = true`. If a
template references a KV field that does not exist yet, the **whole agent exits** — taking
`~/.git-credentials` rendering down with it on every worker it reaches. Sequencing:

1. Merge Phases 1–2. Force the sync Job. Verify with
   `cred get dev-worker-1 helmtest_kubeconfig | wc -c` on one worker — length only, never the value.
2. Only then merge Phase 3, and roll it with `-l dev-worker-1` first.
3. After that worker verifies, roll the remaining five.

Then retire the old channel, in this order and not before step 3 is green:

- Delete `ansible/secrets/tep-tokens.sops.yaml`, `scripts/tep-render-kubeconfigs.py`,
  `roles/dev_worker/templates/tep-kubeconfig.j2`, and the token-rendering half of
  `roles/dev_worker/tasks/tep.yml` (the `tep` CLI install and the `~/.tep` directory stay).
- Rotate the six `tep-dw<N>` tokens afterwards (delete + recreate the token Secrets, let the sync
  job repopulate). The old values were in a SOPS file and on six disks; treat them as burned.

Update the managed `~/.claude/CLAUDE.md` block so agents are told the new path:
`helm --kubeconfig ~/.helmtest/kubeconfig …`, namespace already current-context.

### Phase 4 — Tier B: escrow the unescrowed workstation credentials

Extend `af/estate/*` with the credentials that today exist **only** on the operator workstation.
New paths, each a `<name>.json` key in `estate-seeds.sops.yaml`:

| Path | Fields | Source file |
|---|---|---|
| `af/estate/talos-backup` | `age_key` | `~/work/keys/talos-backup-age.key` |
| `af/estate/restic` | `nextcloud_password` | `~/work/keys/nextcloud-restic-password.txt` |
| `af/estate/rclone` | `crypt_escrow` | `~/work/keys/rclone-crypt-escrow.txt` |
| `af/estate/platform` | `hatchet_encryption_master_keyset`, `hatchet_jwt_public_keyset`, `hatchet_jwt_private_keyset`, `hatchet_client_token`, `sendgrid_key`, `openai_api_key`, `anthropic_api_key` | `~/work/keys/platform.env` |
| `af/estate/oauth` | `gcloud_client_secret`, `github_client_secret` | `~/work/keys.txt` |

Every one of these is Tier B: **no policy grant is added**, so `cred` returns a permission error on
them by design, exactly as it does for `af/estate/proxmox` today.

Two couplings this repo enforces, both of which must move in the same commit:

- Each new `path:field` pair goes into the **completeness matrix** in `estate-provision-job.yaml`
  (the `for pair in \` list). The Job fails closed on any matrix entry a seed did not restore, so a
  seed without a matrix row is a silent post-wipe gap and a matrix row without a seed is a red Job.
- The multi-home table in `docs/runbooks/openbao-estate-credentials.md` gains a row per path.

### Phase 5 — workstation cleanup (operator ceremony, documented not automated)

Once Phase 4 is verified in the vault:

- Delete `~/work/keys/kubeconfig.txt` — a redundant 0644 copy of cluster-admin; `~/.kube/config`
  already holds it.
- Delete `~/work/keys/age.agekey` — redundant with `kubernetes/infra/_out/age.agekey`, and outside
  any repo so no `.gitignore` protects it.
- `chmod 600 ~/.kube/config ~/.kube/ailab.config ~/.git-credentials ~/.gitea_tok
  ~/.cc_gitea_issue_token` and delete the stale `~/.gitea_cred_tmp` and `~/.cutover_*` scratch files.
- Reconcile the two **different** Gitea tokens in `~/.gitea_tok` and `~/.cc_gitea_issue_token`
  (hashes differ — `bdeea7df…` vs `3b19e2e6…`); keep one, revoke the other in Gitea.
- Delete `.env`'s `SSO_PASSWORD` rather than escrowing it —
  `docs/runbooks/openbao-estate-credentials.md` already records that it has zero consumers.

## Critical files

| Path | Role |
|---|---|
| `kubernetes/apps/infrastructure/helmtest/` *(new)* | Namespaces, PSA labels, quotas, LimitRanges, NetworkPolicy/CNP, SAs, token Secrets, Role + RoleBinding |
| `kubernetes/apps/clusters/ai/helmtest.yaml` *(new)* | Flux Kustomization wiring the tree in, mirroring `testpool.yaml` |
| `kubernetes/apps/infrastructure/security/openbao/k8stoken-sync.yaml` *(new)* | CronJob + Job + SA + per-namespace Roles/RoleBindings for the token sync |
| `ansible/roles/dev_worker/templates/openbao-agent.hcl.j2` | Two new `template` stanzas per user |
| `ansible/roles/dev_worker/templates/{tep,helmtest}-kubeconfig.ctmpl.j2` *(new)* | Consul-template sources reading the per-worker KV path |
| `ansible/roles/dev_worker/tasks/tep.yml` | Drop the SOPS token load + `tep-kubeconfig.j2` render; keep the CLI install |
| `ansible/secrets/tep-tokens.sops.yaml`, `scripts/tep-render-kubeconfigs.py` | **Deleted** at the end of Phase 3 |
| `kubernetes/apps/infrastructure/security/openbao/estate-seeds.sops.yaml` | Five new `<name>.json` keys |
| `kubernetes/apps/infrastructure/security/openbao/estate-provision-job.yaml` | Completeness-matrix rows for every new `path:field` |
| `docs/decisions/0021-agent-credential-plane-and-helm-testns.md` *(new)* | ADR: the A/B/C tiering, per-worker namespaces, sync-job-not-seed |
| `docs/runbooks/openbao-dev-workers.md`, `openbao-estate-credentials.md`, `openbao-recovery.md` | New KV fields, new estate rows, new path class for sync-owned fields |
| `kubernetes/apps/infrastructure/testpool/README.md` | Point at `helmtest` for deploys; state that `testpool` stays lease-only |

## Verification

**Phase 1 — RBAC is exactly as wide as intended, and no wider.**

```bash
SA=system:serviceaccount:helmtest-dw1:helmtest-dw1
for r in secrets deployments services configmaps pods jobs ingresses; do
  echo "$r: $(kubectl --context admin@ai auth can-i create $r -n helmtest-dw1 --as=$SA)"   # all yes
done
# must all be NO — the containment boundary:
kubectl --context admin@ai auth can-i create pods -n testpool --as=$SA                     # no
kubectl --context admin@ai auth can-i create namespaces --as=$SA                           # no
kubectl --context admin@ai auth can-i create customresourcedefinitions --as=$SA            # no
kubectl --context admin@ai auth can-i '*' '*' --all-namespaces --as=$SA                    # no
kubectl --context admin@ai auth can-i get secrets -n openbao --as=$SA                      # no
```

PSA actually enforcing, not just labelled:

```bash
kubectl --context admin@ai -n helmtest-dw1 run psa-probe --image=busybox --restart=Never \
  --overrides='{"spec":{"containers":[{"name":"c","image":"busybox","securityContext":{"privileged":true}}]}}'
# must be REJECTED by the admission webhook, not created
```

**Phase 2 — the sync populated KV without printing anything.**

```bash
kubectl --context admin@ai -n openbao create job --from=cronjob/openbao-k8stoken-sync sync-now
kubectl --context admin@ai -n openbao logs job/sync-now | tail -3     # counts only, never values
# on a worker:
cred get dev-worker-1 helmtest_kubeconfig | wc -c                     # non-zero length, no value
```

**Phase 3 — end-to-end, the actual thing the user asked for.**

```bash
ssh c4@192.168.0.8
systemctl status openbao-agent                       # active, no restart loop — the fail-closed check
ls -l ~/.helmtest/kubeconfig                         # 0600, owned by the agent user
helm --kubeconfig ~/.helmtest/kubeconfig install smoke oci://registry-1.docker.io/bitnamicharts/nginx \
  --wait --timeout 5m
helm --kubeconfig ~/.helmtest/kubeconfig test smoke
helm --kubeconfig ~/.helmtest/kubeconfig uninstall smoke
git ls-remote https://git.chifor.me/cchifor/ailab.git HEAD >/dev/null && echo "git creds still ok"
```

The last line is not incidental — it proves the new template stanzas did not take the agent down
and strand `~/.git-credentials`.

**Phase 4 — seeds and matrix agree, and Tier B stayed sealed.**

```bash
kubectl --context admin@ai -n openbao delete job openbao-estate-provision
flux --context admin@ai reconcile kustomization openbao -n flux-system
kubectl --context admin@ai -n openbao logs job/openbao-estate-provision | tail -3   # "estate provision complete"
# on a worker — MUST fail with a permission error:
cred get estate/platform openai_api_key ; echo "exit=$? (non-zero expected)"
```

**Idempotency.** Re-run `just dev-workers` twice; the second run reports near-zero `changed`
(the role's standing contract).

## Rejected

- **Widening `tep-worker` in `testpool`.** Granting pod-create there lets an agent schedule a plain
  pod alongside the Kata-isolated leases, bypassing the sandbox boundary the whole pool exists to
  provide. The lease namespace stays lease-only.
- **One shared `helmtest` namespace.** Cheaper, but six agents share a release-name collision domain
  and one runaway chart's quota. Per-worker is the same manifest generated six times.
- **Provisioning `/etc/claude-agent/kube-rw-config` to make `claude-grant-write` work.** That path
  hands out `edit` on the *whole cluster*, gated only by a sudo prompt an agent can be talked into.
  A namespace-scoped credential with no escalation hatch is the correct shape; `claude-grant-write`
  should be removed from `k8s_tools.yml` in Phase 3 rather than fixed.
- **Keeping the tep kubeconfig in SOPS and only adding helmtest to OpenBao.** Two credentials of the
  same kind on two channels is how rotation split-brains start; the audit already found several.
- **Putting the age key or the `admin@ai` kubeconfig in `af/dev-workers/*`.** See the tiering note.
- **Bound (projected) SA tokens instead of legacy token Secrets.** Correct long-term, but they
  expire and nothing on a worker renews them without a broker — the same problem ADR 0020 solved for
  vault tokens with periodic auth. Legacy tokens match what `tep-access.yaml` already does; the
  broker is a follow-up.

<!-- codex-review-status: pending -->
