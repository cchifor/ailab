#!/usr/bin/env python3
"""Generate the single "Infrastructure Reporting" Grafana dashboard ConfigMap.

Emits kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml — a ConfigMap labeled
grafana_dashboard=1 so the kube-prometheus-stack Grafana sidecar auto-loads it. ONE dashboard,
collapsible rows: Overview · Hypervisors · Instances (VMs+CTs) · AI · Storage.

Metric sources (all verified live):
  pve_*        prometheus-pve-exporter   (label `id` = node/<n> | qemu/<vmid> | lxc/<vmid> | storage/<node>/<pool>)
  amdgpu_*     node_exporter textfile    (iGPU, per AI-LXC instance)
  llamacpp:*   llama-server /metrics
  probe_*      blackbox (QNAP storage fabric)
  kubelet_volume_stats_*  kubelet (k8s PVC usage)

    python scripts/gen-reporting-dashboard.py
"""
import json
import pathlib

DS = "${DS_PROMETHEUS}"
_pid = 0


def _nid():
    global _pid
    _pid += 1
    return _pid


def _ds():
    return {"type": "prometheus", "uid": DS}


def row(title, y):
    return {"id": _nid(), "type": "row", "title": title, "collapsed": False,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}


def ts(title, x, y, w, h, exprs, unit="short", legend="{{id}}", fill=10, stack=False):
    return {
        "id": _nid(), "type": "timeseries", "title": title, "datasource": _ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit, "custom": {
            "drawStyle": "line", "fillOpacity": fill, "showPoints": "never",
            "stacking": {"mode": "normal" if stack else "none"}}}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [{"refId": chr(65 + i), "datasource": _ds(), "expr": e,
                     "legendFormat": legend} for i, e in enumerate(exprs)],
    }


def stat(title, x, y, w, h, expr, unit="none", decimals=0, steps=None, mappings=None,
         color="value", graph="area"):
    defaults = {"unit": unit, "decimals": decimals,
                "thresholds": {"mode": "absolute", "steps": steps or [{"color": "blue", "value": None}]}}
    if mappings:
        defaults["mappings"] = mappings
    return {
        "id": _nid(), "type": "stat", "title": title, "datasource": _ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": color, "graphMode": graph, "textMode": "auto", "justifyMode": "auto"},
        "targets": [{"refId": "A", "datasource": _ds(), "expr": expr, "instant": True}],
    }


def bargauge(title, x, y, w, h, expr, unit="percent", legend="{{id}}", maxv=100):
    return {
        "id": _nid(), "type": "bargauge", "title": title, "datasource": _ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit, "min": 0, "max": maxv, "decimals": 1,
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "green", "value": None}, {"color": "yellow", "value": 75},
                {"color": "red", "value": 90}]}}, "overrides": []},
        "options": {"displayMode": "gradient", "orientation": "horizontal", "showUnfilled": True,
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
        "targets": [{"refId": "A", "datasource": _ds(), "expr": expr,
                     "legendFormat": legend, "instant": True}],
    }


PCT = [{"color": "green", "value": None}, {"color": "yellow", "value": 70}, {"color": "red", "value": 85}]
STATUS_MAP = [{"type": "value", "options": {
    "1": {"text": "● Online", "color": "green", "index": 0},
    "0": {"text": "● Offline", "color": "red", "index": 1}}}]


def _ov(name, props):
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def table(title, x, y, w, h, targets, rename, exclude, overrides):
    return {
        "id": _nid(), "type": "table", "title": title, "datasource": _ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "filterable": True, "cellOptions": {"type": "auto"}}},
                        "overrides": overrides},
        "options": {"showHeader": True, "footer": {"show": False}, "cellHeight": "sm"},
        "transformations": [
            {"id": "joinByField", "options": {"byField": "id", "mode": "outer"}},
            {"id": "organize", "options": {
                "excludeByName": {k: True for k in exclude},
                "renameByName": rename, "indexByName": {}}},
        ],
        "targets": [{"refId": t[0], "datasource": _ds(), "expr": t[1], "format": "table", "instant": True}
                    for t in targets],
    }


panels = []
# ───────────────────────── Overview ─────────────────────────
panels.append(row("Overview", 0))
panels += [
    stat("Hypervisors Online", 0, 1, 3, 4, 'count(pve_up{id=~"node/.*"} == 1)',
         steps=[{"color": "red", "value": None}, {"color": "green", "value": 3}]),
    stat("Guests Running", 3, 1, 3, 4, 'count(pve_up{id=~"qemu/.*|lxc/.*"} == 1)',
         steps=[{"color": "red", "value": None}, {"color": "green", "value": 6}]),
    stat("AI GPUs Active", 6, 1, 3, 4, "count(amdgpu_gpu_busy_percent)",
         steps=[{"color": "red", "value": None}, {"color": "green", "value": 3}]),
    stat("Storage Probes Up", 9, 1, 3, 4, "count(probe_success == 1)",
         steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}]),
    stat("Cluster CPU", 12, 1, 3, 4, 'avg(pve_cpu_usage_ratio{id=~"node/.*"}) * 100',
         unit="percent", decimals=1, steps=PCT),
    stat("Cluster Memory", 15, 1, 3, 4,
         'sum(pve_memory_usage_bytes{id=~"node/.*"}) / sum(pve_memory_size_bytes{id=~"node/.*"}) * 100',
         unit="percent", decimals=1, steps=PCT),
    stat("VRAM Used", 18, 1, 3, 4,
         "sum(amdgpu_vram_used_bytes) / sum(amdgpu_vram_total_bytes) * 100",
         unit="percent", decimals=1, steps=PCT),
    stat("k8s PVCs Used", 21, 1, 3, 4,
         "sum(kubelet_volume_stats_used_bytes) / sum(kubelet_volume_stats_capacity_bytes) * 100",
         unit="percent", decimals=1, steps=PCT),
]

