#!/usr/bin/env python3
"""env-pool soak report — read-only evidence for the V6 soak of the testpool env node(s).

plans/2026-09-20-env-pool-root-cause-followup-plan.md (T3). Queries Prometheus and Loki over an
explicit UTC window (or from the last checkpoint, with overlap), exports the raw series/lines it
used, and prints ONE markdown block for the plan's soak record with a verdict:

  OK                     no NotReady, no Testpool*/EnvNode* alert, no reap, no watchdog closure,
                         no replacement, no restart, no reboot
  RECURRENCE-CONTAINED   warm capacity (the SandboxWarmPool's OWN readyReplicas < replicas) was
                         lost and back within RECOVERY_BOUND_SECONDS, and/or a stall/rotation
                         signal (watchdog closure, reap, member replacement, restart, reboot) was
                         seen, with the node Ready and no firing alert — the prevention worked
  UNRESOLVED             a capacity-loss incident lasted longer than the bound or is still open at
                         the window's end — not yet contained (incidents are derived from the
                         capacity series itself, so an outage needs no "event" to be seen)
  PREVENTION-FAILED      an env node went NotReady, or ANY Testpool*/EnvNode* alert FIRED
                         (TeardownStuck, NoWarmCapacity, OperatorDown, PVC/PV, EnvNode*)
  INCOMPLETE             a query failed, an expected node/series is missing, a Loki page could not
                         be exhausted, the relay/reaper streams have gaps, or the window reaches
                         past retention — never reported as OK

Decision table (total order): PREVENTION-FAILED > UNRESOLVED > INCOMPLETE > RECURRENCE-CONTAINED
> OK. Pending-only alerts are reported but never decide. Every verdict also prints the list of
incompleteness problems, so a known failure with missing evidence reads "PREVENTION-FAILED +
incomplete", never one or the other. A failed endpoint can never yield OK.

Nothing here writes to the cluster. Endpoints are the LAN NodePorts (monitoring/prometheus-lan.yaml
:30090, monitoring/loki-lan.yaml :30310). The exact queries are listed in docs/runbooks/env-pool.md
so any number in the report can be reproduced by hand.

Usage (from the MAIN checkout so the export lands under the gitignored _out/):
  python scripts/env-pool-soak.py --from 2026-09-22T10:00:00Z --to 2026-09-23T10:00:00Z
  python scripts/env-pool-soak.py --checkpoint kubernetes/infra/_out/soak/checkpoint.json
Tests: python3 -m unittest scripts.tests.test_env_pool_soak  (CI: .gitea/workflows/manifests.yaml)
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PROM_DEFAULT = "http://192.168.0.41:30090"
LOKI_DEFAULT = "http://192.168.0.41:30310"
STEP_SECONDS = 60
LOKI_PAGE = 5000
LOKI_MAX_PAGES = 40
LOKI_RETENTION_HOURS = 168  # monitoring/loki.yaml limits_config.retention_period
PROM_RETENTION_DAYS = 12  # retentionSize: 36GB binds before retention: 15d (kube-prometheus-stack.yaml)
HEARTBEAT_GAP_SECONDS = 15 * 60  # reaper heartbeat every ~10 min; a 15 min gap = the reaper was silent
RELAY_GAP_SECONDS = 15 * 60  # at [debug] level the shim/agent chatter is continuous; silence = capture gap
RECOVERY_BOUND_SECONDS = 10 * 60  # closure → GC → reap (~150 s) → refill (~2 min): capacity back well inside 10 min
EVENT_ATTACH_SECONDS = 5 * 60  # a signal (closure, reap, …) belongs to a capacity incident starting within ±5 min
MIN_COVERAGE = 0.8  # a required series must have ≥ 80 % of the window's steps and touch both boundaries
SOURCE_TS_MIN_FRACTION = 0.5  # relay lines must mostly carry their own containerd time= field
REQUIRED_SERIES = ("node_ready", "kubelet_up", "boot_time", "member_age", "warm_ready", "warm_spec", "restarts")
ALERT_RE = "Testpool.*|EnvNode.*"

# Every query the report runs, by name — reproduced verbatim in docs/runbooks/env-pool.md.
# Range queries are evaluated every STEP_SECONDS; the readiness/alert series are aggregated over
# each step with min_over_time/max_over_time so a NotReady or firing sample shorter than a step (the
# scrape interval is 30 s) is never stepped over, and stale samples are not read as fresh.
PROM_RANGE_QUERIES = {
    "node_ready": 'min_over_time(kube_node_status_condition{node=~"talos-env-node-.*",condition="Ready",status="true"}[1m])',
    "kubelet_up": 'min_over_time(up{job="kubelet",metrics_path="/metrics",node=~"talos-env-node-.*"}[1m])',
    "boot_time": 'node_boot_time_seconds{instance=~"192.168.0.3[78]:9100"}',
    "member_age": '(time() - kube_pod_created{namespace="testpool"}) * on(namespace,pod) group_left() '
    'kube_pod_info{namespace="testpool",created_by_kind="Sandbox"}',
    # Warm capacity = the SandboxWarmPool's own accounting (kube-state-metrics customResourceState,
    # monitoring/kube-prometheus-stack.yaml). Pods of LEASED sandboxes are Ready too, so
    # kube_pod_status_ready cannot stand in for this: a healthy lease would mask an empty pool.
    "warm_ready": 'min_over_time(agentsandbox_warmpool_ready_replicas{exported_namespace="testpool"}[1m])',
    "warm_spec": 'max_over_time(agentsandbox_warmpool_spec_replicas{exported_namespace="testpool"}[1m])',
    "restarts": 'kube_pod_container_status_restarts_total{namespace="testpool"}',
    "alerts": f'max_over_time(ALERTS{{alertname=~"{ALERT_RE}"}}[1m])',
}
PROM_INSTANT_QUERIES = {
    "stop_errors": 'sum by (node,operation_type) (increase(kubelet_runtime_operations_errors_total'
    '{node=~"talos-env-node-.*",operation_type=~"stop_.*"}[{window}]))',
}
LOKI_QUERIES = {
    "reaper": '{namespace="kube-system", app="env-reaper"} |~ "reap stage=|evidence|api error:|heartbeat iter="',
    "watchdog": '{namespace="testpool", container="control"} |~ "check hung|check failed|ready-port closed|checks recovered"',
    "relay": '{namespace="monitoring", app="cri-log-relay"} |~ "vmconsole|kata-agent|level=debug"',
}


class SourceError(Exception):
    """An endpoint or query failed; the report records it and the verdict can never be OK."""


class Source:
    """HTTP access to Prometheus + Loki. Tests replace this with canned responses."""

    def __init__(self, prom: str, loki: str, timeout: float = 30.0):
        self.prom = prom.rstrip("/")
        self.loki = loki.rstrip("/")
        self.timeout = timeout

    def _get(self, url: str):
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                return json.load(r)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SourceError(f"{url.split('?')[0]}: {e}") from e

    def prom_range(self, expr: str, start: float, end: float, step: int = STEP_SECONDS) -> list:
        q = urllib.parse.urlencode({"query": expr, "start": start, "end": end, "step": step})
        d = self._get(f"{self.prom}/api/v1/query_range?{q}")
        if d.get("status") != "success":
            raise SourceError(f"prometheus query_range {expr!r}: {d.get('error')}")
        return d["data"]["result"]

    def prom_instant(self, expr: str, at: float) -> list:
        q = urllib.parse.urlencode({"query": expr, "time": at})
        d = self._get(f"{self.prom}/api/v1/query?{q}")
        if d.get("status") != "success":
            raise SourceError(f"prometheus query {expr!r}: {d.get('error')}")
        return d["data"]["result"]

    def loki_range(self, expr: str, start_ns: int, end_ns: int) -> tuple[list[tuple[int, str]], bool]:
        """All (ts_ns, line) in [start, end], newest page first, walked back by timestamp.
        Loki's `end` is EXCLUSIVE, so the next page ends at oldest+1 to re-include the boundary
        timestamp: records sharing it across streams are not skipped and the already-seen ones are
        deduplicated as (ts, line) pairs. A page that makes no progress (every line at one
        timestamp) cannot be exhausted safely → truncated.
        Returns (lines, truncated) — truncated=True also when LOKI_MAX_PAGES was hit."""
        out: set[tuple[int, str]] = set()
        end = end_ns
        for _ in range(LOKI_MAX_PAGES):
            q = urllib.parse.urlencode({"query": expr, "start": start_ns, "end": end, "limit": LOKI_PAGE, "direction": "backward"})
            d = self._get(f"{self.loki}/loki/api/v1/query_range?{q}")
            if d.get("status") != "success":
                raise SourceError(f"loki query_range {expr!r}: {d.get('error')}")
            page = [(int(v[0]), v[1]) for s in d["data"]["result"] for v in s["values"]]
            before = len(out)
            out.update(page)
            if len(page) < LOKI_PAGE:
                return sorted(out), False
            oldest = min(t for t, _ in page)
            if oldest + 1 >= end or len(out) == before:
                return sorted(out), True  # no progress possible: a timestamp alone fills a page
            end = oldest + 1  # exclusive end → the boundary timestamp is queried again
            if end <= start_ns:
                return sorted(out), False
        return sorted(out), True


@dataclasses.dataclass
class Report:
    start: float
    end: float
    verdict: str = "OK"
    problems: list[str] = dataclasses.field(default_factory=list)  # → INCOMPLETE
    failures: list[str] = dataclasses.field(default_factory=list)  # → PREVENTION-FAILED
    unresolved: list[str] = dataclasses.field(default_factory=list)  # → UNRESOLVED
    recurrences: list[str] = dataclasses.field(default_factory=list)  # → RECURRENCE-CONTAINED
    sections: list[str] = dataclasses.field(default_factory=list)
    evidence: list[str] = dataclasses.field(default_factory=list)
    exports: dict[str, object] = dataclasses.field(default_factory=dict)

    def finish(self) -> "Report":
        if self.failures:
            self.verdict = "PREVENTION-FAILED"
        elif self.unresolved:
            self.verdict = "UNRESOLVED"
        elif self.problems:
            self.verdict = "INCOMPLETE"
        elif self.recurrences:
            self.verdict = "RECURRENCE-CONTAINED"
        else:
            self.verdict = "OK"
        return self

    def markdown(self) -> str:
        lines = [
            f"### Soak check-in — {iso(self.start)} → {iso(self.end)} — **{self.verdict}**",
            "",
        ]
        if self.failures:
            lines += ["**Prevention failed:**"] + [f"- {f}" for f in self.failures] + [""]
        if self.unresolved:
            lines += ["**Unresolved (capacity not back within the bound):**"] + [f"- {u}" for u in self.unresolved] + [""]
        if self.problems:
            lines += ["**Incomplete data:**"] + [f"- {p}" for p in self.problems] + [""]
        if self.recurrences:
            lines += ["**Recurrences (contained):**"] + [f"- {r}" for r in self.recurrences] + [""]
        lines += self.sections
        if self.evidence:
            lines += ["", "**Raw evidence lines (redact before committing):**", "```"] + self.evidence[:200] + ["```"]
        return "\n".join(lines).rstrip() + "\n"


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str) -> float:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def fmt_dur(seconds: float) -> str:
    seconds = int(seconds)
    h, m = divmod(seconds // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{seconds % 60:02d}s"


def intervals_where(values: list[tuple[float, float]], predicate, step: int = STEP_SECONDS) -> list[tuple[float, float]]:
    """[(start, end)] runs of consecutive samples satisfying predicate."""
    runs: list[tuple[float, float]] = []
    cur = None
    for t, v in values:
        if predicate(v):
            cur = (t, t) if cur is None else (cur[0], t)
        elif cur is not None:
            runs.append(cur)
            cur = None
    if cur is not None:
        runs.append(cur)
    return [(a, b + step) for a, b in runs]


def sample_gaps(values: list[tuple[float, float]], step: int = STEP_SECONDS, factor: int = 3) -> list[tuple[float, float]]:
    gaps = []
    for (t0, _), (t1, _) in zip(values, values[1:]):
        if t1 - t0 > step * factor:
            gaps.append((t0, t1))
    return gaps


def coverage_problem(name: str, vals: list[tuple[float, float]], start: float, end: float, step: int = STEP_SECONDS) -> str | None:
    """Why a required series does not cover the window (None = it does)."""
    expected = max(1, int((end - start) / step))
    if not vals:
        return f"{name}: no samples in the window"
    if vals[0][0] > start + 2 * step:
        return f"{name}: first sample at {iso(vals[0][0])}, {fmt_dur(vals[0][0] - start)} after the window start"
    if vals[-1][0] < end - 2 * step:
        return f"{name}: last sample at {iso(vals[-1][0])}, {fmt_dur(end - vals[-1][0])} before the window end"
    if len(vals) < MIN_COVERAGE * expected:
        return f"{name}: only {len(vals)} of ~{expected} samples"
    return None


def should_advance_checkpoint(rep: "Report") -> bool:
    """The checkpoint moves only over a window whose data is complete and holds no open incident —
    otherwise the next run (last `to` minus the overlap) could skip the unavailable stretch or the
    open incident's trigger."""
    return not rep.problems and not rep.unresolved


