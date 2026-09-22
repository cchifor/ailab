# Runbook: dsh (DeepSeek Harness)

An agentic coding UI at **<https://dsh.chifor.me>**, served from the `dsh` namespace and using the
estate's own models through LiteLLM. Manifests: `kubernetes/apps/apps/dsh/`.

**The defining constraint: this pod executes model-authored code.** The agent runs shell commands
and writes files, and a `danger-full-access` permission preset is selectable in its UI. Nearly every
design decision below follows from that, and several of them look strange until you hold it in mind.

---

## Topology

```
browser ──▶ Cloudflare Access ──▶ cloudflared ──▶ dsh Service :80
                                                      │
                                                 relay (TCP) :8080
                                                      │
                                                 dsh :3080 (loopback)
                                                      │
                             ┌────────────────────────┼────────────────────┐
                             ▼                        ▼                    ▼
                    LiteLLM (models)         searxng.dsh.svc:8080      CoreDNS
                                                      │
                                                 the internet
```

`dsh` can reach CoreDNS, LiteLLM, SearXNG, the Kubernetes API, and public IPv4 HTTP(S)
destinations. Private ranges remain excluded from *direct* web egress. A separate
`dsh-operator-ssh` NetworkPolicy allows TCP 22 to the explicitly approved operator hosts listed
below. Since 2026-09-18 those exclusions bound only the pod's direct connections, not what the
agent can reach: see the next section.

### Kubernetes access

The pod's ServiceAccount `dsh-k8s-admin` is bound to the built-in **`cluster-admin`** ClusterRole
(`k8s-admin.yaml`, ADR 0025), and `kubectl` v1.31.4 is on the agent's PATH, installed by the
`install-kubectl` init container. Every namespace, every resource, every verb — through the API
the agent can `exec` into any pod, `port-forward` to any Service, and read every Secret, which is
why the network exclusions above are a convention rather than a boundary. Verification, the
degraded-boot curl fallback, revocation and the history of the bounded design this replaced:
[`docs/runbooks/dsh-k8s-admin.md`](dsh-k8s-admin.md).

| Component | What it is |
|---|---|
| `dsh` Deployment | 2 containers: `dsh` (the app) and `relay` (a raw TCP proxy) |
| `relay` | `dsh web` binds loopback only and rejects `--host 0.0.0.0`; the relay is what makes it reachable from the pod network at all |
| `searxng` Deployment | self-hosted metasearch with its own restricted egress |
| `dsh-app` PVC | RWX nfs-csi — the npm-installed dsh tree |
| `dsh-home` PVC | RWO local-path — config, credentials, sessions |
| `dsh-workspace` PVC | RWO local-path — the agent's working directory |

> Both RWO volumes are `local-path`, which carries node affinity, so **the Deployment is pinned to
> one node** (currently talos-cp1). If that node is lost, dsh stays down until it returns. This
> predates the workspace volume; it is inherent to `dsh-home`.

---

## Getting in

dsh prints a **launch token** at startup and refuses everything else:

```
dsh web authentication required; reopen the URL printed by dsh web.
```

Two different secrets are involved, and conflating them wastes an afternoon:

| | lifetime | pinnable? |
|---|---|---|
| **launch token** (`?token=`) | `randomBytes` in an in-memory WeakMap — **changes every restart** | No. No `--token` flag, no env override |
| **cookie signing secret** | persisted in `$DSH_HOME/.credentials.yaml` | n/a — survives restarts |

A valid token on `GET /` mints a signed cookie; after that the token is not needed. Because the
signing secret persists, **cookies survive pod restarts** — only their own expiry ends them, and
`cookieMaxAgeDays` is set to 3650 in `cordis.patch.yml`, so this is once per browser rather than
monthly.

**To get a fresh login URL:**

```bash
# `deploy/dsh`, NOT a filtered pod list: searxng runs in this SAME namespace, is also Running
# and has no "install" in its name, so a `grep -v install | grep Running` selector matches BOTH
# and the resulting two-name $P breaks the very next command.
TOK=$(kubectl -n dsh logs deploy/dsh -c dsh | grep -oE 'token=[A-Za-z0-9_-]+' | head -1 | cut -d= -f2)
echo "https://dsh.chifor.me/?token=$TOK"
```

Cloudflare Access is the real per-person gate; this cookie is a second layer. Revoke by deleting the
`client-connection/browser-session` record from `.credentials.yaml`.

> `cloudflared` deliberately sets **no `httpHostHeader` override** for this hostname. dsh's `/api`
> trust fence refuses any request whose Host is not a declared authority, and the pod declares
> `--trusted-host dsh.chifor.me`. Rewriting Host would 403 every request.

---

## Git access to the forge

