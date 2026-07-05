# Grafana Monitoring Improvements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add runner/worker resource sections to the AI Lab Fleet dashboard, add a new Kubernetes releases+workloads+logs dashboard (backed by new Flux metric collection), and fix the two real issues found in the dashboard audit.

**Architecture:** Everything lives under `kubernetes/apps/infrastructure/monitoring/` (Flux-reconciled on merge to `main`), except one relabel in `kubernetes/apps/apps/ai/monitoring.yaml`. Two new scrape/collection resources unlock missing data (dev-worker node_exporter; Flux `gotk_*`), then dashboards consume it. Dashboard JSON lives in ConfigMaps (one minified JSON string under a `data` key), edited by parse→mutate→re-minify scripts — never by hand — to avoid corruption.

**Tech Stack:** Kubernetes (Talos), Flux, kube-prometheus-stack (Prometheus Operator, kube-state-metrics, Grafana + dashboard sidecar), Loki + Alloy, PromQL/LogQL, Grafana dashboard schema v39.

## Global Constraints

- **Reconcile model:** `kubernetes/apps/**` is GitOps — Flux applies on merge to `main`. Pre-merge validation is local (`kustomize build`) + optional server-side dry-run; full behavioral validation is post-merge (Task 6). Copy verbatim from spec.
- **Operator discovery:** every `ServiceMonitor`/`PodMonitor` MUST carry label `release: kube-prometheus-stack` or the Prometheus Operator's selector ignores it.
- **Dashboard provisioning:** every dashboard ConfigMap MUST carry label `grafana_dashboard: "1"` (the Grafana sidecar selector). No folder annotations are used (all land in the default folder), matching the 4 existing dashboards.
- **Datasource references:** dashboards reference datasources ONLY through template variables — `${DS_PROMETHEUS}` (query `prometheus`) and `${DS_LOKI}` (query `loki`). Never hardcode datasource UIDs.
- **Cluster context:** all live checks use `kubectl --context admin@ai` (the default context is a DIFFERENT cluster). Prometheus is distroless → port-forward + curl the HTTP API.
- **Kustomization registration:** every new resource file MUST be added to `kubernetes/apps/infrastructure/monitoring/kustomization.yaml`.
- **Fleet layout:** `reporting-dashboard.yaml` is a flat 24-column grid of uncollapsed `type:"row"` markers + sibling panels positioned by `gridPos.y`. Current bottom `y=86`, next free panel `id=41`. New panels append at `y>=86`; `x`+`w` sum ≤24 per horizontal band; ids unique and ascending from 41.

---

### Task 1: Dev-worker node_exporter scrape

Unlocks CPU/mem/disk/net for dev-workers `.37/.38/.39` (node_exporter already runs there; nothing scrapes it). Direct mirror of `ci-runners-node.yaml`.

**Files:**
- Create: `kubernetes/apps/infrastructure/monitoring/dev-workers-node.yaml`
- Modify: `kubernetes/apps/infrastructure/monitoring/kustomization.yaml`
- Validate: local (`kustomize build`), optional server dry-run

**Interfaces:**
- Produces: Prometheus series `node_*{job="dev-worker-node"}` for 3 instances (`192.168.0.37/.38/.39:9100`). Consumed by Task 3 (Fleet Dev Workers section).

- [ ] **Step 1: Write the scrape manifest**

Create `kubernetes/apps/infrastructure/monitoring/dev-workers-node.yaml`:

```yaml
# Host-level node_exporter on the 3 dev-worker VMs (.37/.38/.39:9100, installed via the dev_worker
# ansible role which reuses the node_exporter role; its firewall is already open). Gives true
# CPU/mem/disk/network for the interactive dev/build workloads, shown on the AI Lab Fleet dashboard
# alongside the hypervisors and CI runners. relabel forces job="dev-worker-node" so dashboard queries
# stay clean. Mirror of ci-runners-node.yaml.
apiVersion: v1
kind: Service
metadata:
  name: dev-worker-node
  namespace: monitoring
  labels:
    app.kubernetes.io/name: dev-worker-node
spec:
  clusterIP: None
  ports:
    - { name: metrics, port: 9100, targetPort: 9100, protocol: TCP }
---
apiVersion: v1
kind: Endpoints
metadata:
  name: dev-worker-node
  namespace: monitoring
  labels:
    app.kubernetes.io/name: dev-worker-node
subsets:
  - addresses:
      - { ip: 192.168.0.37 }
      - { ip: 192.168.0.38 }
      - { ip: 192.168.0.39 }
    ports:
      - { name: metrics, port: 9100, protocol: TCP }
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: dev-worker-node
  namespace: monitoring
  labels:
    release: kube-prometheus-stack
spec:
  namespaceSelector:
    matchNames: [monitoring]
  selector:
    matchLabels:
      app.kubernetes.io/name: dev-worker-node
  endpoints:
    - port: metrics
      interval: 30s
      relabelings:
        - targetLabel: job
          replacement: dev-worker-node
```

