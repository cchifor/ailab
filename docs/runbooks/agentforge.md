# Runbook: AgentForge (autonomous dev agents on the dev-workers)

Operations for the AgentForge fleet (ADR 0018): the 6 dev-worker VMs (dw1–dw6, 192.168.0.8–.13)
run the `agentforge` orchestrator as a host systemd service, driving `claude`/`codex` subscription
CLIs + litellm-local against Gitea issues (label state machine, `state: 1-needs-plan` … `5-completed`).

- app repo: `cchifor/agentforge` (Gitea) · config/control plane: `cchifor/agentforge-config` (`agentforge.json`)
- role: `ansible/roles/dev_worker/tasks/agentforge.yml` (toggle `dev_worker_enable_agentforge`)
- secrets: `ansible/secrets/dev-worker.sops.yaml` (`dev_worker_agentforge_*`)
- k8s companions: `kubernetes/apps/apps/ai/litellm-local.yaml` (LAN :30400) · gitea webhook
  allowlist (`gitea.yaml`) · `monitoring/agentforge{,-rules}.yaml`
- units on each worker: `agentforge.service` (enabled; started by the updater) ·
  `agentforge-update.timer` (2-min pin convergence) · code under `/opt/agentforge/releases/<ver>`
  + `current` symlink · env `/etc/agentforge/agentforge.env` (0600)

## Day-0 bootstrap (order matters)

1. **Merge the ailab PR** (webhook `ALLOWED_HOST_LIST`, litellm-local, role, monitoring) and let
   Flux reconcile. Verify: `kubectl --context admin@ai -n ai get svc litellm-lan` and a NEGATIVE
   test — a cloud model name must 4xx through `http://192.168.0.41:30400/v1`, qwen must 200.
2. **Subscription logins** (see below) on all 6 workers, then the **reboot auth validation**.
3. **`bootstrap_gitea.py`** (from `cchifor/agentforge`, admin PAT; idempotent reconcile): repos
   `agentforge-config` + `agentforge-playground`, 4 bot users + minimal PATs (printed ONCE — paste
   straight into the SOPS file), org labels, the 6 org webhooks (reconciled by URL) + smokes
   (HMAC-valid delivered, HMAC-invalid rejected, one stopped worker → observe Gitea retry),
   branch protection on the playground, package-registry upload/download/immutability smoke.
4. **SOPS secrets**: fill all `dev_worker_agentforge_*` keys in
   `ansible/secrets/dev-worker.sops.yaml` (litellm key =
   `sops -d kubernetes/apps/apps/ai/litellm-local-secret.sops.yaml`), re-encrypt, commit.
5. **CI green on `cchifor/agentforge`** → tag `v0.1.0` → release workflow publishes the tarball +
   sha256 to the Gitea generic package registry and bumps the config pin (`release` +
   `release_sha256`).
6. **Enable + provision**: set `dev_worker_enable_agentforge: true` in
   `ansible/group_vars/dev_workers.yml`, then `ansible-playbook --check` first,
   `systemd-analyze verify` on the new units, then `just dev-workers` twice (2nd run ≈ no changes).
   The update timer performs the first install and starts the service; check
   `systemctl status agentforge` and `curl -s localhost:8700/healthz | jq .version` per worker.
7. **Canary smoke**: run `scripts/smoke-ailab.sh` (agentforge repo) — a canary issue with
   production engines walks 1→5 with per-stage SLOs, asserts distinct-bot authorship/approval/
   merge, then cleans up. Run it after any fleet-wide change.

## Subscription logins (which account on which worker)

Topology is BINDING (config `accounts` block must match): **Max#1 → dw1+dw2** (Planner/Reviewer),
**Max#2 → dw3+dw4** (Implementer), **Codex Pro → dw5+dw6** (cross-reviewer). Tester uses
litellm-local (no login).

```bash
ssh c4@192.168.0.8            # dw1 (repeat per worker with its account)
claude login                  # browser OAuth; then mint a long-lived headless token:
claude setup-token            # survives non-interactive systemd starts
codex login                   # dw5/dw6 only (Codex Pro)
```

