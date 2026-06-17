# Implementation review — dev-workers — finalized

<!-- codex-impl-review-status: finalized -->

## Outcome

Codex reviewed the dev-workers implementation (tofu module + `dev_worker` Ansible role) against the
finalized plan over two rounds. **No source changes were required.**

- **Round 1** raised two elevated findings; Opus pushed back on both as false positives.
- **Round 2** Codex re-examined both against the actual files and **DROPPED both** — agreeing they
  were non-issues.

### Round-1 findings and resolutions

| # | Finding | Severity (round 1) | Resolution |
|---|---|---|---|
| 1 | Caddyfile.j2 `redir https://{host}{uri}` "needs double braces" | blocker | **Dropped.** Caddy v2 Caddyfile request placeholders are single-brace; double braces are only for the `templates` directive. Also `.j2` is Jinja2 — `{{host}}` would break templating. Single-brace is correct + Jinja-safe (matches the deployed homelab Caddyfile). |
| 2 | restic-env.j2 "folded scalar" | nit | **No change** (misattribution). `restic-env.j2` is already the one-line `RESTIC_REPOSITORY={{ dev_worker_restic_repo }}`; the `>-` is in `restic.yml`'s `set_fact`, where folding is correct. |
| 3 | claude-job-timer.j2 "no trigger if calendar+boot_sec+active_sec omitted" | important | **Dropped.** The `{% else %}` branch uses `default('15min')`/`default('6h')`, so a valid trigger is always emitted. |
| 4 | tofu image filename date suffix | nit | **No change.** Intentional — matches the runners module's dated-pin convention. |
| 5 | per-user npm install relies on binary probe | nit | **No change** (codex agreed none required). Self-healing re-install of a missing CLI is desired. |
| 6 | `/dev/sdb` hardcoded | nit | **No change.** `community.general.filesystem` (force:false) never reformats an existing fs and errors clearly if absent; the disk is tofu-pinned at scsi1. |

### Confirmations (round 1)

Codex explicitly validated as correct: memory ballooning (24/6 GiB), SOPS creation_rule first-placement,
systemd `%%` date escaping, the swap `mkswap` guard, `/workspace`-before-docker ordering, the credential
ACL recursion model, ufw allow-before-enable ordering, docker data-root on `/workspace`, the
data-driven jobs loop, tmux persistence conditional, and the intentional non-recreation of the
cloud-init `c4` user.

## Verification performed before commit

- `terraform fmt -check -recursive` on the module: clean.
- `terraform validate` (after `init -backend=false`): **Success** (only deprecation warnings on
  `proxmox_virtual_environment_download_file`, identical to the existing runners module).
- All 31 new/edited YAML files parse (`yaml.safe_load_all`).
- One latent bug found + fixed during self-review (`jobs.yml` seed-files var collision).
