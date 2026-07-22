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

---

# AgentForge **v2** (Kubernetes / Kata sandbox) — operations & debugging playbook

> This is a **separate deployment** from the v1 dev-worker fleet above. v2 (ADR 0019) runs on the
> Talos **agent-nodes** (`.47`/`.48`/`.49`, pool label `ailab.io/agent-pool=true`) as a
> credential-**broker** + ephemeral **Kata microVM sandbox** architecture: the orchestrator never
> holds the raw provider OAuth — it mints a short-lived capability, and a per-account broker injects
> the real subscription credential on the agent's behalf. Manifests: `kubernetes/apps/infrastructure/
> agentforge-{broker,sandbox,workers,codex-refresh}/` + `kubernetes/apps/apps/agentforge/`.
> **All `kubectl` below uses `--context admin@ai`** (the default context is a DIFFERENT cluster).

## Cluster access & namespace map

| Namespace | What lives there |
|---|---|
| `agentforge-broker` | Per-account **broker** Deployments `broker-anthropic-max1`, `broker-anthropic-max2`, `broker-openai-codex` (each **2 replicas** + PDB + a **pinned-ClusterIP** Service on :8700). Also the `af-codex-refresh` CronJob. Brokers run the `…/agentforge/orchestrator` image (CLI-free broker build), NOT p1-worker. |
| `agentforge-sandbox` | Ephemeral **Kata microVM** Job pods `af-sbx-*` (one per agent run), the reaper's cross-ns RBAC target, the sandbox-guard / sandbox-job-guard VAPs, and the shared NFS staging/workspace PVs. |
| `af-tenant-tenant-zero-playground` | The planner **orchestrator** Deployment `af-orch-playground-planner` (the KEDA scale **target**, `agentforge serve`, `AF_EXECUTOR=sandbox`) + the KEDA `ScaledObject/af-orch-playground-planner`. Runs as SA `af-orch-playground-planner`. |
| `agentforge` | Trusted home: `agentforge-dispatcher` (always-on KEDA scale **oracle**, exports `forge_pending`), `agentforge-reaper` (leader-elected GC of leaked Jobs/Pods/dirs), and `agentforge-platform` (the CP webapp/reconciler). |

Quick posture check:

```bash
kubectl --context admin@ai -n agentforge-broker get deploy,po
kubectl --context admin@ai -n agentforge-sandbox get pods            # af-sbx-* are ephemeral (see below)
kubectl --context admin@ai -n af-tenant-tenant-zero-playground get deploy,scaledobject,po
kubectl --context admin@ai -n agentforge get deploy                  # dispatcher + reaper (+ platform)
```

## Broker debugging (the credential-injection path)

The broker's decisions live in an **audit log** line. Read it and grep for the JSON marker:

```bash
kubectl --context admin@ai -n agentforge-broker logs <broker-pod> | grep broker.audit
```

Each `broker.audit` record carries a **`decision`** (`granted` / `forbidden` / `model-not-allowed` /
`unauthorized`) plus a `status`. Interpreting them:

- **`granted` + `status:200`** — the broker authorized the request and forwarded it upstream.
- **`granted 200` but `tokens_used == 0`** — the broker granted, but the **upstream isn't generating**
  (usually an upstream auth/model problem, not a broker one — see UPSTREAM below).
- **`model-not-allowed` (403)** — the request model is not in the gateway `model_set` (see policy below).
- **`forbidden` (403)** — the model is outside the **kid policy** allow-list (see policy below).
- **`unauthorized`** — capability signature / `iss` / `aud` mismatch (bad or wrong-account capability).
- On a **rejection the audit `"model"` field is intentionally BLANK** — the raw request model is never
  logged pre-authz. A blank `"model":""` on a reject is a red herring, NOT the cause.

**IMPORTANT — brokers run 2 replicas.** `kubectl logs deploy/<name>` (or `logs -l …` without care)
samples **ONE** replica, so the audit line you want may be on the other pod. **Iterate all pods:**

```bash
for p in $(kubectl --context admin@ai -n agentforge-broker \
             get pods -l app.kubernetes.io/name=broker-openai-codex -o name); do
  echo "== $p =="; kubectl --context admin@ai -n agentforge-broker logs "$p" | grep broker.audit
