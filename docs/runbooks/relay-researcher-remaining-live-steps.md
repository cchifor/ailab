# Relay researcher: remaining live steps before the first qualification run

Status: **checklist, updated 2026-11-20. Steps 1-3 are DONE; resume at step 4 after the readbacks in steps 0-3 pass.**
Merging this file changes nothing live. It puts in execution order
the owner and operator live steps that are still open between today's state and lifting the hold on
hub step H6 (ailab #1811). It does not replace the runbooks it cites. Each step is an operator action on
the cluster, OpenBao, the budget ledger or the Router admin API, run only with the owner's direct
confirmation for that step.

Where a runbook gives a command, it is copied verbatim. A command that the runbooks leave unspecified is
marked **NEW (not in the runbook)**; the owner reviews those before use. Values that git cannot give
are `<operator fills: …>` placeholders, each saying where to read the value.

**How to read the citations.** Every step names its source by runbook and section or step number (for
example "Router connection step 3", or the budget runbook's "Changing the witness"), and the section
is the anchor. The `file:line` ranges point to the exact lines, as they stand in the commit that
merges this file: ailab `main` `bce1a51b` plus this PR's one link sentence in the hub runbook.

Later edits to those runbooks will move the lines; the open H6 PR (#1811), for one, shifts the hub
runbook's later lines by a few. To read a cited range after that, use
`git show <the commit that merged this file>:docs/runbooks/<file>`, or follow the named section.

## Starting state

Operator readbacks of 2026-11-16, updated for the completed steps 1-2. Step 0 checks them again;
the other live values below still require that readback.

- **Hub.** Relay v0.10.4 (`e77bd09a`), schema 79. H1-H3 and H4a are applied (H4a: ailab #1805, rolled
  17:33Z). H6 is ailab #1811, held (label `no-automerge`).
- **Router.** Budget step 8's reader inclusion is applied (ailab #1807, rolled 17:16Z) with
  `ROUTER_WORKER_BUDGET_DEPLOYMENT_DIGEST=547353ac649b454646cad921598d8e231c5e9db41c8e99b028351969e1a261ad`.
  The `worker-budget-authority` plugin is disabled. The relay-keys trust component
  (`kubernetes/components/llm-router-relay-keys-trust`) is included by
  `kubernetes/apps/apps/llm-router/kustomization.yaml` (step 2 DONE).
- **Installation releases.** The agent is v0.10.6 (`1599d06e`), pinned by `agentRelease` in
  `docs/relay-workers/researcher-binding-inputs.json`; runtime and binding remain v0.10.3 (`c0fe50f0`).
  The agent-only update does not change the hub's v0.10.4 pin.
- **Ledger.** One `worker_budget.authority` row, `ready=f`. Budget steps 1-7 File A are done: the
  `clients` and `readers` rows (#1753 comment 124073). There is no `qualifications` row: File B comes
  later.
- **Witness.** The offline record's latest entry is the initial witness A. It is
  `af/relay-budget/witness` version 1, sha256
  `8e5a41044815b67975b794d9d82dc3535bd900fbd9e74d1f4c0c05611c13527b` (#1753 comment 123957). The
  Router and Relay Pods both mounted A when they rolled (#1807 and #1805).

  Both components mount `witness.json` as a `subPath` file: `subPath: witness.json` in
  `kubernetes/components/llm-router-worker-budget-reader/deployment.yaml` and
  `kubernetes/components/relay-worker-budget-client/deployment.yaml`. A `subPath` file never updates
  in a running Pod (`relay-worker-budget-authority.md:376-378`), so a Pod's mounted witness is the one
  from its own start until it is rolled. Every "mounted" readback below relies on that.
- **OpenBao.** Step 1's signer and trust documents exist. ExternalSecret
  `llm-router/llm-router-relay-trust` is Ready. `relay/relay-router-files` remains
  `SecretSyncedError` until step 9 supplies the escrow token.
- **Collector Secrets.** There is no `relay-control-metrics` Secret in any namespace, and no
  `monitoring/relay-control-collector`.

## Rules for every step

- **Context.** Use `kubectl --context admin@ai` only. The default context is a different cluster.
- **One step at a time.** Run the pre-checks, then the commands, then the readback. Any failed
  pre-check or readback stops the sequence. Nothing later runs until the owner has decided.
- **The session.** Steps may be days apart. Every step runs in a bash (4.4 or later) shell where
  step 0's command block has been run. In a new shell, run that block again first: it defines the
  helpers and changes nothing live. The helpers set their own shell options, so their failure checks
  do not depend on how the shell was started.
- **Secrets move in pipes only.** They never appear in argv, a transcript, a log, a PR or an issue.
  Use the style of installation runbook steps I3 and I8 (`relay-researcher-installation.md:295-315`
  and `:418-432`):
  - `bao kv get … | kubectl create secret … --from-file=x=/dev/stdin`;
  - `openssl … | bao kv put … key=-`.

  A file that a runbook command needs goes in a private directory (`umask 077; d="$(mktemp -d)"`).
  Remove it with `shred -u` as soon as it has been used.
- **Shell.** Turn off tracing and history for the session (budget step 5 asks for this,
  `relay-worker-budget-authority.md:169-171`). Never run a plain `bao token lookup`: its output
  includes the token itself. Step 0 has a filtered form.
- **Ledger writes.** They run as OS and database user `postgres` over the ledger's local socket, with
  `SET LOCAL ROLE relay_budget_owner`, in one transaction. `relay_budget_owner` is `NOLOGIN`. The form
  is the step-7 one (#1753 comment 124071 §4.1): a write succeeded only if psql exits 0 and its output
  has a literal `COMMIT` line. Step 0 defines `lwrite` for that.
- **Evidence.** Keep only readbacks that hold no secret: names, keys, hashes, accessors, row values and
  timestamps. Never DSNs, passwords, tokens or private keys. Post each drill's evidence on ailab #1753,
  as the budget runbook's "Evidence" says (`relay-worker-budget-authority.md:467-481`). The other
  steps' evidence goes in the operator evidence, as their runbooks say.

**Tags.** Each step has a **Writes** line, naming any write to OpenBao, the budget ledger, Kubernetes
objects or the Router admin API, and a **Restarts** line, naming every rollout. A `force-sync`
annotation is a Kubernetes write, and ESO then re-renders the ExternalSecret's Secret.
For DONE steps 1-3, the Writes/Restarts in the step bodies are historical; only the readbacks are current.

**Owner decisions come first.** The decisions below are recorded in
[ailab #1820](https://git.chifor.me/cchifor/ailab/issues/1820) and repeated at the end of this file.
Their required readbacks and per-step confirmations still apply:

| Step | Needs |
| --- | --- |
| 2 | point 6 |
| 3 | points 1 and 2 |
| 5 | points 3 and 4 |
| 6 | points 7 and 8 |
| 7 | point 7 |
| 8 | point 5 |

Each of those steps repeats the requirement as a pre-check.

| # | Step | Source | Writes | Restarts |
| --- | --- | --- | --- | --- |
| 0 | Session, helpers, baseline | (this checklist) | none | none |
| 1 | **DONE**: Signer keys and the Router trust document (H5, first half) | Router connection step 3 | completed; readback only now | none |
| 2 | **DONE**: Router trust inclusion | Router connection step 6 | completed; readback only now | none |
| 3 | **DONE**: Control-metrics collector Secrets | control monitoring, activation step 2 | completed; readback only now | none |
| 4 | Budget step 8 proofs | budget step 8 | **Kubernetes**: one temporary client Pod | none |
| 5 | Witness drills A-D | budget, "Drills before production `ready=true`" | **OpenBao**: witness versions; **ledger**: the rotation to B and the `ready` flips; **Router admin API**: plugin on and off; **Kubernetes**: ExternalSecret annotations, Deployment restarts, the client Pod's deletion | **Router**, 8 times |
| 6 | Durability evidence (6a), then the independent review (6b) | budget step 8; budget, "Durability evidence for the first `ready=true`" | **Kubernetes**: the disposable probe namespace and its objects, created and deleted; the ledger Pod's deletion. With the owner's OK only: a `qm reset` of the probe's node (tier b), a 60 s host firewall rule (tier c) | **the ledger Pod**, once (`OnDelete`); the probe's node VM if tier (b) runs |
| 7 | `worker_budget.authority.ready=true` | budget step 8 | **ledger** | none |
| 8 | Router plugin enabled (after the bootstrap gates) | budget step 9 | **Router admin API** | none |
| 9 | Escrow token (H5, second half); `relay-router-openbao-tls` | Router connection step 9 | **OpenBao**: 1 document and 1 token; **Kubernetes**: 1 ExternalSecret annotation | none |
| 10 | H6 precondition readback | hub H6 | none | none |

## Step 0. Session, helpers, baseline

**Purpose.** Set up a session that leaves no secret behind, define the helpers that later steps use,
and confirm the starting state.

**Writes:** none. **Restarts:** none.

**Commands.** All of this block is NEW (not in the runbook), except where a line says otherwise.

```sh
set +x; set +o history; set -o pipefail; umask 077   # pipefail: a failed read anywhere in a pipe fails the pipe
ev='<operator fills: a private evidence directory, outside any synced or backed-up share>'
mkdir -p -m 0700 "$ev" "$ev/cap"
export BAO_ADDR='<operator fills: the OpenBao address used for budget steps 5-6>'
# Enter the operator's OpenBao token as for budget steps 5-6, without echo and never on a command line.
bao token lookup -format=json | jq '.data | {display_name, policies, expire_time}'   # never print .data.id

# capfile <label>: sets CAPF to a new evidence file name. A session counter makes every name unique,
# so nothing is overwritten. (It sets a variable rather than printing, so the counter survives.)
capfile() { CAPN=$(( ${CAPN:-0} + 1 )); CAPF="$(printf '%s/cap/%05d-%s-%s' "$ev" "$CAPN" "$(date -u +%Y%m%dT%H%M%SZ)" "$1")"; }
# cap <label> <command...>: run a command, keep its output under $ev/cap, and return the command's own status.
cap() { local -; set -o pipefail; capfile "$1"; f="$CAPF.txt"; shift; { date -u +%FT%TZ; "$@"; } 2>&1 | tee "$f"; }
# Router admin API call. The bearer reaches curl on stdin (-H @-, curl 7.55 or later). No -f, so error bodies stay visible.
radmin() {
  local -; set -o pipefail
  <operator fills: a command that prints the Router admin bearer (the ROUTER_ADMIN_TOKEN value) from the operator's credential store> \
    | sed 's/^/Authorization: Bearer /' | curl -sS -H @- "$@"
}
# SQL on stdin, as postgres over the ledger's local socket (the session of budget step 5 and #1753 comment 124071).
lsql() {
  kubectl --context admin@ai -n relay-budget-authority exec -i relay-budget-postgres-0 -- \
    env PSQL_HISTORY=/dev/null psql -X -v ON_ERROR_STOP=1 -h /var/run/postgresql -U postgres -d relay_budget -f -
}
# A ledger write from "$ev/<name>.sql", output kept in a new evidence file each time.
# Prints COMMITTED only on psql exit 0 plus a literal COMMIT line (124071 §4.1).
lwrite() {
  local -; set -o pipefail
  capfile "$1"; out="$CAPF.out"
  lsql < "$ev/$1.sql" 2>&1 | tee "$out"; rc=$?
  if [ "$rc" -eq 0 ] && grep -qx 'COMMIT' "$out"; then echo "$1 COMMITTED ($out)"; else echo "$1 NOT COMMITTED (rc=$rc, $out)"; return 1; fi
}
# The witness's sha256 in OpenBao, in each ExternalSecret-rendered Secret, and as mounted in each Pod.
# Each fails (non-zero) when a read fails, so a failed read can never look like the empty witness's hash.
# They set pipefail themselves (local -, bash 4.4+), so this holds in any shell.
# An empty witness.json value is legitimate (drill B); a missing key is an error.
wsha_bao()    { local -; set -o pipefail; bao kv get -mount=af -field=witness relay-budget/witness | sha256sum; }
wsha_secret() { local -; set -o pipefail; kubectl --context admin@ai -n "$1" get secret "$2" -o json \
                  | jq -er '.data["witness.json"] // error("witness.json missing")' | base64 -d | sha256sum; }
wsha_router() { kubectl --context admin@ai -n llm-router exec deploy/llm-router -c router -- sha256sum /run/llm-router-worker-budget/witness.json; }
wsha_relay()  { kubectl --context admin@ai -n relay exec deploy/relay -c relay -- sha256sum /run/relay-worker-budget/witness.json; }
# plugins: the two plugins' states. Fails loudly if the answer is not the documented array of PluginInfo.
plugins() {
  local -; set -o pipefail
  radmin https://router.chifor.me/admin/v1/plugins | jq -ec 'if type == "array" then (.[] | select(.id=="worker-budget-authority" or .id=="relay-keys") | {id, state, error}) else error("plugins: not an array") end'
}
# The Router trust document as ESO rendered it (public keys only).
trust() { local -; set -o pipefail; kubectl --context admin@ai -n llm-router get secret llm-router-relay-trust -o json | jq -er '.data["trust.json"]' | base64 -d; }
```

**Baseline readback.** This block is read-only. All of it is NEW except the two `bao` lines from
Router connection step 1 (`relay-router-connection.md:255-260`).

```sh
kubectl --context admin@ai -n relay get externalsecret relay-router-files relay-worker-budget
kubectl --context admin@ai -n llm-router get externalsecret llm-router-relay-trust llm-router-worker-budget
kubectl --context admin@ai -n relay get secretstore relay-router-store
kubectl --context admin@ai -n llm-router get secretstore router-relay-keys-store
bao kv list -mount=af relay-router
kubectl --context admin@ai get secret -A --field-selector metadata.name=relay-control-metrics
kubectl --context admin@ai -n monitoring get secret relay-control-collector
bao policy read relay-router-escrow
bao read auth/token/roles/relay-router-escrow
plugins
kubectl --context admin@ai -n llm-router get deploy llm-router -o json \
  | jq -r '.spec.template.spec.containers[] | select(.name=="router") | .env[] | select(.name=="ROUTER_WORKER_BUDGET_DEPLOYMENT_DIGEST") | .value'
lsql <<'SQL'
SELECT singleton, ready, instance_id, recovery_epoch, witness_digest FROM worker_budget.authority;
SELECT login, issuer, connection_id, deployment_digest, tenant_ids, max_tokens, max_requests, max_authority_ms FROM worker_budget.clients;
SELECT login, deployment_digest FROM worker_budget.readers;
SELECT (SELECT count(*) FROM worker_budget.qualifications) AS qualifications,
       (SELECT count(*) FROM worker_budget.allocations) AS allocations,
       (SELECT count(*) FROM worker_budget.outbox) AS outbox;
SQL
wsha_bao; wsha_secret relay relay-worker-budget; wsha_secret llm-router llm-router-worker-budget; wsha_router; wsha_relay
```

**Expected.**

- **ExternalSecrets.** `relay-router-files` is `SecretSyncedError` (escrow token still missing).
  `llm-router-relay-trust` is Ready. Both `*-worker-budget` ExternalSecrets are
  `SecretSynced`, and both SecretStores are Ready.
- **OpenBao.** `af/relay-router` lists `relay/` and `router/`; step 1's signer/trust readbacks hold.
- **Collector Secrets.** Both exist (step 3 DONE): `relay/relay-control-metrics` with keys
  `expires-at`, `tenant-id`, `token-sha256`, and `monitoring/relay-control-collector` with key `token`.
- **Provision objects.** The policy and the token role read back exactly as Router connection step 1
  says: `allowed_policies [relay-router-escrow]`, `orphan true`, `renewable false`,
  `token_no_default_policy true`, `token_explicit_max_ttl 720h`.
- **Router.** `worker-budget-authority` is `disabled`; `relay-keys` is `active`. The digest is
  `547353ac…1a261ad`.
- **Ledger.**
  - Authority: one row, `singleton=t`, `ready=f`. Its IDs equal the record's latest entry (A).
  - `clients`: `relay_budget_allocator | relay.chifor.me | babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8 |
    547353ac… | {00000000-0000-4000-8000-000000000001} | 7441536 | 8 | 120000`.
  - `readers`: `relay_budget_router | 547353ac…`.
  - Counts: 0 qualifications, 0 allocations, 0 outbox rows.
- **Witness.** All five hashes are `8e5a4104…527b`.

**Stop.** Any difference from the expected state.

## Step 1. Signer keys and the Router trust document (H5, first half)

**DONE.** Keep the commands below as the original ceremony record; do not generate or overwrite keys
again. Recheck the public fingerprints and rendered trust. Resume at step 3 after the readbacks in
steps 0-2 pass.

**Purpose.** Create the two Ed25519 signer keys that Relay uses, and the document that gives the Router
their public keys (Router connection step 3, `relay-router-connection.md:219-231`, readback
`:263-274`). The hub runbook's H5 (`relay-researcher-hub-upgrade.md:312-316`) names only the escrow
token, because it assumed this step was already done. It is now complete. The
escrow token is not minted here, because its 720 h lifetime starts when it is minted. It is step 9.

**Writes (historical; completed):** **OpenBao**, three new documents in KV-v2 mount `af`:

- `relay-router/relay/admin-signer` (field `private_key`);
- `relay-router/relay/reconciler-signer` (field `private_key`);
- `relay-router/router/trust` (fields `admin_public_key` and `reconciler_public_key`).

Also a historical **Kubernetes** write: one `force-sync` annotation on ExternalSecret
`llm-router/llm-router-relay-trust`.

**Restarts (historical):** none.

**Original pre-checks (before the completed ceremony).**

- Before the initial key creation, `bao kv list -mount=af relay-router` was empty.
- SecretStore `relay/relay-router-store` is Ready, so ESO can reach OpenBao (it uses `ca.crt` of
  `relay-router-openbao-tls`; step 9 has more on that Secret).

**Original commands (completed ceremony).** Verbatim from `relay-router-connection.md:221-231`.
The keys are generated in a private directory and shredded at the end:

```text
# HISTORICAL RECORD, step 1 DONE. DO NOT RUN: it would generate NEW keys and overwrite the live
# signer keys and the Router trust document. Every line is commented out so a paste does nothing.
# umask 077; d="$(mktemp -d)"
# openssl genpkey -algorithm ed25519 -out "$d/admin.pem"
# openssl genpkey -algorithm ed25519 -out "$d/reconciler.pem"
# openssl pkey -in "$d/admin.pem" -pubout -out "$d/admin.pub"
# openssl pkey -in "$d/reconciler.pem" -pubout -out "$d/reconciler.pub"
# bao kv put -mount=af relay-router/relay/admin-signer private_key=@"$d/admin.pem"
# bao kv put -mount=af relay-router/relay/reconciler-signer private_key=@"$d/reconciler.pem"
# bao kv put -mount=af relay-router/router/trust admin_public_key=@"$d/admin.pub" reconciler_public_key=@"$d/reconciler.pub"
# shred -u "$d"/*; rmdir "$d"
```

**Readback.** First verbatim from `relay-router-connection.md:266-270`. It prints no private material:

```sh
for r in admin reconciler; do
  bao kv get -mount=af -field=private_key relay-router/relay/$r-signer | openssl pkey -pubout -outform DER | sha256sum
  bao kv get -mount=af -field=${r}_public_key relay-router/router/trust | openssl pkey -pubin -outform DER | sha256sum
done
```

Each role's two fingerprints are equal, and the admin and reconciler fingerprints differ. Record both.
Then recheck the rendered trust with the read-only commands below. The ceremony also forced one
ExternalSecret sync, a write that must not be repeated; it is kept here only as a commented record:

```text
# HISTORICAL RECORD, step 1 DONE. DO NOT RUN (a Kubernetes write):
# kubectl --context admin@ai -n llm-router annotate externalsecret llm-router-relay-trust force-sync="$(date +%s)" --overwrite
```

The read-only recheck. This block was NEW (not in the runbook):

```sh
kubectl --context admin@ai -n llm-router wait --for=condition=Ready externalsecret/llm-router-relay-trust --timeout=3m
kubectl --context admin@ai -n llm-router get secret llm-router-relay-trust -o json | jq -c '.data | keys'
trust | jq -c '.issuers[0] | {issuer, keys: [.keys[] | {kid, roles}], tenant: .tenants[0].tenant, routes: .tenants[0].routes, workerBudgets: .tenants[0].workerBudgets}'
for k in relay-admin-2026-11 relay-reconciler-2026-11; do
  trust | jq -r --arg k "$k" '.issuers[0].keys[] | select(.kid==$k) | .publicKey' | openssl pkey -pubin -outform DER | sha256sum
done
kubectl --context admin@ai -n relay get externalsecret relay-router-files
```

Expected:

- The wait ends with `condition met`, and the keys are exactly `["trust.json"]`. If the wait times
  out, read the ExternalSecret's status and events before deciding anything: a slow sync is not a
  failed readback.
- The trust has issuer `relay.chifor.me` and the kids `relay-admin-2026-11` (roles `[admin]`) and
  `relay-reconciler-2026-11` (roles `[reconciler]`).
- It has tenant `00000000-0000-4000-8000-000000000001` and routes `["relay-researcher-luna"]`.
- `workerBudgets` is `{"maxTokens":7441536,"maxAuthoritySeconds":120,"maxRequests":8}`.
- The two fingerprints in the trust equal the admin and reconciler values above.
- `relay-router-files` is still not Ready, because there is no `escrow-token` yet.

Router connection step 3 and the template both carry `maxTokens` 7441536 (ailab #1778).

**Original stop and rollback (ceremony record).**

- **A command in the block fails.** Run `shred -u "$d"/*; rmdir "$d"` first, then stop.
- **A fingerprint or content readback is wrong.** `relay-router-files` cannot have rendered the
  private keys yet, because the token is missing. So, with the owner's agreement, delete the three
  documents and repeat the step. The commands are in the Router connection runbook's "Rollback"
  (`:341-342`):
  - `bao kv metadata delete -mount=af relay-router/relay/admin-signer`;
  - the same for `relay-router/relay/reconciler-signer` and `relay-router/router/trust`.

  `bao kv metadata delete` **permanently deletes every version** of a document; nothing restores it.
- **After step 2 has included the trust,** a new key pair needs new kids and a rollout of both sides
  (`:348-349`).

## Step 2. Router trust inclusion

**DONE.** The component is included. Re-run the readback only; the PR instructions below record the
completed inclusion and are not a second rollout. Resume at step 3 after the readbacks in steps 0-2 pass.

**Purpose.** Load the Relay trust into the Router's `relay-keys` plugin. This is Router connection step
6 (`relay-router-connection.md:282-297`), and this is where it belongs in the order:

- **After step 1.** Its document must be Ready, or the Recreate rollout leaves the Router Pod unable to
  mount it.
- **Before Router connection step 7.** Step 7 is budget steps 8-9: `ready=true` after independent
  review, then the plugin. Including the trust first means the drills and the production opening run
  against the final Router configuration.
- **Between File A and File B.** That is where the step-7 plan puts it (#1753 comment 124071 §5).
- **Not part of H6.** The H6 PR (#1811) does not include the trust, and no test requires it there. Its
  body says the trust "rides with budget step 8".

**Writes (historical; completed):** one reviewed PR, which Flux applied to
`Deployment llm-router/llm-router`.

**Restarts (historical; completed):** **the Router** (Recreate: a short outage of all Router traffic).

**Original pre-checks (before the completed inclusion).**

- `llm-router-relay-trust` reads back as in step 1.
- **The `clients` row.** Router connection step 6's readback, verbatim:
  `SELECT login, issuer, connection_id, deployment_digest, tenant_ids, max_tokens, max_requests, max_authority_ms FROM worker_budget.clients;`
  It must equal step 0's row.
- **The route precondition** (`relay-router-connection.md:121-194`). This read is NEW:

  ```sh
  radmin https://router.chifor.me/admin/v1/config | jq -c '.policies[] | select(.alias=="relay-researcher-luna")'
  radmin https://router.chifor.me/admin/v1/config | jq -c '.accounts[] | select(.id=="openai-2") | {id, baseUrl, reasoningEffort}'
  ```

  Expected: the route equals the runbook's entry (one deployment, `relay-researcher-luna-openai-2` on
  `openai-2`, `gpt-6-luna`, `access ["api"]`, `native []`). The account has
  `baseUrl "https://api.openai.com/v1"` and `reasoningEffort "none"`.
- **The input bound.** I = 922,000 was accepted (relay#277 comment 122095), so the trust's `maxTokens`
  is already 7441536 (ailab #1778).
- **The trust ceilings, reviewed and recorded before the PR merges.** The Router connection runbook
  asks for this before step 6 (`relay-router-connection.md:117-119`):
  - `maxLifetimeSeconds` 86400 and `maxKeys` 4, against the researcher pool's lifetime. The
    template's `maxLifetimeSeconds` is 28800
    (`docs/relay-workers/researcher-i4/definition.json`, `binding.templates[0].definition.lifetime`).
  - The pool inference policy's grant (`routes`, `access`, `lifetimeSeconds`) must be a subset of the
    ceiling: routes `["relay-researcher-luna"]`, access `["api"]`, lifetime at most 86400. That grant
    is not in git; it is set in Relay when the pool is created, so the comparison is repeated then.

  The owner records the review on #1753.
- **The `qualifications` rows. Decided 2026-11-17** (open point 6, ailab #1820): the trust is
  included without File B. Router connection step 6 and hub H7 agree: File B comes after H6 and the
  final Router rollout, before any run is signed. H6 rolls Relay, not the Router; use Router readings
  after step 5's rolls or any later Router change when generating File B.

**Original PR (completed inclusion).** Add `- ../../../components/llm-router-relay-keys-trust` under
`components:` in
`kubernetes/apps/apps/llm-router/kustomization.yaml`. Amend `scripts/tests/test_relay_v060_activation_gate.py`
for that component only. Its forbidden list names `llm-router-relay-keys-trust`, and the retired
bridge's name `llm-router-relay-keys` is a substring of it, so the amended check must still refuse the
retired component exactly. `InclusionGate.test_router_component_is_not_live_with_the_token_placeholder`
already passes, because `maxTokens` is the integer 7441536. Hold the PR with `no-automerge` until the
owner gives the go-ahead.

**Readback.** These are the items of Router connection step 6. The command forms are NEW:

```sh
kubectl --context admin@ai -n llm-router rollout status deploy/llm-router --timeout=10m
# An unsigned request with a VALID body: {} would fail validation first (400 INVALID_REQUEST,
# llm-router 5a5181b7 packages/plugins/relay-keys/index.ts and contracts.ts) and prove nothing about the trust.
cap relay-keys-probe curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Content-Type: application/json' \
  -d '{"version":1,"operationId":"operator-probe","action":"capabilities"}' https://router.chifor.me/register/relay-keys
                                                                         # HTTP 403, body code RELAY_DELEGATION_DENIED
plugins                                                                  # relay-keys active; worker-budget-authority still disabled
kubectl --context admin@ai -n llm-router logs deploy/llm-router -c router --since=15m | grep -c 'Invalid RELAY_KEYS_TRUST_FILE'   # 0
kubectl --context admin@ai -n llm-router exec deploy/llm-router -c router -- sha256sum /run/llm-router-relay-keys/trust.json
trust | sha256sum                                                        # equal to the mounted file's
wsha_router                                                              # still A
```

**Original stop and rollback (inclusion record).** Revert the PR (another Router Recreate). Before the
Relay connection exists, reverting an inclusion PR is enough (`relay-router-connection.md:331-332`).

## Step 3. Control-metrics collector Secrets

**DONE.** The two Secrets were created on 2026-11-17 at 10:39:37Z (`kubectl create`, both in the same
second, after step 1) and the token was escrowed at `af/relay-control/collector`. The full readback
passed on 2026-11-20: token shape, `MATCH`, the escrow's sha256 equal to `token-sha256`, the tenant row
present in Relay, the expiry and pattern, and the discovery and RBAC checks. Re-run the readback only.
The commented-out creation block below is the historical record and must not be run again: `bao kv put` would replace the
escrowed token, so it would no longer match the two Secrets.

**Purpose.** Create the two Secrets that H6's `relay-control-monitoring` component needs
(`relay-control-monitoring.md:10-22` and activation step 2 at `:55-56`):

- `relay/relay-control-metrics`, with keys `tenant-id`, `token-sha256` and `expires-at`. Relay's
  `metrics-files` init container reads it. The component mounts it without `optional`, so if it is
  missing the H6 Pod cannot start.
- `monitoring/relay-control-collector`, with key `token`. The ServiceMonitor reads it.

The runbook leaves the commands open ("through the existing OpenBao/SOPS route"). The form below
escrows the raw token in OpenBao, as I3 does, and creates both Secrets from pipes, as I8 does.

**Writes:**

- **OpenBao**: the new document `af/relay-control/collector` (field `token`). The path is NEW; see
  the open points.
- **Kubernetes**: the two Secrets.

**Restarts:** none. Nothing reads either Secret before H6.

**Pre-checks.**

- **The tenant exists in Relay.** Relay refuses to start if the metrics tenant does not exist
  (Relay `e77bd09a` `server/main.ts:403-414`). This read is NEW:

  ```sh
  kubectl --context admin@ai -n relay exec relay-postgres-0 -c postgres -- \
    psql -X -U postgres -d relay -Atc "SELECT id, name FROM tenants WHERE id = '00000000-0000-4000-8000-000000000001'"
  ```

  Expected: one row. `00000000-0000-4000-8000-000000000001` is the only tenant of the connection
  record and of the ledger's `clients` row; the owner confirms it as the approved metrics tenant.
- Neither Secret exists (step 0), and `bao kv list -mount=af relay-control` is empty.
- **Owner decisions** (open points 1 and 2), **decided 2026-11-17**: escrow at `af/relay-control/collector`
  (recorded in `openbao-recovery.md`'s path classes), the tenant `00000000-0000-4000-8000-000000000001`,
  and the expiry `2027-02-15T00:00:00Z`.

**Commands.** This block is NEW (not in the runbook).

The token is 32 random bytes in unpadded base64url: 43 characters, no newline. Relay accepts
`Bearer [A-Za-z0-9_-]{43,128}` and compares the SHA-256 of exactly that text (Relay `e77bd09a`
`server/agent-sessions/metrics.ts:164-182`).

```sh
# HISTORICAL RECORD, step 3 DONE. DO NOT RUN: it would replace the escrowed collector token, which
# would then no longer match the two Secrets. Every line is commented out so a paste does nothing.
# openssl rand 32 | basenc --base64url | tr -d '=\n' | bao kv put -mount=af relay-control/collector token=-
# bao kv get -mount=af -field=token relay-control/collector \
#   | kubectl --context admin@ai -n monitoring create secret generic relay-control-collector --from-file=token=/dev/stdin
# sha="$(bao kv get -mount=af -field=token relay-control/collector | tr -d '\n' | sha256sum | cut -d' ' -f1)"
# kubectl --context admin@ai -n relay create secret generic relay-control-metrics \
#   --from-literal=tenant-id=00000000-0000-4000-8000-000000000001 \
#   --from-literal=token-sha256="$sha" \
#   --from-literal=expires-at='2027-02-15T00:00:00Z'
# unset sha
```

**Readback.** This block is NEW. It prints keys, `MATCH` or `OK` lines and the two values that are not
secret, but never the token:

```sh
kubectl --context admin@ai -n relay get secret relay-control-metrics -o json | jq -c '.data | keys'        # ["expires-at","tenant-id","token-sha256"]
kubectl --context admin@ai -n monitoring get secret relay-control-collector -o json | jq -c '.data | keys' # ["token"]
kubectl --context admin@ai -n monitoring get secret relay-control-collector -o json | jq -r '.data.token' | base64 -d \
  | grep -Eqx '[A-Za-z0-9_-]{43}' && echo TOKEN-SHAPE-OK
a="$(kubectl --context admin@ai -n monitoring get secret relay-control-collector -o json | jq -r '.data.token' | base64 -d | sha256sum | cut -d' ' -f1)"
b="$(kubectl --context admin@ai -n relay get secret relay-control-metrics -o json | jq -r '.data["token-sha256"]' | base64 -d)"
[ -n "$a" ] && [ "$a" = "$b" ] && echo MATCH || echo MISMATCH; unset a b
for k in tenant-id expires-at; do
  kubectl --context admin@ai -n relay get secret relay-control-metrics -o json | jq -r --arg k "$k" '.data[$k]' | base64 -d; echo
done
kubectl --context admin@ai get prometheus -A -o json | jq -c '.items[] | {name: .metadata.name, sm: .spec.serviceMonitorSelector, ns: .spec.serviceMonitorNamespaceSelector}'
# RBAC and watch scope of the two identities that use the ServiceMonitor and its token Secret:
# Each lookup must find exactly one identity; anything else stops here instead of building a malformed --as.
psa="$(kubectl --context admin@ai -n monitoring get prometheus -o json \
  | jq -er '[.items[].spec.serviceAccountName] | if length == 1 then .[0] else error("expected one Prometheus, got \(length)") end')" &&
osa="$(kubectl --context admin@ai -n monitoring get deploy -o json \
  | jq -er '[.items[] | select(.metadata.name | endswith("-operator")) | .spec.template.spec.serviceAccountName]
            | if length == 1 then .[0] else error("expected one operator Deployment, got \(length)") end')" &&
echo "prometheus=$psa operator=$osa" || echo "STOP: identity lookup failed"
for v in "get secrets -n monitoring" "list servicemonitors.monitoring.coreos.com -n monitoring"; do
  echo "operator $v: $(kubectl --context admin@ai auth can-i $v --as="system:serviceaccount:monitoring:$osa")"
done
for r in services endpoints endpointslices.discovery.k8s.io pods; do
  echo "prometheus list $r -n relay: $(kubectl --context admin@ai auth can-i list $r -n relay --as="system:serviceaccount:monitoring:$psa")"
done
kubectl --context admin@ai -n monitoring get deploy -o json \
  | jq -c '.items[] | select(.metadata.name | endswith("-operator")) | [.spec.template.spec.containers[].args[]? | select(test("namespaces"))]'
```

Expected:

- `TOKEN-SHAPE-OK` and `MATCH`.
- The tenant is `00000000-0000-4000-8000-000000000001`.
- The expiry is in the future and matches the initializer's pattern
  `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z$`
  (`kubernetes/components/relay-control-monitoring/materialize.mjs`). A value that fails the pattern
  stops the H6 Pod in its first init container, before the backup runs.
- **Discovery.** The Prometheus `serviceMonitorSelector` admits the label
  `release: kube-prometheus-stack` that the ServiceMonitor carries, and its namespace selector admits
  `monitoring`.
- **RBAC.**
  - The operator identity can get Secrets and list ServiceMonitors in `monitoring`; it reads the
    bearer Secret into the generated scrape configuration.
  - The Prometheus identity can list Services, Endpoints, EndpointSlices and Pods in `relay`.
  - Every `can-i` answer is `yes`.
- **Watch scope.** The operator has no `--namespaces` restriction (an empty list), or one that
  includes `monitoring` and `relay`.

These three are activation step 2's discovery and RBAC check.

**Stop and rollback.** Nothing consumes these before H6. Delete both Secrets
(`kubectl --context admin@ai -n relay delete secret relay-control-metrics` and
`-n monitoring delete secret relay-control-collector`). Then permanently delete the document with
`bao kv metadata delete -mount=af relay-control/collector`, which removes every version, and repeat
the step. To rotate later,
follow the runbook's rotation (`relay-control-monitoring.md:98-105`).

## Step 4. Budget step 8 proofs

**Purpose.** The first half of the rest of budget step 8 (`relay-worker-budget-authority.md:229-238`):

- TLS hostname verification;
- TLS-only login;
- different role permissions;
- current primary status, synchronous settings and checksum mode;
- the current external witness;
- preserved allocation and outbox history;
- independent durability and recovery evidence.

The runbook names these proofs but gives no commands, so every command in this step is NEW (not in the
runbook). Step 8's first sentence, the router reader inclusion, is done (#1807).

**Writes:** **Kubernetes**: one temporary Pod, `relay-budget-authority/relay-budget-operator-client`.
The label `relay.chifor.me/budget-operator-client=true` is what the ledger's NetworkPolicies admit
(`kubernetes/optional/relay-budget-authority/network-policy.yaml`). Nothing is written to the ledger.

**Restarts:** none.

**Pre-checks.**

- The parts of step 0's readback that steps 1-3 do not change still hold: the authority row and its
  IDs (A), the `clients` and `readers` rows, the zero counts, the five witness hashes, the plugin
  `disabled`, and the digest.
- Steps 1-3's own readbacks hold.
- The namespace enforces Pod Security `restricted`; the Pod below complies.

**Commands.** First the client Pod. It uses the ledger's own image pin
(`kubernetes/optional/relay-budget-authority/postgres.yaml`), and mounts only `ca.crt` of the ledger's
certificate Secret:

```sh
kubectl --context admin@ai create -f - <<'POD'
apiVersion: v1
kind: Pod
metadata:
  name: relay-budget-operator-client
  namespace: relay-budget-authority
  labels: {relay.chifor.me/budget-operator-client: "true"}
spec:
  restartPolicy: Never
  activeDeadlineSeconds: 86400
  automountServiceAccountToken: false
  enableServiceLinks: false
  tolerations: [{key: dedicated, operator: Equal, value: agent, effect: NoSchedule}]
  securityContext: {runAsNonRoot: true, runAsUser: 70, runAsGroup: 70, seccompProfile: {type: RuntimeDefault}}
  containers:
  - name: psql
    image: docker.io/library/postgres:17.11-alpine@sha256:3540c0362c809df084ffac0ae69fe794515f9de92692f02178bcdce0ef0302ac
    command: [sleep, "86400"]
    securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
    resources: {requests: {cpu: 10m, memory: 32Mi}, limits: {cpu: 250m, memory: 128Mi}}
    volumeMounts: [{name: ca, mountPath: /run/ca, readOnly: true}]
  volumes:
  - name: ca
    secret: {secretName: relay-budget-postgres-tls, defaultMode: 0444, items: [{key: ca.crt, path: ca.crt}]}
POD
kubectl --context admin@ai -n relay-budget-authority wait --for=condition=Ready pod/relay-budget-operator-client --timeout=2m

# q <login> <sql>: one statement as an application login over TCP and TLS. The password comes from
# OpenBao on stdin into the Pod's psql environment, and never reaches argv or the transcript.
# HOST and SSLMODE override the defaults for the negative proofs.
q() {
  bao kv get -mount=af -field=password "relay-budget/${1#relay_budget_}" \
  | kubectl --context admin@ai -n relay-budget-authority exec -i relay-budget-operator-client -- sh -c '
      IFS= read -r P || [ -n "$P" ]; export PGPASSWORD="$P"
      exec psql -X -At -v ON_ERROR_STOP=1 -v VERBOSITY=verbose \
        "host=$2 port=5432 dbname=relay_budget user=$1 sslmode=$3 sslrootcert=/run/ca/ca.crt" -c "$4"' \
      sh "$1" "${HOST:-relay-budget-postgres.relay-budget-authority.svc.cluster.local}" "${SSLMODE:-verify-full}" "$2"
}
```

**The proofs.**

```sh
# TLS with hostname verification, both logins:
q relay_budget_router    "SELECT current_user, ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid()"   # relay_budget_router|t|TLSv1.3
q relay_budget_allocator "SELECT current_user, ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid()"   # relay_budget_allocator|t|TLSv1.3
# A name that is not on the certificate is refused by verify-full:
HOST=relay-budget-postgres.relay-budget-authority q relay_budget_router "SELECT 1"   # fails: ... does not match host name ...
# Plaintext is refused (pg_hba: hostssl only):
SSLMODE=disable q relay_budget_router "SELECT 1"     # fails: pg_hba.conf rejects connection ... no encryption
# The administrator is refused over TCP (no password is sent):
kubectl --context admin@ai -n relay-budget-authority exec relay-budget-operator-client -- psql -X -w \
  "host=relay-budget-postgres.relay-budget-authority.svc.cluster.local port=5432 dbname=relay_budget user=postgres sslmode=verify-full sslrootcert=/run/ca/ca.crt" -c 'SELECT 1'
                                                     # fails: pg_hba.conf rejects connection ... user "postgres" ...
# The logins cannot read tables:
q relay_budget_router    "SELECT count(*) FROM worker_budget.authority"   # fails: 42501 permission denied
q relay_budget_allocator "SELECT count(*) FROM worker_budget.authority"   # fails: 42501 permission denied
```

Then, over the local socket:

```sh
cap step8-proofs lsql <<'SQL'
SELECT pg_is_in_recovery() AS in_recovery;
SHOW fsync; SHOW full_page_writes; SHOW synchronous_commit; SHOW data_checksums;
SELECT rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls,
       pg_has_role(rolname, 'relay_budget_owner', 'MEMBER')  AS owner,
       pg_has_role(rolname, 'relay_budget_client', 'MEMBER') AS client,
       pg_has_role(rolname, 'relay_budget_reader', 'MEMBER') AS reader
  FROM pg_roles WHERE rolname IN ('relay_budget_allocator', 'relay_budget_router') ORDER BY 1;
SELECT p.oid::regprocedure AS function,
       has_function_privilege('relay_budget_allocator', p.oid, 'EXECUTE') AS allocator,
       has_function_privilege('relay_budget_router', p.oid, 'EXECUTE')    AS router
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'worker_budget' ORDER BY 1;
SELECT c.relname,
       has_table_privilege('relay_budget_allocator', c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,MAINTAIN') AS allocator,
       has_table_privilege('relay_budget_router', c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,MAINTAIN')    AS router,
       has_any_column_privilege('relay_budget_allocator', c.oid, 'SELECT,INSERT,UPDATE,REFERENCES') AS allocator_columns,
       has_any_column_privilege('relay_budget_router', c.oid, 'SELECT,INSERT,UPDATE,REFERENCES')    AS router_columns
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'worker_budget' AND c.relkind IN ('r', 'p', 'v', 'm') ORDER BY 1;
SELECT (SELECT count(*) FROM worker_budget.allocations) AS allocations, (SELECT count(*) FROM worker_budget.outbox) AS outbox,
       (SELECT count(*) FROM worker_budget.receipts) AS receipts, (SELECT count(*) FROM worker_budget.scope_runs) AS scope_runs,
       (SELECT count(*) FROM worker_budget.allocation_scopes) AS allocation_scopes;
SELECT ready, instance_id, recovery_epoch, witness_digest FROM worker_budget.authority;
SQL
wsha_bao; wsha_secret relay relay-worker-budget; wsha_secret llm-router llm-router-worker-budget; wsha_router; wsha_relay
```

**Expected.**

- **Primary and durability settings.** `in_recovery` is `f`. `fsync`, `full_page_writes`,
  `synchronous_commit` and `data_checksums` are all `on`. The initializer runs
  `initdb --data-checksums`.
- **Login roles.** Both log in. Neither is a superuser, can create roles or databases, replicates,
  bypasses RLS or is a member of `relay_budget_owner`. The allocator is a member of
  `relay_budget_client`, and the router of `relay_budget_reader`.
- **Function grants.** They are exactly the pinned schema's grants (Relay `d1c77f87`,
  `deploy/worker-budget-ledger/001`-`005`):
  - the allocator executes `acknowledge`, `current_epoch`, `inspect`, `preflight`, `prepare`,
    `prepare_provenance`, `provenance_capability` and `scope`;
  - the router executes `current_epoch` and `runtime_snapshot`;
  - neither executes `gate`, `prepare_token_only`, `lock_worker` or the 005 helpers.
- **Tables.** No table or column privilege of any kind for either login: all four columns are `f`
  on every row. PostgreSQL 17's full table list includes `MAINTAIN`.
- **History.** All five counts are 0. No allocation has ever existed, so this is the baseline that
  every drill must preserve.
- **Witness.** The three-way comparison of the OpenBao witness, the authority row and the record's
  latest entry (A) agrees (`openbao-recovery.md:215-218`). The five hashes are `8e5a4104…527b`.
- **Durability and recovery evidence: not produced by this step, and a hard gate for step 7.** Step 8
  requires "independent durability/recovery evidence" (`relay-worker-budget-authority.md:232-234`).
  The runbook's gates ask for:
  - measured QNAP/CSI power-loss durability (`:40-43`);
  - the `SHOW` values above;
  - storage-cache and power-loss guarantees;
  - a test allocation acknowledged before controlled PostgreSQL, node or storage interruptions,
    compared afterwards (`:576-581`).

  The QNAP no-snapshot check was done at step 6 (#1753 comment 123957). An acknowledged test
  allocation cannot exist before H6: no Relay connection, no allocation. Step 7 therefore does not run
  until the owner has accepted the evidence specified by the recorded decision: (i)-(iii) and the
  NAS-side record in "Durability evidence for the first `ready=true`" (open point 7, ailab #1820).

**Stop.** Any difference keeps `ready=false` (`relay-worker-budget-authority.md:235`) and needs the
owner's investigation. Keep the client Pod for step 5.

## Step 5. Witness drills A-D

**Purpose.** Run the four witness drills, in a drill window before the production opening. Their source
is the budget runbook's "Drills before production `ready=true`" (`relay-worker-budget-authority.md:411-481`),
with its procedure "Changing the witness" (`:375-399`) and "Restoring B" (`:461-465`). The window is
valid now: no Relay router connection or credential binding exists, because H6 is not merged and
`relay-router-files` has no token. So no allocation is possible whatever the gate says.

**Writes:**

- **OpenBao**: new versions of `af/relay-budget/witness` (a rotation to B, injections, restores, a
  soft delete).
- **Ledger**: the rotation of the authority row to B, and `ready` flips (open for each observation,
  then withdraw).
- **Router admin API**: the plugin is enabled for each observation and disabled again.
- **Kubernetes**:
  - `force-sync` annotations on `relay/relay-worker-budget` and `llm-router/llm-router-worker-budget`
    (ESO re-renders both Secrets);
  - `roll_router`'s restart of `llm-router/llm-router`. It writes the Pod template annotation
    `kubectl.kubernetes.io/restartedAt` under field manager `relay-runbook`, which Flux leaves alone
    (see `roll_deploy`). The annotation stays in the template afterwards, by design;
  - at the end, deleting step 4's client Pod.
- **Offline issuance record**: one entry, for B only.

**Restarts:** **the Router**, 8 times on the normal path (A1, A2, A4, B1, B4, C1, C3, D2). Each is a
Recreate outage of all Router traffic, so announce the window. Each `roll_router` is exactly one
outage: it fails unless the Deployment revision rose by exactly 1 and one Pod of that revision runs.
On 2026-11-20 the helper still used kubectl's default field manager, and Flux reverted each restart
at its next reconcile: a second outage up to 10 minutes later (ailab #1897).
The cause is kustomize-controller v1.8.5's field-manager cleanup: before each apply it renames every
`Update` entry whose manager starts with `kubectl` (prefix match, so `kubectl-rollout` too) to its own
`Apply` entry, then its server-side apply drops the `restartedAt` it now owns but does not declare
([kustomization_controller.go:862-912](https://github.com/fluxcd/kustomize-controller/blob/v1.8.5/internal/controller/kustomization_controller.go#L862-L912);
fluxcd/pkg [ssa/patch.go:62-80, 114-171](https://github.com/fluxcd/pkg/blob/ssa/v0.67.5/ssa/patch.go#L62-L171),
[ssa/manager_apply.go:239-244](https://github.com/fluxcd/pkg/blob/ssa/v0.67.5/ssa/manager_apply.go#L239-L244)).
Rejected alternatives: the `kustomize.toolkit.fluxcd.io/reconcile: disabled` annotation only defers
the revert to its removal; `kubectl scale` 0/1 races Flux, which declares `replicas: 1` and can scale the
same ReplicaSet back up while the old Pod still terminates on the ReadWriteOnce volume; suspending
Kustomization `apps` pauses every app.

Relay is not rolled here; see the open points. Relay is wired since H4a, but its budget authority
does not start before H7, because it has no profile source (hub runbook H4a,
`relay-researcher-hub-upgrade.md:299-310`). The runbook's Relay half of drills A-C therefore cannot be
observed yet (`relay-worker-budget-authority.md:418-419`). Relay's ExternalSecret is still
force-synced with the Router's, as the procedure says, so its Secret follows. Its Pod mounts B at the
H6 rollout. Relay's half runs in drill run R0, after File B and before R1
([relay-researcher-r0-relay-drills.md](relay-researcher-r0-relay-drills.md)).

**Pre-checks.**

- Step 4's readback holds, and its client Pod is Running. The Pod ends after 24 h
  (`activeDeadlineSeconds`). If it is not Running, delete it
  (`kubectl --context admin@ai -n relay-budget-authority delete pod relay-budget-operator-client`) and
  create it again with step 4's manifest and `wait` before continuing.
- **Clocks.** The NTP readback of #1753 comment 124071 §6, because the drills set `ready=true`. Find
  the nodes with `kubectl --context admin@ai get pod -o wide` for `relay/relay-*`,
  `llm-router/llm-router-*` and `relay-budget-authority/relay-budget-postgres-0`. Then run
  `talosctl-1142 -n <node IP> time` for each node: synced, with an offset of a few tens of ms at most.
- **No Relay connection.** The Relay Pod env has no `RELAY_ROUTER_CONNECTIONS_FILE`.
- **The owner holds the record.** The owner has the offline issuance record and confirms the window.
- **Owner decisions** (open points 3 and 4) are recorded on #1753. Open point 4 sets the pass
  criterion of drills B and C on the live Router (see B2); it is **decided 2026-11-17**: the refused enable,
  with the gate proven open, is the pass. Open point 3 is **decided 2026-11-17**: Relay's half of the
  drills runs in drill run R0, after File B and before R1.

**Building blocks.** These are used by every drill. All are NEW (not in the runbook), except the
lines marked otherwise. Write the SQL files once, filling in the IDs from the record (the witness is
public identity evidence, not a secret):

```sh
cat > "$ev/withdraw.sql" <<'SQL'
BEGIN;
SET LOCAL ROLE relay_budget_owner;
LOCK TABLE worker_budget.authority IN SHARE ROW EXCLUSIVE MODE;
UPDATE worker_budget.authority SET ready=false;   -- verbatim, "Changing the witness" step 1
DO $c$ BEGIN
  IF (SELECT count(*) FROM worker_budget.authority WHERE NOT ready) <> 1 THEN RAISE EXCEPTION 'withdraw failed'; END IF;
END $c$;
SELECT ready, instance_id, recovery_epoch, witness_digest FROM worker_budget.authority;
COMMIT;
SQL
cat > "$ev/open-B.sql" <<'SQL'
BEGIN;
SET LOCAL ROLE relay_budget_owner;
LOCK TABLE worker_budget.authority IN SHARE ROW EXCLUSIVE MODE;
UPDATE worker_budget.authority SET ready = true
 WHERE singleton AND NOT ready
   AND instance_id = '<B instanceId>' AND recovery_epoch = '<B recoveryEpoch>' AND witness_digest = '<B witnessDigest>';
DO $c$ BEGIN
  IF (SELECT count(*) FROM worker_budget.authority WHERE ready AND instance_id = '<B instanceId>'
        AND recovery_epoch = '<B recoveryEpoch>' AND witness_digest = '<B witnessDigest>') <> 1 THEN
    RAISE EXCEPTION 'ready=true did not apply to exactly the record''s current row';
  END IF;
END $c$;
SELECT worker_budget.gate('<B instanceId>', '<B recoveryEpoch>', '<B witnessDigest>');
SELECT clock_timestamp(), ready, instance_id, recovery_epoch, witness_digest FROM worker_budget.authority;
COMMIT;
SQL
# plugin true|false: the endpoint and body of budget step 9. The HTTP status and the body are both kept.
plugin() {
  capfile "plugin-$1"; pf="$CAPF.json"
  cap "plugin-$1-status" radmin -o "$pf" -w 'HTTP %{http_code}\n' -X POST -H 'Content-Type: application/json' \
    -d "{\"enabled\":$1}" https://router.chifor.me/admin/v1/plugins/worker-budget-authority \
    || { echo "plugin call or its capture failed; response kept in $pf"; return 1; }
  jq -c 'if type == "array" then (.[] | select(.id == "worker-budget-authority") | {id, state, error}) else . end' "$pf"
}
# The form of "Changing the witness" step 4, in relay and in llm-router.
force_sync() {
  kubectl --context admin@ai -n relay annotate externalsecret relay-worker-budget force-sync="$(date +%s)" --overwrite &&
  kubectl --context admin@ai -n llm-router annotate externalsecret llm-router-worker-budget force-sync="$(date +%s)" --overwrite
}
# wait_hashes <sha256>: bounded wait (2 minutes) until OpenBao and both rendered Secrets carry it.
# A failed read is never a match: each read must succeed before its hash is compared.
wait_hashes() {
  local -; set -o pipefail
  for i in $(seq 1 24); do
    a=; b=; c=
    if a="$(wsha_bao | cut -d' ' -f1)" && b="$(wsha_secret relay relay-worker-budget | cut -d' ' -f1)" &&
       c="$(wsha_secret llm-router llm-router-worker-budget | cut -d' ' -f1)" &&
       [ "$a" = "$1" ] && [ "$b" = "$1" ] && [ "$c" = "$1" ]; then
      cap hashes echo "openbao=$a relay=$b llm-router=$c"; return   # the capture's own status
    fi
    sleep 5
  done
  cap hashes-not-converged echo "openbao=$a relay=$b llm-router=$c want=$1"; return 1
}
# rlog: the Router's budget log lines. It fails if the logs cannot be read; "no matching line" is not a failure.
rlog() {
  l="$(kubectl --context admin@ai -n llm-router logs deploy/llm-router -c router --since=10m)" || return 1
  printf '%s\n' "$l" | grep 'Worker budget authority' || true
}
# At llm-router 5a5181b7, rlog is always empty: the plugin logs through cordis's ctx.logger, whose only
# exporter is an in-memory buffer (llm-router #191). The two helpers below are the ledger-side
# observation the owner accepted on 2026-11-20 (#1753). Keep capturing rlog: it shows the gap.
# rreader: the Router plugin's reader session (it refreshes every 5 s over one pooled connection).
rreader() {
  lsql <<'SQL'
SELECT pid, backend_start, application_name, usename, state, query_start, left(query, 60) AS query
  FROM pg_stat_activity WHERE application_name = 'router-worker-budget-reader';
SQL
}
# lerrors <time>: every ERROR, FATAL or PANIC line the ledger logged since <time>, with its CONTEXT and
# STATEMENT lines. <time> is any form `date -d` reads: `date -u +%FT%TZ` output, or psql's
# "2026-11-20 10:00:00.123+00". It is converted to the RFC 3339 form `kubectl logs --since-time` needs. PostgreSQL logs every failed
# statement at ERROR, so a read that fails for any reason (gate, permission, connection) shows here.
# Only relay_budget_router may execute runtime_snapshot (step 4): a runtime_snapshot context line is the
# Router plugin's read; a current_epoch context line is an operator control (`q`).
lerrors() {
  t="$(date -u -d "$1" +%Y-%m-%dT%H:%M:%SZ)" || return 1
  l="$(kubectl --context admin@ai -n relay-budget-authority logs relay-budget-postgres-0 --timestamps --since-time="$t")" || return 1
  printf '%s\n' "$l" | grep -A3 -E ' (ERROR|FATAL|PANIC): ' || true
}
# rident: the two identities the Router compares a snapshot against. The authority row's instance_id
# must equal the mounted witness's instanceId (wsha_router shows which witness is mounted; B's file is
# $ev/witness-B.json), and the readers row's deployment_digest must equal the Router's env digest.
rident() {
  kubectl --context admin@ai -n llm-router get deploy llm-router -o json \
    | jq -r '.spec.template.spec.containers[] | select(.name=="router") | .env[] | select(.name=="ROUTER_WORKER_BUDGET_DEPLOYMENT_DIGEST") | "router_digest " + .value' || return 1
  jq -r '"witness_B_instance " + .instanceId' "$ev/witness-B.json" || return 1
  lsql <<'SQL'
SELECT 'readers' AS row, login, deployment_digest FROM worker_budget.readers;
SELECT 'authority' AS row, instance_id FROM worker_budget.authority;
SQL
}
# rollstate <namespace> <deployment> <pod selector>: one JSON line. The Deployment revision, its Pod template's
# restartedAt and the managedFields entries that own it, and every Pod of the selector with its ReplicaSet's
# revision and creation time.
rollstate() {
  local dj rj pj
  dj="$(kubectl --context admin@ai -n "$1" get deploy "$2" --show-managed-fields -o json)" || return 1
  rj="$(kubectl --context admin@ai -n "$1" get rs -l "$3" -o json)" || return 1
  pj="$(kubectl --context admin@ai -n "$1" get pod -l "$3" -o json)" || return 1
  printf '%s\n%s\n%s\n' "$dj" "$rj" "$pj" | jq -sc '.[0] as $d | .[1] as $r | .[2] as $p
    | ($r.items | map({key: .metadata.name, value: (.metadata.annotations["deployment.kubernetes.io/revision"] | tonumber)}) | from_entries) as $rev
    | {revision: ($d.metadata.annotations["deployment.kubernetes.io/revision"] | tonumber),
       restartedAt: $d.spec.template.metadata.annotations["kubectl.kubernetes.io/restartedAt"],
       restartedAtOwners: [$d.metadata.managedFields[] | select(.fieldsV1["f:spec"]["f:template"]["f:metadata"]["f:annotations"]["f:kubectl.kubernetes.io/restartedAt"] != null) | {manager, operation}],
       pods: [$p.items[] | {pod: .metadata.name, revision: $rev[.metadata.ownerReferences[0].name // ""], created: .metadata.creationTimestamp,
                            phase: .status.phase, deleting: .metadata.deletionTimestamp}]}'
}
# roll_deploy <namespace> <deployment> <pod selector> <label>: exactly one Recreate roll of a Flux-owned Deployment, with proof.
# Why the field manager (ailab #1897): `kubectl rollout restart` records restartedAt under manager `kubectl-rollout`
# (operation Update). Before every apply, kustomize-controller v1.8.5 renames each Update entry whose manager STARTS
# WITH `kubectl` to its own Apply entry (prefix match), so it then owns restartedAt without declaring it, and its
# server-side apply deletes it: a second Recreate roll at the next `apps` reconcile, up to 10 minutes later. An entry
# under `relay-runbook` is not renamed, so the annotation stays and Flux sees no drift. Never remove the annotation
# afterwards: that changes the Pod template, which is itself a roll. The next roll_deploy only replaces its value.
# Recreate scales the old ReplicaSet to 0 and creates the new Pod only when no old Pod is left in a non-terminal
# phase, so the old and new Pods never run together on the ReadWriteOnce volume.
# It refuses to roll while a revert is pending (a `kubectl*` Update entry owns restartedAt), or when the revision moved
# since this session's last roll_deploy of the Deployment (something else rolled it). It fails unless, after the roll,
# the revision rose by exactly 1, `relay-runbook` alone owns restartedAt, and exactly one non-terminal Pod runs, of
# that revision. It keeps the rollout status and the after-state as `cap` files <label>-rollout and <label>-roll.
# Call it directly, never under `cap`: `cap` runs its command in a subshell, which would lose ROLLREV.
declare -A ROLLREV   # <namespace>/<deployment> -> revision this session's last roll_deploy left
roll_deploy() {
  local ns="$1" dep="$2" sel="$3" label="$4" s0 s1 r0
  s0="$(rollstate "$ns" "$dep" "$sel")" || return 1
  r0="$(jq -er .revision <<<"$s0")" || return 1
  if jq -e '[.restartedAtOwners[] | select(.operation == "Update" and (.manager | startswith("kubectl")))] | length > 0' <<<"$s0" >/dev/null; then
    echo "$ns/$dep: a kubectl* Update entry owns restartedAt, so Flux reverts it (one more roll) at its next reconcile (#1897)."
    echo "Not rolling. Wait for that roll (rollstate: no kubectl* owner, revision +1, its Pod Ready), then retry."
    printf '%s\n' "$s0"; return 1
  fi
  if [ -n "${ROLLREV[$ns/$dep]:-}" ] && [ "${ROLLREV[$ns/$dep]}" != "$r0" ]; then
    echo "$ns/$dep: revision $r0, but this session's last roll left ${ROLLREV[$ns/$dep]}: something else rolled it. Stop and investigate."
    printf '%s\n' "$s0"; return 1
  fi
  kubectl --context admin@ai -n "$ns" rollout restart deploy/"$dep" --field-manager=relay-runbook || return 1
  if ! cap "$label-rollout" kubectl --context admin@ai -n "$ns" rollout status deploy/"$dep" --timeout=10m; then
    # The restart is already applied: record where it left the Deployment, so a retry diagnoses correctly.
    local r1=unknown
    if s1="$(rollstate "$ns" "$dep" "$sel")"; then
      cap "$label-roll-timeout" printf '%s\n' "$s1"; r1="$(jq -r .revision <<<"$s1")"
    fi
    # Our restart bumped the revision by one even if rollstate failed: record that, so a retry
    # in this session never mistakes our own roll for a foreign one.
    if [ "$r1" = unknown ]; then ROLLREV[$ns/$dep]="$((r0 + 1))"; else ROLLREV[$ns/$dep]="$r1"; fi
    echo "$ns/$dep: restart APPLIED (revision was $r0, now $r1) but not Ready within 10m. Do not roll again; check its Pod, then rollstate."
    return 1
  fi
  s1="$(rollstate "$ns" "$dep" "$sel")" || return 1
  cap "$label-roll" printf '%s\n' "$s1"
  ROLLREV[$ns/$dep]="$(jq -r .revision <<<"$s1")"
  jq -e --argjson r "$r0" '.revision == $r + 1 and .restartedAtOwners == [{manager: "relay-runbook", operation: "Update"}]
      and ([.pods[] | select(.phase != "Succeeded" and .phase != "Failed")] | length == 1 and .[0].revision == $r + 1 and .[0].deleting == null)' \
      <<<"$s1" >/dev/null || { echo "$ns/$dep: not exactly one roll (expected revision $((r0 + 1)), one Pod of it, relay-runbook owning restartedAt)"; return 1; }
}
# roll_router: keeps the outgoing Router Pod's budget log lines, then rolls it once (Recreate) and proves it.
# It refuses to roll if the log capture failed: a roll destroys that Pod's log.
roll_router() {
  cap router-log-before-roll rlog || { echo "log capture failed: not rolling"; return 1; }
  roll_deploy llm-router llm-router app=llm-router router
}
ledger_state() {
  lsql <<'SQL'
SELECT ready, instance_id, recovery_epoch, witness_digest FROM worker_budget.authority;
SELECT (SELECT count(*) FROM worker_budget.allocations) AS allocations, (SELECT count(*) FROM worker_budget.outbox) AS outbox,
       (SELECT count(*) FROM worker_budget.receipts) AS receipts, (SELECT count(*) FROM worker_budget.scope_runs) AS scope_runs,
       (SELECT count(*) FROM worker_budget.allocation_scopes) AS allocation_scopes;
SQL
}
```

Every observation below goes through `cap`, so each one is kept in `$ev/cap` with its timestamp, and a
later roll cannot overwrite it.

- **W, install a witness state; authority stays withdrawn.** This is "Changing the witness" steps 1-2
  and 4-6. A drill injection skips step 3's record entry.
  1. Withdraw: `lwrite withdraw`, then `plugin false` (`HTTP 200`, state `disabled`).
  2. `sleep 20`. Nothing needs draining: there is no allocation, and the plugin is off.
  3. Write the drill's state to OpenBao, as each drill below says.
  4. `force_sync`, then `wait_hashes <expected sha256>`. It must return 0.
  5. `roll_router`.
  6. `cap mounted wsha_router` shows the expected sha256.
- **O, re-open, observe, withdraw.** This is the drill protocol, steps 2-5.
  1. `lwrite open-B` (COMMITTED). Its output holds the gate check and the row with `ready = t`.
  2. `plugin true`. In drills A and D this gives `HTTP 200` with state `active`. Drills B and C have
     their own expectation (B2).
  3. **Prove that the gate is open**, immediately before the observation:
     - `cap plugins plugins`;
     - the positive control:
       `cap control q relay_budget_router "SELECT worker_budget.current_epoch('<B instanceId>', '<B recoveryEpoch>', '<B witnessDigest>')"`.
       It returns `<B recoveryEpoch>`.
  4. **Observe the drill's Pass column.**
     - `cap router-log rlog`, then `sleep 20` and `cap router-log-after-20s rlog` (empty at this pin,
       see `rreader`).
     - `cap reader rreader`, then `cap errors lerrors "$t0"`, at least 20 s after the commit: the
       Router-side observation (see "Observing the Router" below). `$t0` is set by
       `t0="$(date -u +%FT%TZ)"` immediately before O1's `lwrite open-B`.
     - Where the mounted file has IDs, check the refusal:
       `cap refusal q relay_budget_router "SELECT worker_budget.current_epoch('<mounted instanceId>', '<mounted recoveryEpoch>', '<mounted witnessDigest>')"`.
       It fails with `42501: Worker budget authority unavailable`.
     - Then run the control again (`cap control-again …`): it still succeeds.
  5. Withdraw: `lwrite withdraw`, then `plugin false`.
- **R, restore B and prove it.** This is "Restoring B", on the normal path only, after a drill whose
  readback passed.
  1. Run W with step 3 = `bao kv put -mount=af relay-budget/witness witness=- < "$ev/witness-B.json"`
     (the form of "Who writes it", `:366`), and B's sha256 expected.
  2. `lwrite open-B`, then `plugin true` (`HTTP 200`, `active`).
  3. Available, observed as in "Observing the Router" below: `cap reader rreader`, then at least
     10 s later `cap reader-after-10s rreader`, `cap errors lerrors "$t0"` at least 20 s after the commit (`t0="$(date -u +%FT%TZ)"` set just before
     step 2's `lwrite open-B`), and
     `cap ident rident`. The positive control succeeds. (`cap router-log rlog` is still taken; it is
     empty at this pin.)
  4. Withdraw.
- **F, the failure path. It never re-opens.**
  1. Withdraw: `lwrite withdraw`, then `plugin false`.
  2. `cap ledger-state ledger_state`.
  3. If the authority row is not B's, or any history count is not 0, the ledger's history is in
     question. Stop and escalate to the owner, and restore nothing
     (`relay-worker-budget-authority.md:615-622`).
  4. Otherwise run W with B, as in R step 1. Authority stays withdrawn and the plugin disabled.
  5. Stop. Re-opening, even for a drill, needs the owner's review of the failure first.

**Observing the Router (owner decision 2026-11-20, #1753).** At llm-router `5a5181b7` the
`Worker budget authority …` lines never reach `kubectl logs` (llm-router #191), so a log line cannot be
a pass criterion. Each Router observation below is made on the ledger instead:

- **Unavailable** (`read_failed`): `lerrors` shows `Worker budget authority unavailable` with the
  context `runtime_snapshot(uuid,uuid,text)`, about every 5 s, over at least 20 s. Only
  `relay_budget_router` may execute `runtime_snapshot` (step 4), so these are the Router plugin's reads.
  Lines with the context `current_epoch` are the operator's own controls and are not counted.
  At `runtime.ts:83-97` a failed read is reported as `read_failed`.
- **Available**: all three of:
  - `rreader` shows one session `router-worker-budget-reader`, user `relay_budget_router`, query
    `runtime_snapshot`, and its `query_start` advances between the `reader` and `reader-after-10s`
    captures, at least 10 s apart, with the same `pid` and `backend_start`: the Router keeps reading
    over one connection;
  - `lerrors` shows **no** ERROR, FATAL or PANIC line at all from the open-B capture to at least 20 s
    later. R and D3 run no negative control, so any error, of any kind, fails the check. Together with
    the advancing `query_start`, this shows the ledger served the reads without error: a read the
    ledger refuses or fails (the gate, a permission, a statement error) is logged at ERROR, as A3
    showed;
  - `rident`: the `readers` row's `deployment_digest` equals `router_digest`, and the `authority`
    row's `instance_id` equals the mounted witness's `instanceId` (the witness `wsha_router` shows).
    These are the comparisons `runtime.ts:89-92` makes against the snapshot.
- **Accepted residual.** Any failure on the Router's side after the ledger has served the read without
  error is not visible from the ledger: the Router's own checks (`snapshot_invalid`,
  `identity_mismatch`, `policy_digest_invalid`), and a client timeout or lost connection that leaves
  the Router without the answer. The owner accepted the checks on 2026-11-20 and widened the
  acceptance to the whole class the same day (#1895 review), until llm-router #191 is fixed;
  afterwards, the log line is checked as well. A lasting transport failure would show as a changing
  `pid`/`backend_start` between the two reader captures, which fails the check.
- **Plugin start failure** (B2, C2): unchanged, it is visible in the admin API (`HTTP 500`
  `PLUGIN_FAILED`). `rreader` then has no row.

**Drill A, stale witness.**

- **A1. Rotate to B.** This is an issuance: "Issue A (step 6). Rotate to B". B keeps A's
  `instanceId`; the owner changes the row's `recovery_epoch` and `witness_digest`.
  1. Generate B, and write `rotate-B.sql` with B's IDs (block below).
  2. The owner and operator write the offline record entry for B **before any write**: the witness
     JSON, the UTC date, reason `rotation, witness drill A`, the operator, the ailab commit
     (`relay-worker-budget-authority.md:346-361`).
  3. Run W, with step 3 being two writes, in this order:
     - `lwrite rotate-B`, the ledger row (COMMITTED);
     - `bao kv put -mount=af relay-budget/witness witness=- < "$ev/witness-B.json"`, the witness.
  4. Expected afterwards: OpenBao version 2, and every hash is B's.

  ```sh
  python3 -c 'import json,sys,uuid,secrets; sys.stdout.write(json.dumps({"version":1,"instanceId":sys.argv[1],"recoveryEpoch":str(uuid.uuid4()),"witnessDigest":secrets.token_hex(32)},separators=(",",":")))' \
    '<A instanceId, from the record>' > "$ev/witness-B.json"
  wc -c < "$ev/witness-B.json"; sha256sum "$ev/witness-B.json"      # 203 bytes, like A; record this hash with B's entry
  cat > "$ev/rotate-B.sql" <<'SQL'
  BEGIN;
  SET LOCAL ROLE relay_budget_owner;
  LOCK TABLE worker_budget.authority IN SHARE ROW EXCLUSIVE MODE;
  UPDATE worker_budget.authority SET recovery_epoch = '<B recoveryEpoch>', witness_digest = '<B witnessDigest>'
   WHERE singleton AND NOT ready
     AND instance_id = '<A instanceId>' AND recovery_epoch = '<A recoveryEpoch>' AND witness_digest = '<A witnessDigest>';
  DO $c$ BEGIN
    IF (SELECT count(*) FROM worker_budget.authority) <> 1
       OR (SELECT count(*) FROM worker_budget.authority WHERE NOT ready AND instance_id = '<A instanceId>'
             AND recovery_epoch = '<B recoveryEpoch>' AND witness_digest = '<B witnessDigest>') <> 1 THEN
      RAISE EXCEPTION 'rotation to B did not apply exactly';
    END IF;
  END $c$;
  SELECT ready, instance_id, recovery_epoch, witness_digest FROM worker_budget.authority;
  COMMIT;
  SQL
  ```

- **A2. Restore A in OpenBao.** Run W with step 3 =
  `bao kv rollback -mount=af -version=1 relay-budget/witness`. That is the runbook's
  `bao kv rollback -mount=af -version=<A> relay-budget/witness` with `<A>` = 1, since A is version 1
  (#1753 comment 123957). A's sha256 `8e5a4104…527b` is expected.
- **A3. Run O.**
  - Pass: the Router is unavailable (`read_failed`) as "Observing the Router" defines it, still
    after at least 20 s. The plugin is `active`.
  - `current_epoch` with A's IDs fails with SQLSTATE 42501.
  - The control with B's IDs succeeds.

  On the pinned llm-router `5a5181b7`, a valid but stale witness lets the plugin start, and its snapshot
  read then fails (`packages/plugins/worker-budget-authority/runtime.ts:83-97`).
- **A4. Run R.**

**Drill B, missing witness.**

- **B1.** Run W with step 3 = `printf '' | bao kv put -mount=af relay-budget/witness witness=-`. The
  expected sha256 is that of the empty string,
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
- **B2. Run O,** with the pass criterion the owner set in open point 4. On llm-router `5a5181b7`:
  - the plugin parses the witness file when it starts
    (`packages/plugins/worker-budget-authority/index.ts`, `readBudgetWitness`);
  - an empty or malformed witness makes the start fail;
  - the plugin manager then disables the plugin again and answers `HTTP 500` `PLUGIN_FAILED`
    (`packages/kernel/manager.ts:108-116`).

  So the expected observation is:
  - O2's `plugin true` returns `HTTP 500` with `PLUGIN_FAILED`;
  - `cap plugins plugins` then shows `worker-budget-authority` `disabled`;
  - the gate is open (`open-B` output) and the positive control succeeds.

  There are no mounted IDs to query. This is the accepted bootstrap pass (ailab #1820), also recorded
  in the budget runbook's drill protocol. If the live Router instead starts the plugin and logs
  `Worker budget authority unavailable`, that is the original runbook's pass criterion, observed as
  in "Observing the Router".
- **B3. Soft delete.** First record the Router Pod's UID with
  `cap router-pod-before kubectl --context admin@ai -n llm-router get pod -l app=llm-router -o jsonpath='{.items[*].metadata.uid}'`.
  Then run `bao kv delete -mount=af relay-budget/witness`, the runbook's `bao kv delete` with the mount
  and path written out, and `force_sync`. Wait, bounded, for both ExternalSecrets to report the
  failure. The waits only read:

  ```sh
  cap es-relay kubectl --context admin@ai -n relay wait --for=condition=Ready=false externalsecret/relay-worker-budget --timeout=3m
  cap es-router kubectl --context admin@ai -n llm-router wait --for=condition=Ready=false externalsecret/llm-router-worker-budget --timeout=3m
  cap secret-hash-relay wsha_secret relay relay-worker-budget
  cap secret-hash-router wsha_secret llm-router llm-router-worker-budget
  cap router-pod-after kubectl --context admin@ai -n llm-router get pod -l app=llm-router -o jsonpath='{.items[*].metadata.uid}'
  ```

  Pass:
  - both ExternalSecrets go not Ready;
  - both Secrets remain, still with the empty-witness hash (ESO Retain);
  - the Router Pod's UID is unchanged: nothing changes until a deliberate roll.

  If a wait times out, read the ExternalSecret's status and events before treating it as a failed
  readback: ESO may not have reconciled yet.
- **B4. Run R.** A put after a soft delete writes a new live version. Both ExternalSecrets are Ready
  again.

**Drill C, malformed witness.**

- **C1.** Run W with step 3 = `printf '%s' '{"version":1}' | bao kv put -mount=af relay-budget/witness witness=-`.
  The expected sha256 is that of `printf '%s' '{"version":1}' | sha256sum`.
- **C2. Run O.** Pass: as in B2.
- **C3. Run R.**

**Drill D, older snapshot with an unchanged witness.**

- **D1.** `cap row-before-D ledger_state`.
- **D2.** Run W, with step 3 being:
  - `bao kv put -mount=af relay-budget/witness witness=- < "$ev/witness-B.json"`, a new version with
    the same bytes, which is not a record entry;
  - then `bao kv rollback -mount=af -version=<the previous version, which holds B since C3> relay-budget/witness`.

  The runbook gives this step as prose ("Roll back to the previous version"), so the command is NEW.
  Use `bao kv metadata get -mount=af relay-budget/witness` for the version numbers. B's sha256 is
  expected.
- **D3.** Re-open as in O1-O3, with `t0="$(date -u +%FT%TZ)"` set just before O1's `lwrite open-B`.
  Then the Available captures, as R step 3 takes them: `cap reader rreader`, at least 10 s later
  `cap reader-after-10s rreader`, at least 20 s after the commit `cap errors lerrors "$t0"`, and
  `cap ident rident`. Pass, as far as this window can show it:
  - the Router is available, as "Observing the Router" defines it;
  - the Router's mounted witness equals B and the record's latest entry.

  Then withdraw, and run `cap row-after-D ledger_state`: equal to D1 in every column. The history
  comparison (allocations, `token_limit`, `original_deadline`, `recovery_epoch`, outbox sequences,
  receipts) runs when D is repeated with the Relay-wired drills.

Available (observed as above) proves the gate and the witness, not qualification. The Router
accepts a snapshot with no qualification row (`runtime.ts:20-21`, `:89-96`). File B's pre-signing
check is `file_b_router_check`, defined in the budget runbook's
[File B pre-signing readback](relay-worker-budget-authority.md#file-b-pre-signing-readback).
At Router `5a5181b7` the authenticated preflight durably consumes a nonce
(`packages/plugins/relay-keys/index.ts:23-25`, `service.ts:34-50`). The owner accepted that one
replay-set write on 2026-11-19; the check performs it only behind the explicit `--router-preflight`.
A ledger pass alone does not prove Router qualification.

**Readback, end of step 5.**

- **Witness.** The record's latest entry is B. `wsha_bao` and both `wsha_secret` show B's hash, and so
  does `wsha_router`. `wsha_relay` still shows A, until H6.
- **Ledger and plugin.** `cap final ledger_state`: the authority row has B's IDs and `ready=f`. The plugin
  is `disabled`.
- **History.** Step 4's five history counts are unchanged (0).
- **One outage per roll.** At least 11 minutes after the last `roll_router` (one `apps` interval of
  10m, so Flux has reconciled the Router since), run
  `cap router-final rollstate llm-router llm-router app=llm-router`. The revision equals
  `${ROLLREV[llm-router/llm-router]}`, the one Pod has that revision and the last roll's creation time,
  and `restartedAtOwners` is only `relay-runbook`/`Update`. Across this step's `router-roll` captures
  the revision rises by exactly 1 per roll, 8 on the normal path. A higher final revision means
  something else rolled the Router: record it on #1753.
- **Clean up.** Run `kubectl --context admin@ai -n relay-budget-authority delete pod relay-budget-operator-client`.
- **Evidence.** Post one comment per drill on ailab #1753, titled "Witness drill A", "… B", "… C" and
  "… D". Build each from that drill's files in `$ev/cap`:
  - the O3 captures from the same window as the observation;
  - the Router log captures, including those kept before each roll (empty at this pin), and the
    `reader`, `reader-after-10s`, `errors` and `ident` captures;
  - the mounted hashes;
  - the SQL outputs and the before and after diffs.

  Never post DSNs, passwords or tokens (`relay-worker-budget-authority.md:467-481`).

**Stop and rollback.**

- **A drill readback fails.** Run F. It never re-opens.
- **B cannot be restored and verified.** Stop and escalate to the owner. Authority stays withdrawn.
- **Never rewrite the authority row to match a witness** (`relay-worker-budget-authority.md:398-399`).

## Step 6. Durability evidence, then the independent review

**Purpose.** "After independent review, set `worker_budget.authority.ready=true`"
(`relay-worker-budget-authority.md:236`). "The production `ready=true` (step 8) waits for all four
drills to be reviewed" (`:481`). The owner decided (open point 7) that it also waits for the durability
evidence, which 6a produces and 6b reviews.

**Writes:** 6a: **Kubernetes**: the disposable probe (namespace `relay-budget-durability-probe` and its
objects, its PV's reclaim policy and label), deleted again the same day; the production ledger Pod's
deletion for (iii). With the owner's explicit OK on #1753 only: tier (b)'s `qm reset` of the probe's
node VM, tier (c)'s 60 s host firewall rule. 6b: two comments on #1753.

**Restarts:** **the production ledger Pod**, once ((iii), `OnDelete`, authority withdrawn and plugin
disabled); every Pod on the probe's node if tier (b) runs.

### 6a. Durability evidence

Run the budget runbook's "Durability evidence for the first `ready=true`" in this session (its
commands use step 0's and step 5's helpers): (i); (ii) with tier (a), and tiers (b) and (c) only with
the owner's explicit OK on #1753; the NAS-side record of its "Limit"; then (iii). (iii) runs here,
after step 5: the drills leave `ready=f` with B's IDs and the plugin disabled, the state it asserts
before it restarts the ledger Pod. Post each part on #1753 as that section says.

**Stop.** A chain or comparison that fails keeps `ready=false`; the result goes to the owner.

### 6b. The independent review

**The reviewer (owner decision 2026-11-17, open point 8).** Two parts, both on #1753:

1. **A structured Codex review** of the full scope below, read-only, each item checked against its
   criterion, with a VERDICT per item. Its output is posted verbatim as a comment titled "Step 6
   review".
2. **The owner's explicit acceptance**, a later comment that names the review comment and accepts it.

**A known limit of independence.** The owner took part in the drills: the `ready` flips (the protocol's
re-opening and withdrawals) and the rotation to B. The owner's acceptance is therefore not independent
of those parts; the review is the independent read of them. The acceptance comment states this limit.

**What is reviewed: the full step-6 scope.**

- Step 4's proofs: the `step8-proofs` capture and the outputs of the TLS, refusal and privilege proofs.
- The four drill comments on #1753 ("Witness drill A/B/C/D") and the end-of-step-5 readback.
- The durability evidence: (i), (ii) and (iii) of the budget runbook's "Durability evidence for the
  first `ready=true`", and the NAS-side record of its "Limit", as posted on #1753 (open point 7).

**Commands.** NEW (not in the runbook). The review directory holds the reviewed runbooks and the
non-secret evidence, nothing else:

```sh
rv="$(mktemp -d)"
git -C '<operator fills: the ailab checkout>' archive '<operator fills: the reviewer-merged ailab commit in use>' docs/runbooks \
  | tar -x -C "$rv"
# This endpoint returns every comment of the issue in one answer: it takes since/before, not pages
# (on 2026-11-17, limit=3&page=2 returned all 8 comments). The listing below is the check that every
# evidence comment (drills A-D, durability (i)-(iii)) is in it.
# scripts/gitea-api.sh requires the operator workstation's non-admin workstation-bot identity.
scripts/gitea-api.sh GET /repos/cchifor/ailab/issues/1753/comments > "$rv/issue-1753-comments.txt"
sed 1d "$rv/issue-1753-comments.txt" | jq -r '.[] | "\(.id) \(.created_at) \(.body | split("\n")[0])"'
mkdir "$rv/step4"; cp '<operator fills: the files holding step 4 readbacks, the step8-proofs capture among them>' "$rv/step4/"
# No secret may reach the reviewer: a DSN with a password, a private key, an OpenBao token, a password
# value. grep exits 1 only when it read everything and found none; anything else stops here.
grep -rlE 'postgresql://[^:@/<> ]+:[^@<> ]+@|BEGIN [A-Z ]*PRIVATE KEY|hvs\.[A-Za-z0-9_-]{20,}|password=[^-<[:space:]]' \
  "$rv/issue-1753-comments.txt" "$rv/step4"; scan=$?
[ "$scan" -eq 1 ] && echo "no secret patterns" || echo "STOP: scan=$scan (0: clean the files listed, then scan again; 2: the scan failed)"
cat > "$rv/REVIEW-PROMPT.txt" <<'PROMPT'
You are the independent reviewer of step 6 of docs/runbooks/relay-researcher-remaining-live-steps.md
(ailab). Work READ-ONLY: read the files in this directory and nothing else. Run no command that writes,
and contact no network, cluster, database, OpenBao or Router.

Inputs: docs/runbooks/ (the criteria, at the reviewed ailab commit); issue-1753-comments.txt (every
comment of ailab #1753, the posted evidence, as the Gitea API returned it); step4/ (step 4's readbacks).

Check every item below against its criterion. Quote the evidence (comment id, or file and line) and the
criterion (runbook file and section). Evidence that is missing, unreadable, from the wrong window, or
contradicted elsewhere is not a pass. Do not accept a claim that is not backed by a readback.

S4.1  TLS with hostname verification, both logins, TLSv1.3. Criterion: checklist step 4, "The proofs".
S4.2  Refusals: a name not on the certificate (verify-full), plaintext, postgres over TCP, table reads
      (42501). Criterion: checklist step 4, "The proofs".
S4.3  in_recovery f; fsync, full_page_writes, synchronous_commit, data_checksums all on. Criterion:
      checklist step 4, "Expected".
S4.4  Login roles, function grants and table privileges exactly as checklist step 4, "Expected".
S4.5  History counts all 0; the witness three-way comparison agrees, five equal hashes. Criterion:
      checklist step 4, "Expected".
DA..DD One item per drill A, B, C, D. Each comment has captures timestamped just before the
      observation that prove the gate open (authority row ready=t, positive control), the Router
      observation as checklist step 5 "Observing the Router" defines it (ledger-side reader, error
      and identity captures, owner decision 2026-11-20). That substitute applies only while the
      drill comment records an llm-router pin whose plugin log does not reach kubectl logs
      (5a5181b7, llm-router #191 open); on a pin with #191 fixed, the Router log line is required too,
      the mounted witness SHA-256, the SQL outputs and before/after diffs, and the drill's pass:
      - A: the plugin enabled (HTTP 200, active) and the pass of the budget runbook's table,
        "Drills before production ready=true", as checklist step 5 runs it;
      - B: B2, the pass decided at checklist open point 4: the enable answered HTTP 500
        PLUGIN_FAILED and the plugin stays disabled, while the gate is open and the positive control
        succeeds (or, if the Router started the plugin, the originally documented unavailable pass);
        and B3, the soft delete: both ExternalSecrets not Ready, both Secrets still holding the empty-witness hash,
        the Router Pod UID unchanged;
      - C: as B2 (checklist step 5, C2);
      - D: checklist step 5's D3, the Router-side scope of this window: the Router available as
        "Observing the Router" defines it, the Router's mounted witness equal to B and the record's latest entry, the
        authority row equal before and after. Relay still mounts A until H6; verify its mounted B
        at H6. R0 covers A-C only. D's later history comparison still needs a separately scheduled
        Relay-wired repeat, not a FAIL in this pre-allocation window.
      A refusal without same-window captures of the open gate does not count.
DE    After the drills: the record's latest entry is B, B restored and verified, the authority
      withdrawn, the authority row never rewritten to match a witness. Criterion: checklist step 5,
      "Readback, end of step 5" and "Stop and rollback".
DUR1  Evidence (i). Criterion: budget runbook, "Durability evidence for the first ready=true", (i).
NAS   The NAS-side guarantee of that section's "Limit": what QuTS states for the ledger's LUN
      write cache and the pool's synchronous-write policy, with the owner's explicit acceptance or
      refusal. Criterion: the same section, "Limit".
DUR2  Evidence (ii): same image, flags and schema (the StatefulSet diff, the bundle receipt, the
      functions_md5 equal to production's); throwaway identities that differ from production's;
      for each tier run: its command and UTC time, the PostgreSQL log lines, "lost or changed: 0",
      "missing 0", integrity zeros and every index checked; tiers (b) and (c) only with an owner OK
      on #1753 naming node, window and form, and their after-captures; the teardown (namespace, PV and
      LUN gone); no witness in OpenBao. Criterion: the same section, (ii).
DUR3  Evidence (iii): the maintenance procedure followed (ready=f, plugin disabled, at least 20 s,
      drained); rows and markers identical; the identity identical except postmaster_started; a
      clean-shutdown log line. Criterion: the same section, (iii).
SEC   No secret anywhere in the evidence: no password, token or private key, and no DSN with its
      password (the rendered `dsn` files). Host names, ports and sslmode are not secrets.

Output, one line per item, exactly:
<item>: VERDICT: PASS | FAIL | MISSING - <evidence location> - <criterion> - <one-sentence reason>
Then one final line: VERDICT: OK if every item is PASS, otherwise VERDICT: ISSUES.
PROMPT
# The directory is not a git checkout, hence --skip-git-repo-check; -s read-only is the sandbox.
# It runs only after a clean scan.
[ "$scan" -eq 1 ] && codex exec -s read-only --skip-git-repo-check -C "$rv" - < "$rv/REVIEW-PROMPT.txt" | tee "$rv/review.txt"
```

Post `review.txt` on #1753 as "Step 6 review", with the ailab commit and the list of evidence comment
ids it read. Keep the review directory with the raw evidence.

**Readback.** On #1753: the "Step 6 review" comment ends `VERDICT: OK`, and the owner's later comment
accepts it, names the known limit of independence, and is the acceptance step 7 needs.

**Stop.** Any FAIL, MISSING or owner finding keeps `ready=false`. Fix it, repeat the affected proof,
drill or durability tier, post the new evidence, and run the review again over the full scope.

## Step 7. `worker_budget.authority.ready=true`

**Purpose.** The production opening of the ledger gate (the last sentence of budget step 8).

**Writes:** **ledger**, one row.

**Restarts:** none.

**Pre-checks.**

- Step 6 is accepted: "Step 6 review" ends `VERDICT: OK`, and the owner's acceptance names it.
- **Durability gate (hard stop).** (i), (ii) and (iii) of the budget runbook's "Durability evidence
  for the first `ready=true`", and its "Limit" (the NAS-side record), are posted on #1753 and accepted
  there by the owner (open point 7, decided 2026-11-17). Without that this step does not run. The same section's confirmation after R1
  is a later gate, before R2 is signed.
- The NTP readback (124071 §6) is repeated.
- The three-way witness comparison agrees on B (`openbao-recovery.md:215-218`), and `wsha_router`
  shows B.
- The plugin is `disabled`.
- There is no Relay router connection yet.

**Commands.** This is NEW (not in the runbook); the runbook says only "set it explicitly". It uses the
`open-B.sql` file of step 5, unchanged. The owner confirms the file:

```sh
lwrite open-B                                   # open-B COMMITTED
cap gate-after-open lsql <<<"SELECT worker_budget.gate('<B instanceId>', '<B recoveryEpoch>', '<B witnessDigest>');"
```

**Readback.**

- The `open-B` output shows the row `t | <B ids>`.
- The separate `gate(...)` call, after the commit, returns without an error. While the gate is closed
  it raises `42501: Worker budget authority unavailable`, as it did at step 7A (#1753 comment 124073).
- Kubernetes readiness of the ledger Pod is not this gate and proves nothing here
  (`relay-worker-budget-authority.md:237-238`).

**Stop and rollback.** `lwrite withdraw` (`ready=false`), as "Changing the witness" step 1 does.

## Step 8. Router plugin enabled (budget step 9, after the bootstrap gates)

**Purpose.** Enable the Router's `worker-budget-authority` plugin. Budget step 9 puts it after its
**bootstrap gates** (owner decision 2026-11-17, open point 5; `relay-worker-budget-authority.md`, step 9).

- H6 needs it: the plugin enabled is an H6 precondition (`relay-researcher-hub-upgrade.md:324-327`,
  and the table at `:469`).
- The step-7 plan places it between File A and File B (124071 §5).
- File B is not one of those gates. Without `qualifications` rows the ledger refuses every allocation
  (`42501` `Worker budget qualification unavailable`, budget step 9), so this step proves availability
  only. File B is committed, read back and qualified by the Router before any candidate run is signed
  (hub H7).

**Writes:** **Router admin API**: the plugin state, which the Router keeps in its own store. No
manifest enables it.

**Restarts:** none. The plugin starts in the running Router.

**Pre-checks.**

- Step 7's readback holds.
- **Bootstrap gates (hard stop; budget step 9).** Each is read back in this window. Without them, this
  step does not run.
  - Step 8 of the budget runbook is complete: step 4's proofs, the drills reviewed and the durability
    evidence accepted (step 6), and step 7's readback (`ready=t` with B's IDs, `gate(...)` returns).
  - The trust is included: step 2's readback (`plugins` shows `relay-keys` active; the mounted trust's
    SHA-256 equals `trust | sha256sum`).
  - The Router's `ROUTER_WORKER_BUDGET_DEPLOYMENT_DIGEST` is `547353ac…1a261ad`, and the `clients` and
    `readers` rows (File A) are unchanged: step 0's baseline readback, those lines.

**Commands.** Verbatim (`relay-worker-budget-authority.md`, step 9):
`POST /admin/v1/plugins/worker-budget-authority` with `{"enabled":true}`. With step 5's helper, that
is `plugin true`.

**Readback.**

- Verbatim (`relay-worker-budget-authority.md`, step 9): `GET /admin/v1/plugins` shows the plugin active (run `plugins`), and the
  Router log has `Worker budget authority available`. At llm-router `5a5181b7` that line never reaches
  `kubectl logs` (llm-router #191), so it is observed as step 5's "Observing the Router" defines
  (owner decision 2026-11-20): `cap plugin-on-reader rreader`, at least 10 s later
  `cap plugin-on-reader-after-10s rreader`, and
  `cap plugin-on-errors lerrors "$t0"` at least 20 s later (`t0="$(date -u +%FT%TZ)"` set just before
  `plugin true`), with no
  ERROR, FATAL or PANIC line, and `cap plugin-on-ident rident`. Also keep `cap plugin-on rlog`.
- From 124071 §4.1: the Router stays available, with no `snapshot_invalid` or `policy_digest_invalid`.
  Until #191 is fixed, those two are the accepted residual of "Observing the Router".
- This proves the gate and the witness, not qualification. The Router accepts a snapshot with no
  `qualifications` row (llm-router `runtime.ts:20-21`, `:89-96`).

**Stop and rollback.** `plugin false`. If the gate must close as well, also run `lwrite withdraw`
("Changing the witness" step 1).

## Step 9. Escrow token (H5, second half) and `relay-router-openbao-tls`

**Purpose.** Mint the escrow token that Relay uses for the escrow (Router connection step 9,
`relay-router-connection.md:233-241`, readback `:310-316`; hub H5). Its 720 h lifetime starts when it
is minted: mint it only when the owner has scheduled the H6 rollout, "so H6 follows promptly"
(`relay-researcher-hub-upgrade.md:312-316`).

**Writes:**

- **OpenBao**: one token from token role `relay-router-escrow`;
- **OpenBao**: the document `af/relay-router/relay/escrow-token` (field `token`);
- **Kubernetes**: one `force-sync` annotation on `relay/relay-router-files`. ESO then renders its
  Secret with the private keys and the token.

**Restarts:** none.

**Pre-checks.**

- Steps 7 and 8 hold.
- `bao read auth/token/roles/relay-router-escrow` is as in step 0.
- `relay-router-files` is still not Ready.
- **`relay-router-openbao-tls`.** H6 needs exactly one thing from it: the key `ca.crt`. Two things use
  it:
  - The `relay-router-connection` component projects it, without `optional`, as
    `/run/relay-router/openbao-ca.crt`, which is `NODE_EXTRA_CA_CERTS`
    (`kubernetes/components/relay-router-connection/deployment.yaml`). This is the CA for the
    in-cluster OpenBao origin.
  - SecretStore `relay-router-store` uses the same key to reach OpenBao
    (`kubernetes/apps/relay-router-connection/credentials/relay.yaml`), so `relay-router-files` cannot
    sync without it.

  Its `tls.crt` and `tls.key` belong to a throwaway leaf certificate and are not used. Confirm it by
  name and keys only (NEW):

  ```sh
  kubectl --context admin@ai -n relay get certificate relay-router-openbao-ca \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status} {.spec.issuerRef.name} {.spec.secretName}{"\n"}'   # True ailab-ca relay-router-openbao-tls
  kubectl --context admin@ai -n relay get secret relay-router-openbao-tls -o json | jq -c '.data | keys'           # ["ca.crt","tls.crt","tls.key"]
  kubectl --context admin@ai -n relay get secretstore relay-router-store -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}{"\n"}'   # True
  ```

  If it is missing at H6, the Recreate rollout leaves the new Relay Pod in `ContainerCreating` and the
  hub down (#1811).

**Commands.** Verbatim (`relay-router-connection.md:235-241`):

```sh
umask 077; d="$(mktemp -d)"
bao token create -role=relay-router-escrow -display-name=relay-router-escrow -format=json > "$d/t.json"
jq -r .auth.client_token "$d/t.json" | bao kv put -mount=af relay-router/relay/escrow-token token=-
jq -r .auth.accessor "$d/t.json"     # record the accessor (not secret) in the operator evidence
shred -u "$d/t.json"; rmdir "$d"
```

Run it one line at a time. The order of the cleanup matters, because the accessor is the only handle
for revoking an orphan token, and it lives in `$d/t.json` until line 4 records it:

- **Line 2 fails.** The outcome is uncertain: OpenBao may have created the token before the response
  was lost. Do **not** shred yet.
  - If `jq -r .auth.accessor "$d/t.json"` prints an accessor, record it and revoke it.
  - Otherwise, look for any token from the role with the NEW scan below, and revoke each accessor it
    prints with `bao token revoke -accessor <accessor>`. A lookup by accessor never returns the token
    itself.
  - Only then shred and remove `$d`, and stop.

  ```sh
  bao list -format=json auth/token/accessors | jq -r '.[]' | while read -r a; do
    bao token lookup -format=json -accessor "$a" \
      | jq -r --arg a "$a" 'select(.data.display_name == "token-relay-router-escrow") | [$a, .data.creation_time, .data.expire_time] | @tsv'
  done
  ```

- **Line 3 fails.** A token exists. Do **not** shred yet. Run line 4 and record the accessor, revoke
  the token with `bao token revoke -accessor <accessor>` (the runbook's "Rollback", `:335-336`), and
  only then shred and remove `$d`. Then stop.

**Readback.** First verbatim (`relay-router-connection.md:310-316`), with the admin token:

- `bao token lookup -accessor <accessor>` shows `policies [relay-router-escrow]`, `orphan true`,
  `renewable false` and its `expire_time`;
- `bao write sys/capabilities-accessor accessor=<accessor> paths=af/data/relay-router/escrow/00000000-0000-4000-8000-000000000001/x/y`
  is `create, read, update`;
- the same for `af/data/relay-budget/witness` is `deny`;
- `relay-router-files` becomes Ready with exactly `admin-key.pem`, `reconciler-key.pem` and
  `escrow-token`.

Then this block, which is NEW. The force-sync uses the form of the runbook's rotation paragraph
(`:245`):

```sh
kubectl --context admin@ai -n relay annotate externalsecret relay-router-files force-sync="$(date +%s)" --overwrite
kubectl --context admin@ai -n relay wait --for=condition=Ready externalsecret/relay-router-files --timeout=3m   # condition met
kubectl --context admin@ai -n relay get secret relay-router-files -o json | jq -c '.data | keys'   # ["admin-key.pem","escrow-token","reconciler-key.pem"]
for k in admin-key.pem reconciler-key.pem; do
  kubectl --context admin@ai -n relay get secret relay-router-files -o json | jq -r --arg k "$k" '.data[$k]' | base64 -d \
    | openssl pkey -pubout -outform DER | sha256sum                                       # = step 1's admin / reconciler fingerprint
done
```

**Stop and rollback.**

- **A command in the block fails.** Follow the line rules under "Commands".
- **The wait times out.** Read the ExternalSecret's status and events first: a slow sync is not a
  failed readback.
- **A readback is wrong** (policies, capabilities, keys or fingerprints). Revoke the token
  (`bao token revoke -accessor <accessor>`, from the runbook's "Rollback", `:335-336`) and stop.
  Nothing consumes the Secret before H6.
- **H6 is delayed.** The token must be rotated before 75 % of its 720 h (day 22), following the
  runbook's "Rotation" (`:243-248`).

## Step 10. H6 precondition readback

**Purpose.** One readback of every H6 precondition, before the owner lifts #1811's hold. The list is
the hub runbook's H6 preconditions (`relay-researcher-hub-upgrade.md:322-327`) and #1811's body.

**Writes:** none. **Restarts:** none.

**Readback.**

- **Ledger:** `ready=t` with B's IDs (step 7).
- **Router:** `worker-budget-authority` is active and available, observed as in step 8. `relay-keys` is active (step 2).
- **`relay/relay-router-files`:** Ready, with the three keys (step 9).
- **`relay/relay-router-openbao-tls`:** has `ca.crt`, and its Certificate is Ready (step 9).
- **`relay/relay-control-metrics`:** has the three keys. **`monitoring/relay-control-collector`:**
  has `token`. They match (step 3).
- **Hub:** still Relay v0.10.4 (`e77bd09a`), schema 79, converged, with H3's and H4a's settings in the
  running Pod. A different hub is a stop. This check is NEW:

  ```sh
  kubectl --context admin@ai -n relay rollout status deploy/relay --timeout=1m
  kubectl --context admin@ai -n relay get deploy relay -o json \
    | jq -c '{source: .spec.template.metadata.annotations["chifor.me/source-commit"], release: .spec.template.metadata.annotations["chifor.me/release"]}'
  kubectl --context admin@ai -n relay get pod -l app=relay -o json | jq -c '.items[] | {pod: .metadata.name, phase: .status.phase,
      image: (.status.containerStatuses[] | select(.name=="relay") | .imageID),
      env: [.spec.containers[] | select(.name=="relay") | .env[]
            | select(.name | test("^RELAY_(RECOVERY_GENERATION_FILE|INFRASTRUCTURE_INSTALLATIONS|WORKER_BUDGET_|ROUTER_CONNECTIONS_FILE|AGENT_CONTROL_PLANE)"))
            | {name, value}]}'
  kubectl --context admin@ai -n relay exec relay-postgres-0 -c postgres -- \
    psql -X -U postgres -d relay -Atc 'SELECT count(*), max(version) FROM schema_migrations'
  ```

  Expected:
  - The rollout is complete, with exactly one Running Pod.
  - Source `e77bd09a4d9179d81ebf5d01ac26c535d1eb8433`, release `relay-0.10.4-release-134512`.
  - The image ID ends in
    `sha256:d1482bc101ecf13667a75bfc1892f78b37bed7cf2fed8f9ce42f8c4513d057a7`
    (`kubernetes/apps/apps/relay/relay.yaml`).
  - The env has the generation file, `RELAY_INFRASTRUCTURE_INSTALLATIONS` `1`, and the three
    `RELAY_WORKER_BUDGET_*_FILE` paths under `/run/relay-worker-budget/`. It does not yet have
    `RELAY_ROUTER_CONNECTIONS_FILE` or `RELAY_AGENT_CONTROL_PLANE`, which H6 adds.
  - The schema reads `79|79`. Those values are plain file paths and flags, not secrets.

## After this checklist

1. **H6.** The owner lifts #1811's hold. Read back as in hub H6 and Router connection step 10. Also:
   - Relay's mounted `witness.json` (`wsha_relay`) equals the record's latest entry, B. The drills
     rotated the witness after H4a's roll, and H6's Recreate rollout is what mounts B.
   - Budget startup `start_failed` and `RelayWorkerBudgetStartupFailed` are expected until H7.
2. **The candidate journal** on `10.96.0.42`. It is under Flux at zero replicas since ailab #1813,
   and its activation is its own held PRs
   ([relay-worker-candidate-journal.md](relay-worker-candidate-journal.md), "Activation under Flux").
3. **File B,** budget step 7's `qualifications` row (#1753 comment 124071). It is generated from live
   Router readings taken after the last Router rollout: this checklist's step 5 rolls, or any later
   Router change. **File A and File B are separate.** File B must be committed, read back and
   qualified by the Router **before any candidate run is signed (R0 first)**. Retain the reviewed
   rows as `$ev/file-b.json`, set `FILE_B_REQUIRED_LIFETIME_SECONDS` to cover the interval from the
   check through the original worker deadline, set `FILE_B_ROUTER_LIMIT` to the run's
   `inferenceTokenBudget`, and define the exact `file_b_router_check` helper in
   [budget File B pre-signing readback](relay-worker-budget-authority.md#file-b-pre-signing-readback).
   Run from this repository root, immediately before signing:

   ```sh
   file_b_router_check
   ```

   `ledger: pass` means an exact, fresh, complete-table readback, canonical policy digests,
   matching route/bounds and sufficient expiry. The Router half is the pinned Router's
   authenticated, non-allocating preflight, signed as Relay's reconciler with the key piped from
   `relay/relay-router-files`. It **durably writes one delegation nonce** to the Router's replay set
   for `relay.chifor.me/00000000-0000-4000-8000-000000000001`
   (`5a5181b7:packages/plugins/relay-keys/service.ts:34-50`), which the owner accepted on
   2026-11-19. The tool prints that, and the scope's readiness slots (12 per scope), before and after
   the call. Only exit 0 (`status: pass`, `signingReady: true`) qualifies File B. Each run writes one
   nonce: never retry it automatically. The zero-write `file_b_check --json` still reports
   `router: unavailable` and exits 1, and
   `file_b_check --skip-router-preflight-unavailable --json` exits 2 (evidence collected, not
   signable) with `status: ledger-only` and `signingReady: false`; neither qualifies File B or
   permits signing. The budget section documents the design choice, the inputs and all source
   citations.
4. **The E6 rehearsal**
   ([relay-researcher-qualification-campaign.md](relay-researcher-qualification-campaign.md) §4).
5. **R0, then R1.** R0's per-run PR first includes H7's candidate component for Relay's half of drills
   A-C, with the agent suspended and no grant or create. Reset R0 before signing R1; R1's per-run PR
   includes H7 again ([relay-researcher-r0-relay-drills.md](relay-researcher-r0-relay-drills.md)).

## Open points for the owner

Points 1-8 below record the 2026-11-17 decisions in
[ailab #1820](https://git.chifor.me/cchifor/ailab/issues/1820); they are not undecided proposals.
Their acceptance evidence remains on #1753. R0's additional confirmations are in its own §4.

1. **Collector token escrow. Decided 2026-11-17:** escrow at `af/relay-control/collector`; the path class is
   in `openbao-recovery.md`.
2. **Metrics tenant and expiry. Decided 2026-11-17:** the tenant `00000000-0000-4000-8000-000000000001` and
   the collector expiry `2027-02-15T00:00:00Z` (step 3).
3. **Relay in the drills.** Relay is wired since H4a, and the budget runbook's drill text predates that.
   This checklist rolls only the Router, and Relay mounts B at H6. When does Relay's half of drills A-C
   run? Relay's budget authority starts only at H7, inside the first signed run's readback window. **Decided
   2026-11-17:** drill run R0 before R1 ([relay-researcher-r0-relay-drills.md](relay-researcher-r0-relay-drills.md)).
4. **Drills B and C on the current Router.** On llm-router `5a5181b7`
   (`packages/plugins/worker-budget-authority/index.ts`, `packages/kernel/manager.ts:108-116`) the
   witness is parsed when the plugin starts. With an empty or malformed witness, enabling the plugin
   therefore answers `HTTP 500` `PLUGIN_FAILED` and the plugin stays disabled. The runbook's pass ("Router
   unavailable, as in A", with the plugin enabled) cannot be observed. Is the refused enable, with the
   gate proven open, accepted as these drills' pass (step 5, B2)? **Decided 2026-11-17: yes.**
5. **"The qualification gates" of budget step 9.** Which gates are meant? H6 requires the plugin on,
   File B comes after H6, and an `available` Router proves no qualification. The owner names them, and
   a follow-up PR writes them into the budget runbook. **Decided 2026-11-17: a bootstrap exception.**
   The plugin may be enabled after budget step 8 and the Router-side readbacks (trust, deployment digest,
   File A); File B is not needed to enable it, because the ledger refuses every allocation without it,
   and is mandatory before any candidate run is signed. Budget step 9 now lists the gates (step 8).
6. **Trust inclusion without File B. Decided 2026-11-17: included without File B** (step 2 DONE).
   Router connection step 6 agrees with the hub runbook: File B follows H6 and the final Router
   rollout, before any run is signed.
7. **Durability evidence for `ready=true`.** The runbook asks for measured QNAP/CSI power-loss
   durability and an acknowledged test allocation through controlled interruptions. The second
   cannot exist before H6. Which evidence does the owner accept now, or which reviewed runbook change
   defers that part? Step 7 is blocked until this is settled. **Decided 2026-11-17:** (i) the settings,
   (ii) a measured probe on a disposable second ledger through interruption tiers, (iii) a committed-row
   comparison across one planned production restart, and a confirmation after R1. The procedure is the
   budget runbook's "Durability evidence for the first `ready=true`"; step 7's blocker points there.
8. **The independent reviewer** for step 6. **Decided 2026-11-17:** a structured Codex review of the
   full step-6 scope plus the owner's explicit acceptance, both on #1753, with the owner's part in the
   drills noted as a known limit of independence (step 6).
9. **Resolved documentation correction.** Router connection step 3's readback now says `maxTokens`
   7441536, matching the committed template; there is no placeholder left to replace.
