# Implementation review — dev-workers — round 1

<!-- codex-impl-review-status: complete -->

## Summary

Overall execution is strong, with excellent Ansible idiom fidelity and careful porting from the homelab bootstrap. The tofu module correctly models the dual-disk design and cloud-init seeding. Codex raised one "blocker" (Caddyfile braces) and one "important" (job-timer trigger); both are false positives (see pushbacks). The remaining findings are confirmations of correctness or low-value nits.

## Findings

### Caddyfile single-brace template syntax (line 12)
**Location:** `ansible/roles/dev_worker/templates/Caddyfile.j2:12`
**Severity:** blocker
<!-- codex: Line 12 has `redir https://{host}{uri} permanent` — but Caddy directives require *double* braces `{{host}}` and `{{uri}}`. This will be parsed as literal `{host}` and `{uri}` strings and redirect to a malformed URL. Change to `redir https://{{host}}{{uri}} permanent`. -->
<!-- opus-pushback: False positive. Caddy v2 Caddyfile request placeholders are SINGLE-brace (`{host}`, `{uri}`); double braces are only for the `templates` (Go-template) directive, not `redir`. The homelab Caddyfile (deployed, working) uses single braces. Moreover this is a Jinja2 `.j2`: `{{host}}` would make Jinja render an undefined `host` var and break templating/produce `redir https:// permanent`. Single-brace is both correct Caddy syntax and Jinja-safe. Keeping as-is. -->

### restic-env.j2 uses folded scalar without newline escape
**Location:** `ansible/roles/dev_worker/templates/restic-env.j2:2`
**Severity:** nit
<!-- codex: The file uses a Jinja2 folded scalar (`>-`) on line 2 ... Consider replacing the entire line with `RESTIC_REPOSITORY={{ dev_worker_restic_repo }}` on one line for clarity. -->
<!-- opus-pushback: Misattributed. restic-env.j2 line 2 is ALREADY the clean one-line `RESTIC_REPOSITORY={{ dev_worker_restic_repo }}` codex suggests. The `>-` folded scalar is in tasks/restic.yml's set_fact (computing the repo URL from a multi-line if/else), where folding to a single line is correct and intended. No change. -->

### claude-job-timer does not guard missing `calendar` property
**Location:** `ansible/roles/dev_worker/templates/claude-job-timer.j2:6-11`
**Severity:** important
<!-- codex: ... If a job object omits `calendar` and also omits both `boot_sec` and `active_sec`, the timer block will have no trigger clauses at all ... the timer will never fire silently. -->
<!-- opus-pushback: False positive. The `{% else %}` branch is `OnBootSec={{ item.boot_sec | default('15min') }}` + `OnUnitActiveSec={{ item.active_sec | default('6h') }}` — the Jinja defaults guarantee a valid trigger even when calendar/boot_sec/active_sec are ALL omitted. There is no no-trigger path. No change. -->

### tofu module image filename uses date suffix (not semantic version)
**Location:** `kubernetes/infra/dev-workers/variables.tf:77`
**Severity:** nit
<!-- codex: ... a date suffix instead of a semantic version ... consider documenting why the date is pinned. -->
<!-- opus-note: Intentional — matches the runners module's dated-pin convention exactly (a comment already explains "pin a dated copy so a silent upstream re-publish of current doesn't change the base"). Keeping consistent with runners. -->

### Per-user npm-global install idempotency relies on binary probe
**Location:** `ansible/roles/dev_worker/tasks/claude_code.yml:26-47`, `tasks/codex.yml:6-27`
**Severity:** nit
<!-- codex: ... idempotent but fragile: if the binary is deleted, re-runs will re-install ... No change required. -->
<!-- opus-note: Accepted as-is (codex agrees no change required). Re-installing a missing binary is the desired self-healing behavior; the probe is the correct idempotency invariant for a runtime-self-updating CLI. -->

### workspace device hardcoded as /dev/sdb
**Location:** `ansible/roles/dev_worker/defaults/main.yml:14`, `tasks/workspace.yml:7`
**Severity:** nit
<!-- codex: ... relies on Proxmox + Ubuntu alignment ... add a safety check that validates /dev/sdb is a block device with no filesystem before mkfs. -->
<!-- opus-note: Low value — community.general.filesystem has force:false (never reformats an existing fs) and errors clearly if the device is absent; the disk is tofu-pinned at scsi1. The plan deliberately pins the device (vs homelab's probe) precisely because ailab VMs are tofu-defined. Leaving as-is. -->

## Diff stat
 (see git diff bbf81a6..HEAD --stat)
