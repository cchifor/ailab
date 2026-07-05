# Grafana Monitoring Improvements — Design

**Date:** 2026-07-05
**Status:** Approved-pending-review
**Scope:** `kubernetes/apps/infrastructure/monitoring/` (Flux-reconciled on merge to `main`)

## Goal

Three improvements to the lab's Grafana observability, plus a review/cleanup of the
existing dashboards:

1. Add two resource-usage sections to the **AI Lab Fleet** dashboard — one for the
   GitHub Actions runners, one for the dev-workers.
2. Add a new dashboard for **Kubernetes releases, resource usage, and error/warning logs**.
3. Review all dashboards: keep what's useful, fix real issues, resolve redundancy.

## Non-goals

- No new alerts (PrometheusRules). Dashboards + metric collection only.
- No systemd/journal log shipping from the runner/worker/host VMs (Loki still ingests
  in-cluster pod logs only).
- No changes to the Talos/OpenTofu infra layer. Dev-worker node_exporter is already
  installed (ansible `dev_worker` role) and its firewall is already open; we only add the
  Prometheus scrape.

## Current state (from audit)

- **AI Lab Fleet** = `reporting-dashboard.yaml` — a single minified-JSON ConfigMap
  (`data.reporting.json`), UID `ailab-reporting`, Grafana's **home** dashboard. Flat
  24-column layout, uncollapsed `type:"row"` markers, 40 top-level panels (4 rows + 36
  panels), current bottom `y=86`, next free panel `id=41`. Rows today: Hypervisors (id 1) →
  Instances (id 14) → AI (id 24) → Storage (id 35). Single template var `${DS_PROMETHEUS}`.
- **AI LLM** = `ai-llm-dashboard.yaml` (UID `ai-llm-strixhalo`, 9 panels) — a standalone
  deep-dive that **overlaps** the Fleet AI row (`amdgpu_*` + `llamacpp:*`).
- **Loki — Logs** = `loki-logs-dashboard.yaml` (UID `loki-logs`) — ad-hoc log explorer
  (`${DS_LOKI}`, `namespace`/`pod`/`search` vars). Healthy.
- **Trivy Operator** = `trivy-dashboard.yaml` (UID `trivy-operator`) — imported (grafana.com
  17813), 5 collapsed rows. Healthy.
- **Lint result:** all four parse, use consistent datasource variables, no panel overlaps.
  - Only bug found: the Fleet "AI Node CPU %" panel filters by **hardcoded IPs**
    (`instance=~"192.168.0.4[456]:9100"`) because the AI-LXC scrape is not relabeled to a
    clean job. Fragile if an AI-LXC IP changes.
  - Only redundancy: AI panels duplicated across Fleet AI row + standalone AI LLM.

### Data availability (the two gaps this design fills)

| Needed for | Metric source today | Action |
|---|---|---|
| Runner resource section | ✅ `job="ci-runner-node"` node_exporter on `.47/.48/.49/.33/.34` | none — build panels |
| Worker resource section | ❌ node_exporter runs on `.37/.38/.39` but **nothing scrapes it** | add `dev-worker-node` scrape |
| k8s-releases status | ❌ Flux `gotk_*` exposed on `:8080` but **no PodMonitor/CRS collects it** | add PodMonitor + KSM custom-resource-state |
| k8s resource usage | ✅ cadvisor + kube-state-metrics | none |
| k8s error/warn logs | ✅ Loki (in-cluster pod logs) | none |

## Design

### Component 1 — Dev-worker scrape (`dev-workers-node.yaml`, new)

Direct mirror of `ci-runners-node.yaml`: headless `Service` + manual `Endpoints`
(`192.168.0.37/.38/.39`, port 9100) + `ServiceMonitor` (label `release: kube-prometheus-stack`,
30s interval) that relabels `job="dev-worker-node"`. Registered in `kustomization.yaml`.

Result: `node_*` series for the three dev-workers, queryable by `job="dev-worker-node"`.

### Component 2 — Flux metrics (`flux-monitoring.yaml` new + `kube-prometheus-stack.yaml` edit)

Two complementary sources so the releases dashboard has both live reconcile health and
per-object status:

- **PodMonitor** (`flux-monitoring.yaml`) — selects the 4 Flux controllers in `flux-system`
  (`source-controller`, `kustomize-controller`, `helm-controller`, `notification-controller`)
  on the `http-prom` (8080) port; label `release: kube-prometheus-stack` so the operator
  discovers it. Yields `gotk_reconcile_condition`, `gotk_reconcile_duration_seconds`,
  `gotk_suspend_status`.
