# Claude Fable 5.1 in dsh: a metered `anthropic/` route and a native `anthropic-messages` provider — and the Max-subscription design that was built, then abandoned

Decision record: `docs/decisions/0029-claude-fable-subscription-in-dsh.md`. Operator request:
*"Add support in dsh for claude fable model (chifor@gmail.com) subscription"*, with two follow-up
directions — *"use the mapping that exist in grafana"* (the account identity) and *"follow the
existing dsh codex accounts integration"* (the credential shape) — and *"cross validate the plan
and implementation with fable and codex"*.

**The subscription was not used.** Anthropic's Consumer Terms prohibit a Free/Pro/Max OAuth token
in any third-party product or service (terms updated 2026-02-20, enforced 2026-04-04; pay-as-you-go
API billing is the named alternative). Fable ships on the estate's existing metered Anthropic key.
ADR 0029 § Context carries the reasoning and the autopsy; this plan records what was built, what
was measured, and what the cross-validation found.

## What shipped

Three files, no new credential plane:

| File | Change |
|---|---|
| `kubernetes/apps/apps/ai/litellm.yaml` | `claude-fable-5-1` → `anthropic/claude-fable-5-1` on `os.environ/ANTHROPIC_API_KEY`, `rpm: 4`, real catalog pricing; `checksum/config` re-stamped |
| `kubernetes/apps/apps/dsh/settings.seed.yaml` | provider `anthropic-fable` (`api: anthropic-messages`, no-`/v1` baseURL, `forceAdaptiveThinking`, no `off` row) |
| `kubernetes/apps/apps/dsh/deployment.yaml` | `DSH_PROVIDER=anthropic-fable` reconcile invocation |

No publisher, no OpenBao path, no ExternalSecret, no callback module — the subscription design
needed all five.

## Verification performed

Against the pinned image, the live dsh pod and the live TSDB, on 2026-09-21:

* **Route present, zero cost to check:** unauthenticated `POST /v1/messages` → **401**;
  `POST /v1/definitely-not-a-route` → **404**. This is how to assert the endpoint — it is lazily
  registered and absent from `app.routes` (ADR 0029 finding 5), which misled this investigation
  once.
* **Config parses:** the embedded `config.yaml` loads; 26 `model_list` entries; the new entry
  resolves to `{model: anthropic/claude-fable-5-1, api_key: os.environ/ANTHROPIC_API_KEY, rpm: 4}`.
* **Both CI gates green:** `scripts/gen-litellm-consumers.py` reports "all 3 spans match" (the new
  route is correctly **not** generated into dsh's `openai-completions` block — it has no
  `api_base`, so it is not `dsh_visible`), and `scripts/check-inline-hashes.py` passes after
  `--write` re-stamped `checksum/config` `5c909d46b8c9 → e0df6fc2d5c2`.
* **dsh seed parses** and the provider resolves with `api: anthropic-messages`,
  `baseURL: http://litellm.ai.svc.cluster.local:4000`, `compat.forceAdaptiveThinking: true`,
  `claude-fable-5-1` at 1M/128K, and **no `off` reasoning row**.
* **Reconcile exercised against a PVC-shaped fixture** (the seed minus the new block, plus a
  UI-written `ui.theme` preference): inserts at indent 4, leaves the other three providers and the
  UI preference byte-intact, is idempotent on a second run ("already matches the seed"), and the
  documented rollback (`DSH_PROVIDER_REMOVE=1`) removes it cleanly.
* **`@anthropic-ai/sdk` 0.123.0 in the dsh pod posts `/v1/messages`** relative to `baseURL` — the
  evidence for the no-`/v1` rule.

### Still to verify, on the live route

These need a real call and therefore real money; none is a blocker for merge, all are cheap:

1. A one-turn Fable call through dsh returns content and is billed to the metered key.
2. A three-turn tool-using conversation replays thinking blocks without a 400 and without the
   block count shrinking — the `forceAdaptiveThinking` assertion (ADR 0029 finding 7).
3. dsh's image-offload path (which rewrites older turns) does not trip Fable's preserved-thinking
   history check. If it does, that is a dsh-side constraint to record, not a route defect.

## Cross-validation

Requested against Fable and Codex. Outcome, honestly:

* **Codex: unavailable.** Three dispatch attempts. Seats a, b and d are at 100 % of their weekly
  window and parked (`reviewbot_llm_usage_percent{persona="codex"}`); the reset is 2026-09-23
  09:06. Seat c has ~51 % headroom but the workstation holds no profile for it — its credential
  lives on reviewer-2, and copying it here would break the one-credential-per-seat isolation ADR
  0025 is built on. **Re-run this review after 2026-09-23 before merge.**
* **An independent model stood in for Codex** and killed the then-current design: the
  roll-the-gateway option resets the shared spend guard (`litellm.yaml:997`), and the
  "mint the setup-token from the seat's own HOME" provenance argument was unsound — `$HOME` does
  not bind the account, the browser session does, and `docs/runbooks/dev-workers.md:411-413`
  records that exact mis-binding happening here on a first attempt. It also found that a stray
  `~/.claude/oauth-token` would silently repoint reviewer seat `a` (`claude-seat.sh:16`) and drop
  the live login's tokens from reviewbot's redaction set (`reviewbot.py:1703`).
* **Fable found the blocker** — the Consumer Terms prohibition — which no amount of further
  engineering would have resolved, plus the thinking-block stripping (finding 7), the no-`/v1`
  baseURL, and two corrections to claims this plan had asserted as verified (see below).

### Claims this plan previously made that were wrong

Recorded because they were stated with confidence and are now contradicted:

* *"`drop_params: true` is load-bearing on this route; without it dsh's tool calls 400."* On the
  **native** `/v1/messages` path forced `tool_choice` is never downgraded, and `budget_tokens` /
  disabled-thinking are handled unconditionally rather than behind `drop_params`. `drop_params` is
  also already global (`litellm.yaml:840`). The route carries no per-route flag.
* *"An rpm cap bounds the weekly window."* `rpm` is a concurrency semaphore here, not a rate cap
  (`enable_pre_call_checks` is unset). ADR 0029 decision 5.
* *"Zero-cost pricing protects the shared budget."* Moot now the key is metered, and the
  accumulator it relied on appears not to fire for streaming or chat responses in 1.101.0 —
  recorded as a separate follow-up in ADR 0029 § Consequences.
* *"There is no `/v1/messages` on this image."* It is lazily registered. Probe, don't enumerate.

## Risks and non-goals

* **Cost is the live risk, and it is new.** Fable is $10/$50 per MTok and dsh resends conversation
  state every turn: ~$1.50 per 100K-in/10K-out turn, ~33 turns to $50. `rpm: 4` bounds concurrency
  only. Watch the bill before assuming the route is cheap to leave enabled.
* **Non-goal: making the subscription work.** The identity-injection that would do it is
  circumvention and risks the account that is also reviewer-1 seat `a`.
* **Non-goal: a LiteLLM upgrade.** 1.101.0 was the latest stable on 2026-09-21 and already serves
  the endpoint.
* **Non-goal: retiring the metered `claude-sonnet-*` routes.** They serve the Strive platform and
  are untouched.