- [ ] **Step 2: Register in kustomization**

In `kubernetes/apps/infrastructure/monitoring/kustomization.yaml`, add after the `ci-runners-node.yaml` line (line 12):

```yaml
  - dev-workers-node.yaml # host node_exporter on the 3 dev-worker VMs (scrape target)
```

- [ ] **Step 3: Validate the manifest fails BEFORE and builds AFTER**

Run (from repo root): `python -c "import yaml,sys; list(yaml.safe_load_all(open('kubernetes/apps/infrastructure/monitoring/dev-workers-node.yaml'))); print('yaml OK')"`
Expected: `yaml OK` (3 docs parse).

Run: `kubectl kustomize kubernetes/apps/infrastructure/monitoring >/dev/null && echo "kustomize OK"`
Expected: `kustomize OK` (dev-worker-node resources present, no errors).

Optional server dry-run (validates ServiceMonitor CRD schema against the live cluster, no apply):
`kubectl --context admin@ai apply --dry-run=server -f kubernetes/apps/infrastructure/monitoring/dev-workers-node.yaml`
Expected: `service/dev-worker-node created (server dry run)`, `endpoints/dev-worker-node created (server dry run)`, `servicemonitor.monitoring.coreos.com/dev-worker-node created (server dry run)`.

- [ ] **Step 4: Commit**

```bash
git add kubernetes/apps/infrastructure/monitoring/dev-workers-node.yaml \
        kubernetes/apps/infrastructure/monitoring/kustomization.yaml
git commit -m "feat(monitoring): scrape dev-worker node_exporter (job=dev-worker-node)"
```

---

### Task 2: Flux metrics collection (PodMonitor + kube-state-metrics custom-resource-state)

Unlocks `gotk_reconcile_*` (from the controllers) and `gotk_resource_info` (per HelmRelease/Kustomization/source, from KSM). Neither exists today.

**Files:**
- Create: `kubernetes/apps/infrastructure/monitoring/flux-monitoring.yaml`
- Modify: `kubernetes/apps/infrastructure/monitoring/kube-prometheus-stack.yaml` (add `kube-state-metrics:` values block)
- Modify: `kubernetes/apps/infrastructure/monitoring/kustomization.yaml`

**Interfaces:**
- Produces:
  - `gotk_reconcile_condition{type,status,kind,name,exported_namespace}`, `gotk_reconcile_duration_seconds_bucket{le,controller,kind}`, `gotk_suspend_status` (via PodMonitor).
  - `gotk_resource_info{customresource_kind, customresource_group, name, exported_namespace, ready, suspended, revision}` (Info metric, value 1; state in labels) via KSM CRS.
- Consumed by Task 4 (k8s-releases dashboard, Row 1).

- [ ] **Step 1: Write the Flux PodMonitor**

Create `kubernetes/apps/infrastructure/monitoring/flux-monitoring.yaml`:

```yaml
# Scrape the 4 Flux controllers' Prometheus endpoints (containerPort 8080, port name http-prom in
# gotk-components). Yields gotk_reconcile_condition / gotk_reconcile_duration_seconds / gotk_suspend_status.
# The controllers already expose these; there was no PodMonitor/ServiceMonitor collecting them.
# label release=kube-prometheus-stack so the operator's podMonitorSelector discovers it.
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: flux-controllers
  namespace: flux-system
  labels:
    release: kube-prometheus-stack
spec:
  namespaceSelector:
    matchNames: [flux-system]
  selector:
    matchExpressions:
      - key: app
        operator: In
        values: [source-controller, kustomize-controller, helm-controller, notification-controller]
  podMetricsEndpoints:
    - port: http-prom
      interval: 30s
      relabelings:
        - targetLabel: job
          replacement: flux
```

- [ ] **Step 2: Add kube-state-metrics custom-resource-state to the HelmRelease values**

In `kubernetes/apps/infrastructure/monitoring/kube-prometheus-stack.yaml`, add a top-level `kube-state-metrics:` block under `spec.values:` (same indent level as `prometheus:`, `grafana:`, `alertmanager:`). The kube-prometheus-stack chart passes this straight to the `kube-state-metrics` subchart; when `customResourceState.enabled` is true the subchart auto-adds `--custom-resource-state-config-file` and mounts the config, and the existing KSM ServiceMonitor scrapes the extra series (no additional scrape wiring).

