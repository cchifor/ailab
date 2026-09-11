# Plan — give the dsh agent's shell a git identity for git.chifor.me, from OpenBao

**Date:** 2026-09-11 · **Status:** REVIEWED (codex round 1, §8) — implementing · **Base:** `c1cf50d` (origin/main)
**Repo:** `cchifor/ailab` · **Subject:** `kubernetes/apps/apps/dsh/`, `docs/runbooks/dsh.md`, `scripts/tests/`

---

## 0. The incident, exactly

Session `3d381759` (workspace `/dsh-home/platform`, 2026-09-11 13:18, model
`qwen3.8-27b-vllm-cloud`, preset `team-conductor`). The user asked dsh to analyse
`https://git.chifor.me/cchifor/platform`. The agent, in order:

1. `git clone https://git.chifor.me/cchifor/platform repo` →
   `fatal: could not read Username for 'https://git.chifor.me': No such device or address`
2. looked for `~/.gitconfig`, `~/.git-credentials`, `~/.ssh` → none exist
3. `web_fetch` of the repo URL → Gitea's sign-in page (`/user/login?redirect_to=…`)
4. `curl https://git.chifor.me/api/v1/repos/cchifor/platform` → `403 {"message":"Only signed in user is allowed to call APIs."}`
5. `env | grep -iE 'git|gitea|token|chifor'` → only `GIT_PAGER`
6. asked the user; the user answered "check the openbao agent"
7. the next model call hit `pi-ai stream idle timeout after 300000ms` and the session ended.

Step 7 is an inference problem on the cloud2 vLLM route and is **out of scope** here; it is
recorded so nobody re-diagnoses it as part of this one.

## 1. Why "the OpenBao agent" could not help — what #645 actually built

`feat(dsh): resolve credentials from OpenBao through the credentials seam` (#645, plus
`dsh-provision-job.yaml`) built this pipeline, and **all of it is live and healthy** as of today:

```
OpenBao af/dsh/credentials  ──ESO dataFrom.extract (5m)──▶  Secret dsh-credentials
        │                                                        │ kubelet volume (0440, fsGroup 1000)
        │                                                        ▼
        │                                              /dsh-credentials/<FIELD>   (one file per field)
        │                                                        │
        └── read by openbao-credentials.mjs, dsh's `credentials` SERVICE ◀──── settings.yaml `apiKeyEnv`,
                                                                              plugins calling ctx.credentials
```

