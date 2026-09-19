# A second ChatGPT subscription (realjaynesage) for GPT-6 Astra: LiteLLM `chatgpt/` route, a named DSH provider, reviewer-2 seat d

## Codex Review

- **Route isolation and access control gaps**: Open WebUI lacks model allowlists and will discover the unverified chat-completions path; exclusion from generators is not access control. Enforce model boundaries per caller identity.
- **Auth file robustness**: Device-flow guard assumes valid JSON with nonempty string token and numeric expiry; LiteLLM treats read/parse failures as absent credentials and enters device flow even on read-only mounts. Serialize safely and test malformed/unreadable/missing-key/placeholder/expired cases.
- **ESO and bootstrap schema**: Template validation probe exercises only the populated canary path, not missing keys or absent values. Test complete production template with incomplete fields and verify `ai` SA/role/CA chain. Existing-document recovery is not tested.
- **Credential publication visibility and recovery**: Absence skip applies to all projections; a deleted credential would leave the service successful while tokens age. Recovery incompletely specified—publisher lacks write permission on deleted docs. Clarify ownership and test ordered reconstruction.
- **Timeout and failure mode gaps**: Multi-projection execution with 90-second `TimeoutStartSec` and three-per-projection HTTP operations risks exceeding budget. Parameter loss in Responses transform is unrecorded. Recovery and rollback paths are incomplete.

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
   (`PROTOCOLS` in `lib/index.js`, `buildProvider`). A provider block with any other `api` value once
   invalidated the whole `providers` dict and took every model down (`deployment.yaml`, seed-settings).
2. **LiteLLM 1.101.0 (the pinned image) ships a `chatgpt/` provider** (`litellm/llms/chatgpt/`):
   Responses-native against `https://chatgpt.com/backend-api/codex`, auth from ONE process-global file
   `$CHATGPT_TOKEN_DIR/auth.json` (flat JSON: `access_token`, `refresh_token?`, `id_token?`,
   `expires_at`, `account_id`), re-read from disk on every call, one account per process (upstream
   issue #23777). **Hazard:** an access token that is missing, empty, or expired by the file's
   `expires_at` (else the JWT `exp`) with no `refresh_token` sends `Authenticator.get_access_token()` into
   the OAuth **device flow** — a synchronous `time.sleep` poll of up to 15 min — and `get_llm_provider()`
   calls it at router construction, so it would block a request AND a pod start. The provider prepends
   the Codex-CLI persona prompt to `instructions` unless `CHATGPT_DEFAULT_INSTRUCTIONS` is set; its
   Responses transform keeps only `model/input/instructions/stream/store/include/tools/tool_choice/
   reasoning/previous_response_id/truncation` (drops `max_output_tokens`, `prompt_cache_key`, `text`).
   The `ai` namespace has no egress policy on the litellm pod (only `text-embeddings` carries one).
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
   on a referenced key that is absent, and sprig `default` is available. The `apps` Flux Kustomization
   is `wait: true`, so a never-Ready ExternalSecret wedges the layer (2026-09-10).
6. The device-auth login is a **human ceremony** (sign in as realjaynesage@gmail.com in a browser);
   it cannot be automated from here.

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
  today), DESIRED = the heredoc. Any other document refuses before `policy write`.
- **Writes k8s auth role `af-app-litellm`** (`bound_service_account_names=litellm-eso`,
  `bound_service_account_namespaces=ai`, `token_policies=af-app-litellm`, `token_ttl=1h`,
  `alias_name_source=serviceaccount_uid` — the shape `af-app-dsh` has live). <!-- codex: Idempotent role rewrite does not protect against drift on ServiceAccount bindings; recurring Job can overwrite emergency restrictions. Define ownership and test downstream authentication for litellm-eso plus rejection of other identities. --> Written, not merely
  asserted as the dsh Job does, because no ceremony ever created this one; an identical rewrite is
  idempotent and the values are all in this file.
- **Creates `af/litellm/chatgpt` ONCE** (`-cas=0`) carrying EVERY key the ESO template will reference,
  empty: `CHATGPT_ACCESS_TOKEN`, `CHATGPT_ACCOUNT_ID`, `CHATGPT_ACCOUNT_EMAIL`, `CHATGPT_EXPIRES_AT`,
  plus `CHATGPT_OPENBAO_CANARY=provisioned`. Never touches it again — the publisher owns the contents.
