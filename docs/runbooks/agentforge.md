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

`monitoring/agentforge.yaml` is a static Service+Endpoints+ServiceMonitor for the **dev-worker VMs**
running agentforge as a host systemd service (ansible role `dev_worker`), relabelled to
`job=agentforge`. ⚠️ **Measured 2026-08-10: `job=agentforge` has ZERO active targets and ZERO
`forge_*` series** — that host-service topology is not what serves the estate any more. The live
`forge_*` series come from the in-cluster pods instead, under two different job labels:

| job | source | series |
|---|---|---|
| `agentforge-worker` | `agentforge-workers/worker-podmonitor.yaml` → operator-managed pool pods | 20 |
| `agentforge-dispatcher` | `monitoring/agentforge-dispatcher` ServiceMonitor | 19 |

So do **not** reach for `job=agentforge` when diagnosing; and note CP-rendered tenant pools are
scraped by neither — see `worker-podmonitor.yaml`'s COVERAGE BOUNDARY and ailab#284. Whether the
dev-worker static target should be removed or repaired is part of that open question, not settled
here.

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
| `agentforge-broker` | Per-account **broker** Deployments `broker-anthropic-max1`, `broker-anthropic-max2`, `broker-openai-codex` (each **2 replicas** + PDB + a **pinned-ClusterIP** Service on :8700). Also the `af-codex-refresh` CronJob. Brokers run the `…/agentforge/orchestrator` image (CLI-free broker build), NOT p1-worker. Since plan A1 also the `BrokerSeat` CRD + seat RBAC/VAPs (inert until A2/A3b — see *Controller seats (BrokerSeat)* below). |
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

**Worked example (codex):** codex's built-in default model (`gpt-5.6-sol` on the 0.14x CLIs) is NOT
in the kid policy, so codex is **forced** (via `-c model=<job.model>` on the CLI) to send the config
model. The estate target is **`gpt-6-astra`** (2026-09-05): the kid policy in
`capability-kids-configmap.yaml` (checksum bumped in `provisioner-deploy.yaml`) is the source of
truth for what is ENTITLED, and `cross_review.model` + the cross-reviewer role in
`cchifor/agentforge-config` are the source of truth for what is SENT — read both live rather than
trusting a list in prose; the gateway `model_set` is built from the latter. Both must contain the
sent model, or you get a 403. A THIRD gate sits upstream: chatgpt.com rejects `gpt-6-astra` from a
codex CLI older than 0.153.0 (measured 2026-09-05: 0.152.1 rejected, 0.153.0 accepted;
`400 The 'gpt-6-astra' model requires a newer version of Codex`) —
that is a `broker upstream 400` WARNING line, not an audit line, and the fix is the image's
`CODEX_VERSION` (agentforge `deploy/*.Dockerfile`), not policy.

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
Prometheus gauge exported by the **`agentforge-dispatcher`** (`sum(forge_pending{account="claude-max-2",
role=~"planner|reviewer|implementer|tester"})`, threshold 1, `ignoreNullValues=true`).

- **The `account` in that query is a CROSS-REPO BINDING and it has silently broken once.** The
  account name duplicates `accounts.<name>.workers` in `cchifor/agentforge-config`. On 2026-08-09 the
  planner was re-pointed `claude-max-1` -> `claude-max-2` there and this query was left naming
  `claude-max-1`; the dispatcher then emitted only `account="claude-max-2"`, the query matched no
  series, `ignoreNullValues=true` turned that into a real `0` (the external-metrics API served
  `s0-prometheus = "0"`), the trigger went `Active=False`, and the pool sat at **0 replicas with 4
  pending planner issues**. Nothing raised: the ScaledObject stayed `Ready=True` and the generated
  HPA showed only `TARGETS <unknown>/1`. **Triage:** compare the query's `account` against the live
  label — `forge_pending` label sets are authoritative, the manifest is not. Re-point both together.

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

## Resizing a tenant worker pool (FU-B)

A pool's replica count is set through the control plane —
`PATCH /api/workspaces/{workspace_id}/pools/{pool}` with `{"max_replicas": N}` — or from
**Settings › Clusters › Capacity** in the web app. Operator role, and the **bootstrap org
(`tenant-zero`) only**.

### FIRST: is this pool even control-plane-managed?

