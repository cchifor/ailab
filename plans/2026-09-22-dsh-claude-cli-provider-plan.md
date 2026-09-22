# Claude on the Max subscription in dsh, through the unmodified Claude Code CLI

Operator request (2026-09-22): use the existing Claude Max subscription in dsh, without paying
extra, and *"I don't need the claude code harness. I just need to use claude model in my own dsh
harness."* The second half of that cannot be honoured as stated — see **Why this shape** — but the
first half can, at no cost beyond the subscription already being paid for.

Decision record: `docs/decisions/0030-claude-code-cli-subscription-in-dsh.md` (this PR).
**Relates to:** ADR 0029 (the Fable route on the metered key, and the subscription design it
rejected — this plan does *not* touch that route), ADR 0025 (the reviewer-1 Claude seats, whose
account this spends), ADR 0026 (the Codex subscription providers — the shape this mirrors).

## Context

dsh reaches models through LiteLLM and, since ADR 0026, through two subscription-backed providers
whose credentials are projected from OpenBao. Claude is available only on the metered
`ANTHROPIC_API_KEY` (`anthropic-fable`, `claude-sonnet-*`), which costs real money per turn —
$10/$50 per MTok for Fable, ~$1.50 for a 100K-in/10K-out turn (ADR 0029 finding 8).

ADR 0029 rejected putting a Max OAuth token behind that route. That reasoning stands and is not
revisited here: an adapter that speaks the Anthropic API directly with a subscription token has to
present itself as Claude Code to be served, which is circumvention.

**What is different is not the reasoning but the mechanism.** Anthropic's Claude Code legal page
draws the line at *who is making the request*:

> Nor does it prevent an end user from signing in to the unmodified Claude Code binary with their
> own Claude subscription, including where a platform hosts Claude Code as described under *Can
> customers offer Claude Code in their products?* above.

and the Help Center's Agent SDK page (banner dated June 15, after the May 13 announcement was
withdrawn):

> **Update June 15:** We're pausing the changes to Claude Agent SDK usage described below. For now,
> nothing has changed: Claude Agent SDK, `claude -p`, and third-party app usage still draw from your
> subscription's usage limits.

So `claude -p`, run as the published binary, on the token of the person who owns the subscription,
is ordinary use of Claude Code and draws on the plan. There is no extra charge and no second meter.
`claude setup-token` is Anthropic's own documented credential for exactly this case: *"For CI
pipelines, scripts, or other environments where interactive browser login isn't available."*

## Why this shape

The operator asked for the model without the harness. There is no compliant way to get that: the
subscription is licensed for Anthropic's own clients, and a third-party adapter reaching
`api.anthropic.com` with the same token is the design ADR 0029 rejected. The binary in the loop is
what makes this permitted, so it stays. It runs as a subprocess and nobody interacts with it; from
the dsh UI this is a provider in the model picker like any other.

## Approach

