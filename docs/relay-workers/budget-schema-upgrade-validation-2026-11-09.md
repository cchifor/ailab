# Independent budget schema deployment validation — 2026-11-09

The ailab initializer previously applied only `001-authority.sql`; the current
Relay runtime also requires `002-preflight.sql`, `003-scoped-requests.sql` and
`004-preflight-errors.sql`. This change exports all four from one full reviewed
source commit, independently pins the exact manifest, initializes new databases
at version 4, and supplies a separate offline version-1 upgrade. Neither Job is
in a Flux inventory, and both remain suspended with blank required digest pins.

## Local evidence

The fixture bundle was exported from Relay integration commit
`972a61c7cd68c5d4abd74ac174f94ab68d4f3df6`. Its public manifest SHA256 is
`671c4f5b792d450a36919d56705e4a7cb961437a633e60907dfd7f5bc51c1d85`.
This integration commit is **test input, not an approved production deployment
pin**. Production requires the final reviewer-merged source and a new reviewed
bundle receipt.

- Five bundle/Job boundary tests passed: immutable inventory and checksum closure,
  non-commit refusal, no overwrite, missing-source refusal and suspended standalone
  Jobs with required independent pins.
- Full ailab Python script suite: 1,890 tests, four skips, no failures (164 seconds).
- Pinned Kustomize rendered both optional directories; strict pinned Kubeconform
  accepted all 14 objects across the base, script ConfigMap and two standalone Jobs,
  with zero skipped schemas.
- The disposable pinned PostgreSQL 17.11 Docker test passed fresh initialization,
  altered/missing/extra manifest refusal before storage changes, legacy/empty/
  wrong-major/recovery refusal, projected-key TLS, durability settings and narrow
  runtime roles. New initialization contains no authority, qualifications or scopes.
- The retained fixture upgrade preserved a debit, its original deadline, outbox
  operation and receipt, with authority still withdrawn. Direct execution of
  `prepare_token_only` is denied; ordinary token-only requests intentionally remain
  supported through the new mediating `prepare` function.
- Incorrect original-schema pins refuse before any attempt. Active authority
  refuses SQL preflight repeatedly without consuming an attempt. A simulated
  migration failure after 002 commits preserves marker 1 and the durable attempt
  directory; replay refuses. Version 4 is recorded only after all migrations and
  a successful clean shutdown. All disposable containers/volumes were removed.

## Independent Opus review

Two source-snapshot rounds ran through llm-router's `pool-claude-api` route.
The first requested clearer ordinary-budget semantics and a retryable preflight
refusal. The implementation/documentation changed accordingly; explicit successful
shutdown and a partial-migration failure test were also added. Round two returned
**ALIGNED**, request `d9c4096a-fe18-4236-b466-b9c3ae05ecce`.

The review reports the configured route, not an independently verified underlying
model revision. It does not verify execution of local tests or live infrastructure.
The later wording clarifies that PostgreSQL still writes checkpoints during SQL
preflight and that stale PID files require the existing recovery procedure.

## Remaining operator evidence

No cluster write, authority enablement, live database upgrade or worker
qualification was performed. The operator must establish actual PVC/PV identity,
original version-1 SQL evidence and catalog equivalence when upgrading, clean
shutdown, kubelet/CSI ownership and PGDATA mode, backup exclusions and independently
retained storage durability. The Docker test's legacy checksum comes from its own
synthetic fixture; it cannot prove an existing database's provenance. A manifest
or version marker cannot prove that restored ledger history is current.

Follow [the activation and upgrade runbook](../runbooks/relay-worker-budget-authority.md).

## Repository review follow-up

The independent repository reviewer noted that transaction atomicity belongs to
the pinned external SQL, not `psql -f`. The runbook now explicitly states that
contract and the possible partly applied file if future SQL changes it. Adding
`--single-transaction` around files containing their own COMMIT would not enforce
the claimed boundary. Every source update must review its actual transactions.

The retained upgrade Job is now operator-attended with no hard active deadline,
so a long ALTER is not automatically killed at ten minutes. Init of an empty
database retains its deadline. The runbook pins the retained version-1 wrapper
for the documented recovery path and keeps consumers/authority disabled. These
changes do not establish live recovery or remove the interrupted-attempt gate.

A second repository review also made the shutdown wait explicit (3600 seconds
in both Jobs) and added equality checks for the server, initializer and upgrade
image pins in both the Python boundary suite and real Docker harness. The five
focused checks and complete disposable PostgreSQL harness passed again. This
wait does not extend Kubernetes termination grace or establish storage durability.
