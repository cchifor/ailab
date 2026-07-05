#!/usr/bin/env python3
"""Generate the "Kubernetes — Releases & Workloads" Grafana dashboard ConfigMap.

Emits kubernetes/apps/infrastructure/monitoring/k8s-releases-dashboard.yaml — a ConfigMap labeled
grafana_dashboard=1 so the kube-prometheus-stack Grafana sidecar auto-loads it. Sections:
  Row 1 Releases (Flux) — gotk_resource_info (KSM custom-resource-state) + gotk_reconcile_* (controllers)
  Row 2 Resource usage  — cadvisor (container_*) + kube-state-metrics (kube_pod_*)
  Row 3 Errors & Warnings — Loki logs. Datasource template vars ${DS_PROMETHEUS} / ${DS_LOKI}.

    python scripts/gen-k8s-releases-dashboard.py
"""
import json
import pathlib

PROM = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
LOKI = {"type": "loki", "uid": "${DS_LOKI}"}
NS = 'namespace=~"$namespace"'
_pid = 0


def _nid():
    global _pid
    _pid += 1
    return _pid


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
            "targets": [{"refId": chr(65 + i), "datasource": ds, "expr": e,
                         "legendFormat": legends[i]} for i, e in enumerate(exprs)]}


def logs(title, x, y, w, h, expr):
    return {"id": _nid(), "type": "logs", "title": title, "datasource": LOKI,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                        "enableLogDetails": True, "dedupStrategy": "none"},
            "targets": [{"refId": "A", "datasource": LOKI, "expr": expr, "queryType": "range"}]}


def rel_table(title, x, y, w, h):
    # not-ready OR suspended Flux resources; instant table; organize renames the real KSM CRS labels.
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
# ───────────────────────── Row 1: Releases (Flux) ─────────────────────────
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
# ───────────────────────── Row 2: Resource usage ─────────────────────────
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
    # count of not-ready pods that are still active (excludes Succeeded/Failed via `and on` phase filter).
    # count() of the surviving series (NOT sum, which would add zeros). `and on(...)` avoids group_left.
    stat("Pods not ready", 12, 34, 12, 7,
         f'count((kube_pod_status_ready{{condition="true",{NS}}} == 0) and on(namespace,pod) '
         f'(kube_pod_status_phase{{phase=~"Pending|Running|Unknown",{NS}}} == 1)) or vector(0)',
         steps=GREEN0_REDpos),
]
# ───────────────────────── Row 3: Errors & Warnings (logs) ─────────────────────────
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
    "title": "Kubernetes — Releases & Workloads",
    "uid": "k8s-releases",
    "tags": ["kubernetes", "flux", "releases", "logs"],
    "timezone": "browser",
    "schemaVersion": 39,
    "refresh": "30s",
    "time": {"from": "now-6h", "to": "now"},
    "templating": {"list": [
        {"name": "DS_PROMETHEUS", "type": "datasource", "query": "prometheus",
         "current": {}, "hide": 0, "label": "Prometheus", "refresh": 1},
        {"name": "DS_LOKI", "type": "datasource", "query": "loki",
         "current": {}, "hide": 0, "label": "Loki", "refresh": 1},
        {"name": "namespace", "type": "query", "datasource": PROM,
         "query": "label_values(kube_namespace_status_phase, namespace)",
         "includeAll": True, "multi": True, "allValue": ".*",
         "current": {"text": "All", "value": "$__all"}, "refresh": 2},
    ]},
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
print(f"wrote {out} ({len([p for p in panels if p['type'] != 'row'])} panels, {len([p for p in panels if p['type']=='row'])} rows)")
