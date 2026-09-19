# A second ChatGPT subscription (realjaynesage) for GPT-6 Astra: LiteLLM `chatgpt/` route, a named DSH provider, reviewer-2 seat d

## Context

DSH's GPT-6 Astra route is the native `openai-codex` provider (`kubernetes/apps/apps/dsh/settings.seed.yaml`),
whose credential is reviewer-2 **seat b** (`codexrun2`, `realjaysage@gmail.com`, ChatGPT plan `prolite`),
projected access-only by `ansible/roles/dsh_codex_publisher` into `af/dsh/credentials.DSH_CODEX_*`
and mounted through ESO at `/dsh-credentials` (`docs/runbooks/dsh.md` § Codex subscription). On
2026-09-19 that seat's weekly window reads **100 %** (`reviewbot_llm_usage_percent{persona="codex",seat="b"}`),
so DSH has no usable Astra route. The operator wants a second subscription, `realjaynesage@gmail.com`
(today a Claude seat on reviewer-1 only — no Codex login exists for it anywhere), as "a new openai
subscription in litellm and a new OpenAI Codex (realjaynesage) provider like the realjaysage one".

Findings that fix the shape of the change (all measured on the live estate, 2026-09-19):

1. **DSH cannot host a second native Codex provider.** In `@deepseek-ai/dsh-llm-pi-ai` 0.1.5-alpha.2
   (and 0.1.5-rc.2 / 0.1.6-alpha.2 on npm — checked), the Codex subscription protocol
   (`openai-codex-responses`) is reachable only by reusing the *catalog* route id `openai-codex`; a
   hand-declared route must name `api` ∈ {`openai-completions`, `openai-responses`, `anthropic-messages`}
   (`PROTOCOLS` in `lib/index.js`, `buildProvider`; the installed adapter's `Config` schema rejects any
   other value — verified in the pod, and that schema failure is what took every provider down once,
   `deployment.yaml` seed-settings). The planned provider block below passes that schema (verified).
