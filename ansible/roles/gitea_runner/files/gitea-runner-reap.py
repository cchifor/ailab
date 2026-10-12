#!/usr/bin/env python3
# MANAGED BY ANSIBLE (role: gitea_runner). Job-boundary leftover reaper for the host-mode act_runner.
# Called by gitea-runner-reclaim.sh on every timer tick, before its busy gate.
# Plan: plans/2026-10-12-runner-between-job-cleanup-plan.md (PR 2); review: ailab#1285.
"""Find, and in `enforce` mode remove, what EARLIER jobs left behind on this runner.

act_runner 0.6.1 (host mode) has no per-job hook. The ONLY job boundary it emits is its own journal line
`task <N> repo is <owner>/<repo> ...`, logged as a task starts; with capacity 1 the previous task has
finished by then. So anything CREATED before the newest task line, and not part of the job it started,
is a leftover. Membership is decided by creation time, never ancestry: a live kartra-ai job's
Playwright browsers on cloud-ci-10 were not descendants of act_runner's MainPID.

Boundary E for a pass is max(B, now - (job_timeout + grace)), B = newest task start - margin. No job
outlives its timeout, so E never reaches into a live job even without a parsable B.

  * Edge pass: once per new task line, with that E - but only while the edge is FRESH (within
    EDGE_MAX_LAG_SEC of B). A late edge (first tick after a deploy or reboot, a slow tick) falls back to
    the timeout floor, because by then the new job may already be reusing leftovers.
  * Periodic pass: every PERIODIC_SEC, timeout floor only.
  * Project veto, both passes: an object whose compose project has a container created at or after B
    (the current job is running that project) is left alone.
  * Fail closed: any docker list/inspect that fails or comes back incomplete, or any timestamp that does
    not parse, skips the docker part of the pass. Nothing is ever computed from a partial inventory.

Modes: off | log | enforce. `log` (the default) only reports `would-reap ...` and runs an adoption probe
that gates the switch to enforce. Not detectable, and documented as such: a job that talks to an
untouched leftover (a running container it neither restarts nor composes, a server it only connects
to) without changing it. Process reaping stays behind the same gate.
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
    """Tunables: env var beats the env file beats the default."""
    return os.environ.get(name, CONF.get(name, default))


def inj(name, default):
    """Injection points (tests only): the process environment ONLY, never the env file."""
    return os.environ.get(name, default)


SERVICE = cfg("GITEA_RUNNER_SERVICE", "gitea-act-runner.service")
WORKDIR_PARENT = cfg("GITEA_RUNNER_WORKDIR_PARENT", "/home/runner/act-runner/work").rstrip("/")
CONFIG_YAML = os.path.join(cfg("GITEA_RUNNER_CONFIG_DIR", "/home/runner/act-runner"), "config.yaml")
JOB_TIMEOUT = cfg("GITEA_RUNNER_JOB_TIMEOUT", "3h")
MODE = cfg("GITEA_REAP_MODE", "log")
MARGIN_SEC = float(cfg("GITEA_REAP_MARGIN_SEC", "5"))
GRACE_SEC = float(cfg("GITEA_REAP_GRACE_SEC", "600"))
PERIODIC_SEC = float(cfg("GITEA_REAP_PERIODIC_SEC", "900"))
EDGE_MAX_LAG_SEC = float(cfg("GITEA_REAP_EDGE_MAX_LAG_SEC", "20"))
PROBE_SEC = float(cfg("GITEA_REAP_PROBE_SEC", "30"))
MAX_PROCS = int(cfg("GITEA_REAP_MAX_PROCS", "64"))
TERM_WAIT_SEC = float(cfg("GITEA_REAP_TERM_WAIT_SEC", "10"))
DEADLINE_SEC = float(cfg("GITEA_REAP_DEADLINE_SEC", "45"))
ADOPT_CPU_TICKS = int(cfg("GITEA_REAP_ADOPT_CPU_TICKS", "200"))
KEEP_LABEL_RE = cfg("GITEA_REAP_KEEP_LABEL_RE", r"^(io\.dsh\.conductor\.image-pin|ci\.keep)$")
INFRA_EXCLUDE_RE = cfg("GITEA_CLEANUP_INFRA_EXCLUDE_RE", "buildkit|buildx")
PEER_SERVICES = cfg("GITEA_CLEANUP_PEER_SERVICES", "actions.runner.cchifor-platform.service")
STATE_PATH = cfg("GITEA_REAP_STATE", "/var/lib/gitea-runner-reclaim/reap.state")
BEACON = cfg("GITEA_RECLAIM_BEACON", "1") == "1"
TEXTFILE_DIR = cfg("GITEA_RECLAIM_TEXTFILE_DIR", "/var/lib/prometheus/node-exporter")
PROC = inj("GITEA_REAP_PROC_ROOT", "/proc")
CGROUP_ROOT = inj("GITEA_REAP_CGROUP_ROOT", "/sys/fs/cgroup")
SYSTEMCTL = inj("GITEA_REAP_SYSTEMCTL", "systemctl")
JOURNALCTL = inj("GITEA_REAP_JOURNALCTL", "journalctl")
DOCKER = inj("GITEA_REAP_DOCKER", "docker")
LOGGER = inj("GITEA_REAP_LOGGER", "logger")
KILL_CMD = inj("GITEA_REAP_KILL_CMD", "")
BOOT_ID_FILE = inj("GITEA_REAP_BOOT_ID_FILE", "/proc/sys/kernel/random/boot_id")
NOW_OVERRIDE = inj("GITEA_REAP_NOW", "")
CLK_TCK = int(inj("GITEA_REAP_CLK_TCK", str(os.sysconf("SC_CLK_TCK"))))
ADOPT_WINDOW_SEC = 48 * 3600
WOULD_TTL_SEC = 24 * 3600

# act_runner's own line, verbatim from cloud-ci-9's journal (2026-10-12):
#   time="2026-10-12T13:56:15+03:00" level=info msg="task 434665 repo is cchifor/platform https://github.com https://git.chifor.me"
TASK_RE = re.compile(
    r'^time="(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(Z|[+-]\d\d:\d\d)" level=info '
    r'msg="task (\d+) repo is ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+) ')
NEVER = float("-inf")
ERRORS = []
T0 = time.monotonic()


class Incomplete(Exception):
    """A docker inventory that is not known to be complete: the docker part of the pass is skipped."""


# ---------------------------------------------------------------------------------------------- helpers
def log(msg):
    try:
        subprocess.run([LOGGER, "-t", "gitea-runner-reap", "--", msg], timeout=10, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 - logging must never break the reaper
        pass


def run(argv, timeout=15):
    """stdout, or None (error recorded) on any failure or non-zero exit."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as e:  # noqa: BLE001
        ERRORS.append(f"{os.path.basename(argv[0])} {argv[1] if len(argv) > 1 else ''}: {type(e).__name__}")
        return None
    if p.returncode != 0:
        ERRORS.append(f"{os.path.basename(argv[0])} {' '.join(argv[1:3])}: rc={p.returncode} {p.stderr.strip()[:160]}")
        return None
    return p.stdout


