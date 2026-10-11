# Plan review round 5 — PR #1127 reviewer bots, round 10 — stateless-reviewers

At head `de85f5ab`, reviewer-codex and reviewer-claude both approved with verdict `clean` and left
these items open. They are addressed before merge (owner decision, 2026-10-11).

| ID | Source | Finding | Sev. | Disposition |
| --- | --- | --- | --- | --- |
| Q1 | Codex, Claude (corroborating) | Spike 3 shows that a failed POST *leaves* a PENDING review. It does not show that a PENDING review means the request has *ended*. Gitea creates the pending review before it attaches the comments and submits, so after a client timeout the owner can see its own request still executing, and the delete would race the submit | important | Accepted. A PENDING review counts as evidence only together with a 5xx answered by the forge itself, meaning Gitea's JSON body and not a proxy's 502/503/504: then the handler has returned. After a timeout, a crash or a proxy 5xx, only landing or an attested forge restart resolves the right. The operator's `--force` no longer accepts a PENDING review alone. Phase 0 measures the in-flight PENDING state on Gitea (sections 5 and 9, corner cases 5, 5a and 9) |
| Q2 | Claude | The forge-restart attestation must re-run the landing check after the restart completed. A request that committed just before the restart would otherwise be voided, and a second review would be minted | important | Accepted. `--forge-restarted` re-reads the landing evidence after the restart: landed → `pubfin<G>`, not landed → `pubvoid<G>`. A PENDING review still present after that is a leftover of the killed request, and operator cleanup deletes it (section 9 step 1) |
| Q3 | Claude | A bot PENDING review with no held right marks the whole PR ambiguous for the kind | advisory | No change. This is the stated liveness residual: an operator resolves it, and safety is unaffected |

Claude suggested two reads of an unchanged PENDING review, separated by the client POST timeout plus
the forge request timeout. That suggestion was not adopted, because it is a timing assumption, and
the owner ruled out time-based release in a4. The evidence-only rule is kept, and a PENDING review
without a forge answer leaves the PR to the operator path.
