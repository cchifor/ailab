#!/usr/bin/env python3
# MANAGED BY ANSIBLE (role: gitea_runner). Job-boundary leftover reaper for the host-mode act_runner.
# Called by gitea-runner-reclaim.sh on every timer tick, BEFORE its busy gate, and never from
# ExecStartPre (--no-reap). Plan: plans/2026-10-12-runner-between-job-cleanup-plan.md (PR 2).
"""Find, and in `enforce` mode remove, what earlier jobs left behind on this runner.

act_runner 0.6.1 (host mode) has no per-job hook. The ONLY job boundary it emits is its own journal line
`task <N> repo is <owner>/<repo> ...`, logged as a task starts. With capacity 1 the previous task has
finished by then, so anything that was CREATED before the newest task line, and is not part of the job
that line started, is a leftover. "Not part of the job" is decided by creation time, never by process
ancestry: a live kartra-ai job's Playwright browsers on cloud-ci-10 were NOT descendants of act_runner's
MainPID (their parent had exited), so "kill non-descendants" alone would kill a running job.

Boundary E for a pass is max(B, now - (job_timeout + grace)), where B is the newest task-start time
minus a margin. No job can outlive its timeout, so E never reaches into a live job, even when B is
missing or stale (an act_runner log-format change, a rotated journal). There is no other fallback.

  * Edge pass: once per new task line (a job start), with that E.
  * Periodic pass: every GITEA_REAP_PERIODIC_SEC, with the timeout term ONLY. It never uses B, so an
    object a long job took over by name (compose `up` keeping a leftover container) is never hit mid-job.

Modes: off | log | enforce. `log` (the default) only reports `would-reap ...` and runs the adoption probe
that gates the switch to enforce. Everything external is injectable through env vars for the tests
(tests/test-reap.py); the defaults are the real host paths and binaries.
"""
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time

# ---------------------------------------------------------------------------------------------- config
def load_env_file(path):
    conf = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                conf[k.strip()] = v
    except OSError:
        pass
    return conf


CONF = load_env_file(os.environ.get("GITEA_RECLAIM_ENV_FILE", "/etc/gitea-runner-reclaim.env"))


def cfg(name, default):
    """Env var beats the env file beats the default (the tests set env vars only)."""
    return os.environ.get(name, CONF.get(name, default))


SERVICE = cfg("GITEA_RUNNER_SERVICE", "gitea-act-runner.service")
WORKDIR_PARENT = cfg("GITEA_RUNNER_WORKDIR_PARENT", "/home/runner/act-runner/work").rstrip("/")
CONFIG_YAML = os.path.join(cfg("GITEA_RUNNER_CONFIG_DIR", "/home/runner/act-runner"), "config.yaml")
JOB_TIMEOUT = cfg("GITEA_RUNNER_JOB_TIMEOUT", "3h")
MODE = cfg("GITEA_REAP_MODE", "log")
MARGIN_SEC = float(cfg("GITEA_REAP_MARGIN_SEC", "5"))
GRACE_SEC = float(cfg("GITEA_REAP_GRACE_SEC", "600"))
PERIODIC_SEC = float(cfg("GITEA_REAP_PERIODIC_SEC", "900"))
PROBE_SEC = float(cfg("GITEA_REAP_PROBE_SEC", "30"))
MAX_PROCS = int(cfg("GITEA_REAP_MAX_PROCS", "64"))
TERM_WAIT_SEC = float(cfg("GITEA_REAP_TERM_WAIT_SEC", "10"))
KEEP_LABEL_RE = cfg("GITEA_REAP_KEEP_LABEL_RE", r"^(io\.dsh\.conductor\.image-pin|ci\.keep)$")
INFRA_EXCLUDE_RE = cfg("GITEA_CLEANUP_INFRA_EXCLUDE_RE", "buildkit|buildx")
PEER_SERVICES = cfg("GITEA_CLEANUP_PEER_SERVICES", "actions.runner.cchifor-platform.service")
STATE_PATH = cfg("GITEA_REAP_STATE", "/var/lib/gitea-runner-reclaim/reap.state")
BEACON = cfg("GITEA_RECLAIM_BEACON", "1") == "1"
TEXTFILE_DIR = cfg("GITEA_RECLAIM_TEXTFILE_DIR", "/var/lib/prometheus/node-exporter")
# injection points (tests)
PROC = cfg("GITEA_REAP_PROC_ROOT", "/proc")
CGROUP_ROOT = cfg("GITEA_REAP_CGROUP_ROOT", "/sys/fs/cgroup")
SYSTEMCTL = cfg("GITEA_REAP_SYSTEMCTL", "systemctl")
JOURNALCTL = cfg("GITEA_REAP_JOURNALCTL", "journalctl")
DOCKER = cfg("GITEA_REAP_DOCKER", "docker")
LOGGER = cfg("GITEA_REAP_LOGGER", "logger")
KILL_CMD = cfg("GITEA_REAP_KILL_CMD", "")
BOOT_ID_FILE = cfg("GITEA_REAP_BOOT_ID_FILE", "/proc/sys/kernel/random/boot_id")
NOW_OVERRIDE = cfg("GITEA_REAP_NOW", "")
CLK_TCK = int(cfg("GITEA_REAP_CLK_TCK", str(os.sysconf("SC_CLK_TCK"))))