def over_deadline():
    return time.monotonic() - T0 > DEADLINE_SEC


def parse_duration(s):
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", str(s))
    if not m:
        raise ValueError(f"unparsable duration {s!r}")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def rfc3339(ts, never_ok=False):
    """Docker timestamp (nanoseconds, Z or offset) -> epoch float. `0001-01-01...` is the zero time Docker
    reports for a never-started container: NEVER, but only where the caller says that is meaningful.
    Anything missing or unparsable -> None, and callers skip the object (fail closed)."""
    if not isinstance(ts, str) or not ts:
        return None
    if ts.startswith("0001-01-01"):
        return NEVER if never_ok else None
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
    """(state, ppid, start_boot_seconds, cpu_ticks) or None. comm may hold spaces and ')'."""
    try:
        raw = read(os.path.join(PROC, str(pid), "stat"))
    except OSError:
        return None
    rest = raw[raw.rfind(")") + 2:].split()
    try:
        return rest[0], int(rest[1]), int(rest[19]) / CLK_TCK, int(rest[11]) + int(rest[12])
    except (IndexError, ValueError):
        return None


def proc_comm(pid):
    try:
        return read(os.path.join(PROC, str(pid), "comm")).strip()
    except OSError:
        return ""


SECRETISH = re.compile(r"(?i)(token|secret|passw|apikey|api_key|auth|bearer|credential)")


