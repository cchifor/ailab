# ADR 0031 — No Claude integrations in dsh: the Fable route and the Claude Code CLI provider are withdrawn

**Status:** ACCEPTED (2026-09-22), operator-directed: *"Review the dsh ailab implementation. Remove all
existing claude integrations."* **Supersedes ADR 0029** (Claude Fable 5.1 on the metered key) **and
ADR 0030** (Claude on the Max subscription through the Claude Code CLI). Both ADRs stay in the tree
as the record of what was built and why; their designs are no longer deployed.
**Relates to:** ADR 0022 (model registration single source — the LiteLLM checksum moves with this),
ADR 0025 (the reviewer-1 Claude seats, which dsh no longer touches), ADR 0026 (the Codex
subscription providers, which stay).

## Context

dsh (`docs/runbooks/dsh.md`) had two ways to reach a Claude model, added one day apart:

| Route | Shipped | Credential | Path |
|---|---|---|---|
| `anthropic-fable` → `claude-fable-5-1` | 2026-09-21, #819 | LiteLLM's metered `ANTHROPIC_API_KEY` | LiteLLM `/v1/messages` |
| `claude-cli` → Claude Code CLI child process | 2026-09-22, #823, #824 | `DSH_CLAUDE_CODE_OAUTH_TOKEN` in `af/dsh/credentials` | direct to `api.anthropic.com` |

A review of the deployed state on 2026-09-22, before this change, found **neither route working**:

* **The Fable route has never returned an answer.** LiteLLM answers every `claude-fable-5-1` call
  with `AnthropicException … "API key is invalid."` (16 such lines in the last 72 h of the proxy
  log, all on this route: Anthropic rejects the metered key). Nothing in the estate observes
  this: no `litellm_*` series exists in the TSDB at all, so the "read the bill" control ADR 0029
  relied on was reading nothing.
* **The CLI route is unauthenticated.** The one operator write ADR 0030 left outstanding —
  `DSH_CLAUDE_CODE_OAUTH_TOKEN` into `af/dsh/credentials` — was never made: the synced
  `dsh-credentials` Secret carries no such key. Every Claude turn on that route answers
  `Not logged in · Please run /login`. The 217 MB pinned binary is installed on the `/app` PVC at
  `/app/tools/claude-code`, and the provider row is live in the profile's `cordis.patch.yml`.

So the two integrations were pure cost — some 2,370 lines of manifests, adapter, wrapper, tests
and runbook across seventeen files, a `cluster-admin` pod wired for a second copy of a
RESCUE-class credential (the slot empty, the plumbing live), a NetworkPolicy invariant rewritten
from "LiteLLM is the only model path" to "except this one", and a rollout of the LiteLLM
Deployment — with no working Claude turn to show for either. The operator's direction removes both.

## Decision

1. **dsh serves no Claude model, by any path.** The Fable provider block, its LiteLLM
   `model_list` entry and its reconcile line are gone; the Claude Code CLI provider, its three
   vendored adapter files, wrapper, install step, ConfigMap entries, ESO field, env, tests and
   runbook sections are gone. Mechanically: `git revert` of #824, #823 and #819, with the ADRs
   and plans restored as history.
2. **"LiteLLM is the ONLY model path" is true again** and the NetworkPolicy comment says so, as it
   did before #823. The general HTTPS egress granted on 2026-09-09 is unchanged; nothing in dsh
   depends on it for inference any more.
3. **The PVC copy of `settings.yaml` is unwritten at boot**, not left to drift:
   `DSH_PROVIDER=anthropic-fable DSH_PROVIDER_REMOVE=1` runs every boot in `deployment.yaml`,
   next to the identical `codex` removal, and is safe to keep indefinitely — once the block is
   gone the reconciler prints "not present; nothing to remove" and exits 0. Dropping the seed
   block alone would have left the PVC offering a model LiteLLM no longer has.
4. **The LiteLLM `checksum/config` is re-stamped** (`5db450ac0d74 → 62625e8ff082`) by
   `scripts/gen-litellm-consumers.py --write`, the ADR 0022 recipe, so the proxy rolls once and
   stops parsing the dead route. The consumer lists (Open WebUI `model_ids`, dsh seed `models`)
   are unchanged — the route was never consumer-visible.
5. **`claude-sonnet-4-6` and `claude-sonnet-5` on LiteLLM stay.** They predate dsh and serve the
   Strive platform catalog (ADR 0022); they are not a dsh integration. Whether the metered key is
   invalid for them too was not exercised in the log window and is a separate question for the
   platform's owner — see consequences.
6. **The dormant `tool-subagent-claude-code` rows in the generated agent-team compositions
   stay.** They are upstream's shipped `standard` preset rows, `disabled: true`, with no Bundle in
   `DSH_PLUGINS` and listed in `CREDENTIAL_GATED` by `test_agent_teams.py`; the compositions
   regenerate byte-for-byte from upstream, and hand-deleting a row would cost that property for
   a tool that cannot register anyway. They are not an integration.
7. **The `reviewer-claude` references in `conductor.runtime.json`, the release provenance and the
   runbook stay.** That is the forge's review persona on reviewer-1 (ADR 0025), a gate dsh's PRs
   pass through — not something dsh runs or calls.

## Consequences

* **The `claude-max-2` account's weekly window is dsh-free**, which was ADR 0030's recorded
  cost; and the metered key can no longer be spent from dsh, which was ADR 0029's. The
  Cloudflare Access obligation ADR 0030 placed on the operator (admit only the subscription
  holder) no longer carries anything — dsh has no per-person credential to protect.
* **Two things are left behind on volumes and are not removed by GitOps**, listed so they are
  pruned deliberately rather than found later:
  * `/app/tools/claude-code/…` on the shared `/app` PVC — the 217 MB pinned binary. Harmless
    and off PATH; delete it in the pod when convenient.
  * `/dsh-home/.claude-cli/` — the wrapper's install directory on the home PVC, if the Job
    created it. Same.

  OpenBao needs nothing: the token field was never written.
* **The metered `ANTHROPIC_API_KEY` on LiteLLM is rejected by Anthropic as of 2026-09-22.** This
  change does not fix or rotate it — it removes the only route that was exercising it. If the
  Strive platform's Sonnet routes matter, that key needs checking (`litellm-cloud-keys`, field
  `ANTHROPIC_API_KEY`), and the absence of any `litellm_*` metrics in Prometheus needs its own
  look, because a spend guard that reads no metric guards nothing.
* **A future Claude route in dsh is a new ADR**, not a revert of this one. ADR 0029's compliance
  finding (no subscription token through a third-party adapter) and ADR 0030's carve-out (the
  unmodified binary on the owner's own token) both remain accurate descriptions of the
  constraints; what changed is that the operator no longer wants Claude in this harness at all.
