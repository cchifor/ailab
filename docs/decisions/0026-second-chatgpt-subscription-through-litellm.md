# ADR 0026 — A second ChatGPT subscription (realjaynesage) for GPT-6 Astra, served through LiteLLM

**Status:** ACCEPTED (2026-09-19), operator-directed: *"add a second subscription ... a new openai
subscription in litellm and a new OpenAI Codex (realjaynesage)"*. Design and measurements in
`plans/2026-09-19-second-codex-subscription-plan.md`. Implementation lands in three PRs, in order:
`kubernetes/apps/infrastructure/security/openbao/chatgpt-provision-job.yaml` (the vault side);
`ansible/roles/dsh_codex_publisher/`, `ansible/host_vars/reviewer-2.yml`,
`kubernetes/apps/apps/ai/litellm-chatgpt-eso.yaml`, `kubernetes/apps/apps/ai/litellm.yaml`,
`kubernetes/apps/apps/dsh/{settings.seed.yaml,deployment.yaml}`,
`kubernetes/apps/infrastructure/monitoring/reviewbot-rules.yaml` (the route, staged seat and
signals); then the activation change after the operator's login.
**Relates to:** ADR 0024 (the reviewer-2 Codex seats — this adds seat d), ADR 0020 (access-only
projections — the shape every credential here takes), ADR 0021 (credential tiering — the litellm
pod gets a rendered file, never a vault identity).
**Activated 2026-09-19 (PR 3):** after the operator's device-auth login as `codexrun4` (account
`841dae14…`, plan `prolite`, 0 % of its weekly window), the publisher's first projection landed in
`af/litellm/chatgpt` and the route served a 200; seat d moved into `pr_reviewer_llm_seats`, its
projection is required, all four seats are served, and the staged-seat window described below is
closed.

## Context

dsh's GPT-6 Astra route is its native `openai-codex` provider (`kubernetes/apps/apps/dsh/settings.seed.yaml`),
whose credential is reviewer-2 **seat b** (`codexrun2`, `realjaysage@gmail.com`, ChatGPT plan
`prolite`), projected access-only by `ansible/roles/dsh_codex_publisher` into
`af/dsh/credentials.DSH_CODEX_*` and mounted through ESO at `/dsh-credentials`
(`docs/runbooks/dsh.md` § Codex subscriptions). On 2026-09-19 that seat's weekly window reads
**100 %** (`reviewbot_llm_usage_percent{persona="codex",seat="b"}`), so dsh had no usable Astra
route. The operator asked for a second subscription, `realjaynesage@gmail.com` — on that day a
Claude seat on reviewer-1 only, with no Codex login anywhere in the estate — as a new OpenAI
subscription in LiteLLM and a new "OpenAI Codex (realjaynesage)" provider in dsh, like the
realjaysage one.

Six findings fixed the shape of the change; all were measured on the live estate on 2026-09-19.

1. **dsh cannot host a second native Codex provider.** In `@deepseek-ai/dsh-llm-pi-ai`
   0.1.5-alpha.2 (and 0.1.5-rc.2 / 0.1.6-alpha.2 on npm — checked), the Codex subscription
   protocol (`openai-codex-responses`) is reachable only by reusing the *catalog* route id
   `openai-codex`; a hand-declared route must name `api` ∈ {`openai-completions`,
   `openai-responses`, `anthropic-messages`} (`PROTOCOLS` in `lib/index.js`, `buildProvider`).
   The installed adapter's `Config` schema rejects any other value — verified in the pod — and
   that schema failure is what once took every provider down at once (`deployment.yaml`,
   seed-settings). The provider block chosen below passes that schema (verified).
