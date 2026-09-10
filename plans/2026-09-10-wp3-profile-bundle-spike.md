# WP-3 spike result — profile-bundle packaging for dsh

**Date:** 2026-09-10 · **Verdict: GO on Option B** · Plan:
`plans/2026-09-10-dsh-agent-teams-and-cross-framework-delegation-plan.md` (merged, `481607b`)

Run off-cluster in a `mirror.gcr.io/library/node:22` container reproducing the production layout —
`/app/0.1.5-alpha.2-glibc` as the install tree, `/dsh-home/profiles/web` as the profile. No cluster
access was needed or used.

---

## Verdicts against the plan's go/no-go criteria

| criterion | result |
|---|---|
| Provider rows resolve; `--dump-config` clean, **stderr read** | **PASS** — exit 0, **0 bytes stderr**, `subagent-codex` and `subagent-claude-code` both present |
| **Host boots and stays up** (not just a clean dump) | **PASS** — `dsh web` ran to the 30 s timeout and printed its launch token; stderr empty |
| Single-instance resolution of shared peers | **PASS** — exactly **one** on-disk copy of `@deepseek-ai/cordis`, `dsh-subagent`, `dsh-llm`, `dsh-session`. Two instances are impossible when only one exists |
| Platform payloads executable | **PASS** — see below |
| Interaction with the supported CLI | **Characterised** — see "What `dsh plugin` owns" |

## F3a confirmed empirically — this was worth proving

The plan's install design (WP-3 rules 1–5) rests on the claim that a declared-but-absent bundle
crash-loops the pod rather than degrading. It does:

```
$ dsh web            # with @deepseek-ai/dsh-subagent-codex declared but not installed
Error: dsh: cannot resolve profile bundle "@deepseek-ai/dsh-subagent-codex" from the dsh
installation or /dsh-home/profiles/web; run 'dsh plugin --profile web install' if its
dependency is not installed
    at resolveBundleDir (.../dsh-app-boot/lib/index.js:831:8)
exit 1
```

An unhandled throw at boot. In a 1-replica `Recreate` Deployment that is an outage, not a degraded
feature. **Rules 1–5 stay exactly as written**, and rule 2 (the plugin marker is advisory, never
blocking) is what keeps a failed install from being worse than no install.

## Two plan assumptions that were wrong, both in our favour

**1. `nodeLinker: hoisted` is already the default.** The plan proposed setting it to get a
relocatable `node_modules` instead of pnpm's symlink farm. `initProfile` already writes it:

```yaml
packages: [.]
nodeLinker: hoisted
autoInstallPeers: false
minimumReleaseAgeExclude: [...]
```

The staged tree had **3 symlinks total** and copied with plain `cp -a`. The relocation hazard the
spike existed to probe is not there.

**2. `dsh plugin` is much cheaper than assumed.** The supported route installed 107 packages in
**4.3 s** and reconciled `dsh.profile.bundles` by itself. Option A's "pod startup becomes
registry-dependent" objection is about *availability*, not minutes.

## What `dsh plugin` owns that hand-staging must reproduce

`pnpm-workspace.yaml` carries a **`minimumReleaseAgeExclude`** list, and `dsh plugin add` appends
each package it installs:

```yaml
minimumReleaseAgeExclude:
  - '@deepseek-ai/dsh-brand@0.1.5-alpha.2'
  - '@deepseek-ai/dsh-sdk-protocol@0.1.5-alpha.2'
  - '@deepseek-ai/dsh-subagent-claude-code@0.1.5-alpha.2'
  - '@deepseek-ai/dsh-subagent-codex@0.1.5-alpha.2'
```

pnpm can refuse a package below a minimum release age. These are `-alpha` builds, so **Option B must
copy the profile's `pnpm-workspace.yaml` verbatim into the staging directory and maintain that list**
— this spike did, and the install succeeded. Hand-rolling the file instead would work today and fail
on the next alpha bump, silently and confusingly. Added to WP-3's implementation notes.

## Platform payloads

| payload | result |
|---|---|
| `@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex` | **`codex-cli 0.153.4`** — static-pie ELF, runs on glibc |
| `@openai/codex/bin/codex.js` (node entrypoint) | `codex-cli 0.153.4` |
| `@anthropic-ai/claude-agent-sdk-linux-x64/claude` | **`2.1.263 (Claude Code)`** — 215 MB, dynamically linked |

**The bundled codex is exactly the estate's pinned version.** ADR 0019 finding (b) records
`gpt-6-astra` requiring codex ≥ 0.153.0 (0.152.1 rejected) with the sandbox images on 0.153.4. The
bundle ships 0.153.4, so a delegation would run a CLI the estate already entitles — one fewer
version to reconcile.

## The cost nobody costed: 566 MB onto an RWO local-path PVC

The closure is **566 MB / 98 top-level entries**, and the `claude` payload alone is **215 MB**.

That lands on `dsh-home` — **RWO, `local-path`, node-local disk on talos-cp1**. It is not on the RWX
NFS volume where the dsh install tree lives. Consequences for WP-3:

- Check free space on the pinned node before merging. Nothing in the plan or the manifests bounds
  this volume's growth.
- Option B's content-hashed staging keeps the **last two** closures — that is ~1.1 GB on `/app`
  (RWX NFS, cheap) plus one projected copy on `dsh-home` (expensive). Retention matters more than
  the plan implied.
- A projection is a 566 MB copy at pod start. Measure it against the `wait-for-install` budget.

## Recommendation

**Option B, unchanged in shape, with two amendments:** copy `pnpm-workspace.yaml` verbatim and carry
`minimumReleaseAgeExclude`; and treat the 566 MB projection onto RWO local-path as a sizing item with
an explicit retention policy.

Option A stays the documented fallback and is now known to be *fast*; its cost was always
availability (registry reachable at every pod start) and supply chain (lifecycle scripts in the pod's
namespace), and neither changed.

## Not proven here

- **A real delegation end-to-end.** The payloads execute and the providers mount, but no child was
  driven against a stub upstream. That belongs with WP-4's LiteLLM wire probes, which need the
  gateway.
- **Runtime registration via the host API.** `/api/*` returned 401 — the launch token mints a cookie
  on `GET /`, which this spike did not drive. The evidence for mounting is the clean boot plus the
  resolved rows; a row that throws fails boot, so a silent non-registration is not consistent with
  what was observed. Worth closing properly when the cluster is reachable.

## Reproduce

`scripts/spike-dsh-profile-bundles.sh` in this branch runs the whole thing in one container.
