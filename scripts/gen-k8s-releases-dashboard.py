#!/usr/bin/env python3
"""Generate the "Kubernetes — Releases & Workloads" Grafana dashboard ConfigMap.

Emits kubernetes/apps/infrastructure/monitoring/k8s-releases-dashboard.yaml — a ConfigMap labeled
grafana_dashboard=1 so the kube-prometheus-stack Grafana sidecar auto-loads it. Sections:
  Row 1 Releases (Flux) — KPI stats + Ready/Not-Ready/Suspended trend + an all-resources status table
                          (newest-first, with last-change datetime) + reconcile duration by kind.
  Row 2 Resource usage  — cadvisor (container_*) + kube-state-metrics (kube_pod_*).
  Row 3 Logs            — Loki explorer: errors first, then all-logs (search/pod), volume, rate, noisiest.
Datasource template vars ${DS_PROMETHEUS} / ${DS_LOKI}. Loki metric panels set queryType=range so
Grafana interpolates $__auto (otherwise Loki gets a literal [$__auto] -> parse error).

    python scripts/gen-k8s-releases-dashboard.py
"""
import json
import pathlib

PROM = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
LOKI = {"type": "loki", "uid": "${DS_LOKI}"}
NS = 'namespace=~"$namespace"'
POD = 'pod=~"$pod"'
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


def _color(name, col):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": col}}]}


def ts(title, x, y, w, h, exprs, unit="short", legends=None, ds=PROM, fill=10, draw="line",
       stack=False, overrides=None):
    legends = legends or ["{{namespace}}"] * len(exprs)
    custom = {"drawStyle": draw, "fillOpacity": fill, "showPoints": "never"}
    if stack:
        custom["stacking"] = {"mode": "normal"}
    tgts = []
    for i, e in enumerate(exprs):
        t = {"refId": chr(65 + i), "datasource": ds, "expr": e, "legendFormat": legends[i]}
        if ds is LOKI:
            t["queryType"] = "range"   # so Grafana interpolates $__auto (else Loki parse-errors on [$__auto])
        tgts.append(t)
    return {"id": _nid(), "type": "timeseries", "title": title, "datasource": ds,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "fieldConfig": {"defaults": {"unit": unit, "custom": custom}, "overrides": overrides or []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
            "targets": tgts}


def logs(title, x, y, w, h, expr):
    return {"id": _nid(), "type": "logs", "title": title, "datasource": LOKI,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                        "enableLogDetails": True, "dedupStrategy": "none"},
            "targets": [{"refId": "A", "datasource": LOKI, "queryType": "range", "expr": expr}]}


# Status value-mapping (the `ready` label True/False -> coloured text) and Suspended mapping.
STATUS_MAP = [{"type": "value", "options": {
    "True": {"text": "● Ready", "color": "green", "index": 0},
    "False": {"text": "● Not Ready", "color": "red", "index": 1},
    "Unknown": {"text": "● Unknown", "color": "yellow", "index": 2}}}]
SUSPEND_MAP = [{"type": "value", "options": {
    "true": {"text": "Suspended", "color": "yellow", "index": 0},
    "false": {"text": "—", "color": "text", "index": 1}}}]


def releases_table(title, x, y, w, h):
    # One query: last-transition timestamp (ms) with ready/suspended/revision grafted on via group_left.
    # Value = epoch ms -> rendered as a date; table sorted newest-first.
    expr = ('(gotk_status_last_transition_timestamp{type="Ready"} * 1000)'
            ' * on(customresource_kind, name, exported_namespace)'
            ' group_left(ready, suspended, revision) gotk_resource_info')
    rename = {"customresource_kind": "Kind", "name": "Name", "exported_namespace": "Namespace",
              "ready": "Status", "suspended": "Suspended", "revision": "Revision", "Value": "Last change"}
    order = {"customresource_kind": 0, "name": 1, "exported_namespace": 2, "ready": 3,
             "suspended": 4, "revision": 5, "Value": 6}
    exclude = ["Time", "type", "__name__", "customresource_group", "customresource_version",
               "container", "endpoint", "instance", "job", "namespace", "pod", "service", "uid"]
    overrides = [
        {"matcher": {"id": "byName", "options": "Status"},
         "properties": [{"id": "mappings", "value": STATUS_MAP},
                        {"id": "custom.cellOptions", "value": {"type": "color-text"}}]},
        {"matcher": {"id": "byName", "options": "Suspended"},
         "properties": [{"id": "mappings", "value": SUSPEND_MAP},
                        {"id": "custom.cellOptions", "value": {"type": "color-text"}}]},
        {"matcher": {"id": "byName", "options": "Last change"},
         "properties": [{"id": "unit", "value": "dateTimeAsIso"}]},
    ]
    return {"id": _nid(), "type": "table", "title": title, "datasource": PROM,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "fieldConfig": {"defaults": {"custom": {"align": "auto", "filterable": True,
                "cellOptions": {"type": "auto"}}}, "overrides": overrides},
            "options": {"showHeader": True, "footer": {"show": False}, "cellHeight": "sm",
                        "sortBy": [{"displayName": "Last change", "desc": True}]},
            "transformations": [{"id": "organize", "options": {
                "excludeByName": {k: True for k in exclude}, "renameByName": rename, "indexByName": order}}],
            "targets": [{"refId": "A", "datasource": PROM, "expr": expr, "format": "table", "instant": True}]}


