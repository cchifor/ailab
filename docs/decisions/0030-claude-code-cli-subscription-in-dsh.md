# ADR 0030 — Claude on the Max subscription in dsh, through the unmodified Claude Code CLI

**Status:** SUPERSEDED by ADR 0031 (2026-09-22) — the provider was removed; the findings below remain the record. Original status:
**Status:** ACCEPTED (2026-09-22), operator-directed: *"I'm paying the subscription… I just need to
use claude model in my own dsh harness"*, and, when the compliant options were put: *"Wire
dsh-claude-cli-provider"*. Design, measurements and the review trail in
`plans/2026-09-22-dsh-claude-cli-provider-plan.md`.
**Relates to:** **ADR 0029** (Fable on the metered key — this does not change that route, and does
not reopen its rejected design), ADR 0025 (the reviewer-1 Claude seats — this spends seat `b`'s
account), ADR 0026 (the Codex subscription providers — the credential shape this mirrors), ADR 0021
(credential tiering).

## Context

ADR 0029 asked whether dsh could serve Claude Fable on the estate's Max subscription and answered
no. Its reasoning was not about plumbing: an adapter that speaks the Anthropic API directly with a
subscription token is only served if it presents Claude Code's client identity, and manufacturing
that identity is circumvention. **That finding stands and is not revisited here.** Fable still
bills pay-as-you-go on `ANTHROPIC_API_KEY` at $10/$50 per MTok, ~$1.50 for a 100K-in/10K-out turn.

The operator asked again on 2026-09-22, ruling out extra spend, and — reasonably — asked for the
model without the harness. The second half cannot be honoured: what Anthropic licenses to a
subscription is use through its own clients, so removing the client is precisely what makes a
design non-compliant. What *can* be honoured is the first half, because the line Anthropic draws is
about **who makes the request**, not about which of your own programs benefits from the answer.

### What permits this shape

Three published statements, read together (checked 2026-09-22):

* **Claude Code legal and compliance**, having forbidden third-party developers from routing
  requests through Free/Pro/Max credentials on users' behalf or intermediating Claude.ai
  credentials, then says:

  > Nor does it prevent an end user from signing in to the unmodified Claude Code binary with their
  > own Claude subscription, including where a platform hosts Claude Code as described under *Can
  > customers offer Claude Code in their products?* above.

  and requires of such a platform that the binary not be modified and that **each end user
  authenticate with their own credentials** — no paying for, reselling or intermediating usage on
  anyone else's behalf.
* **Claude Code authentication** documents `claude setup-token` as the credential for exactly this
  situation — *"For CI pipelines, scripts, or other environments where interactive browser login
  isn't available"* — and states it requires a Pro, Max, Team or Enterprise plan.
* **Help Center, "Use the Claude Agent SDK with your Claude plan"**, banner dated June 15 2026,
  after the May 13 announcement of separate Agent SDK credits was withdrawn:

  > **Update June 15:** We're pausing the changes to Claude Agent SDK usage described below. For
  > now, nothing has changed: Claude Agent SDK, `claude -p`, and third-party app usage still draw
  > from your subscription's usage limits.

**These two texts do different jobs and must not be merged into one argument.** The legal page is
what makes the shape *permitted*: the published binary, the owner's own credential. The Help Center
banner says only that such use *draws on the plan's existing limits* rather than a separate credit
— it is about billing. Anthropic says it will give advance notice before changing that, so if the
pause ends, what changes is the cost of this route, not whether it is allowed.

So `claude -p`, run as the published binary, on the token of the person who owns the subscription,
on infrastructure that person operates for themselves, is ordinary use of Claude Code, and today it
costs nothing beyond the plan — which is what the operator asked for.

**ADR 0029 said the Consumer Terms forbid a Max token "in any other product, tool or service", and
that is broader than the line drawn here.** That sentence was written about the design in front of
it — an adapter making the API call itself — and did not consider the carve-out for the unmodified
binary, which is the case this ADR turns on. It is not that the terms changed between 2026-09-21
and 2026-09-22; it is that the earlier reading was a summary of the relevant half.

