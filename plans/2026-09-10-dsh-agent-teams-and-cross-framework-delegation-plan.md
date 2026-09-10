# Plan — native multi-agent plugins for dsh: agent teams and cross-framework delegation

**Date:** 2026-09-10 · **Status:** DRAFT — revised after codex cross-review round 1
**Repos:** `cchifor/ailab` (primary), `cchifor/cloudlab` (companion)

**Pinned base commits** — every claim below was read at these, and the codex cross-review in the
appendix judged them at these.
- ailab `a14fd3e3878c80ed6dfa63f1124e96723d31414c`
- cloudlab `8aa92edfa0f5fb1fcca766b6a0df122a65cdc477`
- dsh in production: `@deepseek-ai/dsh@0.1.5-alpha.2`, `DSH_BUILD=glibc`

> This branch is cut from a later `main` (`cdf8642`, a renovate vaultwarden bump) because that is
> what was current at push time. Nothing between the pinned commit and it touches `dsh`,
> `agentforge-broker`, `ai`, or the plan's reasoning; the pins are left as read rather than
> retro-fitted, so a reviewer can reproduce exactly what was reviewed.

**Relates to:** ADR 0018/0019 (AgentForge — the estate's existing multi-agent control plane and its
credential broker), ADR 0017 (Gitea master forge), `docs/runbooks/dsh.md`, `docs/runbooks/agentforge.md`.

---

## 0. What the ask turns out to be

The request reads as "write two new plugins". Reading the shipped code says otherwise, and the whole
plan hangs off that. Four findings, each verified against the published `0.1.5-alpha.2` packages
rather than inferred:

### F1 — dsh already has native multi-agent; the web profile deliberately withholds it

`@deepseek-ai/dsh-base@0.1.5-alpha.2/cordis.patch.yml` ships the whole delegation stack enabled:
`subagent` (the registry), `subagent-spawn-in-process`, `subagent-fork-in-process`,
`tool-subagent` (as `subagent` and `subagent_fork`), `tool-subagent-control`,
`tool-subagent-list-agents`, `workflow-worker-thread`, `tool-workflow`, `tool-ralph`.

`@deepseek-ai/dsh-web-app@0.1.5-alpha.2/cordis.patch.yml` then **disables every model-facing one of
them** — and its comment says exactly why:

> The subagent registry and its backends STAY in the host plane. `subagents` is a process singleton
> with a cross-session query surface … What a preset chooses is which delegation TOOLS its agent sees.

The web app replaces host-plane tool ownership with **agent presets**, inserting:

```yaml
- id: agent-presets
  name: '@deepseek-ai/dsh-agent-presets'
  config: { default: standard }
```

So "add support for native multi-agent plugins" is, mechanically, **compose presets** — not author
plugins. `dsh-agent-teams` is a *preset root*, and it is ours to write because no such package
exists upstream (verified: `@deepseek-ai/dsh-agent-teams` and `dsh-agent-teams` both 404 on the
registry).

### F2 — cross-framework delegation is shipped and dormant, one bundle away

`presets/standard/agent.cordis.yml` inside `@deepseek-ai/dsh-agent-presets` already carries:

```yaml
    - id: tool-subagent-codex
      name: '@deepseek-ai/dsh-tool-subagent'
      disabled: true
      config: { provider: codex, toolName: subagent_codex, backgroundMode: one-shot, maxDepth: provider-managed }

    - id: tool-subagent-claude-code
      name: '@deepseek-ai/dsh-tool-subagent'
      disabled: true
      config: { provider: claude-code, toolName: subagent_claude_code, backgroundMode: one-shot, maxDepth: provider-managed }
```

with the comment: *"Production dsh does not install these optional providers. Install the matching
Bundle in this Profile and restart the Host, then copy this preset and remove `disabled` from the
matching tool row. Host availability alone grants no tool."*

The bundles exist:

| package | what it brings | provider |
|---|---|---|
| `@deepseek-ai/dsh-subagent-codex` | depends on `@openai/codex` | `codex` |
| `@deepseek-ai/dsh-subagent-claude-code` | depends on `@anthropic-ai/claude-agent-sdk` | `claude-code` |
| `@deepseek-ai/dsh-subagent-acp` | depends on `@agentclientprotocol/sdk`; spawns any ACP agent | `acp` |

Each declares `dsh.bundle.patch`; each patch does one thing — `insert` the dormant provider on the
host plane.

**ACP is out of scope for this plan.** It is the general seam and the obvious way to reach
`platform/services/deepagent` later, but it delivers no capability the operator has asked for, and
installing a third bundle triples the surface of the riskiest work package for nothing. It is not
installed, not spiked, and not tested here.

### F3 — the runbook's "npm plugins crash-loop the pod" rule is narrower than it reads

`docs/runbooks/dsh.md` calls this "the single most expensive lesson in this runbook", and it is
correct about what was tried: `dsh-searxng-web` npm-installed **beside dsh in `/app`** throws
`MODULE_NOT_FOUND`, because a row's `name:` resolves from the **profile** directory, not the install
tree.

The supported route resolves from that same profile directory. `@deepseek-ai/dsh@0.1.5-alpha.2`
ships `dsh plugin --profile <name> <args>` (`lib/plugin-Ddi42qoW.js`), a thin **pnpm forwarder**: it
inits the profile if needed, runs `pnpm <args>` with `cwd` = the profile directory, then reconciles
`dsh.profile.bundles` in the profile's `package.json` against installed state — a dependency
resolving to a package that declares `dsh.bundle` joins the layer stack.

`@deepseek-ai/dsh-app-boot` confirms the layout: a profile is `$DSH_HOME/profiles/<name>` with its
own `package.json`, `pnpm-workspace.yaml` and pnpm-managed `node_modules`, while
`$DSH_HOME/profiles/node_modules` is the *fallback* farm mirroring dsh's own dependency closure.
The 195-vs-193 measurement in the runbook was of the fallback farm; it says nothing about the
pnpm-managed profile `node_modules`, which is where a bundle belongs.

**The runbook is not wrong and should not be deleted.** It should be narrowed to "do not npm-install
a sibling into `/app`", with the profile-plugin route documented beside it.

### F3a — a declared-but-missing bundle crash-loops the pod. It does not degrade.

This corrects an error in the first draft of this plan, which claimed a missing bundle would leave a
preset "broken with a reason" while dsh still booted. That is what happens to a **preset** naming an
unresolvable module. It is *not* what happens to a **bundle**. `dsh-app-boot`'s
`loadProfileDirectory` is unguarded:

```js
const layers = bundles.map((packageName) => {
  const packageDir = resolveBundleDir(binName, packageName, installAnchor, dir);
  const declared = JSON.parse(readFileSync(join(packageDir, "package.json"), "utf8")).dsh?.bundle?.patch;
  if (declared === void 0) throw new Error(`… profile bundle … declares no dsh.bundle …`);
  …
});
```

A name in `dsh.profile.bundles` whose package is absent throws during profile load, before any
preset is evaluated — the same crash-loop class the runbook already documents. **Therefore bundle
activation must be atomic: the name enters `dsh.profile.bundles` only after the closure is proven
present, and leaves it whenever the closure is not.** This drives the design of WP-3 and is the
reason WP-1 — which installs nothing — ships first.