GREEN0_REDpos = [{"color": "green", "value": None}, {"color": "red", "value": 1}]
BLUE_YELLOW1 = [{"color": "blue", "value": None}, {"color": "yellow", "value": 1}]
SRC = 'customresource_kind=~"GitRepository|HelmRepository|OCIRepository"'
TREND_COLORS = [_color("Ready", "green"), _color("Not Ready", "red"), _color("Suspended", "yellow")]

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
    # Trend: how ready/not-ready/suspended counts evolve over time.
    ts("Ready / Not-Ready / Suspended (trend)", 0, 5, 24, 7,
       ['count(gotk_resource_info{ready="True"}) or vector(0)',
        'count(gotk_resource_info{ready!="True"}) or vector(0)',
        'count(gotk_resource_info{suspended="true"}) or vector(0)'],
       "short", legends=["Ready", "Not Ready", "Suspended"], fill=15, overrides=TREND_COLORS),
    # All Flux resources with status, newest change first.
    releases_table("Releases — all resources (newest first)", 0, 12, 24, 10),
    # reconcile duration by kind (gotk_reconcile_duration_seconds carries `kind`)
    ts("Reconcile duration p50 / p99 by kind", 0, 22, 24, 7,
       ['histogram_quantile(0.5, sum by (le, kind) (rate(gotk_reconcile_duration_seconds_bucket[5m])))',
        'histogram_quantile(0.99, sum by (le, kind) (rate(gotk_reconcile_duration_seconds_bucket[5m])))'],
       "s", legends=["{{kind}} p50", "{{kind}} p99"]),
]
# ───────────────────────── Row 2: Resource usage ─────────────────────────
panels.append(row("Resource usage (workloads)", 29))
panels += [
    ts("CPU by namespace (cores)", 0, 30, 12, 7,
       [f'sum by (namespace) (rate(container_cpu_usage_seconds_total{{container!="",{NS}}}[5m]))'], "short"),
    ts("Memory (working set) by namespace", 12, 30, 12, 7,
       [f'sum by (namespace) (container_memory_working_set_bytes{{container!="",{NS}}})'], "bytes"),
    ts("Top 10 pods by CPU (cores)", 0, 37, 12, 7,
       [f'topk(10, sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{{container!="",pod!="",{NS}}}[5m])))'],
       "short", legends=["{{namespace}}/{{pod}}"]),
    ts("Top 10 pods by memory", 12, 37, 12, 7,
       [f'topk(10, sum by (namespace, pod) (container_memory_working_set_bytes{{container!="",pod!="",{NS}}}))'],
       "bytes", legends=["{{namespace}}/{{pod}}"]),
    ts("Pod restarts (increase, 1h)", 0, 44, 12, 7,
       [f'sum by (namespace) (increase(kube_pod_container_status_restarts_total{{{NS}}}[1h]))'], "short"),
    stat("Pods not ready", 12, 44, 12, 7,
         f'count((kube_pod_status_ready{{condition="true",{NS}}} == 0) and on(namespace,pod) '
         f'(kube_pod_status_phase{{phase=~"Pending|Running|Unknown",{NS}}} == 1)) or vector(0)',
         steps=GREEN0_REDpos),
]
# ───────────────────────── Row 3: Logs (Loki explorer) ─────────────────────────
ERR = r'"(?i)(error|err|warn|warning|fatal|panic|exception|fail)"'
ERRRATE = r'"(?i)(error|warn|fatal|panic|exception)"'
# Exclude benign lines that merely contain the substring "error" (e.g. Gatus "success=true; errors=0"
# health checks) so the error views show real problems. Root-cause noise is also cut at the source
# (Gatus GATUS_LOG_LEVEL=WARN; alloy remotecfg dropped in the log pipeline) — this is defence-in-depth.
NEG = r'!~ "success=true"'
panels.append(row("Logs (Loki — errors first, then explorer)", 51))
panels += [
    logs("Errors & warnings", 0, 52, 24, 10, f'{{{NS}, {POD}}} |~ {ERR} {NEG}'),
    logs("All logs · {namespace, pod} |~ search", 0, 62, 24, 10, f'{{{NS}, {POD}}} |~ "(?i)$search"'),
    ts("Log volume by namespace", 0, 72, 12, 8,
       [f'sum by (namespace) (count_over_time({{{NS}}}[$__auto]))'],
       "short", ds=LOKI, draw="bars", fill=60, stack=True),
    ts("Error / warn rate by namespace", 12, 72, 12, 8,
       [f'sum by (namespace) (count_over_time({{{NS}}} |~ {ERRRATE} {NEG}[$__auto]))'],
       "short", ds=LOKI, draw="bars", fill=60, stack=True),
    ts("Top 10 noisiest (error/warn) pods", 0, 80, 24, 8,
       [f'topk(10, sum by (namespace, pod) (count_over_time({{{NS}}} |~ {ERRRATE} {NEG}[$__auto])))'],
       "short", ds=LOKI, legends=["{{namespace}}/{{pod}}"]),
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
         # allValue must be ".+" (not ".*"): Loki rejects an all-empty-compatible stream selector
         # ({namespace=~".*"}). ".+" also matches every (non-empty) namespace for the Prometheus panels.
         "includeAll": True, "multi": True, "allValue": ".+", "sort": 1,
         "current": {"text": "All", "value": "$__all"}, "refresh": 2},
        {"name": "pod", "type": "query", "label": "Pod", "datasource": LOKI,
         "query": "label_values({namespace=~\"$namespace\"}, pod)",
         "includeAll": True, "multi": True, "allValue": ".+", "sort": 1,
         "current": {"text": "All", "value": "$__all"}, "refresh": 2},
        {"name": "search", "type": "textbox", "label": "Search (regex, case-insensitive)",
         "query": "", "current": {"text": "", "value": ""}},
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
