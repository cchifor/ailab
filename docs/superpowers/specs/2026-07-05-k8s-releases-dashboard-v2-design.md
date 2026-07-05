# "Kubernetes — Releases & Workloads" v2 — Design

**Date:** 2026-07-05
**Status:** Approved
**Scope:** `scripts/gen-k8s-releases-dashboard.py` + `kube-prometheus-stack.yaml` (KSM CRS). Flux-reconciled on merge.

## Goal

Make the dashboard a clear at-a-glance overview of Kubernetes/Flux status. Four asks:
1. A **Releases table** ordered newest→oldest with date/time + status.
2. **Fix the Errors & Warnings parse errors.**
3. Replace the **Not-Ready** view with an **all-items status** view + a **ready/not-ready trend** over time (best-practice).
4. Bring the **Loki — Logs** explorer views into the logs section.

## Diagnosis (evidence-based)

- **#2 root cause:** the LogQL is valid (queries succeed directly against Loki). The Grafana parse error comes from the Loki **timeseries** panels missing `queryType: "range"` → Grafana never interpolates `$__auto` → Loki receives the literal `[$__auto]` → `parse error: not a valid duration string: "$__auto"`. The working `loki-logs` dashboard sets `queryType:"range"` on every target. **Fix = set `queryType:"range"` on all Loki metric panels.**
- **#3 trend:** `count(gotk_resource_info{ready="True"})` over a range returns a clean series — trend works directly from the KSM metric.
- **#1 datetime:** no timestamp metric exists today; every resource's `status.conditions[Ready].lastTransitionTime` does. Add one KSM CRS Gauge to expose it.

## Design

### Row 1 — Releases (Flux)

- **Keep** 6 KPI stat tiles (HelmReleases/Kustomizations/Sources Ready, Suspended, Not Ready, Reconcile p99).
- **NEW — Status trend** (timeseries, lines): Ready `count(gotk_resource_info{ready="True"})` (green), Not-Ready `count(gotk_resource_info{ready!="True"})` (red), Suspended `count(gotk_resource_info{suspended="true"})` (yellow) over time.
- **REPLACE** the not-ready-only table with **one all-resources table**, columns **Kind · Name · Namespace · Status · Suspended · Revision · Last change**, sorted **Last change desc** (newest first), Status color-coded (Ready=green / Not Ready=red / Unknown=yellow). Single query (no multi-query join):
  ```promql
  (gotk_status_last_transition_timestamp{type="Ready"} * 1000)
    * on(customresource_kind, name, exported_namespace)
      group_left(ready, suspended, revision) gotk_resource_info
  ```
  Value = last-change epoch **ms** (×1000 so Grafana's `dateTimeAsIso` unit renders it); `group_left` grafts `ready/suspended/revision` onto the timestamp series. `format:table, instant:true`; `organize` renames + hides internals; column override maps `ready`→colored Status; panel `sortBy` Last-change desc.
- **Keep** reconcile-duration-by-kind timeseries.

### KSM change — `kube-prometheus-stack.yaml`

Add a second metric to **each** of the 5 Flux CRS resource blocks:
```yaml
- name: status_last_transition_timestamp
  help: "Unix time of the resource's last condition transition."
  each:
    type: Gauge
    gauge:
      path: [status, conditions]
      valueFrom: [lastTransitionTime]
      labelsFromPath:
        type: [type]
  labelsFromPath:
    exported_namespace: [metadata, namespace]
    name: [metadata, name]
```
→ `gotk_status_last_transition_timestamp{customresource_kind, name, exported_namespace, type}` = epoch seconds. **Validation:** after deploy, confirm the value is a plausible epoch (~1.7e9). Fallback if KSM won't parse the RFC3339 date: drop the datetime column and sort the table by Status instead (documented, non-blocking).

### Row 2 — Resource usage: unchanged.

### Row 3 — "Logs" (expanded from "Errors & Warnings")

- **Fix #2:** every Loki timeseries target gets `queryType:"range"`; drop the stray space before `[$__auto]` to match the working dashboard.
- **New template vars:** `pod` (Loki `label_values({namespace=~"$namespace"}, pod)`, multi, allValue `.+`) and `search` (textbox).
- **Panels** (errors prominent first, then explorer):
  1. **Errors & warnings** (logs) — `{namespace=~"$namespace", pod=~"$pod"} |~ "(?i)(error|err|warn|warning|fatal|panic|exception|fail)"`
  2. **All logs** (logs) — `{namespace=~"$namespace", pod=~"$pod"} |~ "(?i)$search"`
  3. **Log volume by namespace** (timeseries, bars/stacked) — `sum by (namespace) (count_over_time({namespace=~"$namespace"}[$__auto]))`
  4. **Error/warn rate by namespace** (timeseries) — same with the error regex
  5. **Top-10 noisiest (error/warn) pods** (timeseries) — `topk(10, sum by (namespace,pod) (count_over_time(... |~ err [$__auto])))`

The standalone **Loki — Logs** dashboard is unchanged (general explorer).

## Files touched

| File | Change |
|---|---|
| `kube-prometheus-stack.yaml` | +`status_last_transition_timestamp` Gauge in the 5 CRS blocks |
| `scripts/gen-k8s-releases-dashboard.py` | Row 1 trend + all-resources table; Row 3 → Logs explorer; `queryType:range`; `pod`/`search` vars; table/trend helpers |
| `k8s-releases-dashboard.yaml` | regenerated output |

## Validation

Local: generator regenerates; JSON/grid-overlap lint; LogQL syntax against Loki; PromQL (table join, trend) against Prometheus. Post-reconcile live: `gotk_status_last_transition_timestamp` present with epoch values; the table query returns rows with dates + status; the Logs panels render without parse errors; the trend plots.