# act_runner's own line, taken verbatim from cloud-ci-9's journal (2026-10-12):
#   time="2026-10-12T13:56:15+03:00" level=info msg="task 434665 repo is cchifor/platform https://github.com https://git.chifor.me"
TASK_RE = re.compile(
    r'^time="(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(Z|[+-]\d\d:\d\d)" level=info '
    r'msg="task (\d+) repo is ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+) ')
NEVER = float("-inf")
ERRORS = []


# ---------------------------------------------------------------------------------------------- helpers
def log(msg):
    try:
        subprocess.run([LOGGER, "-t", "gitea-runner-reap", "--", msg], timeout=10, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 - logging must never break the reaper
        pass


def run(argv, timeout=20):
    """Run a command; return stdout or None (and record the error) on any failure."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as e:  # noqa: BLE001
        ERRORS.append(f"{argv[0]} {argv[1] if len(argv) > 1 else ''}: {e}")
        return None
    if p.returncode != 0:
        ERRORS.append(f"{' '.join(argv[:3])}: rc={p.returncode} {p.stderr.strip()[:200]}")
        return None
    return p.stdout


def parse_duration(s):
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", str(s))
    if not m:
        raise ValueError(f"unparsable duration {s!r}")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def rfc3339(ts):
    """Docker timestamps (nanosecond precision) -> epoch float. Year-1 'never started' -> NEVER."""
    if not ts or ts.startswith("0001-01-01"):
        return NEVER
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)", ts.strip())
    if not m:
        return None
    base = datetime.datetime.fromisoformat(m.group(1) + ("+00:00" if m.group(3) == "Z" else m.group(3)))
    return base.timestamp() + (float("0" + m.group(2)) if m.group(2) else 0.0)


def read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def btime():
    for line in read(os.path.join(PROC, "stat")).splitlines():
        if line.startswith("btime "):
            return int(line.split()[1])
    raise RuntimeError("no btime in /proc/stat")


def proc_stat(pid):
    """(state, ppid, start_boot_seconds) or None if the process is gone. comm may hold spaces and ')'."""
    try:
        raw = read(os.path.join(PROC, str(pid), "stat"))
    except OSError:
        return None
    rest = raw[raw.rfind(")") + 2:].split()
    try:
        return rest[0], int(rest[1]), int(rest[19]) / CLK_TCK
    except (IndexError, ValueError):
        return None


def cmdline(pid):
    try:
        return read(os.path.join(PROC, str(pid), "cmdline")).replace("\0", " ").strip()[:120]
    except OSError:
        return "?"


# ---------------------------------------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = f"{STATE_PATH}.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------------------------------------- boundary
def capacity_is_one():
    try:
        m = re.search(r"^\s*capacity:\s*(\d+)\s*$", read(CONFIG_YAML), re.M)
    except OSError:
        return False
    return bool(m) and int(m.group(1)) == 1


def main_pid():
    out = run([SYSTEMCTL, "show", "-p", "MainPID", "--value", SERVICE])
    try:
        pid = int((out or "").strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def read_task_lines(st, mainpid, bt, boot_id, since_epoch):
    """Newest boot-relative task start (minus margin) among new act_runner journal entries, or None.
    Updates st['cursor']. Only act_runner's own stdout lines count: journald files `logger` output from
    any job process under the same unit, so _PID and _COMM are both matched, and the regex is anchored."""
    base = [JOURNALCTL, "-o", "json", "--no-pager", f"_SYSTEMD_UNIT={SERVICE}", f"_PID={mainpid}",
            "_COMM=act_runner"]
    out = None
    if st.get("cursor"):
        out = run(base + ["--after-cursor", st["cursor"]], timeout=20)
    if out is None:  # no cursor yet, or it was invalidated by vacuum/rotation: bounded re-read
        out = run(base + ["--since", f"@{int(since_epoch)}"], timeout=30)
    if out is None:
        return None
    newest = None
    for line in out.splitlines():
        try:
            ent = json.loads(line)
        except ValueError:
            continue
        if ent.get("__CURSOR"):
            st["cursor"] = ent["__CURSOR"]
        msg = ent.get("MESSAGE")
        if not isinstance(msg, str):
            continue
        m = TASK_RE.match(msg)
        if not m:
            continue
        tz = "+00:00" if m.group(2) == "Z" else m.group(2)
        logged = datetime.datetime.fromisoformat(m.group(1) + tz).timestamp()  # floored to the second
        estimates = [logged - bt]
        if ent.get("_BOOT_ID", "").replace("-", "") == boot_id and ent.get("__MONOTONIC_TIMESTAMP"):
            estimates.append(int(ent["__MONOTONIC_TIMESTAMP"]) / 1e6)  # journald RECEIVE time: may lag
        b = min(estimates) - MARGIN_SEC  # the earlier estimate is the safe one
        if newest is None or b > newest[0]:
            newest = (b, m.group(3), m.group(4))
    return newest


# ---------------------------------------------------------------------------------------------- objects
def cgroup_pids():
    root = os.path.join(CGROUP_ROOT, "system.slice", SERVICE)
    pids = set()
    for dirpath, _dirs, files in os.walk(root):
        if "cgroup.procs" in files:
            try:
                pids.update(int(x) for x in read(os.path.join(dirpath, "cgroup.procs")).split())
            except (OSError, ValueError):
                pass
    return pids


def is_descendant(pid, ancestor):
    seen = 0
    while pid and pid > 1 and seen < 4096:
        if pid == ancestor:
            return True
        s = proc_stat(pid)
        if not s:
            return False
        pid, seen = s[1], seen + 1
    return False


def process_candidates(mainpid, e_boot):
    out = []
    for pid in sorted(cgroup_pids()):
        if pid == mainpid:
            continue
        s = proc_stat(pid)
        if not s or s[0] == "Z" or s[2] >= e_boot or is_descendant(pid, mainpid):
            continue
        out.append((pid, s[2]))
    return out[:MAX_PROCS], len(out) > MAX_PROCS


def inspect(kind, ids):
    if not ids:
        return []
    argv = {"container": [DOCKER, "inspect"], "network": [DOCKER, "network", "inspect"],
            "volume": [DOCKER, "volume", "inspect"]}[kind] + ids
    out = run(argv, timeout=30)
    try:
        return json.loads(out) if out else []
    except ValueError:
        ERRORS.append(f"{kind} inspect: bad json")
        return []


def list_ids(kind):
    argv = {"container": [DOCKER, "ps", "-aq", "--no-trunc"], "network": [DOCKER, "network", "ls", "-q", "--no-trunc"],
            "volume": [DOCKER, "volume", "ls", "-q"]}[kind]
    out = run(argv, timeout=15)
    return out.split() if out else []


def keep_by_label(labels):
    return any(re.search(KEEP_LABEL_RE, k) for k in (labels or {}))


def under_workdir(path):
    if not path or ".." in path.split("/"):
        return False
    return path == WORKDIR_PARENT or path.startswith(WORKDIR_PARENT + "/")


def peers_none():
    return PEER_SERVICES.strip() == "none"


def attributable(c):
    if peers_none():
        return True
    labels = (c.get("Config") or {}).get("Labels") or {}
    if under_workdir(labels.get("com.docker.compose.project.working_dir", "")):
        return True
    return any(under_workdir(m.get("Source", "")) for m in c.get("Mounts") or [])


def container_view(c):
    labels = (c.get("Config") or {}).get("Labels") or {}
    return {
        "id": c.get("Id", ""), "name": (c.get("Name") or "").lstrip("/"), "image": (c.get("Config") or {}).get("Image", ""),
        "created": rfc3339(c.get("Created", "")), "started": rfc3339((c.get("State") or {}).get("StartedAt", "")),
        "labels": labels, "project": labels.get("com.docker.compose.project"),
        "networks": list(((c.get("NetworkSettings") or {}).get("Networks") or {}).keys()),
        "volumes": [m.get("Name") for m in c.get("Mounts") or [] if m.get("Type") == "volume" and m.get("Name")],
        "raw": c,
    }


def docker_candidates(e_epoch):
    containers = [container_view(c) for c in inspect("container", list_ids("container"))]
    cand_c = []
    for c in containers:
        if c["created"] is None or c["started"] is None:
            continue  # unparsable timestamp: never guess
        if not (c["created"] < e_epoch and c["started"] < e_epoch):
            continue  # created or (re)started by the current job, or brought back by a restart policy
        if re.search(INFRA_EXCLUDE_RE, f"{c['image']} /{c['name']}", re.I) or keep_by_label(c["labels"]):
            continue
        if not attributable(c["raw"]):
            continue
        cand_c.append(c)
    cand_ids = {c["id"] for c in cand_c}
    survivors = [c for c in containers if c["id"] not in cand_ids]  # references AFTER the container pass
    used_nets = {n for c in survivors for n in c["networks"]}
    used_vols = {v for c in survivors for v in c["volumes"]}
    cand_n, cand_v = [], []
    for n in inspect("network", list_ids("network")):
        labels = n.get("Labels") or {}
        created = rfc3339(n.get("Created", ""))
        if n.get("Name") in ("bridge", "host", "none") or "com.docker.compose.project" not in labels:
            continue
        if created is None or created >= e_epoch or keep_by_label(labels) or n.get("Name") in used_nets:
            continue
        if n.get("Containers"):
            attached = set(n["Containers"]) - cand_ids
            if attached:
                continue
        cand_n.append({"id": n.get("Id", ""), "name": n.get("Name", ""), "created": created,
                       "project": labels.get("com.docker.compose.project")})
    for v in inspect("volume", list_ids("volume")):
        labels = v.get("Labels") or {}
        created = rfc3339(v.get("CreatedAt", ""))
        if "com.docker.compose.project" not in labels and "com.docker.volume.anonymous" not in labels:
            continue  # named, non-compose volumes may be deliberate caches
        if created is None or created >= e_epoch or keep_by_label(labels) or v.get("Name") in used_vols:
            continue
        cand_v.append({"id": v.get("Name", ""), "name": v.get("Name", ""), "created": created,
                       "project": labels.get("com.docker.compose.project")})
    return containers, cand_c, cand_n, cand_v


# ---------------------------------------------------------------------------------------------- actions
def signal_pid(pid, sig):
    if KILL_CMD:  # tests: every signal goes through this indirection, never os.kill
        subprocess.run([KILL_CMD, str(int(sig)), str(pid)], timeout=10, check=False)
        return
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def reap_processes(cands, now_boot):
    """SIGTERM, one shared wait, SIGKILL. Start time is re-read before EACH signal (PID reuse)."""
    live = []
    for pid, start in cands:
        s = proc_stat(pid)
        if s and abs(s[2] - start) < 0.5 and s[0] != "Z":
            log(f"reap process pid={pid} age={int(now_boot - start)}s cmd={cmdline(pid)!r}")
            signal_pid(pid, signal.SIGTERM)
            live.append((pid, start))
    deadline = time.monotonic() + TERM_WAIT_SEC
    while live and time.monotonic() < deadline:
        time.sleep(0.5)
        live = [(p, st) for p, st in live if (s := proc_stat(p)) and abs(s[2] - st) < 0.5 and s[0] != "Z"]
    for pid, start in live:
        s = proc_stat(pid)
        if s and abs(s[2] - start) < 0.5 and s[0] != "Z":
            log(f"reap process pid={pid} still alive after {TERM_WAIT_SEC:.0f}s -> SIGKILL")
            signal_pid(pid, signal.SIGKILL)
    return len(cands)


def docker_rm(kind, ids):
    if not ids:
        return 0
    argv = {"container": [DOCKER, "rm", "-f"], "network": [DOCKER, "network", "rm"],
            "volume": [DOCKER, "volume", "rm"]}[kind] + ids
    run(argv, timeout=60)
    return len(ids)


# ---------------------------------------------------------------------------------------------- adoption
def adoption_probe(st, containers):
    """Gate for enforce. A would-reap object counts as ADOPTED when the job running after its boundary
    used it. Inspect-based, not `docker events`: dockerd only keeps a short in-memory event ring for
    --since, and compose `up -d` reusing an unchanged running container emits no event at all.
    Detected:
      - a container created after the boundary joined a would-reap network or mounted a would-reap volume;
      - a container created after the boundary carries the compose project of a would-reap object;
      - a would-reap container was (re)started after its boundary.
    Not detected (documented limit): traffic to a leftover process or an untouched running container
    from outside compose. Process reaping stays behind the same enforce gate."""
    hits = 0
    for w in st.get("would", []):
        if w.get("adopted"):
            continue
        b = w["b_epoch"]
        newer = [c for c in containers if c["created"] is not None and c["created"] >= b]
        reason = None
        if w["kind"] == "network" and any(w["name"] in c["networks"] for c in newer):
            reason = "a newer container joined it"
        elif w["kind"] == "volume" and any(w["name"] in c["volumes"] for c in newer):
            reason = "a newer container mounts it"
        elif w.get("project") and any(c["project"] == w["project"] for c in newer):
            reason = f"compose project {w['project']} has a newer container"
        elif w["kind"] == "container":
            cur = next((c for c in containers if c["id"] == w["id"]), None)
            if cur and cur["started"] is not None and cur["started"] >= b:
                reason = "it was (re)started after the boundary"
        if reason:
            w["adopted"] = True
            hits += 1
            log(f"ADOPTED would-reap {w['kind']} {w['name'] or w['id'][:12]}: {reason}")
    st["adopted_total"] = st.get("adopted_total", 0) + hits
    return hits


# ---------------------------------------------------------------------------------------------- beacon
def write_beacon(m):
    if not BEACON or not os.path.isdir(TEXTFILE_DIR):
        return
    lines = []
    def g(name, help_, samples):
        lines.append(f"# HELP gitea_runner_reap_{name} {help_}")
        lines.append(f"# TYPE gitea_runner_reap_{name} gauge")
        lines.extend(samples)
    g("last_run_seconds", "Unix time the job-boundary reaper last ran.", [f"gitea_runner_reap_last_run_seconds {int(m['now'])}"])
    g("mode", "0=off 1=log 2=enforce.", [f"gitea_runner_reap_mode {m['mode']}"])
    g("refused", "1 if the reaper refused to act (capacity != 1, or no act_runner MainPID).", [f"gitea_runner_reap_refused {m['refused']}"])
    g("boundary_age_seconds", "Seconds since the newest parsed task start (-1 = none parsed yet).",
      [f"gitea_runner_reap_boundary_age_seconds {m['boundary_age']}"])
    g("last_candidates", "Leftovers found by the last edge/periodic pass, by kind.",
      [f'gitea_runner_reap_last_candidates{{kind="{k}"}} {v}' for k, v in sorted(m["candidates"].items())])
    g("last_reaped", "Leftovers removed by the last pass, by kind (always 0 in log mode).",
      [f'gitea_runner_reap_last_reaped{{kind="{k}"}} {v}' for k, v in sorted(m["reaped"].items())])
    g("adopted_total", "Would-reap objects a later job used (blocks enforce).", [f"gitea_runner_reap_adopted_total {m['adopted']}"])
    g("errors", "Failed external commands in the last run.", [f"gitea_runner_reap_errors {len(ERRORS)}"])
    tmp = os.path.join(TEXTFILE_DIR, f"gitea_runner_reap.prom.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(TEXTFILE_DIR, "gitea_runner_reap.prom"))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------- main
def main():
    mode_num = {"off": 0, "log": 1, "enforce": 2}.get(MODE)
    if mode_num is None:
        log(f"refuse: unknown GITEA_REAP_MODE={MODE!r}")
        return 0
    now = float(NOW_OVERRIDE) if NOW_OVERRIDE else time.time()
    metrics = {"now": now, "mode": mode_num, "refused": 0, "boundary_age": -1, "adopted": 0,
               "candidates": {k: 0 for k in ("process", "container", "network", "volume")},
               "reaped": {k: 0 for k in ("process", "container", "network", "volume")}}
    if mode_num == 0:
        write_beacon(metrics)
        return 0
    st = load_state()
    try:
        bt = btime()
        boot_id = read(BOOT_ID_FILE).strip().replace("-", "")
        timeout_sec = parse_duration(JOB_TIMEOUT)
    except (OSError, RuntimeError, ValueError) as e:
        log(f"refuse: cannot read the clock basis ({e})")
        metrics["refused"] = 1
        write_beacon(metrics)
        return 0
    if st.get("boot_id") != boot_id:
        st = {"boot_id": boot_id, "would": [], "adopted_total": st.get("adopted_total", 0)}
    mainpid = main_pid()
    if not capacity_is_one() or mainpid is None:
        log(f"refuse: capacity_is_one={capacity_is_one()} mainpid={mainpid}")
        metrics["refused"] = 1
        metrics["adopted"] = st.get("adopted_total", 0)
        write_beacon(metrics)
        save_state(st)
        return 0
    now_boot = now - bt
    floor_boot = now_boot - (timeout_sec + GRACE_SEC)
    newest = read_task_lines(st, mainpid, bt, boot_id, now - timeout_sec - GRACE_SEC)
    if newest and (st.get("b_boot") is None or newest[0] > st["b_boot"]):
        st["b_boot"], st["b_task"], st["b_repo"] = newest
    b_boot = st.get("b_boot")
    if b_boot is not None:
        metrics["boundary_age"] = int(max(0, now_boot - b_boot))

    # At most one pass per tick. An edge pass's boundary is max(B, floor) >= floor, so it finds everything
    # a periodic pass would, and it also counts as this tick's periodic pass.
    passes = []
    if b_boot is not None and (st.get("last_edge_b") is None or b_boot > st["last_edge_b"] + 0.5):
        passes.append(("edge", max(b_boot, floor_boot)))
    elif now - st.get("last_periodic", 0) >= PERIODIC_SEC:
        passes.append(("periodic", floor_boot))

    containers_seen = None
    for kind_of_pass, e_boot in passes:
        if kind_of_pass == "edge":
            again = read_task_lines(st, mainpid, bt, boot_id, now - timeout_sec - GRACE_SEC)  # fresh-start guard
            if again and again[0] > b_boot:
                st["b_boot"], st["b_task"], st["b_repo"] = again
                b_boot = again[0]
                e_boot = max(b_boot, floor_boot)
        e_epoch = bt + e_boot
        procs, truncated = process_candidates(mainpid, e_boot)
        containers_seen, cand_c, cand_n, cand_v = docker_candidates(e_epoch)
        tag = f"{kind_of_pass} boundary_age={int(now_boot - e_boot)}s task={st.get('b_task', '-')}"
        counts = {"process": len(procs), "container": len(cand_c), "network": len(cand_n), "volume": len(cand_v)}
        for k, v in counts.items():
            metrics["candidates"][k] = max(metrics["candidates"][k], v)
        if MODE == "enforce":
            metrics["reaped"]["process"] += reap_processes(procs, now_boot)
            for c in cand_c:
                log(f"reap container {c['name']} ({c['image']}) {tag}")
            metrics["reaped"]["container"] += docker_rm("container", [c["id"] for c in cand_c])
            for n in cand_n:
                log(f"reap network {n['name']} {tag}")
            metrics["reaped"]["network"] += docker_rm("network", [n["id"] for n in cand_n])
            for v in cand_v:
                log(f"reap volume {v['name']} {tag}")
            metrics["reaped"]["volume"] += docker_rm("volume", [v["id"] for v in cand_v])
        else:
            for pid, start in procs:
                log(f"would-reap process pid={pid} age={int(now_boot - start)}s cmd={cmdline(pid)!r} {tag}")
            known = {(w["kind"], w["id"]) for w in st.get("would", [])}
            for kind, items in (("container", cand_c), ("network", cand_n), ("volume", cand_v)):
                for it in items:
                    log(f"would-reap {kind} {it['name'] or it['id'][:12]} project={it.get('project') or '-'} {tag}")
                    if (kind, it["id"]) not in known:
                        st.setdefault("would", []).append({"kind": kind, "id": it["id"], "name": it["name"],
                                                           "project": it.get("project"), "b_epoch": e_epoch,
                                                           "seen": now})
        if truncated:
            log(f"process cap {MAX_PROCS} hit on {kind_of_pass} pass; the rest go on the next pass")
        if kind_of_pass == "edge":
            st["last_edge_b"] = b_boot
        st["last_periodic"] = now

    # adoption probe: needs fresh container state; cheap enough every PROBE_SEC while entries are pending
    pending = [w for w in st.get("would", []) if not w.get("adopted") and now - w["seen"] < 86400]
    if MODE == "log" and pending and (containers_seen is not None or now - st.get("last_probe", 0) >= PROBE_SEC):
        if containers_seen is None:
            containers_seen = [container_view(c) for c in inspect("container", list_ids("container"))]
        adoption_probe(st, containers_seen)
        st["last_probe"] = now
    st["would"] = [w for w in st.get("would", []) if now - w["seen"] < 86400]
    metrics["adopted"] = st.get("adopted_total", 0)
    save_state(st)
    write_beacon(metrics)
    if ERRORS:
        log("errors: " + " | ".join(ERRORS[:5]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - best effort, like the reclaim script: never block the timer
        log(f"reaper crashed: {exc!r}")
        sys.exit(0)
