#!/usr/bin/env python3
"""Generate the "Local Drives" Grafana dashboard ConfigMap (deterministic, valid JSON).

Emits kubernetes/apps/infrastructure/monitoring/local-drives-dashboard.yaml, a ConfigMap labeled
grafana_dashboard=1 that the kube-prometheus-stack Grafana sidecar loads (uid ailab-local-drives).

WHAT IT WATCHES. Each Proxmox host (ai-node1/2/3) has ONE consumer QLC NVMe (nvme0n1, Kingston
OM8TAP42048K1, DRAM-less, no power-loss protection). It holds the host's root and swap and the LVM thin
pool pve/data (local-lvm), which holds every local VM and LXC disk: the Talos control planes' etcd, CI
runners, dev workers, the zot registry, the LLM containers. On 2026-11-20 node1/node2's drives had used
91%/87% of their rated write endurance, and etcd stalls traced to seconds-long device stalls under CI
write bursts (ailab memory ai-node-nvme-wear-and-thermals). It replaces the two panels the owner found
not useful on the AI Lab Fleet dashboard ("NVMe Writes per Day + Busy %", "NVMe Wear").

SECTIONS (one row each; every panel filters on the $host variable, All by default):
  Overview        life used, written in the last 24 h and per day over 7 days, the 7-day projection to
                  100%, pool used, drive warnings.
  Throughput      read and write bytes/s and IOPS at the drive (nvme0n1), each on its own panel.
  Latency         average read/write latency, flush latency and rate, queue depth, busy %, the host's
                  IO pressure (PSI), and the WAL fsync / backend commit p99 of the etcd member running on
                  that host, so a stall can be read across all of them at one instant.
  Who uses it     per consumer: every guest (by name, VM or LXC), the host's root and swap, and the
                  thin pool's metadata. Source: the host's own device-mapper stats (node_disk_* on the
                  dm-N devices + node_disk_device_mapper_info), which see VMs and LXCs alike and add up to
                  the drive's total. pve-exporter's per-guest counters do not: they are QEMU's view, and
                  for a VM whose disk is not on this drive (talos-cp1 since 2026-11-20) they count
                  traffic that never touches it.
  Capacity        the thin pool's used vs free in GiB and %, metadata %, per-guest allocation and the
                  "untrimmed gap": what a guest's thin volumes hold minus what its filesystems use.
  Endurance       life used over 14 days, bytes written per day (the drive's own counter), the
                  identity/health table, temperatures (composite and the three sensors).
  Trim            discards reaching the pool per consumer, and the age of each trim job's last success.

SOURCES. node_exporter on the hosts (job proxmox-node: diskstats, PSI, hwmon, and the Debian
prometheus-node-exporter-collectors nvme textfile, refreshed every 15 min); pve-exporter (job
pve-exporter: guest names, the pool's size/usage); kube-etcd and kube-state-metrics; the guests' own
node_exporters (filesystem use). Two families are not live yet and their panels read "No data" until
they are: pve_thin_* (#1886, pve_base tag thin_textfile: pool metadata %, per-volume allocation) and the
trim beacons of #1883/#1884/#1885 (pve_lxc_fstrim_*, the node-fstrim CronJobs, node_fstrim_*).

Tests: scripts/tests/test_local_drives_dashboard.py (structure, no services) and
scripts/tests/test_local_drives_dashboard_promql.py (what the joins return, promtool in Docker).

    python scripts/gen-local-drives-dashboard.py
    python scripts/dashboard-preview.py serve --dashboard kubernetes/apps/infrastructure/monitoring/local-drives-dashboard.yaml
"""
import json
import pathlib

DS = "${DS_PROMETHEUS}"
UID = "ailab-local-drives"
OUT = pathlib.Path(__file__).resolve().parents[1] / "kubernetes/apps/infrastructure/monitoring/local-drives-dashboard.yaml"

HOSTS = 'job="proxmox-node"'
NVME = 'device="nvme0n1"'                     # the one drive per host; pinned so a second one can't mix in
HOST_SEL = f'node_uname_info{{{HOSTS},nodename=~"$host"}}'
RI = "$__rate_interval"                       # >= 4 scrapes (30 s): rate[30s] returns nothing here
DATA_UNIT = 512000                            # bytes per NVMe "data unit" (1000 x 512 B, NVMe base spec)

# Device-mapper volumes on the host. A guest disk is vm-<vmid>-disk-<n> (base-* for a template); the
# cloud-init drives (vm-<vmid>-cloudinit, 4 MiB) are left out. The host's own root and swap and the thin
# pool's metadata are the only other writers that reach the drive (data_tdata and the pool's own layers
# are the SUM of the guest volumes, so counting them too would count every byte twice).
DM_GUEST = 'node_disk_device_mapper_info{job="proxmox-node",lv_name=~"(vm|base)-[0-9]+-disk-[0-9]+"}'
DM_HOST = 'node_disk_device_mapper_info{job="proxmox-node",lv_name=~"root|swap|data_tmeta"}'
# vmid -> guest name and type (qemu | lxc). job="pve-exporter" only: the cloudlab exporter (cloud-pve)
# reuses vmids. last_over_time [1h]: pve-exporter's scrapes have gaps (up to ~30 min in a day), and a
# per-step join would turn a guest into "vmid <n>" for them. topk by (vmid) (1, ...): one name per vmid
# even in the hour after a rename, and the old and new exporter pods of a rollout count once.
GUEST_INFO = 'last_over_time(pve_guest_info{job="pve-exporter"}[1h])'
GUEST_NAMES = (f'topk by (vmid) (1, max by (vmid, name, type) (label_replace({GUEST_INFO}, '
               f'"vmid", "$2", "id", "(qemu|lxc)/([0-9]+)")))')
# guest name -> the host it runs on (pve_guest_info's `node`), filtered to the selected hosts; one host per
# guest even in the hour after a migration.
GUEST_HOST = (f'topk by (name) (1, max by (name, nodename) (label_replace({GUEST_INFO}, '
              f'"nodename", "$1", "node", "(.+)"))) * on(nodename) group_left() max by (nodename) ({HOST_SEL})')
# Guest filesystems on block devices, from the guests' own node_exporters (their nodename is the
# Proxmox guest name). One device mounted twice counts once.
GUEST_FS = 'fstype=~"ext4|xfs|btrfs|vfat",job!="proxmox-node"'
# A VM's own virtual disks as the guest sees them. A Talos node also sees the QNAP's iSCSI LUNs
# (model iSCSI_Storage, its PVCs), which are not on this drive.
QEMU_DISK = 'node_disk_info{job!="proxmox-node",model="QEMU_HARDDISK"}'
# Range totals add up one STEP-long increase per step (see range_total), each attributed with the device
# mapping of its own step. 5m: the increases tile the range exactly, 288 steps for a day.
STEP = "5m"

