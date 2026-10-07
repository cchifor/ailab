# Alignment round 2 — Fable and Codex — stateless-reviewers

Both reviewers read plan v3 (`e2411059`). Both agree the pending-review rule stays strict (no blind
delete); Codex adds that POSTs must be serialised per PR.

- **Fable:** NOT ALIGNED — no blocker, two important, six nits.
- **Codex:** NOT ALIGNED — three blockers reopened by the simplifications, four important.

| ID | Source | Finding | Disposition (plan v3.1) |
| --- | --- | --- | --- |
| Codex 1 | blocker | `pubfin` not terminal: a finished right could match the "mine" branch | Finishing is irreversible: consumed in-process before `pubfin`; "mine" requires no `pubsent`/`pubfin` and not consumed |
| Codex 2 | blocker | Wall-clock void timer (clock jump; owner paused after the self-check) | Possibly-sent rights: owner stopped first, then a 1 h wait on the operator tool's monotonic clock, then a marker re-check |
| Codex 3 | blocker | Cut-over exports only quarantined rows; a `posting` row's POST may be in flight | `posting` rows and ambiguous quarantines exported as possibly sent (`pubsent`) |
| Codex 4 / Fable 1 | important | Migrated rights: no `pubsent`, owner-gone rule lost | Export writes `pubsent1.<export time>`; owner = stopped legacy service |
| Codex 5 / Fable 2 | important | Pending-review cleanup across heads; POSTs not serialised per PR | **Publication rights per (kind, PR)**, a new right only when all are resolved: one POST in flight per kind per PR; the void deletes pending reviews of the single unresolved right |
| Codex 6 | important | Policy changes after a verdict was computed | Guard and verdict recomputed against fresh policy before sending; merge gate enforces the current guard |
| Codex 7 | important | Merge audit no longer durable | Stated as best effort; residual includes any crash before the re-read completes |
| Fable 3 | nit | No success outcome for a work claim | `a<N>.ok.<t>` |
| Fable 4 | nit | Head watcher vs a held right | Watcher stops once a right is owned; step 3 re-checks the head |
| Fable 5 | nit | Lost contract details | Exact-ref reads, marker never truncated, diff bound, per-identity budget `400/N` |
| Fable 6 | nit | Approval upgrade vs pending reviews | Approvals use a per-PR right (`purpose: approval`) |
| Fable 7 | nit | "mine" branch budget | Fixed `llm_timeout_s`; brief ambiguity seen by peers documented |