```yaml
    kube-state-metrics:
      # Flux custom-resource-state → gotk_resource_info{customresource_kind, name, exported_namespace,
      # ready, suspended, revision}. Powers the "Kubernetes — Releases & Workloads" dashboard Row 1.
      rbac:
        extraRules:
          - apiGroups: ["kustomize.toolkit.fluxcd.io"]
            resources: ["kustomizations"]
            verbs: ["list", "watch"]
          - apiGroups: ["helm.toolkit.fluxcd.io"]
            resources: ["helmreleases"]
            verbs: ["list", "watch"]
          - apiGroups: ["source.toolkit.fluxcd.io"]
            resources: ["gitrepositories", "helmrepositories", "helmcharts", "ocirepositories", "buckets"]
            verbs: ["list", "watch"]
      customResourceState:
        enabled: true
        config:
          kind: CustomResourceStateMetrics
          spec:
            resources:
              - groupVersionKind: { group: kustomize.toolkit.fluxcd.io, version: v1, kind: Kustomization }
                metricNamePrefix: gotk
                metrics:
                  - name: resource_info
                    help: "The current state of a Flux Kustomization resource."
                    each:
                      type: Info
                      info: { labelsFromPath: { name: [metadata, name] } }
                    labelsFromPath:
                      exported_namespace: [metadata, namespace]
                      ready: [status, conditions, "[type=Ready]", status]
                      suspended: [spec, suspend]
                      revision: [status, lastAppliedRevision]
              - groupVersionKind: { group: helm.toolkit.fluxcd.io, version: v2, kind: HelmRelease }
                metricNamePrefix: gotk
                metrics:
                  - name: resource_info
                    help: "The current state of a Flux HelmRelease resource."
                    each:
                      type: Info
                      info: { labelsFromPath: { name: [metadata, name] } }
                    labelsFromPath:
                      exported_namespace: [metadata, namespace]
                      ready: [status, conditions, "[type=Ready]", status]
                      suspended: [spec, suspend]
                      revision: [status, lastAttemptedRevision]
              - groupVersionKind: { group: source.toolkit.fluxcd.io, version: v1, kind: GitRepository }
                metricNamePrefix: gotk
                metrics:
                  - name: resource_info
                    help: "The current state of a Flux GitRepository resource."
                    each:
                      type: Info
                      info: { labelsFromPath: { name: [metadata, name] } }
                    labelsFromPath:
                      exported_namespace: [metadata, namespace]
                      ready: [status, conditions, "[type=Ready]", status]
                      suspended: [spec, suspend]
                      revision: [status, artifact, revision]
              - groupVersionKind: { group: source.toolkit.fluxcd.io, version: v1, kind: HelmRepository }
                metricNamePrefix: gotk
                metrics:
                  - name: resource_info
                    help: "The current state of a Flux HelmRepository resource."
                    each:
                      type: Info
                      info: { labelsFromPath: { name: [metadata, name] } }
                    labelsFromPath:
                      exported_namespace: [metadata, namespace]
                      ready: [status, conditions, "[type=Ready]", status]
                      suspended: [spec, suspend]
              - groupVersionKind: { group: source.toolkit.fluxcd.io, version: v1, kind: OCIRepository }
                metricNamePrefix: gotk
                metrics:
                  - name: resource_info
                    help: "The current state of a Flux OCIRepository resource."
                    each:
                      type: Info
                      info: { labelsFromPath: { name: [metadata, name] } }
                    labelsFromPath:
                      exported_namespace: [metadata, namespace]
                      ready: [status, conditions, "[type=Ready]", status]
                      suspended: [spec, suspend]
```

**Verify label paths against upstream before committing:** the `ready`/`suspended` paths are stable across Flux kinds, but per-kind `revision` paths (`lastAppliedRevision` for Kustomization, `lastAttemptedRevision` for HelmRelease, `artifact.revision` for sources) should be diffed against the canonical Flux KSM config at `https://github.com/fluxcd/flux2-monitoring-example` (`monitoring/configs/kube-state-metrics/`). `revision` is a table-only nicety — a wrong path drops that one label, it does not break the ready/suspended queries the dashboard depends on. Fetch the upstream config in Step 4.

- [ ] **Step 3: Register the PodMonitor in kustomization**

In `kustomization.yaml`, add after the `alloy.yaml` line:

```yaml
  - flux-monitoring.yaml # PodMonitor for the 4 Flux controllers (gotk_reconcile_* metrics)
```

- [ ] **Step 4: Cross-check the CRS config against upstream, then validate**

Fetch upstream to diff the `revision`/label paths (do NOT wholesale-replace the structure above — just confirm the `labelsFromPath` values): open `https://github.com/fluxcd/flux2-monitoring-example` → `monitoring/configs/kube-state-metrics/`. Adjust any `revision` path that differs; keep `ready`/`suspended` as written.

Run: `python -c "import yaml; d=yaml.safe_load(open('kubernetes/apps/infrastructure/monitoring/kube-prometheus-stack.yaml')); ksm=d['spec']['values']['kube-state-metrics']; assert ksm['customResourceState']['enabled']; assert ksm['customResourceState']['config']['kind']=='CustomResourceStateMetrics'; print('KSM CRS values OK,', len(ksm['customResourceState']['config']['spec']['resources']), 'resources')"`
Expected: `KSM CRS values OK, 5 resources`

Run: `python -c "import yaml; list(yaml.safe_load_all(open('kubernetes/apps/infrastructure/monitoring/flux-monitoring.yaml'))); print('podmonitor yaml OK')"`
Expected: `podmonitor yaml OK`