TYPE_MAP = [{"type": "value", "options": {
    "qemu": {"text": "VM", "index": 0}, "lxc": {"text": "LXC", "index": 1},
    "host": {"text": "host", "color": "text", "index": 2}}}]
WEAR_STEPS = [{"color": "green", "value": None}, {"color": "orange", "value": 70}, {"color": "red", "value": 85},
              {"color": "purple", "value": 95}]
POOL_STEPS = [{"color": "green", "value": None}, {"color": "orange", "value": 75}, {"color": "red", "value": 85}]
TEMP_LIMITS = [{"color": "green", "value": None}, {"color": "orange", "value": 86}, {"color": "red", "value": 90}]
TRIM_AGE_STEPS = [{"color": "green", "value": None}, {"color": "orange", "value": 26 * 3600},
                  {"color": "red", "value": 50 * 3600}]

_pid = 0


def _nid():
    global _pid
    _pid += 1
    return _pid


def _ds():
    return {"type": "prometheus", "uid": DS}


# ───────────────────────── query builders ─────────────────────────
def on_host(expr):
    """Keep the selected hosts only, and label each series with the host's name (nodename)."""
    return f"({expr}) * on(instance) group_left(nodename) {HOST_SEL}"


def drive(metric, fn="rate", window=RI):
    """A counter of the host's NVMe, as a rate (or another range function)."""
    return f"{fn}({metric}{{{HOSTS},{NVME}}}[{window}])"


def ratio(num, den, window=RI):
    """Time per operation: rate(time) / rate(ops). No series while there were no ops (not 0/0 = NaN)."""
    return f"{drive(num, window=window)} / ({drive(den, window=window)} > 0)"


def pool(metric):
    """pve-exporter's local-lvm series for the selected hosts, one per host, labelled nodename (its id is
    storage/<node>/local-lvm). max by: a new exporter pod is a new series (pod/instance labels), and the
    old and new pods overlap for a scrape or two, which drew a pool twice its size."""
    return (f'max by (nodename) (label_replace({metric}{{job="pve-exporter",id=~"storage/($host)/local-lvm"}}, '
            f'"nodename", "$1", "id", "storage/([^/]+)/local-lvm"))')


def top(expr, pick, only_active=False):
    """Draw `expr` over the range for the $topn series with the highest `pick`, chosen ONCE, at the range's
    end (@ end()). topk() alone re-picks at every step, so a legend gathers every series that was ever briefly
    on top and its mean covers only those moments. `pick` is evaluated at the end only and must carry
    nodename and name: a range_total() for a counter, the value itself for a gauge. only_active: never pick a
    series whose total is 0 (a quiet range draws nothing rather than flat zeros)."""
    if only_active:
        pick = f"({pick}) > 0"
    return f"{expr} and on(nodename, name) last_over_time((topk($topn, {pick}))[1m:1m] @ end())"


def range_total(metric, scale=""):
    """A consumer's total over the selected range: the sum of its {STEP} increases, each joined to the
    device-mapper mapping at its OWN step. A dm-N number is reused when a guest stops or the host reboots;
    an increase over the whole range joined to the mapping at its end would hand the old owner's I/O to
    the new one and drop a volume that is gone at the end (reviewer-codex on #1890)."""
    return f"sum_over_time(({consumers(metric, fn='increase', window=STEP, scale=scale)})[$__range:{STEP}])"


def consumers(metric, fn="rate", window=RI, scale=""):
    """Per consumer of the drive: every guest (its disks summed, named from pve_guest_info; a vmid that
    pve-exporter does not know reads "vmid <n>"), plus "<host> root", "<host> swap" and "<host> thin-pool
    metadata". Labels: nodename, name, type (qemu | lxc | host), vmid. `scale` multiplies (sectors -> bytes)."""
    r = f'{fn}({metric}{{{HOSTS},device=~"dm-[0-9]+"}}[{window}]){scale}'
    guest = (f'sum by (instance, vmid) (label_replace({r} * on(instance, device) group_left(lv_name) {DM_GUEST}, '
             f'"vmid", "$1", "lv_name", "(?:vm|base)-([0-9]+)-.+"))')
    named = (f'({guest} * on(vmid) group_left(name, type) {GUEST_NAMES}) '
             f'or on(instance, vmid) label_replace({guest}, "name", "vmid $1", "vmid", "(.+)")')
    host = f'sum by (instance, lv_name) ({r} * on(instance, device) group_left(lv_name) {DM_HOST})'
    host_named = (f'label_replace(label_replace(label_join({on_host(host)}, "name", " ", "nodename", "lv_name"), '
                  f'"name", "$1 thin-pool metadata", "name", "(.+) data_tmeta"), "type", "host", "", "")')
    return f"max by (nodename, name, type, vmid) ({on_host(named)} or {host_named})"


def thin_by_guest(metric):
    """#1886's per-volume thin-pool series (pve_thin_lv_*), summed over a guest's disks, named and on the
    selected hosts. Labels: nodename, name, type, vmid."""
    s = f'sum by (instance, vmid) ({metric}{{{HOSTS},lv=~"(vm|base)-[0-9]+-disk-[0-9]+"}})'
    named = (f'({s} * on(vmid) group_left(name, type) {GUEST_NAMES}) '
             f'or on(instance, vmid) label_replace({s}, "name", "vmid $1", "vmid", "(.+)")')
    return f"max by (nodename, name, type, vmid) ({on_host(named)})"


