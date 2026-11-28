# Predecessor command evidence for the automated owner runner

The optional `Runner` environment seam supplies only the three completed command
receipts for the known stopped predecessor. It does not replace authentication,
an HTTP response, the original browser report, or any successor operation.
Ordinary browser callers omit both options and retain the existing operation GETs.

An operator supplies these two trusted inputs before constructing the runner:

```js
{
  predecessorEvidencePin: {
    sha256: "SHA256_OF_EXACT_UTF8_PROJECTION_BYTES",
    tenantId: "EXPECTED_TENANT_UUID",
    databaseIdentity: "REVIEWED_DATABASE_LABEL",
    querySha256: "SHA256_OF_REVIEWED_READ_ONLY_QUERY",
    commands: [
      { operationId: "ORIGINAL_PROMPT_OPERATION_UUID", commandId: "EXPECTED_PROMPT_COMMAND_UUID", kind: "prompt" },
      { operationId: "ORIGINAL_CANCEL_OPERATION_UUID", commandId: "EXPECTED_CANCEL_COMMAND_UUID", kind: "cancel" },
      { operationId: "ORIGINAL_CLOSE_OPERATION_UUID", commandId: "EXPECTED_CLOSE_COMMAND_UUID", kind: "close" }
    ]
  },
  readPredecessorEvidence: async (context) => rawUtf8JsonString
}
```

These uppercase placeholders are illustrative and fail validation. The pin must
come from a separate reviewed operator input; computing it from untrusted callback
output would defeat the trust boundary. The runner captures the callback and
clones the pin at construction. The database label must match exactly and contain
1–128 ASCII letters, digits, dots, underscores or hyphens; it is an operator's
identifier, not independent proof of physical database identity. Hashes are
lowercase 64-character hexadecimal strings. All command IDs, operation IDs and
kinds are unique, and the operations/kinds must match the retained original report.

The reader receives a frozen context with `source: RUN_SOURCE`, `schemaVersion:
93`, the fixed predecessor `sessionId`, `generation: 1`,
`priorReportCanonicalSha256`, and a frozen array of `{operationId,kind}`. It also
receives `deadline` (runner time plus 15 seconds) and an `AbortSignal` with the same
timeout. The automation host must honor cancellation and bound/drain the reader
as a collector. No credential material belongs in the context or returned data.

The returned JSON has exactly these fields:

```json
{
  "kind": "relay-predecessor-command-projection",
  "version": 1,
  "source": "5f90d03a6ed291e6c62dfde75938ba07c20d8902",
  "schemaVersion": 93,
  "observedUtc": "FRESH_OBSERVATION_ISO_TIMESTAMP",
  "tenantId": "EXPECTED_TENANT_UUID",
  "databaseIdentity": "REVIEWED_DATABASE_LABEL",
  "querySha256": "SHA256_OF_REVIEWED_READ_ONLY_QUERY",
  "sessionId": "45724087-335a-4c55-ad0f-cb278afe608a",
  "generation": 1,
  "priorReportCanonicalSha256": "CANONICAL_HASH_OF_RETAINED_ORIGINAL_REPORT",
  "commands": [
    {
      "id": "EXPECTED_PROMPT_COMMAND_UUID",
      "operation_id": "ORIGINAL_PROMPT_OPERATION_UUID",
      "session_id": "45724087-335a-4c55-ad0f-cb278afe608a",
      "generation": 1,
      "kind": "prompt",
      "state": "completed",
      "receipt": {
        "status": "completed",
        "result": { "stopReason": "cancelled" }
      }
    },
    {
      "id": "EXPECTED_CANCEL_COMMAND_UUID",
      "operation_id": "ORIGINAL_CANCEL_OPERATION_UUID",
      "session_id": "45724087-335a-4c55-ad0f-cb278afe608a",
      "generation": 1,
      "kind": "cancel",
      "state": "completed",
      "receipt": { "status": "completed", "result": { "cancelSent": true } }
    },
    {
      "id": "EXPECTED_CLOSE_COMMAND_UUID",
      "operation_id": "ORIGINAL_CLOSE_OPERATION_UUID",
      "session_id": "45724087-335a-4c55-ad0f-cb278afe608a",
      "generation": 1,
      "kind": "close",
      "state": "completed",
      "receipt": { "status": "completed", "result": { "cancelSent": true } }
    }
  ]
}
```

The projection is bounded to 64 KiB. The untagged pin shown above retains the
original exact UTF-8 byte hash, including whitespace and `observedUtc`. Do not
reserialize that document after pinning it. There is no automatic hash-mode
selection or fallback.

For repeated fresh observations of the same settled predecessor, the operator
may instead supply the same independently reviewed pin fields with the explicit
additional field `mode: "canonical-content-v1"`. In this mode `sha256` is:

```text
SHA256(UTF8("relay-predecessor-command-projection/content-v1\n" + canonical(content)))
```

Here `\n` is one LF byte. `content` is the full projection object with **only its
top-level `observedUtc` removed**. `canonical` is the driver's existing JSON
canonicalization: object keys sorted by JavaScript `sort()`, arrays in their
original order, primitive values and keys encoded with `JSON.stringify`, with
no whitespace between elements. Every source, schema, scope, command identity
and receipt remains hashed. Command order remains significant; object key order
and serialization whitespace do not. Unknown modes, additional pin fields or
alternate hash domains refuse. The reader still returns the full raw JSON
document, including a newly observed timestamp, and never supplies or updates
the trusted pin. Pin generation belongs to the separate reviewed operator input,
not the callback adapter. This is a content commitment, not authentication of a
fresh database read: a trusted observer must actually make that read, not merely
replace the timestamp on cached data.