# ───────────────────────── Hypervisors ─────────────────────────
panels.append(row("Hypervisors (Proxmox hosts)", 5))
panels.append(table(
    "Hosts", 0, 6, 24, 7,
    targets=[
        ("A", 'pve_up{id=~"node/.*"} * 1'),
        ("B", 'pve_cpu_usage_ratio{id=~"node/.*"} * 100'),
        ("C", '100 * pve_memory_usage_bytes{id=~"node/.*"} / pve_memory_size_bytes{id=~"node/.*"}'),
        ("D", '100 * pve_disk_usage_bytes{id=~"node/.*"} / pve_disk_size_bytes{id=~"node/.*"}'),
        ("E", 'pve_uptime_seconds{id=~"node/.*"} * 1'),
        ("F", 'pve_node_info{id=~"node/.*"} * 1'),
    ],
    rename={"name": "Host", "Value #A": "Status", "Value #B": "CPU %", "Value #C": "Mem %",
            "Value #D": "Root FS %", "Value #E": "Uptime"},
    exclude=["Time", "id", "nodeid", "level", "Value #F", "__name__"],
    overrides=[
        _ov("Status", [{"id": "mappings", "value": STATUS_MAP},
                       {"id": "custom.cellOptions", "value": {"type": "color-text"}}]),
        _ov("CPU %", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1},
                      {"id": "thresholds", "value": {"mode": "absolute", "steps": PCT}},
                      {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "gradient"}}]),
        _ov("Mem %", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1},
                      {"id": "thresholds", "value": {"mode": "absolute", "steps": PCT}},
                      {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "gradient"}}]),
        _ov("Root FS %", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1},
                          {"id": "thresholds", "value": {"mode": "absolute", "steps": PCT}}]),
        _ov("Uptime", [{"id": "unit", "value": "dtdurations"}]),
    ]))
panels += [
    ts("Host CPU %", 0, 13, 12, 7, ['pve_cpu_usage_ratio{id=~"node/.*"} * 100'], "percent"),
    ts("Host Memory %", 12, 13, 12, 7,
       ['100 * pve_memory_usage_bytes{id=~"node/.*"} / pve_memory_size_bytes{id=~"node/.*"}'], "percent"),
    ts("Host Network I/O", 0, 20, 12, 6,
       ['rate(pve_network_receive_bytes_total{id=~"node/.*"}[5m])',
        '-rate(pve_network_transmit_bytes_total{id=~"node/.*"}[5m])'], "Bps", legend="{{id}}"),
    ts("iGPU Temperature", 12, 20, 6, 6, ["amdgpu_temp_millicelsius/1000"], "celsius", legend="{{instance}}"),
    ts("iGPU Power", 18, 20, 6, 6, ["amdgpu_power_microwatts/1000000"], "watt", legend="{{instance}}"),
]

# ───────────────────────── Instances (VMs + Containers) ─────────────────────────
panels.append(row("Instances (VMs + Containers)", 26))
panels.append(table(
    "Guests", 0, 27, 24, 8,
    targets=[
        ("A", 'pve_up{id=~"qemu/.*|lxc/.*"} * 1'),
        ("B", 'pve_cpu_usage_ratio{id=~"qemu/.*|lxc/.*"} * 100'),
        ("C", '100 * pve_memory_usage_bytes{id=~"qemu/.*|lxc/.*"} / pve_memory_size_bytes{id=~"qemu/.*|lxc/.*"}'),
        ("D", '100 * pve_disk_usage_bytes{id=~"qemu/.*|lxc/.*"} / pve_disk_size_bytes{id=~"qemu/.*|lxc/.*"}'),
        ("E", 'pve_uptime_seconds{id=~"qemu/.*|lxc/.*"} * 1'),
        ("F", 'pve_guest_info{id=~"qemu/.*|lxc/.*"} * 1'),
    ],
    rename={"name": "Name", "node": "Host", "type": "Type", "Value #A": "Status",
            "Value #B": "CPU %", "Value #C": "Mem %", "Value #D": "Disk %", "Value #E": "Uptime"},
    exclude=["Time", "id", "tags", "template", "Value #F", "__name__"],
    overrides=[
        _ov("Status", [{"id": "mappings", "value": STATUS_MAP},
                       {"id": "custom.cellOptions", "value": {"type": "color-text"}}]),
        _ov("CPU %", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1},
                      {"id": "thresholds", "value": {"mode": "absolute", "steps": PCT}},
                      {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "gradient"}}]),
        _ov("Mem %", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1},
                      {"id": "thresholds", "value": {"mode": "absolute", "steps": PCT}},
                      {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "gradient"}}]),
        _ov("Disk %", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1},
                       {"id": "thresholds", "value": {"mode": "absolute", "steps": PCT}}]),
        _ov("Uptime", [{"id": "unit", "value": "dtdurations"}]),
    ]))