> **`dsh-team-conductor` lives at `cchifor/dsh-team-conductor` since 2026-09-18** (transferred by the
> operator from the restricted `dsh` user, which could not initiate a transfer itself: a restricted
> account cannot see the org). `reviewer-claude` and `reviewer-codex` have write. `dsh` has **admin**
> on it as a collaborator - operator decision, 2026-09-18: a write-only grant broke the agent's
> preflight, which reads `GET /branch_protections` (admin-only in Gitea; the non-admin read of the
> same facts is `GET /branches/main`, which the agent's token gets 200 on). The operator chose to
> restore admin rather than change the agent's check - a DSH-side change - so the agent was
> unblocked at once; switching that preflight to `/branches/main` and dropping admin again is the
> recorded follow-up. **Accepted with it, and without monitoring:** the agent *can* edit the
> protection it is gated by, and nothing detects that. What keeps merging with the reviewers is that
> protection's own configuration - `main`: status check `test / test (pull_request)`, 2 approvals,
> merge and approvals whitelists of `chifor`, `reviewer-claude` and `reviewer-codex`, stale approvals
> dismissed - and that `dsh` leaves it alone. (A drift assertion at converge time would need an
> admin-scoped credential: the hook-check token is `read:organization` only and cannot read
> protections; the reviewer PATs have write, not admin.)
>
> It is on the reviewers' allowlist and `dsh` is a merge author **for this repo only**, so a PR
> authored by the agent merges when both personas are clean and its own CI (`test.yml`, on the
> repo-scoped runner `dsh-conductor-ci-runner-1`, which survived the move) is green. Two things to
> hold in mind about that gate: **CI green is advisory for this author** - the workflow runs the
> tests and scripts the agent itself writes, so the two reviews are the operative control - and
> **a PR that edits `.gitea/workflows/**` is never approved by the bots**: the agent is an
> *unattended* author, the reviewers post such a change as a blocker finding, and a human must
> merge it. That guard covers the CI definition only, not everything the definition runs. Gitea
> answers the old path with a 301, git and API alike, so existing clones keep working - but point
> the agent's remote at the new path rather than living on the redirect.

The agent's shell can clone, fetch and push `https://git.chifor.me/...` repositories **without
being handed a token**, once the operator has provisioned one in OpenBao. Everything on the forge
is private (`REQUIRE_SIGNIN_VIEW` is on: even the API answers 403 anonymously), so without this an
"analyse repository X" request ends the way session `3d381759` did on 2026-09-11:

```
fatal: could not read Username for 'https://git.chifor.me': No such device or address
```

### How it is wired

```
OpenBao af/dsh/credentials {GITEA_USER, GITEA_PAT, ...}
   │  ExternalSecret dsh-credentials (dataFrom.extract, refresh 5m)
   ▼
Secret dsh-credentials  ──kubelet──▶  /dsh-credentials/GITEA_USER, /dsh-credentials/GITEA_PAT
                                            ▲ read at every `get`
/dsh-home/.gitconfig ── [credential "https://git.chifor.me"] helper = /dsh-home/.local/bin/git-credential-openbao
```

Two files in the `dsh-relay` ConfigMap, installed by `seed-settings` on every pod creation
(`git-credential-openbao.sh` → `/dsh-home/.local/bin/git-credential-openbao`, 0755;
`gitconfig.seed` → `/dsh-home/.gitconfig`). The helper reopens the mount **at use time**, so a
rotation reaches the next git command with no pod restart and no manifest change.

> **Why not the credentials plugin, an env var, or a bao agent.** `openbao-credentials.mjs`
> teaches dsh's own `credentials` *service* to read the same mount — that is what `apiKeyEnv:`
> resolves through. The agent's shell is a different world: `@deepseek-ai/dsh-subprocess` builds
> it with `scrubbedParentEnv()`, which drops every variable matching `/KEY|PASSWORD|SECRET|TOKEN/i`
> and every `DSH_*` variable before bash starts. So nothing the service resolves reaches git, and
> an env var never could. A bao agent in the pod is rejected at length in `openbao-eso.yaml` (a
> token in a pod that runs model-authored code). SSH is out: `gitea-ssh` is an in-cluster Service
> in the excluded `10.0.0.0/8`, and Cloudflare does not proxy it. HTTPS + helper is the only path.

### Provisioning (operator ceremony — vault and forge writes, not GitOps)

`dsh-provision-job.yaml` creates `af/dsh/credentials` with the canary and then **never touches the
contents**; the fields are operator-owned by design.

1. **Forge identity: a dedicated `dsh` Gitea account, not the shared `chifor` dev-worker PAT.**
   Separate revocation and audit trail, repository-level blast radius through collaborator/team
   grants, and no second home for the dev-worker secret (the rotation split-brain
   `openbao-estate-credentials.md` forbids). Mark it *restricted* if org-wide visibility is not
   wanted; grant **read** on the repos dsh should analyse (or a `dsh-readers` team); mint a PAT with
   `read:repository` (+ `read:organization` for org listing).

   > **The account's repo grants are the bound, not the PAT scope.** This forge runs Gitea
   > **1.26.1**; [GHSA-cc8w-r4qh-3v65](https://github.com/go-gitea/gitea/security/advisories/GHSA-cc8w-r4qh-3v65)
   > (CVSS 8.1, fixed in **1.26.2**) skips repository-scope enforcement on Git Smart HTTP when the
   > token arrives as a *Bearer* header. The helper speaks Basic, but the agent can read the PAT
   > from the mount and `curl` with Bearer, so on this version a read-scoped PAT on an account
   > that *could* write, would. An account holding only read grants has nothing to unlock.
   > **Follow-up:** upgrade the `gitea` app to ≥ 1.26.2.

   Enabling pushes later is **two** changes — write grants on the repos and a `write:repository`
   PAT — plus the vault patch below. Commit identity (`user.name`/`user.email`) is deliberately not
   seeded; set it with the same ceremony when write arrives.

2. **Vault write**, with the breakglass token inline (never exported, never echoed) — the ceremony
   in `openbao-estate-credentials.md`:

   ```bash
   # PAT in a 0600 file with NO trailing newline (printf '%s' "$PAT" > file, not echo). `@file`
   # keeps the secret out of the argument list; the guard stops an empty file becoming an empty field.
   [ -s /path/to/pat ] || { echo "empty pat file" >&2; exit 1; }
   BAO_TOKEN="$(kubectl --context admin@ai -n openbao get secret openbao-breakglass-token -o jsonpath='{.data.root_token}' | base64 -d)" \
     bao kv patch -mount=af dsh/credentials GITEA_USER=dsh GITEA_PAT=@/path/to/pat
   ```

   `patch`, not `put` — `put` drops the canary and every other field. (A trailing newline in the
   file is tolerated by the helper; an embedded one, or any whitespace, is refused.)

   **As executed on 2026-09-11.** The shared dev-worker PAT is not admin-capable (scope
   `read:organization,write:issue,write:repository`; `/admin/*` answers 403 naming the missing
   `read:admin`/`write:admin`), and human accounts are Authelia/OIDC with no local password, so the
   account was created by API with `gitea_admin` HTTP Basic auth, the password read from the
   SOPS-managed Secret `gitea/gitea-admin` into a 0600 curl config (the same route
   `agentforge-platform-activation.md` uses to mint bot tokens; OpenBao holds no Gitea admin
   credential). `POST /api/v1/admin/users` with `restricted: true`, `visibility: private`,
   `must_change_password: false`, `send_notify: false`; then `PATCH /api/v1/admin/users/dsh` for
   `max_repo_creation: 0` and `allow_create_organization: false` — that endpoint **requires
   `login_name` (and `source_id`) in the body or answers 422 `[LoginName]: Required`**. The token was
   minted *as dsh* (`POST /api/v1/users/dsh/tokens`, Basic auth from a 0600 file) with scope
   `read:repository` only: `read:organization` is inert for a non-member of the private org.
   Collaborator grants went through the shared PAT (repo admin suffices). Proven before the vault
   write: `info/refs?service=git-upload-pack` 200 on `platform`, `permissions: pull only` on the
   three repos, 404 on another private repo, 403 on an issue create.

3. **Verify, polling rather than trusting intervals** — 5m is ESO's refresh *period*, and kubelet's
   Secret-volume sync is "sync period plus cache propagation"; neither is a deadline:

   ```bash
   # Each step names its own failure: a loop that runs out of retries says so and exits non-zero,
   # rather than ending on a successful `sleep`. Key NAMES only, never values.
   # ONE process reads stdin and succeeds only when it has seen both names. Two chained `grep -q`
   # calls do not work here: the first one drains the (small) input and the second reads EOF.
   both() { awk '/GITEA_USER/ { u = 1 } /GITEA_PAT/ { p = 1 } END { exit !(u && p) }'; }
   # a. the Secret gains BOTH keys (ESO)
   ok=; for i in $(seq 1 80); do
     kubectl -n dsh get secret dsh-credentials -o jsonpath='{.data}' | both && { ok=1; break; }; sleep 5
   done; [ -n "$ok" ] || { echo "ESO did not sync both fields within ~7 min: kubectl -n dsh describe externalsecret dsh-credentials" >&2; exit 1; }
   # b. the mount gains BOTH files (kubelet)
   ok=; for i in $(seq 1 40); do
     kubectl -n dsh exec deploy/dsh -c dsh -- ls /dsh-credentials | both && { ok=1; break; }; sleep 5
   done; [ -n "$ok" ] || { echo "kubelet did not project both files within ~3.5 min" >&2; exit 1; }
   # c. git, in a dsh session (the Landlock-confined shell is the thing under test, not kubectl
   #    exec). No pipe after git: a pipe would replace git's exit status with the reader's.
   git ls-remote https://git.chifor.me/cchifor/platform.git > /dev/null && echo "forge auth OK"
   ```

   `both` matches the key names in both shapes (`"GITEA_USER":"..."` in the Secret's `.data`, a bare
   filename in `ls` output); nothing prints a value.

**Rotation:** mint the new PAT, `bao kv patch` it in, then prove the **new** value is what the pod
holds before revoking the old one — step (c) alone proves only that *some* valid PAT is mounted,
and until ESO and kubelet have both propagated, that is still the old one. Compare digests, which
prints neither value:

```bash
# d. the mounted bytes are the bytes you patched in (identical digests; the local file is the same
#    one `@file` read, so a trailing newline, if any, is on both sides)
l=$(sha256sum < /path/to/pat | cut -c1-64); [ ${#l} -eq 64 ] || { echo "cannot hash the local pat file" >&2; exit 1; }
m=; for i in $(seq 1 80); do
  m=$(kubectl -n dsh exec deploy/dsh -c dsh -- sh -c 'sha256sum < /dsh-credentials/GITEA_PAT' 2>/dev/null | cut -c1-64)
  # a FAILED hash (exec error, file missing) is an empty string, which must never compare equal
  [ ${#m} -eq 64 ] && [ "$m" = "$l" ] && { echo "mounted PAT is the new one"; break; }; sleep 5
done
[ ${#m} -eq 64 ] && [ "$m" = "$l" ] || { echo "mounted PAT is STILL the old one (or unreadable) -- do not revoke" >&2; exit 1; }
```

Then (c) from a session — which now proves the **new** PAT authenticates, since (d) proved it is
the one mounted — and only then revoke the old PAT at the forge. If (d) times out, ESO or kubelet
has not propagated yet: nothing has been revoked, so wait and re-run (d). Nothing restarts.

### Diagnosing "could not read Username"

That line alone says only that git obtained no username. What accompanies it decides the cause:

| also printed | meaning | fix |
|---|---|---|
| `git-credential-openbao: no GITEA_USER/GITEA_PAT under /dsh-credentials ...` | helper ran; document not provisioned | step 2 above |
| `git-credential-openbao: GITEA_PAT is empty or not a single ASCII word ...` (or `cannot read`) | provisioned, but the value is wrapped, NUL-containing, non-ASCII, empty or unreadable | re-patch with `@file` from a file holding exactly the token (one trailing newline is tolerated) |
| nothing | git never reached the helper | in the container: `git config --global -l` must show `credential.https://git.chifor.me.helper=/dsh-home/.local/bin/git-credential-openbao` and that path must be `-rwxr-xr-x`; check the `seed-settings` log |

`scripts/tests/test_dsh_git_credential_helper.py` runs the real helper against a kubelet-shaped
fixture (including a `..data` symlink swap for rotation) and fills through the seeded gitconfig
with git itself, isolated from the host's configuration.

---

## Codex subscriptions: the reviewer seats behind dsh and LiteLLM

Two ChatGPT subscriptions serve `gpt-6-astra` to dsh, and both are reviewer-2 seats whose OAuth
refresh happens ONLY on that host (ADR 0024 for the seats, ADR 0026 for the second route). Local
Qwen routes keep using LiteLLM's ordinary routes and are not part of this.

| seat | user / account | document (fields) | consumer |
|---|---|---|---|
| `b` | `codexrun2` / `realjaysage@gmail.com` | `af/dsh/credentials` (`DSH_CODEX_*`) | dsh's native `openai-codex` provider, **OpenAI Codex (realjaysage)** — unchanged since 2026-09-17 |
| `d` | `codexrun4` / `realjaynesage@gmail.com` | `af/litellm/chatgpt` (`CHATGPT_*`) | LiteLLM's `chatgpt/` provider, route `gpt-6-astra-realjaynesage` → dsh's `openai-codex-realjaynesage` provider, **OpenAI Codex (realjaynesage)** — logged in and activated 2026-09-19 |

**Seat b → dsh, natively.** The provider key is `openai-codex`, with
`apiKeyEnv: DSH_CODEX_ACCESS_TOKEN` and no explicit `api`: the installed catalog selects the Codex
subscription protocol. This replaced the model picker's legacy LiteLLM/API-key GPT-6 route; the two
sessions that had selected `litellm/gpt-6-astra` were migrated through dsh's `session/selectModel`
API with their histories and the user's Qwen default preserved. ESO (`openbao-eso.yaml`,
ExternalSecret `dsh-credentials`, refresh 5m) projects the document's fields into
`/dsh-credentials`, and the credentials provider rereads the access token per request. Provider
reconciliation in `seed-settings` preserves the block across pod replacements.

**Seat d → LiteLLM → dsh.** It goes through LiteLLM because the installed dsh adapter cannot host a
second native Codex route: the subscription protocol is bound to the catalog id `openai-codex`, and
any other hand-declared `api` value fails the `Config` schema for EVERY provider (checked on
0.1.5-alpha.2, 0.1.5-rc.2, 0.1.6-alpha.2; ADR 0026). So LiteLLM's `chatgpt/` provider holds the
subscription — route `gpt-6-astra-realjaynesage` → `chatgpt/gpt-6-astra`, `mode: responses` in
`kubernetes/apps/apps/ai/litellm.yaml`, auth from `/chatgpt-auth/auth.json`
(`CHATGPT_TOKEN_DIR`), `CHATGPT_DEFAULT_INSTRUCTIONS` set so the Codex-CLI persona prompt is not
prepended to dsh's own system prompt — and dsh's provider `openai-codex-realjaynesage` is an
`api: openai-responses` route to `http://litellm.ai.svc.cluster.local:4000/v1` with
`apiKeyEnv: LITELLM_API_KEY` and the one model `gpt-6-astra-realjaynesage`, reconciled per boot by
`DSH_PROVIDER=openai-codex-realjaynesage node /seed/reconcile-provider.js` in `deployment.yaml`.
The file LiteLLM reads is rendered by ESO in ns `ai` (`kubernetes/apps/apps/ai/litellm-chatgpt-eso.yaml`:
ServiceAccount `litellm-eso`, SecretStore `litellm-store` on k8s-auth role `af-app-litellm`,
ExternalSecret `litellm-chatgpt-auth`, refresh 5m) into Secret `litellm-chatgpt-auth`, mounted
read-only with `optional: false` — a litellm pod without the file must not start. The template
renders ONE key, `auth.json`, with `default "unconfigured"` on the token and account id, `toJson`,
and `expires_at: 4102444800`: measured in the pinned image (2026-09-19), a missing, empty,
unparseable or file-expired token sends LiteLLM into the OAuth device flow (a synchronous poll of
`auth.openai.com` for up to 15 min, at Router construction), while the placeholder and the
far-future sentinel built the Router in 0.07 s with no network and failed FAST upstream. Never
remove any of the three from that template. The real expiry is `CHATGPT_EXPIRES_AT` in the vault
and `dsh_codex_projection_token_expires_at_seconds` on the textfile below.

> **Do not put a ChatGPT OAuth token in LiteLLM's `OPENAI_API_KEY` or send it to `api.openai.com`.**
> The `chatgpt/` provider — that mounted `auth.json` — is the ONLY LiteLLM home for a subscription
> token: it speaks to `chatgpt.com/backend-api/codex`, where the token is valid. An `openai/` route
> would present it to the paid API, where it is not.

**Refresh has one owner per seat:** the Codex CLI running as that seat's user on reviewer-2. Its
`/home/codexrunN/.codex/auth.json` retains the refresh token; nothing copies it. The CLI renews
the access token when the seat is run — reviews, or the hourly `codex app-server` usage probe,
which iterates only the seats in `pr_reviewer_llm_seats` (so a staged seat's token ages until it
is activated). If a seat needs a fresh login, fix it there; dsh and LiteLLM follow the next
publication and ESO refresh. Nothing switches to another account automatically.

**The publisher** (`ansible/roles/dsh_codex_publisher/`, enabled only on reviewer-2 by its host
vars; `dsh-codex-publisher.timer` every minute) publishes access-only projections. Its config,
`/etc/dsh-codex-publisher/config.json`, is `dsh_codex_publisher` from
`ansible/host_vars/reviewer-2.yml`, shaped
`{address, textfile, projections: [{auth_path, email, kv_path, prefix, optional}]}`. Each
projection reads its seat's `auth.json` and CAS-**PATCH**es four fields into its own document:
`<prefix>_ACCESS_TOKEN`, `<prefix>_ACCOUNT_ID` (added 2026-09-19 — the ChatGPT account id, a JWT
claim and not a secret, which LiteLLM's auth file wants beside the token; without it LiteLLM
derives it and then tries to WRITE the file to cache it, which the read-only mount refuses),
`<prefix>_ACCOUNT_EMAIL`, `<prefix>_EXPIRES_AT`. The PATCH preserves every unrelated field;
unchanged source credentials and an unchanged vault version produce no write. It never refreshes
OAuth and never publishes the refresh or id token. It refuses the wrong email, a token with five
minutes or less remaining, and a token without an account id.

**Optional vs required, and the exit-1 rule.** `optional: true` marks a STAGED seat (provisioned,
not logged in): an ABSENT auth file is a logged skip and nothing else. For a required projection
absence is a failure — "the service ran green while the token aged" is the silent failure the
publisher exists to prevent. An unreadable or unparseable file, a wrong account, an expiring
token and a vault error (HTTP code logged, never the body) are failures whether or not the
projection is optional. Every projection runs, each logs in on its own (the AppRole token lives
60 s and is never carried across projections; every HTTP operation has a 20 s timeout;
`TimeoutStartSec=150`), and the service exits 1 if ANY failed — visible in the journal shipped to
Loki and on the textfile.

**Metrics and alerts.** The publisher writes
`/var/lib/prometheus/node-exporter/dsh-codex-publisher.prom` atomically (temp + rename, beside
reviewbot's own textfile): `dsh_codex_projection_ok{document,email}` (1 published or confirmed
unchanged, 0 otherwise), `dsh_codex_projection_optional{document}`,
`dsh_codex_projection_token_expires_at_seconds{document}` (the `exp` of the last token it
published), `dsh_codex_projection_last_success_timestamp_seconds{document}` (carried across
failing runs from its state file) and `dsh_codex_publisher_last_run_timestamp_seconds`.
Five rules watch the chain. In `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.yaml`:
`CodexProjectionFailing` (a required projection with `ok == 0` for 30 m), `CodexProjectionStale`
(a published token with under 24 h left), `CodexPublisherDown` (the heartbeat
`dsh_codex_publisher_last_run_timestamp_seconds` 15 min old — a stopped timer leaves the previous
textfile scrapeable with `ok == 1`) and `CodexPublisherMetricsMissing` (no heartbeat series at
all). In `ha-rules.yaml`: `LiteLLMChatGPTAuthNotReady`, on ESO's own status of
`ai/litellm-chatgpt-auth`, because the publisher's metrics describe the OpenBao copy only.
Kubelet's projection into the mounted volume is the remaining blind spot (ADR 0026). A staged
seat (`optional: true`) never pages the first two.

**Policy and AppRole.** Since 2026-09-19 the policy `dsh-codex-publisher` is OWNED by
`kubernetes/apps/infrastructure/security/openbao/chatgpt-provision-job.yaml` — the role's
`files/policy.hcl` is gone: **patch** on `af/data/dsh/credentials` and `af/data/litellm/chatgpt`,
**read** on both metadata paths, no credential-value reads and no whole-document put/delete. ACLs
are per document, so patch permission covers every field in those two documents; the script's
allowlist restricts its writes to the four fields per prefix. Git owns the policy whole (`bao
policy write` replaces): a hand edit is reverted on the Job's next run, and the Job refuses to
run at all if the live document matches neither its reviewed baseline nor its desired form. The
AppRole itself stays an operator ceremony: create the same-named AppRole with
`token_policies=dsh-codex-publisher`, `token_ttl=60s`, `token_max_ttl=120s`,
`token_no_default_policy=true`, `bind_secret_id=true`, `secret_id_ttl=0`, and
`secret_id_num_uses=0`. Mint a role-id/secret-id pair and transfer it without terminal output into
`/etc/dsh-codex-publisher/approle.json` on reviewer-2, shaped as
`{"role_id":"...","secret_id":"..."}`, root-owned mode 0600. Run the `dsh-codex` tag of
`ansible/reviewers.yml` to install the publisher, CA, LAN hosts entries, config and timer. The
AppRole credential is independently revocable and is never projected into dsh or LiteLLM. After
a vault wipe the secret-id is dead and the documents are empty; the order that rebuilds them is
the PUBLISHER-OWNED row of `docs/runbooks/openbao-recovery.md`.

**Adding a seat that feeds a consumer** is the staged procedure in `docs/runbooks/dev-workers.md`
§ "Seats: the codex persona holds several subscriptions" — stage it, `-t seats,dsh-codex` with
its projection `optional: true`, log in directly as its user, verify, then activate. Seat d's
login was that ceremony (done 2026-09-19; kept here as the worked example):

**Operator ceremony (seat d login).** Prerequisite: `realjaynesage@gmail.com` is a ChatGPT account
with Codex access (device login is what the CLI offers a headless host). The login happens
**directly as the staged seat user** — no scratch HOME, no copy, nothing to delete, so no second
refresh-token family can ever exist:

```
ssh c4@192.168.0.25
sudo -n -u codexrun4 HOME=/home/codexrun4 setsid nohup /usr/bin/codex login --device-auth \
    > /tmp/seat-d-login.log 2>&1 < /dev/null &
sleep 10 && cat /tmp/seat-d-login.log          # URL + one-time code; sign in as realjaynesage@gmail.com
# then WAIT for the CLI to report success in that log before anything else:
tail -f /tmp/seat-d-login.log                  # "Successfully logged in" (or the CLI's equivalent)
sudo -n -u codexrun4 HOME=/home/codexrun4 /usr/local/lib/reviewbot/codex-usage.py
```

The last command exits 0 either way; read its JSON: `"ok": true` and
`"email": "realjaynesage@gmail.com"` are the check. The publisher's next minute publishes to
`af/litellm/chatgpt`; ESO follows within 5 min; LiteLLM reads the file per request — nothing
restarts. Activation (moving seat d into `pr_reviewer_llm_seats`, dropping `optional` from its
projection, `-t reviewbot,dsh-codex`) followed the same day (ADR 0026, PR 3): both projections
are now required, and seat d sits under the hourly probe and the `CodexProjection*` rules like
seat b. No seat is staged any more; `pr_reviewer_llm_seats_staged` is `[]`.

Check without showing tokens:

```bash
ssh c4@192.168.0.25 'sudo systemctl status dsh-codex-publisher.timer --no-pager'
ssh c4@192.168.0.25 'sudo journalctl -u dsh-codex-publisher.service -n 10 --no-pager'
kubectl --context admin@ai -n dsh get externalsecret dsh-credentials
kubectl --context admin@ai -n ai get externalsecret litellm-chatgpt-auth
# the textfile, through Prometheus (labels and numbers only, never a value):
kubectl --context admin@ai -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090
curl -s 'http://127.0.0.1:9090/api/v1/query?query=dsh_codex_projection_ok'
curl -s 'http://127.0.0.1:9090/api/v1/query?query=dsh_codex_projection_token_expires_at_seconds'
```

---

## Claude: none, by decision

dsh has **no Claude route** since 2026-09-22 (ADR 0031). Two were built and withdrawn the day
after each shipped: `anthropic-fable` on LiteLLM's metered key (ADR 0029, never answered — the
key is rejected by Anthropic) and `claude-cli`, the Claude Code CLI as a child process on the Max
subscription (ADR 0030, never authenticated). The deployment still runs
`DSH_PROVIDER=anthropic-fable DSH_PROVIDER_REMOVE=1` each boot to unwrite the PVC copy of the
provider block; that line is safe to keep. "LiteLLM is the ONLY model path" holds again. Adding
Claude back is a new ADR, and both old ones still describe the constraints accurately.