2. **LiteLLM 1.101.0 (the pinned image) ships a `chatgpt/` provider** (`litellm/llms/chatgpt/`):
   Responses-native against `https://chatgpt.com/backend-api/codex`, auth from ONE process-global file
   `$CHATGPT_TOKEN_DIR/auth.json` (flat JSON: `access_token`, `refresh_token?`, `id_token?`,
   `expires_at`, `account_id`), re-read from disk on every call, one account per process (upstream
   issue #23777). **Hazard, measured in the pinned image with sockets denied:** an access token that is
   missing, empty, unparseable, or expired by the file's `expires_at` (else the JWT `exp`) with no
   `refresh_token` sends `Authenticator.get_access_token()` into the OAuth **device flow** — a synchronous
   `time.sleep` poll of up to 15 min against `auth.openai.com` — and `get_llm_provider()` runs it at
   Router construction: a missing file or a real past expiry made `Router(...)` reach for
   `auth.openai.com` immediately; the same file with `access_token: "unconfigured"` and
   `expires_at: 4102444800` built the Router in 0.07 s with no network and sent the request to
   `chatgpt.com/backend-api/codex/responses` with `Bearer unconfigured`, `ChatGPT-Account-Id`, and
   body keys exactly `include,input,instructions,model,reasoning,store,stream`. The provider prepends the
   Codex-CLI persona prompt to `instructions` unless `CHATGPT_DEFAULT_INSTRUCTIONS` is set (verified: the
   override is sent verbatim); its Responses transform drops `max_output_tokens`, `prompt_cache_key`,
   `text`, `parallel_tool_calls`. The `ai` namespace has no egress policy on the litellm pod.
3. **The publisher is single-seat, single-document.** Its AppRole policy `dsh-codex-publisher` (live text
   == `files/policy.hcl` verbatim, comments included) is `patch` on `af/data/dsh/credentials` + `read`
   on its metadata, installed by a hand ceremony. Codex access tokens live ~7 days (seat b: 163 h left)
   and are renewed by the CLI when the seat is run — reviews, or the hourly `codex app-server` usage
   probe, which only iterates seats in `pr_reviewer_llm_seats`.
4. **A seat without a credential must not be in `pr_reviewer_llm_seats`.** `resolve_seats()` keeps a
   reachable seat whose `auth.json` cannot be read ("account unknown") and sticky selection would hand
   it work, which fails as an ordinary error and burns attempts (the runbook's ORDER IS THE PROCEDURE
   note). But the seat user must exist before the credential can be installed into its HOME.
5. `af/litellm/*` does not exist; no ESO SecretStore exists in ns `ai`; ESO v2.7.0's v2 template errors
   on a referenced key that is absent, and sprig `default` / `toJson` are available (a throwaway
   ExternalSecret against `dsh-store` rendered `{{ .DSH_OPENBAO_CANARY | default "x" }}` and
   `{{ "" | default "unconfigured" }}` as expected — evidence for the engine only, not for the new
   store). `bao kv put KEY=` seeds an empty string (verified on a scratch path, then deleted). The
   `apps` Flux Kustomization is `wait: true`, so a never-Ready ExternalSecret wedges the layer (2026-09-10).
6. The device-auth login is a **human ceremony** (sign in as realjaynesage@gmail.com in a browser, on an
   account that has Codex access); it cannot be automated from here.

## Approach

**One OAuth owner** — reviewer-2 seat d (`codexrun4`, realjaynesage) — and **access-only projections**
everywhere, exactly the shape seat b already has. LiteLLM serves the subscription through its own
`chatgpt/` provider; DSH gets the named provider the operator asked for as a Responses-protocol route
*through LiteLLM*, because (1) rules out a second native one. Three PRs, in order, each merged before
the next lands.

### PR 1 — the OpenBao side (`kubernetes/apps/infrastructure/security/openbao/chatgpt-provision-job.yaml`)

A breakglass Job `openbao-chatgpt-provision`, same shape and guards as `dsh-provision-job.yaml`:

- **Owns policy `af-app-litellm`**: `read` on `af/data/litellm/chatgpt` only.
- **Owns policy `dsh-codex-publisher`** (taking it over from the hand ceremony; `files/policy.hcl`
  leaves the ansible role): `patch af/data/dsh/credentials`, `read af/metadata/dsh/credentials`,
  `patch af/data/litellm/chatgpt`, `read af/metadata/litellm/chatgpt`. Drift guard per policy: full-line
  comments stripped, then whitespace-normalised; BASELINE = the two live path lines (read from the vault
  today), DESIRED = the heredoc. Both guards run before either write. Any other document refuses.
- **Owns k8s auth role `af-app-litellm` whole** (`bound_service_account_names=litellm-eso`,
  `bound_service_account_namespaces=ai`, `token_policies=af-app-litellm`, `token_ttl=1h`,
  `alias_name_source=serviceaccount_uid` — the shape `af-app-dsh` has live). Written, not merely
  asserted as the dsh Job does, because no ceremony ever created this one. The ownership contract is
  the same as the policies' and is stated in the file: `bao write` replaces the role, so a hand
  restriction (a narrowed binding during an incident) is reverted by the next run — an emergency change
  must suspend the Job and land in git. The script reads back `token_policies`,
  `bound_service_account_names` and `bound_service_account_namespaces` after writing (exact-element
  match, not substring). Live proof that the binding works is the `litellm-store` SecretStore turning
  Ready in PR 2 (the only identity that logs in with it is `litellm-eso`); a negative login with
  another ServiceAccount is not added — the read-back asserts the binding the vault enforces.
- **Creates `af/litellm/chatgpt` ONCE** (`-cas=0`) carrying EVERY key the ESO template will reference,
  empty: `CHATGPT_ACCESS_TOKEN`, `CHATGPT_ACCOUNT_ID`, `CHATGPT_ACCOUNT_EMAIL`, `CHATGPT_EXPIRES_AT`,
  plus `CHATGPT_OPENBAO_CANARY=provisioned`. Never touches it again — the publisher owns the contents.
- Tests: `scripts/tests/test_openbao_chatgpt_provision.py` + a fixture `bao` shim with per-policy state,
  mirroring `test_openbao_dsh_provision.py` (self-wedge invariant for BOTH policies, baseline == live,
  no write on drift/unreadable/list-failure, role written and read back, KV seeded with all four keys
  once, soft-delete loud). `test_openbao_dsh_provision.py` keeps passing (that Job is untouched).
- Verify live, without printing a value: Job `Succeeded`; `bao policy read af-app-litellm` /
  `dsh-codex-publisher` match the heredocs; `bao read auth/kubernetes/role/af-app-litellm`;
  `bao kv get -mount=af -field=CHATGPT_OPENBAO_CANARY litellm/chatgpt` prints `provisioned`, and
  `bao kv get -mount=af -format=json litellm/chatgpt | grep -o '"CHATGPT_[A-Z_]*":'` prints the five
  key NAMES (the pattern ends at the colon, so no value can appear — `bao kv list` lists document
  names under a prefix, not a document's fields, so it cannot do this). The `dsh-codex-publisher`
  journal keeps reading "unchanged" — the existing projection is unaffected.

### PR 2 — publisher, reviewer staging, LiteLLM, DSH, monitoring, docs (after PR 1's Job is green)

- **Publisher generalised** (`ansible/roles/dsh_codex_publisher/files/publish.py`, `tasks/main.yml`,
  `scripts/tests/test_dsh_codex_publisher.py`): config becomes
  `{address, projections: [{auth_path, email, kv_path, prefix, optional}]}`. Each projection publishes
  `<prefix>_ACCESS_TOKEN`, `<prefix>_ACCOUNT_ID`, `<prefix>_ACCOUNT_EMAIL`, `<prefix>_EXPIRES_AT` by CAS
  PATCH into its own document with its own state digest (the account id, a UUID JWT claim, is what
  LiteLLM's auth file wants beside the token; it is added to the DSH document too, for symmetry).
  Failure semantics per projection, each isolated from the others: wrong email, ≤5 min left, or a
  missing account id → refused (as today); an ABSENT auth file is a logged skip ONLY when the projection
  is `optional: true` (seat d until PR 3 activates it) and a failure otherwise; an unreadable or
  unparseable file is always a failure; a vault error (HTTP code logged, never the body) is a failure.
  Every projection runs; the service exits 1 if any failed, which the journal-to-Loki shipping and the
  textfile metrics below make visible. Each projection logs in on its own (AppRole token ttl is 60 s;
  a run must never carry one token across projections), every HTTP operation has a 20 s timeout, and
  `TimeoutStartSec` rises 90 → 150 s (two projections × three operations × 20 s = 120 s worst case).
  Tests cover, independently: missing (optional and required), malformed JSON, permission denied,
  wrong account, expired, vault HTTP error on one projection with the other still published, one login
  per projection, per-document state, and that no line printed contains a token or refresh token.
  host_vars `reviewer-2.yml`: seat b → `dsh/credentials` prefix `DSH_CODEX` (required); seat d
  (`/home/codexrun4/.codex/auth.json`, `realjaynesage@gmail.com`) → `litellm/chatgpt` prefix `CHATGPT`,
  `optional: true` until PR 3.
- **Projection freshness signals** (same PR): the publisher writes a node_exporter textfile on
  reviewer-2 (the `pr_reviewer_textfile` directory the reviewbot metrics already use) with, per
  projection, `dsh_codex_projection_ok{document,email}` (1/0), `dsh_codex_projection_token_expires_at_seconds{document}`
  (the JWT expiry it last published) and `dsh_codex_projection_last_success_timestamp_seconds{document}`.
  One rule in `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.yaml`, `CodexProjectionStale`:
  a required projection with `ok == 0` for 30 m, or a published token with less than 24 h left — the
  reviewer's own login can be healthy while the publisher, ESO or kubelet fails, and this is the
  signal that tells those apart. The textfile is written atomically (temp + rename) like reviewbot's;
  the unit gains `ReadWritePaths=/var/lib/prometheus/node-exporter` under its `ProtectSystem=strict`,
  and the writer chmods the file 0644 explicitly because the unit's `UMask=0077` masks the mode
  `os.open` is given (a 0600 file is invisible to node_exporter). Live check after the apply:
  `curl -s http://192.168.0.25:9100/metrics | grep dsh_codex_` shows every series.
  **A stopped publisher must not read as healthy.** A stopped timer, or a process killed before it
  writes, leaves the previous textfile scrapeable with `ok == 1`, so a third rule,
  `CodexPublisherDown` — `time() - dsh_codex_publisher_last_run_timestamp_seconds > 900`, `for: 10m`
  (the timer fires every minute; 15 min of silence is a stopped unit, not a slow run) — plus
  `CodexPublisherMetricsMissing` on `absent(dsh_codex_publisher_last_run_timestamp_seconds{job="reviewer-node"})`
  (`for: 30m`: the textfile was never written or was removed; `ReviewerNodeDown` covers a dead VM).
  `last_success` advances on an UNCHANGED run too (confirming the document is the success), so a
  quiet week of identical tokens does not age it. promtool cases cover: a required document failing
  for 30 m fires, an optional one does not, a healthy one does not; a token 12 h from expiry fires,
  7 days does not; a frozen `last_run` fires after 15 m and a ticking one does not; the absent series.
- **reviewer-2 seat d, staged**: new role var `pr_reviewer_llm_seats_staged` (default `[]`) —
  provisioned like a seat (user, 0700 `~/.codex`, model pin, sudoers) but NOT rendered into
  `config.json`, so reviewbot never sees it. `pr_reviewer_seats_effective` = active list (or the legacy
  single seat) + staged. `host_vars/reviewer-2.yml`: `staged: [{name: d, sudo_user: codexrun4}]`.
- **ESO in ns `ai`** (`kubernetes/apps/apps/ai/litellm-chatgpt-eso.yaml`, the dsh pattern): SA
  `litellm-eso` (no token automount), Certificate `openbao-ca` → Secret `openbao-tls` (ClusterIssuer
  `ailab-ca`), SecretStore `litellm-store` (k8s auth role `af-app-litellm`), ExternalSecret
  `litellm-chatgpt-auth` (refresh 5m, `dataFrom.extract` `litellm/chatgpt`, template v2 rendering ONE
  key `auth.json`):
  `{"access_token": {{ .CHATGPT_ACCESS_TOKEN | default "unconfigured" | toJson }}, "account_id": {{ .CHATGPT_ACCOUNT_ID | default "unconfigured" | toJson }}, "expires_at": 4102444800}`
  — `toJson` makes the string fields JSON-safe whatever the vault holds, so the file LiteLLM parses is
  valid JSON by construction. The far-future `expires_at` and the non-empty placeholder are the guard
  against finding 2: LiteLLM never enters the device flow — a stale or not-yet-published token fails
  FAST upstream (401) instead of freezing a worker or a rollout. The real expiry stays visible as
  `CHATGPT_EXPIRES_AT` in OpenBao and as `dsh_codex_projection_token_expires_at_seconds`.
  **What those metrics do and do not cover.** They describe the OpenBao copy. Two more hops sit
  between it and LiteLLM's request: ESO's sync, and kubelet's projection into the mounted volume.
  ESO is watched by its own scraped status series (`externalsecret_status_condition`, 54 live series,
  the precedent being `ForgeConductSecretNotReady`): a fourth rule, `LiteLLMChatGPTAuthNotReady`,
  `externalsecret_status_condition{exported_namespace="ai",name="litellm-chatgpt-auth",condition="Ready",status="False"} == 1`,
  `for: 15m` (one refresh interval plus slack), in `ha-rules.yaml` beside LiteLLM's availability
  rules. Kubelet's hop is the remaining blind spot and is stated as such in the ADR: it is bounded
  by kubelet's sync period (minutes), not observable from outside the pod, and LiteLLM exports no
  token-expiry metric; the post-login verification decodes the mounted token's `exp` in both
  replicas once, and the contract test proves the process re-reads the file per call, so a
  projected token that ESO and kubelet delivered is the token LiteLLM sends.
- **LiteLLM** (`kubernetes/apps/apps/ai/litellm.yaml`): env `CHATGPT_TOKEN_DIR=/chatgpt-auth`,
  `CHATGPT_DEFAULT_INSTRUCTIONS="You are a helpful assistant."` (DSH's own system prompt arrives as a
  developer message; the Codex-CLI persona text must not be prepended to it); Secret volume
  `litellm-chatgpt-auth` at `/chatgpt-auth`, readOnly, **`optional: false`** — a pod without the file is
  the device-flow path, so it must not start without it. `model_list` entry
  `gpt-6-astra-realjaynesage` → `chatgpt/gpt-6-astra`, `model_info: { mode: responses, supports_vision: true }`.
  A NEW model_name, not a second deployment of `gpt-6-astra`: least-busy across the paid API key and a
  subscription is not a policy anyone chose, and the DSH provider needs a name that maps to the
  subscription only. `mode: responses` keeps it out of the generated Open WebUI Local group and the
  dsh-litellm list; like every other route on this proxy it stays discoverable through Open WebUI's
  master-key connection under External (chat-completions through this provider is unverified for this
  model).
  Regenerate `checksum/config` with `scripts/gen-litellm-consumers.py --write`.
- **Offline route contract for the new route**, in CI (`scripts/tests/integration/test_litellm_chatgpt_route_contract.py`,
  run by `litellm-route-contract.yaml`'s docker step beside the existing test — the pinned image,
  network disabled, sockets denied at the Python level like the existing document-route test; the
  workflow's `push` AND `pull_request` path filters gain both the new test and
  `kubernetes/apps/apps/ai/litellm-chatgpt-eso.yaml`, so a template-only or test-only change still
  runs the device-flow regression): (a) with the exact rendered
  production `auth.json` shape (placeholder + sentinel), `Router([route])` and a Responses call make
  NO network attempt, the request goes to `chatgpt.com/backend-api/codex/responses` with the expected
  headers, `instructions` is the override, `store` false, `include` carries
  `reasoning.encrypted_content`; (b) a JWT whose `exp` is in the past under the sentinel `expires_at`
  behaves identically (the sentinel is what is load-bearing); (c) a missing file, an empty token and a
  malformed file each make LiteLLM attempt `auth.openai.com` — asserted as the denied attempt — which
  documents in an executable form why the template must always render (a); (d) upstream 401 and 429
  map to `AuthenticationError` / `RateLimitError` in seconds; (e) **credential switch without a
  restart**: through ONE already-constructed Router, three calls with the file rewritten atomically
  between them (placeholder → token A → token B, each a syntactically valid JWT with its own
  `chatgpt_account_id` claim and `account_id` field) send `Authorization: Bearer <that token>` and
  `ChatGPT-Account-Id` for the file that was current at call time, with no OAuth attempt — the
  per-call re-read is what makes a rotation reach LiteLLM with nothing restarted, and a cached
  credential would be invisible to any check of the mounted file.
- **DSH** (`settings.seed.yaml`, `deployment.yaml`): provider `openai-codex-realjaynesage`, displayName
  `OpenAI Codex (realjaynesage)`, `api: openai-responses`, `baseURL: http://litellm.ai.svc.cluster.local:4000/v1`,
  `apiKeyEnv: LITELLM_API_KEY`, `transport: sse`, one model `gpt-6-astra-realjaynesage` (name
  `GPT-6 Astra`, `contextWindow: 272000`, `maxTokens: 128000`, `input: [text, image]`,
  `reasoningEfforts` mirroring the catalog's gpt-6-astra map, `"off"` quoted — YAML 1.1 reads a bare
  `off` as false; this exact block passes the installed adapter's `Config` schema). seed-settings gains
  `DSH_PROVIDER=openai-codex-realjaynesage node /seed/reconcile-provider.js`.
  **Accepted parameter loss on this path, recorded in the ADR:** `max_output_tokens` is never sent
  (DSH's `maxTokens` sizes the model and is not a per-request cap unless configured; the model's own
  128k ceiling applies); `text.verbosity` and `prompt_cache_key` are dropped by LiteLLM's transform
  (default verbosity; prefix caching relies on the backend's `session_id` header LiteLLM sets per call);
  `parallel_tool_calls` is dropped (backend default). None of these changes an answer's correctness;
  none is surfaced to the user beyond this record.
- **Monitoring**: `scripts/gen-reporting-dashboard.py` `SEAT_NAMES` += `d`; regenerate
  `reporting-dashboard.yaml`. Alert rules are seat-count agnostic (checked). The projection rule above
  is the new signal.
- **Docs**: ADR 0026 (why LiteLLM's provider and not a second native DSH route; the device-flow guard
  and its measurements; the single-account limit; the parameter loss; the subscription-use risk already
  accepted in the 2026-09-16 plan; **rollback**: removing the provider block from the seed does not
  remove it from the PVC — the seed-settings step must run `DSH_PROVIDER=openai-codex-realjaynesage
  DSH_PROVIDER_REMOVE=1` for one boot; reverting the Job does not undo the vault objects — delete the
  two policies' grants, the role and the KV document explicitly with the breakglass token);
  `docs/runbooks/dsh.md` § Codex subscription rewritten for two seats / two documents;
  `docs/runbooks/dev-workers.md` seats table (c exists; d staged) and the staged-seat procedure;
  `docs/runbooks/openbao-recovery.md` path classes gain **PUBLISHER-OWNED** (`dsh/credentials.DSH_CODEX_*`,
  `litellm/chatgpt.*`) with the **ordered reconstruction**: (0) restore the k8s auth role
  `af-app-dsh` by hand (`bao write auth/kubernetes/role/af-app-dsh bound_service_account_names=dsh-eso
  bound_service_account_namespaces=dsh token_policies=af-app-dsh token_ttl=1h alias_name_source=serviceaccount_uid`)
  — the unchanged `openbao-dsh-provision` Job ASSERTS that role and aborts without it, a pre-existing
  gap this plan records rather than fixes; the chatgpt Job needs no such step because it writes its
  own role; (1) the `openbao-chatgpt-provision` and `openbao-dsh-provision` Jobs recreate both
  policies, the role `af-app-litellm` and both documents — `af/litellm/chatgpt` with its four empty
  keys, `af/dsh/credentials` with ONLY its canary (its other operator fields — GITEA_*, the operator
  SSH identity — come back by the ceremonies in `dsh.md`); (2) the AppRole ceremony in `dsh.md`
  recreates `dsh-codex-publisher` and mints a role-id / secret-id into
  `/etc/dsh-codex-publisher/approle.json`; (3) the publisher's next minute adds `DSH_CODEX_*` and
  `CHATGPT_*` — it can only PATCH, so (1) must precede it, and a soft-deleted document is refused
  until an operator `kv undelete`s or the Job's loud-failure branch is resolved;
  `docs/runbooks/openbao-estate-credentials.md` access paragraph (a second ESO store, `af-app-litellm`,
  reads one non-estate path).
- **Apply after merge** (WSL, `ANSIBLE_CONFIG`): `ansible-playbook reviewers.yml -l reviewer-2 -t seats,dsh-codex`
  → `codexrun4` exists, the publisher config carries both projections (seat d logged as skipped until
  the login). Flux rolls litellm (2 replicas, maxUnavailable 0) and dsh.

### Operator ceremony (human, after PR 2)

Prerequisite: `realjaynesage@gmail.com` is a ChatGPT account with Codex access (device login is what
the CLI offers a headless host). The login happens **directly as the staged seat user** — no scratch
HOME, no copy, nothing to delete, so no second refresh-token family can ever exist:

```
ssh c4@192.168.0.25
sudo -n -u codexrun4 HOME=/home/codexrun4 setsid nohup /usr/bin/codex login --device-auth \
    > /tmp/seat-d-login.log 2>&1 < /dev/null &
sleep 10 && cat /tmp/seat-d-login.log          # URL + one-time code; sign in as realjaynesage@gmail.com
# then WAIT for the CLI to report success in that log before anything else:
tail -f /tmp/seat-d-login.log                  # "Successfully logged in" (or the CLI's equivalent)
sudo -n -u codexrun4 HOME=/home/codexrun4 /usr/local/lib/reviewbot/codex-usage.py
```

The last command exits 0 either way; read its JSON: `"ok": true` and `"email": "realjaynesage@gmail.com"`
are the check. The publisher's next minute publishes to `af/litellm/chatgpt`; ESO follows within 5 min;
LiteLLM reads the file per request — nothing restarts.

### PR 3 — activation (after the login)

Move seat d from `pr_reviewer_llm_seats_staged` to `pr_reviewer_llm_seats` (hourly probe = token
refresh; sticky rotation a→b→c→d), flip its projection to `optional: false`, `-t reviewbot,dsh-codex`.
Verify `reviewbot_llm_seats_distinct{persona="codex"} == 4` and
`reviewbot_llm_seat_info{seat="d",email="realjaynesage@gmail.com"}`.

## Critical files

| path | role |
|---|---|
| `kubernetes/apps/infrastructure/security/openbao/chatgpt-provision-job.yaml` (+ `kustomization.yaml`) | PR 1: policies `af-app-litellm` + `dsh-codex-publisher`, role, KV bootstrap |
| `scripts/tests/test_openbao_chatgpt_provision.py`, `scripts/tests/fixtures/openbao-chatgpt-provision/bao` | PR 1 gates |
| `ansible/roles/dsh_codex_publisher/{files/publish.py,files/dsh-codex-publisher.service,tasks/main.yml}`, `files/policy.hcl` (removed) | multi-projection publisher + textfile metrics |
| `scripts/tests/test_dsh_codex_publisher.py` | publisher contract (access-only, per-projection isolation, CAS, metrics) |
| `ansible/host_vars/reviewer-2.yml` | seat d staged; two projections |
| `ansible/roles/pr_reviewer/defaults/main.yml` | `pr_reviewer_llm_seats_staged`, `pr_reviewer_seats_effective` |
| `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.yaml` (+ `.test.yaml`) | `CodexProjectionFailing`, `CodexProjectionStale`, `CodexPublisherDown`, `CodexPublisherMetricsMissing` |
| `kubernetes/apps/infrastructure/monitoring/ha-rules.yaml` (+ `.test.yaml`, the file that already scopes LiteLLM's availability) | `LiteLLMChatGPTAuthNotReady` |
| `kubernetes/apps/apps/ai/litellm-chatgpt-eso.yaml` (+ `kustomization.yaml`) | SA, CA cert, SecretStore, templated ExternalSecret |
| `kubernetes/apps/apps/ai/litellm.yaml` | env, volume, `gpt-6-astra-realjaynesage` route, checksum |
| `scripts/tests/integration/test_litellm_chatgpt_route_contract.py`, `.gitea/workflows/litellm-route-contract.yaml` | the device-flow guard and the route, in the pinned image |
| `kubernetes/apps/apps/dsh/settings.seed.yaml`, `deployment.yaml` | the named provider + its reconcile |
| `scripts/gen-reporting-dashboard.py`, `kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml` | seat d on the dashboard |
| `docs/decisions/0026-second-chatgpt-subscription-through-litellm.md`, `docs/runbooks/{dsh,dev-workers,openbao-recovery,openbao-estate-credentials}.md` | record |

## Verification

- **Unit / lint (CI shape):** `python -m unittest scripts.tests.test_dsh_codex_publisher scripts.tests.test_openbao_chatgpt_provision scripts.tests.test_openbao_dsh_provision scripts.tests.test_gen_litellm_consumers scripts.tests.test_litellm_platform_routes scripts.tests.test_dsh_pod_security scripts.tests.test_dsh_install_job scripts.tests.test_local_embeddings`;
  `scripts/gen-litellm-consumers.py --check`; `scripts/check-inline-hashes.py`; `bash scripts/manifest-lint.sh`
  and `scripts/rules-lint.sh` (CI); `ansible-playbook reviewers.yml --syntax-check` and
  `--check -l reviewer-2 -t seats,dsh-codex`; the offline chatgpt route contract in the pinned image (CI).
- **The real chain, before PR 2 merges:** once PR 1's Job is green, apply PR 2's
  `litellm-chatgpt-eso.yaml` by hand into ns `ai` from the branch (the same bytes Flux will adopt on
  merge): `litellm-store` must turn Ready (proves the `litellm-eso` → `af-app-litellm` → CA chain), and
  the rendered Secret's `auth.json` must parse as JSON with exactly `access_token`, `account_id`,
  `expires_at`, both strings equal to `unconfigured` against the seeded empty fields. This is the
  production template against the production document, not the engine probe.
- **PR 1 live:** Job log shows both policies "written and verified", the role written and read back,
  KV created with the five key names; the dsh publisher journal still reads "unchanged".
- **PR 2 live, before the login:** litellm pods Ready with `/chatgpt-auth/auth.json` present (keys
  checked, values not printed); a Responses call to `gpt-6-astra-realjaynesage` returns an auth error
  in **seconds** (no device flow, no device code in the litellm log); DSH `session/modelCatalog` lists
  `openai-codex-realjaynesage` in `routableProviders` with no failures; picking it yields an honest
  upstream error. reviewer-2: `id codexrun4` exists, `journalctl -u reviewbot | grep seats:` still shows
  `['a', 'b', 'c']`, publisher journal shows the seat-d skip line and DSH's projection unchanged;
  `dsh_codex_projection_ok{document="dsh/credentials"} == 1` on the textfile.
- **After the login:** publisher journal "updated" for `litellm/chatgpt` and
  `dsh_codex_projection_ok{document="litellm/chatgpt"} == 1`; the Secret's `auth.json` changes and
  BOTH litellm replicas' mounted files carry a JWT whose `exp` matches
  `dsh_codex_projection_token_expires_at_seconds` (decoded in-pod, token never printed); a Responses call
  completes; in DSH on the new provider: a turn with a tool call and its result fed back, a follow-up
  turn on the same session (encrypted reasoning round-trips), an effort change, an image attachment,
  and a cancelled stream leaving the session usable; Open WebUI: one chat-completions call to the
  route, result recorded (working, or an honest 4xx); after PR 3,
  `reviewbot_llm_seats_distinct{persona="codex"} == 4`.

## Risks and non-goals

- Using a ChatGPT subscription through a gateway is the same class of risk the operator accepted on
  2026-09-16 (rotation plan); this adds a consumer, not a new class. Recorded in the ADR, not re-raised.
- LiteLLM's chat-completions path for `chatgpt/gpt-6-astra` is unverified; only the Responses path has
  a consumer (DSH). The route is discoverable in Open WebUI like every other route on this proxy; the
  post-login check above records what a user gets there.
- `chatgpt/gpt-6-astra` is absent from LiteLLM's price map — cost logging warns, routing is unaffected.
- If seat d is never activated (PR 3) its token is not renewed, the publisher stops updating after ~7
  days, `CodexProjectionStale` fires at 24 h left, and LiteLLM fails fast with 401 — visible, not silent,
  and self-healing on activation.
- Not in scope: a second LiteLLM account (impossible in one process), DSH version bumps, moving seat b,
  a per-caller model allowlist on the proxy.

<!-- codex-review-status: complete -->