# Grafana Monitoring Improvements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add runner/worker resource sections to the AI Lab Fleet dashboard, add a new Kubernetes releases+workloads+logs dashboard (backed by new Flux metric collection), and fix the two real issues found in the dashboard audit.

**Architecture:** Everything lives under `kubernetes/apps/infrastructure/monitoring/` (Flux-reconciled on merge to `main`), except one relabel in `kubernetes/apps/apps/ai/monitoring.yaml`. Two new scrape/collection resources unlock missing data (dev-worker node_exporter; Flux `gotk_*`), then dashboards consume it. **Both Fleet and the new dashboard are produced by Python generator scripts under `scripts/` (`gen-reporting-dashboard.py`, new `gen-k8s-releases-dashboard.py`) — the `*-dashboard.yaml` ConfigMaps are generated output; never hand-edit them.**

**Tech Stack:** Kubernetes (Talos), Flux v2.8, kube-prometheus-stack (Prometheus Operator, kube-state-metrics, Grafana + dashboard sidecar), Loki + Alloy, PromQL/LogQL, Grafana dashboard schema v39, Python dashboard generators.

## Global Constraints

- **Reconcile model:** `kubernetes/apps/**` is GitOps — Flux applies on merge to `main`. Pre-merge validation is local (`kustomize build`) + optional server-side dry-run; full behavioral validation is post-merge (Task 6).
- **Operator discovery:** every `ServiceMonitor`/`PodMonitor` MUST carry label `release: kube-prometheus-stack`.
- **Dashboard provisioning:** every dashboard ConfigMap MUST carry label `grafana_dashboard: "1"`. No folder annotations.
- **Dashboards are generated:** edit the `scripts/gen-*.py` generator, then run it to regenerate the ConfigMap. Never edit `reporting-dashboard.yaml`/`k8s-releases-dashboard.yaml` by hand. Panel `id`s are auto-assigned by the generator's `_nid()` counter in creation order — do NOT hand-number panels.
- **Datasource references:** dashboards reference datasources ONLY through template variables `${DS_PROMETHEUS}` / `${DS_LOKI}`. Never hardcode datasource UIDs.
- **Cluster context:** all live checks use `kubectl --context admin@ai`. Prometheus is distroless → port-forward + curl the HTTP API.
- **Kustomization registration:** every new resource file MUST be added to `kubernetes/apps/infrastructure/monitoring/kustomization.yaml`.
- **Grid invariants (assert in every generator's self-check):** unique panel ids, no gridPos overlaps, `x+w<=24` per band, and the Fleet dashboard ends at `maxY=124` after the two new sections.

## Codex Review (Round 1) — Dispositions

Genuine codex `gpt-5.5` review (27 markers; artifact at `plans/2026-07-05-grafana-monitoring-codex-review-round1.md`). All findings **accepted and folded into the tasks below**, except two where the intent is honored but the exact expression is corrected (documented inline):

- **Fleet is script-generated** → Task 3 now edits `scripts/gen-reporting-dashboard.py` (not the JSON). *(critical)*
- **AI-CPU keep/trim contradiction** → AI row keeps **5** panels incl. AI Node CPU (fixed to job label); removes 5. Counts updated everywhere.
- **PodMonitor** → lives in ns `monitoring`, `namespaceSelector: [flux-system]`, `podMetricsEndpoints`, `app In (...)` — verified against live pods (label `app: <controller>`, port `http-prom`).
- **HelmRelease revision** → `status.history[0].chartVersion` (the deployed release), not `lastAttemptedRevision`.
- **KSM CRS** → keep default collectors enabled (Task 4 needs standard `kube_*`); `metricNamePrefix: gotk` + `resource_info` per kind; plural RBAC; exact GVKs.
- **`ready`/`suspended` casing** → `ready="True"`, Not-Ready uses `ready!="True"` (catches `Unknown`), `suspended="true"`.
- **Zero-target guards** → `... or vector(0)` on discrete count stats (Up, Not Ready, Suspended, Pods-not-ready).
- **k8s dashboard panel count** → **17** non-row panels (8+6+3), not 15; grid overlap lint added.
- **Table label names** → organize transform renames the real KSM labels `customresource_kind`/`exported_namespace`.
- **topk before aggregation** + `pod!=""`; **namespace var** `label_values(kube_namespace_status_phase, namespace)` + `allValue: ".*"`.
- **Refinement 1 (vs codex):** Pods-not-ready uses `count(kube_pod_status_ready{condition="true"} == 0) or vector(0)` — `count` of the filtered series, NOT `sum` (codex suggested `sum`, which would add zeros and always yield 0). Also excludes Succeeded/Completed pods via a `kube_pod_status_phase` join filter.
- **Refinement 2 (vs codex):** reconcile-duration breakdown groups by `kind` (`sum by(le, kind)`), the label Flux's `gotk_reconcile_duration_seconds` actually carries, not `controller`; Task 6 verifies the real label once metrics flow and switches if needed.

---

### Task 1: Dev-worker node_exporter scrape

Unlocks CPU/mem/disk/net for dev-workers `.37/.38/.39` (node_exporter already runs there; nothing scrapes it). Direct mirror of `ci-runners-node.yaml`.

**Files:**
- Create: `kubernetes/apps/infrastructure/monitoring/dev-workers-node.yaml`
- Modify: `kubernetes/apps/infrastructure/monitoring/kustomization.yaml`

**Interfaces:**
- Produces: `node_*{job="dev-worker-node"}` for `192.168.0.37/.38/.39:9100`. Consumed by Task 3.

- [ ] **Step 1: Write the scrape manifest**

Create `kubernetes/apps/infrastructure/monitoring/dev-workers-node.yaml`:

```yaml
# Host-level node_exporter on the 3 dev-worker VMs (.37/.38/.39:9100, installed via the dev_worker
# ansible role which reuses the node_exporter role; its firewall is already open). Gives true
# CPU/mem/disk/network for the interactive dev/build workloads, shown on the AI Lab Fleet dashboard
# alongside the hypervisors and CI runners. relabel forces job="dev-worker-node". Mirror of ci-runners-node.yaml.
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

In `kustomization.yaml`, add after the `ci-runners-node.yaml` line (line 12):

```yaml
  - dev-workers-node.yaml # host node_exporter on the 3 dev-worker VMs (scrape target)
```

- [ ] **Step 3: Validate**

```
python -c "import yaml; list(yaml.safe_load_all(open('kubernetes/apps/infrastructure/monitoring/dev-workers-node.yaml'))); print('yaml OK')"
kubectl kustomize kubernetes/apps/infrastructure/monitoring >/dev/null && echo "kustomize OK"
kubectl --context admin@ai apply --dry-run=server -f kubernetes/apps/infrastructure/monitoring/dev-workers-node.yaml
```
Expected: `yaml OK`; `kustomize OK`; three `... created (server dry run)` lines.

- [ ] **Step 4: Commit**

```bash
git add kubernetes/apps/infrastructure/monitoring/dev-workers-node.yaml \
        kubernetes/apps/infrastructure/monitoring/kustomization.yaml
git commit -m "feat(monitoring): scrape dev-worker node_exporter (job=dev-worker-node)"
```

---

### Task 2: Flux metrics collection (PodMonitor + kube-state-metrics custom-resource-state)

Unlocks `gotk_reconcile_*` (controllers, via PodMonitor) and `gotk_resource_info` (per HelmRelease/Kustomization/source, via KSM CRS). Neither exists today (verified: `gotk_*` returns empty pre-merge).

**Files:**
- Create: `kubernetes/apps/infrastructure/monitoring/flux-monitoring.yaml`
- Modify: `kubernetes/apps/infrastructure/monitoring/kube-prometheus-stack.yaml`
- Modify: `kubernetes/apps/infrastructure/monitoring/kustomization.yaml`

**Interfaces (verified against live cluster, Flux v2.8.8):**
- Controller pods carry label `app: <controller>` and container port `http-prom=8080`.
- Produces `gotk_reconcile_condition{type,status,kind,name,...}`, `gotk_reconcile_duration_seconds_bucket{le,kind,...}` (PodMonitor); `gotk_resource_info{customresource_kind, customresource_group, name, exported_namespace, ready, suspended, revision}` (KSM CRS, Info metric value 1). Consumed by Task 4.

- [ ] **Step 1: Write the Flux PodMonitor** (in ns `monitoring`, mirroring where the other monitors live)

Create `kubernetes/apps/infrastructure/monitoring/flux-monitoring.yaml`:

```yaml
# Scrape the 4 Flux controllers' Prometheus endpoints (container port http-prom=8080). Yields
# gotk_reconcile_condition / gotk_reconcile_duration_seconds / gotk_suspend_status. The controllers
# already expose these; nothing collected them. PodMonitor lives in `monitoring` (like the other
# monitors, proven-scraped) and selects the flux-system pods by their `app` label.
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: flux-controllers
  namespace: monitoring
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

In `kube-prometheus-stack.yaml`, add a top-level `kube-state-metrics:` block under `spec.values:` (same indent as `prometheus:`/`grafana:`). **Keep default KSM collectors enabled** (do NOT set `collectors: []` or `--custom-resource-state-only`) — Task 4 needs standard `kube_pod_*`, `container_*`, `kube_namespace_*` metrics. The subchart auto-adds `--custom-resource-state-config-file` and mounts the config when `customResourceState.enabled`; the existing KSM ServiceMonitor scrapes the extra series.

```yaml
    kube-state-metrics:
      # Flux custom-resource-state → gotk_resource_info{customresource_kind, name, exported_namespace,
      # ready, suspended, revision}. Powers the "Kubernetes — Releases & Workloads" dashboard Row 1.
      # Default collectors stay ON (Task 4 uses kube_pod_*/container_*/kube_namespace_*).
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
                      revision: [status, history, "0", chartVersion]  # deployed release (codex round1)
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
                      revision: [status, artifact, revision]
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
                      revision: [status, artifact, revision]
```

- [ ] **Step 3: Register the PodMonitor in kustomization** — add after the `alloy.yaml` line:

```yaml
  - flux-monitoring.yaml # PodMonitor for the 4 Flux controllers (gotk_reconcile_* metrics)
```

- [ ] **Step 4: Cross-check upstream, then validate**

Diff the per-kind `revision` paths against `https://github.com/fluxcd/flux2-monitoring-example` (`monitoring/configs/kube-state-metrics/`); `ready`/`suspended` are stable. `revision` is table-only — a wrong path drops that one column, not the ready/suspended queries.

```
python -c "import yaml; d=yaml.safe_load(open('kubernetes/apps/infrastructure/monitoring/kube-prometheus-stack.yaml')); ksm=d['spec']['values']['kube-state-metrics']; assert ksm['customResourceState']['enabled']; assert ksm['customResourceState']['config']['kind']=='CustomResourceStateMetrics'; print('KSM CRS OK,', len(ksm['customResourceState']['config']['spec']['resources']), 'resources'); assert 'collectors' not in ksm or ksm['collectors'], 'default collectors must stay enabled'"
python -c "import yaml; list(yaml.safe_load_all(open('kubernetes/apps/infrastructure/monitoring/flux-monitoring.yaml'))); print('podmonitor yaml OK')"
kubectl kustomize kubernetes/apps/infrastructure/monitoring >/dev/null && echo "kustomize OK"
kubectl --context admin@ai apply --dry-run=server -f kubernetes/apps/infrastructure/monitoring/flux-monitoring.yaml
```
Expected: `KSM CRS OK, 5 resources`; `podmonitor yaml OK`; `kustomize OK`; `podmonitor.monitoring.coreos.com/flux-controllers created (server dry run)`.

- [ ] **Step 5: Commit**

```bash
git add kubernetes/apps/infrastructure/monitoring/flux-monitoring.yaml \
        kubernetes/apps/infrastructure/monitoring/kube-prometheus-stack.yaml \
        kubernetes/apps/infrastructure/monitoring/kustomization.yaml
git commit -m "feat(monitoring): collect Flux metrics (PodMonitor + KSM custom-resource-state)"
```

---

### Task 3: AI Lab Fleet — Runners + Workers sections + AI fixes (edit the generator)

The Fleet dashboard is generated by `scripts/gen-reporting-dashboard.py` (helpers `row/ts/stat/...`; ids auto-assigned by `_nid()`; panels appended in order). Edit the generator and regenerate — do NOT touch `reporting-dashboard.yaml`.

**Files:**
- Modify: `scripts/gen-reporting-dashboard.py`
- Regenerate (output): `kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml`
- Modify: `kubernetes/apps/apps/ai/monitoring.yaml` (relabel AI scrape)

- [ ] **Step 1: Fix A — relabel the AI scrape**

In `kubernetes/apps/apps/ai/monitoring.yaml`, the `ai-llm-node` ServiceMonitor endpoint (~line 69-71) has no relabeling. Add:

```yaml
  endpoints:
    - port: metrics
      interval: 15s
      relabelings:
        - targetLabel: job
          replacement: ai-llm-node
```

- [ ] **Step 2: Fix A (cont.) + AI trim in the generator**

In `scripts/gen-reporting-dashboard.py`:

(a) Change the AI-node selector constant (line 20) from the hardcoded IPs to the job label:

```python
AINODE = 'job="ai-llm-node"'  # the 3 AI LXCs' node_exporter (relabeled; was instance-IP regex)
```

(b) Replace the entire AI section panel list (lines 213-229) with the trimmed 5-panel version — keep AI Node CPU (now IP-free via AINODE), iGPU Utilization, VRAM, Decode Throughput, iGPU Temperature; drop GTT, Prompt Throughput, Busy Slots, iGPU Power, Requests. Filter the `amdgpu_*` series by the AI job label so no other node_exporter textfile metric can leak in:

```python
panels += [
    ts("AI Node CPU %", 0, 51, 8, 7,
       [f'100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{{{AINODE},mode="idle"}}[5m])))'],
       "percent", maxv=100),
    ts("iGPU Utilization", 8, 51, 8, 7, ['amdgpu_gpu_busy_percent{job="ai-llm-node"}'], "percent"),
    ts("VRAM Used vs Total", 16, 51, 8, 7,
       ['amdgpu_vram_used_bytes{job="ai-llm-node"}', 'amdgpu_vram_total_bytes{job="ai-llm-node"}'],
       "bytes", legends=["{{instance}} used", "{{instance}} total"]),
    ts("Decode Throughput (tokens/s)", 0, 58, 12, 7, ["llamacpp:predicted_tokens_seconds"], "tok/s"),
    ts("iGPU Temperature", 12, 58, 12, 7, ['amdgpu_temp_millicelsius{job="ai-llm-node"}/1000'], "celsius"),
]
```

Storage stays at its existing y (row 71). The trimmed AI block ends at y=65, leaving a harmless 6px gap before Storage (Grafana renders rows fine; leaving Storage's y untouched keeps the diff minimal and avoids reflow risk).

- [ ] **Step 3: Add Runners + Workers sections (append after Storage in the generator)**

After the Storage `panels += [...]` block (after line 248, before the `dashboard = {...}` dict), append. Note `NETDEV` already excludes docker/veth (correct for runner hosts). Ids are auto-assigned; positions are explicit:

```python
# ───────────────────────── GitHub Actions Runners ─────────────────────────
RUNNERS = 'job="ci-runner-node"'
panels.append(row("GitHub Actions Runners (host node_exporter)", 86))
panels += [
    stat("Runners Up", 0, 87, 4, 4, f'count(up{{{RUNNERS}}} == 1) or vector(0)',
         steps=[{"color": "red", "value": None}, {"color": "green", "value": 5}]),
    stat("Runner Cores", 4, 87, 4, 4, f'count(node_cpu_seconds_total{{{RUNNERS},mode="idle"}})'),
    stat("Runner Memory", 8, 87, 4, 4, f'sum(node_memory_MemTotal_bytes{{{RUNNERS}}})', unit="bytes", decimals=1),
    stat("Fleet CPU Used", 12, 87, 6, 4,
         f'100 * (1 - avg(rate(node_cpu_seconds_total{{{RUNNERS},mode="idle"}}[5m])))',
         unit="percent", decimals=1, steps=PCT),
    stat("Fleet Memory Used", 18, 87, 6, 4,
         f'100 * (1 - sum(node_memory_MemAvailable_bytes{{{RUNNERS}}}) / sum(node_memory_MemTotal_bytes{{{RUNNERS}}}))',
         unit="percent", decimals=1, steps=PCT),
    ts("CPU % per Runner", 0, 91, 12, 7,
       [f'100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{{{RUNNERS},mode="idle"}}[5m])))'],
       "percent", maxv=100),
    ts("Memory % per Runner", 12, 91, 12, 7,
       [f'100 * (1 - node_memory_MemAvailable_bytes{{{RUNNERS}}} / node_memory_MemTotal_bytes{{{RUNNERS}}})'],
       "percent", maxv=100),
    ts("Root Disk Used % per Runner", 0, 98, 12, 7,
       [f'100 * (1 - node_filesystem_avail_bytes{{{RUNNERS},mountpoint="/"}} / node_filesystem_size_bytes{{{RUNNERS},mountpoint="/"}})'],
       "percent", maxv=100),
    ts("Network per Runner (RX+ / TX-)", 12, 98, 12, 7,
       [f'sum by (instance) (rate(node_network_receive_bytes_total{{{RUNNERS},{NETDEV}}}[5m]))',
        f'0 - sum by (instance) (rate(node_network_transmit_bytes_total{{{RUNNERS},{NETDEV}}}[5m]))'],
       "Bps", legends=["{{instance}} rx", "{{instance}} tx"]),
]

# ───────────────────────── Dev Workers ─────────────────────────
WORKERS = 'job="dev-worker-node"'
panels.append(row("Dev Workers (host node_exporter)", 105))
panels += [
    stat("Workers Up", 0, 106, 4, 4, f'count(up{{{WORKERS}}} == 1) or vector(0)',
         steps=[{"color": "red", "value": None}, {"color": "green", "value": 3}]),
    stat("Worker Cores", 4, 106, 4, 4, f'count(node_cpu_seconds_total{{{WORKERS},mode="idle"}})'),
    stat("Worker Memory", 8, 106, 4, 4, f'sum(node_memory_MemTotal_bytes{{{WORKERS}}})', unit="bytes", decimals=1),
    stat("Fleet CPU Used", 12, 106, 6, 4,
         f'100 * (1 - avg(rate(node_cpu_seconds_total{{{WORKERS},mode="idle"}}[5m])))',
         unit="percent", decimals=1, steps=PCT),
    stat("Fleet Memory Used", 18, 106, 6, 4,
         f'100 * (1 - sum(node_memory_MemAvailable_bytes{{{WORKERS}}}) / sum(node_memory_MemTotal_bytes{{{WORKERS}}}))',
         unit="percent", decimals=1, steps=PCT),
    ts("CPU % per Worker", 0, 110, 12, 7,
       [f'100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{{{WORKERS},mode="idle"}}[5m])))'],
       "percent", maxv=100),
    ts("Memory % per Worker", 12, 110, 12, 7,
       [f'100 * (1 - node_memory_MemAvailable_bytes{{{WORKERS}}} / node_memory_MemTotal_bytes{{{WORKERS}}})'],
       "percent", maxv=100),
    ts("Root Disk Used % per Worker", 0, 117, 12, 7,
       [f'100 * (1 - node_filesystem_avail_bytes{{{WORKERS},mountpoint="/"}} / node_filesystem_size_bytes{{{WORKERS},mountpoint="/"}})'],
       "percent", maxv=100),
    ts("Network per Worker (RX+ / TX-)", 12, 117, 12, 7,
       [f'sum by (instance) (rate(node_network_receive_bytes_total{{{WORKERS},{NETDEV}}}[5m]))',
        f'0 - sum by (instance) (rate(node_network_transmit_bytes_total{{{WORKERS},{NETDEV}}}[5m]))'],
       "Bps", legends=["{{instance}} rx", "{{instance}} tx"]),
]
```

- [ ] **Step 4: Regenerate and validate**

```
python scripts/gen-reporting-dashboard.py
```
Expected: `wrote …/reporting-dashboard.yaml (49 panels, 6 rows)`.

Then lint the generated output (datasource consistency, no hardcoded IPs, no overlaps, unique ids, x+w≤24, contiguity, maxY):

```bash
python - <<'PY'
import yaml, json
docs=list(yaml.safe_load_all(open("kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml",encoding="utf-8")))
cm=[d for d in docs if d and d.get("kind")=="ConfigMap"][0]
raw=cm["data"]["reporting.json"]; dash=json.loads(raw)
assert "192.168.0.4" not in raw, "hardcoded IP still present"
panels=[p for p in dash["panels"] if p.get("type")!="row"]
bad=[p["id"] for p in panels if json.dumps(p.get("datasource"))!='{"type": "prometheus", "uid": "${DS_PROMETHEUS}"}']
assert not bad, f"non-standard datasource {bad}"
ids=[p["id"] for p in dash["panels"]]; assert len(ids)==len(set(ids)), "dup ids"
cells={}; overlap=set()
for p in panels:
    g=p["gridPos"]; assert g["x"]+g["w"]<=24, (p["id"],"x+w>24")
    for yy in range(g["y"],g["y"]+g["h"]):
        for xx in range(g["x"],g["x"]+g["w"]):
            if (yy,xx) in cells: overlap.add((p["id"],cells[(yy,xx)]))
            cells[(yy,xx)]=p["id"]
assert not overlap, f"overlaps {overlap}"
rows=sorted(p["gridPos"]["y"] for p in dash["panels"] if p.get("type")=="row")
maxY=max(p["gridPos"]["y"]+p["gridPos"]["h"] for p in panels)
assert rows[-2:]==[86,105], f"runner/worker rows at {rows}"
assert maxY==124, f"maxY={maxY}"
print(f"OK panels={len(panels)} rows={len(rows)} rowY={rows} maxY={maxY}")
PY
python -c "import yaml; list(yaml.safe_load_all(open('kubernetes/apps/apps/ai/monitoring.yaml'))); print('ai monitoring yaml OK')"
```
Expected: `OK panels=49 rows=6 rowY=[0, 21, 50, 71, 86, 105] maxY=124`; `ai monitoring yaml OK`.

- [ ] **Step 5: Commit**

```bash
git add scripts/gen-reporting-dashboard.py \
        kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml \
        kubernetes/apps/apps/ai/monitoring.yaml
git commit -m "feat(monitoring): Fleet runner+worker sections; fix AI-node job label; trim AI row"
```

---

### Task 4: New dashboard — "Kubernetes — Releases & Workloads" (new generator)

Create a generator `scripts/gen-k8s-releases-dashboard.py` (same style as `gen-reporting-dashboard.py`) emitting the ConfigMap. 3 rows / **17** non-row panels (8 Flux + 6 resource + 3 logs).

**Files:**
- Create: `scripts/gen-k8s-releases-dashboard.py`
- Generate (output): `kubernetes/apps/infrastructure/monitoring/k8s-releases-dashboard.yaml`
- Modify: `kubernetes/apps/infrastructure/monitoring/kustomization.yaml`

- [ ] **Step 1: Write the generator**

Create `scripts/gen-k8s-releases-dashboard.py`:

```python
#!/usr/bin/env python3
"""Generate the "Kubernetes — Releases & Workloads" Grafana dashboard ConfigMap.

Row 1 Releases (Flux): gotk_resource_info (KSM CRS) + gotk_reconcile_* (controllers).
Row 2 Resource usage: cadvisor + kube-state-metrics.
Row 3 Errors & Warnings: Loki logs. Datasource vars ${DS_PROMETHEUS} / ${DS_LOKI}.

    python scripts/gen-k8s-releases-dashboard.py
"""
import json, pathlib

PROM = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
LOKI = {"type": "loki", "uid": "${DS_LOKI}"}
NS = 'namespace=~"$namespace"'
_pid = 0
def _nid():
    global _pid; _pid += 1; return _pid

def row(title, y):
    return {"id": _nid(), "type": "row", "title": title, "collapsed": False,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}

def stat(title, x, y, w, h, expr, unit="none", decimals=0, steps=None, ds=PROM):
    return {"id": _nid(), "type": "stat", "title": title, "datasource": ds,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals,
                "thresholds": {"mode": "absolute", "steps": steps or [{"color": "blue", "value": None}]}},
                "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "value", "graphMode": "none", "textMode": "auto", "justifyMode": "auto"},
            "targets": [{"refId": "A", "datasource": ds, "expr": expr, "instant": True}]}

def ts(title, x, y, w, h, exprs, unit="short", legends=None, ds=PROM, fill=10):
    legends = legends or ["{{namespace}}"] * len(exprs)
    return {"id": _nid(), "type": "timeseries", "title": title, "datasource": ds,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "fieldConfig": {"defaults": {"unit": unit,
                "custom": {"drawStyle": "line", "fillOpacity": fill, "showPoints": "never"}}, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
            "targets": [{"refId": chr(65+i), "datasource": ds, "expr": e,
                         "legendFormat": legends[i]} for i, e in enumerate(exprs)]}

def logs(title, x, y, w, h, expr):
    return {"id": _nid(), "type": "logs", "title": title, "datasource": LOKI,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                        "enableLogDetails": True, "dedupStrategy": "none"},
            "targets": [{"refId": "A", "datasource": LOKI, "expr": expr, "queryType": "range"}]}

def rel_table(title, x, y, w, h):
    # not-ready OR suspended Flux resources; instant table; organize renames the real KSM labels.
    expr = 'gotk_resource_info{ready!="True"} or gotk_resource_info{suspended="true"}'
    keep_rename = {"customresource_kind": "Kind", "name": "Name", "exported_namespace": "Namespace",
                   "ready": "Ready", "suspended": "Suspended", "revision": "Revision"}
    exclude = ["Time", "Value", "__name__", "customresource_group", "customresource_version",
               "container", "endpoint", "instance", "job", "namespace", "pod", "service", "uid"]
    return {"id": _nid(), "type": "table", "title": title, "datasource": PROM,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "fieldConfig": {"defaults": {"custom": {"align": "auto", "filterable": True,
                "cellOptions": {"type": "auto"}}}, "overrides": []},
            "options": {"showHeader": True, "footer": {"show": False}, "cellHeight": "sm"},
            "transformations": [{"id": "organize", "options": {
                "excludeByName": {k: True for k in exclude}, "renameByName": keep_rename, "indexByName": {}}}],
            "targets": [{"refId": "A", "datasource": PROM, "expr": expr, "format": "table", "instant": True}]}

GREEN0_REDpos = [{"color": "green", "value": None}, {"color": "red", "value": 1}]
BLUE_YELLOW1 = [{"color": "blue", "value": None}, {"color": "yellow", "value": 1}]
SRC = 'customresource_kind=~"GitRepository|HelmRepository|OCIRepository"'

panels = []
# ── Row 1: Releases (Flux) ──
panels.append(row("Releases (Flux — reconcile status)", 0))
panels += [
    stat("HelmReleases Ready", 0, 1, 4, 4,
         'count(gotk_resource_info{customresource_kind="HelmRelease",ready="True"}) or vector(0)'),
    stat("Kustomizations Ready", 4, 1, 4, 4,
         'count(gotk_resource_info{customresource_kind="Kustomization",ready="True"}) or vector(0)'),
    stat("Sources Ready", 8, 1, 4, 4,
         f'count(gotk_resource_info{{{SRC},ready="True"}}) or vector(0)'),
    stat("Suspended", 12, 1, 4, 4,
         'count(gotk_resource_info{suspended="true"}) or vector(0)', steps=BLUE_YELLOW1),
    stat("Not Ready", 16, 1, 4, 4,
         'count(gotk_resource_info{ready!="True"}) or vector(0)', steps=GREEN0_REDpos),
    stat("Reconcile p99", 20, 1, 4, 4,
         'histogram_quantile(0.99, sum by (le) (rate(gotk_reconcile_duration_seconds_bucket[5m])))',
         unit="s", decimals=2),
    rel_table("Not-ready / suspended resources", 0, 5, 24, 7),
    # reconcile duration by kind (Flux gotk_reconcile_duration_seconds carries `kind`, not `controller`)
    ts("Reconcile duration p50 / p99 by kind", 0, 12, 24, 7,
       ['histogram_quantile(0.5, sum by (le, kind) (rate(gotk_reconcile_duration_seconds_bucket[5m])))',
        'histogram_quantile(0.99, sum by (le, kind) (rate(gotk_reconcile_duration_seconds_bucket[5m])))'],
       "s", legends=["{{kind}} p50", "{{kind}} p99"]),
]
# ── Row 2: Resource usage ──
panels.append(row("Resource usage (workloads)", 19))
panels += [
    ts("CPU by namespace (cores)", 0, 20, 12, 7,
       [f'sum by (namespace) (rate(container_cpu_usage_seconds_total{{container!="",{NS}}}[5m]))'], "short"),
    ts("Memory (working set) by namespace", 12, 20, 12, 7,
       [f'sum by (namespace) (container_memory_working_set_bytes{{container!="",{NS}}})'], "bytes"),
    ts("Top 10 pods by CPU (cores)", 0, 27, 12, 7,
       [f'topk(10, sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{{container!="",pod!="",{NS}}}[5m])))'],
       "short", legends=["{{namespace}}/{{pod}}"]),
    ts("Top 10 pods by memory", 12, 27, 12, 7,
       [f'topk(10, sum by (namespace, pod) (container_memory_working_set_bytes{{container!="",pod!="",{NS}}}))'],
       "bytes", legends=["{{namespace}}/{{pod}}"]),
    ts("Pod restarts (increase, 1h)", 0, 34, 12, 7,
       [f'sum by (namespace) (increase(kube_pod_container_status_restarts_total{{{NS}}}[1h]))'], "short"),
    # count of not-ready pods that are NOT Succeeded/Completed. count() of the ==0 filter (NOT sum, which adds zeros).
    stat("Pods not ready", 12, 34, 12, 7,
         f'count((kube_pod_status_ready{{condition="true",{NS}}} == 0) * on(namespace,pod) group_left '
         f'(kube_pod_status_phase{{phase=~"Pending|Running|Unknown",{NS}}} == 1)) or vector(0)',
         steps=GREEN0_REDpos),
]
# ── Row 3: Errors & Warnings (logs) ──
ERR = r'"(?i)(error|warn|fatal|panic|exception|fail)"'
panels.append(row("Errors & Warnings (logs)", 41))
panels += [
    logs("Error / warning logs", 0, 42, 24, 8, f'{{{NS}}} |~ {ERR}'),
    ts("Error/warn rate by namespace", 0, 50, 12, 7,
       [f'sum by (namespace) (count_over_time({{{NS}}} |~ {ERR} [$__auto]))'], "short", ds=LOKI),
    ts("Top 10 noisiest (error/warn) pods", 12, 50, 12, 7,
       [f'topk(10, sum by (namespace, pod) (count_over_time({{{NS}}} |~ {ERR} [$__auto])))'],
       "short", legends=["{{namespace}}/{{pod}}"], ds=LOKI),
]

dashboard = {
    "title": "Kubernetes — Releases & Workloads", "uid": "k8s-releases",
    "tags": ["kubernetes", "flux", "releases", "logs"], "timezone": "browser",
    "schemaVersion": 39, "refresh": "30s", "time": {"from": "now-6h", "to": "now"},
    "templating": {"list": [
        {"name": "DS_PROMETHEUS", "type": "datasource", "query": "prometheus",
         "current": {}, "hide": 0, "label": "Prometheus", "refresh": 1},
        {"name": "DS_LOKI", "type": "datasource", "query": "loki",
         "current": {}, "hide": 0, "label": "Loki", "refresh": 1},
        {"name": "namespace", "type": "query", "datasource": PROM,
         "query": "label_values(kube_namespace_status_phase, namespace)",
         "includeAll": True, "multi": True, "allValue": ".*",
         "current": {"text": "All", "value": "$__all"}, "refresh": 2}]},
    "panels": panels,
}
configmap = {
    "apiVersion": "v1", "kind": "ConfigMap",
    "metadata": {"name": "k8s-releases-dashboard", "namespace": "monitoring",
                 "labels": {"grafana_dashboard": "1"}},
    "data": {"k8s-releases.json": json.dumps(dashboard, indent=2)},
}
out = pathlib.Path(__file__).resolve().parents[1] / "kubernetes/apps/infrastructure/monitoring/k8s-releases-dashboard.yaml"
out.write_text(json.dumps(configmap, indent=2) + "\n", encoding="utf-8")
print(f"wrote {out} ({len([p for p in panels if p['type']!='row'])} panels, {len([p for p in panels if p['type']=='row'])} rows)")
```

- [ ] **Step 2: Generate and validate**

```
python scripts/gen-k8s-releases-dashboard.py
```
Expected: `wrote …/k8s-releases-dashboard.yaml (17 panels, 3 rows)`.

```bash
python - <<'PY'
import yaml, json
d=yaml.safe_load(open("kubernetes/apps/infrastructure/monitoring/k8s-releases-dashboard.yaml",encoding="utf-8"))
assert d["metadata"]["labels"]["grafana_dashboard"]=="1"
dash=json.loads(d["data"]["k8s-releases.json"])
vars={v["name"] for v in dash["templating"]["list"]}
assert {"DS_PROMETHEUS","DS_LOKI","namespace"} <= vars, vars
panels=[p for p in dash["panels"] if p.get("type")!="row"]
for p in panels:
    for t in p.get("targets",[]):
        u=(t.get("datasource") or p.get("datasource") or {}).get("uid","")
        assert u in ("${DS_PROMETHEUS}","${DS_LOKI}"), (p["id"],u)
cells={}; overlap=set()
for p in panels:
    g=p["gridPos"]; assert g["x"]+g["w"]<=24, (p["id"],"x+w>24")
    for yy in range(g["y"],g["y"]+g["h"]):
        for xx in range(g["x"],g["x"]+g["w"]):
            if (yy,xx) in cells: overlap.add((p["id"],cells[(yy,xx)]))
            cells[(yy,xx)]=p["id"]
assert not overlap, f"overlaps {overlap}"
ids=[p["id"] for p in dash["panels"]]; assert len(ids)==len(set(ids))
print(f"OK panels={len(panels)} rows={len([p for p in dash['panels'] if p.get('type')=='row'])}")
PY
kubectl kustomize kubernetes/apps/infrastructure/monitoring >/dev/null && echo "kustomize OK"
```
Expected: `OK panels=17 rows=3`; `kustomize OK`.

- [ ] **Step 3: Register in kustomization** — add after the `loki-logs-dashboard.yaml` line:

```yaml
  - k8s-releases-dashboard.yaml # "Kubernetes — Releases & Workloads": Flux status + workload usage + error/warn logs
```

- [ ] **Step 4: Commit**

```bash
git add scripts/gen-k8s-releases-dashboard.py \
        kubernetes/apps/infrastructure/monitoring/k8s-releases-dashboard.yaml \
        kubernetes/apps/infrastructure/monitoring/kustomization.yaml
git commit -m "feat(monitoring): add Kubernetes Releases & Workloads dashboard"
```

---

### Task 5: Open PR

- [ ] **Step 1: Push and open the PR**

```bash
git push -u origin feat/grafana-monitoring-improvements
gh pr create --base main --title "feat(monitoring): Grafana improvements — runner/worker Fleet sections, k8s releases dashboard, dashboard audit" \
  --body "$(cat <<'BODY'
## What
- AI Lab Fleet: new **GitHub Actions Runners** + **Dev Workers** resource sections (via the dashboard generator).
- New **Kubernetes — Releases & Workloads** dashboard (Flux release status + workload resource usage + error/warn logs).
- New collection: `dev-worker-node` scrape; Flux PodMonitor + kube-state-metrics custom-resource-state (`gotk_resource_info`).
- Dashboard audit: fix hardcoded AI-node IPs (→ `job=ai-llm-node`); trim AI-row/AI-LLM redundancy. Keep all 4 dashboards.

## Validation
Local: generators re-run cleanly; JSON parse + datasource/overlap/grid lint + `kubectl kustomize` all pass. Post-merge live verification per plan Task 6.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```
Expected: PR URL printed.

---

### Task 6: Post-merge live verification (after Flux reconciles)

Run after merge + `flux get kustomizations` shows the monitoring KS reconciled. Port-forward: `kubectl --context admin@ai -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090`.

- [ ] **Step 1: New scrape targets up** — `up{job="dev-worker-node"}` = 3 series all `1`; Flux PodMonitor targets healthy.
- [ ] **Step 2: Flux metrics present (both sources, independently)** — `count(gotk_resource_info{customresource_kind="HelmRelease"})` matches `kubectl --context admin@ai get helmrelease -A` count; `gotk_reconcile_duration_seconds_bucket` returns data. **Confirm the reconcile-duration label is `kind`** (`sum by(kind)(gotk_reconcile_duration_seconds_count)`); if Flux emits a different label, adjust the id-9 panel and regenerate.
- [ ] **Step 3: Dashboards render** — Fleet shows Runners + Workers sections with data, AI row shows all 3 AI nodes (no "No data") and the 5 trimmed panels. k8s-releases: 3 rows populate; not-ready table empty or lists genuine failures; logs stream.
- [ ] **Step 4: Edge cases** — with all dev-worker/Flux targets absent, the Up/Not-Ready/Suspended stats render `0` (not "No data"); one AI node down still shows the other two AI series; Fleet stays contiguous to `maxY=124`; empty `gotk_resource_info` (e.g. CRS misconfig) does not break the namespace variable or stat panels.
- [ ] **Step 5: No regressions** — existing Fleet rows (Hypervisors/Instances/Storage) unchanged; AI-LLM / Loki / Trivy dashboards unchanged.

---

## Self-Review

**Spec coverage:** Runner section → Task 3; Worker section → Task 1 + Task 3; k8s dashboard releases → Task 2 + Task 4 Row 1; resource usage → Task 4 Row 2; error/warn logs → Task 4 Row 3; audit keep-all + Fix A (AI IP) + Fix B (AI trim) → Task 3. Cross-review → this skill (round 1 done; impl review after Task 4). ✓

**Placeholder scan:** all generator code and PromQL/LogQL is concrete; `revision` paths carry an upstream cross-check; no TODO/TBD. ✓

**Type/label consistency:** job labels (`ci-runner-node`, `dev-worker-node`, `ai-llm-node`) consistent; KSM CRS labels (`customresource_kind`, `ready`, `suspended`, `exported_namespace`, `revision`) consistent between Task 2 (producer) and Task 4 (consumer/table); panel ids auto-assigned + overlap/grid-asserted by each generator's lint. ✓

<!-- codex-review-status: finalized -->