---

## Dedicated operator SSH

Provisioned on 2026-09-17 in the existing **`af/dsh/credentials`** KV-v2 document:

| Field | Purpose |
|---|---|
| `DSH_OPERATOR_SSH_KEY` | Dedicated Ed25519 private key, including its original line breaks |
| `DSH_OPERATOR_SSH_USER` | `dsh-operator` |
| `DSH_OPERATOR_SSH_KNOWN_HOSTS` | Verified host keys; trust provenance below |

The public-key fingerprint is `SHA256:JXbAxiKLrZeKEbqHOHFTNV1cAIsdJLPcrt5WVZNPHSs`.
The public key is authorized on **reviewer-1 (`192.168.0.24`)**, **reviewer-2
(`192.168.0.25`)**, and all eight active runner hosts, explicitly selected by the operator:

| Runner | Address |
|---|---|
| ci-runner-1 | `192.168.0.14` |
| ci-runner-2 | `192.168.0.15` |
| ci-runner-3 | `192.168.0.16` |
| ci-runner-4 | `192.168.0.17` |
| ci-runner-5 | `192.168.0.18` |
| ci-runner-6 | `192.168.0.19` |
| ci-runner-9 | `192.168.0.31` |
| ci-runner-10 | `192.168.0.23` |

These ten addresses are the only destinations allowed by `operator-ssh-networkpolicy.yaml`,
on TCP 22 only. Retired runners and hypervisor hosts are not included.