Run: `kubectl kustomize kubernetes/apps/infrastructure/monitoring >/dev/null && echo "kustomize OK"`
Expected: `kustomize OK`

Optional server dry-run (PodMonitor CRD schema): `kubectl --context admin@ai apply --dry-run=server -f kubernetes/apps/infrastructure/monitoring/flux-monitoring.yaml`
Expected: `podmonitor.monitoring.coreos.com/flux-controllers created (server dry run)`

- [ ] **Step 5: Commit**

```bash
git add kubernetes/apps/infrastructure/monitoring/flux-monitoring.yaml \
        kubernetes/apps/infrastructure/monitoring/kube-prometheus-stack.yaml \
        kubernetes/apps/infrastructure/monitoring/kustomization.yaml
git commit -m "feat(monitoring): collect Flux metrics (PodMonitor + KSM custom-resource-state)"
```

---

### Task 3: AI Lab Fleet — Runners + Workers sections + AI fixes

Edit the single minified JSON in `reporting-dashboard.yaml` via a transform script (parse → append rows/panels + trim AI row + fix AI-CPU query → re-minify), and add a job relabel to the AI scrape.

**Files:**
- Modify: `kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml` (`data.reporting.json`)
- Modify: `kubernetes/apps/apps/ai/monitoring.yaml` (relabel AI scrape)
- Script (scratch, not committed): `scratchpad/edit_reporting.py`

