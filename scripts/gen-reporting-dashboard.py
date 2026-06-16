#!/usr/bin/env python3
"""Generate the "AI Lab Fleet" Grafana dashboard ConfigMap (deterministic, valid JSON).

Emits kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml — a ConfigMap labeled
grafana_dashboard=1 so the kube-prometheus-stack Grafana sidecar auto-loads it (and it is the default
home dashboard via grafana.ini default_home_dashboard_path). Sections (collapsible rows):
  Hypervisors  — host-level node_exporter on the 3 Proxmox hosts (job="proxmox-node")
  Instances    — pve-exporter per-guest (VMs + LXCs), label `id` = qemu/<vmid> | lxc/<vmid>
  AI           — amdgpu_* (iGPU) + llamacpp:* + AI-node CPU (node_exporter on the LXCs)
  Storage      — pve_storage pools + k8s PVC usage + host disk I/O + QNAP fabric probes

    python scripts/gen-reporting-dashboard.py
"""
import json
import pathlib

DS = "${DS_PROMETHEUS}"
_pid = 0
HOSTS = 'job="proxmox-node"'           # the 3 Proxmox hosts' node_exporter
AINODE = 'instance=~"192.168.0.4[456]:9100"'  # the 3 AI LXCs' node_exporter
GUEST = 'id=~"qemu/.*|lxc/.*"'         # pve-exporter VMs + containers
NETDEV = 'device!~"lo|veth.*|fw.*|tap.*|vmbr.*|bond.*|docker.*"'
DISKDEV = 'device=~"nvme.*|sd.*"'
PCT = [{"color": "green", "value": None}, {"color": "yellow", "value": 70}, {"color": "red", "value": 85}]
STATUS_MAP = [{"type": "value", "options": {
    "1": {"text": "● Online", "color": "green", "index": 0},
    "0": {"text": "● Offline", "color": "red", "index": 1}}}]


def _nid():
    global _pid
    _pid += 1
    return _pid


def _ds():
    return {"type": "prometheus", "uid": DS}


def row(title, y):
    return {"id": _nid(), "type": "row", "title": title, "collapsed": False,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}


def ts(title, x, y, w, h, exprs, unit="short", legends=None, fill=10, decimals=None, maxv=None):
    legends = legends or ["{{instance}}"] * len(exprs)
    defaults = {"unit": unit, "custom": {"drawStyle": "line", "fillOpacity": fill, "showPoints": "never",
                                         "stacking": {"mode": "none"}}}
    if decimals is not None:
        defaults["decimals"] = decimals
    if maxv is not None:
        defaults["max"] = maxv
    return {
        "id": _nid(), "type": "timeseries", "title": title, "datasource": _ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [{"refId": chr(65 + i), "datasource": _ds(), "expr": e, "legendFormat": legends[i]}
                    for i, e in enumerate(exprs)],
    }


def stat(title, x, y, w, h, expr, unit="none", decimals=0, steps=None, color="value", graph="area"):
    return {
        "id": _nid(), "type": "stat", "title": title, "datasource": _ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals,
            "thresholds": {"mode": "absolute", "steps": steps or [{"color": "blue", "value": None}]}},
            "overrides": []},
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
        "targets": [{"refId": "A", "datasource": _ds(), "expr": expr, "legendFormat": legend, "instant": True}],
    }


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
            {"id": "organize", "options": {"excludeByName": {k: True for k in exclude},
                                           "renameByName": rename, "indexByName": {}}},
        ],
        "targets": [{"refId": t[0], "datasource": _ds(), "expr": t[1], "format": "table", "instant": True}
                    for t in targets],
    }


def qtable(title, x, y, w, h, expr, rename, exclude, overrides=None):
    # single instant query -> table (no join); for label-carrying gauges like node_cpu_scaling_governor
    return {
        "id": _nid(), "type": "table", "title": title, "datasource": _ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                        "overrides": overrides or []},
        "options": {"showHeader": True, "footer": {"show": False}, "cellHeight": "sm"},
        "transformations": [{"id": "organize", "options": {
            "excludeByName": {k: True for k in exclude}, "renameByName": rename, "indexByName": {}}}],
        "targets": [{"refId": "A", "datasource": _ds(), "expr": expr, "format": "table", "instant": True}],
    }


# group_left(name) join so per-instance panels show the friendly guest name instead of qemu/4001
def gl(expr):
    return f'{expr} * on(id) group_left(name) pve_guest_info{{{GUEST}}}'


