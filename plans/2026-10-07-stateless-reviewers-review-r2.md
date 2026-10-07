# Plan review round 2 — Fable and Codex — stateless-reviewers

Both reviewers read plan commit `12e6aa0c` with their round-1 dispositions. A third pass (Codex on a
reviewer-2 seat, against the round-1 draft `493ba541`, run as a fallback when the local dispatcher
failed) arrived late; its findings not already covered are folded in here as `X<n>`.

## Round-1 resolution

- **Fable F1–F17:** all RESOLVED.
- **Codex C1–C32:** 24 RESOLVED; NOT RESOLVED: C5, C11, C13, C17, C18, C19, C28, C30 (dispositions below).
- **C21 (retargeted base):** Codex: drop (scope). Fable: **strengthen** — a review-gate bypass for
  push-capable allowlisted authors (PR head=`side2` base=`side` reviewed clean on a small diff, then
  retargeted to `main` and merged with unreviewed commits). Opus withdraws the pushback; accepted.

## Findings and dispositions

| ID | Source | Finding | Sev. | Disposition (revised plan section) |
| --- | --- | --- | --- | --- |
| R1 | Fable new-1 = C5 | `pubvoid<G>` lets a stalled-but-alive `pub<G>` owner post after `pub<G+1>` posts | important/blocker | Accepted: `--requeue --force` requires proof the owner process is gone (its `/healthz` reports another boot/pid, or the instance is stopped); owner re-reads `pubvoid<G>` and the epoch immediately before the POST; at most one POST per held right (§4, §11) |
| R2 | Fable new-2 | A create that times out and lands later strands the right | important | Accepted: nonce fixed per (kind, head, G) per process; readback polled up to `post_margin_s`; a late-landing own right may be used once (§3) |
| R3 | Fable new-3 = C13 | `pubok` without a marker is terminal forever | important | Accepted: `pubok` is a hint; no marker for `pub_grace_s` → ambiguous; `--force` refuses only on a visible marker (§4) |
| R4 | C11 | Unlocked approval upgrades add authenticated markers without `pub` | important | Accepted: approval upgrades carry **no marker** (they are not verdicts); the review-marker invariant becomes literal (§4, §8) |
| R5 | C17 | Rollback import misses young `publishing` rights | important | Accepted: import runs after drain and treats every `pub` without a marker as ambiguous regardless of age (§12) |
| R6 | C18 | A stray `local` process can pass a per-sweep fence check and post after cut-over | blocker | Accepted: every instance (both modes) checks the mode fence and epoch immediately before each POST and merge (§12) |
| R7 | C19 | Cached policy lets a merge skip a newly required persona | blocker | Accepted: `policy.json` is re-read immediately before each merge (§8) |
| R8 | C28 | `moved`/`closed` releases uncapped | important | Accepted: total claims per head capped (`hw > 40` → exhausted) (§5) |
| R9 | C30 | No server-crash test around ref creation | important | Accepted in part: readback makes any half state "not owned" or "unreadable → fail closed"; Phase 0 adds a Gitea-restart-during-burst test **only in an approved maintenance window** (§15) |
| R10 | Codex new-1 | Restore with a live publisher → two surviving reviews | blocker | Accepted: restore procedure quiesces reviewers first; a restore bumps the coordination **epoch** (`rb1.epoch.<n>` tag); instances read the epoch at start and refuse to post or merge when it changed (§12, §14) |
| R11 | Codex new-2 | Successful work claims stay leased and delay the next head | important | Accepted: `a<N>.done.<t>` written after publication; cross-head waits re-checked every 60 s (§5, §6) |
| R12 | Fable C21 | Base retarget bypass | important | Accepted: merge gate counts a marker only if its `v1.cov` `base=<ref>` equals the PR's current base ref (legacy markers accepted) (§9) |
| X1 | reviewer-2 Codex | Head ABA: the current-PR `.diff` may be another head's diff | important | Accepted: fetch the diff by immutable commits (merge base … head) and bind the review to them (§7) |
| X2 | reviewer-2 Codex | Gitea reuses a pending review per (account, PR): a failed attempt's partial review can be consumed by another instance's POST | important | Accepted: before POST, list the account's PENDING reviews on the PR; any found → delete it if created by this attempt, else treat the head as ambiguous; Phase 0 verifies the behaviour (§4, §15) |
| X3 | reviewer-2 Codex | A delete 404 does not mean the tag is gone | important | Accepted: janitor confirms absence through the refs listing (§10) |
| X4 | reviewer-2 Codex | One failing PR stops the repository's sweep | important | Accepted: per-PR error isolation in discovery (§7) |
| X5 | reviewer-2 Codex | Merge reads verdicts, then merges: a verdict deleted in between is not seen | important | Accepted as a documented boundary: with publication only humans can change verdicts at a head; operator edits to bot reviews are out of scope (§8) |
| X6 | reviewer-2 Codex | Per-head claims do not give literal per-PR execution exclusivity | blocker | Accepted as a restated goal: posting exclusivity is absolute; execution exclusivity is best effort with a bounded (~60 s) overlap after a push (Goal, §6) |
