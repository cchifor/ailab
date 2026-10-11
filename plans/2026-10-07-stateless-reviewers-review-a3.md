# Alignment round 3 — Fable and Codex — stateless-reviewers

Both read plan v3.1 (`18812989`). **No blockers from either.** Codex: Codex 1–4, 6, 7 resolved; Codex 5
(per-PR serialisation) incompletely specified. Fable: the per-PR structure is sound.

| ID | Source | Finding | Disposition (plan v3.2) |
| --- | --- | --- | --- |
| Codex a3-1 | important | Acquisition could leave one process with two unresolved rights | A late-landed own right is **finished, never used**; step 1 finishes mine before allocating |
| Codex a3-2 | important | Operator cleanup not serialised against the next right | Operator cleanup holds its own right (`purpose: operator`) while deleting pending reviews |
| Codex a3-3 / Fable 4 | important | A "mine" right (old head) blocks later heads / bypasses caps | The "mine → run" branch is removed |
| Codex a3-4 | important | Approval eligibility and recovery used verdict markers | Eligibility: clean marker and no APPROVED review by the bot; landing evidence: APPROVED review |
| Fable 1 | important | Pending review has no stateless stop condition | A bot PENDING review makes the PR **ambiguous**; the operator path covers the all-resolved case |
| Fable 2 | important | Right enumeration depended on listing completeness | Rights enumerated by exact-ref walk (no gaps by construction) |
| Fable 3 | important | Stuck right blocks the PR; no `pubfin` retry; residual unstated; alert per head | `pubfin` retried for the process's life; residual stated; `ReviewbotAmbiguousPR` per PR |
| Fable 5 | nit | Post-time collision discards a finished run | Wait for the peer's right within the budget |
| Fable 6 | nit | Export corner cases | One migrated right per PR, G+1, `pubfin` when the review already landed |
| Fable 7 | nit | Text inconsistencies | Outcome naming, open/not-draft in step 3, step 1 wording |