done
```

**UPSTREAM errors** — failures returned by the *real provider* AFTER the broker granted appear as
`broker upstream <status>` **WARNING** lines (NOT audit lines), e.g.
`broker upstream 401 (model=gpt-5.6): "…authentication token is expired…"`, or an upstream model
rejection. A `401` here means the **model check PASSED and auth failed** — an expired credential, not a
model problem (for codex, jump to the token lifecycle section). Bursty traffic caveat: a narrow
`logs --since=8m` window can show "0 requests" as a **sampling artifact** — don't conclude "idle".

## Capability / policy model (the model is enforced TWICE)

A request is authorized only if the model the CLI **actually SENDS** is present in **both** allow-lists:

1. **Gateway `model_set` check** — built from the job's **capability**, whose model is sourced from
   `agentforge.json` (`cross_review.model` for a gate cross-reviewer, or the **role model** otherwise,
   in the `cchifor/agentforge-config` repo). Violation → audit `model-not-allowed` (403).
2. **Operator KID-POLICY** — Secret **`broker-openai-codex-kids`**, key `registry.json` →
   `.kids.<kid>.allowed_models`, synced by **ESO** from OpenBao
   `af/data/operator/broker/openai/codex-pro/kids`. Violation → audit `forbidden` (403,
   "model(s) outside kid policy"). This is operator-controlled config; changing it needs an OpenBao
   operator write (gated), so prefer aligning the *sent* model to what's already allowed.

**Worked example (codex):** codex's built-in default model is **`gpt-5.6-sol`**, which is NOT in the
kid policy. The kid policy allows **`{gpt-5.3-codex, gpt-5.5, gpt-5.6}`**, so codex is **forced** (via
`-c model=<job.model>` on the CLI) to send **`gpt-5.6`**, and `cross_review.model` in config is set to
`gpt-5.6` so the gateway `model_set` agrees. Both lists must contain the sent model, or you get a 403.

Inspect the live kid policy (keys/models only — never dump secret values):

```bash
kubectl --context admin@ai -n agentforge-broker get secret broker-openai-codex-kids \
  -o jsonpath='{.data.registry\.json}' | base64 -d | jq '.kids | map_values(.allowed_models)'
```

## Sandbox debugging (catch the log before it's gone)

Sandbox pods (`af-sbx-*`) are **Kata microVMs that PURGE their container logs ~8s after the process
exits** (the log dies with the VM), and the Job's TTL reaps the pod ~300s later. So a one-shot
post-mortem `kubectl logs af-sbx-…` after the run almost always returns **empty**. To see an agent's
real stdout/error you must capture the log **while the pod is `Running` or the instant it completes** —
a tight poll loop is the tool:

```bash
# poll for a new af-sbx pod, then stream its log the moment it appears (before Kata purges it)
while :; do
  p=$(kubectl --context admin@ai -n agentforge-sandbox get pods \
        -o jsonpath='{.items[?(@.status.phase=="Running")].metadata.name}' | tr ' ' '\n' | grep '^af-sbx' | head -1)
  [ -n "$p" ] && { kubectl --context admin@ai -n agentforge-sandbox logs -f "$p"; break; }
done
```

The **orchestrator already streams sandbox logs** (`stream_pod_logs(follow=True)`, started at `Running`)
to work around this purge, so the plan/critique text also surfaces in the orchestrator pod's own log —
but for a raw agent error the fast poll above is the fallback. (Note: the orchestrator swallows the
per-handler exception without logging it; the real error is often ONLY in the ephemeral sandbox stdout,
or surfaces later in the `needs-human` escalation after `_MAX_FAILURES` on the **same** pod.)

## Codex OAuth token lifecycle (~10-day JWT, static in the broker)

The codex OAuth **access token is a ~10-day JWT** and the broker uses it **statically** (no
self-refresh). When it expires you get `broker upstream 401 … "authentication token is expired"`
(model passed, auth failed). **Manual refresh + reload:**

```bash
# 1) mint a fresh 10-day auth.json from this box's live codex creds
cd ~/work/home/agentforge && uv run python -m agentforge.broker.codex_refresh \
  --in ~/.codex/auth.json --out <fresh-auth.json> --force