def guest_fs_used():
    """Bytes a guest's own filesystems use ON ITS LOCAL VOLUMES (size - free, the filesystem's metadata
    included), per vmid, from the guests' own node_exporters. Only filesystems that can be tied to the
    guest's local-lvm volumes count, so storage elsewhere (the QNAP) can't make the gap look smaller
    (reviewer-codex on #1890):
      - a container's volumes are mounted under their device-mapper name, /dev/mapper/pve-vm--<id>--disk--<n>:
        exact, per volume;
      - a VM's filesystems on its QEMU hard disks count only when the guest has as many QEMU hard disks as
        the host has local thin volumes for it (#1886's pve_thin_lv_size_bytes). A VM with a disk elsewhere
        gets no figure rather than a wrong one. iSCSI LUNs (a Talos node's PVCs) are never QEMU disks.
    Guests without node_exporter (the registry container) have no figure. Labels: name, type, vmid, nodename."""
    fs_used = f"(node_filesystem_size_bytes{{{GUEST_FS}}} - node_filesystem_free_bytes{{{GUEST_FS}}})"
    lxc_dev = 'device=~"/dev/mapper/pve-vm--[0-9]+--disk--[0-9]+"'
    lxc_used = (f"(node_filesystem_size_bytes{{{GUEST_FS},{lxc_dev}}} - "
                f"node_filesystem_free_bytes{{{GUEST_FS},{lxc_dev}}})")
    lxc = (f'sum by (vmid) (max by (vmid, device) (label_replace({lxc_used}, "vmid", "$1", "device", '
           f'"/dev/mapper/pve-vm--([0-9]+)--disk--[0-9]+")))')
    uname = 'label_replace(node_uname_info{job!="proxmox-node"}, "name", "$1", "nodename", "(.+)")'
    qemu = f'label_replace({QEMU_DISK}, "disk", "$1", "device", "(.+)")'
    vm_fs = (f'max by (job, instance, device) (label_replace({fs_used}, "disk", "$1", "device", '
             f'"/dev/((?:sd|vd)[a-z]+)[0-9]*") * on(job, instance, disk) group_left() {qemu})')
    vm_used = (f"sum by (name) ({vm_fs} * on(job, instance) group_left(name) {uname}) "
               f"* on(name) group_left(vmid) {GUEST_NAMES}")
    vm_disks = (f"count by (name) ({QEMU_DISK} * on(job, instance) group_left(name) {uname}) "
                f"* on(name) group_left(vmid) {GUEST_NAMES}")
    local_vols = f'count by (vmid) (pve_thin_lv_size_bytes{{{HOSTS},lv=~"(vm|base)-[0-9]+-disk-[0-9]+"}})'
    vm = (f"max by (vmid) ({vm_used}) and on(vmid) "
          f"(max by (vmid) ({vm_disks}) == on(vmid) {local_vols})")
    return (f"max by (nodename, name, type, vmid) ((({lxc}) or on(vmid) ({vm})) "
            f"* on(vmid) group_left(name, type) {GUEST_NAMES} * on(name) group_left(nodename) {GUEST_HOST})")


def untrimmed_gap():
    """Thin allocation minus the guest's filesystem use: blocks the guest freed but never discarded, plus
    filesystem overhead and anything outside its filesystems (Talos's boot partitions, a guest's swap)."""
    return (f"{thin_by_guest('pve_thin_lv_allocated_bytes')} - on(nodename, name, type, vmid) "
            f"{guest_fs_used()}")


def etcd_p99(hist):
    """p99 of an etcd disk histogram for the member whose VM runs on each selected host. etcd instance
    (<ip>:2381) -> kube_node_info (internal_ip -> node) -> pve_guest_info (name -> node = host)."""
    p = f'histogram_quantile(0.99, sum by (instance, le) (rate({hist}{{job="kube-etcd"}}[{RI}])))'
    by_ip = f'label_replace({p}, "internal_ip", "$1", "instance", "(.+):[0-9]+")'
    member = (f'{by_ip} * on(internal_ip) group_left(name) '
              f'max by (internal_ip, name) (label_replace(kube_node_info, "name", "$1", "node", "(.+)"))')
    return f"max by (nodename, name) ({member} * on(name) group_left(nodename) {GUEST_HOST})"


def trim_ages():
    """Seconds since each trim's last success, per guest, labelled `trim` (which job) and nodename.
    CI runners: gitea_runner_fstrim_last_run_seconds (main; a run trims one slice, so it is the last RUN).
    Dev workers: node_fstrim_last_success_timestamp_seconds (#1885). Containers: the oldest volume's last
    complete pass of the host's nightly sweep (#1883). Talos nodes: their node-fstrim CronJob's last
    success (#1884)."""
    def by_nodename(metric, job, trim):
        return (f'label_replace(max by (name) ({metric}{{job="{job}"}} * on(job, instance) group_left(name) '
                f'label_replace(node_uname_info{{job="{job}"}}, "name", "$1", "nodename", "(.+)")), '
                f'"trim", "{trim}", "", "")')
    parts = [
        by_nodename("gitea_runner_fstrim_last_run_seconds", "ci-runner-node", "runner nightly (last run)"),
        by_nodename("node_fstrim_last_success_timestamp_seconds", "dev-worker-node", "dev-worker daily"),
        (f'label_replace(max by (name) (min by (vmid) (label_replace('
         f'pve_lxc_fstrim_volume_last_complete_timestamp_seconds{{{HOSTS}}}, "vmid", "$1", "ctid", "(.+)")) '
         f'* on(vmid) group_left(name) {GUEST_NAMES}), "trim", "LXC nightly sweep", "", "")'),
        (f'label_replace(max by (name) (label_replace(kube_cronjob_status_last_successful_time'
         f'{{namespace="node-fstrim"}}, "name", "$1", "cronjob", "node-fstrim-(.+)")), '
         f'"trim", "Talos node-fstrim", "", "")'),
    ]
    stamps = " or ".join(f"({p})" for p in parts)
    return f"max by (name, trim, nodename) ((time() - ({stamps})) * on(name) group_left(nodename) {GUEST_HOST})"


DUW = f"nvme_data_units_written_total{{{HOSTS},{NVME}}}"
USED = f"nvme_percentage_used_ratio{{{HOSTS},{NVME}}}"


def written_units(window):
    """Data units the drive took in `window`: the RAW difference of its own counter now and `window` ago
    (exact; increase() extrapolates), unless the counter went back inside the window, i.e. the drive was
    replaced: then increase(), which counts across the reset. A raw difference would go negative there,
    and the projection below would read a falsely reassuring rate for days (reviewer-codex on #1891)."""
    return (f"(({DUW} - {DUW} offset {window}) unless resets({DUW}[{window}]) > 0) "
            f"or increase({DUW}[{window}])")


def written_bytes(window, per_day=1):
    return f"({written_units(window)}) * {DATA_UNIT}" + (f" / {per_day}" if per_day != 1 else "")


# Days to 100% at the last 7 days' write rate, with the data units per 1% the drive has shown over its life
# (percentage_used moves in whole percent, so its own 7-day slope is too coarse to project from). No figure
# until the drive reports 1% used (a new or just-replaced drive reads 0, and dividing by it would project
# years); >= 1 data unit/day; a drive past 100% reads 0 days.
DAYS_LEFT = (f"clamp_min((1 - {USED}) * ({DUW} / {USED}) / clamp_min(({written_units('7d')}) / 7, 1), 0) "
             f"and {USED} >= 0.01")


