# Alignment round 4 (final) — Fable and Codex — stateless-reviewers

Both read plan v3.2 (`943873e3`).

| ID | Source | Finding | Disposition (plan v3.3, final) |
| --- | --- | --- | --- |
| a4-1 | Fable (important), Codex (blocker) | "Mine" conflated a right in use (incl. after a POST timeout, which may still land) with a late-landed one; a walk could finish the process's own in-flight right | In-process **held** set: a right is held from ownership until finished; only my-nonce rights that are **not held** are late-landed and finished on sight; a timed-out POST keeps its right held and unresolved |
| a4-2 | Codex (important) | Voiding a possibly-sent right after a 1 h wait contradicts "absolute" posting exclusivity | **Escalated to the owner, who chose "evidence only"**: a possibly-sent right is resolved only on landing evidence, a bot PENDING review (the request ended), or an attested forge restart after the send — never by elapsed time. The owner checks the first two itself; otherwise a human resolves it (stated liveness residual) |
| Fable nits | | 4xx charged as `fail`; re-read `pubvoid`/`pubfin` before `pubsent`; cache resolved rights; DISMISSED counts as approval evidence; one forge account per kind; owner self-resolves on evidence; `rel.moved` named; section reference | All applied |
| Codex nits | | PENDING-review ambiguity precedes "head done"; obsolete section reference | Applied (ordered state table) |

**Verdicts:** Fable: would sign off with a4-1 applied. Codex: NOT ALIGNED on a4-1 and a4-2, both now
resolved as above (a4-2 by the owner's decision). The round cap is reached; the plan is finalized at
v3.3. The v3.3 text itself is reviewed again in the implementation review (Phase B).