2. **LiteLLM 1.101.0 (the pinned image) ships a `chatgpt/` provider** (`litellm/llms/chatgpt/`):
   Responses-native against `https://chatgpt.com/backend-api/codex`, authenticated from ONE
   process-global file `$CHATGPT_TOKEN_DIR/auth.json` (flat JSON: `access_token`,
   `refresh_token?`, `id_token?`, `expires_at`, `account_id`), re-read from disk on every call,
   one account per process (upstream issue #23777). **Hazard, measured in the pinned image with
   sockets denied:** an access token that is missing, empty, unparseable, or expired by the file's
   `expires_at` (else the JWT `exp`) with no `refresh_token` sends
   `Authenticator.get_access_token()` into the OAuth **device flow** — a synchronous `time.sleep`
   poll of up to 15 min against `auth.openai.com` — and `get_llm_provider()` runs it at Router
   construction: a missing file or a real past expiry made `Router(...)` reach for
   `auth.openai.com` immediately. The same file with `access_token: "unconfigured"` and
   `expires_at: 4102444800` built the Router in 0.07 s with no network and sent the request to
   `chatgpt.com/backend-api/codex/responses` with `Bearer unconfigured`, `ChatGPT-Account-Id`,
   and body keys exactly `include,input,instructions,model,reasoning,store,stream`. The provider
   prepends the Codex-CLI persona prompt to `instructions` unless `CHATGPT_DEFAULT_INSTRUCTIONS`
   is set (verified: the override is sent verbatim); its Responses transform drops
   `max_output_tokens`, `prompt_cache_key`, `text`, `parallel_tool_calls`. The `ai` namespace has
   no egress policy on the litellm pod.
3. **The publisher was single-seat, single-document.** Its AppRole policy `dsh-codex-publisher`
   (live text == the role's `files/policy.hcl` verbatim, comments included) was `patch` on
   `af/data/dsh/credentials` + `read` on its metadata, installed by a hand ceremony. Codex access
   tokens live ~7 days (seat b: 163 h left) and are renewed by the CLI when the seat is run —
   reviews, or the hourly `codex app-server` usage probe, which only iterates seats in
   `pr_reviewer_llm_seats`.
4. **A seat without a credential must not be in `pr_reviewer_llm_seats`.** `resolve_seats()`
   keeps a reachable seat whose `auth.json` cannot be read ("account unknown"), and sticky
   selection would hand it work, which fails as an ordinary error and burns attempts (the
   runbook's ORDER IS THE PROCEDURE note). But the seat user must exist before the credential can
   be installed into its HOME.
5. `af/litellm/*` did not exist; no ESO SecretStore existed in ns `ai`; ESO v2.7.0's v2 template
   errors on a referenced key that is absent, and sprig `default` / `toJson` are available (a
   throwaway ExternalSecret against `dsh-store` rendered `{{ .DSH_OPENBAO_CANARY | default "x" }}`
   and `{{ "" | default "unconfigured" }}` as expected — evidence for the engine only, not for the
   new store). `bao kv put KEY=` seeds an empty string (verified on a scratch path, then deleted).
   The `apps` Flux Kustomization is `wait: true`, so a never-Ready ExternalSecret wedges the layer
   (2026-09-10).
6. The device-auth login is a **human ceremony** (sign in as `realjaynesage@gmail.com` in a
   browser, on an account that has Codex access); it cannot be automated from here.

## Decision

**One OAuth owner — reviewer-2 seat d (`codexrun4`, `realjaynesage@gmail.com`) — and access-only
projections everywhere, exactly the shape seat b already has.** The refresh token never leaves
`/home/codexrun4/.codex/auth.json`; only the access token and its identity claims are published.

- **LiteLLM serves the subscription through its own `chatgpt/` provider**, as a NEW route
  `gpt-6-astra-realjaynesage` → `chatgpt/gpt-6-astra` (`model_info: { mode: responses,
  supports_vision: true }`) in `litellm.yaml`, with `CHATGPT_TOKEN_DIR=/chatgpt-auth`,
  `CHATGPT_DEFAULT_INSTRUCTIONS="You are a helpful assistant."` (dsh's own system prompt arrives
  as a developer message; the Codex-CLI persona text must not be prepended to it), and Secret
  `litellm-chatgpt-auth` mounted read-only at `/chatgpt-auth` with `optional: false` — a pod
  without the file is the device-flow path of finding 2, so it must not start without it.
- **dsh gets the named provider the operator asked for as a Responses route THROUGH LiteLLM:**
  provider key `openai-codex-realjaynesage`, displayName `OpenAI Codex (realjaynesage)`,
  `api: openai-responses`, `baseURL: http://litellm.ai.svc.cluster.local:4000/v1`,
  `apiKeyEnv: LITELLM_API_KEY`, `transport: sse`, one model `gpt-6-astra-realjaynesage`
  (`contextWindow: 272000`, `maxTokens: 128000`, `input: [text, image]`, `reasoningEfforts`
  mirroring the catalog's gpt-6-astra map with `"off"` quoted — YAML 1.1 reads a bare `off` as
  false). Through LiteLLM because finding 1 rules out a second native route on the installed
  adapter (checked on 0.1.5-alpha.2, 0.1.5-rc.2, 0.1.6-alpha.2). seed-settings reconciles it per
  boot: `DSH_PROVIDER=openai-codex-realjaynesage node /seed/reconcile-provider.js`.
- **The vault side is a Job, not a ceremony** (`chatgpt-provision-job.yaml`, breakglass-auth like
  `dsh-provision-job.yaml`): it owns policy `af-app-litellm` (`read` on `af/data/litellm/chatgpt`
  only), takes over policy `dsh-codex-publisher` from the hand ceremony (the role's
  `files/policy.hcl` is gone; the policy now also carries `patch af/data/litellm/chatgpt` +
  `read af/metadata/litellm/chatgpt`, with a whole-document drift guard per policy), owns the
  k8s-auth role `af-app-litellm` (`litellm-eso`@`ai`, `token_ttl=1h`,
  `alias_name_source=serviceaccount_uid`, written and read back), and creates `af/litellm/chatgpt`
  ONCE (`-cas=0`) with every key the ESO template references EMPTY (`CHATGPT_ACCESS_TOKEN`,
  `CHATGPT_ACCOUNT_ID`, `CHATGPT_ACCOUNT_EMAIL`, `CHATGPT_EXPIRES_AT`) plus
  `CHATGPT_OPENBAO_CANARY=provisioned` — finding 5 is why the keys must exist before the first
  publish. The AppRole itself (role-id/secret-id, `token_ttl=60s`) stays the operator ceremony in
  `docs/runbooks/dsh.md`.
- **The device-flow guard.** The ExternalSecret `litellm-chatgpt-auth` (ns `ai`, SecretStore
  `litellm-store`, refresh 5m) renders ONE key, `auth.json`:
  `{"access_token": {{ .CHATGPT_ACCESS_TOKEN | default "unconfigured" | toJson }}, "account_id": {{ .CHATGPT_ACCOUNT_ID | default "unconfigured" | toJson }}, "expires_at": 4102444800}`.
  The non-empty placeholder and the far-future `expires_at` (2100-01-01) are the guard against
  finding 2: LiteLLM never judges the token expired from this file and never enters the device
  flow — a not-yet-published or stale token fails FAST upstream (401) instead of freezing a
  worker or a rollout. `toJson` makes the file valid JSON whatever the vault holds (an unparseable
  file is the device-flow path too). The real expiry stays visible as `CHATGPT_EXPIRES_AT` in the
  vault and as `dsh_codex_projection_token_expires_at_seconds`. `account_id` is published beside
  the token because without it LiteLLM derives it from the JWT and then tries to WRITE the file to
  cache it, which a read-only Secret mount refuses on every request.
- **One account per LiteLLM process.** The `chatgpt/` provider reads one auth file per process
  (finding 2), so this proxy serves exactly one ChatGPT subscription. A third would need a second
  LiteLLM deployment, not a second route; that is not in scope.
- **Accepted parameter loss on this path.** `max_output_tokens` is never sent (dsh's `maxTokens`
  sizes the model and is not a per-request cap unless configured; the model's own 128k ceiling
  applies); `text.verbosity` and `prompt_cache_key` are dropped by LiteLLM's transform (default
  verbosity; prefix caching relies on the backend `session_id` header LiteLLM sets per call);
  `parallel_tool_calls` is dropped (backend default). None of these changes an answer's
  correctness; none is surfaced to the user beyond this record.
- **The publisher is generalised, and it exports its own freshness.** Config becomes
  `{address, textfile, projections: [{auth_path, email, kv_path, prefix, optional}]}`; each
  projection publishes `<prefix>_ACCESS_TOKEN`, `<prefix>_ACCOUNT_ID`, `<prefix>_ACCOUNT_EMAIL`,
  `<prefix>_EXPIRES_AT` by CAS PATCH into its own document, logs in on its own (the AppRole
  token lives 60 s), and fails independently of the others; an ABSENT auth file is a logged skip
  only for an `optional` (staged) projection, and the service exits 1 if any projection failed.
  It writes a node_exporter textfile on reviewer-2 with `dsh_codex_projection_ok{document,email}`,
  `dsh_codex_projection_optional{document}`,
  `dsh_codex_projection_token_expires_at_seconds{document}`,
  `dsh_codex_projection_last_success_timestamp_seconds{document}` and
  `dsh_codex_publisher_last_run_timestamp_seconds`. Four rules in `reviewbot-rules.yaml` read
  them: `CodexProjectionFailing` (a required projection with `ok == 0` for 30 m),
  `CodexProjectionStale` (a published token with less than 24 h left), `CodexPublisherDown`
  (the heartbeat 15 min old — a stopped unit leaves the previous file scrapeable with `ok == 1`,
  which the first two cannot see) and `CodexPublisherMetricsMissing` (`absent()` on the
  heartbeat: a textfile never written or removed is not health). The reviewer's own login can
  be healthy while the publisher fails; these tell those apart. **What they cannot see.** They
  describe the OpenBao copy. ESO's hop is watched separately by `LiteLLMChatGPTAuthNotReady`
  (`ha-rules.yaml`, on ESO's own `externalsecret_status_condition`). Kubelet's projection of
  the Secret into the volume is the remaining blind spot, stated rather than papered over: it
  is bounded by kubelet's sync period (minutes), not observable from outside the pod, and
  LiteLLM exports no token-expiry metric; the post-login verification decodes the mounted
  token's `exp` in both replicas once, and the offline contract test proves the process
  re-reads the file on every call, so a token ESO and kubelet delivered is the token LiteLLM
  sends.
- **The seat is STAGED before it is served** (finding 4): a new role var
  `pr_reviewer_llm_seats_staged` provisions a seat like any other (user, 0700 `~/.codex`, model
  pin, sudoers) but does NOT render it into reviewbot's `config.json`;
  `pr_reviewer_seats_effective` = the active list (or the legacy single seat) + staged. Seat d's
  projection is `optional: true` while staged.
- **Sequencing, and the human step in the middle.** PR 1 (the Job) is green before PR 2 lands;
  PR 2 ships the publisher, the staged seat, ESO, the LiteLLM route, the dsh provider, the alerts
  and these docs (the dashboard's seat-d row is a separate PR: the generated ConfigMap's one-line
  JSON put the combined diff past the reviewers' size cap), and is applied with `ansible-playbook reviewers.yml -l reviewer-2 -t seats,dsh-codex`.
  Then the operator logs in DIRECTLY as `codexrun4` (`sudo -n -u codexrun4 HOME=/home/codexrun4
  setsid nohup /usr/bin/codex login --device-auth ...`, sign in as `realjaynesage@gmail.com`,
  wait for the CLI to report success, verify with `codex-usage.py` JSON `"ok": true` and the
  email) — no scratch HOME, no copy, nothing to delete, so no second refresh-token family can
  exist. The publisher's next minute fills `af/litellm/chatgpt`, ESO follows within 5 min, LiteLLM
  reads the file per request; nothing restarts. PR 3 moves seat d into `pr_reviewer_llm_seats`
  (hourly probe = token refresh; sticky rotation a→b→c→d), flips its projection to
  `optional: false`, and is verified by `reviewbot_llm_seats_distinct{persona="codex"} == 4`.
- **Rollback is explicit at every layer.** Removing the provider block from the dsh seed does not
  remove it from the PVC: the seed-settings step must run
  `DSH_PROVIDER=openai-codex-realjaynesage DSH_PROVIDER_REMOVE=1 node /seed/reconcile-provider.js`
  for one boot. Reverting the Job from git does not undo its writes: policy `af-app-litellm`, the
  k8s-auth role `af-app-litellm` and the `af/litellm/chatgpt` document are deleted explicitly with
  the breakglass token, and policy `dsh-codex-publisher` returns to its two-path form through the
  Job's heredoc (and its BASELINE/DESIRED strings) — git owns that policy now, so a hand
  `bao policy write` would be reverted by the Job's next run.

## Consequences

- **The litellm pod carries a subscription token as a mounted file, and nothing else.** No OpenBao
  identity, no token, no egress exception (the same argument as dsh's `openbao-eso.yaml`). The
  policy `af-app-litellm` is a single-path `read`; the ServiceAccount `litellm-eso` has no token
  automount and is used only by ESO.
- **The guard is load-bearing, not cosmetic.** With the `ai` namespace having no egress policy on
  the litellm pod (finding 2), a device flow would actually reach `auth.openai.com` and park a
  worker for up to 15 min — and `Router(...)` would do it at pod start. Any change to the
  template must keep the placeholder, the sentinel `expires_at` and `toJson`; the offline route
  contract the plan adds to `litellm-route-contract.yaml` (`scripts/tests/integration/test_litellm_chatgpt_route_contract.py`)
  is what makes that rule executable.
- **A pod without the Secret does not start** (`optional: false`), so the ExternalSecret must be
  Ready before the litellm rollout — which is why the Job seeds every key empty (finding 5) and
  why PR 1 precedes PR 2. The `apps` layer is `wait: true`; a never-Ready ExternalSecret would
  wedge it.
- **Until PR 3, seat d's token is not renewed, and nothing pages for it.** The publisher stops
  updating after ~7 days and LiteLLM fails fast with 401 — `CodexProjectionStale` and
  `CodexProjectionFailing` are gated on `optional == 0`, so a staged seat ages out WITHOUT an alert
  (the rule's own comment records this seam); PR 3 flipping the projection to required is what brings
  it under them. Between the login ceremony and PR 3 the publisher journal and the textfile are the
  only signals. LiteLLM's failure is — visible, not
  silent, and self-healing on activation. Once activated, seat d is both reviewer capacity and
  dsh's Astra route, the same arrangement seat b has.
- **Discoverability is the proxy's, unchanged.** `mode: responses` keeps the route out of the
  generated Open WebUI Local group and the dsh-litellm list, but like every other route on this
  proxy it is discoverable through Open WebUI's master-key connection under External; the
  chat-completions path for `chatgpt/gpt-6-astra` is unverified (only the Responses path has a
  consumer, dsh) and the post-login check records what a user gets there. There is no per-caller
  model allowlist on the proxy; adding one is a separate project.
- `chatgpt/gpt-6-astra` is absent from LiteLLM's price map — cost logging warns, routing is
  unaffected.
- **Two documents, two prefixes, one publisher, one policy.** `af/dsh/credentials.DSH_CODEX_*`
  and `af/litellm/chatgpt.CHATGPT_*` are publisher-owned: not seeded, not restorable by any
  seeder, and only PATCHable — the post-wipe order is in `docs/runbooks/openbao-recovery.md`
  (PUBLISHER-OWNED row). `DSH_CODEX_ACCOUNT_ID` is added to the dsh document for one field set
  across projections; nothing in dsh reads it.
- **The subscription-use risk is not new.** Using a ChatGPT subscription through a gateway is the
  class of risk the operator accepted on 2026-09-16 (`plans/2026-09-16-codex-seat-rotation-plan.md`);
  this adds a consumer, not a new class, and it is not re-raised here.

## Alternatives rejected

- **A second native dsh Codex provider.** Impossible on the installed adapter (finding 1): the
  subscription protocol is bound to the catalog id `openai-codex`, and a hand-declared route with
  any other `api` value fails the `Config` schema — which takes every provider down, not one.
- **A LiteLLM-owned device-flow login, with its own refresh token in the pod.** Rejected: it
  creates a second refresh-token family for the same account (OpenAI refresh tokens are
  single-use; two families revoke each other on first refresh — the 2026-09-10 outage), and puts
  a long-lived credential outside OpenBao, in a pod with no egress policy. One owner, on the
  reviewer host, projecting access tokens, is the shape ADR 0020 established.
- **A second deployment under the existing `gpt-6-astra` model_name.** Rejected: least-busy
  routing across a paid API key and a subscription is not a policy anyone chose, and the dsh
  provider needs a name that maps to the subscription only.
- **Moving seat b** (to LiteLLM, or off dsh). Out of scope; seat b's projection is unchanged.
- **A per-caller model allowlist on the proxy** (virtual keys per consumer). Not in scope; the
  exposure class already exists for every route on this proxy.

Operations — the two seats, the publisher's config and metrics, the AppRole ceremony, the login
ceremony and the checks: `docs/runbooks/dsh.md` § "Codex subscriptions"; the staged-seat
procedure: `docs/runbooks/dev-workers.md` § "Seats: the codex persona holds several subscriptions".