_SRC_TS = re.compile(r'(?:"time":"|\btime=")(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))"')


def source_ts(line: str) -> float | None:
    """The containerd record's OWN timestamp (JSON `"time":"…"` or logfmt `time="…"`), which is the
    event time — Loki's ingestion timestamp is when the RELAY emitted it, and a relay reconnect
    replays the node's whole ring with fresh ingestion timestamps."""
    m = _SRC_TS.search(line)
    if not m:
        return None
    try:
        return parse_ts(m.group(1))
    except ValueError:
        return None


def _series(result: list) -> dict[tuple, list[tuple[float, float]]]:
    out = {}
    for s in result:
        key = tuple(sorted(s["metric"].items()))
        out[key] = [(float(t), float(v)) for t, v in s.get("values", [])]
    return out


def _label(key: tuple, name: str) -> str:
    return dict(key).get(name, "?")


def run(src: Source, start: float, end: float, nodes: list[str], now: float | None = None) -> Report:
    now = now or dt.datetime.now(dt.timezone.utc).timestamp()
    rep = Report(start=start, end=end)
    window = f"{max(60, int(end - start))}s"

    if now - start > LOKI_RETENTION_HOURS * 3600:
        rep.problems.append(f"window starts {fmt_dur(now - start)} ago, past Loki's {LOKI_RETENTION_HOURS} h retention — log evidence before that point is gone")
    if now - start > PROM_RETENTION_DAYS * 86400:
        rep.problems.append(f"window starts more than ~{PROM_RETENTION_DAYS} d ago — Prometheus history may be cut by retentionSize")

    prom: dict[str, dict] = {}
    for name, expr in PROM_RANGE_QUERIES.items():
        try:
            prom[name] = _series(src.prom_range(expr, start, end))
        except SourceError as e:
            rep.problems.append(f"prometheus {name}: {e}")
            prom[name] = {}
    stop_errors = []
    try:
        stop_errors = src.prom_instant(PROM_INSTANT_QUERIES["stop_errors"].replace("{window}", window), end)
    except SourceError as e:
        rep.problems.append(f"prometheus stop_errors: {e}")
    rep.exports["prometheus"] = {n: {json.dumps(dict(k)): v for k, v in s.items()} for n, s in prom.items()}
    rep.exports["prometheus"]["stop_errors"] = stop_errors

    # --- required telemetry: present, covering the window, without interior gaps ----------------
    for name in REQUIRED_SERIES:
        if not prom[name]:
            rep.problems.append(f"no {name} series in the window — absence of observations proves nothing")
            continue
        for key, vals in prom[name].items():
            who = f"{name} {_label(key, 'node') if 'node' in dict(key) else _label(key, 'pod') if 'pod' in dict(key) else _label(key, 'name')}".strip()
            cov = coverage_problem(who, vals, start, end)
            if cov and name not in ("member_age", "restarts"):  # pods legitimately start/end inside the window
                rep.problems.append(cov)
            for a, b in sample_gaps(vals):
                if name in ("member_age", "restarts") and (b - a) < 6 * STEP_SECONDS:
                    continue
                rep.problems.append(f"{who}: no samples {iso(a)} → {iso(b)} ({fmt_dur(b - a)})")

    # --- nodes --------------------------------------------------------------------------------
    rep.sections.append("| Node | Ready samples | NotReady intervals | kubelet /metrics down | boots seen |")
    rep.sections.append("|---|---|---|---|---|")
    seen_nodes = {_label(k, "node") for k in prom["node_ready"]}
    for n in nodes:
        if n not in seen_nodes:
            rep.problems.append(f"no kube_node_status_condition series for expected node {n}")
        if n not in {_label(k, "node") for k in prom["kubelet_up"]}:
            rep.problems.append(f"{n}: no kubelet /metrics target series (up{{job=\"kubelet\"}}) in the window")
    boot_vals = sorted({int(v) for vals in prom["boot_time"].values() for _, v in vals})
    for key, vals in sorted(prom["node_ready"].items()):
        node = _label(key, "node")
        notready = intervals_where(vals, lambda v: v < 1)
        for a, b in notready:
            rep.failures.append(f"{node} NotReady {iso(a)} → {iso(b)} ({fmt_dur(b - a)})")
        kdown = []
        for k2, v2 in prom["kubelet_up"].items():
            if _label(k2, "node") == node:
                kdown = intervals_where(v2, lambda v: v < 1)
        rep.sections.append(
            f"| {node} | {len(vals)} | {len(notready)} | "
            + (", ".join(f"{iso(a)}→{iso(b)}" for a, b in kdown) or "none")
            + f" | {len(boot_vals)} ({', '.join(iso(b) for b in boot_vals)}) |"
        )

    # --- members (Sandbox-owned pods: warm AND leased) -----------------------------------------
    rep.sections += ["", "| Sandbox pod | first seen | last seen | max age reached | restarts |", "|---|---|---|---|---|"]
    members = {}
    for key, vals in prom["member_age"].items():
        pod = _label(key, "pod")
        if vals:
            members[pod] = {"first": vals[0][0], "last": vals[-1][0], "max_age": max(v for _, v in vals)}
    restarts: dict[str, float] = {}
    restart_events: list[tuple[float, str]] = []
    for key, vals in prom["restarts"].items():
        pod = _label(key, "pod")
        if vals:
            restarts[pod] = restarts.get(pod, 0) + vals[-1][1] - vals[0][1]
            for (t0, v0), (t1, v1) in zip(vals, vals[1:]):
                if v1 > v0:
                    restart_events.append((t1, f"restart of {pod}/{_label(key, 'container')}"))
    for pod, m in sorted(members.items(), key=lambda kv: kv[1]["first"]):
        rep.sections.append(f"| {pod} | {iso(m['first'])} | {iso(m['last'])} | {fmt_dur(m['max_age'])} | {int(restarts.get(pod, 0))} |")
    ended = [p for p, m in members.items() if m["last"] < end - 2 * STEP_SECONDS]

    # --- alerts ---------------------------------------------------------------------------------
    rep.sections += ["", "| Alert | state | intervals |", "|---|---|---|"]
    for key, vals in sorted(prom["alerts"].items()):
        name, state = _label(key, "alertname"), _label(key, "alertstate")
        runs = intervals_where(vals, lambda v: v >= 1)
        rep.sections.append(f"| {name} | {state} | " + ", ".join(f"{iso(a)}→{iso(b)}" for a, b in runs) + " |")
        if state == "firing" and runs:
            rep.failures.append(f"alert {name} FIRING " + ", ".join(f"{iso(a)}→{iso(b)}" for a, b in runs))
    if not prom["alerts"]:
        rep.sections.append("| (none) | | |")

    # --- runtime stop errors ------------------------------------------------------------------
    rep.sections += ["", f"Kubelet `stop_*` runtime errors over the window ({window}):"]
    for se in stop_errors:
        rep.sections.append(f"- {se['metric'].get('node')} {se['metric'].get('operation_type')}: {float(se['value'][1]):.0f}")
    if not stop_errors:
        rep.sections.append("- none")

    # --- loki ------------------------------------------------------------------------------------
    start_ns, end_ns = int(start * 1e9), int(end * 1e9)
    loki: dict[str, list[tuple[int, str]]] = {}
    for name, expr in LOKI_QUERIES.items():
        try:
            lines, truncated = src.loki_range(expr, start_ns, end_ns)
        except SourceError as e:
            rep.problems.append(f"loki {name}: {e}")
            lines, truncated = [], False
        if truncated:
            rep.problems.append(f"loki {name}: more than {LOKI_MAX_PAGES} pages of {LOKI_PAGE} lines, or a page that cannot be exhausted — export truncated")
        loki[name] = lines
    rep.exports["loki"] = {n: [(t, l) for t, l in v] for n, v in loki.items()}

    reap_re = re.compile(r"(^|\s)reap stage=1 .*?sandbox=(\S+) .*?pid=(\d+)")
    evid_re = re.compile(r"(^|\s)evidence stage=1 .*?sandbox=(\S+) .*?pid=(\d+) state=")
    reaps = [(t, m.group(2), m.group(3)) for t, l in loki["reaper"] for m in [reap_re.search(l)] if m]
    evid_ok = {(m.group(2), m.group(3)) for _, l in loki["reaper"] for m in [evid_re.search(l)] if m}
    evid_incomplete = [l for _, l in loki["reaper"] if re.search(r"(^|\s)evidence stage=1 .* incomplete", l)]
    api_err = [l for _, l in loki["reaper"] if "api error:" in l]
    beats = [t for t, l in loki["reaper"] if "heartbeat iter=" in l]
    closures = [(t, l) for t, l in loki["watchdog"] if "ready-port closed" in l or "check hung" in l or "check failed" in l]
    recovered = [l for _, l in loki["watchdog"] if "checks recovered" in l]

    # relay: dedupe on the containerd record's OWN time + content; freshness/gaps on that time
    relay_raw = loki["relay"]
    relay_src: dict[tuple[float, str], None] = {}
    unparsed = 0
    for t, l in relay_raw:
        st = source_ts(l)
        if st is None:
            unparsed += 1
            st = t / 1e9
        relay_src.setdefault((st, l), None)
    relay = sorted(relay_src)
    if relay_raw and unparsed > (1 - SOURCE_TS_MIN_FRACTION) * len(relay_raw):
        rep.problems.append(f"{unparsed} of {len(relay_raw)} relay lines carry no containerd time= field — replay cannot be told from fresh capture")
    sandboxes = sorted({sb for _, sb, _ in reaps})
    relay_per_sb = {sb: sum(1 for _, l in relay if sb in l) for sb in sandboxes}

    rep.sections += [
        "",
        "| Stream | lines |",
        "|---|---|",
        f"| reaper `reap stage=1` kills | {len(reaps)} |",
        f"| reaper complete `evidence` (sandbox,pid) | {len(evid_ok)} |",
        f"| reaper incomplete `evidence` | {len(evid_incomplete)} |",
        f"| reaper `api error` | {len(api_err)} |",
        f"| reaper heartbeats | {len(beats)} |",
        f"| watchdog closures (`check hung`/`check failed`/`ready-port closed`) | {len(closures)} |",
        f"| watchdog `checks recovered` | {len(recovered)} |",
        f"| relay records (deduplicated on source time + content; {len(relay_raw)} raw) | {len(relay)} |",
    ]
    if sandboxes:
        rep.sections.append("| relay records per reaped sandbox | " + ", ".join(f"{sb[:12]}..={n}" for sb, n in relay_per_sb.items()) + " |")
    for pod in members:
        n = sum(1 for _, l in relay if pod in l)
        if n:
            rep.sections.append(f"| relay records naming {pod} | {n} |")
    for t, sb, pid in reaps:
        if (sb, pid) not in evid_ok:
            rep.problems.append(f"reap of sandbox {sb[:12]}.. pid {pid} at {iso(t / 1e9)} has no complete evidence line (T2d not deployed, timed out, or unreadable)")
        if relay_per_sb.get(sb, 0) == 0:
            rep.problems.append(f"reaped sandbox {sb[:12]}.. has no relay (shim/agent/console) records — host-log capture gap")
    if not beats:
        rep.problems.append("no reaper heartbeat in the window — reaper not running, or its log stream is not in Loki")
    else:
        beats_s = sorted(t / 1e9 for t in beats)
        for a, b in zip([start] + beats_s, beats_s + [end]):
            if b - a > HEARTBEAT_GAP_SECONDS:
                rep.problems.append(f"reaper heartbeat gap {iso(a)} → {iso(b)} ({fmt_dur(b - a)})")
    if not relay:
        rep.problems.append("no cri-log-relay records in the window — durable host-log capture is not shipping")
    else:
        relay_s = [t for t, _ in relay]
        for a, b in zip([start] + relay_s, relay_s + [end]):
            if b - a > RELAY_GAP_SECONDS:
                rep.problems.append(f"relay capture gap {iso(a)} → {iso(b)} ({fmt_dur(b - a)}) by source time — host-log evidence for that stretch does not exist")

    # --- capacity incidents (from the warm pool's own accounting) ------------------------------
    ready = next(iter(prom["warm_ready"].values()), [])
    spec = next(iter(prom["warm_spec"].values()), [])
    spec_at = {t: v for t, v in spec}
    cap = [(t, v - spec_at.get(t, max((s for _, s in spec), default=1))) for t, v in ready]  # < 0 = capacity missing
    incidents = intervals_where(cap, lambda d: d < 0)
    open_at_end = bool(cap) and cap[-1][1] < 0
    rep.sections += ["", "Warm-capacity incidents (SandboxWarmPool readyReplicas < replicas):"]
    if not incidents:
        rep.sections.append("- none")
    for k, (a, b) in enumerate(incidents):
        is_last = k == len(incidents) - 1
        if is_last and open_at_end:
            rep.unresolved.append(f"capacity incident from {iso(a)} still open at the window end ({fmt_dur(end - a)} so far)")
        elif b - a > RECOVERY_BOUND_SECONDS:
            rep.unresolved.append(f"capacity incident {iso(a)} → {iso(b)} lasted {fmt_dur(b - a)} > {fmt_dur(RECOVERY_BOUND_SECONDS)} bound")
        else:
            rep.recurrences.append(f"capacity incident {iso(a)} → {iso(b)} ({fmt_dur(b - a)}), back inside the bound" + (" — underway at the window start" if a <= start + STEP_SECONDS else ""))
        rep.sections.append(f"- {iso(a)} → {iso(b)} ({fmt_dur(b - a)})" + (" OPEN" if is_last and open_at_end else ""))

    # --- signals: attach to incidents (±5 min), otherwise transient ----------------------------
    signals: list[tuple[float, str]] = []
    signals += [(t / 1e9, "watchdog closure") for t, _ in closures]
    signals += [(t / 1e9, f"reap of {sb[:12]}..") for t, sb, _ in reaps]
    signals += [(members[p]["last"], f"pod {p} gone") for p in ended]
    signals += [(b, "node reboot") for b in boot_vals[1:]]
    signals += restart_events
    for st, what in sorted(signals):
        inc = next(((a, b) for a, b in incidents if abs(a - st) <= EVENT_ATTACH_SECONDS or a <= st <= b), None)
        if inc:
            rep.sections.append(f"- signal {what} at {iso(st)} → capacity incident {iso(inc[0])}→{iso(inc[1])}")
        elif what.startswith("pod ") and what.endswith(" gone"):
            rep.sections.append(f"- {what} at {iso(st)} with no capacity loss — lease turnover, not a warm-member failure")
        else:
            rep.recurrences.append(f"{what} at {iso(st)} with no capacity loss observed (transient)")
    if signals and not cap:
        rep.problems.append("signals in the window but no warm-capacity series to prove recovery")
    rep.evidence += [l for _, l in loki["reaper"] if "heartbeat" not in l][:100]
    rep.evidence += [l for _, l in closures][:50] + recovered[:20]
    return rep.finish()