Host keys were obtained over SSH connections verified against the workstation's existing
known-host entries, except ci-runner-3: its saved entries were stale. Its current Ed25519 key
was independently verified through a trusted SSH connection to ai-node2, ai-node3's host key
from Proxmox cluster metadata, and the QEMU guest agent for VM 4103 on ai-node3. No unverified
key scan or disabled host-key checking was used. The workstation's global trust file was left
unchanged.

The operator explicitly authorized **passwordless sudo on all ten hosts** on 2026-09-17.
`/etc/sudoers.d/dsh-operator` is root-owned, mode 0440, and validated with `visudo`:

```sudoers
dsh-operator ALL=(ALL:ALL) NOPASSWD: ALL
```

This grants full administrative command access, including reviewer configuration and service
installation. The dedicated account has no supplementary groups. Its home and
`.ssh/authorized_keys` are root-owned; `/home/dsh-operator/work` is its writable working directory.
The authorized key uses OpenSSH's `restrict` option, disabling forwarding, PTYs and user rc files
for the normal SSH session; this is not a privilege boundary once sudo is granted. Use `sudo -n`
for noninteractive commands. SSH/sudo access alone does not install or register an additional
runner; its repository still needs to be specified for that separate operation. Do not reuse
`c4`, `ubuntu`, a hypervisor key, or a vault token as this identity.