**The conditions this rests on, stated so a later change can be checked against them:**

1. The binary is Anthropic's, unmodified, checksum-verified against Anthropic's own signed release
   manifest. Nothing rewrites its identity, its system prompt or its headers.
2. The credential is the subscription owner's own setup-token. dsh does not collect it from anyone,
   does not offer anyone a Claude.ai login, and does not serve anyone else's requests on it.
3. **This deployment serves one person, and nothing in dsh enforces that.** The legal text requires
   each end user to authenticate with their own credentials; dsh carries no per-person identity at
   all — `networkpolicy.yaml` says so in its own words, that a dsh session cannot tell one person
   from another. So the condition is carried entirely by the Cloudflare Access policy in front of
   it. **Operator obligation: that policy must admit only the holder of the account behind
   `claude-max-2`.** Admitting a second person makes this route one subscription intermediated on
   behalf of whoever arrives, which is the sentence immediately above the carve-out. If dsh is ever
   opened up, this route must be removed or re-credentialed per person, and no code change will
   signal that — hence this paragraph.
4. A corollary of 3 that is easy to miss: the *agent* must not be able to drive the CLI either, or
   the requester stops being any end user at all. That is why the tool policy and the credential
   scrub live in `claude-cli.sh` rather than in the adapter's argv, and why the wrapper is off
   PATH — see decision 2.

### What was measured

In the live dsh pod, 2026-09-22, before any of this was wired:

1. **The pin is reachable and authentic.** `GET …/2.1.267/linux-x64/claude` → 217,013,744 bytes,
   sha256 `0399c793…03c0`, byte-for-byte the value in Anthropic's signed manifest for that release.
2. **It runs here.** `claude --version` → `2.1.267 (Claude Code)`. `claude doctor` → native,
   `linux-x64`, `Search: OK (bundled)`, `Auto-updates: disabled (set by env: DISABLE_AUTOUPDATER)`.
   The node is x86_64 / glibc 2.36, so the non-musl build is the right one.
3. **Unauthenticated degrades cleanly**, which is what the estate sees until the operator writes
   the token: a well-formed result object with `is_error: true` and
   `result: "Not logged in · Please run /login"`. No crash, no hang.
4. **`isolateTools` alone does NOT produce a text-only tier, and this is the finding most likely to
   be lost.** Tool counts read straight out of the CLI's own init event, which it emits before it
   authenticates, so this is measurable without spending anything:

   | flags | tools the session gets |
   |---|---|
   | none | **22** — `Task`, `Bash`, `Edit`, `Read`, `NotebookEdit`, … |
   | `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` (what `isolateTools` sends) | **22** |
   | an explicit deny list of the tool names a reviewer would think to write | **14** |
   | `--disallowed-tools '*'` | **0** |
   | `--tools ""` | **0** |
   | all of the above plus `--setting-sources ""` | **0**, and no settings file loaded |

   The upstream README calls the first flag "stripping Claude's tooling": true of MCP servers,
   false of `Bash`, `Read` and `Edit`, in a pod whose ServiceAccount is cluster-admin. The explicit
   deny list is the trap a careful reader falls into — it cannot name tools a future release adds,
   and it left 14 standing here. The wildcard and the empty allowlist each reach zero on their own;
   the route sends both because they fail differently.

5. **Project settings are a hook vector, and the default working directory was the wrong one.**
   Claude Code reads `.claude/settings.json` from its cwd, and such a file can carry `PreToolUse`
   hooks, which are arbitrary shell running inside a process that holds the subscription token.
   The container's default cwd is `/workspace` — precisely the tree model-authored code is allowed
   to write. Measured: with a hostile settings file planted there, the CLI *does* read it and
   refuses its `permissions.allow` because the workspace is untrusted, and with
   `--setting-sources ""` it does not read it at all. The route uses a cwd outside `/workspace`
   *and* passes the flag; the CLI's own trust gate is a third layer, not the plan.

6. **The CLI runs under a minimal environment.** With `env -i` carrying only `PATH`, `HOME`,
   `LANG`, `TMPDIR` and the `CLAUDE_*` settings, it starts, resolves TLS and reaches Anthropic.
   That is what lets the wrapper hand the child an allowlisted environment instead of the pod's.
