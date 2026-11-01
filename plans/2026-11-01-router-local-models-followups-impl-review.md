# Implementation review — router-local-models-followups

<!-- codex-impl-review-status: finalized -->

Plan: `plans/2026-11-01-router-local-models-followups-plan.md` (codex-reviewed, finalized; section C revised by the
owner). This file records how each part was reviewed and what changed because of it. The results are in
`docs/runbooks/llm-router.md` ("Local follow-ups release", "Results of the local follow-ups"), `dev-workers.md`
("Local models") and cloudlab runbook 13e.

## Reviews

| Change | Reviewer(s) | Outcome |
|---|---|---|
| llm-router #127 (A1-A3, plus the two real-CLI fixes) | the real Codex 0.166 CLI; reviewer-codex + reviewer-claude | The real CLI found two bugs, fixed test-first (reasoning effort not a planned control; usage Codex could not parse). Reviews: `CONTROL_PARAMS` missing `reasoning_effort` (fixed), usage shape keyed on the adaptation instead of the caller (fixed), PATCH projection (fixed), hedged test header (fixed), `*` target (no change, reasoned). Both approved; merged. |
| A standalone codex review of #127 | codex (review profile) | Died of `429 Too Many Requests` after 216k tokens (quota); the reviewbot's codex persona covered it. |
| cloudlab #48 (section D record) | codex (review profile) | Request changes, 7 findings: tool call vs. final answer, the full acceptance bar, the probe's scope, overstated causes, the Q6 footprint, the power hypothesis with a stop instruction, README wording. All fixed; merged. |
| ailab #1553 (roll, dev-worker role v1, runbooks) | reviewer-codex + reviewer-claude, 4 rounds | Codex's claim that profiles must be `[profiles.*]` tables was false for codex 0.166: reproduced on a worker (the table is refused, the file is loaded), and the evidence and a code comment converged it. Claude: a separate switch for the local models, removal tolerating missing files, user-owned directories, stale profiles cleaned up, the TUI note, images in the skill. All fixed and exercised with `ansible-playbook` against throwaway homes; that test caught my own `find contains` bug. Merged. |
| ailab #1554 (`local-agent`) | reviewer-codex + reviewer-claude, 2 rounds | The sandbox decision made fail-closed (`~/.codex` read-only as proof), private home deleted, runs pruned after 3 days, the skill's answer path, unchecked `mkdir`/`cp`, runbook structure. All fixed and re-tested: direct, nested sandboxed, nested unsandboxed. Merged. |
| llm-router#128 (Anthropic endpoint plan) | codex (plan-review profile) | 26 findings, all but one accepted as requirements in the issue. The one rejected (reasoning effort) was already resolved by #127. |

## Deviations from the plan, as rulings

- **Section C, revised by the owner after the first status:** agents on the local models from Codex and Claude Code,
  not `codex -p` profiles as the interface. Short pointers `qwen-27b` / `flash-next` were added; the keys carry those
  names only.
- **Codex's native `spawn_agent` does not accept the local models** (codex 0.166 checks its built-in catalog), and a
  plain nested `codex exec` cannot start inside a Codex sandbox. Ruling: `local-agent` for both CLIs. The native
  path is a follow-up: router-served catalog entries, or a `model_catalog_json`, which was tested to work but is
  rejected because it freezes the GPT metadata.
- **Reasoning effort became a planned control for every caller** (lenient: dropped and disclosed; strict: target
  skipped), not a Codex-only drop. It matches the documented lenient semantics. Cost if wrong: a client that
  relied on a hard error on a lenient local/Anthropic-API route now gets the effort dropped, disclosed in the
  response header.
- **MTP was rejected against the plan's own acceptance bar.** The self-contained draft head is kept on cloud3 to
  2026-12-01.
- **No router-mode re-benchmark of flash-next:** nothing about the model changed after D0.

## Known gaps (follow-ups)

- llm-router#128 (Anthropic endpoint).
- Native Codex `spawn_agent` on the local models.
- The Activity page / `explain_request` rendering of an adaptation-only `parameters_adjusted` event.
- The console's account editor does not show `assistantModels`.
- There is no chat tool for the key PATCH.
- flash-next's garbled copied values (cloudlab 13e).
- cloud3's two abrupt power losses during 4-GPU cold loads: the circuit and PSU are the owner's to inspect.
- 2026-12-01: delete the MTP draft head on cloud3 (`/models-local/qwen3.8-flash-next/MTP/`, 4.1 GB).
