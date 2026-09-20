# Command-only transcript visibility: deployment plan

## Evidence and scope

Real Playwright against the existing DSH0.1.5-alpha.2 GUI reproduced the defect in a new workspace: `/conductor status` returned HTTP200 and live `command/run`/`command/done`, but the welcome screen hid the command-only transcript. A browser-only change to `chatViewDefinition.isActive` passed initial visibility, reload, default status alias and unknown-verb rejection, without an ordinary chat prompt or conductor task.

Change only the chat target activation predicate to include existing command nodes. Keep host blank metadata, prompt lifecycle and all conductor controls unchanged. This intentionally makes command-only content visible, including other command outcomes; it does not imply a model turn. Host blank-session reuse/title behavior remains unchanged and must not be represented as fixed.

## Durable artifact deployment

Use a deployment-owned, version-and-hash-pinned artifact rebuild in a non-root init container:

1. Mount the installed app PVC read-only and verify DSH/build/package identity.
2. Read the exact original Web client bundle and verify SHA256 `3891fc589652c50be24c6d8e68d35c5d4066ed9ae7b7d57a086d5a34ad20c0d9`.
3. Require exactly one occurrence of the reviewed activation predicate. Rebuild that artifact with the proven replacement; syntax-check without execution and verify output SHA256 `9f53814b335eaa3f892fd70e8a22f167b4ba6fef412c598309a17d001bcf847c` (369972bytes).
4. Publish only to a dedicated emptyDir; mount its `client.js` read-only over the exact runtime package file using subPath.
5. Version/source mismatches fail closed with an actionable diagnostic. An upstream version bump must remove or rebase this explicitly versioned hotfix; tests must detect pin/mount drift.

This rebuilds the affected served Web artifact on each pod creation without modifying the original install tree, ps1, ps2, credentials, or durable histories. It does not require an HMR watcher, browser interception, a replacement server, or a full shell rebuild: the unchanged host serves the rebuilt client plugin after a normal Recreate rollout and browser refresh. The original upstream artifact remains intact on the PVC for rollback.

## Validation and release gates

- Internal plan review before implementation, then internal implementation review.
- Unit tests for unique replacement, malformed/source/version mismatch, deterministic output, publication and manifest wiring; real installed-source rebuild and syntax verification.
- Render Kustomize and server-side dry-run the affected Deployment/ConfigMap. Never apply the desired Deployment out of band.
- Two independent external reviews and all required CI; external maintainer merge only.
- Before rollout, inspect the production ledger and coordinate if a human has started a run. Never start/pause/resume/acknowledge a task for testing.
- Independent rollout observation must verify new pod readiness and the exact rebuilt served artifact, original source/ps1 preservation and zero validation-created runs/calls.
- After rollout, run the real Playwright test with **no JavaScript response interception**, including first status in a new workspace and reload. Delete test-created workspace registrations and empty directories afterward; retain only audit/session histories as appropriate.

## Rollback

Externally reviewed GitOps revert removes only this init container, generated ConfigMap reference and file overlay. The unchanged original package becomes visible again. Do not undo PR796/798, uninstall the conductor, reset budgets, delete state or weaken authentication/review/health gates. Recreate causes a bounded GUI interruption; protect active user work.

## Current status

Internal plan review approved (agent c20aab95), with adjustments accepted: init immediately after wait-for-install; .installed and version/hash guards; version/build derived through existing replacements; dedicated bounded emptyDir and non-root resources; separate ConfigMap, no relay/conductor edits. The new init masks the default service-account path with an empty read-only volume because pod-wide automount remains enabled for the existing operator container. Client artifact revisions are content-derived (dsh-client-modules artifactRevision), so rebuilt bytes receive a new immutable-cache URL. No profile closure shadows this package. Upgrades must remove/rebase the hotfix in the same-or-earlier PR. Implementation and native mount tests completed; no production changes yet.

### Pre-merge verification

- Internal implementation review bf9a2f9d: no blockers after reviewing the final guard, mount wiring and non-intercepting browser test.
- Full GitOps Python suite: 923 tests, 922 passed / 1 existing skip. The hotfix's six Python checks also execute its Node suite: 11 passed, zero skipped.
- Rebuilt the actual installed artifact locally: exact pinned output hash and 369972bytes, original unchanged.
- Native probe `dsh-command-ui-probe-970dfb43a95e` exposed a CLI-entry bug: ConfigMap paths are symlinks, so comparing import.meta.url to unresolved argv silently skipped the builder. Corrected to realpathSync(argv[1]) and added a regression test that fails the old guard.
- Corrected native probe `dsh-command-ui-probe-5cec14271997` completed: overlaid hash correct, original PVC source hash unchanged, API token absent, no server or model calls. The probe uses the actual manifest's mounts and builder; its ConfigMap is owned by the TTL-bounded Job.
- Kustomize rendered and server-side dry-run admitted the exact ConfigMap/Deployment (`dsh-command-ui-fix-bk5h5th7hm`). No live Deployment apply was performed.
- Checked-in Playwright regression under `scripts/browser/dsh-command-ui-visibility` failed on the unfixed live GUI as expected (first command visibility after12seconds), and automatically removed its unique workspace, verified after refresh. No interception is available in this test.

External reviews, required CI, external merge, rollout and unmodified-browser post-deploy verification are still required.