**Reboot auth validation (required before go-live):** reboot each worker and prove the service
passes its auth canary *non-interactively* — `curl -s localhost:8700/readyz` must go ready without
anyone logging in. If it degrades with an auth failure, the OAuth store didn't survive: re-login
and re-check. The unit pins `HOME`/`CLAUDE_HOME`/`CODEX_HOME` to `/home/c4`, so a login as any
other user (or via a different `$HOME`) will NOT be seen by the service.

## Pause / resume (first lever for anything weird)

`FORGE_PAUSED` lives IN the config JSON — no restarts, no ansible:

```bash
# pause: workers finish + release current claims, then stop claiming
tea pr ... # or edit agentforge.json in cchifor/agentforge-config: "FORGE_PAUSED": true, push to main
```

Propagation ≤2 min worst case (config-repo webhook is near-instant; the 2-min poll is the floor).
It is checked before every agent invocation AND every forge write batch — mid-run work stops at
the next checkpoint, not mid-write. Resume = flip back to `false`. Per-issue stop: add the
`needs-human` label (global stop for that issue until removed).

## Release / rollback (pin bump + revert)

Deploys are **config-repo pin flips**, converged by `agentforge-update.timer` (≤2 min/worker):

- **Release**: tag in `cchifor/agentforge` → release workflow uploads the immutable package and
  bumps `release`/`release_sha256` in agentforge.json. Watch `forge_build_info{version}` in
  Prometheus converge across the 6 workers.
- **Rollback**: revert the pin commit. Same mechanism, backwards — workers download (or reuse, the
  last 3 releases are kept) the old version and restart onto it.
- **Failed update self-heals**: the updater health-checks the new build (`/healthz` must report
  the pinned version AND a new MainPID within 60s) and on failure flips the `current` symlink
  back, restarts onto the previous release, verifies it healthy, and beacons
  (`journalctl -t agentforge-update`, plus a retry every 2 min until the pin is fixed/reverted —
  deliberate: loud, but `current` never breaks).
- **Protocol-changing releases**: pause → pin bump → verify → resume; `min_agent_version` makes
  too-old workers degrade (claim nothing) instead of misbehaving during the skew window.

## Re-login (subscription OAuth expiry)

Symptom: `/readyz` degraded with an auth-canary failure; 2 consecutive auth failures escalate to
`needs-human` + alert. Fix on the affected worker:

```bash
ssh c4@<worker-ip>
claude login && claude setup-token     # or: codex login (dw5/dw6)
sudo systemctl restart agentforge      # re-runs the startup auth canary
curl -s localhost:8700/readyz          # must be ready again
```

Rate-window cooldowns are NOT auth failures: the worker backs off locally and the reconciler
retries — no operator action.

## Claim cleanup (usually: none)

Claims are issue comments with leases — self-healing by design:

- **Graceful stop** (`systemctl stop`, deploys): SIGTERM releases all held claims before exit.
- **Crash / SIGKILL / VM loss**: the claim's lease expires (TTL = run timeout × 1.5, min 10 min);
  the reconciler's reaper treats expired claims as dead and work is re-claimed. Expect recovery
  latency of one lease TTL, not permanence.
- **Manual override** (stuck NOW, can't wait): edit the claim comment on the issue and set
  `released: true` in its JSON payload (as any bot/admin), or simply delete the comment — the next
  reconcile re-elects. Never flip state labels by hand mid-flight; if you must, expect the
  reconciler to re-derive state from the latest `af:run` marker (marker wins, label is a mirror).

## Monitoring

`monitoring/agentforge.yaml` scrapes :9464 on all 6 workers (job=agentforge);
`agentforge-rules.yaml` alerts: ForgeWorkerDown / ForgeIssueStuck / ForgeNeedsHumanPending /
ForgeWebhookHMACFailures / ForgeReconcileDriftHigh → ntfy. First diagnostics stop:
`journalctl -u agentforge` on the worker + the issue's `af:run`/`af:claim` comment ledger.