# ───────────────────────── panel builders ─────────────────────────
def row(title, y):
    return {"id": _nid(), "type": "row", "title": title, "collapsed": False,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}


def _target(i, expr, legend=None, instant=False, table=False):
    t = {"refId": chr(65 + i), "datasource": _ds(), "expr": expr}
    if legend is not None:
        t["legendFormat"] = legend
    if instant:
        t["instant"] = True
        t["range"] = False
    if table:
        t["format"] = "table"
    return t


def ts(title, x, y, w, h, exprs, unit, legends, description, fill=0, stack=False, maxv=None, minv=None,
       thresholds=None, overrides=None, time_from=None, decimals=None, repeat=None, calcs=None):
    """One unit per panel (the owner's complaint about the old NVMe panel was two units on one axis)."""
    defaults = {"unit": unit, "custom": {"drawStyle": "line", "lineWidth": 1, "fillOpacity": fill,
                                         "showPoints": "never", "spanNulls": False,
                                         "stacking": {"mode": "normal" if stack else "none", "group": "A"}}}
    if maxv is not None:
        defaults["max"] = maxv
    if minv is not None:
        defaults["min"] = minv
    if decimals is not None:
        defaults["decimals"] = decimals
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
        defaults["custom"]["thresholdsStyle"] = {"mode": "dashed"}
    legend = {"displayMode": "table" if calcs else "list", "placement": "bottom", "showLegend": True}
    if calcs:
        legend["calcs"] = calcs
        # largest first: the legend table lists what matters at the top
        legend["sortBy"], legend["sortDesc"] = {"mean": "Mean", "max": "Max", "lastNotNull": "Last *"}[calcs[0]], True
    panel = {
        "id": _nid(), "type": "timeseries", "title": title, "description": description, "datasource": _ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {"legend": legend, "tooltip": {"mode": "multi", "sort": "desc"}},
        "targets": [_target(i, e, legends[i]) for i, e in enumerate(exprs)],
    }
    if time_from:
        panel["timeFrom"] = time_from
    if repeat:
        panel["repeat"] = repeat
        panel["repeatDirection"] = "h"
        panel["maxPerRow"] = 3
    return panel


def stat(title, x, y, w, h, expr, unit, description, legend="{{nodename}}", steps=None, decimals=None,
         mappings=None):
    """One value per selected host, read at the end of the range."""
    defaults = {"unit": unit, "thresholds": {"mode": "absolute",
                                             "steps": steps or [{"color": "blue", "value": None}]}}
    if decimals is not None:
        defaults["decimals"] = decimals
    if mappings:
        defaults["mappings"] = mappings
    return {
        "id": _nid(), "type": "stat", "title": title, "description": description, "datasource": _ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "none", "textMode": "value_and_name",
                    "justifyMode": "auto", "orientation": "vertical", "wideLayout": True,
                    # fixed sizes: auto-sizing wrapped "ai-node2" under a red value in one tile and not the next
                    "text": {"titleSize": 16, "valueSize": 24}},
        "targets": [_target(0, expr, legend, instant=True)],
    }


def bars(title, x, y, w, h, expr, unit, description, legend="{{name}}", repeat=None):
    """Horizontal bars, one per series, in the query's order (sort_desc), for an instant query."""
    panel = {
        "id": _nid(), "type": "bargauge", "title": title, "description": description, "datasource": _ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit, "min": 0, "decimals": 1,
                                     "color": {"mode": "fixed", "fixedColor": "blue"},
                                     "thresholds": {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}},
                        "overrides": []},
        "options": {"displayMode": "basic", "orientation": "horizontal", "showUnfilled": True,
                    "valueMode": "text", "namePlacement": "left", "sizing": "auto",
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
        "targets": [_target(0, expr, legend, instant=True)],
    }
    if repeat:
        panel["repeat"] = repeat
        panel["repeatDirection"] = "h"
        panel["maxPerRow"] = 3
    return panel


def _ov(name, props):
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def table(title, x, y, w, h, targets, rename, description, overrides=None, order=None, sort=None,
          exclude=("Time",)):
    """Several instant queries with the SAME label set, merged into one row per label combination
    (Grafana's merge transformation); each query's value becomes a column ("Value #<refId>"). With ONE
    query Grafana names that column "Value", so "Value #A" is translated here."""
    if len(targets) == 1:
        fix = lambda n: "Value" if n == "Value #A" else n  # noqa: E731
        rename = {fix(k): v for k, v in rename.items()}
        order = [fix(n) for n in order or []]
        sort = (fix(sort[0]), sort[1]) if sort else None
        exclude = tuple(fix(n) for n in exclude)
    transformations = [{"id": "merge", "options": {}}]
    if sort:
        transformations.append({"id": "sortBy", "options": {"sort": [{"field": sort[0], "desc": sort[1]}]}})
    transformations.append({"id": "organize", "options": {
        "excludeByName": {k: True for k in exclude}, "renameByName": rename,
        "indexByName": {n: i for i, n in enumerate(order or [])}}})
    return {
        "id": _nid(), "type": "table", "title": title, "description": description, "datasource": _ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "filterable": True,
                                                "cellOptions": {"type": "auto"}}},
                        "overrides": overrides or []},
        "options": {"showHeader": True, "footer": {"show": False}, "cellHeight": "sm"},
        "transformations": transformations,
        "targets": [_target(i, e, instant=True, table=True) for i, e in enumerate(targets)],
    }


