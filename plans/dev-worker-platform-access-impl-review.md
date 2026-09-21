# Implementation review — dev-worker-platform-access — round 1

Reviewer: codex `gpt-6-astra` (xhigh) on reviewer-2 seat c, with the finalized plan and the full
`48c5efae..c4735c84` diff pasted inline (bwrap cannot start on that host, so Codex cannot read
files). 16 findings, 3 of them blockers. Every load-bearing claim was checked against the live
cluster before it was acted on; the fixes are commit `b45b3359`.

## Findings

### The psql helper cannot start its port-forward
**Location:** `ansible/roles/dev_worker/files/platform` — `port-forward "svc/$svc" "127.0.0.1:$port:5432"`
**Severity:** blocker
**ACCEPTED — verified.** `kubectl port-forward svc/strive-pg-ro 127.0.0.1:15999:5432` against the
live cluster answers `error: Service 'strive-pg-ro' does not have a named port '127.0.0.1'`; the
two-part spec with `--address 127.0.0.1` works. Fixed in both `pf` and `psql`; the helper test now
asserts the working form and **forbids** the broken one, so the permissive stub cannot hide it again.

### Every worker can observe another worker's rotating password
**Location:** `platform-access/pg-sync.yaml` — `ALTER ROLE {} WITH PASSWORD %s` + `GRANT pg_monitor`
**Severity:** blocker
**ACCEPTED — verified.** A throwaway role with `pg_monitor` read `app`'s query text verbatim out of
`pg_stat_activity` on strive-pg (probe role dropped immediately after). So a cleartext rotation
statement would hand slot B's new password to any slot A polling that view, and `pods/log` is a
second path to the same string. Fixed by computing a **SCRAM-SHA-256 verifier client-side** — the
password never reaches the server — plus `log_statement = none` / `log_min_duration_statement = -1`
on the sync's own session. The integration test asserts `rolpassword` starts `SCRAM-SHA-256$`, that
the published password does not appear inside the verifier, that the credential still authenticates,
and that no password reaches the server log.

### Concurrent syncs can publish an already-invalid password
**Location:** `platform-access/pg-sync.yaml` — the separate proof/publish loops, `concurrencyPolicy: Forbid`
**Severity:** blocker
**ACCEPTED.** `Forbid` serialises a CronJob against itself only; the bootstrap Job and any
`kubectl create job --from=cronjob/…` are separate objects. Fixed with a cluster-wide
`pg_try_advisory_lock` taken before the first KV read and held (session-level, on its own connection)
until after publication and retirement; a second run exits cleanly saying the other run owns it.

### Port forwarding does not enforce the advertised service read-only boundary
**Location:** `platform-access/rbac.yaml` — `pods/portforward`
**Severity:** important
**ACCEPTED as a correction to the claim, not as a code change — verified.** `valkey-master` runs with
`ALLOW_EMPTY_PASSWORD=yes`, so a forward to it could write. RBAC cannot scope a forward to a Service,
and pinning pod names in `resourceNames` breaks on every CNPG instance rename, so the honest fix is
to stop claiming what is not enforced: ADR 0028 §2 now states the boundary (fine for Postgres, which
is read-only by privilege, and for anything that authenticates its own callers; not a general write
boundary), the runbook lists it second in "what is not", and the agents' CLAUDE.md block says
"inspect through a forward; never mutate". Service-side auth (Valkey `requirepass`) is named as the
real fix, in the platform repo.

### Connection exhaustion is treated as a bad password
**Location:** `platform-access/pg-sync.yaml` — `except psycopg2.Error: return False` in `login_works`
**Severity:** important
**ACCEPTED.** Replaced by a three-state probe: `ok` / `rejected` (SQLSTATE 28xxx, or an explicit
"password authentication failed" / missing role) / `unavailable`. Only `rejected` rotates;
`unavailable` exits the run **without touching Postgres or KV**, so the working password stands.
Tested by setting `CONNECTION LIMIT 0` and asserting neither the password nor the KV write count
changes.

### Successful reconciliation can leave a worker writable
**Location:** `platform-access/pg-sync.yaml` — `assert_role`, `grant_database`, the rotated-only proof
**Severity:** important
**ACCEPTED, with a stronger fix than proposed.** Verifying effective privileges every run for every
role across every database is expensive and still only *detects*. Instead the run now **repairs**:
non-SELECT privileges are revoked on all tables/sequences in every allowlisted database, `CREATE` is
revoked on each schema and on the database, role memberships other than `pg_monitor` are revoked, and
the catalog is then queried for any remaining non-SELECT grant (which fails the run). The live proof
now covers **kept** credentials too. The test grants `INSERT, UPDATE` on a table the one-table probe
would never sample, plus a column-level `INSERT`, and asserts both are gone afterwards.

### Removing a database neither revokes access nor guarantees retirement
**Location:** `platform-access/pg-sync.yaml` — the per-database loops
**Severity:** important
**ACCEPTED — and writing the test found a second, worse bug.** Cleanup now enumerates every
connectable database, not the allowlist. The first version of that fix ran the cleanup pass *after*
the grants and silently broke everything: **`DROP OWNED BY` also revokes privileges on SHARED objects
(pg_database)**, so cleaning up in `keycloak`/`postgres` stripped the database-level `CONNECT` just
granted in `airlock`/`profile`/`platform_managed`. Cleanup now runs BEFORE the grant pass, with the
reason recorded in the code and the ADR. The test covers grant → de-allowlist → retire.

