# Plan review round 1 — Fable — stateless-reviewers

Reviewer: Claude Fable (independent, read-only), on plan commit `493ba541`. Findings verbatim in
substance; the dispositions are in the revised plan.

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| F1 | A `201` is not proof of ownership: Gitea's create-tag can write the git tag for one caller and the release row (201) for another | blocker | **Confirmed by spike 2** (25/25 staggered rounds: the 201 caller never wrote the git tag). Accepted: ownership = nonce in the git tag object's message, read back after every create |
| F2 | Fencing is not "a few hundred ms" before the POST (up to ~13 API calls at 60 s timeout); a duplicate can also flip a merge because `persona_verdicts` takes the last marker | important | Accepted: time check last, short fencing timeouts, taker grace, strictest-marker-wins |
| F3 | Crash or failed `.q` write after an ambiguous POST converts it into an automatic retry | important | Accepted: intent tag `.post` before the POST |
| F4 | Uncharged `.rel` loops can retry a head forever | important | Accepted: lease-expiry exits charged as deadline class; `.rel` capped |
| F5 | Rollback to `local` / old code re-reviews heads; new marker field breaks the old `MARKER_RE` | important | Accepted: separate coverage comment; quarantine migration both ways |
| F6 | Duplicate instance names defeat readback; a restarted instance waits out its own dead lease | important | Accepted: per-process nonce; self-release of own-name foreign-nonce leases |
| F7 | Prefix listing carries no messages; self-reported `issued_at` unverifiable | important | Confirmed by spike 2. Accepted: `tagger.date` (server time) as the lease start; timestamps in outcome names |
| F8 | `appr` has no lease; a crash holds the merge forever | important | Accepted: `appr` uses the claim shape with a lease |
| F9 | Sweep leadership contended by muted/slow instances; non-leaders have no candidates | important | Accepted: muted instances do not contend; every instance lists candidates; only merge/janitor are leader-only |
| F10 | Janitor deletes rewrite history (reopened PRs lose `.q`, caps, resets) and race claims | important | Accepted: janitor only for merged PRs and non-current heads without `.q`; deletes via git push |
| F11 | `coverage` change makes step 1 not a no-op | nit | Accepted |
| F12 | Cross-head exclusivity delays a new head by a whole lease | nit | Accepted: owner polls the head during the run and aborts within 60 s |
| F13 | `server_now` goes stale during local work | nit | Accepted: Date + monotonic elapsed; fail closed without Date |
| F14 | Persistent non-race create errors retried silently | nit | Accepted: counter + alert |
| F15 | Candidate polling cost unbounded | nit | Accepted: per-candidate `next_check` |
| F16 | Tagger check is costly and the tagger is user-settable | nit | Superseded by spike 2: the tagger is always `Gitea <gitea@fake.local>`; check dropped, rely on repo ACL + message fields |
| F17 | Phase 0 omissions (listing payload, 201-vs-git, residue after losers, tagger.date, Date on errors, protected tags, side effects, prefix termination, delete+create race, PAT scope, annotated type) | important | Accepted; several already answered by spike 2 |

## Spike 2 facts (2026-10-07, `cchifor/primes-lab`, flat names, cleaned up)

- 25 rounds × 20 callers with 0–80 ms jitter: always one `201`, 19 × `500`; in **25/25** rounds the
  git tag's message carried a `500` caller's nonce, not the `201` caller's.
- Such tags have no release row: `DELETE /tags/{name}` → 404; removal only by `git push :refs/tags/<name>`.
- `GET /git/refs/tags/<prefix>` → `{ref, url, object{type: "tag", sha}}`, no message.
- `GET /git/tags/{sha}` → message + tagger `{name: "Gitea", email: "gitea@fake.local", date: <server time>}`.
- `Date` header present on 404; `cf-cache-status: DYNAMIC`.
