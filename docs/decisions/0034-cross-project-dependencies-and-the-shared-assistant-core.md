# ADR 0034 — Cross-project dependencies: one owner per deliverable; the Assistant core is shared, its UIs are not

**Status:** PROPOSED (2026-10-03), owner-directed: *"Provide the best approach to address these accidental
interdependencies … recommend the best approach to avoid interdependencies that can disrupt or negatively affect the
project's development"* and *"analyze the existing repos … if we should implement the assistant as a shared reusable
component, maybe in a repository of shared components, or we should wait"*. The analysis was cross-validated with Codex.
**Relates to:** ADR 0017 (Gitea is the forge; CI on Gitea Actions), ADR 0032 (the shared runner pool),
`cchifor/platform` ADR-022 (draft, the vendoring rule) and `plans/2026-10-02-platform-modularization-program-plan.md`
(Track H, the harness; Track W, the Assistant page), `cchifor/llm-router` #79, #80, #81.

## Context

**How llm-router came to depend on Relay.**
- llm-router lived in the personal namespace of the `dsh` agent account (`dsh/llm-router`).
- The org's 16 CI runners and its registry credentials are scoped to the `cchifor` org, so the repo had no CI and could
  not publish an image.
- It was first deployed as PVC-staged releases (runbook `llm-router.md`).
- On 2026-10-02 the Relay session needed router PR #65 live. It built a pipeline in `cchifor/relay` that packages
  both Relay and the router (`deploy/ci/releases.json`, `publish.py`, with release v0.2.0 hard-coded). Every router
  release since then edits Relay's release and rebuilds Relay's image.
- On 2026-10-03, shipping two router UI fixes took:
  - a router archive attached to Relay's release;
  - a branch in Relay's repo;
  - a cherry-picked router release branch, because router `main` carried unreleased in-progress work (ailab #1040).

**The Assistant/chat UI across the forge (all 18 repositories scanned, 2026-10-03).**

| Repository | Chat UI | Framework | Protocol | State |
|---|---|---|---|---|
| `cchifor/llm-router` | Assistant page, plus a framework-free web core (U12) | React | router chat wire (`SessionView`, `AgentEvent`, `ChatInput`, `PanelView`, `PendingInteraction`) over SSE | the reference; very active |
| `cchifor/platform` `apps/web/src/features/ai_chat` | ~16k lines | Vue | AG-UI (deepagent) | retiring for the harness (Track W) |
| `cchifor/platform` `services/harness` | vendors llm-router's server core (main) and web core (`feat/web-harness-wire`); a 1:1 Vue port of the router's Assistant page is planned | Vue | router chat wire | active |
| `cchifor/agentforge-platform` `webapp/src/features/chat` | a lift of platform's chat | Vue | AG-UI | dormant since 2026-08-02 |
| `cchifor/relay` `web/src/chat` | a copy of the router's React UI (2026-10-02) | React | router chat wire | already missing router fixes (#69, #75) |
| `cchifor/trueswarm-admin` | Svelte Assistant | Svelte | its own (`ChatView`, `TraceEvent`) | active; resembles the router only in look and feel |

Codex also found a chat component in platform's app-runner, a terminal chat in deepagent, and per-framework UI
packages in `cchifor/forge` (`@forge/canvas-vue`, `@forge/canvas-svelte`, a Dart one; alpha).

**Platform already shares the router's core, rigorously.** `services/harness/scripts/sync-llm-router.mjs` works as
follows:
- two manifests list exactly which files are taken (server core and web core);
- the files are copied byte-identical at a pinned commit, with per-file SHA-256 in `UPSTREAM.json`;
- `--check` reports drift, and the router's conformance kit (U8) runs against the harness;
- the vendored copies are never edited: changes land upstream first.

The llm-router U1–U13 series exists to serve this. The router's boundary tests (`tests/library-boundary.test.ts`,
`tests/web-core-boundary.test.ts`) define what may be vendored. No CI enforced them until #81.

**Drift is real where code was copied without that discipline.** Relay's copy lacks the router's later fixes. Platform
and agentforge-platform already differ in how they cancel a stream (Codex).

## Decision

### A. Rules for dependencies between projects

1. **One owner per deliverable.** The repository that owns a service or a library builds, tests, publishes and
   releases it. No repository builds another repository's artifact.
2. **Depend on published, versioned artifacts.** Services by a versioned API. Images by digest. Code by a version, or
   by a pinned-commit vendored copy with integrity checks (rule B4). Never on another repository's branch, and never
   on an unrecorded paste.
