# Upgrade Gitea 1.27.3 → 28.4.0 (chart 12.7.0, unchanged)

Status: prepared, rollout not scheduled · Created: 2026-11-16 · Procedure: `docs/runbooks/gitea-upgrade.md`
Follows from `plans/2026-11-08-gitea-1-27-upgrade-plan.md`, which dated the 28.x upgrade for 2026-12-08, with the
`[webhook] ALLOWED_HOST_LIST` move and a check of the `RUN_RETENTION_DAYS` interplay.

## Context

- **Versioning, not a backlog.** Gitea dropped the `1.` prefix: 28.0.0 (2026-09-29) is the release that would have been
  1.28.0. Since then: 28.1.0 (10-06), 28.2.0 (10-18), 28.3.0 (11-01), 28.4.0 (11-15). The 1.27 line ended at 1.27.3
  (08-29), so we get no more fixes on it. Every 28.x release carries security fixes: 28.0 (push validation, SSH key
  identification, fork-PR approval gate, x/crypto SSH DoS, repo-scoped team authorization), 28.2 (x/net, OpenID
  `state`), 28.3 (x/net, public-only token scope on more routes, context user access scope, missing access checks),
  28.4 (brotli, TOTP attempt limit, missing checks). Source: the GitHub release notes for each tag.
- **Chart.** 12.7.0 is still the newest chart at `https://dl.gitea.com/charts` (index generated 2026-10-27,
  appVersion 1.27.0). The image is pinned above it, as 1.27.3 was.
- **Live:** `docker.gitea.com/gitea:1.27.3-rootless`, chart 12.7.0, database `gitea` on CNPG `infra-pg` (PostgreSQL
  17.11), migration version 343.

## Findings