ESO discovers these fields automatically. Patch the document with a version check (`bao kv patch
-cas=<current-version> -mount=af dsh/credentials ...`) and preserve every existing field. Neither
the OpenBao policy nor the Deployment needs a change. Force a refresh when needed:

```bash
kubectl --context admin@ai -n dsh annotate externalsecret dsh-credentials \
  force-sync="$(date +%s)" --overwrite
kubectl --context admin@ai -n dsh get externalsecret dsh-credentials
kubectl --context admin@ai -n dsh describe externalsecret dsh-credentials
```

Check `Ready=True`, a refresh after the write, and eventual file projection under
`/dsh-credentials`. No pod restart is needed. DSH's subprocess environment strips `DSH_*`
variables, so shell commands must read the mounted files. For example, inside DSH:

```bash
(
  set -eu
  umask 077
  sshdir=$(mktemp -d)
  trap 'rm -f "$sshdir/key" "$sshdir/known_hosts"; rmdir "$sshdir"' EXIT
  cp /dsh-credentials/DSH_OPERATOR_SSH_KEY "$sshdir/key"
  cp /dsh-credentials/DSH_OPERATOR_SSH_KNOWN_HOSTS "$sshdir/known_hosts"
  ssh -F /dev/null -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$sshdir/known_hosts" -i "$sshdir/key" \
    "$(cat /dsh-credentials/DSH_OPERATOR_SSH_USER)@192.168.0.24" id
)
```

To revoke access, remove the dedicated public key and `/etc/sudoers.d/dsh-operator` on each
authorized host and terminate any existing sessions for this account, including privileged
processes it started. Because the account can become root, investigate any additional access
it created when revoking after a compromise. Removing a field from OpenBao alone does not revoke
a key that has already been read. For rotation, authorize the new public key first, patch OpenBao,
verify that the mounted credential authenticates, and then remove the previous public key.

---

## The conductor validator image pin on ci-runner-1

`ci-runner-1` (`192.168.0.14`) permanently runs one container that does nothing:
**`dsh-conductor-image-pin`** — `/bin/sleep` as UID 65532, `--network=none`, read-only root, all
capabilities dropped, 16 MiB memory (swap equal), 0.01 CPU, 4 PIDs, no mounts, no log driver. Its
only purpose is to keep the conductor's validation image
`sha256:318b8ae52ecf3656a602ba9edcc29d2130728a5f2ea9ac5ece2658df77fda39d` referenced, so neither
runner cleanup on that host can prune it. IaC: `ansible/roles/dsh_validator_pin/` +
`ansible/dsh-validator-pin.yml` + `ansible/host_vars/ci-runner-1.yml`; unit tests in
`scripts/tests/test_dsh_validator_pin.py` (they run in the broker-inventory CI job).

**Why a sleeping container.** The conductor (`cchifor/dsh-team-conductor`, `ops/validation/`) runs
its test suite on this host through the operator SSH identity above: a root-owned helper
(`/usr/local/sbin/dsh-validation-helper`) and policy (`/etc/dsh-validation/policy.json`) start a
throwaway container from an image pinned by **local image ID**, which the policy refuses to
substitute with a tag. That image is **untagged and idle between validations**, and two things on
this shared host delete exactly that:

