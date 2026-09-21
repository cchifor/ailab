# Claude Fable 5.1 in dsh on the chifor@gmail.com Max subscription: a LiteLLM `anthropic/` OAuth route, a native `anthropic-messages` provider, reviewer-1 claude seat `a`

Decision record: `docs/decisions/0029-claude-fable-subscription-in-dsh.md`. Operator request:
*"Add support in dsh for claude fable model (chifor@gmail.com) subscription"*, with two follow-up
directions — *"use the mapping that exist in grafana"* (the account identity) and *"follow the
existing dsh codex accounts integration"* (the credential shape).

## Context

Everything below was measured on 2026-09-21 against the pinned image
`ghcr.io/berriai/litellm@sha256:d295634e…` (1.101.0) and the live TSDB. The nine findings are
stated once, in ADR 0029 § Context; this plan does not restate them and assumes they are read.

Three of them are load-bearing enough to name again, because the plan's ordering exists to respect
them:

* `/v1/messages` is **lazily registered** — `app.routes` does not list it, an unauthenticated POST
  returns 401 while a nonexistent path returns 404 (ADR 0029 finding 2).
* The `/anthropic` passthrough silently omits `anthropic-beta: oauth-2025-04-20` (finding 3); the
  `model_list` path supplies it.
* The `max_budget: 50` USD / `30d` ceiling in `litellm_settings` is **global across all models and
  keys** (finding 6), so a mispriced subscription route takes down the Strive platform's cloud
  routes and not just dsh's.

## Approach

Four stages. The ordering is the point: every stage proves the layer beneath it before the next
one depends on it, and dsh is pointed at the route only once the route is already serving Fable.

### Stage 0 — pre-flight probes (no PR, nothing merged)

Three assumptions remain unverified. Each is cheap to settle and each would change the design if
it turned out false, so none of them may be assumed by a later stage.

1. **Seat `a`'s credential shape on reviewer-1 (192.168.0.24).** `claude-seat.sh` reads
   `$HOME/.claude/oauth-token` if present and otherwise falls back to the ordinary
   `~/.claude/.credentials.json` login. ADR 0029 finding 8 infers a **browser login** from the
   presence of an `email` label, which requires `user:profile`; confirm that directly by
   inspecting which file exists in the seat's HOME and reading its `expiresAt`.
   **If it is instead a setup-token, the publisher is not needed at all** and the credential is a
   one-time seed — that is a materially smaller change, so settle it before writing stage 2.
2. **An authenticated `/v1/messages` call with an `sk-ant-oat…` key actually reaches Anthropic
   with both headers.** Findings 1 and 2 are read from source and from an unauthenticated probe;
   neither proves the end-to-end path. Verify against a scratch route before the real one is
   committed, and confirm the request carried `Authorization: Bearer` **and**
   `anthropic-beta: oauth-2025-04-20` — the second is the one the passthrough drops, so its
   presence is the whole assertion.
3. **The zero-cost override is honoured.** Confirm `input_cost_per_token: 0` /
   `output_cost_per_token: 0` on the route leaves `litellm_spend_metric` (and the global budget)
   untouched across a call. If LiteLLM prices it from the cost map regardless, fall back to a
   separate virtual key with its own budget — the option `litellm.yaml:853` already names.

### Stage 1 — the OpenBao side

`kubernetes/apps/infrastructure/security/openbao/` — mirrors `chatgpt-provision-job.yaml`, which
is the worked example for this exact shape.

* Create `af/litellm/anthropic` **once**, with every key the ExternalSecret template references
  seeded **empty**, plus a canary. ESO v2's template engine errors on an absent key, which is why
  the Job seeds rather than the publisher creating on first write.
* Extend the existing `af-app-litellm` policy to `read` this second path. The role
  `auth/kubernetes/role/af-app-litellm` is already bound to the `litellm-eso` ServiceAccount in
  `ai`; no new role, no new binding.
* Extend the publisher's AppRole policy with `patch` on `af/data/litellm/anthropic` + `read` on
  its metadata.

### Stage 2 — publisher, ESO, the LiteLLM route, monitoring