Verified live: ExternalSecret `dsh-credentials` `Ready=True SecretSynced`, Secret carries exactly
one key, `DSH_OPENBAO_CANARY` (the provision Job's marker), and the mount shows that one file.

**The seam it feeds is dsh's in-process credential service** — the thing `apiKeyEnv:` references
and plugins resolve through. **It is not the agent's shell.** `git` runs in a Landlock-confined
`bash` spawned by `dsh-bash-local`, whose environment is `scrubbedParentEnv()` from
`@deepseek-ai/dsh-subprocess`:

```js
const SENSITIVE_ENV_PATTERN = /KEY|PASSWORD|SECRET|TOKEN/i;
// every process.env entry matching that, and every DSH_* entry, is dropped
```

plus `ENV_OVERRIDES` (`NO_COLOR`, `TERM=dumb`, `PAGER=cat`, `GIT_PAGER=cat`) and the
`shellEnv` registry's `DSH_HOME`/`DSH_SHELL`/`DSH_SESSION_ID`. So:

- nothing that #645 delivers is *visible to git* — no `~/.gitconfig`, no helper, no `.git-credentials`;
- an env-var route is closed twice over: `LITELLM_API_KEY` is (correctly) scrubbed, and any
  `*_TOKEN`/`*_KEY`/`*_PASSWORD`/`*_SECRET` name would be too;
- the field for a forge credential was never added to `af/dsh/credentials` in the first place —
  the document holds only the canary. The provision Job deliberately owns nothing but the canary
  ("every field after that belongs to the operator").

And the forge side: `git.chifor.me` is **not** behind Cloudflare Access (cloudflared.yaml says so,
precisely so git-over-HTTPS can use Gitea's own auth), it resolves to Cloudflare edge IPs, and the
dsh NetworkPolicy has allowed :443 egress to public IPv4 since 2026-09-09 — the sandboxed shell
reached Gitea and got an HTTP answer. `REQUIRE_SIGNIN_VIEW` is on, so even the API 403s
anonymously. Every one of `platform`, `cloudlab`, `ailab` is private on Gitea. **The only missing
piece is a credential that git can find.**

## 2. What to build

### 2.1 A git credential helper that reads the ESO mount, installed at boot

Two new files in the content-hashed `dsh-relay` ConfigMap, installed by `seed-settings` on every
boot exactly the way `AGENTS.md`, `cordis.patch.yml` and `openbao-credentials.mjs` already are:

| ConfigMap key | Installed to | mode | role |
|---|---|---|---|
| `git-credential-openbao.sh` | `/dsh-home/.local/bin/git-credential-openbao` | 0755 | git credential helper; on `get` prints `username=`/`password=` from `/dsh-credentials/GITEA_USER` and `/dsh-credentials/GITEA_PAT` |
| `gitconfig.seed` | `/dsh-home/.gitconfig` | 0644 | `[credential "https://git.chifor.me"] helper = /dsh-home/.local/bin/git-credential-openbao` |

Mechanics, each one checked against the running pod rather than assumed:

- `HOME=/dsh-home` in the dsh container, and `HOME` survives the scrub (it matches nothing in the
  pattern), so git reads `/dsh-home/.gitconfig`. The transcript's `~` expansions confirm it.
- The helper is named by **absolute path** in gitconfig (gitcredentials(7): an absolute path is
  run as-is with the operation appended), so it does not depend on `PATH` surviving anything.
- Landlock grants under `workspace-write` are `readOnly: ["/"]` plus `readWrite: [/dev/null, /tmp,
  <workspace>]` (`dsh-sandbox-local` `landlockProfileArgs`), and the sandboxed shell already
  executes `/usr/bin/git` and `/usr/bin/curl`. Reading `/dsh-credentials/*` and executing a file
  under `/dsh-home/.local/bin` are the same class of access. The mount is `0440` + fsGroup 1000 —
  the fix in `43b9199` — so uid 1000 can read it.
- The helper reads the file **at every `get`**, reopening by pathname, so a rotated PAT reaches
  the next `git` invocation once ESO (5m refresh *period*) and kubelet (sync period plus cache
  propagation; eventually consistent, no deadline) have propagated it — which is why the rotation
  ceremony in §3 proves the mounted bytes are the new ones before anything is revoked. **No pod
  restart** and no manifest change for rotation, which is the property #645 exists to provide.
- `store`, `erase` and any operation the helper does not know are accepted and ignored (exit 0):
  git calls `store` after a successful auth and `erase` after a 401, and gitcredentials(7) says
  unknown operations must be ignored so helpers survive future extensions.
- Absence is not an error: if either file is missing the helper prints nothing (git then fails
  exactly as it does today, "could not read Username") and writes ONE line to stderr naming the
  two fields and the OpenBao path, so the *next* agent that hits this reads the cause instead of
  guessing. Same "absence vs failure" discipline as `openbao-credentials.mjs`.
- Values are read with command substitution, which strips trailing newlines and nothing else — a
  PAT cannot legitimately end in one, and the credential protocol is line-based. Anything else
  that is not a single opaque word is **refused, not repaired**: an empty value, a value with
  embedded newline/CR/whitespace/control characters (a wrapped PAT would become extra protocol
  lines, which git 2.39 echoes in a warning), or a file that exists but cannot be read. Refusal
  prints ONE stderr line that never contains the value, emits nothing on stdout, and exits 0 so git
  falls through exactly as it does on absence.

### 2.2 Tell the agent

A short section in `agents.seed.md`: git over HTTPS to `git.chifor.me` authenticates itself; do
not hunt for tokens in the environment or ask the user for one. The diagnosis it teaches is tied
to the helper's own stderr line, not to git's generic "could not read Username" (which a missing
gitconfig, an unexecutable helper or an unreadable mount would also produce): if the helper
printed `git-credential-openbao: ...`, the operator has not provisioned (or has mis-provisioned)
`GITEA_USER`/`GITEA_PAT` and that line is what to report; if it printed nothing, check that
`~/.gitconfig` names the helper and that the helper is executable, without displaying file
contents. The REST API is **not** covered by this change (a helper answers git, not curl) and the
note says so, so the model does not spend turns on it.

### 2.3 Runbook + manifest comments

`docs/runbooks/dsh.md` gains "Git access to the forge": the topology above, the **operator
ceremony** (§3), how to verify, and the "why not" table (§4). `openbao-eso.yaml`'s discovery
ExternalSecret comment gets one line naming `GITEA_USER`/`GITEA_PAT` as the first real fields, so
the manifest and the vault agree on what the document is for.

### 2.4 Tests (risk-appropriate: this is config + a 30-line shell script)

`scripts/tests/test_dsh_git_credential_helper.py`, same shape as `test_dsh_credentials_plugin.py`:

- **wiring** — both ConfigMap keys present in `kustomization.yaml`; `seed-settings` installs both,
  the helper with `0755`; `gitconfig.seed` names the helper by the exact installed path and scopes
  it to `https://git.chifor.me`; `sh -n` on the helper (the embedded-shell test only sees
  manifests, not ConfigMap files).
- **behaviour** — run the real helper against a fixture directory shaped like kubelet's
  projection (`..data` symlink + per-key symlinks; `GIT_CREDENTIAL_OPENBAO_DIR` override, like
  `cred`'s `CRED_TOKEN_FILE`): `get` prints both lines verbatim; a trailing newline prints as one
  line; embedded newline/CR, whitespace, empty value, missing user, missing PAT, missing directory,
  unreadable file → empty stdout, exit 0, one stderr line that does not contain the secret;
  `store`/`erase`/unknown op → silent exit 0; **rotation** — swap the `..data` symlink to a new
  directory between two `get`s and the second answer is the new value.
- **end-to-end protocol through the SEEDED config** — `git credential fill` on this worker with
  `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=<gitconfig.seed with the helper path pointed at the
  fixture copy>`, `GIT_TERMINAL_PROMPT=0`: `https://git.chifor.me` yields username+password;
  another host and plain `http://git.chifor.me` yield nothing. Proves git parses what the helper
  emits AND that the shipped scoping holds. No network.

`test_dsh_embedded_shell.py` continues to `sh -n` the modified `seed-settings` program.

## 3. What the operator does (not in this PR — vault and forge writes)

The provision Job's contract says the document's fields are operator-owned; that is where the
identity choice lives, and it is a choice, so it is stated rather than made here:

1. **Forge identity — recommended: a dedicated `dsh` Gitea account**, not the shared `chifor`
   dev-worker PAT. Reasons: separate revocation and audit trail (`dsh` shows up in Gitea's log as
   itself); repo-level blast radius via collaborator/team grants rather than "everything chifor can
   see"; and copying the dev-worker PAT into `af/dsh/credentials` creates a second home for one
   secret, which is the rotation split-brain `openbao-estate-credentials.md` and the provision Job
   header both forbid. Mark the account *restricted* if org-wide visibility is not wanted. Grant
   read on the repos dsh should analyse (or a `dsh-readers` team). Mint a PAT with
   `read:repository` (add `read:organization` for org listing). Widening to pushes later is TWO
   changes — a `write:repository` PAT re-mint AND write grants on the repos (a PAT scope cannot
   exceed the account's own permission) — plus a vault patch; still no manifest change.

   **Why the account's repo-level READ grants are the bound, not the PAT scope.** The forge runs
   Gitea **1.26.1** (`/api/v1/version`, verified). GHSA-cc8w-r4qh-3v65 (CVSS 8.1, fixed in
   **1.26.2**): on Git Smart HTTP, repository-scope enforcement runs only for HTTP *Basic*
   credentials; a token presented as *Bearer* bypasses it and can fetch or push regardless of
   scope. The helper speaks Basic, but the agent can read the PAT from the mount and `curl` with
   Bearer, so on this version a read-scoped PAT on an account with write permission could push.
   A dedicated account holding only read grants has nothing for the bypass to unlock. Upgrading
   Gitea to ≥1.26.2 is a separate follow-up for the `gitea` app, recorded in the runbook.
2. **Vault write**, through the breakglass ceremony in `openbao-estate-credentials.md` (inline
   token, never exported, never echoed):
   ```bash
   # the PAT sits in a 0600 file with NO trailing newline (printf '%s', not echo); `@file` keeps it
   # out of the process argument list, and the guard stops an empty file becoming an empty field.
   [ -s /path/to/pat ] || { echo "empty pat file" >&2; exit 1; }
   BAO_TOKEN="$(kubectl --context admin@ai -n openbao get secret openbao-breakglass-token -o jsonpath='{.data.root_token}' | base64 -d)" \
     bao kv patch -mount=af dsh/credentials GITEA_USER=dsh GITEA_PAT=@/path/to/pat
   ```
   `patch`, not `put`: `put` would drop the canary and any other field. For a rotation, mint the
   new PAT, patch it in, confirm a clone from a session, and only then revoke the old one.
3. **Verify**, in order, polling rather than trusting intervals (5m is ESO's refresh *period* and
   kubelet's volume sync is "sync period plus cache propagation", neither is a deadline): the
   Secret gains both keys; the mount gains both files; in a dsh session
   `git ls-remote https://git.chifor.me/cchifor/platform.git` lists refs. The runbook carries the
   three commands with bounded waits.

## 4. Alternatives, and why not

| Option | Why not |
|---|---|
| **dsh-native: a `shellEnv` contributor resolving `GITEA_PAT` through `ctx.credentials` into `DSH_GITEA_…`** | Puts the secret into the shell *environment*, where `env` dumps it into the transcript (this very session ran `env \| grep -i token`). Also plugin code against the alpha `shellEnv`/`credentials` interfaces for a problem a 30-line helper solves with a stable git contract. Freshness is the same either way: both read the kubelet-propagated mount. |
| **Render `~/.git-credentials` at boot from the mount** | Pins the value at pod start; rotation needs a pod roll. The helper reads at use time. |
| **bao agent sidecar rendering `~/.git-credentials`** (the dev-worker idiom) | Rejected in `openbao-eso.yaml` twice, on stronger grounds each time: a token in a pod that runs model-authored code. ESO already delivers the value; nothing else is needed. |
| **SSH deploy key** | `gitea-ssh` is an in-cluster Service (10.0.0.0/8, excluded from egress) and Cloudflare does not proxy SSH. HTTPS is the only forge path this pod has. |
| **Env var `GITEA_PAT` in the Deployment** | Per-credential manifest edit (the thing #645 removed), and `PAT`-suffixed names only *happen* to dodge the scrub — an operator renaming it `GITEA_TOKEN` would silently lose it. |
| **Make repos public / paste a token in chat** | The agent's own suggestions; both defeat the point. |

## 5. Security accounting, stated plainly

- The agent can already `cat /dsh-credentials/*` — `deployment.yaml` says so and says the
  containment is the OpenBao ACL, not the filesystem. This change exposes nothing new; it makes
  git *use* what is there. What goes into `af/dsh/credentials` must therefore be a credential dsh
  is *meant* to hold, which is the argument for a dedicated, read-scoped forge identity in §3.
- The helper's Basic auth does not narrow what the agent can do with the PAT it can read (see
  §3.1 and the Gitea advisory); the account's grants do.
- `/dsh-home/.gitconfig` and the helper are writable by uid 1000 outside the sandbox and by a
  session whose workspace is `/dsh-home` (the legacy workspace record). Both are reinstalled on
  every pod creation, same trade as `AGENTS.md`. Tampering with them gains the agent nothing it
  cannot do by reading the mount directly.
- Commit identity (`user.name`/`user.email`) is deliberately not seeded: read scope cannot push,
  and when write scope arrives the identity should be the bot's, set with the same ceremony.

## 6. Acceptance

- `python3 -m unittest discover -s scripts/tests -p "test_dsh*.py"` green, including the new file.
- `bash scripts/manifest-lint.sh` green — that is the repo's gate (`kustomize build` v5.4.3 in
  docker, hard-fail, then `kubeconform -strict -ignore-missing-schemas`; the ESO CRDs have no
  schema there and are NOT validated by it, only rendered). `scripts/check-inline-hashes.py` green.
- On the live pod after merge (no vault write yet): `seed-settings` log shows both installs; `git
  config --global -l` in the dsh container shows the helper; a clone attempt fails as today **plus**
  the helper's one-line stderr hint. That is the correct pre-provisioning state.
- After the operator ceremony: `git ls-remote` succeeds from a dsh session with no pod restart.

## 7. Sequence

1. codex cross-review of this plan (read-only, inline, pinned to `c1cf50d`).
2. Implement §2 on `feat/dsh-forge-git-credentials`; tests first for the helper's behaviour.
3. Validate per §6 (local tiers); PR to Gitea; CI; codex diff review; operator merges.
4. Operator runs §3; verification commands from the runbook.

## 8. Cross-review, round 1 (codex `gpt-6-astra`, read-only, 2026-09-11)

Codex's sandbox could not exec in this environment, so it reviewed from the supplied live facts and
upstream docs; the pinned-source check moves to the final-diff review, which sees the real files.
Verdict: *"core diagnosis and mount-reading helper design are sound; fix the operational and
validation details."* Findings and what changed:

| # | finding | resolution |
|---|---|---|
| 1 | ceremony put the PAT in `bao`'s argv via `$(cat)`, and an empty file would write an empty field | §3.2 uses `GITEA_PAT=@file` with a non-empty guard |
| 2 | malformed values (embedded newline, empty, unreadable) under-specified; git 2.39 echoes malformed protocol lines | §2.1: refuse, one value-free stderr line, exit 0; tests for each |
| 3 | protocol test with `-c credential.helper` does not exercise the seeded host scoping; no rotation test | §2.4: fill through the seeded config with isolated `GIT_CONFIG_*`; `..data` symlink swap test |
| 4 | PAT scope cannot exceed account grants; Gitea ≤1.26.1 Bearer scope bypass (verified: forge is 1.26.1, advisory patched in 1.26.2) | §3.1 rewritten; upgrade follow-up recorded |
| 5 | AGENTS.md diagnosis over-certain | §2.2 ties it to the helper's stderr line |
| 6 | bare `kubeconform` cannot validate ESO CRDs | §6 names `scripts/manifest-lint.sh` and what it does not validate |
| 7 | propagation intervals stated as bounds | §3.3 polls with bounded waits; keep the old PAT until the new one works |
| 8 | unknown op → exit 2 contradicts gitcredentials(7) | §2.1: ignored, exit 0 |

## 9. Cross-review of the diff (codex, read-only, pinned `526a968`) and the PR reviewer bot

Both reviewers found the same gap independently: after `bao kv patch`, a successful `git ls-remote`
proves only that *some* valid PAT is mounted, so revoking the old one on that evidence can cut
access (persistently, if the new PAT is bad). Resolutions, all in the follow-up commit:

| # | finding | resolution |
|---|---|---|
| D1 / bot | rotation could revoke the PAT still mounted | runbook step (d): compare sha256 of the mounted file with the patched file (neither value printed), bounded poll, explicit refusal to proceed on timeout; then (c); then revoke |
| D2 | verification loops ended on `sleep` (exit 0 after exhausting retries), checked only `GITEA_PAT`, and `git ... \| head` hid git's exit status | loops check both fields and fail loudly on timeout; no pipe after git |
| D3 (nit) | dash's `$(...)` drops NUL bytes, so `abc<NUL>def` passed the single-word check as `abcdef` | bytes are judged before expansion (`tr -d '[:graph:]' \| wc -c`, one trailing newline tolerated); tests for NUL, double newline, lone newline, non-ASCII |
| R1 #7 | plan §2.1 still said "≤5m / ≤~1m" | reworded as periods, not bounds |
| reviewer-codex round 2 (important, on 8a0aee8) | runbook `both()` chained two `grep -q` on one stdin; the second read EOF | one `awk` process that succeeds only after seeing both names; proven against the JSON and `ls` shapes |
| codex delta (should-fix) | runbook digest compare: two FAILED hashes are two empty strings, which compare equal | both digests must be 64 chars before they may compare equal; local digest computed once, guarded |
| codex delta (nit) | validate-then-read through the key symlink: a kubelet swap in between emits unvalidated bytes | resolve each key once (`readlink -f`), validate and read that target; a vanished target is refused; tests pin it |
| reviewer-claude (nit, on 526a968) | helper drained stdin unread, so scoping relied on gitconfig alone | helper parses the request and answers only for `https` + `git.chifor.me` (silent otherwise); wiring test pins the helper's authority to the gitconfig section |

Round-1 dispositions from the diff review: #1, #3, #4, #5, #6, #8 resolved; #2 fully resolved by D3;
#7 by D1/D2.