- `gitea-runner-cleanup.sh` (§3) runs `docker image prune -af --filter until=<24h>` on every
  sweep (timer, ~15 min) — an unused image older than a day is gone at the next tick. The
  previous pinned image went missing that way (rebuilt and re-archived 2026-09-18; the ceremony
  is `ops/validation/IMAGE-RECOVERY-EVIDENCE.md` in that repo).
- the co-located **GitHub** agent's `runner-reclaim.sh` (`ExecStartPre` of
  `actions.runner.cchifor-platform.service`, so every boot and every ephemeral cycle) does
  `docker rm -f` on **every** container and then `docker image prune -f`.

A running container makes the image "in use" for both prunes. The container itself is kept out of
both removal paths by two host-only variables in `host_vars/ci-runner-1.yml`, which extend the
fleet defaults with the exact space-prefixed name ` /dsh-conductor-image-pin$` (both scripts match
`<image> <name>`, case-insensitively): `gitea_runner_cleanup_infra_exclude_re` for the Gitea reap
and `github_runner_reclaim_keep_re` for the GitHub reclaim (empty fleet-wide, so the ephemeral
contract is unchanged everywhere else). The Gitea value is a literal copy of the role default plus
the pin — `test_dsh_validator_pin.py` fails CI if the default ever diverges from it.

**Reconciliation is a timer, not just boot.** `dsh-conductor-image-pin.service` is a plain
oneshot (no `RemainAfterExit`) run at boot and every 5 min by `dsh-conductor-image-pin.timer`: two
inspects when the pin is healthy; `docker start` if it was stopped; a checksum-verified restore
from the archive plus `docker run` if the image or the container is gone. Anything else on the
reserved name — wrong image, any isolation field off — is refused and left untouched, with the
failing field names in the journal. So `systemctl is-active` is NOT the health signal; the container
is (below).

**The helper, policy and image are NOT in ailab** — the agent installed them by hand under
`dsh-operator` sudo, and the conductor repo's own `IAC-ADOPTION.md` lists a proper `dsh_validation`
role as proposed work. This role codifies only the retention. Two consequences:

- `image-pin.py` restores a missing image from `/var/lib/dsh-validation/images/<id>.tar`
  (root-owned 0600, `docker save` output) only when the file's size and SHA-256 match the constants
  in the script, and refuses otherwise. **A rebuilt VM has no archive**, so on such a host the unit
  fails at every tick (`journalctl -u dsh-conductor-image-pin`: `Image retention refused: …`) until
  the image is rebuilt and re-archived on the dsh side; that is fail-closed and expected, not a
  bug in the role. Rebuilding the image changes its ID, and the archive with it: bump `IMAGE`,
  `ARCHIVE_SHA` and `ARCHIVE_SIZE` in the script in the same change that rebinds the host policy.
- The real fix is to push the image to `registry.chifor.me` and pin the host policy by registry
  digest, which makes local pruning harmless and survives a rebuild; it needs the helper to pull by
  digest, a dsh-side change. Until then this pin is the retention.

**Apply / verify / rollback** (from WSL, `ansible/` dir, `ANSIBLE_CONFIG` explicit — see
`ci-runners.md` § Troubleshooting for why):

```bash
ansible-playbook runners.yml -l ci-runner-1            # FIRST: renders runner-reclaim.sh with the keep-list
ansible-playbook dsh-validator-pin.yml                 # refuses any other host, and refuses until the line above ran
ssh ubuntu@192.168.0.14 'sudo docker ps --filter name=dsh-conductor-image-pin --format "{{.Names}} {{.Status}}";
  systemctl list-timers dsh-conductor-image-pin.timer --no-pager | head -2;
  sudo journalctl -u dsh-conductor-image-pin -n 1 --no-pager -o cat;
  grep INFRA_EXCLUDE /etc/gitea-runner-cleanup.env; grep "^KEEP_RE" /usr/local/bin/runner-reclaim.sh'
```

The pin play asserts the host, refuses while `/usr/local/bin/runner-reclaim.sh` lacks the
keep-list (the GitHub reclaim would otherwise undo it at its next cycle), installs the script and
units, runs the unit (so a squatter on the reserved name fails the play BEFORE any exemption is
written), then rewrites the one env line — refusing if the file holds anything but the fleet base
or the composed value, or more than one assignment of the key in any shell spelling. A second run is a no-op for the container (`"created": false,
"started": false` in the journal); the `restarted` task still reports changed because it is the
"reconcile now". The normal `just gitea-runners` / `just runners` renders keep both exemptions
through `host_vars/ci-runner-1.yml`. Rollback: `systemctl disable --now dsh-conductor-image-pin.timer
dsh-conductor-image-pin.service`, `docker rm -f dsh-conductor-image-pin`, delete
`host_vars/ci-runner-1.yml` and re-render both runner roles on the host; leave the archive and the
image alone.

---

## The image, and why it is what it is

**`node:24` since 2026-09-11 (Debian, same toolchain); the comparison below was measured on
`node:22` and is why it is Debian, not alpine and not slim.** Measured in-cluster:

| | alpine | node:22-slim | **node:22** |
|---|---|---|---|
| `bash` | ✗ | ✓ | ✓ |
| `python3` / `git` / `curl` | ✗ | ✗ | ✓ |
| `make` / `g++` | ✗ | ✗ | ✓ |

On alpine every Bash tool call failed with **`spawn bash ENOENT`** — dsh's shell backends spawn the
literal binary `bash`, and alpine ships only busybox `sh`. slim fixes that but leaves the agent with
no `python3`, `git` or `curl`. `make`/`g++` matter separately: they let npm build a native module
from source when no prebuilt matches.

### The install path is keyed on libc

```
/app/${DSH_VERSION}-${DSH_BUILD}      e.g. /app/0.1.2-rc.1-glibc
```

Moving musl → glibc invalidates a tree npm resolved for the other libc, and the installer's
`.installed` marker check is **per-directory** — without a distinct path it reports "already
installed" and the mismatch is silent. `DSH_BUILD` rides the same kustomize replacement as
`DSH_VERSION`, from the Job, so the Deployment cannot end up waiting on a path the installer never
creates.

**Bump `DSH_BUILD` on any base-image libc change.** The Job's name encodes it too, because a Job's
pod template is immutable — the `kustomize.toolkit.fluxcd.io/force` annotation is what lets Flux
replace it.

---

## The agent's workspace and what confines it

`workingDir: /workspace`, backed by its own PVC.

This matters because dsh derives the sandbox root from **`process.cwd()`**:

```yaml
sandbox-policy:
  workspaceRoot: !!js process.cwd()
  mode:          !!js process.env.DSH_PERMISSION_MODE ?? 'workspace-write'
```

The container's cwd used to be `/`, which made the sandbox root **the entire filesystem** — model
code could read `/dsh-home/.credentials.yaml` (the cookie signing secret) and rewrite `settings.yaml`
and the trust config.

**Two honest limits:**

1. This is a *policy* layer (landlock), not a Unix permission. A shell inside the container can
   still read those files; what changes is what dsh's own tools will do.