def export(rep: Report, export_dir: pathlib.Path) -> None:
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / "prometheus.json").write_text(json.dumps(rep.exports.get("prometheus", {}), indent=1), encoding="utf-8")
    for name, lines in rep.exports.get("loki", {}).items():
        with (export_dir / f"loki-{name}.log").open("w", encoding="utf-8") as f:
            for t, l in lines:
                f.write(f"{iso(t / 1e9)} {l}\n")
    (export_dir / "report.md").write_text(rep.markdown(), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", help="window start, UTC ISO (default: checkpoint - overlap, or --to - 24h)")
    ap.add_argument("--to", dest="end", help="window end, UTC ISO (default: now)")
    ap.add_argument("--checkpoint", type=pathlib.Path, help="JSON file holding the last successful `to`; read for --from, updated on success")
    ap.add_argument("--overlap-hours", type=float, default=1.0)
    ap.add_argument("--nodes", default="talos-env-node-1", help="comma-separated env nodes that MUST have readiness series")
    ap.add_argument("--prom", default=PROM_DEFAULT)
    ap.add_argument("--loki", default=LOKI_DEFAULT)
    ap.add_argument("--export-dir", type=pathlib.Path, help="default kubernetes/infra/_out/soak/<from>_<to> (gitignored)")
    args = ap.parse_args(argv)

    now = dt.datetime.now(dt.timezone.utc).timestamp()
    end = parse_ts(args.end) if args.end else now
    if args.start:
        start = parse_ts(args.start)
    elif args.checkpoint and args.checkpoint.exists():
        start = parse_ts(json.loads(args.checkpoint.read_text())["to"]) - args.overlap_hours * 3600
    else:
        start = end - 24 * 3600
    if start >= end:
        ap.error("--from must be before --to")

    rep = run(Source(args.prom, args.loki), start, end, [n for n in args.nodes.split(",") if n], now=now)
    export_dir = args.export_dir or pathlib.Path("kubernetes/infra/_out/soak") / f"{iso(start)}_{iso(end)}".replace(":", "")
    export(rep, export_dir)
    rep.sections.append("")
    rep.sections.append(f"Raw export: `{export_dir}` (gitignored; keep for the retention window)")
    sys.stdout.write(rep.markdown())
    if args.checkpoint and should_advance_checkpoint(rep):
        args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        args.checkpoint.write_text(json.dumps({"to": iso(end), "verdict": rep.verdict}), encoding="utf-8")
    elif args.checkpoint:
        sys.stderr.write("checkpoint NOT advanced: the window has incomplete data (see problems); re-run it after the gap is understood\n")
    return 0 if rep.verdict in ("OK", "RECURRENCE-CONTAINED") else 1  # UNRESOLVED/INCOMPLETE/FAILED are non-zero


if __name__ == "__main__":
    sys.exit(main())