- Tests: `scripts/tests/test_openbao_chatgpt_provision.py` + a fixture `bao` shim with per-policy state,
  mirroring `test_openbao_dsh_provision.py` (self-wedge invariant for BOTH policies, baseline == live,
  no write on drift/unreadable/list-failure, role written with the policy, KV created once, soft-delete
  loud). `test_openbao_dsh_provision.py` keeps passing (that Job is untouched).
- Verify live: Job `Succeeded`; `bao policy read af-app-litellm` / `dsh-codex-publisher` match;
  `bao kv list -mount=af litellm/chatgpt` lists the keys (values never printed); <!-- codex: Plain `bao kv get` displays values; use `list` or boolean validation whose output cannot contain tokens. --> `dsh-codex-publisher`
  keeps publishing ("unchanged" in its journal) — the existing projection is unaffected.

### PR 2 — publisher, reviewer staging, LiteLLM, DSH, monitoring, docs (after PR 1's Job is green)

- **Publisher generalised** (`ansible/roles/dsh_codex_publisher/files/publish.py`, `tasks/main.yml`,
  `scripts/tests/test_dsh_codex_publisher.py`): config becomes
  `{address, projections: [{auth_path, email, kv_path, prefix}]}`. Each projection publishes
  `<prefix>_ACCESS_TOKEN`, `<prefix>_ACCOUNT_ID`, `<prefix>_ACCOUNT_EMAIL`, `<prefix>_EXPIRES_AT` by CAS
  PATCH into its own document with its own state digest (the account id, a UUID JWT claim, is what
  LiteLLM's auth file wants beside the token; it is added to the DSH document too, for symmetry). 
  <!-- codex: Adding account_id to seat b solely for symmetry is unnecessary and increases scope. -->
  Rules per projection: wrong email / ≤5 min left → refused as today; an ABSENT auth file is a logged
  skip ("seat not logged in yet") so seat b keeps publishing while seat d waits; <!-- codex: Absence skip applies to both active and staged projections. A deleted credential leaves the service successful while tokens age. Make absence acceptable only for explicitly staged projections; required projections must fail while allowing others to continue. Test missing, malformed, wrong-account, and permission-denied sources independently. --> any other failure is
  logged and the run exits 1 after every projection ran. host_vars `reviewer-2.yml`: seat b →
  `dsh/credentials` prefix `DSH_CODEX`; seat d (`/home/codexrun4/.codex/auth.json`,
  `realjaynesage@gmail.com`) → `litellm/chatgpt` prefix `CHATGPT`.
- **reviewer-2 seat d, staged**: new role var `pr_reviewer_llm_seats_staged` (default `[]`) —
  provisioned like a seat (user, 0700 `~/.codex`, model pin, sudoers) but NOT rendered into
  `config.json`, so reviewbot never sees it. `pr_reviewer_seats_effective` = active list (or the legacy
  single seat) + staged. `host_vars/reviewer-2.yml`: `staged: [{name: d, sudo_user: codexrun4}]`.
- **ESO in ns `ai`** (`kubernetes/apps/apps/ai/litellm-chatgpt-eso.yaml`, the dsh pattern): SA
  `litellm-eso` (no token automount), Certificate `openbao-ca` → Secret `openbao-tls` (ClusterIssuer
  `ailab-ca`), SecretStore `litellm-store` (k8s auth role `af-app-litellm`), ExternalSecret
  `litellm-chatgpt-auth` (refresh 5m, `dataFrom.extract` `litellm/chatgpt`, template v2 rendering ONE
  key `auth.json` =
  `{"access_token": "<CHATGPT_ACCESS_TOKEN | default "unconfigured">", "account_id": "<CHATGPT_ACCOUNT_ID | default "unconfigured">", "expires_at": 4102444800}`). 
  <!-- codex: Template probe exercises only populated canary, not missing keys or absent values. Test complete production template with empty and missing fields. Verify the actual ai ServiceAccount/role/CA chain. The dsh-store probe cannot validate new auth paths. -->
  The far-future `expires_at` and the non-empty placeholder are the guard against finding 2: LiteLLM
  never enters the device flow — a stale or not-yet-published token fails FAST upstream (401) instead of
  freezing a worker or a rollout. <!-- codex: Guard assumes valid JSON with nonempty string token and numeric expiry; LiteLLM treats read/parse failures as absent credentials and enters device flow even on read-only mounts, affecting every model in the shared process. Serialize JSON safely and test malformed, unreadable, missing-key, placeholder, and expired-token cases against the pinned image before deploying. A startup validator alone does not protect later file reads. --> The real expiry stays visible as `CHATGPT_EXPIRES_AT` in OpenBao and
  as the seat's login on the reviewer dashboard once activated.
- **LiteLLM** (`kubernetes/apps/apps/ai/litellm.yaml`): env `CHATGPT_TOKEN_DIR=/chatgpt-auth`,
  `CHATGPT_DEFAULT_INSTRUCTIONS="You are a helpful assistant."` (DSH's own system prompt arrives as a
  developer message; the Codex-CLI persona text must not be prepended to it); Secret volume
  `litellm-chatgpt-auth` at `/chatgpt-auth`, readOnly, **`optional: false`** — a pod without the file is
  the device-flow path, so it must not start without it. `model_list` entry
  `gpt-6-astra-realjaynesage` → `chatgpt/gpt-6-astra`, `model_info: { mode: responses, supports_vision: true }`.
  A NEW model_name, not a second deployment of `gpt-6-astra`: least-busy across the paid API key and a
  subscription is not a policy anyone chose, and the DSH provider needs a name that maps to the
  subscription only. `mode: responses` also keeps it out of the generated Open WebUI / dsh-litellm lists
  (chat-completions through this provider is unverified for this model). <!-- codex: Open WebUI lacks model allowlists and uses the LiteLLM master key. Excluding the model from generated lists does not exclude it from /v1/models or prevent calls. This contradicts the stated non-goal and exposes the unverified chat-completions path. Define intended callers and enforce that boundary. Verify discovery and access using each consumer's credentials. --> Regenerate `checksum/config`
  with `scripts/gen-litellm-consumers.py --write`.
- **DSH** (`settings.seed.yaml`, `deployment.yaml`): provider `openai-codex-realjaynesage`, displayName
  `OpenAI Codex (realjaynesage)`, `api: openai-responses`, `baseURL: http://litellm.ai.svc.cluster.local:4000/v1`,
  `apiKeyEnv: LITELLM_API_KEY`, `transport: sse`, one model `gpt-6-astra-realjaynesage` (name
  `GPT-6 Astra`, `contextWindow: 272000`, `maxTokens: 128000`, `input: [text, image]`,
  `reasoningEfforts` mirroring the catalog's gpt-6-astra map, `"off"` quoted — YAML 1.1 reads a bare
  `off` as false). seed-settings gains `DSH_PROVIDER=openai-codex-realjaynesage node /seed/reconcile-provider.js`.
- **Monitoring**: `scripts/gen-reporting-dashboard.py` `SEAT_NAMES` += `d`; regenerate
  `reporting-dashboard.yaml`. <!-- codex: Reviewer expiry metrics cannot detect broken downstream publication. The reviewer can refresh successfully while publisher auth, ESO sync, or kubelet projection fails. The dashboard then shows healthy login while LiteLLM uses an expired token. Add per-projection failure/freshness signals and verify actual mounted JWT expiry on both replicas. Exercise rotation without pod restarts and downstream sync failure. --> Alert rules are seat-count agnostic (checked).
- **Docs**: ADR 0026 (why LiteLLM's provider and not a second native DSH route; the device-flow guard;
  the single-account limit; the subscription-use risk already accepted in the 2026-09-16 plan);
  `docs/runbooks/dsh.md` § Codex subscription rewritten for two seats / two documents;
  `docs/runbooks/dev-workers.md` seats table (c exists; d staged) and the staged-seat procedure;
  `docs/runbooks/openbao-recovery.md` path classes gain **PUBLISHER-OWNED** (`dsh/credentials.DSH_CODEX_*`,
  `litellm/chatgpt.*`: not seeded, restored by the publisher's next minute once its AppRole secret-id
  is re-minted); <!-- codex: Recovery and rollback are incomplete. Re-minting a secret-id alone cannot restore a wiped AppRole, policy, or missing KV document; the publisher has PATCH-only permission and refuses deleted documents. Document ordered reconstruction and test it. Specify rollback: removing the DSH provider from Git does not remove its persisted PVC block, and reverting the Job does not undo its OpenBao writes. Existing DSH_PROVIDER_REMOVE=1 support provides the removal mechanism. --> `docs/runbooks/openbao-estate-credentials.md` access paragraph (a second ESO store,
  `af-app-litellm`, reads one non-estate path).
- **Apply after merge** (WSL, `ANSIBLE_CONFIG`): `ansible-playbook reviewers.yml -l reviewer-2 -t seats,dsh-codex`
  → `codexrun4` exists, the publisher config carries both projections (seat d logged as skipped until
  the login). <!-- codex: Multi-projection execution with 90-second TimeoutStartSec and three HTTP operations per projection can exceed the timeout budget before all projections finish. Sharing one login also needs AppRole token TTL (60-second documented) consideration. Specify bounded per-projection execution, authentication lifetime, and service timeout together; test slow operations and immediate exceptions. --> Flux rolls litellm (2 replicas, maxUnavailable 0) and dsh.

### Operator ceremony (human, after PR 2)

```
ssh c4@192.168.0.25
rm -rf ~/.seat4 && mkdir -p ~/.seat4
setsid env HOME=/home/c4/.seat4 nohup /usr/bin/codex login --device-auth > ~/.seat4/login.log 2>&1 < /dev/null &
sleep 10 && cat ~/.seat4/login.log            # URL + one-time code; sign in as realjaynesage@gmail.com
sudo install -o codexrun4 -g codexrun4 -m 0600 /home/c4/.seat4/.codex/auth.json /home/codexrun4/.codex/auth.json
rm -rf /home/c4/.seat4                         # copies of one refresh-token family revoke each other
sudo -n -u codexrun4 HOME=/home/codexrun4 /usr/local/lib/reviewbot/codex-usage.py   # ok:true, email realjaynesage
```

<!-- codex: The ceremony can delete the only successful login after a failed copy. Sleeping ten seconds does not establish login completion, and unconditional cleanup follows install without checking success. The usage helper also exits zero on failure; its JSON must be inspected. Prefer logging in directly as the already-staged codexrun4 user. Otherwise pin CODEX_HOME and file credential storage, protect the scratch directory, wait for successful completion, and verify destination identity before deletion. Include the account's device-login prerequisite. -->

The publisher's next minute publishes to `af/litellm/chatgpt`; ESO follows within 5 min; LiteLLM reads
the file per request — nothing restarts.

### PR 3 — activation (after the login)

Move seat d from `pr_reviewer_llm_seats_staged` to `pr_reviewer_llm_seats` (hourly probe = token
refresh; sticky rotation a→b→c→d), `-t reviewbot`. Verify `reviewbot_llm_seats_distinct{persona="codex"} == 4`
and `reviewbot_llm_seat_info{seat="d",email="realjaynesage@gmail.com"}`.

## Critical files

| path | role |
|---|---|
| `kubernetes/apps/infrastructure/security/openbao/chatgpt-provision-job.yaml` (+ `kustomization.yaml`) | PR 1: policies `af-app-litellm` + `dsh-codex-publisher`, role, KV bootstrap |
| `scripts/tests/test_openbao_chatgpt_provision.py`, `scripts/tests/fixtures/openbao-chatgpt-provision/bao` | PR 1 gates |
| `ansible/roles/dsh_codex_publisher/{files/publish.py,tasks/main.yml}`, `files/policy.hcl` (removed) | multi-projection publisher |
| `scripts/tests/test_dsh_codex_publisher.py` | publisher contract (access-only, per-projection isolation, CAS) |
| `ansible/host_vars/reviewer-2.yml` | seat d staged; two projections |
| `ansible/roles/pr_reviewer/defaults/main.yml` | `pr_reviewer_llm_seats_staged`, `pr_reviewer_seats_effective` |
| `kubernetes/apps/apps/ai/litellm-chatgpt-eso.yaml` (+ `kustomization.yaml`) | SA, CA cert, SecretStore, templated ExternalSecret |
| `kubernetes/apps/apps/ai/litellm.yaml` | env, volume, `gpt-6-astra-realjaynesage` route, checksum |
| `kubernetes/apps/apps/dsh/settings.seed.yaml`, `deployment.yaml` | the named provider + its reconcile |
| `scripts/gen-reporting-dashboard.py`, `kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml` | seat d on the dashboard |
| `docs/decisions/0026-second-chatgpt-subscription-through-litellm.md`, `docs/runbooks/{dsh,dev-workers,openbao-recovery,openbao-estate-credentials}.md` | record |

## Verification

- **Unit / lint (CI shape):** `python -m unittest scripts.tests.test_dsh_codex_publisher scripts.tests.test_openbao_chatgpt_provision scripts.tests.test_openbao_dsh_provision scripts.tests.test_gen_litellm_consumers scripts.tests.test_litellm_platform_routes scripts.tests.test_dsh_pod_security scripts.tests.test_dsh_install_job scripts.tests.test_local_embeddings`;
  `scripts/gen-litellm-consumers.py --check`; `scripts/check-inline-hashes.py`; `bash scripts/manifest-lint.sh`;
  `ansible-playbook reviewers.yml --syntax-check` and `--check -l reviewer-2 -t seats,dsh-codex`.
- **ESO template probe (before PR 2 merges):** a throwaway ExternalSecret in ns `dsh` against the
  existing `dsh-store` rendering `{{ .DSH_OPENBAO_CANARY | default "x" }}` proves the v2 template +
  sprig `default` path on this ESO; deleted afterwards.
- **PR 1 live:** Job log shows both policies "written and verified", role written, KV created; the
  dsh publisher journal still reads "unchanged".
- **PR 2 live, before the login:** litellm pods Ready with `/chatgpt-auth/auth.json` whose keys are
  exactly `access_token, account_id, expires_at` and whose token is the placeholder; a Responses call to
  `gpt-6-astra-realjaynesage` returns an auth error in **seconds** (no device flow, no code in the
  litellm log); DSH `session/modelCatalog` lists `openai-codex-realjaynesage` in `routableProviders`
  with no failures; picking it yields an honest upstream error. reviewer-2: `id codexrun4` exists,
  `journalctl -u reviewbot | grep seats:` still shows `['a', 'b', 'c']`, publisher journal shows the
  seat-d skip line and DSH's projection unchanged.
- **After the login:** publisher journal "updated" for `litellm/chatgpt`; the Secret's `auth.json`
  changes; a Responses call completes; in DSH, one turn on the new provider with a tool call and an
  image attachment; after PR 3, `reviewbot_llm_seats_distinct{persona="codex"} == 4`.

## Risks and non-goals

- Using a ChatGPT subscription through a gateway is the same class of risk the operator accepted on
  2026-09-16 (rotation plan); this adds a consumer, not a new class. Recorded in the ADR, not re-raised.
- LiteLLM's chat-completions path for `chatgpt/gpt-6-astra` is unverified; only the Responses path has
  a consumer (DSH). Open WebUI does not get the route.
- `chatgpt/gpt-6-astra` is absent from LiteLLM's price map — cost logging warns, routing is unaffected.
- If seat d is never activated (PR 3) its token is not renewed, the publisher stops updating after ~7
  days, and LiteLLM fails fast with 401 — visible, not silent, and self-healing on activation.
- Not in scope: a second LiteLLM account (impossible in one process), DSH version bumps, moving seat b.
- **Known Responses parameter loss is recorded without acceptance decision.** Advertising `maxTokens` does not make an outgoing `max_output_tokens` effective through the Responses transform. Likewise, `text` formatting constraints and `prompt_cache_key` disappear. <!-- codex: Define which limitations are acceptable and how unsupported requests are surfaced. Extend verification to a complete tool-result round trip, subsequent conversation turns, reasoning settings, cancellation, and upstream 401/429/stream failures—not just catalog presence and one successful turn. -->

<!-- codex-review-status: complete -->