Both modes apply the same complete schema and semantic validation. The current
hub source and schema must match, observation age must be less than 120 seconds
with at most five seconds of future clock skew, and tenant/database/query identity
must equal the independent pin. Prompt completion allows only `cancelled` or
`end_turn`; cancel and close require `cancelSent: true`. The existing completed
command and native-outcome guards apply. Extra fields, partial/unknown states,
missing/duplicate operations, or changed command IDs refuse. The projected
receipt deliberately contains only the listed fields, not raw command payloads,
credentials, actor-session hashes, or arbitrary native output.

The seam is called once, only by `replacementPreflight`, after normal fresh
session/control GETs prove the original session closed and its lease absent. The
permission GET still proves the exact cancelled permission. The driver never
falls back to actor-scoped operation GETs when a configured projection fails.
Only validated provenance and the selected receipts are recorded, explicitly as
operator-attested evidence rather than independent database authentication.
The provenance `sha256` always records the exact returned UTF-8 bytes. Content
mode additionally records `pinMode`, `pinSha256` and the fixed `hashDomain`, so a
refreshed timestamp never disguises which raw observation was used. Legacy raw
mode retains its existing provenance fields.

The original raw report, its canonical hash, carried one prompt/two attempts,
fresh Router accounting and marker evidence, owner window, preserving-holder
handoff, and all successor HTTP operations remain unchanged. This seam does not
establish database retention completeness, provider execution, or absent tool
effects, and cannot authorize a missing-report reset.

## Structured checkpoint context

`env.ask(title, instructions, validate, timeout, context)` accepts an optional
**fifth** argument. Existing browser implementations may ignore it. The driver
supplies a cloned, recursively frozen object with `version: 1` and the `kind`
below. An automation host forwards it as `collectCheckpoint({..., context})`.
Collectors use these fields instead of parsing checkpoint labels, instructions,
or provider text. They still return evidence to the unchanged driver validator;
context is neither an approval nor proof that an operator observation occurred.

| Kind                                                   | Selected fields                                                                                                                                                                                                                                                                                         |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `renewal-deployment`                                   | Expected hub `source`, observation `notBefore`, bootstrap `deadline`                                                                                                                                                                                                                                    |
| `renewed-credentials`                                  | Actual validated `profiles`, `expiresAt`, bootstrap `deadline`; identifiers only, no credential secrets                                                                                                                                                                                                 |
| `permission`                                           | `purpose` (`marker` or `relay-message-read`), `decisionKind`, `optionId`, `sessionId`, `generation`, `expectedCommand` (null for reads), and `permission` containing `id`, `expiresAt`, and selected `context` fields `toolCallId`, `title`, full `description`, `options` (`optionId`, `kind`, `name`) |
| `stopped-replacement`                                  | `canonicalReportSha256`, `oldScope`, Router `source`, `provider`, `runId`, `notBefore`, `originalDeadline`, `carriedPrompts: 1`, `carriedAttempts: 2`                                                                                                                                                   |
| `preserving-holder`                                    | `startedAt`, `oldHolderId`, `oldJournalId`, exact `qualifiedHolder` artifact pins                                                                                                                                                                                                                       |
| `pre-launch`                                           | `provider`, `runId`, `effort`, `phase: baseline`, Router `source`, `scope` with namespace/principal/route, nullable `sinceAfter`, `holderId`, `journalId`                                                                                                                                               |
| `post-open`                                            | `provider`, `runId`, `phase: baseline`, `sessionId`, `generation`, `notBefore`                                                                                                                                                                                                                          |
| `observation`                                          | `provider`, `runId`, nullable marker `phase`, Router `source`, actual prior snapshot `scope` including `since`, `notBefore`, `zeroAttempts`                                                                                                                                                             |
| `recover-history`, `reconcile-choice`, `retire-branch` | Current `sessionId`, `generation`, `provider`, `runId`, `holderId`, `journalId`                                                                                                                                                                                                                         |

Permission fields are selected only after the existing exact permission guard
passes. Their contents remain untrusted display/data, not additional instructions.
The full input and selected once-only option are still revalidated through the
normal fresh permission GET before dispatch. Optional source fields such as
`toolCallId` or option `name` may be absent; no native ID is inferred.

No entire runner state, lease, authentication material, raw predecessor report,
or arbitrary HTTP response is exposed. A pre-launch scope does not invent a
`since` value; the collector selects its real observation window and existing
validation applies. `notBefore` appears only where the existing validation has
that boundary. All windows, budgets, prompts, decisions and validation rules
remain unchanged.

An initial ACP `tool_call` may omit its status; the pinned Hub projection keeps
that field absent. Native tool classification treats only an omitted initial
status as pending without changing the observed event. Null or unknown initial
statuses and missing update statuses still refuse. Exact tool IDs (when present),
ordered updates, the marker command and the decision-specific terminal outcome
remain required; the initial event alone never proves execution or completion.