2. It does nothing under the **`danger-full-access`** preset, whose purpose is to lift the sandbox.

The pre-existing workspace record in `storages/workspace.json` still names `/dsh-home`. It was left
alone deliberately — it keys workspaces by UUID with attached session ids, and editing it can orphan
sessions. Use the UI's directory picker to move an existing workspace.

---

## Capabilities

`dsh --profile web --dump-config` prints the resolved plugin tree. Upstream ships **27 of 145 rows
disabled**; seven are enabled in `cordis.patch.yml`:

`compaction-basic`, `command-compact`, `agent-instructions`, `plan-mode`, `tool-todo`, `tool-skill`,
`skill-filesystem`

Compaction is the one that matters most — without it a long session simply fills the context window
and ends.

> **Every enabled row restates its FULL config.** A patch **replaces** a row's config rather than
> merging into it. Enabling a row with a bare `disabled: false` silently erases whatever config it
> had. `plan-mode`'s config is a 37-line prompt section; losing it leaves plan mode enabled but mute.
> The rows in `cordis.patch.yml` were generated from `--dump-config` output so they are byte-exact.

### Two patch forms, and the trap

```yaml
- id: existing-row        # OVERRIDE — only works if the row already exists
  config: { ... }

- insert:                 # INSERT — required for a NEW row
    - id: new-row
      name: './thing.mjs'
```

An override whose target does not exist is **skipped** with a single log line:

```
dsh: [.../cordis.patch.yml] patch: entry "searxng-web" not found
```

…and everything downstream referencing it fails at runtime. This shipped once and was caught only by
resolving the patch against the running instance.

---

## Web search

Live and working. `web_search` returns real results through SearXNG; **`web_fetch` is on since
2026-09-09** over the pod's own egress (private ranges excluded) — see below for why it was off.

```
dsh ──▶ searxng.dsh.svc:8080 ──▶ search engines
```

The provider is a **file**, `searxng-search.mjs`, shipped in the content-hashed ConfigMap and
installed by the init container beside `cordis.patch.yml`, referenced as `name: './searxng-search.mjs'`.

### Why a file and not an npm plugin

This is the single most expensive lesson in this runbook. `dsh-searxng-web` exists on npm, is
compatible, and **cannot be used here**:

dsh resolves a row's `name:` from the **profile directory**, not the dsh install tree. That
directory's `node_modules` is a symlink farm holding dsh's *own dependency closure* and nothing else
— verified against the running pod: 195 entries in `/app/0.1.2-rc.1-glibc/node_modules`, 193 in the
farm at `/dsh-home/profiles/node_modules`. Do not read 195 − 193 as the difference: the two sets are
**not nested**. Three entries are in the install tree and absent from the farm (`@emnapi`,
`@koromix`, `node-addon-require-builtin-linux-x64-gnu` — all native-addon plumbing dsh does not
depend on), and one, `react-dom`, is in the farm but not the install tree. 195 − 3 + 1 = 193. An npm-installed sibling therefore throws `MODULE_NOT_FOUND`, `boot()` rethrows,
and **the pod crash-loops**. It does not degrade to "search is broken"; the web UI goes down.

A relative specifier resolves against that same directory, so a file works. It also removes the
supply-chain question entirely: no third-party code in the process that runs model-authored tool
calls.

### Why `web_fetch` was off (until 2026-09-09)

SearXNG is a *metasearch* engine — it does not fetch arbitrary pages for a caller. dsh's built-in
`http` fetch provider does, but it fetches **from the dsh pod**, which at the time had no egress,
so every call would have failed. Advertising a tool that always fails wastes the model's turns
discovering it doesn't work, so `tool-web` shipped `fetch: false`. Since the 2026-09-09 egress
change (`networkpolicy.yaml`, public IPv4 HTTP(S) with private ranges excluded) it is `fetch: true`
(`cordis.patch.yml`); the paragraph below is kept for the reasoning.

Adding real fetch needs its own component; see `docs/plans/` if one exists, or the discussion on the
PR that introduced search.

### SearXNG tuning worth knowing

| setting | why |
|---|---|
| `search.formats: [html, json]` | **the whole point** — SearXNG serves HTML only by default and the JSON API 403s without this |
| `engines: google disabled: false` | upstream ships Google **disabled**; without this it is never queried |
| `enable_metrics: true` | with metrics off, `count_error()` returns immediately and `/stats/errors` answers `{}` — monitoring reports "no failures" forever |
| `startpage disabled: true` | behind a proof-of-work interstitial served as HTTP 200, so it fails as a JSON parse error, is **never suspended**, and is re-queried on every search |
| `suspended_times.SearxEngineCaptcha: 300` | a CAPTCHA otherwise takes an engine dark for an hour |
| `request_timeout: 5.0` | one hanging engine holds the entire response for this long |

`pool_maxsize` and `keepalive_expiry` were **removed upstream** by the curl_cffi migration; the
settings loader iterates its schema, not your keys, so stale options are ignored silently rather than
erroring.

**SearXNG will not boot** with the upstream placeholder secret (`server.secret_key is not changed`),
and its entrypoint owns `/etc/searxng` — it copies a template, `sed -i`s it and chowns the directory.
So the ConfigMap is seeded into a writable emptyDir by an init container that also generates the key.

---

## Diagnosing

### The agent says a tool is unavailable or a command fails oddly

Check the binary exists before anything else — this accounted for every reported fault once:

```bash
kubectl -n dsh exec deploy/dsh -c dsh -- sh -c 'for t in bash python3 git curl kubectl; do printf "%-8s %s\n" $t "$(command -v $t || echo MISSING)"; done'
```

`kubectl` MISSING is the documented degraded boot (the download at pod start failed): read
`kubectl --context admin@ai -n dsh logs deploy/dsh -c install-kubectl` and roll the pod. The other
four missing means the wrong image.

### Models disappeared from the picker