**Not every `af-orch-*` Deployment in an `af-tenant-*` namespace belongs to the control
plane, and the CP can only resize the ones that do.** This estate runs both kinds, they
look nearly identical, and mistaking one for the other wastes real time (it did on
2026-08-15, and produced a wrong hand-off comment on engine#186 before it was caught):

```bash
kubectl --context admin@ai get deploy -A -o custom-columns=\
'NS:.metadata.namespace,NAME:.metadata.name,POOL:.metadata.labels.agentforge\.io/pool,\
FLUX:.metadata.labels.kustomize\.toolkit\.fluxcd\.io/name' | grep af-tenant
```

| Signal | CP-rendered pool | Estate-managed pool |
|---|---|---|
| `agentforge.io/pool` label | **present** (the renderer stamps it) | absent |
| `kustomize.toolkit.fluxcd.io/name` | not `agentforge-workers` | **`agentforge-workers`** |
| Manifest source | the **agentforge-tenants** repo, committed by the CP | **this repo**, `kubernetes/apps/infrastructure/agentforge-workers/` |
| Resizable by the CP | **yes** | **no** — a CP resize commits to a tenant path nothing here reconciles |

As of 2026-08-15: `af-orch-platform-dev-delivery` is CP-rendered (resizable);
`af-orch-playground-planner` is estate-managed (**not** resizable, and it keeps its own
KEDA `ScaledObject` — see *KEDA scaling gotchas* above).

Resizing an estate-managed pool means editing its manifest here, not calling the CP.

### Arming the estate (nothing resizes until you do)

Two independent knobs, and raising the ceiling alone does nothing:

| Setting | Default | Effect |
|---|---|---|
| `AFP_MAX_WORKER_REPLICAS` | `2` (bounded at 8) | The ceiling a request may not exceed. A request above it is a `422`. |
| `AFP_INSTANCE_AWARE_WORKER_IMAGES` | **empty** | JSON array of digest-pinned worker images the operator asserts read `AF_WORKER_INSTANCE`. **This is the arming switch.** |

Empty allowlist ⇒ every commit of `replicas > 1` is refused. That is why deploying the
resize surface changed nothing: check with

```bash
kubectl --context admin@ai -n agentforge get deploy agentforge-platform \
  -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}{"\n"}{end}' \
  | grep -E 'INSTANCE_AWARE|MAX_WORKER'      # no output == un-armed == nothing can grow
```

**Only add a digest you have actually verified reads the env.** It must be the same
reference `AFP_WORKER_IMAGE` carries (compared as an exact string — a tag can never
match). Confirm the running pool has the downward-API env before asserting anything:

```bash
kubectl --context admin@ai -n af-tenant-tenant-zero-platform-dev \
  get deploy af-orch-platform-dev-delivery \
  -o jsonpath='{.spec.template.spec.containers[0].env}' | grep -o AF_WORKER_INSTANCE
```

### Doing the resize, and reading the answer

The route writes the pool row **and** re-commits that pool's whole tenant manifest set in
one request. Two consequences worth knowing before you click:

- **A refusal is total.** The row rolls back with the commit, so a refused resize leaves
  nothing behind.
- **A `502` means STATE UNKNOWN — go look.** The git write is not inside the database
  transaction and the committer writes one file per request, so a mid-batch or post-push
  failure leaves git at the requested count and the row at the old one. Scaling up, the
  manifest is ahead; scaling down, it is behind. Neither is silent, and the next resize or
  rollout re-renders the full set from the row and reconciles it.

Refusals you may meet, all of which name their own remedy:

| Answer | Meaning |
|---|---|
| `403` | Not an operator, or not the bootstrap org. |
| `422 … AFP_MAX_WORKER_REPLICAS` | Above the ceiling. |
| `422 … AFP_INSTANCE_AWARE_WORKER_IMAGES` (G-1) | The image is not declared instance-aware. |
| `422 … AF_WORKER_INSTANCE … roll this pool` (G-2) | The pod template lacks the downward-API env. The remedy is a **re-render**, not a retry: `POST /api/workspaces/{id}/pools/{pool}/rollout`, then resize. |

**Carry `creds_revision` through** if the pool has an `agentforge.io/creds-revision`
annotation from a release-gate rollout — the re-commit strips it otherwise. The CP stores
only its checksum and cannot recover or even detect the token, so this is on the caller.

A successful answer reports what was **committed** — not that Flux applied it, not that
pods are Ready. Confirm the rollout separately:

```bash
kubectl --context admin@ai -n af-tenant-tenant-zero-platform-dev \
  rollout status deploy/af-orch-platform-dev-delivery
kubectl --context admin@ai -n af-tenant-tenant-zero-platform-dev get pods -o wide
```

Per-replica liveness is in `GET /api/workers`, keyed per instance: each pod reports its own
`AF_WORKER_INSTANCE` (its pod name), so `instances`/`instances_online` show the replicas
individually while the row stays one-per-pool.

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

## Webhook secret durability (2026-08-11 program follow-up)

`AF_WEBHOOK_SECRET` (dispatcher listener HMAC; minted 2026-08-11) lives in OpenBao at
`af/data/operator/dispatcher/webhook` and — worker copy — inside
`af/data/tenants/tenant-zero/playground/orchestrator`. It is **not** in
`operator-seeds.sops.yaml`, so it will not survive an OpenBao wipe+recovery
(day-to-day it is safe: `_apply_operator_seeds` never visits the dispatcher
sibling doc). To make it durable, the age-key holder runs:

```sh
# 1. read the live value (exec-in-pod loopback, operator token on stdin — see the v3-cutover runbook §vault access)
kubectl -n openbao exec -i openbao-0 -- sh -c 'BAO_TOKEN=$(cat) bao kv get -field=AF_WEBHOOK_SECRET af/operator/dispatcher/webhook' <<<"$OPERATOR_TOKEN"
# 2. add it to the seeds document (needs the age private key)
sops edit kubernetes/apps/infrastructure/security/openbao/operator-seeds.sops.yaml
#    -> add under a new logical path entry: af/operator/dispatcher/webhook: {AF_WEBHOOK_SECRET: <value>}
#       (the seed is a bootstrap FLOOR, not a mirror: create-if-absent, so it fills the path after a
#        wipe and can NOT overwrite the live value — a mismatch is reported as drift instead. Still
#        record the live value: the floor is what a re-provision installs, so a wrong one fails the
#        listener's HMAC after the next wipe. Rotation re-points the Gitea org hook and updates this
#        document in the same change: engine `--webhook-rotate-secret`. Canonical contract:
#        docs/runbooks/openbao-recovery.md § "The seed-ownership contract")
# 3. commit + PR as usual; no cluster action needed (the seeds file is read at provision time only)
```

## Controller seats (BrokerSeat) — git-less seats materialised by the provisioner

> **Status: A1 landed, INERT** (`plans/2026-09-06-brokerseat-controller-plan.md`). The CRD
> `brokerseats.agentforge.io`, the two Roles and the two ValidatingAdmissionPolicies exist in
> `kubernetes/apps/infrastructure/agentforge-broker/brokerseat-{crd,rbac,admission}.yaml`, but
> **nothing reads them yet**: the provisioner only starts reconciling once A2 sets
> `AF_PROVISIONER_BROKERSEAT_NAMESPACE`, and the control plane only creates a CR once A3b flips
> `AFP_SEAT_MODE=controller`. Until then the four `broker-*.yaml` git seats are the whole estate and
> everything above about them is unchanged. The kind-based pre-flip check (plan F18,
> `scripts/seat-kind-check.sh`, attached to A3b) is **pending** — not built in A1. What A1 DID prove
> on a throwaway kind v1.31.4 cluster (2026-09-06, not committed): the CRD and both policies apply
> and compile (the objects guard's first draft did not — cel-go refuses a `[dyn, string]` list
> literal that cel-python accepts, which is exactly why F18 must run before the flip); every CRD CEL
> rule (mechanical name, reserved git stems, reserved slugs, CIDR, immutability) and 85 of the
> harness's cases replay with identical verdicts through server dry-runs under SA impersonation,
> including a provisioner UPDATE over a Flux-labelled git object being refused.

A controller seat is one `BrokerSeat` CR in `agentforge-broker`, named `broker-<provider>-<account>`
(the CRD's CEL forces that name and refuses the hand-named git stems), whose spec is ONLY
`{provider, account, clusterIP}` — the control plane names those three, the provisioner decides
everything else (image from `AF_PROVISIONER_SEAT_IMAGE`, ledger DSN + entitlement cloned from the
provider's template seat in `AF_PROVISIONER_SEAT_TEMPLATE_AUDS`, barrier membership from Ready). Per CR
the provisioner renders the SAME 8 objects a `broker-*.yaml` carries (Deployment, PDB, `<stem>-headless`
+ pinned `<stem>` Service, `<stem>-{oauth,kids,ledger}` ExternalSecrets, CiliumNetworkPolicy), byte-for-byte
from the vendored seat templates.

### Ownership: how to tell a controller seat's objects from a git seat's

| Marker | Controller seat object | Flux-managed git seat object |
|---|---|---|
| `metadata.labels["agentforge.io/broker-seat"]` | **`<stem>`** (the CR name) | absent |
| `metadata.ownerReferences` | **exactly one**: `BrokerSeat/<stem>`, `controller: true`, `blockOwnerDeletion: true`, `uid` = the CR's | absent |
| `metadata.annotations["agentforge.io/render-digest"]` | `sha256:…` of the desired body (steady state = no write) | absent |
| `kustomize.toolkit.fluxcd.io/name` label | **never** (admission refuses it) | `agentforge-broker` |
| Flux `prune: true` | never sees it (not label-tracked) | owns it |

```bash
kubectl --context admin@ai -n agentforge-broker get bseat                      # Aud / Phase / ClusterIP / Ready / Age
kubectl --context admin@ai -n agentforge-broker get bseat -o wide              # + Reason
kubectl --context admin@ai -n agentforge-broker get deploy,svc,pdb,externalsecret,cnp \
  -l agentforge.io/broker-seat=<stem>                                          # the 8 owned objects
kubectl --context admin@ai -n agentforge-broker get deploy -o custom-columns=\
'NAME:.metadata.name,SEAT:.metadata.labels.agentforge\.io/broker-seat,FLUX:.metadata.labels.kustomize\.toolkit\.fluxcd\.io/name'
```

### Who may write what (RBAC + admission, both in this repo)

| Identity | RBAC (brokerseat-rbac.yaml) | Admission pin (brokerseat-admission.yaml) |
|---|---|---|
| CP `agentforge/agentforge-platform` | `brokerseats` create/get/list/delete; `services` get (teardown witness). NO update/patch/status. | `agentforge-cp-brokerseat-guard`: a CR it creates carries no finalizers, ownerReferences, labels, annotations or status, and is named `broker-<provider>-<account>`. |
| provisioner `openbao/agentforge-provisioner` | `brokerseats` get/list/patch, `/status` patch, `/finalizers` update; the 5 child kinds get/create/update. NO delete, NO watch, NO list on children, NO secrets/configmaps/pods. | `agentforge-provisioner-seat-objects-guard`: every object it writes is named from its stem, labelled + owner-referenced by its CR, never Flux-labelled, never replacing an object that is not already that CR's, and structurally the template's shape (one digest-pinned container as SA `agentforge-broker`, only its own three Secrets, Services/PDB/CNP selecting its own pods, ExternalSecrets on `agentforge-broker-store` sourcing only `operator/broker/<p>/<a>/{oauth,kids,ledger}`, no `target.template`, only the template's CNP rule shapes). |

Both guards also carry the list of **git-managed stems** (each Flux seat's hand-named stem and the
mechanical `broker-<provider>-<account>` alias of its audience): the CRD refuses a CR named that way at
create, the objects guard refuses any seat object under such a stem (validation 13 — it is what stops an
ExternalSecret `<alias>-oauth` from syncing a git seat's credential into a new Secret name). The two
literals are pinned to `gen-broker-inventory.py`'s seat list by `test_brokerseat_{crd,admission}.py`:
**adding or retiring a git seat means updating both literals in the same PR** or CI is red. What
admission cannot do is look the cluster up: whether one of a seat's 8 derived names already exists
under a foreign owner stays the controller's `NameCollision` refusal (plus the apiserver's own 409 on
create and the objects guard's same-owner rule on update).

An admission **denial** (`kubectl` / provisioner log line starting `agentforge CP …` or `agentforge
provisioner …`) is never a configuration step you are missing: the writer submitted a non-seat shape.
Treat it as a bug in the writer (or a compromised SA) and read the failing clause's comment in
`brokerseat-admission.yaml`. The CEL of both guards is proven by
`scripts/check-seat-guard-cel.py` (cel-python; the baseline is the stamped render of the newest git seat,
one adversarial mutation per clause) — run it after **every** edit of that file, it is what
`.gitea/workflows/tenant-guard-cel.yaml` runs:

```bash
uv run --no-project --with 'cel-python==0.5.0' python3 scripts/check-seat-guard-cel.py -v
python3 -m unittest discover -s scripts/tests -p "test_brokerseat_*.py" -v     # CRD/RBAC/VAP shape pins (stdlib)
```

### Refusal reasons (`status.phase` / `status.reason`) and what each means

The provisioner writes status ONLY when it changes; `kubectl get bseat -o wide` shows the reason,
`-o yaml` the message and conditions. Every refusal below is **zero writes** for that seat that pass and
an `af_provisioner_alerts_total{alert="brokerseat-*"}` increment; the other seats and the tenant pass
are unaffected (per-CR isolation).

| Phase / Reason | Meaning | What to do |
|---|---|---|
| `Pending` / `CredentialMissing` | `af/data/operator/broker/<aud>/oauth` is absent or soft-deleted (the CP writes it in the wizard add). | Retry the add from the wizard; nothing is rendered until the credential exists. |
| `Pending` / `CasNotStamped` | The oauth path's KV metadata has no `cas_required=true` (the CP stamps it at add; plan D2, #287). An unstamped seat is **never rendered**. | Retry from the wizard (re-stamps). |
| `Degraded` / `AudManagedByGit` | The CR's aud is in the provisioner's readyz map, i.e. it is one of the Flux seats. | Delete the CR (it is a duplicate of a git seat); the git seat stays untouched. |
| `Degraded` / `NameCollision` | One of the 8 derived names exists WITHOUT this CR's ownerReference uid — a git object, a hand-made object, or a previous incarnation still being garbage-collected. The controller never adopts, never fights Flux. | Wait one pass if a delete is in flight; otherwise remove the foreign object through git / the wizard. Admission (`Flux label` / `same owner uid` clauses) refuses any write to it regardless. |
| `Degraded` / `ClusterIPImmutable` | The live pinned Service's clusterIP ≠ `spec.clusterIP`. The spec is immutable (CRD CEL), so nothing can be corrected in place. | Remove + re-add from the wizard (delete + re-create). |
| `Degraded` / `ClusterIPRejected` | The apiserver refused the pinned address (already assigned to another Service). | Retry from the wizard: Foreground-delete, wait for CR + Service 404, allocate a fresh address, re-create (plan F22). |
| `Degraded` / `SpecInvalid` | Spec failed the controller's own parse (slug / CIDR) — should be impossible past the CRD schema. | Delete the CR; report. |
| `Seeding` / `LedgerSourceUnset` | `AF_PROVISIONER_SEAT_TEMPLATE_AUDS` names no template seat for this provider (fail closed). | Fix the provisioner env (A2 PR); the seat converges on the next pass. |
| `Degraded` / `KidsDocInvalid` or `LedgerDocInvalid` | An existing `…/kids` or `…/ledger` doc for this aud does not parse / lacks `AF_BROKER_LEDGER_DSN`. Seeds are create-if-absent and are **never overwritten**. | Inspect the doc through the operator token (names only in logs); fix or soft-delete it; never resurrect a soft-deleted doc. |
| `Rendering` / `DeploymentUnavailable` | All 8 objects exist but the Deployment has no available replica (the pod readinessProbe IS `/readyz`, fail-closed on ledger/credential). | Debug the pods exactly like a git seat (§ Broker debugging): ESO `NotReady`, image pull, ledger reachability. |
| `Degraded` / `<ExceptionName>` | A per-CR error (OpenBao / seat API / provisioner) — alert `brokerseat-reconcile-error`. | Provisioner logs for the named exception; the seat is retried every pass. |
| `Terminating` | `deletionTimestamp` set — teardown in progress (below). | Wait; nothing to do. |
| `Ready` with condition `Entitled=False` | The seat serves the credential but no workspace kid names its aud yet (the clone from the template seat found no entitled workspace). | Loud, not fatal: add a `capability-kids` ConfigMap entry or entitle the template seat; explicit ConfigMap entries always win. |

Alerts to watch: `brokerseat-list-unreadable` (CR LIST failing → last-good view kept, barrier defers new
kids — safe direction), `brokerseat-inventory-unknown` (3 consecutive unknown passes since process
start), `brokerseat-collision`, `brokerseat-reconcile-error`.

### Teardown = Foreground delete, and how the address is released

The wizard's remove (or a cancel) makes the CP `DELETE` the CR with `propagationPolicy: Foreground` and a
uid precondition. Kubernetes GC then deletes the 8 `blockOwnerDeletion` dependents FIRST (Deployment →
ReplicaSets → Pods, both Services, the PDB, the three ExternalSecrets and their owned Secrets, the CNP);
the provisioner's finalizer `agentforge.io/brokerseat` holds the CR until it has GETs of all 8 returning
404, then removes itself; the CR disappears. The CP's witness for "address released" is **CR 404 AND the
pinned Service `<stem>` 404** (the `services: get` grant) — the apiserver, not a probe window, is the
authority, and only then is the ClusterIP freed in the CP's DB. The OpenBao docs (`oauth`/`kids`/`ledger`)
are left in place, exactly as for a git seat remove — the operator KV soft-delete step the CP's
`cleanup_required` names is unchanged. Break-glass by hand MUST be
`kubectl --context admin@ai -n agentforge-broker delete bseat <stem> --cascade=foreground`: kubectl's
default is a **background** delete, and background GC waits for the owner to disappear while the
provisioner's finalizer waits for the children to disappear — a deadlock that leaves the CR
`Terminating` and the 8 objects in place. (Foreground is the only propagation the CP uses; orphan and
background propagation are untested until the A3b live proof.) A hand delete also leaves the CP row in
drift until **Repair** re-creates it — prefer the wizard.

### Revert order (never remove the CRD while a CR exists)

1. **A3b** (`AFP_SEAT_MODE=controller` → `gitops`): configuration-only; new adds open PRs again, existing
   controller seats keep serving and stay removable/repairable (mode is persisted per account).
2. Remove every controller seat from the wizard and wait until `kubectl -n agentforge-broker get bseat`
   is empty and the pinned Services are gone. The controller must still be running for this step (it
   removes its finalizer).
3. **A2** (unset `AF_PROVISIONER_BROKERSEAT_NAMESPACE` / pin back): the controller stage becomes a no-op.
4. **A1** (this section's files) only after step 2. Deleting the CRD cascades to any remaining CR, and a
   CR carrying the provisioner's finalizer with no controller left to remove it wedges the CRD deletion
   (`customresourcecleanup` waits forever) — which is why CRs go first. An image or CRD rollback while
   seats exist is refused by this order, not by tooling.

### DR

- **A CR vanished out of band** (hand delete, etcd restore): the CP row shows drift ("controller seat
  missing"); **Repair** on the row re-creates the CR from the persisted spec (same name, same clusterIP);
  the kids/ledger seeds are create-if-absent so re-adding converges on the retained docs; the oauth doc
  is untouched. Never duplicate-add.
- **Cluster rebuilt from git**: Flux re-applies A1 (CRD/RBAC/VAPs); A2's env brings the controller up on
  zero CRs; each controller-managed account is re-added from the wizard (Repair per row). Git seats come
  back with Flux as today.

### Operator env the provisioner will read (set by A2, NOT by A1)

| Env (provisioner-deploy.yaml) | Meaning | Default when unset |
|---|---|---|
| `AF_PROVISIONER_BROKERSEAT_NAMESPACE` | The namespace to LIST BrokerSeats in (`agentforge-broker`). **The feature flag**: unset = byte-identical provisioner behaviour. | unset (controller OFF) |
| `AF_PROVISIONER_SEAT_IMAGE` | The digest-pinned `registry.chifor.me/agentforge/orchestrator@sha256:…` every controller seat's Deployment runs; validated at startup against the same regex the objects guard enforces; `just pin-bootstrap` rewrites it in lockstep with the pod image. | none — required once the flag is set (startup refusal otherwise) |
| `AF_PROVISIONER_SEAT_TEMPLATE_AUDS` | JSON `{provider: aud}` naming the reviewed seat whose ledger DSN is copied and whose per-workspace entitlements are cloned for every new seat of that provider. | `{"anthropic":"anthropic/claude-max-1","openai":"openai/codex-pro"}` (a provider missing here → `LedgerSourceUnset`, fail closed) |
