# Bounded Phase A observer support

This gate stack extends operator `a5121af4` on main `13dd3f9b`. It preserves the operator absolute hello/read deadlines and startup containment.

The supported source tuple remains Hub `5f90d03a6ed291e6c62dfde75938ba07c20d8902`
(schema versions exactly 1 through 93), Router
`4e4bb34e2f968f9450aabe9cf228d3e70c8bab71`, Pi
`2604c41b69c5d9c50a3324545a51f0bfa38435a0` and holder
`64dc50f47a4867a6d9a7afc2e7d5ae24068c59af`. Existing bundle, profiles,
artifact, holder and deployment checks still apply. These source constants select
reviewed readers; they do not authenticate a deployment or independently attest
the Pi artifact. Unknown configured Hub/Router/holder revisions are unsupported.
There is no 0.14.2 bridge, new live activation pin or execution-window grant here.

Implemented here: predecessor, C9 retained native evidence.
Remaining gates: C10, C11. C10/C11 remain unsupported until their own collector changes arrive.

## Predecessor projection

[`predecessor-projection.sql`](../../scripts/relay-phase-a/sql/predecessor-projection.sql)
reads only the three operations from the independently retained report, scoped
by tenant, session and generation. It projects terminal receipt fields and reads
`clock_timestamp()` in the same read-only transaction. The collector checks all
three identities, kinds and outcomes. Missing or extra rows refuse.
`SUPPORT_PREDECESSOR_SESSION` explicitly names the pinned A driver's
`STOPPED_SESSION`; this support does not generalize the accepted predecessor.

The operator must independently provision the Runner's projection pin before the
callback. The observer never returns or replaces that pin. Legacy untagged raw
SHA mode remains supported by A. Explicit `mode:"canonical-content-v1"` uses:

```text
SHA256(UTF8("relay-predecessor-command-projection/content-v1\n" + canonical(content)))
```

`content` omits **only top-level `observedUtc`**. Command order, schema, source,
query SHA, database identity, receipt fields and every scope field remain pinned.
Freshness remains less than 120 seconds old and at most five seconds in the
future. Every observation performs a fresh query; cached rows must never receive
a replacement timestamp. Evidence preserves the returned raw SHA as well as
content SHA and domain. The database label is an attributed namespace/pod/database
identity, not cryptographic database authentication.

## C9 retained native and Router readers

`dw.py native-checkpoint` takes already-bound state-root, Relay/native/profile,
workspace, holder and journal identities. Its CLI only accepts the configured
private state location. It reads the holder journal and descriptor-selected
history; it never scans arbitrary session files. Private files require UID1000,
mode0600, one link and bounded `O_NOFOLLOW` reads. Directories require mode0700;
before/after identities and journal bytes must agree. These are sampling guards,
not protection from a malicious process with the same UID.

The original history is bound by `native-state-retained`, the retained receipt,
home/workspace identities, profile digest, native UUID and history SHA. The reader
validates the v3 header, unique backwards parent links and timestamps, then walks
the final leaf's active ancestry. It selects the last
`relay-inference-session-v1` receipt there. Abandoned physical branches cannot
replace it. Duplicate/unmatched tool results, unresolved effects, unknown tools
or changed marker outcomes refuse. The raw transcript and tool arguments are
not returned; marker commands are represented by hashes.

The resumed home is joined through `holder-recovery.json`, `native-recovery.json`
and `resumed-session.jsonl`. The exact imported prefix is hashed independently of
the current file. Pi can append metadata on open, so the initial imported SHA and
current SHA are retained separately. Appended messages/tool work before C10 are
refused. Matching the two runtime-manifest references is a descriptor continuity
check; artifact qualification remains an external bundle prerequisite.

The imported prefix must equal the retained active history after the pinned
importer's specific transformations: discard saved model/thinking selections and
system messages, remove tool-result display details and compaction details/system
configuration, and rewrite parent links. All remaining conversation and tool
records must match. Appended metadata must extend that prefix without changing
the retained native coordination receipt. JSONL splits only on LF; Unicode line
separators in message text remain data. This reader intentionally supports only
`message`, coordination `custom`, `model_change`, `thinking_level_change` and
`compaction` records. Other Pi history kinds (`session_info`, `label`,
`branch_summary`, `custom_message`) remain unsupported until separately qualified.

`router.js branch` opens only the configured SQLite file with `readOnly:true`,
extensions disabled and `query_only`. It selects one exact row from `kv`:

```text
ns = inference-session-v1
key = SHA256(JSON.stringify(["default", "key:" + renewedKeyId, conversation, branch]))
```

It returns only epoch, operation, state, updatedAt and optional successor, plus
the exact queried JSON's SHA and query/store/read-time provenance. A record more
than five seconds ahead of the read clock refuses. No managed token, runtime Store construction,
namespace enumeration, secret-table query or write is used in this mode. Unknown
database types are unsupported. Pod UID/restart and store identity must match the
existing observer continuity record.

C9 stores the original join in `RunState.retained_branch`. Reader support for resumed histories is qualified here; C10 remains unsupported until its decision collector is integrated.

## Offline verification

The native-reader test module requires actual Linux UID1000 ownership. On a
different runner account or platform its unittest entry runs the whole module
in the same pinned Node/Python Docker image used by the owner proxy tests, with
UID1000, no network, a read-only checkout and private tmpfs. Docker absence fails;
no tests are skipped and the production ownership constant is never overridden.
The child suite runs directly at UID1000, with a 60-second process bound and owned
container cleanup. Production observer ownership and source gates are unchanged.

Run `python3 -m unittest -v scripts.tests.test_relay_phase_a_support scripts.tests.test_relay_phase_a_observer` in an isolated Linux UID1000 container with Node24. All service and authority fixtures are synthetic.

`--sql-fixture` emits the projection schema and rows for disposable PostgreSQL17. `--sql-readbacks DIRECTORY` validates saved actual query results for the implemented gates. No migrations, deployed database, JIT authority or live provider is qualified.

`observer-support.test.cjs` is an explicit integration entry. It requires
`PHASE_A_DRIVER` containing exact f1 driver bytes (SHA256
`a9ff8fc0f37c5a79d4e956acd718e512c320fba593f4731cea6df92da481ed98`) and
`OBSERVER_SQL_READBACKS` pointing to the fresh database outputs. Missing inputs
fail; they are not skipped. It uses the actual Unix client, Python collector and
`Runner.replacementPreflight`, stopping deliberately at the subsequent replacement
review. The test primes a synthetic renewed expiry but does not replace clocks,
validators or prototypes. It checks two actual database timestamps under one
independent content pin, legacy raw mode and changed provenance/content refusals.
It is not a complete A lifecycle, published artifact, JIT, provider or live-host
qualification. The independent bounded-refusal transport PR is not required for these collector contracts.

Run the two predecessor queries against the disposable PostgreSQL fixture
immediately before the Node integration entry, saving the first response as
`predecessor.json` and the second as `predecessor-fresh.json`. Execute both queries
with the same fixed SQL and scoped variables; the second timestamp must be later
and all other projected content identical. Start and finish the integration
within the collector's strict two-minute freshness window. If the window expires,
rerun the actual queries and use their new outputs. Never edit the timestamps in
saved readbacks. Also run the same fixed SQL with the same scoped variables except
for a different synthetic tenant UUID, and save that response as `foreign.json`;
its `commands` must be empty. This third read is a required tenant-isolation
negative, not an optional input. `--sql-readbacks` checks both normal responses
structurally and requires the foreign response's empty command list; the
actual collector invoked by the Node entry separately enforces current freshness.