- **kube-state-metrics custom-resource-state** (values edit in `kube-prometheus-stack.yaml`,
  under the `kube-state-metrics:` subchart key):
  - `rbac.extraRules` granting `list`/`watch` on Flux CRDs
    (`kustomize.toolkit.fluxcd.io`, `helm.toolkit.fluxcd.io`, `source.toolkit.fluxcd.io`).
  - `customResourceState.enabled: true` + `config:` = the **upstream Flux KSM config**
    (from the Flux monitoring docs / `fluxcd/flux2-monitoring-example`) covering
    `Kustomization`, `HelmRelease`, `GitRepository`, `HelmRepository`, `HelmChart`,
    `OCIRepository`. Produces `gotk_resource_info{customresource_kind, name,
    exported_namespace, ready, suspended, revision}` (Info metric, value 1; state in labels).
  - The subchart auto-adds `--custom-resource-state-config-file` and mounts the config when
    `customResourceState.enabled`; the existing KSM ServiceMonitor scrapes it — no extra
    scrape wiring.
  - The exact upstream config block is pulled verbatim at implementation time (not
    hand-authored) to avoid drift; the design fixes only the metric name/label contract above.

### Component 3 — AI Lab Fleet: two new sections + two fixes

**Append at bottom** (starting `y=86`, ids from 41) so no existing panel's `y` shifts.
Each section mirrors the existing **Hypervisors** row idiom (a `type:"row"` marker `w:24,h:1`,
then a strip of `stat` tiles, then `timeseries`), just swapping the job label.

**Section: GitHub Actions Runners** (`job="ci-runner-node"`)

| Panel | Type | Query (abbrev.) |
|---|---|---|
| Runners Up | stat | `count(up{job="ci-runner-node"}==1)` |
| Cores | stat | `count(node_cpu_seconds_total{job="ci-runner-node",mode="idle"})` |
| Memory | stat | `sum(node_memory_MemTotal_bytes{job="ci-runner-node"})` |
| Fleet CPU % | stat | `100*(1-avg(rate(node_cpu_seconds_total{job="ci-runner-node",mode="idle"}[5m])))` |
| Fleet Mem % | stat | `100*(1-sum(node_memory_MemAvailable_bytes{…})/sum(node_memory_MemTotal_bytes{…}))` |
| CPU % per runner | timeseries | `100*(1-avg by(instance)(rate(node_cpu_seconds_total{…,mode="idle"}[5m])))` |
| Mem % per runner | timeseries | `100*(1-node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes)` |
| Root Disk Used % | timeseries | `100*(1-node_filesystem_avail_bytes{…,mountpoint="/"}/node_filesystem_size_bytes{…})` |
| Network RX+/TX- | timeseries | `sum by(instance)(rate(node_network_receive_bytes_total{…,device!~"lo|veth.*|docker.*|cali.*|cni.*"}[5m]))` (+ negated transmit) |
| Docker build-cache | timeseries (optional bonus) | `runner_docker_build_cache_bytes` (runner-specific disk pressure) |