panels = []
# ───────────────────────── Hypervisors ─────────────────────────
panels.append(row("Hypervisors (Proxmox hosts — host node_exporter)", 0))
panels += [
    stat("Hypervisors Up", 0, 1, 4, 4, f'count(up{{{HOSTS}}} == 1)',
         steps=[{"color": "red", "value": None}, {"color": "green", "value": 3}]),
    stat("Physical CPU Cores", 4, 1, 4, 4, f'count(node_cpu_seconds_total{{{HOSTS},mode="idle"}})'),
    stat("Physical Memory", 8, 1, 4, 4, f'sum(node_memory_MemTotal_bytes{{{HOSTS}}})', unit="bytes", decimals=1),
    stat("Fleet CPU Used", 12, 1, 4, 4,
         f'100 * (1 - avg(rate(node_cpu_seconds_total{{{HOSTS},mode="idle"}}[5m])))',
         unit="percent", decimals=1, steps=PCT),
    stat("Fleet Memory Used", 16, 1, 4, 4,
         f'100 * (1 - sum(node_memory_MemAvailable_bytes{{{HOSTS}}}) / sum(node_memory_MemTotal_bytes{{{HOSTS}}}))',
         unit="percent", decimals=1, steps=PCT),
    stat("Fleet Load (1m sum)", 20, 1, 4, 4, f'sum(node_load1{{{HOSTS}}})', unit="short", decimals=2),
    ts("CPU % per Hypervisor", 0, 5, 8, 8,
       [f'100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{{{HOSTS},mode="idle"}}[5m])))'],
       "percent", maxv=100),
    ts("Memory % per Hypervisor", 8, 5, 8, 8,
       [f'100 * (1 - node_memory_MemAvailable_bytes{{{HOSTS}}} / node_memory_MemTotal_bytes{{{HOSTS}}})'],
       "percent", maxv=100),
    ts("CPU Clock per Hypervisor (avg)", 16, 5, 8, 8,
       [f'avg by (instance) (node_cpu_scaling_frequency_hertz{{{HOSTS}}})'], "hertz"),
    ts("Root Disk Used % per Hypervisor", 0, 13, 8, 8,
       [f'100 * (1 - node_filesystem_avail_bytes{{{HOSTS},mountpoint="/"}} / node_filesystem_size_bytes{{{HOSTS},mountpoint="/"}})'],
       "percent", maxv=100),
    ts("Network per Hypervisor (RX+ / TX-)", 8, 13, 8, 8,
       [f'sum by (instance) (rate(node_network_receive_bytes_total{{{HOSTS},{NETDEV}}}[5m]))',
        f'0 - sum by (instance) (rate(node_network_transmit_bytes_total{{{HOSTS},{NETDEV}}}[5m]))'],
       "Bps", legends=["{{instance}} rx", "{{instance}} tx"]),
    qtable("CPU Governor per Hypervisor", 16, 13, 8, 8,
           # node_exporter emits a series per (cpu, governor) — 1=active, 0=inactive — so filter ==1
           f'count by (instance, governor) (node_cpu_scaling_governor{{{HOSTS}}} == 1)',
           rename={"instance": "Host", "governor": "Governor", "Value": "Cores"},
           exclude=["Time", "__name__"],
           overrides=[_ov("Governor", [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                       {"id": "mappings", "value": [{"type": "value", "options": {
                                           "performance": {"color": "green", "index": 0}}}]}])]),
]

# ───────────────────────── Instances (VMs + Containers) ─────────────────────────
panels.append(row("Instances (VMs + Containers — pve-exporter)", 21))
panels += [
    stat("Instances Running", 0, 22, 6, 4, f'count(pve_up{{{GUEST}}} == 1)',
         steps=[{"color": "red", "value": None}, {"color": "green", "value": 6}]),
    stat("vCPUs Allocated", 6, 22, 6, 4, f'sum(pve_cpu_usage_limit{{{GUEST}}})'),
    stat("Memory Allocated", 12, 22, 6, 4, f'sum(pve_memory_size_bytes{{{GUEST}}})', unit="bytes", decimals=1),
    stat("CPU Used by Instances (cores)", 18, 22, 6, 4,
         f'sum(pve_cpu_usage_ratio{{{GUEST}}} * pve_cpu_usage_limit{{{GUEST}}})', decimals=2),
    ts("CPU Rate per Instance (cores)", 0, 26, 12, 8,
       [gl(f'(pve_cpu_usage_ratio{{{GUEST}}} * pve_cpu_usage_limit{{{GUEST}}})')], "short",
       legends=["{{name}}"], decimals=2),
    ts("Memory Used per Instance", 12, 26, 12, 8, [gl(f'pve_memory_usage_bytes{{{GUEST}}}')], "bytes",
       legends=["{{name}}"]),
    ts("Disk I/O per Instance (read+ / write-)", 0, 34, 12, 8,
       [gl(f'rate(pve_disk_read_bytes_total{{{GUEST}}}[5m])'),
        '0 - ' + gl(f'rate(pve_disk_written_bytes_total{{{GUEST}}}[5m])')],
       "Bps", legends=["{{name}} read", "{{name}} write"]),
    ts("Network per Instance (RX+ / TX-)", 12, 34, 12, 8,
       [gl(f'rate(pve_network_receive_bytes_total{{{GUEST}}}[5m])'),
        '0 - ' + gl(f'rate(pve_network_transmit_bytes_total{{{GUEST}}}[5m])')],
       "Bps", legends=["{{name}} rx", "{{name}} tx"]),
]
panels.append(table(
    "Instances Inventory", 0, 42, 24, 8,
    targets=[
        ("A", f'pve_guest_info{{{GUEST}}} * 1'),
        ("B", f'pve_cpu_usage_limit{{{GUEST}}} * 1'),
        ("C", f'pve_memory_size_bytes{{{GUEST}}} * 1'),
        ("D", f'pve_up{{{GUEST}}} * 1'),
    ],
    rename={"node": "Hypervisor", "name": "Instance", "type": "Type",
            "Value #B": "vCPUs", "Value #C": "Memory", "Value #D": "Status"},
    exclude=["Time", "id", "tags", "template", "Value #A", "__name__"],
    overrides=[
        _ov("Status", [{"id": "mappings", "value": STATUS_MAP},
                       {"id": "custom.cellOptions", "value": {"type": "color-text"}}]),
        _ov("Memory", [{"id": "unit", "value": "bytes"}, {"id": "decimals", "value": 1}]),
        _ov("vCPUs", [{"id": "unit", "value": "short"}]),
    ]))