### F4 — the credential problem is already solved in this estate, by AgentForge

Both cross-framework providers scrub credential-shaped variables from the parent environment and
require the child's key through an explicit `env` block. The naive move is to mount a Claude Max /
Codex Pro OAuth token into the dsh pod. That is the wrong answer here, and `docs/runbooks/dsh.md`
says why in its first line: **this pod executes model-authored code**, and `danger-full-access` is
selectable from its UI.

The estate already built the alternative (ADR 0019): per-account **credential-injecting brokers** in
`agentforge-broker` that mount one operator OAuth credential and inject it only after verifying the
caller's capability JWT, so callers never see a token.

Two caveats that shape the sequencing in §4:
- `AF_BROKER_UPSTREAM_BASE_URL` is `""` in `agentforge-broker/configmap.yaml` — the gateway proxy is
  preflight-gated and the enumerated upstream host has not landed.
- The same ConfigMap records `af_broker_tokens_used_total = 0` on all four seats over 30 days and
  `af_broker_requests_total` with no series at all. **Nothing is currently relayed through any
  broker.** dsh delegation would be the broker's first standing consumer — an argument for the
  design, and simultaneously the reason it cannot be the first thing shipped.

---

## 1. Architecture

```
                       ┌───────────────────────── ailab: ns dsh ─────────────────────────┐
  browser ─▶ CF Access ─▶ cloudflared ─▶ relay ─▶ dsh (host plane, 1 replica, Recreate)  │
                       │                    │                                             │
                       │        L1  agent-presets roster                                  │
                       │            roots: [shipped(system), /dsh-teams(system), user]    │
                       │                    │                                             │
                       │            ┌───────┴────────┬──────────────┐                     │
                       │        team-solo       team-review     team-swarm                │
                       │        (L1 only)       (needs L2)      (L1 only)                 │
                       │                             │                                    │
                       │        L2 provider bundles in dsh.profile.bundles ───────────────┤
                       └────────────┬───────────────────┬───────────────────┬─────────────┘
                                    │                   │                   │
                              LiteLLM :4000      L3a LiteLLM-backed   L3b broker-backed
                                    │            (phase 1, no creds)  (phase 2, gated)
                     ┌──────────────┴──────────────┐                        │
             ai LXCs (.44-.46)        cloudlab LXCs (.26/.28)      agentforge-broker :8700
             always on                 DAYTIME ONLY                (Claude Max / Codex Pro)
```

- **L1 — `dsh-agent-teams`.** A GitOps-owned preset root registered as a `system` trust root on the
  `agent-presets` row. Each team is one preset directory (`preset.yml` + `agent.cordis.yml`)
  composing a delegation posture. **L1 needs no new packages** — `subagent`, `subagent_fork`,
  `tool-workflow` and `tool-ralph` are already installed and already registered on the host plane.
- **L2 — provider bundles.** `dsh-subagent-codex` / `dsh-subagent-claude-code` installed into the
  `web` profile. Only `team-review` needs them.
- **L3 — child credentials.** Phase 1 routes child CLIs at LiteLLM (no subscription credential near
  the harness). Phase 2, operator-gated, routes them at the AgentForge broker.

### What a preset root is and is not

`dsh-agent-presets`' README: *"Treat every authored preset as trusted configuration because it grants
the capabilities of the plugins it selects."* `/dsh-teams` is mounted read-only and owned by git for
that reason.

**A preset is not a security boundary, and this plan does not treat it as one.** Three facts follow
from the deployment as it stands, and each is stated here so nobody later mistakes a preset for a
sandbox:

1. `includeUserRoot` defaults to true, so `$DSH_HOME/.agent-presets` — on a PVC the agent can write —
   is a live `user` root. The agent can author a preset that exposes any *installed* provider under a
   new id. Team ids cannot be shadowed (the shipped root is prepended and wins duplicate ids), but new
   ids can be created.
2. The agent has Bash. Once L2 lands, `codex` and `claude` executables exist inside the pod and are
   directly invocable, whatever any preset says.
3. `LITELLM_API_KEY` is already in the pod environment for the parent. Anything the agent can reach
   with the provider row's `env`, it could already reach from a shell.

The real containment is what it has always been: the pod, its NetworkPolicy, and the workspace root.
The presets decide **what the model is told it can do**, which is worth a great deal for steering and
nothing for containment. If the operator wants a genuine boundary on the user root, set
`includeUserRoot: false` on the `agent-presets` row — a one-line decision recorded in WP-1, which is
where that row is amended.

### Why not the obvious alternatives

| Alternative | Why not |
|---|---|
| Write a bespoke `agent-teams` cordis plugin as a file, like `searxng-search.mjs` | The delegation machinery already exists; a file plugin would reimplement `dsh-subagent` badly and own a supply-chain surface for no gain. |
| **Adopt the first-party experimental Agent Teams packages** | **NOT EVALUATED IN THIS PLAN, AND THAT WAS AN ERROR.** `@deepseek-ai/dsh-experimental-agent-team` and `-tool-agent-team` exist and became installable in `0.1.5-alpha.2`; this plan asserted no such package existed. They give named members, a durable peer mailbox and a shared task DAG — things preset composition cannot. Spiked separately (`plans/2026-09-10-dsh-experimental-agent-teams-spike.md`): they install and boot here. Not adopted, because members share one checkout with only advisory write scopes, which is the hazard WP-6 exists to contain. |
| Put team definitions in `$DSH_HOME/.agent-presets` | Exactly the unmanaged-state failure `cordis.patch.yml`'s header documents: it lived only on the PVC until 2026-09-08 and a volume rebuild would have silently reverted it. |
| Delegate by shelling out to `claude`/`codex` from Bash | No child accounting, no cancellation, no structured result, no depth handling. The providers exist to avoid this. |
| Reuse `platform`'s `deepagent` subagent machinery | Different runtime and process. A legitimate ACP *target* later; not a way to give dsh teams. |

---

## 2. Work packages — ailab

### WP-0 (gate) — reconcile the estate patch with the preset architecture

**Problem.** `kubernetes/apps/apps/dsh/cordis.patch.yml` re-enables, at the **host plane**, eight rows
that `dsh-web-app` disabled *because presets own them per-agent*: `compaction-basic`,
`command-compact`, `agent-instructions`, `plan-mode`, `tool-todo`, `tool-skill`, `skill-filesystem`,
`tool-web`. The shipped `standard` preset mounts every one per session.

The patch's own comment already smells it — *"The running agent already exposes Bash/Write/Read …
even though those rows read disabled here, so enabling them risks two registrations colliding"* —
that is the preset mounting them. The same reasoning applies to the eight that *were* enabled.

**This is a gate, not a cleanup.** Those rows are credited with making the harness usable —
compaction most of all — and that credit may be misattributed in either direction. Nothing is removed
on reasoning alone.

**Evidence must be behavioural, not a tool-name diff.** A duplicate service or hook can leave the
model-visible tool list identical, and so can removing a load-bearing one. Per row:

| row | the check that actually proves it |
|---|---|
| `compaction-basic` | drive a session past the compaction threshold and observe a compaction event; then again with the row removed |
| `command-compact` | `/compact` produces a reduction |
| `agent-instructions` | an `AGENTS.md` in the workspace reaches the system prompt |
| `plan-mode` | plan mode is enterable **and its 37-line prompt section is present** — the documented failure is "enabled but mute" |
| `tool-skill` / `skill-filesystem` | a filesystem skill is discovered and loadable |
| `tool-todo` | todo writes round-trip |
| `tool-web` | `web_search` and `web_fetch` both answer |

Plus, for every row, `--dump-config` **read with stderr** (loader errors go to stderr while the
process exits 0) and the registering plane identified.

**Secret hygiene, before any of this becomes documentation.** `--dump-config` resolves the tree, and
`dsh-agent-presets` documents `!!js` gates being *evaluated* during inventory reads. The estate's
config contains `!!js` expressions and the pod holds `LITELLM_API_KEY`. **Prove whether a dump emits
evaluated values or preserves expressions, on a throwaway `DSH_HOME` with a dummy key, before any
dump output is pasted into a runbook, a PR, or a review prompt.** If it evaluates, redact
mechanically, not by eye.

**Exit criteria** — not a dichotomy. Per row, one of: *duplicate, removed*; *load-bearing, kept, with
a comment saying which plane it serves and why*; or *inconclusive, kept, with the open question
recorded*. Presets may be active while some rows remain necessary; that is an expected outcome, not a
contradiction.

**Deliverable:** row-by-row evidence in `docs/runbooks/dsh.md` under a new "Presets own the tools"
section, plus the F3 narrowing of the "why not the community plugin" section.

**Blocked on:** cluster access — see §6.

### WP-1 — `dsh-agent-teams`: the preset root (no new packages)

Ships first, because it proves the roster, the trust model and the mount with **zero** packaging
risk: every plugin it names is already installed.

**New directory** `kubernetes/apps/apps/dsh/agent-teams/`, projected by `seed-settings` into
`/dsh-teams` and registered by amending the `agent-presets` row:

```yaml
- id: agent-presets
  name: '@deepseek-ai/dsh-agent-presets'
  config:
    default: standard          # RESTATED — a patch REPLACES a row's config, never merges into it
    roots:
      - { path: /dsh-teams, trust: system }
    # includeUserRoot: false   # operator decision, see §1 "not a security boundary"
```

Two mechanics already scarred into this repo:
- **A patch replaces.** Omitting `default:` does not "keep the default"; it erases it. Same trap as
  `trustedHosts` on the `connection` row.
- **The shipped root is prepended and wins duplicate ids.** Prefix every team `team-`.

**Staging is not activation, and a preset has no off switch.** Projecting a `preset.yml` into
`/dsh-teams` makes that preset *selectable* — the roster has no per-preset `disabled` flag. So
"ship the files now, activate later" is not available at preset granularity, and an earlier draft of
this plan quietly assumed it was.

What *is* available is the row-level `disabled:` flag inside `agent.cordis.yml` — the same mechanism
the shipped `standard` preset uses to keep `tool-subagent-codex` and `tool-subagent-claude-code`
dormant. The staging/activation split therefore lives **inside** each team file, and a team's
directory does not ship until that team is meant to be selectable at all.

**Teams shipped in this WP**

| id | composition | the case for it |
|---|---|---|
| `team-solo` | `standard` copied verbatim, model pinned, **every delegation row `disabled: true`** | The migration target for WP-0 and the control in every comparison. Copying it puts today's behaviour under git, which it currently is not. |

`team-solo` is a copy of `standard`, and `standard` carries `tool-subagent` (`subagent`) and
`tool-subagent-fork` (`subagent_fork`). **Copied verbatim it is a fan-out-capable team**, so WP-1
ships it with those rows `disabled: true`. What WP-1 proves is the roster, the trust root, the mount
and the `system`-root precedence — with no delegation reachable, and therefore no dependency on the
bounds, night-window or workspace gates.

`team-swarm`'s directory does **not** ship here; it lands in WP-1b (step 6) behind those gates.
`team-review` needs L2 and lands in WP-4.

**WP-1b (step 6)** removes `disabled:` from `team-solo`'s delegation rows and adds `team-swarm`
(`subagent` + `subagent_fork` + `tool-workflow` + `tool-ralph`; `tool-ralph`'s shipped
`maxRounds: 64` is far too high for a shared 9-GPU estate — **pin it to 8** and say so in the file).

Every team file states inline: model pin, `maxDepth`, `backgroundMode`, whether it can reach a
`-cloud` route (WP-2), and its concurrency posture (WP-5). `tool-ask-user` stays in every team — a
team that cannot ask is a team that guesses.

**Files (WP-1):** `kubernetes/apps/apps/dsh/agent-teams/team-solo/{preset.yml,agent.cordis.yml}`,
`cordis.patch.yml`, `deployment.yaml` (seed step), `kustomization.yaml` (`configMapGenerator` — content-
hashed, so editing a team rolls the pod, the property `relay.js` already relies on).

### WP-2 — the night window (ailab side)

Split out of the old WP-5 because a comment in a preset file changes no routing. `litellm.yaml`
already carries the scar:

> ConnectionRefused from 2026-09-08T17:20Z with NO fallback configured … the chain is empty exactly
> overnight. A fallback list whose members share an outage window is not a fallback list.

Fan-out makes this worse: N children hitting a dead route at 02:00 produce N failures per turn.

**Work**
1. Audit every `-cloud` model a team may pin and give each a fallback chain terminating in an
   always-on route (`qwen3.6-35b-a3b-local`, ai LXCs). Members sharing the cloudlab outage window do
   not count.
2. Check the fallback is *usable* for the pinned workload, not merely reachable: context length and
   — for any vision-capable route — modality. `litellm.yaml` already documents a vision-capable cloud
   route falling into a 500 against a text-only fallback.
3. Define mid-stream behaviour: what a child sees when a cloud stream dies partway through a
   response, and whether the team retries or surfaces it.
4. **Acceptance test:** an overnight (or simulated-outage) run of one delegation per team, asserting
   completion on the fallback route rather than an error.

This WP produces the routing change. CL-3 (cloudlab) records the decision; it does not implement it.

### WP-3 — a supported, atomic install path for profile bundles

**The conflict.** The profile directory lives on `dsh-home` (RWO, `local-path`, node-pinned). The
`dsh-install` Job mounts only `dsh-app` (RWX, nfs-csi) and *deliberately* does not mount `dsh-home` —
`install-job.yaml` documents that mounting it "could pin the Job to a different node than the
Deployment and deadlock the volume". Bundle resolution is anchored at the profile directory
(`resolveBundleDir(NAME, packageName, INSTALL_ANCHOR, profileDir)`). The thing with the network
cannot write where the packages must land.

**Option A — an `install-profile-plugins` initContainer.** Mounts `dsh-home`, runs
`corepack pnpm add` in `/dsh-home/profiles/web`. The 2026-09-09 egress change makes the registry
reachable, so the *network* objection in `install-job.yaml`'s header is moot; the *supply-chain* one
is not — pnpm lifecycle scripts would run in the pod's namespace at every start — and pod startup
becomes registry-dependent.

**Option B (recommended) — Job-side staging, init-container projection.**
1. `dsh-install` gains a second phase: build a profile closure under
   `/app/profiles/${DSH_VERSION}-${DSH_BUILD}-${PLUGINSET}/web` with `node-linker=hoisted` (a plain,
   relocatable `node_modules`), containing the provider bundles and a committed lockfile.