# ───────────────────────── the dashboard ─────────────────────────
def build():
    global _pid
    _pid = 0
    p = []
    y = 0

    # ── Overview ──
    p.append(row("Overview (per host; values at the end of the range)", y))
    y += 1
    used, days_left = USED, DAYS_LEFT
    p += [
        stat("Life used (rated endurance)", 0, y, 4, 5, on_host(f"{used} * 100"), "percent",
             "SMART percentage_used: the drive's own estimate of its rated write endurance consumed. 100% is "
             "the rating, not a failure; the risk of the drive going read-only rises past it. AiNodeNvmeWearHigh "
             "fires at 95% (ADR 0042: pause that node's CI runners).", steps=WEAR_STEPS, decimals=0),
        stat("Written · last 24 h", 4, y, 4, 5, on_host(written_bytes("1d")),
             "decbytes", "Bytes the host wrote to the drive in the last 24 h: the raw difference of the drive's "
             "own counter (data units written x 512,000 B) now and 24 h ago, not an extrapolated increase() "
             "(that only across a drive replacement). This is what wears it. TB here are decimal, as in the "
             "drive's endurance rating.", decimals=1),
        stat("Written per day · 7-day average", 8, y, 4, 5,
             on_host(written_bytes("7d", per_day=7)), "decbytes",
             "The same counter's raw difference over 7 days, divided by 7.", decimals=1),
        stat("Days to 100% at the 7-day rate", 12, y, 4, 5, on_host(days_left), "suffix: d",
             "Days until life used reaches 100% if the drive keeps being written at its 7-day average: "
             "remaining % x (bytes written per 1% so far) / bytes per day. No figure until the drive reports 1% "
             "used (a new drive). The health table has the date. A "
             "projection, not a failure date.",
             steps=[{"color": "red", "value": None}, {"color": "orange", "value": 30},
                    {"color": "green", "value": 90}], decimals=0),
        stat("Thin pool used", 16, y, 4, 5,
             f"100 * {pool('pve_disk_usage_bytes')} / {pool('pve_disk_size_bytes')}", "percent",
             "local-lvm (pve/data) data space allocated, from pve-exporter. A full pool fails every guest's "
             "writes on the host; #1886 alerts at 85% and 95%. The Capacity row has the GiB and the guests.",
             steps=POOL_STEPS, decimals=1),
        stat("Critical warning · media errors", 20, y, 4, 5,
             on_host(f"nvme_critical_warning{{{HOSTS},{NVME}}} + nvme_media_errors_total{{{HOSTS},{NVME}}}"),
             "none", "0 = OK. Otherwise the drive's critical-warning byte (spare below threshold, temperature, "
             "reliability degraded, read-only, backup failed) is set or it has reported unrecovered media "
             "errors: the Endurance row's health table says which.",
             steps=[{"color": "green", "value": None}, {"color": "red", "value": 1}], decimals=0,
             mappings=[{"type": "value", "options": {"0": {"text": "OK", "color": "green", "index": 0}}}]),
    ]
    y += 5

    # ── Throughput ──
    p.append(row("Throughput at the drive (nvme0n1; bytes/s and IOPS on separate panels)", y))
    y += 1
    tput = ("Everything the host sends to the drive: every guest, the host's root and swap, and the thin pool's "
            "metadata. Rate over Grafana's $__rate_interval (at least 4 scrapes).")
    p += [
        ts("Write throughput", 0, y, 6, 8, [on_host(drive("node_disk_written_bytes_total"))], "binBps",
           ["{{nodename}}"], tput),
        ts("Read throughput", 6, y, 6, 8, [on_host(drive("node_disk_read_bytes_total"))], "binBps",
           ["{{nodename}}"], tput),
        ts("Write IOPS", 12, y, 6, 8, [on_host(drive("node_disk_writes_completed_total"))], "iops",
           ["{{nodename}}"], tput + " Completed write requests per second (after the block layer merged them)."),
        ts("Read IOPS", 18, y, 6, 8, [on_host(drive("node_disk_reads_completed_total"))], "iops",
           ["{{nodename}}"], tput + " Completed read requests per second."),
    ]
    y += 8

    # ── Latency, queue, pressure, etcd ──
    p.append(row("Latency, queue and pressure, next to the etcd member on the same host", y))
    y += 1
    p += [
        ts("Average I/O latency (read / write)", 0, y, 8, 8,
           [on_host(ratio("node_disk_write_time_seconds_total", "node_disk_writes_completed_total")),
            on_host(ratio("node_disk_read_time_seconds_total", "node_disk_reads_completed_total"))],
           "s", ["{{nodename}} write", "{{nodename}} read"],
           "Time per completed request, queueing included, averaged over the rate window: a seconds-long "
           "device stall shows as a spike to tens or hundreds of ms here. node1's worn drive runs at tens of "
           "ms per write under CI load where node3's runs at about 1 ms."),
        ts("Flush latency", 8, y, 8, 8,
           [on_host(ratio("node_disk_flush_requests_time_seconds_total", "node_disk_flush_requests_total"))],
           "s", ["{{nodename}}"],
           "Time per cache flush at the drive. Every guest fsync (etcd's WAL first) becomes one or more "
           "flushes; this drive has no power-loss protection, so a flush must reach NAND."),
        ts("etcd p99: WAL fsync / backend commit", 16, y, 8, 8,
           [etcd_p99("etcd_disk_wal_fsync_duration_seconds_bucket"),
            etcd_p99("etcd_disk_backend_commit_duration_seconds_bucket")],
           "s", ["{{name}} WAL fsync ({{nodename}})", "{{name}} backend commit ({{nodename}})"],
           "The etcd member whose VM runs on the selected host. Over ~100 ms the member falls behind; at "
           "seconds the cluster loses quorum for writes. Read it at the same instant as the drive's latency "
           "and flush panels (shared crosshair). talos-cp1's disk is on the QNAP since 2026-11-20, so its "
           "numbers no longer reflect ai-node1's drive.", thresholds=[{"color": "green", "value": None},
                                                                     {"color": "orange", "value": 0.1},
                                                                     {"color": "red", "value": 1}]),
    ]
    y += 8
    p += [
        ts("Queue depth (average requests in flight)", 0, y, 6, 7,
           [on_host(drive("node_disk_io_time_weighted_seconds_total"))], "short", ["{{nodename}}"],
           "rate(io_time_weighted): the average number of requests in flight. It rises with load AND with "
           "latency; a deep queue at low throughput is the device stalling."),
        ts("Busy % (time with I/O in flight)", 6, y, 6, 7,
           [on_host(drive("node_disk_io_time_seconds_total") + " * 100")], "percent", ["{{nodename}}"],
           "Share of time at least one request was in flight. An NVMe serves many requests at once, so 100% "
           "is NOT saturation; read it with latency and queue depth.", maxv=100, minv=0),
        ts("IO pressure (PSI, host)", 12, y, 6, 7,
           [on_host(f"rate(node_pressure_io_waiting_seconds_total{{{HOSTS}}}[{RI}]) * 100"),
            on_host(f"rate(node_pressure_io_stalled_seconds_total{{{HOSTS}}}[{RI}]) * 100")],
           "percent", ["{{nodename}} some", "{{nodename}} full"],
           "Share of wall time some (at least one) / all non-idle tasks on the host were stalled waiting for "
           "I/O. Host-wide: it includes the QNAP's NFS/iSCSI, not only this drive.", minv=0),
        ts("Flushes per second", 18, y, 6, 7, [on_host(drive("node_disk_flush_requests_total"))], "ops",
           ["{{nodename}}"],
           "Cache flushes the drive completed per second. Fell from ~600/s to ~25/s on node1 when the CI "
           "runner and reviewer disks moved to cache=unsafe (ADR 0042, 2026-11-19)."),
    ]
    y += 7

    # ── Who uses it ──
    p.append(row("Who uses the drive (per guest; host device-mapper stats)", y))
    y += 1
    who = ("Per consumer, from the host's device-mapper stats: each guest's thin volumes summed (VMs and "
           "LXCs alike; what actually reaches the pool, after the host page cache), plus the host's own "
           "root/swap and the thin pool's metadata. Together they add up to the drive's total. The $topn "
           "with the highest average over the selected range; pick a host to read one drive.")
    p += [
        ts("Top writers (bytes/s)", 0, y, 12, 9,
           [top(consumers('node_disk_written_bytes_total'), range_total('node_disk_written_bytes_total'))], "binBps", ["{{name}} ({{nodename}})"],
           who, calcs=["mean", "max"]),
        ts("Top readers (bytes/s)", 12, y, 12, 9,
           [top(consumers('node_disk_read_bytes_total'), range_total('node_disk_read_bytes_total'))], "binBps", ["{{name}} ({{nodename}})"],
           who, calcs=["mean", "max"]),
    ]
    y += 9
    p += [
        ts("Top writers (IOPS)", 0, y, 12, 8,
           [top(consumers('node_disk_writes_completed_total'), range_total('node_disk_writes_completed_total'))], "iops",
           ["{{name}} ({{nodename}})"], who, calcs=["mean", "max"]),
        ts("Top readers (IOPS)", 12, y, 12, 8,
           [top(consumers('node_disk_reads_completed_total'), range_total('node_disk_reads_completed_total'))], "iops",
           ["{{name}} ({{nodename}})"], who, calcs=["mean", "max"]),
    ]
    y += 8
    per_s = " / $__range_s"
    lat = (f"{range_total('node_disk_write_time_seconds_total')} / "
           f"({range_total('node_disk_writes_completed_total')} > 0)")
    p.append(table(
        "Per guest over the selected range (averages; sorted by bytes written)", 0, y, 24, 11,
        [range_total("node_disk_written_bytes_total"),
         range_total("node_disk_written_bytes_total") + per_s,
         range_total("node_disk_read_bytes_total") + per_s,
         range_total("node_disk_writes_completed_total") + per_s,
         range_total("node_disk_reads_completed_total") + per_s,
         lat,
         range_total("node_disk_discarded_sectors_total", scale=" * 512")],
        rename={"name": "Consumer", "type": "Type", "nodename": "Host", "vmid": "VMID",
                "Value #A": "Written", "Value #B": "Write B/s", "Value #C": "Read B/s",
                "Value #D": "Write IOPS", "Value #E": "Read IOPS", "Value #F": "Write latency",
                "Value #G": "Discarded"},
        description=who.split(" The $topn")[0] + " Write latency: time per write request at the guest's thin "
        "volume, waiting for the drive included. For the disks on cache=unsafe (CI runners, reviewers; ADR 0042) "
        "the writes arrive as host writeback in large batches, so their time per request is mostly queueing "
        "behind the batch, not what the guest itself waits for.",
        order=["name", "type", "nodename", "vmid", "Value #A", "Value #B", "Value #C", "Value #D", "Value #E",
               "Value #F", "Value #G"],
        sort=("Value #A", True),
        overrides=[_ov("Type", [{"id": "mappings", "value": TYPE_MAP}, {"id": "custom.width", "value": 70}]),
                   _ov("VMID", [{"id": "custom.width", "value": 70}]),
                   _ov("Host", [{"id": "custom.width", "value": 90}]),
                   _ov("Written", [{"id": "unit", "value": "decbytes"}, {"id": "decimals", "value": 1}]),
                   _ov("Discarded", [{"id": "unit", "value": "decbytes"}, {"id": "decimals", "value": 1}]),
                   _ov("Write B/s", [{"id": "unit", "value": "binBps"}, {"id": "decimals", "value": 1}]),
                   _ov("Read B/s", [{"id": "unit", "value": "binBps"}, {"id": "decimals", "value": 1}]),
                   _ov("Write IOPS", [{"id": "unit", "value": "iops"}, {"id": "decimals", "value": 1}]),
                   _ov("Read IOPS", [{"id": "unit", "value": "iops"}, {"id": "decimals", "value": 1}]),
                   _ov("Write latency", [{"id": "unit", "value": "s"}, {"id": "decimals", "value": 1}])]))
    y += 11

    # ── Capacity ──
    p.append(row("Capacity: the local-lvm thin pool and what each guest holds in it", y))
    y += 1
    used_b = pool("pve_disk_usage_bytes")
    p.append(ts("Thin pool on $host: used / free (GiB)", 0, y, 8, 8,
                [used_b, f"{pool('pve_disk_size_bytes')} - {used_b}"], "bytes", ["used", "free"],
                "local-lvm's data space, stacked: used (allocated to volumes) under free, so the top line is the "
                "pool's size (1,755 GiB). From pve-exporter (60 s).", fill=40, stack=True, minv=0, repeat="host",
                calcs=["lastNotNull"],
                overrides=[_ov("free", [{"id": "color", "value": {"mode": "fixed", "fixedColor": "green"}}]),
                           _ov("used", [{"id": "color", "value": {"mode": "fixed", "fixedColor": "blue"}}])]))
    y += 8
    p += [
        ts("Thin pool used % (data and metadata)", 0, y, 12, 8,
           [f"100 * {used_b} / {pool('pve_disk_size_bytes')}",
            on_host(f"pve_thin_pool_metadata_percent{{{HOSTS},pool=\"pve/data\"}}")],
           "percent", ["{{nodename}} data", "{{nodename}} metadata"],
           "Data: allocated share of the pool (pve-exporter). Metadata: the pool's mapping table (#1886, "
           "pve_thin_pool_metadata_percent; No data until its collector runs); full metadata switches the pool "
           "read-only. Dashed: #1886's warning thresholds (data 85%, metadata 80%).",
           maxv=100, minv=0, thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 80},
                                          {"color": "red", "value": 85}], calcs=["lastNotNull"]),
        ts("Allocated per guest (top $topn, GiB)", 12, y, 12, 8,
           [top(thin_by_guest('pve_thin_lv_allocated_bytes'), thin_by_guest('pve_thin_lv_allocated_bytes'))], "bytes", ["{{name}} ({{nodename}})"],
           "Bytes each guest's thin volumes hold in the pool, over time: which guest fills it. Source: #1886's "
           "pve_thin_lv_allocated_bytes (No data until its collector runs).", calcs=["lastNotNull"]),
    ]
    y += 8
    gap_desc = ("Untrimmed gap = allocated in the pool minus what the guest's own filesystems use (node_exporter "
                "in the guest: size - free). Mostly blocks the guest freed and never discarded, which a trim "
                "gives back; it also holds filesystem overhead and anything outside the guest's filesystems "
                "(Talos's boot partitions, ~1 GiB). Only guests running node_exporter (not the registry). Needs "
                "#1886.")
    p.append(table(
        "Per guest: virtual size, allocated, filesystem used, untrimmed gap", 0, y, 24, 11,
        [thin_by_guest("pve_thin_lv_size_bytes"),
         thin_by_guest("pve_thin_lv_allocated_bytes"),
         f"100 * {thin_by_guest('pve_thin_lv_allocated_bytes')} / on(nodename, name, type, vmid) "
         f"{thin_by_guest('pve_thin_lv_size_bytes')}",
         f"max by (nodename, name, type, vmid) ({guest_fs_used()})",
         untrimmed_gap(),
         f"100 * {thin_by_guest('pve_thin_lv_allocated_bytes')} / on(nodename) group_left() "
         f"max by (nodename) ({pool('pve_disk_size_bytes')})"],
        rename={"name": "Guest", "type": "Type", "nodename": "Host", "vmid": "VMID",
                "Value #A": "Virtual size", "Value #B": "Allocated", "Value #C": "Allocated %",
                "Value #D": "Filesystem used", "Value #E": "Untrimmed gap", "Value #F": "Share of pool"},
        description="Per guest (all its disks summed; cloud-init drives left out), from #1886's "
                    "pve_thin_lv_* (No data until its collector runs). " + gap_desc,
        order=["name", "type", "nodename", "vmid", "Value #A", "Value #B", "Value #C", "Value #D", "Value #E",
               "Value #F"],
        sort=("Value #B", True),
        overrides=[_ov("Type", [{"id": "mappings", "value": TYPE_MAP}, {"id": "custom.width", "value": 70}]),
                   _ov("VMID", [{"id": "custom.width", "value": 70}]),
                   _ov("Host", [{"id": "custom.width", "value": 90}])]
        + [_ov(c, [{"id": "unit", "value": "bytes"}, {"id": "decimals", "value": 1}])
           for c in ("Virtual size", "Allocated", "Filesystem used", "Untrimmed gap")]
        + [_ov(c, [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1}])
           for c in ("Allocated %", "Share of pool")]))
    y += 11
    p.append(bars("Allocated per guest on $host", 0, y, 8, 10,
                  f"sort_desc({thin_by_guest('pve_thin_lv_allocated_bytes')})", "bytes",
                  "Bytes each guest holds in $host's thin pool (#1886). Repeated per selected host.",
                  repeat="host"))
    y += 10
    p.append(bars("Untrimmed gap per guest on $host", 0, y, 8, 10, f"sort_desc({untrimmed_gap()})", "bytes",
                  gap_desc + " Repeated per selected host.", repeat="host"))
    y += 10

    # ── Endurance & health ──
    p.append(row("Endurance, health and temperature (the drive's SMART log; 14-day panels)", y))
    y += 1
    sensor = f'node_hwmon_temp_celsius{{{HOSTS},chip="nvme_nvme0"}}'
    sensor = (f'label_replace(label_replace(label_replace(label_replace({sensor}, '
              f'"sensor", "composite", "sensor", "temp1"), "sensor", "sensor 1 (controller)", "sensor", "temp2"), '
              f'"sensor", "sensor 2", "sensor", "temp3"), "sensor", "sensor 3", "sensor", "temp4")')
    p += [
        ts("Life used (%)", 0, y, 8, 9, [on_host(f"{used} * 100")], "percent", ["{{nodename}}"],
           "SMART percentage_used over the last 14 days (whatever the dashboard range, when the range is "
           "relative). It moves in whole percent. Dashed: 70 / 85 / 95 (AiNodeNvmeWearHigh).",
           thresholds=WEAR_STEPS, time_from="14d", decimals=0, calcs=["lastNotNull"]),
        ts("Written per day: last 24 h and 7-day average", 8, y, 8, 9,
           [on_host(written_bytes("1d")), on_host(written_bytes("7d", per_day=7))], "decbytes",
           ["{{nodename}} last 24 h", "{{nodename}} 7-day avg"],
           "At each point, bytes written in the 24 h (solid) and the 7 days / 7 (dashed) before it: raw "
           "differences of the drive's data-units-written counter (x 512,000 B), not increase() (that only "
           "across a drive replacement). The counter "
           "is refreshed every 15 min. Decimal TB, as in the endurance rating.", time_from="14d",
           calcs=["lastNotNull"],
           overrides=[{"matcher": {"id": "byRegexp", "options": ".* 7-day avg"},
                       "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}]}]),
        ts("Drive temperatures", 16, y, 8, 9, [on_host(sensor)], "celsius", ["{{nodename}} {{sensor}}"],
           "hwmon on the NVMe (30 s): the composite temperature (what the drive's own throttling and its "
           "86 / 90 C warning / critical thresholds, dashed, apply to) and its three sensors; sensor 1 is the "
           "controller, the hottest. ProxmoxNvmeTemperatureHigh fires on it above 95 C for 15m.",
           thresholds=TEMP_LIMITS, decimals=0),
    ]
    y += 9

    def health(expr):
        return f"max by (nodename) ({on_host(expr)})"
    sel = f"{{{HOSTS},{NVME}}}"
    width = lambda w: {"id": "custom.width", "value": w}  # noqa: E731  fixed widths: 12 columns fit unscrolled
    red_from_1 = [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                  {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                      {"color": "green", "value": None}, {"color": "red", "value": 1}]}}]
    p.append(table(
        "Drive identity and health (SMART)", 0, y, 24, 5,
        [f"max by (nodename, model, firmware, serial) ({on_host('nvme_device_info' + sel)})",
         health(f"nvme_percentage_used_ratio{sel} * 100"),
         health(f"(time() + 86400 * {days_left}) * 1000"),
         health(f"nvme_available_spare_ratio{sel} * 100"),
         health(f"nvme_media_errors_total{sel}"),
         health(f"nvme_critical_warning{sel}"),
         health(f"nvme_num_err_log_entries_total{sel}"),
         health(f"nvme_unsafe_shutdowns_total{sel}"),
         health(f"nvme_power_on_hours_total{sel} * 3600"),
         health(f"nvme_data_units_written_total{sel} * {DATA_UNIT}")],
        rename={"nodename": "Host", "model": "Model", "firmware": "Firmware", "serial": "Serial",
                "Value #B": "Life used", "Value #C": "100% at 7-day rate", "Value #D": "Spare",
                "Value #E": "Media errors", "Value #F": "Critical warning", "Value #G": "Error log",
                "Value #H": "Unsafe shutdowns", "Value #I": "Powered on", "Value #J": "Written (life)"},
        description="From the hosts' nvme textfile collector (Debian prometheus-node-exporter-collectors, every "
                    "15 min). 100% at 7-day rate: the date life used would reach 100% at the last 7 days' "
                    "write rate (the overview's days). Spare: replacement blocks left (the drive sets a critical "
                    "warning below its 5% threshold). Media errors: unrecovered data errors (any is bad). "
                    "Critical warning: a bit field, 0 = none. Error log: entries in the drive's error log. "
                    "Unsafe shutdowns: power lost without a clean shutdown; with no power-loss protection, each "
                    "can lose writes the drive had acknowledged.",
        exclude=("Time", "Value #A"),
        order=["nodename", "model", "firmware", "serial", "Value #B", "Value #C", "Value #D", "Value #E",
               "Value #F", "Value #G", "Value #H", "Value #I", "Value #J"],
        sort=("nodename", False),
        overrides=[_ov("Host", [width(90)]), _ov("Firmware", [width(90)]), _ov("Serial", [width(170)]),
                   _ov("Life used", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 0}, width(100),
                                     {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                     {"id": "thresholds", "value": {"mode": "absolute", "steps": WEAR_STEPS}}]),
                   _ov("100% at 7-day rate", [{"id": "unit", "value": "dateTimeAsIsoNoDateIfToday"}, width(165)]),
                   _ov("Spare", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 0}, width(80)]),
                   _ov("Media errors", red_from_1 + [width(125)]),
                   _ov("Critical warning", red_from_1 + [width(145)]),
                   _ov("Error log", [width(95)]),
                   _ov("Unsafe shutdowns", [width(155)]),
                   _ov("Powered on", [{"id": "unit", "value": "s"}, {"id": "decimals", "value": 0}, width(115)]),
                   _ov("Written (life)", [{"id": "unit", "value": "decbytes"}, {"id": "decimals", "value": 1},
                                          width(125)])]))
    y += 5

    # ── Trim ──
    p.append(row("Trim: discards reaching the pool, and each trim job's last success", y))
    y += 1
    p += [
        ts("Discards per consumer (bytes/s)", 0, y, 12, 9,
           [top(consumers('node_disk_discarded_sectors_total', scale=' * 512'),
                range_total('node_disk_discarded_sectors_total', scale=' * 512'), only_active=True)], "binBps",
           ["{{name}} ({{nodename}})"],
           "Bytes discarded (trimmed) per second at each guest's thin volumes: what a guest trim actually "
           "handed back to the pool, whoever ran it (the nightly jobs, an online-discard mount, a hand-run "
           "fstrim). Discards are cheap for the pool but are still device commands on the etcd drive.",
           calcs=["max"]),
        table("Trim jobs: time since the last success", 12, y, 12, 9, [trim_ages()],
              rename={"name": "Guest", "trim": "Trim job", "nodename": "Host", "Value #A": "Since last success"},
              description="Seconds since each trim's last success: CI runners' nightly slice (its last RUN: each "
                          "run trims one slice), dev workers' daily trim (#1885), the hosts' nightly LXC sweep "
                          "(#1883; the oldest volume's last complete pass) and the Talos node-fstrim CronJobs "
                          "(#1884). Rows appear as each of those ships. Orange past 26 h, red past 50 h (the "
                          "trim alerts' threshold).",
              order=["name", "trim", "nodename", "Value #A"], sort=("Value #A", True),
              overrides=[_ov("Since last success", [
                  {"id": "unit", "value": "s"}, {"id": "decimals", "value": 0},
                  {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                  {"id": "thresholds", "value": {"mode": "absolute", "steps": TRIM_AGE_STEPS}}])]),
    ]
    y += 9
    return {
        "title": "Local Drives (Proxmox hosts' NVMe)",
        "uid": UID,
        "description": "The Proxmox hosts' local NVMe drives: throughput, latency, who uses them, the thin pool, "
                       "endurance and trims. Generated by scripts/gen-local-drives-dashboard.py.",
        "tags": ["infrastructure", "ailab", "storage", "nvme", "proxmox"],
        "timezone": "browser",
        "schemaVersion": 39,
        "graphTooltip": 1,                     # shared crosshair: read latency, etcd and the writers at one instant
        "refresh": "1m",
        "time": {"from": "now-24h", "to": "now"},
        "links": [{"title": "AI Lab Fleet", "type": "link", "icon": "dashboard", "targetBlank": False,
                   "url": "/d/ailab-reporting/"}],
        "templating": {"list": [
            {"name": "DS_PROMETHEUS", "type": "datasource", "query": "prometheus",
             "current": {}, "hide": 0, "label": "Datasource", "refresh": 1},
            {"name": "host", "type": "query", "label": "Host", "datasource": _ds(),
             "query": {"query": f'label_values(node_uname_info{{{HOSTS}}}, nodename)', "refId": "host"},
             "definition": f'label_values(node_uname_info{{{HOSTS}}}, nodename)',
             "refresh": 1, "multi": True, "includeAll": True, "sort": 1,
             "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "hide": 0},
            {"name": "topn", "type": "custom", "label": "Top N", "query": "5,8,10,15,25",
             "current": {"selected": True, "text": "8", "value": "8"}, "hide": 0, "multi": False,
             "includeAll": False,
             "options": [{"selected": v == "8", "text": v, "value": v} for v in ("5", "8", "10", "15", "25")]},
        ]},
        "panels": p,
    }


def configmap(dashboard):
    return {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "local-drives-dashboard", "namespace": "monitoring",
                     "labels": {"grafana_dashboard": "1"}},
        "data": {"local-drives.json": json.dumps(dashboard, indent=2)},
    }


def render():
    return json.dumps(configmap(build()), indent=2) + "\n"


if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8", newline="\n")
    d = build()
    print(f"wrote {OUT} ({len([x for x in d['panels'] if x['type'] != 'row'])} panels, "
          f"{len([x for x in d['panels'] if x['type'] == 'row'])} rows)")