# ───────────────────────── AI ─────────────────────────
panels.append(row("AI (llama.cpp on Strix Halo iGPU)", 50))
panels += [
    ts("AI Node CPU %", 0, 51, 8, 7,
       [f'100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{{{AINODE},mode="idle"}}[5m])))'],
       "percent", maxv=100),
    ts("iGPU Utilization", 8, 51, 8, 7, ["amdgpu_gpu_busy_percent"], "percent"),
    ts("VRAM Used vs Total", 16, 51, 8, 7, ["amdgpu_vram_used_bytes", "amdgpu_vram_total_bytes"],
       "bytes", legends=["{{instance}} used", "{{instance}} total"]),
    ts("GTT Used (system-RAM spill)", 0, 58, 8, 7, ["amdgpu_gtt_used_bytes"], "bytes"),
    ts("Decode Throughput (tokens/s)", 8, 58, 8, 7, ["llamacpp:predicted_tokens_seconds"], "tok/s"),
    ts("Prompt Throughput (tokens/s)", 16, 58, 8, 7, ["llamacpp:prompt_tokens_seconds"], "tok/s"),
    ts("Busy Slots per Decode", 0, 65, 8, 6, ["llamacpp:n_busy_slots_per_decode"], "short"),
    ts("iGPU Temperature", 8, 65, 4, 6, ["amdgpu_temp_millicelsius/1000"], "celsius"),
    ts("iGPU Power", 12, 65, 4, 6, ["amdgpu_power_microwatts/1000000"], "watt"),
    ts("Requests: processing / deferred", 16, 65, 8, 6,
       ["llamacpp:requests_processing", "llamacpp:requests_deferred"], "short",
       legends=["{{instance}} processing", "{{instance}} deferred"]),
]

# ───────────────────────── Storage ─────────────────────────
panels.append(row("Storage (Proxmox pools / PVCs / disk I/O / QNAP fabric)", 71))
panels += [
    bargauge("Proxmox Storage Pools (used %)", 0, 72, 8, 8,
             '100 * pve_disk_usage_bytes{id=~"storage/.*"} / pve_disk_size_bytes{id=~"storage/.*"}',
             legend="{{id}}"),
    bargauge("k8s PVCs (used %)", 8, 72, 8, 8,
             "100 * kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes",
             legend="{{persistentvolumeclaim}}"),
    ts("Host Disk Read+ / Write- Rate", 16, 72, 8, 8,
       [f'sum by (instance) (rate(node_disk_read_bytes_total{{{HOSTS},{DISKDEV}}}[5m]))',
        f'0 - sum by (instance) (rate(node_disk_written_bytes_total{{{HOSTS},{DISKDEV}}}[5m]))'],
       "Bps", legends=["{{instance}} read", "{{instance}} write"]),
    ts("QNAP Fabric Reachability (NFS / iSCSI)", 0, 80, 12, 6, ["probe_success"],
       "short", legends=["{{fabric}} @ {{node}}"], fill=0, maxv=1),
    ts("QNAP Fabric Probe Latency", 12, 80, 12, 6, ["probe_duration_seconds"],
       "s", legends=["{{fabric}} @ {{node}}"]),
]

dashboard = {
    "title": "AI Lab Fleet",
    "uid": "ailab-reporting",
    "tags": ["reporting", "infrastructure", "ailab", "fleet"],
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
print(f"wrote {out} ({len([p for p in panels if p['type'] != 'row'])} panels, {len([p for p in panels if p['type']=='row'])} rows)")