2. `seed-settings` projects that closure into `/dsh-home/profiles/web/node_modules`.

`PLUGINSET` is a short hash of the declared bundle set **and its lockfile** — without it, changing
providers silently overwrites the same staging path and a rollback has nowhere to roll back to.

**Atomicity and rollback are requirements, not polish** (F3a: getting this wrong crash-loops a
single-replica, `Recreate` UI). Five rules, each with an existing idiom in this repo:

1. **Verify-then-mark.** The Job writes `${STAGE}/.installed` only after proving every declared
   bundle resolves — the exact discipline `install-job.yaml` already applies to `.installed` after
   an OOM-killed Job left a marker over a half-written tree.
2. **The plugin marker is ADVISORY, never blocking.** This is the correction that makes rule 4 true.
   `wait-for-install` keeps blocking on the *dsh* install marker exactly as it does today and gains
   **no** condition on the plugin closure — otherwise a failed plugin install leaves `.installed`
   absent, `wait-for-install` spinning, and a single-replica `Recreate` UI down, which is a worse
   outcome than having no providers. `seed-settings` reads the plugin marker instead and decides.
3. **Fall back to the last good closure, then to none.** Staging paths are content-hashed
   (`PLUGINSET`), so previous closures survive on the RWX volume. `seed-settings` projects the
   newest closure carrying a valid marker; if the closure named by this git revision is unmarked, it
   projects the previous good one and logs the divergence; if there is none, it projects nothing.
   Retention: keep the last two, prune older with the existing `ttlSecondsAfterFinished` idiom.
4. **Declarative, not merged.** `seed-settings` **sets** `dsh.profile.bundles` to exactly
   (shipped template bundles) + (git-declared bundles **verified present in the closure it actually
   projected**). A merge leaves stale names after a git revert, and a stale name crash-loops the pod.
   This mirrors `cordis.patch.yml` being reinstalled unconditionally every boot.
5. **Fail closed on the name, open on the boot.** If a declared bundle is absent from the projected
   closure, its name never enters `dsh.profile.bundles`. dsh boots without the provider; a team that
   names its tool is listed broken with a reason. That degradation is *engineered by rules 2–4*; it
   is not a property of the loader, and it does not survive removing any of them.

**Why this stays a spike.** Node resolves through a symlink's realpath unless `--preserve-symlinks`
is set, and the bundles' peers (`@deepseek-ai/cordis`, `dsh-subagent`, `dsh-llm`, `dsh-session`,
`dsh-subprocess`, `dsh-timeout`, `dsh-agent`) must resolve from wherever the closure lands. Two
copies of a framework package would be worse than a clean failure: a second `cordis` instance means a
provider registering into a registry nobody reads.

**Spike (off-cluster, on a dev-worker; independent of WP-0 and runnable in parallel)**
- Reproduce the layout from `mirror.gcr.io/library/node:22`: install `@deepseek-ai/dsh@0.1.5-alpha.2`
  into `/app/0.1.5-alpha.2-glibc`, let it init a `web` profile, stage the bundles per Option B.
- Assert `--dump-config` resolves both provider rows, **reading stderr**.
- Assert **single-instance** resolution **against the instances the running host actually loaded**,
  not merely across the new packages. A static `require.resolve` from the profile dir and from each
  bundle can agree on the same *second* copy while the host and base tools use another under `/app`
  — which passes the assertion and still leaves the provider registering into a registry nobody
  reads. Boot the host, then compare the realpaths in its live module registry for each shared peer
  (`@deepseek-ai/cordis` above all) with those the bundles resolve; one realpath per peer, or fail.
- Assert the platform payloads are executable in this container (`@openai/codex` ships a native
  binary; `@anthropic-ai/claude-agent-sdk` ships a CLI payload) — a resolvable package with an
  unusable executable passes a config dump and fails the first delegation.
- Start the host and **actually run one delegation per provider**, against a stub upstream. A clean
  config dump is not the bar.
- Confirm the interaction with the supported CLI: whether a later `dsh plugin --profile web add`
  reconciles away hand-staged state, and record the answer in the runbook either way.

**Go/no-go.** If Option B cannot satisfy single-instance resolution, fall back to Option A with
`--ignore-scripts` plus an explicit allowlist for the platform payloads that need a build step,
accepting the registry dependency at pod start, and keeping rules 1–5 unchanged — including
rule 2: a failed `pnpm add` must not fail the initContainer, or it takes the UI down with it.

**Files:** `install-job.yaml` (new Job name — a Job's pod template is immutable and
`kustomize.toolkit.fluxcd.io/force` must be kept or `scripts/pin-image-digests.py` fails the estate's
image-pin job), `deployment.yaml`, `kustomization.yaml` (the `replacements` block gains the new Job
name and `PLUGINSET`).

### WP-4 — `team-review` and child credentials

#### Phase 1 (recommended first cut) — LiteLLM-backed, no subscription credential

Point both child CLIs at the estate's own gateway. The dsh pod already egresses to
`litellm.ai.svc:4000` and already holds a LiteLLM key from OpenBao via ESO. Nothing new is trusted.

**Permission modes — corrected.** The first draft claimed `acceptEdits` and `never` provided
containment. They do not. Codex's `never` governs *approval prompting*, not sandboxing; Claude's
`acceptEdits` *grants* edits and establishes no OS isolation. Restated honestly:

| provider | setting | what it actually does | why this one |
|---|---|---|---|
| `codex` | `approve-for-me` | `approvalPolicy: on-request`, `approvalsReviewer: auto_review`, **`sandbox: workspace-write`** | the only documented value that *pins* a sandbox. `never` omits the sandbox field and leaves it to codex's own default |
| `claude-code` | `dontAsk` (the default) | denies operations not already authorized rather than prompting | strictly tighter than `acceptEdits`; an earlier draft loosened it for no stated reason |

**`approve-for-me` is not auto-deny, and must not be described as one.** `auto_review` routes
permission requests through codex's own automatic reviewer *without a human* — it can **allow**.
So the escalation path has to be characterised, not assumed: **spike it** by delegating a task that
deliberately requests an out-of-sandbox operation and recording what `auto_review` grants under
`sandbox: workspace-write`. If it grants more than the workspace, the choice becomes `never` plus an
explicitly pinned sandbox, and this table changes. Do not activate `team-review` on an
uncharacterised reviewer.

`dangerously-bypass-approvals-and-sandbox` and `bypassPermissions` are never selected. The pinning
lives on the **provider row** in the bundle overlay, not in a team preset, so copying a preset cannot
raise it.

**There is no child→parent permission bridge.** Both providers run unattended and auto-deny; a child
that needs a wider scope reports the limitation in its reply (the shipped child prompt says exactly
this). `tool-ask-user` on the parent does not bridge it. `team-review` must therefore delegate work
that is *already* inside the child's scope: review and analysis, not privileged mutation.

