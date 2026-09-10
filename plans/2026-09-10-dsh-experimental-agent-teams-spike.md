# Spike — the experimental Agent Teams packages

**Date:** 2026-09-10 · **Verdict: GO — they work here** · **Recommendation: evaluate, do not auto-adopt**

## Why this exists: a miss

The original analysis concluded there was no upstream `dsh-agent-teams` and that agent teams were
ours to build as a preset root. That conclusion was **wrong**. Two first-party packages exist and
became installable in `0.1.5-alpha.2` — the exact version this estate runs:

- `@deepseek-ai/dsh-experimental-agent-team` — *"Implicit-root Agent Teams roster, durable peer
  mailbox, and shared task DAG"*
- `@deepseek-ai/dsh-experimental-tool-agent-team` — the nine model-facing tools

They were missed because the search probed plausible npm names (`dsh-agent-teams`,
`dsh-agent-team`, `dsh-teams`, `dsh-tool-agent-team` — all 404) and npm's registry search returns
nothing for `deepseek team`. The `dsh-experimental-*` prefix was never guessed. A codex review of an
unrelated question found them by reading the upstream repository instead.

## What they are, and how they differ from what was built

| | preset-composed teams (merged) | experimental Agent Teams |
|---|---|---|
| what a "team" is | a **tool-set**: which delegation tools a session sees | a **roster** of named members with a Lead |
| members | anonymous children of one agent | named, permanent, addressable (`reviewer`, `impl`) |
| communication | parent ↔ child result only | **durable peer mailbox**, any member to any member |
| coordination | none | **shared task board** (DAG) with owner + revision checks |
| survives a crash | no | **yes** — queued messages delivered when a member resumes |
| config | per-row | `maxMembers: 8`, `maxTasks: 256`, `maxPendingMessagesPerMember: 64` |

The merged work is **not wasted** — the preset root, the profile-plugin install path, the bounds and
the codex routes are prerequisites either way. But these deliver something the preset approach
cannot: members that persist, address each other, and coordinate on shared state.

## Spike result

Run in a `node:22` container reproducing the production layout.

```
PASS  dump-config exit 0
PASS  dump-config stderr EMPTY
PASS  team rows present in the resolved tree
PASS  host booted and stayed up 40s with Agent Teams mounted, stderr empty
TEAM SPIKE: GO
```

### Four facts the adoption depends on

1. **They are NOT Profile Bundles.** Neither declares `dsh.bundle`, so they never join
   `dsh.profile.bundles` — verified. They mount as ordinary composition rows, named from a preset.
   The install path already built for the codex bundle handles them unchanged: `pnpm add` into the
   profile closure makes a bare specifier resolve, and `reconcile-bundles.js` correctly leaves them
   out of the bundles list because `declaresBundle()` is false for them.
2. **Do NOT add `dsh-session-persistence-jsonl`.** The README's "smallest working setup" assumes a
   bare composition and lists it; the **web profile already ships it**, and inserting it again is a
   duplicate loader entry id that fails the boot outright:
   `duplicate loader entry id: session-persistence-jsonl`. Cost an iteration here; would cost an
   outage in production.
3. **`koffi` is a red herring.** `pnpm add` reports `ERR_PNPM_IGNORED_BUILDS` for `koffi@3.2.1` and
   exits non-zero **after completing the install**. It arrives transitively via
   `session-persistence-jsonl`, which the profile does not need — installing only the two team
   packages removes it. Note dsh's own `initProfile` already writes `onlyBuiltDependencies: [koffi]`
   and pnpm 12.3.4 gates it anyway, so the allowlist is not the lever it looks like.
4. **Adoption is small**: add both packages to `DSH_PLUGINS`, bump `DSH_PLUGINSET`, add two rows to a
   team preset. No new machinery.

## What to weigh before adopting

**One process, one shared checkout.** From the package's own limitations: *"members share cwd and
observe edits immediately; this package provides no worktree, remote member, merge, or filesystem
lock"*, and write scopes are **advisory** — *"Bash, formatters, code generators, and direct external
writers can bypass filesystem version checks"*.

That collides head-on with **WP-6's single-writer contract**, which exists because children run in
the parent's cwd and nothing stops two writers. Agent Teams does not solve that problem; it makes it
*more likely* by giving several members concurrent reason to write. The task board's owner and
revision checks are coordination, not enforcement.

**Other stated limits:** flat immutable roster (only the Lead creates teammates; no nesting, rename,
deletion or name reuse), and **no automatic ownership release** — idle, interruption, process exit
and failed work all leave a task owner in place.

**Experimental.** Published under an experimental name, explicitly *"carries no stability promise"*.
It moved `0.1.5-alpha.2` → `rc.1` → `rc.2` already.

## Recommendation

**Evaluate against a concrete use, do not auto-adopt.** It is cheap to install and it boots, but it
buys durable multi-member coordination at the cost of several members writing one checkout — the
exact hazard WP-6 was written to contain. The honest sequence:

1. Decide whether any real workload here needs *named, durable, mutually-addressable* members. If
   the answer is "we want a reviewer opinion", the merged `team-review` already does that more
   cheaply and with one writer.
2. If yes, resolve the write-ownership question **first** — either single-writer by convention with
   the task board as the coordination record, or accept concurrent writes and add a real review gate
   on the final diff.
3. Only then wire it, with `maxMembers` set well below the default 8.

## Not proven here

The tools were not exercised — no teammate was spawned, no message sent, no task created. The spike
establishes that the packages **install, resolve and boot** in this layout, not that the team
workflow behaves. That needs a live session with a model behind it.