def proc_label(pid):
    """comm plus an argv sample with likely secrets masked: this goes to the journal (and on to Loki)."""
    try:
        argv = read(os.path.join(PROC, str(pid), "cmdline")).split("\0")
    except OSError:
        return "?"
    out, mask_next = [], False
    for a in [x for x in argv if x][:8]:
        if mask_next:  # the value after `--token` / `-p` style flags
            a, mask_next = "***", False
        elif "=" in a:
            a = a.split("=", 1)[0] + "=***"
        elif SECRETISH.search(a):
            a, mask_next = a[:12] + "***", a.startswith("-")
        elif len(a) > 64:
            a = a[:12] + "***"
        out.append(a)
    return f"{proc_comm(pid)} [{' '.join(out)[:100]}]"


# ---------------------------------------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            st = json.load(fh)
            return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = f"{STATE_PATH}.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh)
    os.replace(tmp, STATE_PATH)


def refuse(st, reason):
    """Log a refusal only when its reason changes; a stopped daemon would otherwise log every 5 s."""
    if st.get("refused") != reason:
        log(f"refuse: {reason}")
    st["refused"] = reason


# ---------------------------------------------------------------------------------------------- boundary
def capacity_is_one():
    try:
        m = re.search(r"^\s*capacity:\s*(\d+)\s*$", read(CONFIG_YAML), re.M)
    except OSError:
        return False
    return bool(m) and int(m.group(1)) == 1