A dsh LLM provider, `claude-cli`, that spawns the local `claude` binary in print mode, reads its
NDJSON event stream and translates it into dsh `StreamChunk`s. Upstream is
[`Eyalm321/dsh-claude-cli-provider`](https://github.com/Eyalm321/dsh-claude-cli-provider) (MIT), at
commit `e149de694b18194a7439d0ab18bc53ec21be27f6`, 2026-09-10.

**Text-only reasoning tier** (operator's choice, 2026-09-22). Claude answers, plans and reviews; it
runs no tools — neither its own nor dsh's. See **What this does not do**.

### Seven pieces

| # | What | Where |
|---|---|---|
| 1 | The provider, vendored as three files | `kubernetes/apps/apps/dsh/claude-cli-{provider,translate,images}.mjs` |
| 2 | A wrapper that supplies the token and execs the pinned binary | `kubernetes/apps/apps/dsh/claude-cli.sh` |
| 3 | The pinned CLI, downloaded and checksum-verified | `install-job.yaml`, into `/app/tools/claude-code/<ver>/` |
| 4 | Install the wrapper + provider files at boot | `deployment.yaml` (`seed-settings`) |
| 5 | The provider row | `cordis.patch.yml` |
| 6 | ConfigMap files, Job rename, version replacement | `kustomization.yaml` |
| 7 | ADR, runbook, NetworkPolicy comment, wiring tests | `docs/`, `scripts/tests/` |

### 1. The provider — vendored files, not an npm package

`searxng-search.mjs` and `openbao-credentials.mjs` already establish this and the runbook calls it
"the single most expensive lesson": a row's `name:` resolves from the **profile directory**, whose
`node_modules` is a symlink farm of dsh's own closure. An npm-installed sibling throws
`MODULE_NOT_FOUND`, `boot()` rethrows, and a 1-replica Recreate Deployment crash-loops. It is not a
degraded feature, it is the web UI going down.

Measured on the live pod 2026-09-22: `@deepseek-ai/dsh-llm` — the only bare import these files need
— does resolve from `/dsh-home/profiles/web`, and its `LlmAdapter.prototype.prepareCall` exists
(the base method the adapter overrides).

Files land flat and keep upstream's content. **Three deviations, each marked in the header** so a
re-vendor can re-apply them:

* the two relative import specifiers, renamed with the files;
* a config key `images` (default `true`, upstream's behaviour). With `images: false` the catalog
  declares `inputModalities: ['text']` and the turn skips materialising attachments, appending one
  line saying they were not available. Upstream hardcodes `['text','image']` and writes the bytes to
  a temp directory for Claude to open with its Read tool — which in text-only mode does not exist,
  so the catalog would claim a capability the route cannot honour and the model would be told to
  read files it cannot open;
* nothing else. The credential handling stays in the wrapper (below), so the JavaScript in the
  process that runs model-authored tool calls carries no token logic at all.

### 2. The wrapper

`claude-cli.sh`, installed to `/dsh-home/.claude-cli/bin/claude-cli`, is the provider's `command`. It:

* reads `/dsh-credentials/DSH_CLAUDE_CODE_OAUTH_TOKEN` if present and exports
  `CLAUDE_CODE_OAUTH_TOKEN`; if absent, execs anyway and the CLI returns a clean
  `Not logged in · Please run /login` (measured, below) rather than hanging or crashing;
* **unsets** `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` rather than blanking them. Both outrank
  `CLAUDE_CODE_OAUTH_TOKEN` in the CLI's credential precedence, and the documentation is explicit
  that an empty value still counts as set in at least one precedence path. Unsetting removes the
  question. (Upstream blanks them; this is why that logic moved here.)
* sets `CLAUDE_CONFIG_DIR=/dsh-home/.claude-cli`, `DISABLE_AUTOUPDATER=1` (the binary lives on a
  read-only mount) and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`;
* never passes `--bare`: bare mode does not read `CLAUDE_CODE_OAUTH_TOKEN`, so it would silently
  de-authenticate the route.

This is the same shape as `ansible/roles/pr_reviewer/files/claude-seat.sh`, which has fed the
reviewer's seats a token through the environment since 2026-09-18.

**It confines nothing, and must not be described as if it does.** The dsh agent runs in this
container as the same uid and can already read `/dsh-credentials` directly — `deployment.yaml` says
so at the volume. The wrapper is a credential *path*, not a boundary; containment here is the
OpenBao ACL on the document.

### 3. The pinned CLI

Downloaded in the install Job (which has egress; the runtime pod's NetworkPolicy is not the place to
solve this) to `/app/tools/claude-code/<version>/claude`, outside the version-keyed prefix like
`uv`, so a `DSH_VERSION` bump does not re-download 217 MB.

Pinned to **2.1.267** (the `stable` dist-tag; Fable 5.1 needs ≥ 2.1.255) and verified against the
sha256 from Anthropic's own signed release manifest. Same non-fatal, verify-then-publish,
cache-on-sha shape as the existing `install-kubectl` container: three attempts, atomic publish, and
on any failure the route is simply absent rather than the harness being dead.

`/app` is mounted read-only in the dsh container, so model-authored code cannot alter the binary.

**Bumping it:** edit both pins together (version and sha256, from
`https://downloads.claude.ai/claude-code-releases/<ver>/manifest.json`) and the Job name.

### 4-6. Wiring

`seed-settings` installs the wrapper beside `git-credential-openbao` and the three provider files
beside `searxng-search.mjs`. All ride the `dsh-relay` ConfigMap's content hash, so editing any of
them rolls the pod — required, because the profile is installed at boot.

The provider row is an `insert:` (a new row; an override of an absent id is skipped with one stderr
line and everything downstream fails at runtime):

```yaml
- insert:
    - id: claude-cli-provider
      name: './claude-cli-provider.mjs'
      config:
        command: /dsh-home/.claude-cli/bin/claude-cli
        isolateTools: true
        images: false
        extraArgs: ['--disallowed-tools', '*']
        timeoutMs: 600000
        models: [...]
```

`--disallowed-tools '*'` is what makes this a text tier. `isolateTools: true` alone passes
`--strict-mcp-config --mcp-config '{"mcpServers":{}}'`, which removes MCP servers and **leaves every
built-in tool in place** — Bash, Read, Edit and the rest, inside this pod. The upstream README
describes that flag as stripping Claude's tooling; for MCP it does, for built-ins it does not, and
the difference is the whole security posture of this row.

`agent-default-model` is **not** changed. A tool-less model as the default would break the ordinary
agent loop; Claude is something you switch to in the picker for a question and switch away from to
act on the answer.

Models exposed: `claude-fable-5-1` (1M), `claude-opus-5` (200K), `claude-sonnet-5` (200K —
conservative; under-declaring only makes dsh compact sooner, over-declaring overflows the turn).

The Job's pod template is immutable, so its name changes with its contents
(`…-glibc-ps2` → `…-glibc-ps2-cc267`) and the three `replacements` in `kustomization.yaml` that
name it move with it. `DSH_PLUGINSET` stays `ps2`: the npm plugin set genuinely has not changed,
and bumping it would force a pointless re-stage of the codex closure.

### 7. The credential — one operator write, no new plumbing

Seat b (`clauderun2`) is `constantin.chifor@strive.us`, per `docs/runbooks/dev-workers.md` and the
*Claude seat capacity* dashboard panel. Its setup-token already exists in OpenBao at
`operator/broker/anthropic/claude-max-2/oauth`, field `CLAUDE_CODE_OAUTH_TOKEN`.

The operator copies that value into `af/dsh/credentials` as `DSH_CLAUDE_CODE_OAUTH_TOKEN`. Nothing
else is needed: the `dsh-credentials` ExternalSecret uses `dataFrom.extract` over that whole
document, so a new field appears in the pod at the next 5-minute refresh **with no change to this
repo** — which is exactly what that design exists for.

A copy is safe here for the reason ADR 0025 decision 2 already gives: a setup-token does not rotate,
so the refresh-token-family hazard that shaped the Codex design does not apply.

## Critical files

| Path | Role |
|---|---|
| `kubernetes/apps/apps/dsh/claude-cli-provider.mjs` | adapter: spawn, stream, catalog (new, vendored) |
| `kubernetes/apps/apps/dsh/claude-cli-translate.mjs` | pure event → StreamChunk translation (new, vendored) |
| `kubernetes/apps/apps/dsh/claude-cli-images.mjs` | attachment materialisation (new, vendored) |
| `kubernetes/apps/apps/dsh/claude-cli.sh` | credential + env wrapper around the pinned binary (new) |
| `kubernetes/apps/apps/dsh/install-job.yaml` | CLI download + checksum; Job renamed |
| `kubernetes/apps/apps/dsh/deployment.yaml` | install wrapper and provider files at boot; `CLAUDE_CODE_VERSION` |
| `kubernetes/apps/apps/dsh/cordis.patch.yml` | the provider row |
| `kubernetes/apps/apps/dsh/kustomization.yaml` | ConfigMap files, Job name, version replacement |
| `kubernetes/apps/apps/dsh/networkpolicy.yaml` | comment only: LiteLLM is no longer the only model path |
| `docs/decisions/0030-claude-code-cli-subscription-in-dsh.md` | the decision and its compliance basis |
| `docs/runbooks/dsh.md` | operating, bumping, diagnosing |
| `scripts/tests/test_dsh_claude_cli_provider.py` | wiring gates |

## Verification

### Already measured (live dsh pod, 2026-09-22)

1. **The pin is real and reachable.** `GET …/2.1.267/linux-x64/claude` from the pod: 217,013,744
   bytes, sha256 `0399c793…03c0` — byte-for-byte the value in Anthropic's signed manifest.
2. **It runs here.** `claude --version` → `2.1.267 (Claude Code)`; `claude doctor` → native,
   `linux-x64`, `Search: OK (bundled)`, `Auto-updates: disabled (set by env: DISABLE_AUTOUPDATER)`.
   glibc 2.36 / x86_64, so the non-musl build is correct.
3. **Unauthenticated degrades cleanly.** `claude -p ok --output-format json` with no credential
   returns a well-formed result with `is_error: true` and
   `result: "Not logged in · Please run /login"` — no crash, no hang. That is what dsh will surface
   until the OpenBao field exists.
4. **Module resolution.** `@deepseek-ai/dsh-llm` imports from `/dsh-home/profiles/web` and exposes
   `LlmAdapter`, `LlmError` and `LlmAdapter.prototype.prepareCall`.

### Before merge

5. `npm test` in a checkout of the vendored source (34 tests upstream) plus a case for the `images:
   false` deviation.
6. The runbook's non-destructive dry run: candidate `cordis.patch.yml` and the three files into a
   throwaway `DSH_HOME`, `dsh --profile web --dump-config`, **reading stderr** — a dropped row logs
   one line and still exits 0.
7. `--disallowed-tools '*'` parses: an unauthenticated run carrying the full argv still fails with
   `Not logged in`, not an option error.
8. `python3 -m unittest discover -s scripts/tests` and `kustomize build` clean.

### After merge, needs the operator's OpenBao write

9. One Fable turn through the dsh picker returns text and appears on seat b's usage.
10. A turn that would need a tool refuses in words instead of hanging.
11. `kubectl -n dsh logs job/dsh-install-…-cc267` shows the checksum matching, and a second run of
    the Job re-uses the cached binary without downloading.

## What this does not do

* **No tools on a Claude turn.** Not Claude's, not dsh's. The adapter never passes dsh's tool
  schemas to the CLI — `renderPrompt` flattens messages and the system prompt into one text prompt —
  and `--disallowed-tools '*'` removes the CLI's own. Ask Claude to edit a file and it will say it
  cannot.
* **No conversation resume.** `claude -p` is one-shot; history is replayed as a prompt each turn, so
  a long thread re-sends everything and spends limit faster than an incremental API route would.
* **Block-level streaming**, not token-level: text appears in paragraph-sized chunks.
* **It shares seat b's weekly window** with reviewer-1's claude persona, including the 50 % cap on
  Fable models that Max plans apply. Heavy dsh use will visibly reduce what the PR reviewer can do
  on that seat. The dashboard panel that shows this is *Claude seat capacity*.
* **It does not touch `anthropic-fable`.** That metered route stays exactly as ADR 0029 left it.

## Risks

* **The weekly window is the shared resource, and it is the one that will bite.** Nothing in this
  design bounds dsh's consumption; the only signals are the seat dashboard and the reviewer starting
  to park. Accepted for now — a bound would need a proxy this route deliberately does not have.
* **A CLI upgrade is a manual, two-pin edit.** Auto-update is off by design (read-only mount), so
  the pinned version ages. Fable 5.1 needs ≥ 2.1.255; a future model may need newer, and the failure
  is an error naming an unsupported model.
* **`--disallowed-tools '*'` is load-bearing** and its effect is invisible in the row's name. A test
  pins it.

<!-- codex-review-status: complete -->

---

## Review

**Codex was unavailable.** Both dispatch attempts (`plan-review` profile) were refused upstream with
`usage_limit_reached` before the first token; the CLI seat's weekly window is spent. Nothing in the
LiteLLM gateway logs shows the refusal, which is consistent with the codex CLI seat authenticating
on its own credential rather than through the gateway. `plans/2026-09-21-…-plan.md` hit the same
wall the day before and recorded the same workaround, so this is the estate's normal state this
week rather than a surprise.

**An independent reviewer stood in**, read-only, against the committed branch, with the same six
challenges Codex was given. Twenty findings. Dispositions, with the code that answered them:

| # | Finding | Disposition |
|---|---|---|
| 1 | The carve-out needs per-end-user auth; dsh cannot tell users apart | **Accepted.** ADR 0030 condition 3 now names the Cloudflare Access policy as the only thing carrying it, as an operator obligation. Escalated to the operator. |
| 2 | Deny flags were in the adapter's argv only; the agent could call the wrapper directly and get a fully-tooled CLI | **Accepted — the most serious finding.** Policy moved into `claude-cli.sh`; wrapper moved off PATH. |
| 3 | `--disallowed-tools '*'` may be inert (reviewbot saw `"LS" matches no known tool`); the measurement was unauthenticated | **Refuted by measurement, and the fix kept anyway.** Init-event tool counts: none 22, `--strict-mcp-config` 22, explicit deny list **14**, `'*'` 0, `--tools ""` 0. The wildcard works and the recommended explicit list is the weaker option. The route now sends both forms. |
| 4 | cwd is `/workspace`, so the agent can plant `.claude/settings.json` with `PreToolUse` hooks | **Accepted.** `cwd` moved outside `/workspace`; `--setting-sources ""` added. Verified against a planted hostile settings file. |
| 5 | The adapter hands the child the whole pod environment | **Accepted, and fixed harder than proposed.** The wrapper now applies an **allowlist**. The denylist written first was exercised with planted variables and `GITEA_PAT` walked through it. |
| 6 | Unhandled `EPIPE` on child stdin turns the degraded path into a crash | **Did not reproduce** (node 24, immediate child exit, 200 KB write, guarded and unguarded both survived). Listener added as insurance and labelled as unproven rather than as a fixed bug. |
| 7 | The credential is the broker's RESCUE-class token; ADR 0025 decision 2 was misapplied | **Half accepted.** The misapplied justification is withdrawn: the copy does not rest on ADR 0025 decision 2. The recommendation to mint a dedicated token was put to the operator and **overruled** (2026-09-22) — minting a second setup-token for one account is not documented to leave the first valid, and that unknown would take the whole `claude-max-2` broker fleet. The copy ships, with its blast radius stated in ADR 0030 decision 5 and the second location recorded beside the rescue step in `openbao-recovery.md`. |
| 8 | Seat-b attribution miscited to `dev-workers.md`, which names that address as a *Codex* seat | **Accepted.** ADR 0030 now cites the runtime dashboard label, says the email is deliberately not in git, and asks the operator to confirm ownership. |
| 9 | ADR 0029's blocker is broader than the line drawn; the new argument leans on a *paused* policy | **Accepted.** The legal page (permission) and the Help Center banner (billing) are now separated, and the ADR says plainly why the earlier reading was a summary of half the question. |
| 10 | "No repo change needed" leaves the credential inventory false | **Accepted.** Field documented in `openbao-eso.yaml`. |
| 11 | Plan mode is unexitable on a tool-less route | **Accepted.** ADR consequence + runbook. |
| 12 | No bound on concurrent 217 MB children against a 4 Gi limit | **Accepted as a recorded risk.** The adapter offers no cap; named in ADR consequences. |
| 13 | Unbounded CLI transcripts, and the runbook section the wrapper cited did not exist | **Accepted.** Seven-day prune in the wrapper; the runbook section now exists. |
| 14 | NetworkPolicy invariant left false | Already fixed in the working tree when reviewed. |
| 15 | Promised tests absent | Already written when reviewed; now 26, covering the new enforcement points. |
| 16 | `images` not coupled to the tool policy; `claude-cli-images.mjs` is dead weight | **Coupling accepted** (test added). Module **kept**: `images` defaults to upstream's behaviour, so dropping it would be a larger deviation and a harder re-vendor. |
| 17 | dsh's own usage is unobservable on a setup-token; the 50 % Fable cap is uncited | **Accepted.** Both corrected in ADR consequences, with the cap cited to its source and dated. |
| 18 | Plan out of step with the code | **Accepted.** This section and the corrections above. |
| 19 | Job rename is convention, not mechanism (`force: enabled`); stale `-ps1` in the runbook | **Accepted.** Both noted in the runbook; stale names fixed. |
| 20 | Unnamed window where a CLI-only bump leaves turns failing | **Accepted.** Documented in the runbook as the expected shape. |

Findings 1 and 7 need the operator, not code: who may pass Cloudflare Access, and minting the
dedicated token. Everything else is in the branch.

<!-- codex-review-status: finalized -->