**Section: Dev Workers** (`job="dev-worker-node"`) — same panel set as Runners (Up, Cores,
Memory, Fleet CPU%/Mem%, per-worker CPU%/Mem%, Root disk%, Network), swapping the job label.
No build-cache panel (workers aren't buildx).

**Fix A — AI CPU hardcoded IP.** Relabel the AI-LXC scrape (`apps/apps/ai/monitoring.yaml`
Service `ai-llm-node`) to force `job="ai-llm-node"`, then change the Fleet "AI Node CPU %"
panel to `…{job="ai-llm-node",mode="idle"}…`. Removes the brittle IP regex. (Cross-folder edit;
kept minimal and verified that no other panel relies on the old default job label.)

**Fix B — AI redundancy.** Trim the Fleet **AI row** to a compact 4-panel summary — iGPU
Utilization (`amdgpu_gpu_busy_percent`), VRAM Used vs Total, Decode Throughput
(`llamacpp:predicted_tokens_seconds`), iGPU Temperature — and remove the deeper duplicates
(GTT, prompt throughput, busy-slots, power, requests) from the *overview*. Full detail remains
in the standalone **AI LLM** dashboard (unchanged). No metric is lost lab-wide; the home page
gets lighter.

### Component 4 — New dashboard `k8s-releases-dashboard.yaml` — "Kubernetes — Releases & Workloads"

New JSON ConfigMap (`grafana_dashboard: "1"`, UID `k8s-releases`, tags
`[kubernetes, flux, releases, logs]`). Template vars: `${DS_PROMETHEUS}`, `${DS_LOKI}`,
`namespace` (`label_values(kube_namespace_created, namespace)`). Three uncollapsed rows:

**Row 1 — Releases (Flux)**
- Stats: HelmReleases Ready `count(gotk_resource_info{customresource_kind="HelmRelease",ready="True"})`
  vs total; Kustomizations Ready vs total; GitRepositories Ready; **Suspended**
  `count(gotk_resource_info{suspended="true"})`; **Not Ready**
  `count(gotk_resource_info{ready="False"})` (red threshold >0).
- **Table — failing / not-ready resources:** `gotk_resource_info{ready="False"}` with a
  labels-to-fields transform → columns: kind, name, exported_namespace, revision, suspended.
- Timeseries: reconcile p99 duration
  `histogram_quantile(0.99, sum by(le,controller)(rate(gotk_reconcile_duration_seconds_bucket[5m])))`;
  currently-failing gauge `gotk_reconcile_condition{type="Ready",status="False"}`.

**Row 2 — Resource usage**
- CPU by namespace `sum by(namespace)(rate(container_cpu_usage_seconds_total{container!=""}[5m]))`.
- Memory (working set) by namespace `sum by(namespace)(container_memory_working_set_bytes{container!=""})`.
- Top 10 pods by CPU / by memory (`topk(10, …)`).
- Pod restarts `sum by(namespace)(increase(kube_pod_container_status_restarts_total[1h]))`.
- Not-ready pods `count by(namespace)(kube_pod_status_ready{condition="true"}==0)`.

**Row 3 — Errors & Warnings (logs, Loki)**
- Log stream `{namespace=~"$namespace"} |~ "(?i)(error|warn|fatal|panic|exception|fail)"`.
- Error/warn rate by namespace
  `sum by(namespace)(count_over_time({namespace=~"$namespace"} |~ "(?i)(error|warn|fatal|panic|exception)"[$__auto]))`.
- Top 10 noisiest (error/warn) pods
  `topk(10, sum by(namespace,pod)(count_over_time({namespace=~"$namespace"} |~ "(?i)(error|warn|fatal)"[$__auto])))`.

Light, intentional overlap with the general **Loki — Logs** dashboard: that one stays the
free-form explorer; this row is the workload-scoped errors-at-a-glance view for the releases
context.

### Component 5 — Dashboard audit outcome

**Keep all four.** None is broken or fully redundant. Cleanup is limited to Fix A + Fix B
above. No whole-dashboard deletion (the only candidate would be folding AI LLM into Fleet,
explicitly not chosen).

## Files touched

| File | Change |
|---|---|
| `dev-workers-node.yaml` | **new** — dev-worker node_exporter scrape (`job=dev-worker-node`) |
| `flux-monitoring.yaml` | **new** — PodMonitor for the 4 Flux controllers |
| `k8s-releases-dashboard.yaml` | **new** — "Kubernetes — Releases & Workloads" dashboard |
| `kube-prometheus-stack.yaml` | edit — `kube-state-metrics` CRS + rbac.extraRules for Flux |
| `reporting-dashboard.yaml` | edit — +Runners section, +Workers section, trim AI row, fix AI CPU job |
| `apps/apps/ai/monitoring.yaml` | edit — relabel AI-LXC scrape to `job=ai-llm-node` (Fix A) |
| `kustomization.yaml` | edit — register the 3 new resource files |

## Validation plan

Flux reconciles on merge; validate against the live cluster (`kubectl --context admin@ai`,
Prometheus port-forward):

1. **YAML/JSON sanity (pre-merge):** each dashboard's JSON parses; `kustomize build` of the
   monitoring overlay succeeds; ServiceMonitor/PodMonitor schemas valid.
2. **Targets up:** in Prometheus, `up{job="dev-worker-node"}` = 3 series =1;
   Flux PodMonitor targets healthy.
3. **Metrics present:** `node_memory_MemTotal_bytes{job="dev-worker-node"}` returns 3;
   `gotk_resource_info` and `gotk_reconcile_duration_seconds_bucket` return data;
   `count(gotk_resource_info{customresource_kind="HelmRelease"})` matches the HelmRelease count.
4. **Dashboards render:** Fleet shows the two new sections with live data and a trimmed AI row;
   the new k8s-releases dashboard's three rows populate; no "No data"/datasource errors.
5. **No regressions:** existing Fleet rows unchanged; AI CPU panel shows all 3 AI nodes via
   the new job label.

## Risks & mitigations

- **KSM CRS config drift / RBAC** — pull the upstream Flux config verbatim; grant matching
  `extraRules`; if metrics are absent, check KSM logs for CR-state parse/RBAC errors.
- **AI job-relabel (Fix A)** — a cross-folder change to the `ai` app; verify no other query
  depends on the AI LXC's old default job label before switching (grep confirmed only the one
  Fleet panel references those IPs).
- **PodMonitor discovery** — must carry `release: kube-prometheus-stack` or the operator's
  selector ignores it (same convention as existing ServiceMonitors).
- **Dashboard JSON is one minified line** — edits are done by parsing → mutating → re-minifying
  (a script), not by hand, to avoid corruption; JSON re-validated before commit.
- **Fleet append math** — new panels start at `y=86`, `x`/`w` sum ≤24 per band, ids unique from
  41; re-run the overlap linter after editing.

## Cross-review

Per the user request, the plan and the implementation are both cross-reviewed with Codex
(codex-reviewed-planning skill): plan review before implementation, implementation review after.
