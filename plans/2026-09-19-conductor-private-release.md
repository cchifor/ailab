# Private conductor 0.1.0 deployment plan

**Activation update:** the user has since approved enabled production with a separate 40-call / 40M-token allowance. [CONDUCTOR-ACTIVATION.md](CONDUCTOR-ACTIVATION.md) supersedes disabled-only assumptions and binary-in-GitOps ownership below (the user approved a separately provisioned immutable artifact ConfigMap); retain this original plan as preparation history. No task is authorized merely by activation.

Status: release published; deployment changes prepared but not applied. Clean native acceptance passed (26/28 calls, PR #7 externally merged as `cd1fbc192309df160076fe9af070286618b40cac` after two exact-head approvals and CI). Private Gitea v0.1.0 is published with SHA256 `7a97432202131d79f11bb578708f2a3df32cde65700a28c778ca958ad4bdcb28`. Downloaded assets passed real DSH install/disable/removal checks and the named-file alias was verified with pnpm 12.4.1. No deployment PR, staged-slot mutation or live-profile change yet.

## Authority and completed prerequisites

- Product PR #6 externally merged as `eb7356bbc69272cf47c97cb5def830da7f0f6167` after two exact-head external approvals and required CI. Product local/isolated suites passed 215 tests; actual native negative tests passed.
- The reviewed package passed actual DSH CLI install, configured-disabled boot, disable, removal and state-hash preservation with both pnpm 10.11.0 and the existing installer's pnpm 12.4.1. These fixture checks made zero model calls.
- `cchifor/ailab` is private; the existing dsh identity has pull-only access. The user explicitly approved a private fork PR. `dsh/ailab` was verified private and forked from this repository. Use an externally merged PR, never an out-of-band override of Flux-managed objects.
- This worktree was created separately from origin/main `b024f725`; the unrelated existing `ansible/roles/pr_reviewer/defaults/main.yml` modification in another worktree must remain untouched.
- The first new acceptance timed out in plan review after 12 calls. The user approved one separate retry with the remaining 28 calls, <=45-minute stages and <=two-hour total execution. Original 80 and failed 12 reservations remain preserved. No publication without a successful unassisted loop.

## Artifact and durable installation

1. Publish/download the final checksummed **private Gitea release** artifact after acceptance. `private:true` remains in package.json. There is no registry publication and `dsh-team-conductor@0.1.0` is NOT a valid registry-install plan.
2. Verify the downloaded bytes against release provenance. Include those exact small (~55KB) bytes in a generated binary ConfigMap in this private GitOps repository, with the complete 64-character SHA256 pinned. No Gitea credentials need to be provisioned to the installer. Never select an old `dist/*.tgz` or an agent-reported preview hash.
3. Mount the artifact and `stage-private-conductor.mjs` read-only in the installer. The helper verifies one bounded descriptor-read buffer, publishes that same buffer using an exclusive temporary file and atomic no-clobber link, and refuses corrupt/linked existing outputs. The destination is `/app/conductor-artifacts/<full-sha>/dsh-team-conductor-0.1.0.tgz`.
4. Preserve **ps1** unchanged. Update the installer Job name and `DSH_PLUGINSET` to a distinct new slot (ps2 if still unused at implementation). Use the named local dependency `dsh-team-conductor@file:/app/conductor-artifacts/<full-sha>/dsh-team-conductor-0.1.0.tgz` alongside the existing pinned Codex provider. Keep the existing staging success/failure signal and verify-before-marker behavior. Verify the named file alias with the actual pnpm version before applying.
5. Extend the authoritative installer/kustomization sources, not merely the live Web profile. `seed-settings` replaces the profile closure and `reconcile-bundles.js` rebuilds its bundle list on boot; a manual live-profile installation is erased. Preserve existing profile rows and providers.

## Initial configuration and readiness

- Initial deployment is **configured but disabled**. No production task or standing model spending authority is implied by acceptance approval. Keep restrictive example limits until the operator explicitly configures production budgets and enables execution.
- Supply an absolute trusted configuration file through an authoritative read-only mount. Put SQLite/state outside model workspaces on durable storage. Do not reuse acceptance databases or their run namespaces.
- Live inspection confirms one replica with Recreate strategy, persistent `dsh-home`, `dsh-workspace` and RWX app volumes. Verify current init-container behavior before relying on workspace persistence; older global notes describe an empty workspace.
- Add a narrowly scoped in-process readiness observer, not a second coordinator or another GUI server. It must inject the existing `teamConductor`, require the actual installed class/version, call non-model preflight, and record only bounded readiness metadata. No global LLM veto or appExit from a live observer.
- Cordis 4.0.2 source confirms strict `ctx.get()` only returns a service whose provider fiber is ACTIVE (`src/reflect.ts:237-243`), after async initialization is awaited (`src/fiber.ts:646-672`). Required injection can therefore observe the initialized provider; mere constructor registration is insufficient.
- Copy any observer module into the profile so package resolution uses its installed closure. Extend the authoritative boot patch writer, not its overwritten output. Bind readiness to the current Pod UID and a per-observer nonce; remove only its own marker on disposal and clear stale readiness on boot. Do not treat an old persistent marker or HTTP 200 alone as proof of the new service.
- Keep the existing GUI's health behavior intact; an optional-addon failure must not silently be reported as conductor readiness. Verify the existing `http://127.0.0.1:3080` after refresh. No replacement server or HMR promise.

## Review, activation and rollback

- Before submitting the deployment PR, replace every pending artifact/provenance reference with verified final values, run targeted tests plus manifest rendering, and perform server-side dry runs. Respect upstream review/CI policy; protection inspection currently returns 403 and must not be bypassed.
- Arrange independent post-rollout observation and durable rollback instructions before any GUI restart that can interrupt the operator session. The current completion goal is paused/disarmed; automatic continuation requires user action in the GUI.
- Verify staged closure identity, initialized disabled service, non-model preflight, empty initial task state, current-Pod readiness, and existing GUI availability. State inspection must not create another enabled coordinator.
- Roll back slot selection to preserved ps1 through an authorized GitOps change, disable/remove only the conductor layer/config activation, and preserve SQLite, reservations, uncertainty and evidence. Never delete or refund state to make rollback appear successful.

## Rollout hold and rollback checklist

Do not open a mergeable deployment PR until independent observation is armed: external reviewers may merge immediately after approval, and Flux can then restart this very GUI. A branch push alone is not activation. The completion goal is still paused; resume it using the GUI control before relying on autonomous follow-up.

Before requesting merge:
1. Fetch current upstream main, rebase/resolve only this change, rerun tests and rendering, and save the exact upstream base and deployment commit.
2. Record current Deployment/Pod UID, image identity, ps1 `.installed`/`.specs` and closure metadata, PVC identities and existing GUI response. Do not print credentials or launch tokens.
3. Prepare a separate bounded Kubernetes/API observer able to survive this process restart. It must verify the new Pod UID, ps2 staged closure, current-process readiness marker, disabled configuration, empty task state and existing GUI; TCP availability alone is insufficient. Its failure must not be reported as success.
4. Confirm an external merger can handle an emergency rollback PR. No agent self-merge, protection bypass or direct mutation of Flux-owned manifests.

Rollback, if needed:
- Fetch the actual externally merged deployment commit and inspect its parents. From current upstream main, create a private-fork rollback branch and revert the deployment change (use `git revert -m 1` only for a verified merge commit, not blindly for a squash commit).
- Review the diff: slot selection returns to ps1, conductor configuration activation is removed, and the previous profile patch is restored. Do not remove `/app/profiles/...-ps1`, SQLite/WAL, workspaces or evidence. No acknowledgement/refund is implied.
- Submit the rollback PR and obtain external review/merge under existing policy; watch Flux restore the prior closure and verify the original GUI. Keep the conductor disabled while any effects are uncertain.
- The downloaded-package fixture already proved package/layer removal with an unchanged SQLite hash. That is fixture rollback evidence, **not** a claim that production rollout/rollback has been exercised.

## Implemented preparation

The actual installed-service readiness fixture passed real Gitea and SSH-validator preflight with zero inference, empty task state and marker removal on disposal. A first failure exposed the missing SQLite parent directory; the fixture and authoritative seed now create it without deleting state. The observer uses nested injection: a required top-level dependency can make optional-addon absence fatal in DSH's profile-entry activation assertion.

`stage-private-conductor.mjs` and its Python/Node execution tests are preparatory code, not deployment completion. Parent review replaced unsafe hash/re-read, unbounded-read and rename behavior from the initial agent draft. Tests cover actual inert import, projected-script entry, FIFO/nonregular bounds, corrupt/symlink/hardlink refusal, and a deterministic real-file race immediately before the link syscall. Directory checks assume operator-owned ancestry and are not a hostile concurrent-directory sandbox.