### Kept credentials do not reconcile PostgreSQL's actual expiry
**Location:** `platform-access/pg-sync.yaml` — `rotation_reason`, `assert_role`
**Severity:** important
**ACCEPTED.** `rotation_reason` now also reads `pg_roles.rolvaliduntil` and rotates when Postgres has
no expiry for the role or disagrees with the published one by more than 5 minutes. Tested by
`ALTER ROLE … VALID UNTIL 'infinity'` and asserting the run rotates and brings the expiry back inside
the published window.

### The write proof commits the mutation it is supposed to detect
**Location:** `platform-access/pg-sync.yaml` — `conn.autocommit = True` in `prove_login`
**Severity:** important
**ACCEPTED.** The proof runs in explicit transactions that are always rolled back, with a `SAVEPOINT`
per probe, and the connection is rolled back in `finally`. A SQLSTATE other than 42501 now fails the
run rather than counting as "refused" — an RLS rejection must not masquerade as missing privilege.
Tested: after a forced rotation the row count of the probed table is unchanged and no probe table
exists.

### A transient field-read failure silently removes platform stanzas
**Location:** `ansible/roles/dev_worker/tasks/openbao.yml` — the probe's `|| { echo missing }`
**Severity:** important
**ACCEPTED.** A failed `cred get` is now re-qualified: if a follow-up `cred list` still succeeds the
field really is absent (rc 90 → no stanzas, retry next run); if it fails the vault went away (rc 91 →
the play fails). `failed_when` accepts only 0 and 90.

### Check mode always reports platform credentials absent
**Location:** `ansible/roles/dev_worker/tasks/openbao.yml` — `when: not ansible_check_mode`
**Severity:** important
**ACCEPTED.** Both probes are read-only, so they now carry `check_mode: false` and run in a check
pass; the rendered agent.hcl a `--check` run proposes therefore matches the host's real KV state.

### An inherited tenant option overrides the explicit tenant argument
**Location:** `ansible/roles/dev_worker/files/platform` — `PGOPTIONS="-c app.tenant_id=$tenant${PGOPTIONS:+ $PGOPTIONS}"`
**Severity:** important
**ACCEPTED.** The explicit `--tenant` now goes last. The helper test runs with
`PGOPTIONS="-c app.tenant_id=other"` in the environment and asserts the final string ends with the
flag's value.

### The health check derives a different role from an FQDN
**Location:** `ansible/roles/dev_worker/tasks/openbao.yml` — `want_role="dw${HOSTNAME##*-}_platform_ro"`
**Severity:** nit
**ACCEPTED.** Uses `hostname -s`, exactly as the helper does.

### Recovery still restarts agents before the second sync is restored
**Location:** `docs/runbooks/openbao-recovery.md`
**Severity:** important
**ACCEPTED.** The sync-owned sequence now names both k8s-auth roles, requires **both** syncs to print
their own success line before agents are restarted, drops the stale `12/12`, and states the thing the
row implied but did not say: a wipe does not remove the stanzas already in `agent.hcl`, so a worker
that had platform access will restart-loop until the pg-sync publishes — stop the agent or remove the
stanzas if that cannot be done promptly.

### The documented primary write test leaves the current transaction read-only
**Location:** `docs/runbooks/dev-worker-platform-access.md`
**Severity:** important
**ACCEPTED.** `psql -c 'a; b'` runs both in one implicit transaction, and
`default_transaction_read_only` only affects the *next* one, so the documented command proved the
setting rather than the privilege. Split into two `-c` requests, with the reason in a comment. (The
integration test was already correct — it pipes the statements on stdin, where each is its own
transaction — which is why the behaviour itself was never wrong.)

### The slot consistency gate silently drops missing enumerations
**Location:** `scripts/check-slot-enumerations.py`
**Severity:** important
**ACCEPTED.** Every source is mandatory now: a regex that stops matching exits with the file and the
pattern named, the three observer RoleBindings are required by namespace, and both pg-sync env blocks
must be present and agree. `scripts/tests/test-check-slot-enumerations.sh` (new, wired into CI) proves
it with ten fixtures — mismatch, removal, reformat, and a slot that is both live and retired.

### The credential-template contract is never executed by the new test
**Location:** `ansible/roles/dev_worker/tests/test-platform-helper.sh` — section `[E]`
**Severity:** important
**ACCEPTED in substance.** Section `[F]` now *executes* the render chain — the Jinja pass
(`inventory_hostname`, `{% raw %}` unwrapping) and then the consul-template pass (`{{ with secret }}`
over `{{ .Data.data.<field> }}`) against fixtures — and checks the output the way its consumers do:
the pgpass must be a single five-field libpq line with wildcard host/port/database and the published
user/password in the right slots; the kubeconfig must parse and land in `strive-ailab` with a token.
The template is also asserted to read no KV field the sync does not publish. A real `bao agent` render
is left to the rollout's health block (it runs the agent and then uses both files), since CI has no
vault.

<!-- codex-impl-review-status: finalized -->