**Provider row shape** (illustrative; child model pinned explicitly — a parent model pin does not
constrain a child CLI's defaults or its auxiliary requests):

```yaml
- id: subagent-claude-code
  config:
    permissionMode: dontAsk
    model: <pinned route>                        # explicit; do not inherit
    env:
      ANTHROPIC_BASE_URL: http://litellm.ai.svc.cluster.local:4000
      ANTHROPIC_AUTH_TOKEN: !!js process.env.LITELLM_API_KEY
```

**Gating spikes — a reachable endpoint is not a working delegation.** Both must pass an
end-to-end delegation, not a curl:
- **Anthropic wire.** Beyond `POST /v1/messages`: streaming, a tool-use round trip with a
  tool-result turn, token counting, and the header handling a Claude gateway expects.
- **Codex wire.** ADR 0019 finding (b) already paid for this: codex uses `wire_api="responses"` and
  POSTs `{base_url}/responses` **without** prepending `/v1`, so `base_url` must itself end in `/v1`.
  Beyond that: streaming, tool round trip, reasoning-field handling, and cancellation.
- **Route verification.** Assert from LiteLLM's own logs which model each child actually used,
  including auxiliary requests (title generation, compaction).

If a wire fails, that provider ships in phase 2 only and `team-review` degrades to the one that
works — stated in the preset file, not discovered at runtime.

#### Phase 2 (operator-gated) — broker-backed, subscription models

**Prerequisites owned by AgentForge, not by this plan.** Phase 2 cannot start until, independently
of dsh: `AF_BROKER_UPSTREAM_BASE_URL` is populated and the preflight gate is cleared; the broker's
first authenticated end-to-end relay succeeds; and streaming through the broker is demonstrated for
both wires. Operator approval is a *further* dependency, not the only one.

**Then**
- **4a — broker admission.** A `fromEndpoints` ingress rule for ns `dsh` + `app: dsh` on 8700, added
  to `broker-anthropic-max1/max2/claude-max-3/claude-max-4` and `broker-openai-codex`. The
  `BrokerSeat` controller is **inert in A1** (`brokerseat-crd.yaml`: nothing reads the CRD until the
  A2 pin bump), so this is five git edits, not a CR.
- **4b — dsh egress.** An explicit allow to each broker's pinned ClusterIP on 8700 — required, since
  `networkpolicy.yaml`'s internet rule *excludes* `10.0.0.0/8`.
- **4c — a capability for dsh, designed rather than merely approved.** A capability JWT is bearer
  authority in its own right: concealing the upstream OAuth is not the same as being harmless, and a
  pod running model-authored code can read its own environment. The design must state **scope**
  (which `aud`, which models), **expiry and refresh** (AgentForge capabilities were designed
  per-sandbox-run; a standing grant is a different object), **revocation** (how it is killed without
  a redeploy), and **quota** (per-capability ceiling, so a runaway team cannot drain a seat). Minted
  through the `agentforge-provisioner` operator path — ADR 0019 finding (c): OpenBao 2.5.5 disabled
  `generate-root`, so there is no root-token shortcut — and delivered by ESO reusing this directory's
  `openbao-eso.yaml` pattern.

### WP-5 — bounds, before activation

Codex's review was right that the first draft's bounds did not bound anything, and investigating it
surfaced a sharper problem.

**`maxDepth` cannot be set on the cross-framework providers.** `dsh-tool-subagent`'s README:
`maxDepth` "requires a provider with the `depthLimit` capability", and "a numeric `maxDepth` … the
provider cannot enforce **fails the mount**". The codex and claude-code providers reject agent-route
overrides and are configured `maxDepth: provider-managed` in the shipped preset — which "sends no
cap". So the shipped configuration for cross-framework delegation has **no depth cap the estate can
set**, and trying to add one breaks the mount rather than tightening it.

**`dsh-tool-subagent` exposes no sibling-concurrency knob at all.** Depth is not width; `one-shot`
bounds continuation, not runtime, tokens, or grandchildren.

> **CORRECTED 2026-09-10.** The sentence above is true of `dsh-tool-subagent` and was then wrongly
> generalised below into "the only lever dsh gives". dsh exposes **several scoped** fan-out
> controls; none is a process-wide agent ceiling:
>
> | control | scope | default |
> |---|---|---|
> | `workflow-worker-thread.maxConcurrentAgents` | concurrent `agent()` calls, **per workflow run** | `0` → `min(16, max(1, availableParallelism() - 2))` |
> | `workflow-worker-thread.maxTotalAgents` | cumulative `agent()` budget per run — a runaway backstop, **not** a second concurrency knob | `1000` |
> | `agent-loop.maxParallelToolCalls` | parallel tool executions per agent step; foreground `subagent` calls await, so it indirectly caps overlapping foreground children | `10` |
> | `jobs-local.maxConcurrentJobsPerOwner` | running+stopping jobs per owner; one-shot **background** subagents enter here before spawning (continuable ones bypass it) | `10` |
>
> Three things follow. The `0` default is **not** unlimited and **not** fixed at 1 — it resolves
> between 1 and 16, so an unset value on a large node permits 16 concurrent agents per run, which
> nobody reads out of "0". The workflow ceilings are **per run**, so K overlapping runs permit K×C
> — enforcement lives in per-session `WorkflowExecution` instance state (`activeSlots`,
> `slotWaiters`), not a shared semaphore. And **none of these bounds live agents process-wide**;
> they bound tool steps, job slots, and per-run workflow children respectively.
>
> WP-1b (#639) has since set `maxConcurrentAgents`/`maxTotalAgents` to 3/48 (4/64 on the conductor),
> so for those rows WP-5's job is now to **verify against CL-1's envelope**, not to design a bound.

So the bounds have to come from outside the tool config, and they land **before** any team that can
fan out is activated:

| bound | mechanism | note |
|---|---|---|
| depth, native providers | `maxDepth` on `tool-subagent` rows | default 3; pin explicitly per team |
| depth, external providers | **not settable** | forced `provider-managed`; compensate with width and resources |
| width, workflow path | `workflow-worker-thread.maxConcurrentAgents` (+ `maxTotalAgents` as a cumulative backstop) | per **run**, so K runs permit K×C; the `0` default resolves to up to 16, not 1 |
| width, direct path | `agent-loop.maxParallelToolCalls` (10) and, for one-shot background children, `jobs-local.maxConcurrentJobsPerOwner` (10) | indirect and partial: continuable background children bypass the job registry entirely |
| width, process-wide | **nothing** | no dsh setting bounds live agents across the process; prompted team size remains guidance |
| runtime | `tool-call-timeout-policy` (already in the host composition) | set an explicit per-delegation deadline |
| **process/CPU/memory** | the dsh container's `resources` **and a PID limit** | bounds the *local* workload — every codex/claude child is a process in this pod. Size it before WP-4. It does **not** bound inference |
| **inference CONCURRENCY** | **`max_parallel_requests` on dsh's LiteLLM key** | the in-flight ceiling, and the one that corresponds to CL-1's measured envelope. In-process native children are locally cheap and remotely expensive: they can hold many concurrent requests without approaching any CPU or PID limit |
| **inference RATE and BUDGET** | **`rpm_limit` / `tpm_limit` (+ budget) on the same key** | bounds sustained load and spend over time. Complementary to the row above, **not** a substitute for it |
| ralph | `maxRounds: 8` | down from 64 |
| GPU | CL-1's measured team capability map | WP-1 pins team sizes to it; the gateway limits are what make the pin enforceable rather than advisory |

**Rate limits are not concurrency limits, and an earlier draft conflated them.** `rpm_limit` and
`tpm_limit` bound admission *over a window*: a permitted burst at the top of the minute, or a handful
of overlapping long streams, can sit far above CL-1's measured concurrent-stream envelope while
violating neither. The knob that bounds in-flight requests is a separate one — LiteLLM's key and
budget parameters carry `max_parallel_requests` alongside `rpm_limit` and `tpm_limit` (read in
`litellm/proxy/_types.py`, not assumed).

**And a gateway limit bounds REQUESTS, not agents.** A second correction: `max_parallel_requests` is
documented as **per deployment**, while `global_max_parallel_requests` is the proxy-wide one — and
neither counts live *agents*, only routed requests. An agent waiting on a tool call, or executing
one, holds no request and is invisible to both. So a gateway limit protects the *backend* from
concurrent inference; it does not cap how many agents exist. Earlier drafts called it "the only
global ceiling", which conflated the two.

So the team size CL-1 measures is an **enforced** ceiling on concurrent inference only if the
correctly-scoped gateway limit is set **and proven enforced on the deployed image**: a gating test that fires N+1 concurrent requests on
dsh's key and asserts the extra is rejected or queued rather than served. Until that test passes,
team sizes are advisory, and this plan says so rather than implying a bound it does not have.
`litellm-vkeys.yaml` establishes the per-key idiom (applied there to the **litellm-local** gateway);
this is the same pattern applied to dsh's key on the cloud-capable `litellm` gateway, not that file
reused.

**Metrics need a producer.** dsh has no ServiceMonitor and this plan does not invent one. What is
actually available without new code: LiteLLM's per-key request/token series, and container CPU/
memory/PID from cAdvisor. Alert on those — and on the rate-limit and parallel-request rejections
above, which are the signal that a team is exceeding its envelope.

**Per-*team* virtual keys stay descoped**, and for a reason the rpm/tpm row does not change: dsh's
LiteLLM provider is host-level in `settings.yaml` with a single `apiKeyEnv`, so there is no
per-session key selection to hang three keys off. One key for dsh can be capped; three keys nothing
selects cannot. Revisit only if a future dsh version lets a preset own its provider row.

For phase 2, `af_broker_requests_total` gaining a series is a *signal*, not a proof: absence is
legitimate after an idle restart, and presence does not prove a successful relay. Alert on relay
outcome, not on counter existence.

### WP-6 — the workspace ownership contract

Not in the first draft, and it is the gap most likely to produce silent damage.

Children run in **the parent's workspace** — one `/workspace` PVC, `workingDir: /workspace`, which is
also the sandbox root dsh derives from `process.cwd()`. Two agents with edit permission in one tree
can corrupt each other's work without either violating its permission mode. `one-shot` does not help;
a parent and a foreground child overlap by construction.

**Required before any fan-out team is activated:**
1. **A write-ownership rule — and for external providers this plan commits to single-writer.**
   The codex and claude-code providers run in the **parent session cwd** and expose no `cwd`
   override, so a worktree the parent creates is not where the child runs; creating one would give
   the appearance of isolation and none of the substance. Only `dsh-subagent-acp` takes a `cwd`, and
   ACP is out of scope (F2). Therefore: **`team-review` delegates read-only analysis and review, and
   no external child writes to the workspace.** That is `team-review`'s natural shape anyway, and it
   composes with the fact that there is no child→parent permission bridge. Per-child working
   directories become available only if ACP is brought into scope; native `subagent`/`subagent_fork`
   children are in-process and inherit the parent's own workspace policy, so the same single-writer
   discipline governs `team-swarm` until measured otherwise.
2. **A descendant-cancellation test.** Cancelling a delegation must reap the whole tree, not the
   immediate CLI PID. Both providers document `disposeGraceMs` and a SIGTERM→SIGKILL tier; assert no
   orphaned processes remain in the pod afterwards.
3. **A two-session test.** `dsh-subagent`'s README notes the registry is a process singleton with a
   cross-session query surface, and that "the Activation inbox and ownership graph do not coordinate
   two harness processes". With two browser sessions open, assert session A cannot list, resume, or
   cancel session B's children.
4. **Convention propagation — asserted read-only.** A child CLI running in this workspace inherits
   none of the estate's habits. Explicit-path staging, Gitea-not-GitHub, and **no AI attribution in
   commits or PRs** must reach the child — via the workspace `AGENTS.md` (note
   `origin/fix/dsh-image-bytes-and-agents-md` is already adding `agents.seed.md`) and, for
   claude-code, via the settings sources it reads relative to the parent cwd.

   **Assert it without writing.** Delegate a task that requires the child to *report* the conventions
   it resolved — which forge it would push to, how it would stage, what it would put in a trailer —
   and check the answer. An earlier draft called for "one delegated commit", which contradicts rule 1:
   an external child must not write to the shared workspace, and a test that requires it to is a test
   that breaks the contract it exists to protect.

   If a *mutation* test is genuinely wanted, it runs in a disposable scratch repo created outside
   `/workspace` for that purpose and deleted afterwards — never in the production workspace, and
   never against a real remote.

---

## 3. Work packages — cloudlab

cloudlab supplies **inference capacity and its availability window**. No agent control plane moves
here; that boundary is the point of ADR 0001 and is worth recording rather than assuming.

### CL-1 — measure concurrent-team load

Extend `scripts/vllm-test.sh` (or add `scripts/team-load-test.sh` beside it) to drive N concurrent
long-context streams and record: aggregate and per-stream tokens/s, KV-cache occupancy, TTFT at the
knee, and the concurrency at which preemption starts.

**Measure through LiteLLM, not only against the backend**, and with a competing interactive session
running — a direct backend benchmark omits the gateway and the contention that actually matters here.

Record the result in `docs/runbooks/cloud-gpu-cluster.md` as a **team capability map** beside the
existing per-GPU one: how many concurrent agents each route sustains at which context length. WP-1
pins team sizes to this number.

### CL-2 — decide the context/concurrency trade (only if CL-1 says so)

The README's 7.71× figure is a capacity estimate *at the stated maximum request length*; it does not
establish that the server is mistuned, and the first draft overread it. Lowering `max_model_len` is
not equivalent to running shorter requests, and raising `--max-num-seqs` can *worsen* preemption.

So: **no tuning change is proposed here.** If CL-1 shows preemption inside the team envelope,
evaluate a change then, and record the case for leaving it alone with equal weight.

### CL-3 — ADR 0002: the delegation capacity boundary

New `docs/decisions/0002-delegation-capacity-and-the-night-window.md`, recording:
- the estate boundary (cloudlab serves inference to ailab's delegation; the agent control plane stays
  in ailab);
- that `-cloud` routes are unavailable nightly, and that the *implementation* is ailab's WP-2 — this
  ADR records the decision, it does not change routing;
- the recommendation: teams fall back to `qwen3.6-35b-a3b-local` overnight and degrade in quality.
  On-demand wake via `cloud-power` (`MODE=wol`, `POST /api/wake`) is **rejected for now** — an agent
  that can wake nine RTX 3090s at 03:00 is a cost surface, and the runbook §4 records cloud3 reporting
  zero enabled ACPI wake devices pending a BIOS visit, so the mechanism is not uniformly available.

cloudlab has no `plans/` directory; CL-1..3 land as one PR carrying the ADR and the runbook section.

---

## 4. Sequencing

Reordered after review: the riskiest packaging work no longer gates the cheapest proof, and bounds
land before the capability they bound.

| # | Gate / package | Depends on | Ships as |
|---|---|---|---|
| G0 | Land `fix/dsh-image-bytes-and-agents-md` first | — | existing PR |
| G1 | **WP-0** evidence + runbook narrowing | cluster access | ailab PR 1 |
| 1 | **WP-1** — preset root + `team-solo`, **delegation rows disabled** | G1 | ailab PR 2 |
| 2 | **CL-1** — measure (parallel from day 1) | — | cloudlab PR 1 |
| 3 | **WP-2** — night-window routing + acceptance test | — (parallel) | ailab PR 3 |
| 4 | **WP-5** — bounds: timeouts, pod resources, PID limit, ralph 8 | 1 | ailab PR 4 |
| 5 | **WP-6** — workspace contract + cancellation/two-session tests | 1, 4 | ailab PR 5 |
| 6 | **WP-1b** — enable `team-solo`'s delegation rows; add `team-swarm` | 2, **3**, 4, 5 | ailab PR 6 |
| G2 | **WP-3 spike** — packaging (parallel from day 1, no dependency on G1) | — | note in `plans/` |
| 7 | **WP-3 build** — install path (Option B or A) | G2 go | ailab PR 7 |
| 8 | **WP-4 phase 1** probes + `team-review` | 7, 5, **3**, probes pass | ailab PR 8 |
| 9 | **CL-3** — ADR 0002 | 2, 3 | cloudlab PR 2 |
| 10 | **WP-4 phase 2** — broker-backed | AgentForge prerequisites **and** operator approval | ailab PR 9 |

**No delegation is reachable before WP-2's fallback acceptance test passes.** Steps 6 and 8 depend on
step 3 for that reason: the work is deliberately parallel, and without the edge an engineer following
the graph could switch on fan-out while the overnight routing is still the empty chain `litellm.yaml`
already documents.

Step 1 is exempt **only because it ships no reachable delegation.** A preset is selectable the moment
its files land — the roster has no per-preset off switch — so this gate cannot live at the file level.
It lives at the row level: `team-solo` ships with `disabled: true` on every delegation row, and step 6
is what removes it. Drop that flag from WP-1's PR and step 1 inherits steps 3, 4 and 5 as
dependencies, collapsing the ordering.

**G0 is real.** `origin/fix/dsh-image-bytes-and-agents-md` touches `deployment.yaml`,
`kustomization.yaml` and `settings.seed.yaml` and adds `agents.seed.md` — the same files WP-1 and
WP-3 edit, including the seed-settings initContainer.

Steps 1–6 deliver working multi-agent teams with **no new packages, no new credentials, and no new
egress**. Everything after step 6 is the cross-framework half, and it can slip without taking the
first half with it.

---

## 5. Risks

| Risk | Why it is real here | Mitigation |
|---|---|---|
| **A declared-but-absent bundle crash-loops a single-replica UI** | Verified in `loadProfileDirectory`: unguarded `resolveBundleDir` + `readFileSync` (F3a) | WP-3 rules 1–5: verify-then-mark, advisory (never blocking) plugin marker, last-good-closure fallback, declarative bundle list, fail-closed on the name |
| **A failed plugin install takes the UI down instead of the providers** | The obvious design — gate `wait-for-install` on the plugin marker — converts a degraded feature into an outage on a 1-replica `Recreate` Deployment | WP-3 rule 2: the plugin marker is advisory; `wait-for-install` is untouched |
| **Fan-out is unbounded in *inference*** | Native in-process children are locally cheap and remotely expensive; no CPU or PID limit sees a burst of concurrent model requests | `max_parallel_requests` on dsh's LiteLLM key bounds in-flight requests; `rpm_limit`/`tpm_limit` bound rate and spend (WP-5). **Team sizes stay advisory until the N+1 gating test proves enforcement on the deployed image** |
| **Two framework instances load and the provider registers into a dead registry** | pnpm's isolated layout vs. a hoisted copy; realpath resolution | Spike asserts single-instance `require.resolve` for every peer, and runs a real delegation |
| **A patch erases config it looks like it merges** | Bitten twice already: `trustedHosts`, and `plan-mode`'s 37-line prompt | Every amended row restates its full config, generated from `--dump-config`, not retyped |
| **WP-0 removes a load-bearing row** | Compaction is the difference between a long session and a dead one; a tool-name diff cannot see it | Behavioural check per row; three-way exit criteria; nothing removed on reasoning alone |
| **`--dump-config` evidence leaks an evaluated secret into a runbook or a review prompt** | `!!js` gates are documented as evaluated; the pod holds `LITELLM_API_KEY` | Prove evaluation behaviour on a throwaway `DSH_HOME` with a dummy key **before** any dump becomes documentation; redact mechanically |
| **Fan-out is unbounded in width** | dsh exposes no sibling-concurrency knob; `maxDepth` is unsettable on external providers and `provider-managed` sends no cap | Bounds land in WP-5 *before* activation, and are enforced where they can be: pod CPU/memory/PID, call timeouts, team composition |
| **Two agents corrupt one workspace** | One `/workspace` PVC; children run in the parent cwd; both permission modes permit it | WP-6 write-ownership rule; worktree-per-child where isolation is needed |
| **A subscription credential lands in a pod that runs model-authored code** | The pod's defining constraint; `danger-full-access` is selectable in its UI | Phase 1 carries none. Phase 2 is broker-mediated, designed (scope/expiry/revocation/quota), and operator-gated |
| **Presets are mistaken for a boundary** | Writable user root, Bash access to installed executables, a key already in the pod env | Stated explicitly in §1; `includeUserRoot: false` offered as the one real lever |
| **Delegation fan-out starves the GPU estate** | 9 GPUs, daytime only, shared with interactive use | CL-1 measures through the gateway under contention; team sizes pinned to it |
| **Job pod template immutability breaks the whole Kustomization** | The 2026-07-28 outage, and the `pin-image-digests.py` gate | New Job name on every change; keep `kustomize.toolkit.fluxcd.io/force` |
| **Phase 2 blocks on work this plan does not own** | `AF_BROKER_UPSTREAM_BASE_URL` empty; zero relayed requests in 30 d | Listed as AgentForge prerequisites; phase 1 does not touch the broker |
| **RWO `local-path` pins dsh to one node** | Pre-existing (`dsh-home`); every new init step inherits it | Add no second node-affine volume; Option B keeps the installer on the RWX volume |

---

## 6. Verification and testing

**Repo-local gates (every PR):** `just manifest-lint`, `just af-verify-hashes`,
`scripts/tests/test_manifest_paths.py`. New files under `kubernetes/apps/apps/dsh/` join the
content-hashed `configMapGenerator`, so `scripts/check-inline-hashes.py`'s invariant applies.

**New repo-local test:** every shipped team directory has a `preset.yml` and an `agent.cordis.yml`
that parses as a list of named plugin rows; no team id collides with a shipped id (`standard`, `ptc`,
`cordis`, `minimal`); every plugin a team names is either in the base composition or in the
git-declared bundle set. A broken preset is listed-with-a-reason in production rather than loud, so
this has to be caught in CI.

**Live, non-destructive:** the runbook's throwaway-`DSH_HOME` resolution, extended to assert each
provider row resolves and each team composes — read **stderr**, and redact per the secret-hygiene rule.

**Install-failure test (the availability case, distinct from the boot case):** make the plugin install
fail (unreachable registry, bad spec) and assert that `wait-for-install` still completes, dsh boots,
`seed-settings` projects the previous good closure or none, and the UI is up. An outage here would be
self-inflicted by the guard meant to prevent one.

**Boot-failure test (the one an earlier draft got wrong):** declare a bundle name whose package is
absent and confirm the engineered behaviour — `seed-settings` omits it, dsh **boots**, an ordinary
session works, and only the team naming its tool is listed broken. Then confirm the unengineered
path still fails as expected by injecting the name directly into the profile `package.json`, proving
the guard is what is protecting the pod.

**Per-provider end-to-end (WP-4):** one delegation returning a result; one *cancelled* delegation with
no orphaned processes; one **read-only** convention check per WP-6 rule 4 — the child reports the
forge, staging rule and trailer policy it resolved, and writes nothing.

**Concurrency ceiling (WP-5), gating:** fire N+1 concurrent requests on dsh's LiteLLM key and assert
the extra is rejected or queued. Until this passes, `max_parallel_requests` is unproven on the
deployed image and every team size in this plan is advisory, not enforced.

**Concurrency (WP-6):** two browser sessions; assert no cross-session listing, resumption or
cancellation.

**Not available from this worker.** This dev-worker has no kubeconfig — `kubectl config get-contexts`
is empty and `admin@ai` does not exist here. Every live step above needs a machine with cluster
access, and WP-0 gates the first half. That is the first thing to unblock. The WP-3 spike and CL-1
are deliberately *not* blocked on it and can start immediately.

---

## 7. Open questions for the operator

1. **Phase 2, yes or no?** A standing subscription capability for dsh via the broker. Phase 1 is
   useful on its own; this decides whether the plan has a step 10 — and it also decides whether the
   AgentForge broker prerequisites become this project's problem.
2. **Which teams are actually wanted?** `team-solo` / `team-swarm` / `team-review` are drawn from how
   the estate already works. Cutting `team-swarm` removes the largest capacity and fan-out risk and
   leaves the plan coherent.
3. **`includeUserRoot: false`?** Closes the model-writable preset root. Costs the operator the ability
   to author a preset live on the PVC.
4. **Night window (CL-3).** Confirm: teams degrade to `qwen3.6-35b-a3b-local` overnight rather than
   gaining the ability to wake the GPU cluster.
5. **ACP later?** Scoped out here. It is the general seam and would make
   `platform/services/deepagent` a delegation target. Say if it should be in.

---

## Appendix — codex cross-review

Reviewed at the pinned base commits with `codex exec -s read-only -m gpt-6-astra` (xhigh), plan
supplied inline. Two rounds, the skill's limit.

### Round 1 — *"I would not approve this plan as written"*, 15 findings

**Folded in (13):** sandbox-semantics error; the missing-bundle degradation claim (F3a — verified
wrong against `loadProfileDirectory` and corrected); install atomicity and rollback; bounds that do
not bound (which also surfaced that `maxDepth` is *unsettable* on external providers); workspace
ownership (new WP-6); presets are not a boundary; LiteLLM probes insufficient; the night fallback had
no implementation (new WP-2); WP-0's evidence was a tool-name diff; the `--dump-config` secret-leak
path; per-team vkeys unimplementable as described; Option B vs. the supported pnpm contract;
phase 2's AgentForge prerequisites.

**Folded in with a narrower change (2):** the vLLM tuning claim was overread — CL-2 now proposes no
tuning change unless CL-1 demands one. Scope ordering was wrong — WP-1 (preset root, no packages) now
precedes WP-3 (packaging), the spike no longer waits on WP-0, and ACP is dropped entirely rather than
installed while nominally excluded.

### Round 2 — *"I would not approve this revision yet"*, 5 partials + 2 new defects

Round 2 marked 8 findings resolved, 6 partial and 1 unresolved, and judged the round-1 appendix to
have overstated closure. It was right. Every partial and both defects are addressed above:

| round-2 point | what changed |
|---|---|
| `approve-for-me` is not auto-deny; `auto_review` can allow | claim corrected; characterising the reviewer is now a gate on activating `team-review` (WP-4) |
| degraded boot conflicts with the startup gate; a failed install leaves the UI down | WP-3 rule 2 makes the plugin marker **advisory** — `wait-for-install` is untouched — plus rule 3's last-good-closure fallback and a new install-failure test (§6) |
| CPU/memory/PID do not bound remote inference | new inference rows in WP-5 on dsh's LiteLLM key, the estate's own `litellm-vkeys` idiom applied to the cloud-capable gateway. *(The round-1 fix named rpm/tpm; the PR review below corrected that to `max_parallel_requests` for the in-flight bound.)* |
| worktrees do not place children in them | WP-6 now **commits to single-writer** for external providers and says why a parent-created worktree is theatre when the provider exposes no `cwd` |
| the graph permits activation before overnight routing is ready | steps 6 and 8 now depend on step 3, with the reason stated in §4 |
| the singleton assertion has a false-positive case | the WP-3 spike now compares against the realpaths in the **running host's** live module registry, not across the new packages only |

**Not carried:** none.

### PR review round (Gitea PR #622, `reviewer-codex` + `reviewer-claude`)

Six findings on the pushed plan, all accepted and fixed in one commit:

| finding | what changed |
|---|---|
| codex: step 1 activates a team before the gates the plan itself requires | **The real defect: a preset has no off switch.** Projecting `preset.yml` makes it selectable, so a file-level staging split does not exist. `team-solo` is a copy of `standard`, which carries `subagent`/`subagent_fork` — verbatim it is fan-out-capable. WP-1 now ships it with `disabled: true` on every delegation row, and WP-1b removes it (WP-1, §4) |
| claude: `team-swarm`'s files in WP-1 make it selectable before its bounds land | same root cause; `team-swarm`'s directory no longer ships in WP-1 at all |
| codex: the delegated-commit test contradicts WP-6's single-writer rule | convention propagation is now asserted **read-only** — the child reports what it resolved; any mutation test moves to a disposable scratch repo outside `/workspace` (WP-6 rule 4, §6) |
| codex: rpm/tpm bound rate, not concurrency | corrected and split into two rows: `max_parallel_requests` is the in-flight ceiling, `rpm_limit`/`tpm_limit` bound rate and spend. Team sizes are **advisory until** a gating test proves `max_parallel_requests` is enforced on the deployed image (WP-5, §6) |
| claude: stale "WP-2 ships first" in F3a | → WP-1, the package that installs nothing |
| claude: stale "recorded in WP-2" for `includeUserRoot` | → WP-1, where the `agent-presets` row is amended |

Two of these were the same defect seen from different angles, and it was a real one: the plan's
central sequencing promise — no fan-out before its bounds — rested on a staging/activation split that
the preset roster does not offer. The gate now lives at the row level, where the mechanism actually
exists.

The plan has not been re-reviewed since this round. Merge judgement is the operator's.