1. **Egress policy (28.0 BREAKING, go-gitea #39426; 28.1 #39560).** Outbound HTTP and git network operations go
   through a new egress policy. The `external` builtin is gone: `EGRESS_MODE = lax` (the default) allows every public
   target and denies non-public ones unless an IP entry allows them. `[webhook] ALLOWED_HOST_LIST` is deprecated in
   favour of `[security] ALLOWED_HOST_LIST`, still honoured as a fallback. With our current values, 28.4.0 logs two
   startup errors (seen in the smoke boot below), `Deprecation: config option [webhook].ALLOWED_HOST_LIST` and
   `ignores an invalid entry: the "external" builtin was replaced by EGRESS_MODE = lax`, but behaves the same.
   - Consumers in 28.4.0 source: webhooks (`[webhook]` key, falling back to `[security]`), OpenID sign-in (disabled
     here) and OAuth2 avatar fetches use the `[security]` list; mirrors, migrations and the new git proxy use the
     separate `[migrations]` list (empty: public only).
   - Live targets (rehearsal copy): two active org webhooks to the reviewer VMs `192.168.0.24/.25:8477`; push mirrors
     to `github.com` for `cchifor/platform` and `cchifor/ailab`; no pull mirrors; no webhook targets a ClusterIP
     (AgentForge is retired). OIDC discovery `sso.chifor.me` resolves to Cloudflare from the Gitea pod (public).
   - **The chart cannot remove the old key.** `init-app-ini` runs `gitea config edit-ini --apply-env` onto the
     existing `app.ini` on the data volume; it never deletes keys (tested with the 28.4.0 binary). The live file
     holds `[webhook] ALLOWED_HOST_LIST = external, 192.168.0.0/24, 10.96.0.0/12`, so dropping it from the values
     leaves it in effect.
2. **Run retention (28.0 BREAKING, #38855).** New `RUN_RETENTION_DAYS` (default 400) with `cron.cleanup_action_runs`
   at `@midnight` deletes completed runs in one pass. `0` now means "keep forever" for the log, artifact and run
   settings alike (ours are 14/14, unaffected). The oldest stored run is 2026-09-29, so the default would delete nothing
   today. Our paced, replication-gated `gitea-actions-run-retention` CronJob (16 days) already owns this, and an
   unpaced midnight sweep is the shape behind the 2026-09-28 replication loss.
3. **Actions API status strings (#39376).** A job held by `needs` gets the new status 9 (`pending`), reported as
   `requested`; `StatusBlocked` is now reported as `pending` instead of `waiting` (approval-gated runs stay `waiting`).
   `queued`, `in_progress` and `completed` are unchanged. Our consumers: the run-retention script (`completed`),
   `ci-rerun-watchdog` (`completed` versus anything else), `cloud-power` (`in_progress` jobs, runner
   `id/name/status/busy/disabled`, all still present). None of them matches on `waiting`.
   `scripts/ci-queue-stats.py` only prints the raw status as an outcome label, so a blocked job's label reads `pending`
   instead of `waiting` (cosmetic). The runbook's running-jobs SQL (status 6) is unchanged.
4. **API routes.** Every Actions route the estate calls exists in 28.4.0 (`routers/api/v1/api.go`): repo runs
   (list, get, `DELETE`, `POST .../rerun`, `.../rerun-failed-jobs`, `.../jobs`), and org runners (list, `PATCH`) and
   org jobs.
5. **Runners.** The protocol module moved from `actions-proto-go` v0.6.0 to `actionslib` v1.2.1, with the same RPC
   service (`runner.v1.RunnerService`: Register, Declare, FetchTask, UpdateTask, UpdateLog). act_runner 0.6.1 stays
   compatible; the post-upgrade CI run confirms it.
6. **Chart 12.7.0 with a 28.x image.** Every CLI call in the chart's init scripts exists in 28.4.0 with the flags the
   chart passes: `config edit-ini --apply-env --config --out`, `generate secret`, `migrate`, `admin user list --admin`,
   `admin user create`, `admin user change-password --must-change-password`, `admin auth list --vertical-bars`,
   `admin auth add-oauth`/`update-oauth` (`--name --provider --key --secret --auto-discover-url --scopes`).
   `/usr/local/bin/environment-to-ini` is now a wrapper over `gitea config edit-ini --in-place --apply-env`.
7. **OAuth2 account linking (28.3, #40121).** With `ACCOUNT_LINKING = auto`, the link-account sign-up form now
   auto-links only when the existing user's email equals the provider email or its name equals the provider user
   name. Auto-registration is unchanged. Our accounts come from Authelia with matching email, so the normal path is
   unchanged.
8. **UI notifications use a WebSocket (`/-/ws`, #36965)** instead of server-sent events. The edge is Caddy
   `reverse_proxy` (upgrades pass through) behind Cloudflare. To verify after the rollout.
9. **Built-in SSH server** moved from `gliderlabs/ssh` to `golang.org/x/crypto/ssh` (#38837). To verify with a clone
   over SSH.
10. **Review-triggered workflows now run on the PR head** (28.3, #40117). No stored run has a `pull_request_review`
    event (rehearsal copy: push, pull_request(_sync/_label/_comment), schedule, workflow_dispatch, issue_comment).
11. **`[server] DOMAIN`.** 28.4.0 derives the app domain from `ROOT_URL`; ours agree (`git.chifor.me`). No change.
12. **Metrics.** No alert or dashboard reads a family from Gitea's own `/metrics`; the smoke boot serves the same
    `gitea_*` families. The `cnpg_gitea_*` credential metrics come from the `credential_inventory` views (below).

## Rehearsal (2026-11-16, workstation Docker)

- **Dump:** `pg_dump -Fc` of `gitea` from the primary `infra-pg-6`: 937 MB in 108 s (read-only, no freeze; the
  rollout takes its own under a write freeze).
- **Restore:** `postgres:17.11`, role `gitea`, `pg_restore --exit-on-error -j 4`: exit 0 in 45 s, 3.4 GB, migration
  version 343, the three `credential_inventory` views present.
- **Migrate:** `docker.gitea.com/gitea:28.4.0-rootless gitea migrate` as role `gitea`: exit 0, migrations 343 →
  **358** in about 1 s (15 migrations, among them 348 "Recreate email_hash table", 353 "Add audit event table",
  357 "Normalize legacy team authorize values"). **358 is the acceptance value for runbook step 7.**
- **Views and permissions:** the three `credential_inventory` views answer as `pg_monitor` on the migrated copy
  (none of the 15 migrations rebuilds `user`, `access_token` or `oauth2_grant`). `team` (8 rows) and `team_unit`
  (46 rows) are identical before and after: migration 357 changed nothing here.
- **Smoke boot:** 28.4.0 on a copy of the migrated database, on an `--internal` Docker network (no egress), crons
  and mirrors off. With the current values it logs the two egress errors above; with this PR's values it logs none.
  The only remaining errors are the expected OIDC-discovery failures (no network). `/metrics` serves the `gitea_*`
  families.
- **Render:** `helm template` of chart 12.7.0, current values against this PR's: the only differences are the
  image (`28.4.0-rootless`, all four Gitea containers), version labels, `[actions] RUN_RETENTION_DAYS=0`,
  `[security] ALLOWED_HOST_LIST` + `EGRESS_MODE` (beside the chart's `INSTALL_LOCK`), and the `[webhook]` section
  gone from the config Secret.
- **`app.ini` key removal:** the runbook's `awk` rewrite, run in the 28.4.0 image on a synthetic `app.ini` with the
  live layout (owner 1000:1000, mode 660), removed only the `[webhook] ALLOWED_HOST_LIST` line, kept the inode, owner
  and mode, and is a no-op on a second run.

## Change (this PR)

- `kubernetes/apps/apps/gitea/gitea.yaml`: `image.tag` `1.27.3` → `28.4.0`; `[webhook] ALLOWED_HOST_LIST` →
  `[security] EGRESS_MODE = lax` + `ALLOWED_HOST_LIST = "192.168.0.0/24, 10.96.0.0/12"` (same reach as before);
  `[actions] RUN_RETENTION_DAYS = 0`.
- `kubernetes/apps/apps/gitea/actions-run-retention.yaml`: header notes the native setting and why it stays off.
- `docs/runbooks/gitea-upgrade.md`: the `app.ini` merge and the views as standing facts; rehearsal checks for views,
  teams, smoke boot and render diff; the one-time key removal in step 5; 28.x verification items; the rollback note
  for keys the new values added.

## Rollout (not part of the merge)

Merging this PR deploys it: Flux applies the HelmRelease within minutes. So the PR stays `WIP:` until the window, and
the window follows the runbook: freeze the HelmRelease (annotate + suspend) BEFORE merging, then write freeze, `pg_dump`,
Velero backup of both volumes, the `[webhook]` key removal, unsuspend, verify. Expected outage about 5 minutes
(2026-11-08: 14, of which 9 were the `backup` short-name trap). Every Actions job running at the stop fails and is
re-run afterwards (step 5).

## Rollback

As in the runbook: restore the dump, reinstall 1.27.3 from staged values with an explicit `image.tag=1.27.3`, restore
the volumes if needed. A 1.27.3 binary refuses a database at version 358. If the volumes are not restored, remove the
`[security] EGRESS_MODE` and `ALLOWED_HOST_LIST` keys this PR adds (runbook rollback step 3).

## Follow-ups (not in this PR)

- `10.96.0.0/12` has no webhook consumer since AgentForge retired; dropping it narrows the list.
- The stale `[storage.actions_*] MINIO_BASE_PATH` and `[database] SQLITE_*` keys in `app.ini` are harmless leftovers
  of the same merge behaviour.

## Review

(codex, implementation round: below)