# 2) write it to OpenBao operator path (mount `af`, KV v2), property `auth.json`:
#      af/operator/broker/openai/codex-pro/oauth
#    e.g. (token via STDIN, never argv): bao kv patch af/operator/broker/openai/codex-pro/oauth auth.json=@<fresh-auth.json>
```

Then ESO re-syncs `broker-openai-codex-oauth` and each broker replica **reloads every ~5 min**
(log line `broker operator credential reloaded`) → upstream 200 again.

**Gotchas:**
- The write in step 2 needs an **OPERATOR-scoped** OpenBao token. **OpenBao 2.5.5 has DISABLED
  `generate-root`** (405 "unsupported operation") — there is **no root-recovery** via the unseal key.
  Use the `agentforge-provisioner`'s k8s-auth access (it can write operator paths) or a **held
  operator token** — not root.
- Do **NOT** patch the k8s Secret `broker-openai-codex-oauth` directly: ESO (`creationPolicy=Owner`)
  **drift-reverts** it within seconds. The fresh token MUST go to OpenBao (the source of truth).
- The **`af-codex-refresh` CronJob** (ns `agentforge-broker`, schedule `0 3 * * *`, `--skew-seconds
  172800`) is meant to automate this, but currently **fails `HTTP 400`** because the OpenBao role
  **`af-codex-refresher` is missing** (a bootstrap-sentinel gap). Until that role exists, refresh is
  the manual procedure above. Check it with:
  `kubectl --context admin@ai -n agentforge-broker logs job/<af-codex-refresh-…>`.

## KEDA scaling gotchas (the planner ScaledObject)

`ScaledObject/af-orch-playground-planner` scales the planner on **`forge_pending`** — a
Prometheus gauge exported by the **`agentforge-dispatcher`** (`sum(forge_pending{account="claude-max-1",
role=~"planner|reviewer"})`, threshold 1, `ignoreNullValues=true`).

- **Dispatcher can't reach Gitea → plans die.** If the dispatcher can't compute `forge_pending`
  (e.g. a Gitea outage / SQLite lock storm), the metric goes **null**; with `ignoreNullValues=true`
  KEDA treats null as 0 and **scales the planner to 0**, killing in-flight plans. Fix the dispatcher's
  Gitea reachability (the metric is NOT in-flight-subtracted, so a claimed issue keeps it ≥1 and KEDA
  holds the pods stably once Gitea is healthy).
- **`maxReplicaCount:2` + a long codex gate → claim-race interruptions.** With 2 planner replicas a
  long multi-round codex cross-review can be interrupted by claim-racing. Pin to a **single** replica:

  ```bash
  kubectl --context admin@ai -n af-tenant-tenant-zero-playground \
    annotate scaledobject/af-orch-playground-planner \
    autoscaling.keda.sh/paused-replicas=1 --overwrite
  ```

  This is a **bridge, not durable — Flux/KEDA may revert it.** Remove the annotation to resume normal
  0→N scaling.

## Image repin cycle (how a code change ships)

AgentForge **code** changes ride the p1-worker image; **config** changes do not:

- **Code change** → PR to `cchifor/agentforge` → **squash-merge** → Gitea CI rebuilds
  `registry.chifor.me/agentforge/p1-worker` → **repin the 4 ailab digests** (all the same `@sha256`):
  1. `kubernetes/apps/apps/agentforge/deployment.yaml` (the CP's `AFP_WORKER_IMAGE`)
  2. `kubernetes/apps/infrastructure/agentforge-sandbox/reaper-deployment.yaml`
  3. `kubernetes/apps/infrastructure/agentforge-workers/dispatcher-deployment.yaml`
  4. `kubernetes/apps/infrastructure/agentforge-workers/worker-deployment.yaml`

  → open the ailab PR → merge → **Flux rolls** the pods. (The brokers + `af-codex-refresh` run the
  separate `…/agentforge/orchestrator` image, repinned independently.)
- **Config change** (`cchifor/agentforge-config` → `agentforge.json`, e.g. a role/`cross_review` model
  or budget) is **polled live** by the orchestrator (`config_poll_s≈120s`) — **no image rebuild or
  repin**, effective ~2 min after merge.
