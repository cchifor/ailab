# ADR 0029 — Claude Fable 5.1 in dsh, on the chifor@gmail.com Max subscription

**Status:** ACCEPTED (2026-09-21), operator-directed: *"Add support in dsh for claude fable model
(chifor@gmail.com) subscription"*. Design and measurements in
`plans/2026-09-21-dsh-claude-fable-subscription-plan.md`.
**Relates to:** ADR 0026 (the ChatGPT subscription through LiteLLM — this is its Anthropic sibling
and reuses its vault path class, its SecretStore and its publisher), ADR 0025 (the reviewer-1
Claude seats — this spends seat `a`'s window), ADR 0020 (access-only projections), ADR 0021
(credential tiering — the litellm pod gets a rendered value, never a vault identity),
ADR 0022 (model registration single source).

## Context

dsh (`docs/runbooks/dsh.md`) reaches every model through LiteLLM and nothing else: its
NetworkPolicy allows the gateway explicitly and its own comment states the invariant — *"LiteLLM is
the ONLY model path ... no OpenAI, no Anthropic, no DeepSeek"* (`networkpolicy.yaml:63`). The
estate already serves Claude through that gateway, but only as **metered API** —
`claude-sonnet-4-6` and `claude-sonnet-5` on `os.environ/ANTHROPIC_API_KEY`
(`litellm.yaml:565-568`). The operator asked for **Fable on a subscription**, which is a different
credential class and the reason this ADR exists.

Nine findings fixed the shape of the change. All were measured on the live estate on 2026-09-21,
against the pinned image `ghcr.io/berriai/litellm@sha256:d295634e…` (1.101.0) and the live TSDB.

1. **LiteLLM already speaks Anthropic subscription OAuth — no shim, no code.**
   `litellm/llms/anthropic/common_utils.py:102` `optionally_handle_anthropic_oauth()`: an
   `api_key` beginning `ANTHROPIC_OAUTH_TOKEN_PREFIX` = **`sk-ant-oat`** causes LiteLLM to drop
   `x-api-key`, set `Authorization: Bearer <token>`, merge
   `anthropic-beta: oauth-2025-04-20` (`ANTHROPIC_OAUTH_BETA_HEADER`) and add
   `anthropic-dangerous-direct-browser-access: true`. The same switch is applied in the header
   builder at line ~787. A subscription token is therefore an ordinary `api_key:` value.
2. **`/v1/messages` EXISTS on the pinned image, but is LAZILY REGISTERED — enumerating
   `app.routes` says otherwise and is wrong.** The unified endpoint lives in
   `proxy/anthropic_endpoints/endpoints.py` ("Unified /v1/messages endpoint - (Anthropic Spec)")
   and is mounted on first matching request by `proxy/_lazy_features.py:196`
   (`path_prefixes=("/v1/messages", "/anthropic", "/api/event_logging")`). Probed from inside the
   pod: an unauthenticated `POST /v1/messages` returns **401** (route present, auth required)
   while `POST /v1/definitely-not-a-route` returns **404**. That probe costs nothing and needs no
   key; it is the assertion to use, not the route table.
3. **The `/anthropic` passthrough is NOT an equivalent path, and is the trap here.**
   `AnthropicModelInfo.get_auth_header` (~line 929) returns *only* `{"authorization": "Bearer …"}`
   for an OAuth key — it never adds the `oauth-2025-04-20` beta header that finding 1 supplies on
   the `model_list` path. The passthrough also bypasses `drop_params`, budgets, the spend guard
   and per-route logging. The native-looking option is the worse one.
4. **`drop_params: true` is load-bearing on a Fable route, not hygiene.** Fable 400s on forced
   `tool_choice` (`any`/`tool`), on `budget_tokens`, and on explicitly disabled thinking. The
   pinned image knows all three — it carries `DROP_FORCED_TOOL_CHOICE_WARNING` ("Downgrading
   forced tool_choice to 'auto' … this model rejects tool_choice type 'any'/'tool' with a 400
   because thinking is always on") and `DROP_DISABLED_THINKING_WARNING` — but only acts on them
   when the flag is set. Without it dsh's tool calls fail, which is the same class of defect that
   cost a debugging cycle on `openai-codex-realjaynesage` (ADR 0026).
5. **`claude-fable-5-1` is already in `litellm.model_cost`** (1M input, 128K output), so the route
   needs no custom model registration to be priced or validated.
6. **The spend guard is GLOBAL, and subscription traffic would corrupt it.**
   `litellm_settings.max_budget: 50` USD / `budget_duration: 30d` (`litellm.yaml:851-855`) is
   "Global across all models/keys". Subscription inference has **zero marginal cost** — it is
   already paid — but LiteLLM would price it from the cost map at Fable's $10/$50 per MTok and
   charge it to that shared ceiling. Left alone, a few heavy dsh sessions exhaust a budget that
   also gates the Strive platform's `claude-sonnet-5` and GPT-5.x routes: collateral damage
   entirely outside dsh.
7. **chifor@gmail.com is claude seat `a`, and it has headroom.**
   `reviewbot_llm_seat_info{persona="claude",seat="a"}` carries `email="chifor@gmail.com"`,
   `plan="default_claude_max_20x"` (seat `b` = `constantin.chifor@strive.us`, seat `c` =
   `realjaynesage@gmail.com`). At the time of writing `reviewbot_llm_usage_percent{persona="claude",
   seat="a"}` reads **session 52 %, weekly_all 8 %, weekly_fable 11 %**.
8. **Seat `a`'s credential is a browser login, so it rotates.** Only a browser login carries
   `user:profile`; a setup-token answers `/api/oauth/profile` with `403 oauth_scope_insufficient`
   (ADR 0025's same-day addendum). Seat `a` publishes an `email` label at all, so it has that
   scope. The contrast is visible in the same TSDB: all four AgentForge brokers report
   `af_broker_usage_probe_total{aud="anthropic/claude-max-N", outcome="scope_missing"}` — those
   are setup-tokens. A rotating credential is what makes a publisher necessary rather than a
   one-time seed.
9. **The broker tokens cannot be reused, because they cannot be identified.** Reusing
   `operator/broker/anthropic/claude-max-1/oauth` was considered. A setup-token cannot report its
   own account (finding 8), so there is no way to assert which of `claude-max-1…4` is
   chifor@gmail.com; picking wrong silently spends `constantin.chifor@strive.us`'s window. The
   seat path is the only one with a positive identity assertion.

## Decision

1. **Serve Fable through LiteLLM's `model_list` on `/v1/messages`, never the `/anthropic`
   passthrough.** dsh gets a provider with `api: anthropic-messages` pointed at the gateway, so
   the protocol is Anthropic-native end to end — which matters for Fable, whose thinking blocks
   must be replayed unchanged — while the request still passes through the router and therefore
   gets finding 1's OAuth headers, finding 4's `drop_params`, budgets and logging.
2. **The credential is reviewer-1 claude seat `a`, projected access-only.** A new publisher
   projection reads the seat's credential on reviewer-1 and PATCHes the access token into
   `af/litellm/anthropic`; an ExternalSecret on the **existing** `litellm-store` SecretStore
   (ADR 0026) renders it into the `ai` namespace as `ANTHROPIC_OAUTH_TOKEN`. The refresh token,
   the id token and the credential document never leave the reviewer host, and the litellm pod
   receives a value, not a vault identity.
3. **Identity is asserted against `/api/oauth/profile`, not against token claims.** This is the
   one real divergence from `dsh_codex_publisher`, and it is forced: a Codex token is a JWT, so
   `publish.py` reads `claims[PROFILE_CLAIM]['email']` and refuses on mismatch, whereas **Claude
   access tokens are opaque** and carry no readable claims. The Claude projection therefore calls
   `GET /api/oauth/profile` (the same call behind the Grafana email label, `claude-usage.py:13`)
   and refuses unless it returns `chifor@gmail.com`; expiry comes from the credential document's
   `expiresAt` rather than a JWT `exp`. The refusal-on-mismatch property of ADR 0020 is preserved;
   only its mechanism changes.
4. **The route declares zero cost.** `input_cost_per_token: 0` / `output_cost_per_token: 0`, so
   finding 6's global ceiling continues to measure real metered spend and is not exhausted by
   inference that costs nothing. This is accounting accuracy, not an exemption — the metered
   `claude-sonnet-*` routes keep their real prices and keep counting.
5. **The token is never placed in the dsh pod.** dsh authenticates to the gateway with
   `LITELLM_API_KEY`; the gateway authenticates to Anthropic. This is the ADR 0026 posture and it
   is load-bearing here for a specific reason: the dsh pod executes model-authored code and offers
   a `danger-full-access` preset, so a live subscription credential inside it is reachable by the
   agent.
6. **Staleness is alertable.** The projection exports the `dsh_codex_projection_*` metric family
   for its document, bringing it under the existing `CodexProjectionStale` rule
   (`reviewbot-rules.yaml:612`) and its stopped-publisher companion. A token that ages out while
   every component reports green is the failure that pattern exists to catch.

## Consequences

* **dsh and the claude reviewer persona share seat `a`'s weekly Fable window.** The failure mode
  is not an outage but a silent quality regression: reviewbot's ladder descends `fable → opus →
  sonnet` (ADR 0025 decision 3) and PR reviews get weaker with nothing paging. Finding 7 says
  there is headroom today (11 %); an rpm cap on the route and an alert on `weekly_fable` bound it,
  and they are the first things to revisit if reviews start arriving at a lower tier.
* The reviewer host stays the sole OAuth refresher for this account. Nothing in the cluster can
  renew the token; if the publisher, ESO or kubelet fails downstream, the route fails fast
  upstream with a 401 rather than silently serving stale traffic.
* `af/litellm/*` gains a second document beside `chatgpt`, under the same `af-app-litellm` policy
  and the same SecretStore. No new vault identity, no new egress exception, no new ESO store.
* **The ADR 0022 generator is left to ignore this route, and must stay that way.**
  `gen-litellm-consumers.py` writes dsh's `litellm` (`openai-completions`) provider block from
  `litellm.yaml` under `dsh_visible() = eligible and (private_base or dsh_only)`. This entry has
  no `api_base`, so it is correctly not generated there. Setting `model_info.dsh_only: true` on it
  — the key that exists to offer a paid route to dsh's picker — would list `claude-fable-5-1`
  inside the `openai-completions` provider *as well as* the `anthropic-messages` one this ADR
  adds: one model on two protocols, one of them wrong. That key must not be set here.
* The `/v1/messages` lazy-registration behaviour (finding 2) is now recorded. It has already
  produced one wrong conclusion in this estate's investigation of it, and it will produce another
  on the next image bump unless the 401-vs-404 probe is used instead of the route table.
* A LiteLLM upgrade was considered and rejected as unnecessary: 1.101.0 was the latest **stable**
  release on 2026-09-21 (1.102.x and 1.103.x are rc/dev only) and already serves the endpoint.