`settings.yaml` lives on the PVC and is **owned by dsh** (the Settings UI writes it). The
`reconcile-provider.js` init script rewrites only the `litellm` provider block from the seed on every
boot, preserving everything else. The `models:` rows of that seed block are not hand-written:
`scripts/gen-litellm-consumers.py` derives them (together with Open WebUI's `model_ids` and
litellm.yaml's `checksum/config`) from litellm.yaml's `model_list`, listing every route whose
`api_base` is a private IPv4 or a `.svc.cluster.local` name and whose `model_info.mode` is unset
or `chat`, unless the entry sets `model_info.hidden: true` (which drops it from dsh and from Open
WebUI's Local group only; Open WebUI's second connection still shows it under External), with
`input: [text, image]` on the routes that declare `supports_vision: true`. After editing
`model_list`, run `just af-gen-litellm` (`python3 scripts/gen-litellm-consumers.py --write` where
just is not installed) and commit the three files together — CI runs `just af-verify-litellm`'s
check and fails on drift. If model ids were renamed in LiteLLM and dsh still lists the old ones,
check that script's output:

```bash
kubectl -n dsh logs deploy/dsh -c seed-settings
```

### The "Internal Testing Notice" modal comes back on every reload

The modal (zh: 内测声明) is `WelcomeNotice` in `@deepseek-ai/dsh-client-ui-settings-models`. Its
acknowledgement persists only from a **loopback** page: the browser computes `isLoopback` from its
own page hostname, `ui-settings` therefore keeps every settings scope in memory mode on
`dsh.chifor.me`, and in memory mode `acknowledge()` sets a process-local flag only. Upstream states
the limitation outright ("Non-loopback pages get no durable settings", ui-settings README) and
there is no config field for it, so nothing per browser or per machine can fix it.

`cordis.patch.yml` therefore **disables the `ui-settings-models` row** -- the documented row
switch, the same form upstream uses for `ui-schedule`. That removes the notice at the source, and
with it the Settings > Models section and the DeepSeek first-run dialog, both inert for a remote
browser anyway ("settings are unavailable in this browser"). Providers are GitOps-owned
(`settings.seed.yaml` + `reconcile-provider.js`) and the `/model` picker is a different plugin, so
nothing usable is lost. The alternative -- making `dsh.chifor.me` count as loopback -- would make
the whole Settings UI writable from every browser past Access; it was declined on 2026-09-11, and
if it is ever adopted this row must go in the same change.

Check after the rollout, and **on every dsh version bump** -- an override whose row id vanished is
skipped with `patch: entry "ui-settings-models" not found` on stderr and the notice returns:

```bash
# 0 = the package is out of the served boot graph (it was 5 before the row was disabled)
kubectl -n dsh exec deploy/dsh -c dsh -- sh -c \
  "curl -s -H 'Host: dsh.chifor.me' http://127.0.0.1:8080/ | grep -c dsh-client-ui-settings-models"
# nothing here may name ui-settings-models
kubectl -n dsh logs deploy/dsh -c dsh | grep -i 'patch:'
```

Then open a NEW session in a private window: the coordinator only runs on a blank session, so that
is the state in which the modal used to appear.

### Pod stuck in `Init:0/4` with NO events

**The cause that hid for days, and how to tell it apart.** A dsh pod that sits in `Init:0/4` with
`PodReadyToStartContainers=False` and no pod events, while other pods on the same node start
normally, *may* be kubelet chowning the whole `/app` NFS volume. The `nfs.csi.k8s.io` CSIDriver is
registered with `fsGroupPolicy: File`, so a pod that sets `fsGroup` gets a recursive ownership walk
over every file of the volume after `NodePublishVolume` returns and before the sandbox is created.
The walk emits no pod event (kubelet logs a "still applying ownership" warning after 30 s, which
only `talosctl logs kubelet` shows), and `PodReadyToStartContainers=False` by itself only says the
sandbox is not up -- so corroborate before concluding:

```bash
# completed-walk totals for the NFS plugin on the node. The metric is recorded AFTER a walk
# returns, so it does NOT move while a pod is stalled: compare count/sum before the rollout and
# after the pod starts. On 2026-09-11 that delta was one apply of ~25 min; the day's average
# over 39 applies was ~6.6 min, growing with the file count under /app.
kubectl get --raw /api/v1/nodes/talos-cp1/proxy/metrics | grep 'volume_apply_access_control' | grep 'nfs.csi'
```

Both pod specs now set `fsGroupChangePolicy: OnRootMismatch`, which skips the walk when the volume
root already has gid 1000, the setgid bit and group `rwx`. Verify that precondition on the live
mount after the first rollout that follows the change -- it is what the skip depends on:

```bash
kubectl -n dsh exec deploy/dsh -c dsh -- stat -c '%A %U:%G %n' /app    # expect drwxrws... node:node
```

**Do not delete the pod while it walks** -- the replacement starts the walk from zero.

**Otherwise it is usually not stuck.** The init sequence mounts NFS and runs four init containers
(`fix-ownership`, `wait-for-install`, `seed-settings`, `install-kubectl` -- the last one is never
fatal and finishes in under a second on a normal boot, see `dsh-k8s-admin.md`); a
slow start looks identical to a stall in `kubectl get pods`. Read the pod *status* before acting -- a
healthy pod was once deleted for no reason because the summary column lagged:

```bash
kubectl -n dsh get pod <pod> -o jsonpath='{range .status.initContainerStatuses[*]}{.name}: {.state}{"\n"}{end}'
```

If `wait-for-install` is genuinely waiting, look at the install Job, which gates it via
`.installed`:

```bash
kubectl -n dsh get job
kubectl -n dsh logs job/dsh-install-<version>-<build>
```

### A team's delegation tool is absent (`subagent_codex` never registers)

The providers in `DSH_PLUGINS` reach the profile through the install Job's **staging** step, and
`seed-settings` projects whatever that step produced at pod creation. First read the two signals:

```bash
kubectl -n dsh logs deploy/dsh -c seed-settings | grep -E 'staging state|closure'
kubectl -n dsh logs job/dsh-install-0-1-5-alpha-2-glibc-ps1 | tail -5
```

`staging state: failed` with `/bin/sh: pnpm: not found` in the Job log was the 2026-09-11 shape:
the step ran `corepack enable pnpm` and a bare `pnpm add`, but as uid 1000 `corepack enable`
cannot write its shims into `/usr/local/bin` (true on node:22 and node:24 alike), so it only worked
where a shim already existed, and the node:24 image ships none. The Job now runs a **pinned** pnpm
through corepack (`corepack "$PNPM" add`, cache at `/app/.corepack`); `scripts/tests/test_dsh_install_job.py`
pins that shape. Two things to know when it recurs:

- The Job re-runs on its own only when Flux re-applies it (daily TTL) or its template changes; a
  fixed Job leaves the **running** pod without the closure until the next pod creation, because
  `seed-settings` projects at boot and nothing revisits a live pod. Roll the Deployment after the
  Job reports `ok`.
- Renovate bumps the `node` image under this Job without exercising staging. Anything the step needs
  must be pinned in the script, never inherited from the image.

### Web search returns nothing

```bash
# is SearXNG healthy?
kubectl -n dsh exec deploy/dsh -c dsh -- node -e 'fetch("http://searxng.dsh.svc.cluster.local:8080/healthz").then(r=>console.log(r.status))'

# which engines failed on the last query? unresponsive_engines is emitted unconditionally
kubectl -n dsh exec deploy/searxng -- python3 -c "..."   # or read /stats/errors, needs enable_metrics
```

Individual engines failing is **normal**. A live probe saw DuckDuckGo return a CAPTCHA and Startpage
malformed JSON, and the query still returned 32 results from the rest. That is metasearch working as
intended, not a fault.

### Verifying a config change before it merges

The highest-value habit in this runbook. Resolve the patch against the running instance with a
throwaway `DSH_HOME`, which is non-destructive:

```bash
# Same reason as the login snippet: address the deployment, never a filtered pod list.
kubectl -n dsh exec deploy/dsh -c dsh -- sh -c '
  mkdir -p /tmp/t/profiles/web
  cp /dsh-home/settings.yaml /tmp/t/settings.yaml
  cp /dsh-home/profiles/web/package.json /tmp/t/profiles/web/
  # copy your candidate cordis.patch.yml into /tmp/t/profiles/web/ first
  cd /app/*/ && DSH_HOME=/tmp/t ./node_modules/.bin/dsh --profile web --dump-config 2>&1 >/dev/null | head
'
```

Loader errors go to **stderr** and the process still exits 0, so a change that silently drops a row
looks like success unless you read stderr explicitly.