* **`ansible/roles/dsh_codex_publisher/files/publish.py`** gains a Claude projection kind. It is
  the same file rather than a new role because the projection loop, the 60 s AppRole login, the
  textfile export and the orphan-temp handling are all shared; only identity and expiry differ
  (ADR 0029 decision 3): `GET /api/oauth/profile` must return `chifor@gmail.com` or the projection
  refuses, and expiry is read from the credential document's `expiresAt`, not a JWT `exp`.
  Published fields follow the existing allowlist discipline — access token, account email, expiry
  — and never the refresh token, the id token or the document.
* **`ansible/host_vars/reviewer-1.yml`** gains the projection entry, in the shape reviewer-2
  already uses (`host_vars/reviewer-2.yml:55-66`): `auth_path`, `email`, `kv_path:
  litellm/anthropic`, `prefix`. Required, not optional — an absent credential must be a failure
  the alerts see.
* **`kubernetes/apps/apps/ai/litellm-anthropic-eso.yaml`** — one `ExternalSecret` on the existing
  `litellm-store` SecretStore, rendering `ANTHROPIC_OAUTH_TOKEN`. Unlike the ChatGPT sibling this
  needs no `auth.json` templating and no far-future `expires_at` guard: that guard exists because
  LiteLLM's `chatgpt/` provider starts an OAuth **device flow** on a stale file, and the
  `anthropic/` provider has no such path — a bad token is refused upstream as a 401.
* **`kubernetes/apps/apps/ai/litellm.yaml`** — one `model_list` entry named `claude-fable-5-1`,
  beside the metered Claude routes (`:565-568`): `model: anthropic/claude-fable-5-1`, `api_key:
  os.environ/ANTHROPIC_OAUTH_TOKEN`, `drop_params: true`, zero cost per ADR 0029 decision 4, and
  an rpm cap. Plus the env var from the new Secret, beside the existing
  `ANTHROPIC_API_KEY`/`litellm-cloud-keys` wiring (`:1031-1033`).
* **The ADR 0022 generator must be left to ignore this route — and one key would break that.**
  `scripts/gen-litellm-consumers.py` writes dsh's **`litellm`** provider block (the
  `openai-completions` one) from `litellm.yaml`, by `dsh_visible() = eligible and (private_base or
  dsh_only)`. This entry has **no `api_base`** — it egresses to api.anthropic.com — so
  `private_base` is false and the route is correctly not generated into that block. **Do not set
  `model_info.dsh_only: true` on it.** That key exists to offer a paid route to dsh's picker, and
  here it would list `claude-fable-5-1` inside the `openai-completions` provider *in addition* to
  the `anthropic-messages` provider this plan adds: the same model on two protocols, one of them
  wrong. `model_info.hidden` is not needed either, and the two are mutually exclusive anyway.
  Assert the outcome rather than trusting it — `python3 scripts/gen-litellm-consumers.py` must
  exit 0 (no drift) with the new route present and `settings.seed.yaml`'s generated span
  unchanged.
* **`kubernetes/apps/infrastructure/monitoring/reviewbot-rules.yaml`** — confirm the new document
  is covered by `CodexProjectionStale` (`:612`) and its stopped-publisher companion. Both join on
  `document`, so a new projection is covered without a rule change; assert that in the fixture
  rather than assuming it.

### Stage 3 — dsh, and the runbook

Only after stage 2's route answers with Fable content.

* **`kubernetes/apps/apps/dsh/settings.seed.yaml`** — a new provider block `anthropic-fable`
  (displayName "Claude Fable (chifor)"): `api: anthropic-messages`, `baseURL` at the gateway,
  `apiKeyEnv: LITELLM_API_KEY`, and one model
  `claude-fable-5-1` with `contextWindow: 1000000`, `maxTokens: 128000`, `input: [text, image]`
  and a `reasoningEfforts` map. A hand-entered model is text-only until it says otherwise, and a
  non-catalog route inherits nothing, so every field is spelled out — the same discipline the
  `openai-codex-realjaynesage` block documents.