panels += [
    ts("Guest CPU %", 0, 35, 12, 7, ['pve_cpu_usage_ratio{id=~"qemu/.*|lxc/.*"} * 100'], "percent"),
    ts("Guest Memory", 12, 35, 12, 7, ['pve_memory_usage_bytes{id=~"qemu/.*|lxc/.*"}'], "bytes"),
    ts("Top k8s Pods by Memory", 0, 42, 24, 7,
       ['topk(15, sum by (namespace, pod) (container_memory_working_set_bytes{pod!=""}))'],
       "bytes", legend="{{namespace}}/{{pod}}"),
]

# ───────────────────────── AI ─────────────────────────
panels.append(row("AI (llama.cpp on Strix Halo iGPU)", 49))
panels += [
    ts("iGPU Utilization", 0, 50, 8, 7, ["amdgpu_gpu_busy_percent"], "percent", legend="{{instance}}"),
    ts("VRAM Used vs Total", 8, 50, 8, 7, ["amdgpu_vram_used_bytes", "amdgpu_vram_total_bytes"],
       "bytes", legend="{{instance}} {{__name__}}"),
    ts("GTT Used (system-RAM spill)", 16, 50, 8, 7, ["amdgpu_gtt_used_bytes"], "bytes", legend="{{instance}}"),
    ts("Decode Throughput", 0, 57, 8, 7, ["llamacpp:predicted_tokens_seconds"], "none", legend="{{instance}}"),
    ts("Prompt Throughput", 8, 57, 8, 7, ["llamacpp:prompt_tokens_seconds"], "none", legend="{{instance}}"),
    ts("KV-cache Usage", 16, 57, 8, 7, ["llamacpp:kv_cache_usage_ratio"], "percentunit", legend="{{instance}}"),
    ts("iGPU Temperature", 0, 64, 6, 6, ["amdgpu_temp_millicelsius/1000"], "celsius", legend="{{instance}}"),
    ts("iGPU Power", 6, 64, 6, 6, ["amdgpu_power_microwatts/1000000"], "watt", legend="{{instance}}"),
    ts("Requests: processing / deferred", 12, 64, 12, 6,
       ["llamacpp:requests_processing", "llamacpp:requests_deferred"], "short",
       legend="{{instance}} {{__name__}}"),
]

# ───────────────────────── Storage ─────────────────────────
panels.append(row("Storage (Proxmox pools · fabric · k8s PVCs)", 70))
panels += [
    bargauge("Proxmox Storage Pools (used %)", 0, 71, 12, 8,
             '100 * pve_disk_usage_bytes{id=~"storage/.*"} / pve_disk_size_bytes{id=~"storage/.*"}',
             legend="{{id}}"),
    bargauge("k8s PVCs (used %)", 12, 71, 12, 8,
             "100 * kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes",
             legend="{{persistentvolumeclaim}}"),
    ts("Storage Fabric Reachability (QNAP NFS/iSCSI)", 0, 79, 12, 6, ["probe_success"],
       "short", legend="{{fabric}} @ {{node}}", fill=0),
    ts("Storage Fabric Probe Latency", 12, 79, 12, 6, ["probe_duration_seconds"],
       "s", legend="{{fabric}} @ {{node}}"),
]

dashboard = {
    "title": "ailab — Infrastructure Reporting",
    "uid": "ailab-reporting",
    "tags": ["reporting", "infrastructure", "ailab"],
    "timezone": "browser",
    "schemaVersion": 39,
    "refresh": "30s",
    "time": {"from": "now-6h", "to": "now"},
    "templating": {"list": [
        {"name": "DS_PROMETHEUS", "type": "datasource", "query": "prometheus",
         "current": {}, "hide": 0, "label": "Datasource", "refresh": 1}
    ]},
    "panels": panels,
}

configmap = {
    "apiVersion": "v1", "kind": "ConfigMap",
    "metadata": {"name": "reporting-dashboard", "namespace": "monitoring",
                 "labels": {"grafana_dashboard": "1"}},
    "data": {"reporting.json": json.dumps(dashboard, indent=2)},
}

out = pathlib.Path(__file__).resolve().parents[1] / "kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml"
out.write_text(json.dumps(configmap, indent=2) + "\n", encoding="utf-8")
print(f"wrote {out} ({len(panels)} panels)")