7. **The row resolves in the real harness.** `dsh --profile web --dump-config` against a throwaway
   `DSH_HOME` carrying the candidate patch and the three vendored files: exit 0 with **empty
   stderr**, the provider and all three models present, and searxng / credentials-openbao /
   subagent-codex still resolving. Empty stderr is the signal that matters — a dropped row logs one
   line and the process still exits 0.
8. **`@deepseek-ai/dsh-llm` resolves from the profile directory** and exposes `LlmAdapter`,
   `LlmError` and `LlmAdapter.prototype.prepareCall`, which is what lets the adapter ship as a file
   rather than a package.

## Decision

1. **Serve Claude in dsh by running the published Claude Code CLI as a child process**, as the
   provider `claude-cli`, on a `claude setup-token` belonging to the subscription owner. The binary
   is not an implementation detail to be optimised away later: it is the thing that makes this
   route legitimate. A future change that removes it and talks to `api.anthropic.com` directly with
   the same token is the design ADR 0029 rejected.
2. **Text-only tier** (operator's choice): `--disallowed-tools '*'` alongside `isolateTools`. Claude
   answers, plans and reviews; it executes nothing. Removing that flag silently converts this route
   into a full agent with a shell in a cluster-admin pod, so it is pinned by a test.
3. **The adapter is vendored as three files, not installed from npm.** A row's `name:` resolves
   from the profile directory, whose `node_modules` is a symlink farm of dsh's own closure; a
   package installed beside it is a boot crash-loop on a 1-replica Recreate Deployment, not a
   missing feature. Upstream is `Eyalm321/dsh-claude-cli-provider` @ `e149de6` (MIT), with three
   marked deviations and no others — the full diff is in the files.
4. **The credential is handled in a wrapper, not in the adapter.** `claude-cli.sh` reads the token
   from the ESO mount per turn, unsets the two variables that outrank it, and execs the pinned
   binary. The adapter — JavaScript running inside the process that executes model-authored tool
   calls — carries no token logic at all.
5. **The credential is a copy of the `claude-max-2` broker's existing token**, written once by the
   operator into `af/dsh/credentials` as `DSH_CLAUDE_CODE_OAUTH_TOKEN`. No new ExternalSecret, env
   var or publisher — the `dataFrom.extract` discovery secret picks the field up at its next
   refresh (the field is listed in `openbao-eso.yaml` all the same; see there for why).

   **Operator decision, 2026-09-22, against the recommendation in the first draft of this ADR.**
   That draft called for a token minted for dsh alone, on the grounds that
   `operator/broker/anthropic/claude-max-2/oauth` is a **RESCUE-class** path with `cas_required`
   that `openbao-recovery.md` tracks in exactly one place, so a second home for it in a pod running
   model-authored code means a leak forces a re-mint *and* a re-seed across the broker chain. That
   cost is real and is not withdrawn.

   The operator chose the copy, and the argument for it is the stronger one on the evidence
   available: **minting a second setup-token for one account is not documented to leave the first
   valid.** If it revokes it, the blast radius is the whole `claude-max-2` broker fleet, discovered
   at the moment the estate needs it. A copy has a known failure mode; minting has an unknown one.
   One credential in two places also beats two credentials to track when neither rotates.

   Note for a later reader that the *original* justification offered for a copy was wrong and is
   not what carries it: ADR 0025 decision 2 says the absence of a refresh-token family means
   seeding cannot invalidate sibling seats. It says nothing about a copy landing somewhere with a
   wider blast radius, and non-rotating cuts the other way there. The copy stands on the minting
   risk, not on that decision.

   **Retained risk, recorded so it is not rediscovered:** a leak from the dsh pod now requires
   revoking a credential the broker fleet also runs on. `openbao-recovery.md` names the second
   location for exactly that reason — update both places or neither.
6. **`agent-default-model` is not changed.** A model that can never emit a tool call cannot drive
   dsh's agent loop.
7. **`anthropic-fable` is untouched.** The metered Fable route stays exactly as ADR 0029 left it,
   including its cost. This adds a cheaper neighbour; it does not retire anything.

## Consequences

* **dsh now reaches `api.anthropic.com` directly, and "LiteLLM is the ONLY model path" is no longer
  true.** That invariant was already weakened when general HTTPS egress was granted on 2026-09-09;
  this is the first route that depends on it. The NetworkPolicy comment is corrected in this change
  rather than left to mislead the next reader. No policy rule changes — the reach already existed.
* **It spends the `claude-max-2` account's weekly window, and more consumers share it than
  "dsh and the reviewer".** Seat `b` is `clauderun2`, whose account is the one behind
  `claude-max-2`; the *Claude seat capacity* panel labels that row `constantin.chifor@strive.us`.
  **That attribution is a runtime Prometheus label, not a repo fact** — ADR 0025 decision 5 keeps
  the email out of git deliberately, and `dev-workers.md` attributes that address to a *Codex* seat
  on reviewer-2, which is a different seat lettered the same. Confirm against the panel or
  `claude-usage.py` as the seat before treating the owner as established; the load-bearing claim in
  this ADR is that the operator owns the account, and it is the operator's to confirm.

  The account also backs the `claude-max-2` broker, which the AgentForge activation notes put
  planner, reviewer, implementer and tester behind. So the window is shared at least three ways.
  Nothing in this design bounds dsh's share; the signals are that panel and the reviewer beginning
  to park. This is the opposite trade from ADR 0029's, which spent money to protect the seat —
  recorded plainly because it is the cost the operator chose instead.
* **dsh's own consumption is not separately observable, and the obvious check does not work.** A
  setup-token carries only `user:inference`: `/api/oauth/usage` and `/api/oauth/profile` answer 403
  `oauth_scope_insufficient` (measured on all three seats 2026-09-18, `dev-workers.md`). The
  dashboard reads the seat's *browser login*, which measures the account — so dsh's spend does show
  up there, mixed with everything else on that account and attributable to none of them. "Check it
  appears on seat b's usage" is therefore not a test anyone can run against this credential.
* **The Fable share of a Max plan is capped at 50% of the weekly limit**, per Anthropic's
  "Claude Fable models on your plan" (retrieved 2026-09-22). That is an external citation, not
  something this repo establishes; nothing in the ladder docs or ADR 0025 mentions a cap.
* **Plan mode cannot be exited on this route.** `plan-mode` is enabled here and its prompt requires
  `exit_plan_mode` to be called as a tool. A route with no tools can never call it, so a session
  switched to `claude-cli` while in plan mode stays there until the person changes session mode by
  hand. `tool-todo` and `tool-skill` are inert for the same reason. Not a defect of this route so
  much as the shape of a tool-less model in a tool-driven harness — but it will read as a hang.
* **Nothing bounds concurrent CLI children.** Each turn spawns a ~217 MB native binary in a
  container limited to 4 Gi, and dsh fans out across sessions and agent teams. ADR 0029 kept
  `rpm: 4` on the Fable route specifically to bound a runaway fan-out; this route has no equivalent,
  because the adapter offers none. A cgroup OOM on a 1-replica Recreate Deployment is an outage.
* **A Claude turn cannot use tools, dsh's or its own.** The adapter never passes dsh's tool schemas
  to the CLI, and the CLI has none of its own. Ask it to edit a file and it says it cannot.
* **Each pinned CLI version is ~217 MB on the shared `/app` PVC**, and old versions are kept for
  rollback per that volume's own policy. Prune deliberately, never to force an upgrade.
* **Version bumps are manual and are a two-value edit plus a Job rename.** Auto-update is off by
  design because the binary sits on a read-only mount. Claude Fable 5.1 needs ≥ 2.1.255; a future
  model may need newer, and the failure mode is a turn erroring on an unsupported model.
* **A failed download leaves the route absent, not the harness dead.** The install step is
  non-fatal like `uv` and `install-kubectl`, and the wrapper exits 78 with a message naming the Job.
* **One operator write is still outstanding** at merge time. Until it lands, picking a Claude model
  returns `Not logged in · Please run /login` and nothing else in dsh is affected.