* **`kubernetes/apps/apps/dsh/deployment.yaml`** — a `DSH_PROVIDER=anthropic-fable node
  /seed/reconcile-provider.js` line beside the existing three (`:169-178`). **This is not
  optional and the seed alone does nothing:** `settings.yaml` is seeded only when absent and has
  existed on this PVC since 2026-09-07, so a provider added to the seed never reaches the live
  volume. That is exactly how #613's AGENTS.md shipped inert.
* **`docs/runbooks/dsh.md`** — a section for the Claude subscription route alongside the Codex
  ones, and the rollback: `DSH_PROVIDER=anthropic-fable DSH_PROVIDER_REMOVE=1` for one boot, *then* delete
  the line. Dropping the seed block alone leaves the PVC copy in place.

## Critical files

| File | Change |
|---|---|
| `kubernetes/apps/infrastructure/security/openbao/anthropic-provision-job.yaml` | create `af/litellm/anthropic`, extend two policies |
| `ansible/roles/dsh_codex_publisher/files/publish.py` | Claude projection kind: profile-based identity, `expiresAt` expiry |
| `ansible/host_vars/reviewer-1.yml` | the projection entry (required) |
| `kubernetes/apps/apps/ai/litellm-anthropic-eso.yaml` | new — ExternalSecret on the existing store |
| `kubernetes/apps/apps/ai/litellm.yaml` | the route + the env var |
| `kubernetes/apps/apps/dsh/settings.seed.yaml` | the `anthropic-messages` provider block |
| `kubernetes/apps/apps/dsh/deployment.yaml` | the reconcile invocation |
| `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.yaml` | fixture coverage for the new document |
| `docs/runbooks/dsh.md` | the route, and its rollback |

## Verification

* **Route present, no key, no spend:** `POST /v1/messages` → 401; a nonexistent path → 404. Use
  this and not the route table (ADR 0029 finding 2).
* **Headers:** a call through the route carries `Authorization: Bearer` *and* `anthropic-beta:
  oauth-2025-04-20`. The second is the assertion; the passthrough would omit it.
* **Fable constraints:** a tool-using turn succeeds. Forced `tool_choice`, `budget_tokens` and
  disabled thinking are each rejected by the model and each dropped by `drop_params`; a 400
  mentioning any of them means the flag is not in effect.
* **Budget integrity:** the global `max_budget` counter is unchanged across a Fable call, and the
  metered `claude-sonnet-5` route still accrues normally.
* **Projection:** `dsh_codex_projection_ok{document="litellm/anthropic"}` is 1,
  `…_token_expires_at_seconds` is in the future, and a deliberately wrong configured email makes
  the projection refuse rather than publish.
* **dsh:** the provider appears in the composer after a pod restart, and `settings.yaml` on the
  PVC carries the block — the reconcile ran, not merely the seed.
* **Unit tests:** extend `scripts/tests/test_dsh_codex_publisher.py` for the Claude projection —
  identity mismatch refuses, an expired credential refuses, the refresh token is never published.

## Risks and non-goals

* **Shared weekly window (accepted, bounded).** dsh spends seat `a`'s Fable quota alongside the
  claude reviewer persona; at 11 % there is headroom. The failure is silent — reviewbot descends
  `fable → opus → sonnet` and reviews weaken with nothing paging. The rpm cap and a
  `weekly_fable` alert bound it; revisit both if reviews start arriving at a lower tier.
* **A second consumer of one OAuth family.** The reviewer host remains the sole refresher. If it
  stops, the route 401s upstream — fail-fast, not silent staleness — and `CodexProjectionStale`
  fires first.
* **Non-goal: a LiteLLM upgrade.** 1.101.0 was the latest stable on 2026-09-21 and already serves
  the endpoint; the rc/dev builds bring a whole-estate blast radius this feature does not need.
* **Non-goal: retiring the metered `claude-sonnet-*` routes.** They serve the Strive platform on a
  different credential and are untouched.
* **Non-goal: a second Fable seat.** If seat `a`'s window proves too tight, the answer is quota or
  a second subscription, not code — the conclusion ADR 0024 and ADR 0025 both reached.