**Interfaces:**
- Consumes: `node_*{job="ci-runner-node"}` (exists), `node_*{job="dev-worker-node"}` (Task 1), `node_*{job="ai-llm-node"}` (this task's relabel).
- Produces: updated `reporting.json` with 2 new rows (ids 41+), a trimmed AI row, and an IP-free AI-CPU panel.

**Canonical panel templates** (match the existing dashboard exactly). Stat tile:

```json
{ "id": 42, "type": "stat", "title": "Runners Up",
  "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
  "gridPos": { "x": 0, "y": 87, "w": 4, "h": 4 },
  "fieldConfig": { "defaults": { "unit": "none", "decimals": 0,
    "thresholds": { "mode": "absolute", "steps": [ { "color": "red", "value": null }, { "color": "green", "value": 5 } ] } }, "overrides": [] },
  "options": { "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false },
    "colorMode": "value", "graphMode": "area", "textMode": "auto", "justifyMode": "auto" },
  "targets": [ { "refId": "A", "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
    "expr": "count(up{job=\"ci-runner-node\"} == 1)", "instant": true } ] }
```

Timeseries panel:

```json
{ "id": 46, "type": "timeseries", "title": "CPU % per Runner",
  "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
  "gridPos": { "x": 0, "y": 91, "w": 12, "h": 7 },
  "fieldConfig": { "defaults": { "unit": "percent", "min": 0, "max": 100,
    "custom": { "drawStyle": "line", "fillOpacity": 10, "showPoints": "never" } }, "overrides": [] },
  "options": { "legend": { "displayMode": "list", "placement": "bottom" }, "tooltip": { "mode": "multi" } },
  "targets": [ { "refId": "A", "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
    "expr": "100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{job=\"ci-runner-node\",mode=\"idle\"}[5m])))",
    "legendFormat": "{{instance}}" } ] }
```

**Runners section** — row marker `id=41` at `y=86` (`title:"GitHub Actions Runners (host node_exporter)"`), then:

| id | type | x,y,w,h | title | expr |
|----|------|---------|-------|------|
| 42 | stat | 0,87,4,4 | Runners Up | `count(up{job="ci-runner-node"}==1)` (green≥5) |
| 43 | stat | 4,87,4,4 | Cores | `count(node_cpu_seconds_total{job="ci-runner-node",mode="idle"})` |
| 44 | stat | 8,87,4,4 | Memory | `sum(node_memory_MemTotal_bytes{job="ci-runner-node"})` (unit bytes) |
| 45 | stat | 12,87,6,4 | Fleet CPU % | `100*(1-avg(rate(node_cpu_seconds_total{job="ci-runner-node",mode="idle"}[5m])))` (unit percent) |
| 46 | stat | 18,87,6,4 | Fleet Mem % | `100*(1-sum(node_memory_MemAvailable_bytes{job="ci-runner-node"})/sum(node_memory_MemTotal_bytes{job="ci-runner-node"}))` (unit percent) |
| 47 | timeseries | 0,91,12,7 | CPU % per Runner | `100*(1-avg by(instance)(rate(node_cpu_seconds_total{job="ci-runner-node",mode="idle"}[5m])))` |
| 48 | timeseries | 12,91,12,7 | Mem % per Runner | `100*(1-node_memory_MemAvailable_bytes{job="ci-runner-node"}/node_memory_MemTotal_bytes{job="ci-runner-node"})` |
| 49 | timeseries | 0,98,12,7 | Root Disk Used % | `100*(1-node_filesystem_avail_bytes{job="ci-runner-node",mountpoint="/"}/node_filesystem_size_bytes{job="ci-runner-node",mountpoint="/"})` |
| 50 | timeseries | 12,98,12,7 | Network RX+/TX- | A: `sum by(instance)(rate(node_network_receive_bytes_total{job="ci-runner-node",device!~"lo\|veth.*\|docker.*\|cali.*\|cni.*\|br.*"}[5m]))` legend `{{instance}} rx`; B: `-sum by(instance)(rate(node_network_transmit_bytes_total{...same...}[5m]))` legend `{{instance}} tx` (unit Bps) |

**Workers section** — row marker `id=51` at `y=105` (`title:"Dev Workers (host node_exporter)"`), then panels ids 52–60 identical in shape to 42–50 but `job="dev-worker-node"`, Up threshold green≥3, y offset +19 (row stats at `y=106`, ts at `y=110` and `y=117`):

| id | type | x,y,w,h | title | expr (swap job=dev-worker-node) |
|----|------|---------|-------|------|
| 52 | stat | 0,106,4,4 | Workers Up | `count(up{job="dev-worker-node"}==1)` (green≥3) |
| 53 | stat | 4,106,4,4 | Cores | `count(node_cpu_seconds_total{job="dev-worker-node",mode="idle"})` |
| 54 | stat | 8,106,4,4 | Memory | `sum(node_memory_MemTotal_bytes{job="dev-worker-node"})` |
| 55 | stat | 12,106,6,4 | Fleet CPU % | `100*(1-avg(rate(node_cpu_seconds_total{job="dev-worker-node",mode="idle"}[5m])))` |
| 56 | stat | 18,106,6,4 | Fleet Mem % | `100*(1-sum(node_memory_MemAvailable_bytes{job="dev-worker-node"})/sum(node_memory_MemTotal_bytes{job="dev-worker-node"}))` |
| 57 | timeseries | 0,110,12,7 | CPU % per Worker | `100*(1-avg by(instance)(rate(node_cpu_seconds_total{job="dev-worker-node",mode="idle"}[5m])))` |
| 58 | timeseries | 12,110,12,7 | Mem % per Worker | `100*(1-node_memory_MemAvailable_bytes{job="dev-worker-node"}/node_memory_MemTotal_bytes{job="dev-worker-node"})` |
| 59 | timeseries | 0,117,12,7 | Root Disk Used % | `100*(1-node_filesystem_avail_bytes{job="dev-worker-node",mountpoint="/"}/node_filesystem_size_bytes{job="dev-worker-node",mountpoint="/"})` |
| 60 | timeseries | 12,117,12,7 | Network RX+/TX- | same as id 50 with `job="dev-worker-node"` |

- [ ] **Step 1: Relabel the AI scrape (Fix A)**

In `kubernetes/apps/apps/ai/monitoring.yaml`, the `ai-llm-node` ServiceMonitor (endpoints block ~line 69-71) currently has no relabeling. Add a `relabelings` under the endpoint so `job` is forced:

```yaml
  endpoints:
    - port: metrics
      interval: 15s
      relabelings:
        - targetLabel: job
          replacement: ai-llm-node
```

- [ ] **Step 2: Write the transform script**

Create `scratchpad/edit_reporting.py` (path: the session scratchpad). It must: load the ConfigMap YAML, `json.loads` the `data['reporting.json']`, then (a) **fix the AI-CPU panel**: find the panel whose title contains "AI Node CPU" and replace its target expr's `instance=~"192.168.0.4[456]:9100"` selector with `job="ai-llm-node"`; (b) **trim the AI row**: keep only panels titled iGPU Utilization, VRAM (VRAM Used vs Total), Decode Throughput, iGPU Temperature within the AI row's y-band (y 50–70), removing the other AI panels (GTT, Prompt Throughput, Busy Slots, iGPU Power, Requests) and re-flow the 4 kept panels to a tidy 2×2 (x 0/12, w 12) so no gap remains; (c) **append** the Runners row+panels and Workers row+panels per the tables above; (d) re-serialize with `json.dumps(dash, separators=(",",":"))` back into `data['reporting.json']`; (e) dump the ConfigMap YAML preserving the block. Because the file is a hand-written ConfigMap with one long JSON line, prefer editing ONLY that line: read the file text, locate the `reporting.json: |`-style key (it is inline JSON as a quoted string or block), replace the JSON payload, and write back — do not reformat the surrounding YAML.

- [ ] **Step 3: Run the transform**

Run: `python scratchpad/edit_reporting.py`
Expected: prints `panels: 40 -> N` and `OK` (N = 40 − removed-AI-panels + 2 rows + 18 new panels).

- [ ] **Step 4: Validate the resulting dashboard**

Run this lint (parses the edited JSON, checks datasource consistency, and panel overlap):

```bash
python - <<'PY'
import yaml, json
docs=list(yaml.safe_load_all(open("kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml",encoding="utf-8")))
cm=[d for d in docs if d and d.get("kind")=="ConfigMap"][0]
dash=json.loads(cm["data"]["reporting.json"])
panels=[p for p in dash["panels"] if p.get("type")!="row"]
# datasource consistency
bad=[p["id"] for p in panels if json.dumps(p.get("datasource"))!='{"type": "prometheus", "uid": "${DS_PROMETHEUS}"}']
assert not bad, f"non-standard datasource on panels {bad}"
# no hardcoded IPs remain
raw=cm["data"]["reporting.json"]
assert "192.168.0.4" not in raw, "hardcoded IP still present"
# overlap check
cells={}; overlap=[]
for p in panels:
    g=p["gridPos"]
    for yy in range(g["y"],g["y"]+g["h"]):
        for xx in range(g["x"],g["x"]+g["w"]):
            if (yy,xx) in cells: overlap.append((p["id"],cells[(yy,xx)]))
            cells[(yy,xx)]=p["id"]
assert not overlap, f"overlaps: {set(overlap)}"
ids=[p["id"] for p in dash["panels"]]
assert len(ids)==len(set(ids)), "duplicate ids"
print(f"OK panels={len(panels)} rows={len([p for p in dash['panels'] if p.get('type')=='row'])} maxY={max(p['gridPos']['y']+p['gridPos']['h'] for p in panels)}")
PY
```
Expected: `OK panels=… rows=6 maxY=124` (no assertion errors; 6 rows = original 4 + Runners + Workers).

Run: `python -c "import yaml; list(yaml.safe_load_all(open('kubernetes/apps/apps/ai/monitoring.yaml'))); print('ai monitoring yaml OK')"`
Expected: `ai monitoring yaml OK`

- [ ] **Step 5: Commit**

```bash
git add kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml \
        kubernetes/apps/apps/ai/monitoring.yaml
git commit -m "feat(monitoring): Fleet runner+worker sections; fix AI-node job label; trim AI row"
```

---

### Task 4: New dashboard — "Kubernetes — Releases & Workloads"

Build a new dashboard ConfigMap. Author the dashboard as pretty JSON in scratch, validate, then embed minified into the ConfigMap.

**Files:**
- Create: `kubernetes/apps/infrastructure/monitoring/k8s-releases-dashboard.yaml`
- Modify: `kubernetes/apps/infrastructure/monitoring/kustomization.yaml`
- Script (scratch): `scratchpad/build_k8s_releases.py` (assembles panels → minified JSON → ConfigMap)

**Interfaces:**
- Consumes: `gotk_resource_info`, `gotk_reconcile_duration_seconds_bucket`, `gotk_reconcile_condition` (Task 2); `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`, `kube_pod_container_status_restarts_total`, `kube_pod_status_ready` (existing cadvisor/KSM); Loki logs (existing).

Dashboard header:

```json
{ "title": "Kubernetes — Releases & Workloads", "uid": "k8s-releases",
  "tags": ["kubernetes","flux","releases","logs"], "timezone": "browser",
  "schemaVersion": 39, "refresh": "30s", "time": { "from": "now-6h", "to": "now" },
  "templating": { "list": [
    { "name": "DS_PROMETHEUS", "type": "datasource", "query": "prometheus", "current": {}, "hide": 0, "label": "Prometheus", "refresh": 1 },
    { "name": "DS_LOKI", "type": "datasource", "query": "loki", "current": {}, "hide": 0, "label": "Loki", "refresh": 1 },
    { "name": "namespace", "type": "query", "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
      "query": "label_values(kube_namespace_status_phase, namespace)", "includeAll": true, "multi": true, "current": { "text": "All", "value": "$__all" }, "refresh": 2 } ] } }
```

**Row 1 — Releases (Flux)** (row marker `id=1` y=0):

| id | type | x,y,w,h | title | query |
|----|------|---------|-------|-------|
| 2 | stat | 0,1,4,4 | HelmReleases Ready | `count(gotk_resource_info{customresource_kind="HelmRelease",ready="True"})` / total via 2nd target `count(gotk_resource_info{customresource_kind="HelmRelease"})` |
| 3 | stat | 4,1,4,4 | Kustomizations Ready | `count(gotk_resource_info{customresource_kind="Kustomization",ready="True"})` |
| 4 | stat | 8,1,4,4 | Sources Ready | `count(gotk_resource_info{customresource_group="source.toolkit.fluxcd.io",ready="True"})` |
| 5 | stat | 12,1,4,4 | Suspended | `count(gotk_resource_info{suspended="true"})` (yellow≥1) |
| 6 | stat | 16,1,4,4 | Not Ready | `count(gotk_resource_info{ready="False"})` (red≥1, green=0) |
| 7 | stat | 20,1,4,4 | Reconcile p99 | `histogram_quantile(0.99, sum by(le)(rate(gotk_reconcile_duration_seconds_bucket[5m])))` (unit s) |
| 8 | table | 0,5,24,7 | Not-ready / suspended resources | `gotk_resource_info{ready="False"} or gotk_resource_info{suspended="true"}`, `instant:true, format:table`; transforms: `labelsToFields` then `organize` to show columns customresource_kind, name, exported_namespace, ready, suspended, revision; hide Time/Value |
| 9 | timeseries | 0,12,24,7 | Reconcile duration p50/p99 by controller | A `histogram_quantile(0.5, sum by(le,controller)(rate(gotk_reconcile_duration_seconds_bucket[5m])))` legend `{{controller}} p50`; B p99 legend `{{controller}} p99` (unit s) |

**Row 2 — Resource usage** (row marker `id=20` y=19):

| id | type | x,y,w,h | title | query |
|----|------|---------|-------|-------|
| 21 | timeseries | 0,20,12,7 | CPU by namespace | `sum by(namespace)(rate(container_cpu_usage_seconds_total{container!="",namespace=~"$namespace"}[5m]))` (unit "short", legend `{{namespace}}`) |
| 22 | timeseries | 12,20,12,7 | Memory (working set) by namespace | `sum by(namespace)(container_memory_working_set_bytes{container!="",namespace=~"$namespace"})` (unit bytes) |
| 23 | timeseries | 0,27,12,7 | Top 10 pods by CPU | `topk(10, sum by(namespace,pod)(rate(container_cpu_usage_seconds_total{container!="",namespace=~"$namespace"}[5m])))` legend `{{namespace}}/{{pod}}` |
| 24 | timeseries | 12,27,12,7 | Top 10 pods by memory | `topk(10, sum by(namespace,pod)(container_memory_working_set_bytes{container!="",namespace=~"$namespace"}))` (unit bytes) |
| 25 | timeseries | 0,34,12,7 | Pod restarts (1h) | `sum by(namespace)(increase(kube_pod_container_status_restarts_total{namespace=~"$namespace"}[1h]))` |
| 26 | stat | 12,34,12,7 | Pods not ready | `count(kube_pod_status_ready{condition="true",namespace=~"$namespace"}==0)` (red≥1) |

**Row 3 — Errors & Warnings (logs)** (row marker `id=30` y=41):

| id | type | x,y,w,h | title | query (LogQL, datasource `${DS_LOKI}`) |
|----|------|---------|-------|------|
| 31 | logs | 0,42,24,8 | Error / warning logs | `{namespace=~"$namespace"} \|~ "(?i)(error\|warn\|fatal\|panic\|exception\|fail)"` (options: showTime, wrapLogMessage, sortOrder Descending) |
| 32 | timeseries | 0,50,12,7 | Error/warn rate by namespace | `sum by(namespace)(count_over_time({namespace=~"$namespace"} \|~ "(?i)(error\|warn\|fatal\|panic\|exception)"[$__auto]))` |
| 33 | timeseries | 12,50,12,7 | Top 10 noisiest (error/warn) pods | `topk(10, sum by(namespace,pod)(count_over_time({namespace=~"$namespace"} \|~ "(?i)(error\|warn\|fatal)"[$__auto])))` legend `{{namespace}}/{{pod}}` |

- [ ] **Step 1: Build the dashboard JSON**

Create `scratchpad/build_k8s_releases.py` that constructs the dashboard dict (header + the three rows and panels above, using the stat/timeseries/table/logs templates from the existing dashboards — copy the `logs` panel shape from `loki-logs-dashboard.yaml`, the `table` shape from Fleet id 23, timeseries from `ai-llm-dashboard.yaml`). Row-1/2/3 panels use `${DS_PROMETHEUS}` except Row-3 which uses `{ "type":"loki","uid":"${DS_LOKI}" }`. Serialize minified and write the ConfigMap:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: k8s-releases-dashboard
  namespace: monitoring
  labels:
    grafana_dashboard: "1"
data:
  k8s-releases.json: |
    <minified JSON here>
```

- [ ] **Step 2: Run the builder**

Run: `python scratchpad/build_k8s_releases.py`
Expected: `wrote k8s-releases-dashboard.yaml, panels=15 rows=3`

- [ ] **Step 3: Register in kustomization**

In `kustomization.yaml`, add after the `loki-logs-dashboard.yaml` line:

```yaml
  - k8s-releases-dashboard.yaml # "Kubernetes — Releases & Workloads": Flux release status + workload resource usage + error/warn logs
```

- [ ] **Step 4: Validate**

```bash
python - <<'PY'
import yaml, json
d=yaml.safe_load(open("kubernetes/apps/infrastructure/monitoring/k8s-releases-dashboard.yaml",encoding="utf-8"))
assert d["metadata"]["labels"]["grafana_dashboard"]=="1"
dash=json.loads(d["data"]["k8s-releases.json"])
vars={v["name"] for v in dash["templating"]["list"]}
assert {"DS_PROMETHEUS","DS_LOKI","namespace"} <= vars, vars
# every prometheus panel uses ${DS_PROMETHEUS}, every loki panel uses ${DS_LOKI}
for p in dash["panels"]:
    if p.get("type")=="row": continue
    for t in p.get("targets",[]):
        u=(t.get("datasource") or p.get("datasource") or {}).get("uid","")
        assert u in ("${DS_PROMETHEUS}","${DS_LOKI}"), (p["id"], u)
print("OK panels", len([p for p in dash["panels"] if p.get("type")!="row"]))
PY
kubectl kustomize kubernetes/apps/infrastructure/monitoring >/dev/null && echo "kustomize OK"
```
Expected: `OK panels 15` then `kustomize OK`

- [ ] **Step 5: Commit**

```bash
git add kubernetes/apps/infrastructure/monitoring/k8s-releases-dashboard.yaml \
        kubernetes/apps/infrastructure/monitoring/kustomization.yaml
git commit -m "feat(monitoring): add Kubernetes Releases & Workloads dashboard"
```

---

### Task 5: Open PR

**Files:** none (git/gh only).

- [ ] **Step 1: Push and open the PR**

```bash
git push -u origin feat/grafana-monitoring-improvements
gh pr create --base main --title "feat(monitoring): Grafana improvements — runner/worker Fleet sections, k8s releases dashboard, dashboard audit" \
  --body "$(cat <<'BODY'
## What
- AI Lab Fleet: new **GitHub Actions Runners** + **Dev Workers** resource sections.
- New **Kubernetes — Releases & Workloads** dashboard (Flux release status + workload resource usage + error/warn logs).
- New collection: `dev-worker-node` scrape; Flux PodMonitor + kube-state-metrics custom-resource-state (`gotk_resource_info`).
- Dashboard audit: fix hardcoded AI-node IPs (→ `job=ai-llm-node`); trim AI-row/AI-LLM redundancy. Keep all 4 dashboards.

## Validation
Local: JSON parse + datasource/overlap lint + `kubectl kustomize` all pass. Post-merge live verification per plan Task 6.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```
Expected: PR URL printed.

---

### Task 6: Post-merge live verification (after Flux reconciles)

**Files:** none (live cluster checks). Run after the PR merges and Flux reconciles (`flux get kustomizations` shows the monitoring KS reconciled).

- [ ] **Step 1: New scrape targets are up**

Port-forward Prometheus, then curl the API:
```bash
kubectl --context admin@ai -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090 &
sleep 3
curl -s 'http://localhost:9090/api/v1/query?query=up{job="dev-worker-node"}' | python -c "import sys,json; r=json.load(sys.stdin)['data']['result']; print('dev-worker up series:', len(r), [x['value'][1] for x in r])"
```
Expected: `dev-worker up series: 3 ['1','1','1']`

- [ ] **Step 2: Flux metrics present**

```bash
curl -s 'http://localhost:9090/api/v1/query?query=count(gotk_resource_info{customresource_kind="HelmRelease"})' | python -c "import sys,json; print('helmreleases:', json.load(sys.stdin)['data']['result'])"
curl -s 'http://localhost:9090/api/v1/query?query=count(gotk_reconcile_duration_seconds_count)' | python -c "import sys,json; print('reconcile series:', json.load(sys.stdin)['data']['result'])"
```
Expected: non-empty results; helmreleases count matches `kubectl --context admin@ai get helmrelease -A | wc -l` (minus header).

- [ ] **Step 3: Dashboards render**

In Grafana (`https://grafana.chifor.me`): open **AI Lab Fleet** → confirm the two new bottom sections (Runners, Workers) show live data, and the AI row shows all 3 AI nodes' CPU (no "No data") and only the 4 trimmed panels. Open **Kubernetes — Releases & Workloads** → all three rows populate; the not-ready table is empty or lists genuinely-failing resources; the logs panel streams. Confirm no datasource errors.

- [ ] **Step 4: No regressions**

Confirm the existing Fleet rows (Hypervisors, Instances, AI, Storage) still render, and the standalone AI LLM / Loki / Trivy dashboards are unchanged.

---

## Self-Review

**Spec coverage:**
- Runner section → Task 3. Worker section → Task 1 (scrape) + Task 3 (panels). ✓
- New k8s dashboard: releases → Task 2 (metrics) + Task 4 Row 1; resource usage → Task 4 Row 2; error/warn logs → Task 4 Row 3. ✓
- Review/cleanup: keep-all + Fix A (AI IP) → Task 3 Step 1/2; Fix B (AI redundancy trim) → Task 3 Step 2. ✓
- Cross-review: handled by the codex-reviewed-planning skill wrapping this plan (plan review now, impl review after Task 4). ✓

**Placeholder scan:** CRS `revision` label paths flagged with an explicit upstream cross-check (Task 2 Step 4) rather than left vague; all queries are concrete. The two transform scripts are described with exact inputs/outputs and validated by lint gates — acceptable because the deterministic artifact (the resulting JSON) is fully specified by the panel tables + templates and checked by the lint.

**Type/label consistency:** job labels (`ci-runner-node`, `dev-worker-node`, `ai-llm-node`) consistent across tasks; `gotk_resource_info` label names (`customresource_kind`, `ready`, `suspended`, `exported_namespace`, `revision`) consistent between Task 2 (producer) and Task 4 (consumer); panel ids unique and non-overlapping (verified by lint gates in Tasks 3 & 4).
