# Retain the approved conductor validation image

## Authority and finding

The user explicitly selected **Add durable image retention**: one 16 MiB idle, network-isolated container and an exact-name cleanup exemption on the existing validator, `ci-runner-1` / `192.168.0.14`. This does not authorize additional inference or change validation policy.

Enabled conductor preflight refused because the exact image was absent. SSH and pinned helper/policy hashes were intact; no validation containers remained. Restoring the checksum-verified root-only archive restored enabled preflight. This establishes image absence, not which historical process deleted it. The shared runner's configured image pruning and stale-container reaping can remove an unreferenced image again.

## Narrow design

- Reserve `dsh-conductor-image-pin`, with an ownership label, using the **existing exact image ID**. Sleep only; no validation, repository mount, model work, network, published ports or capabilities.
- Limit it to UID/GID65532, read-only root, no-new-privileges, 16 MiB memory/equal swap, 0.01 CPU and four PIDs.
- Preserve a running image reference across Docker restarts. A oneshot systemd unit reconciles the declared container at host boot and explicit operator application.
- Refuse a reserved-name collision or isolation drift without deleting it. Restore a missing image only from bounded, root-owned, no-follow archive bytes whose checksum matches the approved archive; feed those same verified bytes to Docker.
- Extend the cleanup exemption only for the exact space-prefixed `/dsh-conductor-image-pin` name at end of metadata. Existing buildkit/buildx exceptions remain. **Ordinary `dsh-validation-*` containers remain eligible for cleanup.** Do not modify pruning windows, disk-pressure behavior, runner services, SSH grants, validator helper or policy.
- Host variables preserve this one exemption on future normal runner reconciliation. The focused playbook refuses any other host/IP and refuses unknown pre-existing cleanup exemptions rather than overwriting them.

## Application and verification

After independent external review/merge, apply only `ansible/dsh-validator-pin.yml` through the existing approved operator SSH identity. The playbook does not rerun broader runner roles. Verify exact live resource/security settings, idempotent reconciliation, systemd enablement and the one cleanup key; do not trigger a destructive fleet cleanup to test retention. Then repeat the actual enabled DSH preflight, preserving zero model calls.

This retention PR is separated from the GUI activation PR so it cannot restart the GUI. The checksummed archive remains on this VM only; this is not off-host backup, not least-privilege sudo, and not Kata isolation.

## Rollback

Keep the image/archive and all conductor databases. Withdraw the dedicated playbook's desired state, disable the image-pin unit, and stop/remove only the verified owned `dsh-conductor-image-pin` container. Restore only this added cleanup alternative while preserving other exemptions, and remove the host-variable override if it contains no other settings. Restore the known original key `buildkit|buildx` only after checking no later operator changes exist. Do not prune images, delete validation history, or change the approved helper/policy.

Status: prepared; no retention container or cleanup exemption deployed yet.
