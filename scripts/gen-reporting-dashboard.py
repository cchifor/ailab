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
    python scripts/dashboard-preview.py serve            # render it in a local Grafana (live Prometheus)
    python scripts/dashboard-preview.py check --row "PR Reviewers" --shot out.png   # Playwright gate
"""
import json
import pathlib

DS = "${DS_PROMETHEUS}"
DS_LOKI = "${DS_LOKI}"
_pid = 0
HOSTS = 'job="proxmox-node"'           # the 3 Proxmox hosts' node_exporter
AINODE = 'job="ai-llm-node"'           # the 3 AI LXCs' node_exporter (relabeled; was instance-IP regex)
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


def logs(title, x, y, w, h, expr):
    """A Loki logs panel. Separate helper because it takes the LOKI datasource, not DS."""
    return {
        "id": _nid(), "type": "logs", "title": title,
        "datasource": {"type": "loki", "uid": DS_LOKI},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                    "enableLogDetails": True, "dedupStrategy": "none"},
        "targets": [{"refId": "A", "datasource": {"type": "loki", "uid": DS_LOKI},
                     "expr": expr, "queryType": "range"}],
    }


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


def stat(title, x, y, w, h, expr, unit="none", decimals=0, steps=None, color="value", graph="area",
         mappings=None):
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


def bargauge(title, x, y, w, h, expr, unit="percent", legend="{{id}}", maxv=100, steps=None):
    return {
        "id": _nid(), "type": "bargauge", "title": title, "datasource": _ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit, "min": 0, "max": maxv, "decimals": 1,
            "thresholds": {"mode": "absolute", "steps": steps or [
                {"color": "green", "value": None}, {"color": "yellow", "value": 75},
                {"color": "red", "value": 90}]}}, "overrides": []},
        "options": {"displayMode": "gradient", "orientation": "horizontal", "showUnfilled": True,
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
        "targets": [{"refId": "A", "datasource": _ds(), "expr": expr, "legendFormat": legend, "instant": True}],
    }


def stat_name(title, x, y, w, h, expr, legend):
    """A stat that shows a LABEL rather than a number - for info metrics whose value is always
    1 (reviewbot_llm_seat_info, reviewbot_llm_active_model_info). textMode "name" renders the
    legendFormat, so the panel reads e.g. the account email or the model alias."""
    return {
        "id": _nid(), "type": "stat", "title": title, "datasource": _ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"thresholds": {"mode": "absolute",
                                                    "steps": [{"color": "blue", "value": None}]}},
                        "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "none", "graphMode": "none", "textMode": "name", "justifyMode": "auto"},
        "targets": [{"refId": "A", "datasource": _ds(), "expr": expr, "legendFormat": legend, "instant": True}],
    }


def state_timeline(title, x, y, w, h, exprs, legends):
    """A 0/1 series per row over time - parked (1) or free (0) per (seat, model) pair."""
    return {
        "id": _nid(), "type": "state-timeline", "title": title, "datasource": _ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {
            "custom": {"lineWidth": 0, "fillOpacity": 70},
            "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None},
                                                         {"color": "red", "value": 1}]},
            "mappings": [{"type": "value", "options": {
                "0": {"text": "free", "color": "green", "index": 0},
                "1": {"text": "parked", "color": "red", "index": 1}}}]}, "overrides": []},
        "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
                    "legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "single"}},
        "targets": [{"refId": chr(65 + i), "datasource": _ds(), "expr": e, "legendFormat": legends[i]}
                    for i, e in enumerate(exprs)],
    }


def _ov(name, props):
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def table(title, x, y, w, h, targets, rename, exclude, overrides, by="id", order=None, cell_height="sm"):
    """Several instant queries outer-joined on the `by` label into one row per key. `order` lists
    the ORIGINAL column names (before rename) left to right; columns it omits keep their place."""
    return {
        "id": _nid(), "type": "table", "title": title, "datasource": _ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "filterable": True, "cellOptions": {"type": "auto"}}},
                        "overrides": overrides},
        "options": {"showHeader": True, "footer": {"show": False}, "cellHeight": cell_height},
        "transformations": [
            {"id": "joinByField", "options": {"byField": by, "mode": "outer"}},
            {"id": "organize", "options": {"excludeByName": {k: True for k in exclude},
                                           "renameByName": rename,
                                           "indexByName": {n: i for i, n in enumerate(order or [])}}},
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


# The claude seat table: one instant series per seat per window, aggregated so the scrape
# labels (endpoint/instance/job/namespace/service) never become columns.
def _seat_pct(limit):
    return f'max by (seat) (reviewbot_llm_usage_percent{{persona="claude",limit="{limit}"}})'


def _seat_reset(limit):
    # Only a reset still ahead is a countdown. resets_at is 0 for a window with no open period,
    # and it is refreshed HOURLY, so once the reset moment passes the stale stamp sits in the
    # past until the next probe - a `> 0` filter alone rendered that as "-42 min"
    # (reviewer-claude on ailab#784). Either case -> no series -> a blank cell.
    return (f'max by (seat) (reviewbot_llm_usage_resets_at_seconds{{persona="claude",limit="{limit}"}} > time())'
            ' - time()')


USAGE_STEPS = [{"color": "green", "value": None}, {"color": "orange", "value": 80},
               {"color": "red", "value": 100}]


def _gauge_col(name):
    return _ov(name, [{"id": "unit", "value": "percent"}, {"id": "min", "value": 0},
                      {"id": "max", "value": 100}, {"id": "decimals", "value": 0},
                      {"id": "thresholds", "value": {"mode": "absolute", "steps": USAGE_STEPS}},
                      {"id": "custom.cellOptions",
                       "value": {"type": "gauge", "mode": "gradient", "valueDisplayMode": "color"}}])


def _reset_col(name):
    return _ov(name, [{"id": "unit", "value": "dtdurations"}, {"id": "decimals", "value": 0}])


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
# Compact summary only — the full deep-dive (GTT, prompt tput, busy-slots, power, requests) lives in the
# standalone "AI LLM" dashboard (gen-ai-dashboard.py). amdgpu_* filtered by the AI job so no other
# node_exporter textfile metric can leak into this row.
panels.append(row("AI (llama.cpp on Strix Halo iGPU)", 50))
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

# ───────────────────────── GitHub Actions Runners ─────────────────────────
RUNNERS = 'job="ci-runner-node"'       # the 5 GHA runner VMs' node_exporter
panels.append(row("GitHub Actions Runners (host node_exporter)", 86))
panels += [
    stat("Runners Up", 0, 87, 4, 4, f'count(up{{{RUNNERS}}} == 1) or vector(0)',
         steps=[{"color": "red", "value": None}, {"color": "green", "value": 5}]),
    stat("Runner Cores", 4, 87, 4, 4, f'count(node_cpu_seconds_total{{{RUNNERS},mode="idle"}}) or vector(0)'),
    stat("Runner Memory", 8, 87, 4, 4, f'sum(node_memory_MemTotal_bytes{{{RUNNERS}}}) or vector(0)', unit="bytes", decimals=1),
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
WORKERS = 'job="dev-worker-node"'      # the 3 dev-worker VMs' node_exporter
panels.append(row("Dev Workers (host node_exporter)", 105))
panels += [
    stat("Workers Up", 0, 106, 4, 4, f'count(up{{{WORKERS}}} == 1) or vector(0)',
         steps=[{"color": "red", "value": None}, {"color": "green", "value": 3}]),
    stat("Worker Cores", 4, 106, 4, 4, f'count(node_cpu_seconds_total{{{WORKERS},mode="idle"}}) or vector(0)'),
    stat("Worker Memory", 8, 106, 4, 4, f'sum(node_memory_MemTotal_bytes{{{WORKERS}}}) or vector(0)', unit="bytes", decimals=1),
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

# ───────────────────────── Test Env Pool ─────────────────────────
# The leasable test-environment pool (kubernetes/apps/infrastructure/testpool + the env-pool Talos
# worker). Sources: kube-state-metrics (env pods are created_by_kind="Sandbox"; the pre-pull
# DaemonSet is deliberately excluded by that filter), node_exporter on the env node(s), and
# kubelet volume stats. cAdvisor is BLIND to kata pods on this estate — node-level panels instead.
ENVNODE = 'instance=~"192.168.0.37:9100"'   # env-pool Talos worker node_exporter (extend when env-node-2 lands)
TP = 'namespace="testpool"'
TPPOD = f'kube_pod_info{{{TP},created_by_kind="Sandbox"}}'
panels.append(row("Test Env Pool (leasable Kata DinD environments — testpool)", 124))
panels += [
    stat("Envs Ready", 0, 125, 4, 4,
         f'(count((kube_pod_status_ready{{{TP},condition="true"}} == 1) * on (namespace, pod) group_left () {TPPOD}) or vector(0))',
         steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}]),
    stat("Envs Total (warm + leased)", 4, 125, 4, 4, f'(count({TPPOD}) or vector(0))'),
    stat("Env Volumes (PVCs)", 8, 125, 4, 4, f'(count(kube_persistentvolumeclaim_info{{{TP}}}) or vector(0))'),
    stat("Operator Up", 12, 125, 4, 4,
         'kube_deployment_status_replicas_available{namespace="agent-sandbox-system",deployment="agent-sandbox-controller"}',
         steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}]),
    stat("Env Node CPU Used", 16, 125, 4, 4,
         f'100 * (1 - avg(rate(node_cpu_seconds_total{{{ENVNODE},mode="idle"}}[5m])))',
         unit="percent", decimals=1, steps=PCT),
    stat("Env Node Memory Used", 20, 125, 4, 4,
         f'100 * (1 - sum(node_memory_MemAvailable_bytes{{{ENVNODE}}}) / sum(node_memory_MemTotal_bytes{{{ENVNODE}}}))',
         unit="percent", decimals=1, steps=PCT),
    ts("Environments over Time (Ready / total)", 0, 129, 8, 7,
       [f'(count((kube_pod_status_ready{{{TP},condition="true"}} == 1) * on (namespace, pod) group_left () {TPPOD}) or vector(0))',
        f'(count({TPPOD}) or vector(0))'],
       "short", legends=["ready", "total"], decimals=0),
    ts("Env Node CPU / Memory %", 8, 129, 8, 7,
       [f'100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{{{ENVNODE},mode="idle"}}[5m])))',
        f'100 * (1 - node_memory_MemAvailable_bytes{{{ENVNODE}}} / node_memory_MemTotal_bytes{{{ENVNODE}}})'],
       "percent", legends=["{{instance}} cpu", "{{instance}} mem"], maxv=100),
    ts("Env Volume Usage", 16, 129, 8, 7,
       [f'kubelet_volume_stats_used_bytes{{{TP}}}'],
       "bytes", legends=["{{persistentvolumeclaim}}"]),
]

# ───────────────────────── PR Reviewers ─────────────────────────
# The automatic LLM review bots (ansible/roles/pr_reviewer; plan: agentforge
# plans/2026-09-02-ai-pr-review-plan.md). Source: reviewbot_* textfile metrics through the
# workers' node_exporter — heartbeat AGE is the liveness signal (a dead daemon leaves a
# stale file that queue-depth panels would happily keep showing).
#
# ERRORS ARE A FIRST-CLASS ROW HERE, because this dashboard could not show the 2026-09-06
# incident at all: platform#1074 (a 4.0 MB diff carrying a non-UTF-8 byte) failed on EVERY
# attempt on BOTH personas for 22 hours — 66 failures — and the only visible trace was a
# sawtooth on "Queue Depth / Oldest Age", which is the RETRY BACKOFF of the failing job and
# reads like mild queueing rather than a hard stop. reviewbot_llm_failures_total was already
# being exported and was plotted nowhere.
#
# Two panel-level traps this row is written to avoid:
#   * The "Quarantined" stat used reviewbot_quarantined_jobs, the CUMULATIVE gauge, which
#     never falls — after any single quarantine it is permanently red and therefore ignored.
#     The 24h-windowed twin is the one that answers "is something wrong NOW".
#   * Counters must be shown as increase() over a window, not as raw totals: a monotonic
#     total climbs forever and its slope is invisible at a glance.
panels.append(row("PR Reviewers (automatic LLM review bots — reviewbot)", 136))
panels += [
    stat("Claude Bot Heartbeat Age", 0, 137, 4, 4,
         'time() - reviewbot_heartbeat_timestamp_seconds{persona="claude"}',
         unit="s", steps=[{"color": "green", "value": None}, {"color": "orange", "value": 120},
                          {"color": "red", "value": 900}]),
    stat("Codex Bot Heartbeat Age", 4, 137, 4, 4,
         'time() - reviewbot_heartbeat_timestamp_seconds{persona="codex"}',
         unit="s", steps=[{"color": "green", "value": None}, {"color": "orange", "value": 120},
                          {"color": "red", "value": 900}]),
    stat("Queue Depth (all personas)", 8, 137, 4, 4, 'sum(reviewbot_queue_depth) or vector(0)',
         steps=[{"color": "green", "value": None}, {"color": "orange", "value": 3},
                {"color": "red", "value": 10}]),
    stat("Oldest Queued Job", 12, 137, 4, 4,
         'max(reviewbot_oldest_job_age_seconds) or vector(0)', unit="s",
         steps=[{"color": "green", "value": None}, {"color": "orange", "value": 1800},
                {"color": "red", "value": 7200}]),
    # Orange at 3 mirrors ReviewbotReviewFailures / ReviewbotReviewTimeouts exactly, so the
    # panel turning amber and the alert firing mean the same thing. Healthy is a flat 0:
    # every one of the 66 failures in the 48h around 2026-09-06 was the same broken PR.
    stat("Review Failures (1h)", 16, 137, 4, 4,
         'sum(increase(reviewbot_llm_failures_total[1h])) or vector(0)',
         steps=[{"color": "green", "value": None}, {"color": "orange", "value": 3},
                {"color": "red", "value": 10}]),
    stat("Deadline Timeouts (1h)", 20, 137, 4, 4,
         'sum(increase(reviewbot_llm_timeouts_total[1h])) or vector(0)',
         steps=[{"color": "green", "value": None}, {"color": "orange", "value": 3},
                {"color": "red", "value": 10}]),
    # 24h window, NOT the cumulative gauge — see the header.
    stat("Quarantined (24h)", 0, 141, 4, 4,
         'sum(reviewbot_quarantined_recent_jobs) or vector(0)',
         steps=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
    # A job held this long is wedged, not working: one attempt is capped at llm_timeout_s
    # (900s claude / 600s codex) including the fallback. Matches ReviewbotWorkerStuck.
    stat("Running Job Age", 4, 141, 4, 4,
         'max(reviewbot_running_job_age_seconds) or vector(0)', unit="s",
         steps=[{"color": "green", "value": None}, {"color": "orange", "value": 900},
                {"color": "red", "value": 2400}]),
    # The evidence that decides whether llm_timeout_s is still right. SPLIT PER PERSONA
    # because the deadlines differ (claude 900s, codex 600s): a single max() across both,
    # thresholded on claude's budget, renders a codex run one second from ITS deadline as
    # green — blind for the tighter persona, which is the one that would break first.
    # Orange at two thirds of each persona's own budget.
    stat("Longest Review — claude", 8, 141, 4, 4,
         'max(reviewbot_llm_seconds_max{persona="claude"}) or vector(0)', unit="s",
         steps=[{"color": "green", "value": None}, {"color": "orange", "value": 600},
                {"color": "red", "value": 900}]),
    stat("Longest Review — codex", 12, 141, 4, 4,
         'max(reviewbot_llm_seconds_max{persona="codex"}) or vector(0)', unit="s",
         steps=[{"color": "green", "value": None}, {"color": "orange", "value": 400},
                {"color": "red", "value": 600}]),
    stat("Peak Output Tokens", 16, 141, 4, 4,
         'max(reviewbot_llm_output_tokens_max) or vector(0)'),
    # 24h, not the cumulative total: the total only ever climbs and says nothing about now.
    # The "Reviews Completed over Time" panel below carries the running figure.
    stat("Reviews Done (24h)", 20, 141, 4, 4,
         'sum(increase(reviewbot_jobs_done[24h])) or vector(0)'),
    # THE ERROR PANEL. Failures and timeouts are separate series because they mean different
    # things and have different remedies: a timeout says the deadline is too tight for the
    # work, a failure says the review could not be produced at all (unparseable output, an
    # undecodable diff, a Gitea write that did not land).
    ts("Errors — Review Failures / Deadline Timeouts (1h)", 0, 145, 12, 7,
       ['increase(reviewbot_llm_failures_total[1h])',
        'increase(reviewbot_llm_timeouts_total[1h])',
        'reviewbot_quarantined_recent_jobs'],
       "short", legends=["{{persona}} failures", "{{persona}} timeouts",
                         "{{persona}} quarantined 24h"], decimals=0),
    # Duration against the deadline it must fit inside. The gap between last and max is what
    # says whether the budget has headroom or is being grazed.
    ts("Review Duration vs Deadline", 12, 145, 12, 7,
       ['reviewbot_llm_seconds_last', 'reviewbot_llm_seconds_max'],
       "s", legends=["{{persona}} last", "{{persona}} max"], decimals=0),
    ts("Reviews Completed over Time", 0, 152, 12, 7,
       ['reviewbot_jobs_done'], "short", legends=["{{persona}}"], decimals=0),
    # The sawtooth here is retry backoff, not queueing: a job in 'retry' counts toward both
    # series until its next_at expires, so a permanently failing PR draws a rising ramp that
    # resets on every attempt. Read it together with the Errors panel above.
    ts("Queue Depth / Oldest Age", 12, 152, 12, 7,
       ['reviewbot_queue_depth', 'reviewbot_oldest_job_age_seconds'],
       "short", legends=["{{persona}} depth", "{{persona}} oldest s"]),
    # ── seats, ladder and usage (plans/2026-09-18-claude-seat-rotation-plan.md, PR 2) ──────
    # Which account and model the claude persona is on RIGHT NOW, and every account's windows
    # from the usage API (GET /api/oauth/usage, read hourly by reviewbot as each seat). The
    # email lives on ONE info series, reviewbot_llm_seat_info, and is joined onto the active-seat
    # series and the table here, so the numeric series never carry it.
    #
    # LAYOUT RULES, learned from the first cut (2026-09-18, screenshot review): a stat 3 columns
    # wide truncates its own title ("Seats Usa..."), a 6-wide stat wraps an email, a bar gauge
    # with nine bars labelled "<email> · <window>" cuts every label, and a table fed straight
    # from the scrape leaks endpoint/instance/job/namespace/service as columns and scrolls
    # sideways. So: every stat is >= 4 wide, the account stat takes half the row, the numeric
    # series are aggregated (max by (seat)) so the scrape labels never reach the table, and the
    # usage is ONE WIDE TABLE - one row per account, one column pair (used %, resets in) per
    # window - with the percentages drawn as in-cell gauges. The windows are fixed by the API
    # (session, weekly_all, weekly_fable), which is what makes the wide shape possible.
    stat_name("Active Claude Account", 0, 159, 12, 4,
              'reviewbot_llm_active_seat_info{persona="claude"} * on(persona,seat) '
              'group_left(email,plan) reviewbot_llm_seat_info{persona="claude"}',
              "{{email}} · seat {{seat}}"),
    stat_name("Active Claude Model", 12, 159, 4, 4,
              'reviewbot_llm_active_model_info{persona="claude"}', "{{model}}"),
    # 0 = a seat's probe failed on the last poll (ReviewbotUsageProbeFailing after 3h of it).
    stat("Usage Probe", 16, 159, 4, 4,
         'min(reviewbot_llm_usage_probe_ok{persona="claude"}) or vector(0)',
         steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
         mappings=[{"type": "value", "options": {"1": {"text": "OK", "index": 0},
                                                 "0": {"text": "FAILING", "index": 1}}}]),
    # Seats that can serve SOMETHING: neither account-parked nor parked on every tier.
    # Aggregated before `or vector(0)`: a labelled series OR'd with the label-less vector(0)
    # keeps BOTH (the label sets differ), and the stat showed a phantom red zero beside the
    # real value (codex review of PR 2). min() drops the labels, as the other stats do.
    stat("Seats Usable", 20, 159, 4, 4,
         'min(reviewbot_llm_seats_available{persona="claude"}) or vector(0)',
         steps=[{"color": "red", "value": None}, {"color": "orange", "value": 1},
                {"color": "green", "value": 2}]),
    # Orange at 80, red at 100: 100 IS the parked state, and the API's percent is what the
    # persona is parked on, so anything below it is still capacity. A window with no open
    # period reports resets_at = 0 (a seat with no session running) and one that just reset
    # carries a stale past stamp; _seat_reset drops both so the cell is blank rather than the
    # "0 seconds" a clamp produced. The email rides on its OWN target (H), not on the session
    # series: a seat whose probe is failing, or a fresh seat with no usage yet, still gets its
    # account named - the state that most needs it (reviewer-claude on ailab#784).
    table("Claude Seats — usage per account and window", 0, 163, 24, 7,
          targets=[
              ("H", 'max by (seat, email) (reviewbot_llm_seat_info{persona="claude"})'),
              ("A", _seat_pct("session")),
              ("B", _seat_pct("weekly_all")),
              ("C", _seat_pct("weekly_fable")),
              ("D", _seat_reset("session")),
              ("E", _seat_reset("weekly_all")),
              ("F", _seat_reset("weekly_fable")),
              ("G", '(max by (seat) (reviewbot_llm_active_seat_info{persona="claude"}) * 2) '
                    'or max by (seat) (reviewbot_llm_seat_parked{persona="claude"})'),
          ],
          by="seat",
          order=["seat", "email", "Value #G", "Value #A", "Value #D", "Value #B", "Value #E",
                 "Value #C", "Value #F"],
          rename={"seat": "Seat", "email": "Account", "Value #G": "State",
                  "Value #A": "Session used", "Value #D": "Session resets in",
                  "Value #B": "Weekly used (all models)", "Value #E": "Weekly resets in",
                  "Value #C": "Weekly used (Fable)", "Value #F": "Fable resets in"},
          exclude=["Time", "Value #H"] + [f"Time {i}" for i in range(1, 9)],
          overrides=[
              _ov("Seat", [{"id": "custom.width", "value": 70}]),
              _ov("State", [{"id": "custom.width", "value": 100},
                            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                            {"id": "mappings", "value": [{"type": "value", "options": {
                                "2": {"text": "● active", "color": "green", "index": 0},
                                "1": {"text": "parked", "color": "red", "index": 1},
                                "0": {"text": "free", "color": "text", "index": 2}}}]}]),
          ] + [_gauge_col(c) for c in ("Session used", "Weekly used (all models)",
                                       "Weekly used (Fable)")]
            + [_reset_col(c) for c in ("Session resets in", "Weekly resets in",
                                       "Fable resets in")],
          cell_height="md"),
    # Which seats and which (seat, model) pairs the rotation is parked on: the ladder's whole
    # state in one picture. The account row is the seat-level park (a weekly_all wall, or a
    # rate limit the API did not attribute to a model); without it a seat parked that way
    # shows every tier green. Twelve rows: 9 units tall, or a row is 11px and unreadable.
    state_timeline("Parked per Seat — account and per tier", 0, 170, 24, 9,
                   ['reviewbot_llm_seat_parked{persona="claude"}',
                    'reviewbot_llm_model_parked{persona="claude"}'],
                   ["{{seat}} / account", "{{seat}} / {{model}}"]),
    # THE REASON, not just the rate. Everything above is numeric and can only say THAT a review
    # failed; this says which PR and why. Shipped by roles/journal_ship (Alloy -> loki-lan).
    # The filter is deliberately broad — `failed`, `error`, `skipped` — because the 2026-09-06
    # failure text ("'utf-8' codec can't decode byte 0xf6") matched no term anyone would have
    # thought to search for in advance.
    logs("Reviewer Errors — reviewbot journal (failures, errors, skips)", 0, 179, 24, 9,
         '{job="host-journal", unit="reviewbot.service"} '
         '|~ "(?i)(failed|error|quarantin|skipped|exhausted)"'),
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
         "current": {}, "hide": 0, "label": "Datasource", "refresh": 1},
        # The Reviewer Errors logs panel needs Loki. Same shape loki-logs-dashboard.yaml uses.
        {"name": "DS_LOKI", "type": "datasource", "query": "loki",
         "current": {}, "hide": 0, "label": "Logs", "refresh": 1},
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