def main_pid():
    out = run([SYSTEMCTL, "show", "-p", "MainPID", "--value", SERVICE], timeout=10)
    try:
        pid = int((out or "").strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def read_task_lines(st, mainpid, bt, boot_id, since_epoch):
    """Newest boot-relative task start (minus margin) among new act_runner journal entries, or None.
    Only act_runner's own stdout counts: journald files `logger` output from any job process under the
    same unit, so _PID and _COMM are both matched and the regex is anchored."""
    base = [JOURNALCTL, "-o", "json", "--no-pager", f"_SYSTEMD_UNIT={SERVICE}", f"_PID={mainpid}",
            "_COMM=act_runner"]
    out = None
    if st.get("cursor"):
        out = run(base + ["--after-cursor", st["cursor"]], timeout=15)
        if out is None:
            st.pop("cursor", None)  # vacuumed or rotated: never retry a dead cursor every tick
    if out is None:
        out = run(base + ["--since", f"@{int(since_epoch)}"], timeout=20)
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
        if ent.get("_BOOT_ID", "").replace("-", "") == boot_id and str(ent.get("__MONOTONIC_TIMESTAMP", "")).isdigit():
            estimates.append(int(ent["__MONOTONIC_TIMESTAMP"]) / 1e6)  # journald RECEIVE time: may lag
        b = min(estimates) - MARGIN_SEC  # the earlier estimate is the safe one
        if newest is None or b > newest[0]:
            newest = (b, m.group(3), m.group(4))
    return newest


# ---------------------------------------------------------------------------------------------- processes
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
        out.append((pid, s[2], s[3]))
    return out[:MAX_PROCS], len(out) > MAX_PROCS


def signal_pid(pid, sig):
    if KILL_CMD:  # tests: every signal goes through this indirection, never os.kill
        subprocess.run([KILL_CMD, str(int(sig)), str(pid)], timeout=10, check=False)
        return True
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False


def same_process(pid, start):
    s = proc_stat(pid)
    return bool(s) and abs(s[2] - start) < 0.5 and s[0] != "Z"


def reap_processes(cands, now_boot):
    """SIGTERM, one shared wait, SIGKILL. Start time is re-read before EACH signal (PID reuse)."""
    live, signalled = [], 0
    for pid, start, _cpu in cands:
        if same_process(pid, start):
            log(f"reap process pid={pid} age={int(now_boot - start)}s {proc_label(pid)}")
            if signal_pid(pid, signal.SIGTERM):
                signalled += 1
                live.append((pid, start))
    deadline = time.monotonic() + TERM_WAIT_SEC
    while live and time.monotonic() < deadline:
        time.sleep(0.5)
        live = [(p, s) for p, s in live if same_process(p, s)]
    for pid, start in live:
        if same_process(pid, start):
            log(f"reap process pid={pid} still alive after {TERM_WAIT_SEC:.0f}s -> SIGKILL")
            signal_pid(pid, signal.SIGKILL)
    return signalled


# ---------------------------------------------------------------------------------------------- docker
LIST = {"container": ["ps", "-aq", "--no-trunc"], "network": ["network", "ls", "-q", "--no-trunc"],
        "volume": ["volume", "ls", "-q"]}
INSPECT = {"container": ["inspect"], "network": ["network", "inspect"], "volume": ["volume", "inspect"]}
ID_KEY = {"container": "Id", "network": "Id", "volume": "Name"}


def inventory(kind):
    """Every object of a kind, or raise Incomplete. One retry: a container removed between `ps` and
    `inspect` makes the bulk inspect exit non-zero, which must never read as "nothing exists"."""
    for _attempt in (1, 2):
        out = run([DOCKER] + LIST[kind], timeout=10)
        if out is None:
            raise Incomplete(f"{kind} ls failed")
        ids = out.split()
        if not ids:
            return []
        raw = run([DOCKER] + INSPECT[kind] + ids, timeout=20)
        if raw is None:
            continue
        try:
            items = json.loads(raw)
        except ValueError:
            continue
        got = {i.get(ID_KEY[kind]) for i in items if isinstance(i, dict)}
        if all(x in got or any(str(g).startswith(x) for g in got if g) for x in ids):
            return items
    raise Incomplete(f"{kind} inspect incomplete")


def keep_by_label(labels):
    return any(KEEP_RE.search(k) for k in (labels or {}))


def under_workdir(path):
    if not isinstance(path, str) or not path or ".." in path.split("/"):
        return False
    return path == WORKDIR_PARENT or path.startswith(WORKDIR_PARENT + "/")


def peers_none():
    return PEER_SERVICES.strip() == "none"


def container_view(c):
    cfgd = c.get("Config") or {}
    labels = cfgd.get("Labels") or {}
    return {
        "id": c.get("Id", ""), "name": (c.get("Name") or "").lstrip("/"), "image": cfgd.get("Image", ""),
        "created": rfc3339(c.get("Created")),
        "started": rfc3339((c.get("State") or {}).get("StartedAt"), never_ok=True),
        "labels": labels, "project": labels.get("com.docker.compose.project"),
        "service": labels.get("com.docker.compose.service"),
        "workdir_attr": under_workdir(labels.get("com.docker.compose.project.working_dir"))
        or any(under_workdir(m.get("Source")) for m in c.get("Mounts") or []),
        "networks": list(((c.get("NetworkSettings") or {}).get("Networks") or {}).keys()),
        "volumes": [m.get("Name") for m in c.get("Mounts") or [] if m.get("Type") == "volume" and m.get("Name")],
    }


def docker_snapshot():
    containers = [container_view(c) for c in inventory("container")]
    if any(c["created"] is None or c["started"] is None for c in containers):
        raise Incomplete("a container timestamp did not parse")
    return containers, inventory("network"), inventory("volume")


def docker_candidates(e_epoch, b_epoch, snapshot):
    containers, networks, volumes = snapshot
    # Project veto: a compose project with a container created at/after B is in use by the current job.
    live_projects = {c["project"] for c in containers if c["project"] and b_epoch is not None and c["created"] >= b_epoch}
    gitea_projects = {c["project"] for c in containers if c["project"] and c["workdir_attr"]}
    cand_c = []
    for c in containers:
        if not (c["created"] < e_epoch and c["started"] < e_epoch):
            continue  # created or (re)started by the current job, or brought back by a restart policy
        if re.search(INFRA_RE, f"{c['image']} /{c['name']}") or keep_by_label(c["labels"]):
            continue
        if c["project"] in live_projects:
            continue
        if not peers_none() and not c["workdir_attr"]:
            continue  # a co-located GitHub agent's container (or unknown): never ours to touch
        cand_c.append(c)
    cand_ids = {c["id"] for c in cand_c}
    survivors = [c for c in containers if c["id"] not in cand_ids]
    used_nets = {n for c in survivors for n in c["networks"]}
    used_vols = {v for c in survivors for v in c["volumes"]}
    cand_n, cand_v = [], []
    for n in networks:
        labels = n.get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        created = rfc3339(n.get("Created"))
        if n.get("Name") in ("bridge", "host", "none") or not project or created is None:
            continue
        if created >= e_epoch or keep_by_label(labels) or n.get("Name") in used_nets or project in live_projects:
            continue
        if set((n.get("Containers") or {}).keys()) - cand_ids:
            continue
        if not peers_none() and project not in gitea_projects:
            continue
        cand_n.append({"kind": "network", "id": n.get("Id", ""), "name": n.get("Name", ""), "project": project})
    for v in volumes:
        labels = v.get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        anonymous = "com.docker.volume.anonymous" in labels
        created = rfc3339(v.get("CreatedAt"))
        if not (project or anonymous) or created is None:
            continue  # named, non-compose volumes may be deliberate caches; unknown age: never guess
        if created >= e_epoch or keep_by_label(labels) or v.get("Name") in used_vols or project in live_projects:
            continue
        if not peers_none() and (anonymous or project not in gitea_projects):
            continue  # on a shared daemon an anonymous volume cannot be attributed at all
        cand_v.append({"kind": "volume", "id": v.get("Name", ""), "name": v.get("Name", ""), "project": project})
    return cand_c, cand_n, cand_v


def docker_rm(kind, ids):
    if not ids:
        return 0
    argv = {"container": [DOCKER, "rm", "-f"], "network": [DOCKER, "network", "rm"],
            "volume": [DOCKER, "volume", "rm"]}[kind] + ids
    return len(ids) if run(argv, timeout=30) is not None else 0


# ---------------------------------------------------------------------------------------------- adoption
def adoption_probe(st, containers, now):
    """Gate for enforce: a would-reap object counts as ADOPTED when it was used after it was first
    reported. Inspect-based, not `docker events`: dockerd's --since buffer is a short ring, and a
    detached compose `up` reusing an unchanged running container emits no event at all.
      container: a newer container (created after first report) with the same compose project AND
                 service (compose reuses by project+service, so the shared `hostexecutor` project alone
                 proves nothing), or the container itself (re)started after the first report;
      network / volume: a newer container joined / mounts it;
      process: it burned ADOPT_CPU_TICKS of CPU since the first report, or gained a child started since.
    """
    hits = 0
    for w in st.get("would", []):
        if w.get("adopted_at"):
            continue
        since = w["first_seen"]
        reason = None
        if w["kind"] == "process":
            pid, start = int(w["id"].split(":")[0]), float(w["id"].split(":")[1])
            s = proc_stat(pid)
            if s and abs(s[2] - start) < 0.5:
                if s[3] - w.get("cpu", s[3]) >= ADOPT_CPU_TICKS:
                    reason = f"it used {(s[3] - w.get('cpu', s[3])) / CLK_TCK:.1f}s CPU since first reported"
                else:
                    since_boot = since - btime()
                    for child in cgroup_pids():
                        cs = proc_stat(child)
                        if cs and cs[1] == pid and cs[2] >= since_boot:
                            reason = f"it started child pid={child} since first reported"
                            break
        elif containers is not None:
            newer = [c for c in containers if c["created"] >= since]
            if w["kind"] == "network" and any(w["name"] in c["networks"] for c in newer):
                reason = "a newer container joined it"
            elif w["kind"] == "volume" and any(w["name"] in c["volumes"] for c in newer):
                reason = "a newer container mounts it"
            elif w["kind"] == "container":
                if w.get("project") and any(c["project"] == w["project"] and c["service"] == w.get("service")
                                            for c in newer):
                    reason = f"compose {w['project']}/{w.get('service')} has a newer container"
                cur = next((c for c in containers if c["id"] == w["id"]), None)
                if cur and cur["started"] >= since:
                    reason = "it was (re)started after it was first reported"
        if reason:
            w["adopted_at"] = now
            hits += 1
            log(f"ADOPTED would-reap {w['kind']} {w['name']}: {reason}")
    st["adopted_total"] = st.get("adopted_total", 0) + hits
    return hits


def remember(st, kind, oid, name, now, **extra):
    for w in st.setdefault("would", []):
        if w["kind"] == kind and w["id"] == oid:
            w["last_seen"] = now
            return
    st["would"].append({"kind": kind, "id": oid, "name": name, "first_seen": now, "last_seen": now, **extra})


# ---------------------------------------------------------------------------------------------- beacon
def write_beacon(st, now, mode_num, refused):
    if not BEACON or not os.path.isdir(TEXTFILE_DIR):
        return
    lp = st.get("last_pass") or {}
    cand, reaped = lp.get("candidates") or {}, lp.get("reaped") or {}
    kinds = ("process", "container", "network", "volume")
    b_age = -1 if st.get("b_epoch") is None else int(max(0, now - st["b_epoch"]))
    adopted_48h = sum(1 for w in st.get("would", []) if w.get("adopted_at") and now - w["adopted_at"] < ADOPT_WINDOW_SEC)
    lines = []

    def g(name, help_, samples):
        lines.append(f"# HELP gitea_runner_reap_{name} {help_}")
        lines.append(f"# TYPE gitea_runner_reap_{name} gauge")
        lines.extend(samples)
    g("last_run_seconds", "Unix time the job-boundary reaper last ran (every reclaim tick).", [f"gitea_runner_reap_last_run_seconds {int(now)}"])
    g("mode", "0=off 1=log 2=enforce.", [f"gitea_runner_reap_mode {mode_num}"])
    g("refused", "1 if the reaper refused to act (capacity != 1, no or wrong MainPID, a bad regex).", [f"gitea_runner_reap_refused {refused}"])
    g("boundary_age_seconds", "Seconds since the newest parsed task start (-1 = none parsed on this boot).",
      [f"gitea_runner_reap_boundary_age_seconds {b_age}"])
    g("last_pass_seconds", "Unix time of the last edge/periodic pass (the counts below are from it).",
      [f"gitea_runner_reap_last_pass_seconds {int(lp.get('time', 0))}"])
    g("last_candidates", "Leftovers found by the last pass, by kind.",
      [f'gitea_runner_reap_last_candidates{{kind="{k}"}} {int(cand.get(k, 0))}' for k in kinds])
    g("last_reaped", "Leftovers removed (containers/networks/volumes) or signalled (processes) by the last pass.",
      [f'gitea_runner_reap_last_reaped{{kind="{k}"}} {int(reaped.get(k, 0))}' for k in kinds])
    g("docker_skipped", "1 if the last pass skipped docker because its inventory was incomplete.",
      [f"gitea_runner_reap_docker_skipped {int(bool(lp.get('docker_skipped')))}"])
    g("adopted_48h", "Would-reap objects a later job used, last 48h (the enforce gate).", [f"gitea_runner_reap_adopted_48h {adopted_48h}"])
    g("adopted_total", "Would-reap objects a later job used, since the state file began.", [f"gitea_runner_reap_adopted_total {st.get('adopted_total', 0)}"])
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
def run_pass(st, kind_of_pass, e_boot, b_boot, bt, now, now_boot, mainpid):
    e_epoch = bt + e_boot
    b_epoch = None if b_boot is None else bt + b_boot
    tag = f"{kind_of_pass} boundary_age={int(now_boot - e_boot)}s task={st.get('b_task', '-')}"
    counts = {k: 0 for k in ("process", "container", "network", "volume")}
    reaped = dict(counts)
    procs, truncated = process_candidates(mainpid, e_boot)
    counts["process"] = len(procs)
    if MODE == "enforce":
        reaped["process"] = reap_processes(procs, now_boot)
    else:
        for pid, start, cpu in procs:
            log(f"would-reap process pid={pid} age={int(now_boot - start)}s {proc_label(pid)} {tag}")
            remember(st, "process", f"{pid}:{start}", f"pid {pid}", now, cpu=cpu)
    docker_skipped, snapshot = False, None
    try:
        if over_deadline():
            raise Incomplete("deadline")
        # computed AFTER the process step (in enforce that includes the TERM wait), and in enforce a
        # second snapshot must agree with the first before anything is removed
        snapshot = docker_snapshot()
        cand = docker_candidates(e_epoch, b_epoch, snapshot)
        if MODE == "enforce":
            again = docker_candidates(e_epoch, b_epoch, docker_snapshot())
            keep = {(o["kind"] if "kind" in o else "container", o["id"]) for grp in again for o in grp}
            cand = tuple([o for o in grp if (o.get("kind", "container"), o["id"]) in keep] for grp in cand)
    except Incomplete as e:
        docker_skipped = True
        log(f"docker part of the {kind_of_pass} pass skipped (fail closed): {e}")
        cand = ([], [], [])
    cand_c, cand_n, cand_v = cand
    counts.update(container=len(cand_c), network=len(cand_n), volume=len(cand_v))
    if MODE == "enforce":
        for c in cand_c:
            log(f"reap container {c['name']} ({c['image']}) {tag}")
        reaped["container"] = docker_rm("container", [c["id"] for c in cand_c])
        for n in cand_n:
            log(f"reap network {n['name']} {tag}")
        reaped["network"] = docker_rm("network", [n["id"] for n in cand_n])
        for v in cand_v:
            log(f"reap volume {v['name']} {tag}")
        reaped["volume"] = docker_rm("volume", [v["id"] for v in cand_v])
    else:
        for c in cand_c:
            log(f"would-reap container {c['name']} project={c['project'] or '-'} {tag}")
            remember(st, "container", c["id"], c["name"], now, project=c["project"], service=c["service"])
        for o in cand_n + cand_v:
            log(f"would-reap {o['kind']} {o['name']} project={o['project'] or '-'} {tag}")
            remember(st, o["kind"], o["id"], o["name"], now, project=o["project"])
    if truncated:
        log(f"process cap {MAX_PROCS} hit on the {kind_of_pass} pass; the rest are picked up by a later pass")
    st["last_pass"] = {"kind": kind_of_pass, "time": now, "candidates": counts, "reaped": reaped,
                       "docker_skipped": docker_skipped}
    return snapshot


def main():
    mode_num = {"off": 0, "log": 1, "enforce": 2}.get(MODE)
    now = float(NOW_OVERRIDE) if NOW_OVERRIDE else time.time()
    st = load_state()
    if mode_num is None:
        refuse(st, f"unknown GITEA_REAP_MODE={MODE!r}")
        write_beacon(st, now, 0, 1)
        save_state(st)
        return 0
    if mode_num == 0:  # the kill switch still publishes a beacon, so mode=0 is visible in Prometheus
        st.pop("refused", None)
        write_beacon(st, now, 0, 0)
        save_state(st)
        return 0
    global KEEP_RE, INFRA_RE
    try:
        KEEP_RE, INFRA_RE = re.compile(KEEP_LABEL_RE), re.compile(INFRA_EXCLUDE_RE, re.I)
        bt = btime()
        boot_id = read(BOOT_ID_FILE).strip().replace("-", "")
        timeout_sec = parse_duration(JOB_TIMEOUT)
    except (OSError, RuntimeError, ValueError, re.error) as e:
        refuse(st, f"bad configuration or clock basis: {e}")
        write_beacon(st, now, mode_num, 1)
        save_state(st)
        return 0
    if st.get("boot_id") != boot_id:
        st = {"boot_id": boot_id, "would": [w for w in st.get("would", []) if w["kind"] != "process"],
              "adopted_total": st.get("adopted_total", 0)}
    mainpid = main_pid()
    if not capacity_is_one():
        reason = "act_runner capacity is not 1"
    elif mainpid is None:
        reason = f"no {SERVICE} MainPID (daemon stopped or starting)"
    elif proc_comm(mainpid) != "act_runner":
        reason = f"{SERVICE} MainPID {mainpid} is {proc_comm(mainpid)!r}, not act_runner"
    else:
        reason = None
    if reason:
        refuse(st, reason)
        write_beacon(st, now, mode_num, 1)
        save_state(st)
        return 0
    if st.pop("refused", None):
        log("refusal cleared")
    now_boot = now - bt
    floor_boot = now_boot - (timeout_sec + GRACE_SEC)
    newest = read_task_lines(st, mainpid, bt, boot_id, now - timeout_sec - GRACE_SEC)
    if newest and (st.get("b_boot") is None or newest[0] > st["b_boot"]):
        st["b_boot"], st["b_task"], st["b_repo"] = newest
        st["b_epoch"] = bt + newest[0]
    b_boot = st.get("b_boot")

    passes = []
    if b_boot is not None and (st.get("last_edge_b") is None or b_boot > st["last_edge_b"] + 0.5):
        lag = now_boot - b_boot
        if lag <= EDGE_MAX_LAG_SEC:
            passes.append(("edge", max(b_boot, floor_boot)))
        else:
            # a late edge: the new job may already be reusing leftovers, so only the floor is safe
            passes.append(("late-edge", floor_boot))
        st["last_edge_b"] = b_boot
    elif now - st.get("last_periodic", 0) >= PERIODIC_SEC:
        passes.append(("periodic", floor_boot))

    snapshot = None
    for kind_of_pass, e_boot in passes:
        if kind_of_pass == "edge":
            again = read_task_lines(st, mainpid, bt, boot_id, now - timeout_sec - GRACE_SEC)  # fresh-start guard
            if again and again[0] > b_boot:
                st["b_boot"], st["b_task"], st["b_repo"] = again
                st["b_epoch"] = bt + again[0]
                b_boot = st["last_edge_b"] = again[0]
                e_boot = max(b_boot, floor_boot) if now_boot - b_boot <= EDGE_MAX_LAG_SEC else floor_boot
        snapshot = run_pass(st, kind_of_pass, e_boot, b_boot, bt, now, now_boot, mainpid)
        st["last_periodic"] = now

    pending = [w for w in st.get("would", []) if not w.get("adopted_at")]
    if MODE == "log" and pending and (snapshot is not None or now - st.get("last_probe", 0) >= PROBE_SEC):
        containers = None
        if any(w["kind"] != "process" for w in pending) and not over_deadline():
            try:
                containers = snapshot[0] if snapshot is not None else [container_view(c) for c in inventory("container")]
                if any(c["created"] is None or c["started"] is None for c in containers):
                    containers = None
            except Incomplete:
                containers = None
        adoption_probe(st, containers, now)
        st["last_probe"] = now
    st["would"] = [w for w in st.get("would", []) if now - w.get("last_seen", 0) < WOULD_TTL_SEC
                   or (w.get("adopted_at") and now - w["adopted_at"] < ADOPT_WINDOW_SEC)]
    save_state(st)
    write_beacon(st, now, mode_num, 0)
    if ERRORS:
        log("errors: " + " | ".join(ERRORS[:5]))
    return 0


KEEP_RE = INFRA_RE = None

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - best effort, like the reclaim script: never block the timer
        log(f"reaper crashed: {type(exc).__name__}: {exc}")
        sys.exit(0)