3. **Integrate through contracts.** Capability endpoints, conformance kits, and contract tests that run in the CI of
   both sides. A pin is not a compatibility guarantee: a client and a server that deploy separately need a protocol
   version and compatibility tests.
4. **Cross-project change flow.** If project A needs something changed in project B, A opens a PR in B, B releases,
   and A moves its pin. A never patches B's code or B's release path.
5. **Pin moves are reviewed PRs**, automated where possible. They replace deploying projects in lockstep.
6. **Copies only as declared forks.** A fork records where it came from (source repository, commit, date), who owns
   it, and its sync policy (`none` is valid). The fork's owner decides whether an upstream fix applies.
7. **Estate repositories live in the `cchifor` org**, each with its owner recorded. Personal namespaces are for
   scratch work. llm-router moved to `cchifor/llm-router` on 2026-10-03. The old URL redirects for the web, fetch and
   push, and `dsh` keeps write access.
8. **Release from tags, or a release branch for an urgent fix**, while a multi-PR series is in flight. A releasable
   trunk is the goal, not today's assumption.

### B. The Assistant

1. **Shared:** the framework-agnostic Assistant core.
   - The router chat wire: the types, and their meaning.
   - The server core (U1): kernel, contracts, agent-core, chat-api, the model loop, the brief's dismissals.
   - The web core (U12): SSE parsing with keepalive and CRLF tolerance, event upsert, drafts, inline and markdown
     tokenizing, the link rule, the brief helpers.
2. **Not shared now:** UI components and views. Each product keeps its own UI on its own framework, because
   products need specific behaviour:
   - the router's React page;
   - Relay's React UI, now an owned fork (rule A6);
   - platform's Vue port;
   - trueswarm-admin's Svelte UI.

   Not chosen: a shared-components repository, a cross-framework widget (web components), a component package per
   framework.
3. **Owner:** llm-router, with a consumer reviewer (platform) required on changes to the core's paths. Framework-free
   is not the same as product-neutral: the core must not quietly encode router-only assumptions. The boundary is the
   one llm-router's boundary tests define, and #81 gates it in CI.
4. **Distribution now:** pinned-commit vendoring with the platform tooling, which this ADR adopts as the estate rule
   (platform ADR-022). Every pin move records its validation: the conformance kit, the boundary and wire-equality
   tests, the consumer's suite.
5. **Protocol:** the router chat wire gets an explicit version and a changelog of additive and breaking changes.
   AG-UI (agentforge-platform) and trueswarm-admin's protocol stay independent, with adapters at the boundaries. No
   forced convergence.
6. **Revisit when one of these happens:**
   - Share UI components: two *active* products on the same framework keep implementing the same behaviour or fix.
   - Versioned package instead of vendoring: manual re-syncs cause conflicts or missed fixes, or a third consumer
     vendors the core.
   - A separate repository for the core: consumers are blocked by llm-router's pace, and a maintainer is assigned.
   - One protocol: a real need for interchangeable backends.

### C. Actions

- Done (2026-10-03):
  - llm-router moved to the org;
  - CI for every PR and push to `main` (#81), with two tracked non-blocking checks: #79 (architecture rule) and #80
    (a flaky browser test).
- Next:
  - llm-router publishes its own image from tags (`images.yml`) with a push user scoped to `llm-router/**`
    (the `registry_zot_scoped_push_users` pattern). After one release through that path, Relay drops the router
    entry from its pipeline.
  - Relay: hand-off issue (vendor the web core in place of its copied logic, keep its views as an owned fork, triage
    three bugs fixed in the router).
  - Platform: notice of the new upstream URL.
  - A version field on the router chat wire.

## Consequences

- **Positive:**
  - A router fix ships from the router's own repository.
  - The products keep their own UIs, so their product needs don't collide.
  - The logic where drift hurts has one owner and an integrity-checked distribution.
  - No package registry or shared-components repository to run yet.
- **Negative:**
  - Bugs in views get fixed once per product.
  - Vendoring means consumers move pins by hand until a trigger in B6 fires.
- **Risks and mitigations:**
  - The router's application model becoming an estate dependency: the consumer reviewer and the documented boundary.
  - Pins mistaken for compatibility: the wire version and compatibility tests.
  - Fixes stranded in copies or old pins: the consumers' `--check` in CI and a sync cadence.
