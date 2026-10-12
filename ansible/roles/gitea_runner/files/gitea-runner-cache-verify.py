#!/usr/bin/env python3
# MANAGED BY ANSIBLE (role: gitea_runner). Shared-cache integrity check for the host-mode runner.
# Plan: plans/2026-10-12-runner-between-job-cleanup-plan.md (PR 3).
"""Verify the runner's npm and uv caches against their OWN hashes, queue bad entries, purge them safely.

Every job on a runner shares ~/.npm/_cacache and ~/.cache/uv, so ONE corrupt cache entry fails every
later job that uses it: a corrupt cryptography-50.0.2 `_rust.abi3.so` in cloud-ci-5's uv cache
segfaulted every kartra-ai job that installed it (2026-10-12), and cloud-ci-7 held 14 npm entries that no
longer matched their own sha512. Nothing checked.

  verify   (systemd User=runner, nightly) - hash every npm content file against the digest its path
           encodes, and every file of every uv archive against its *.dist-info/RECORD sha256. Record
           evidence for each bad file (nlink; for uv, ctime against the archive dir, i.e. whether it was
           modified in place after unpack), and append the entries to the purge queue.
  publish  (root, ExecStartPost=+) - validate the result and write the node_exporter beacon.
  apply    (root) - purge queued entries. Called ONLY where no job can be using them: the reclaim's
           ExecStartPre (daemon down) and gitea-runner-cleanup.sh inside its confirmed idle window.

The queue is written by `runner` and read by root, so it is untrusted: each line must match a strict
pattern, every path is built here from fixed roots, and deletion runs as `runner` (runuser), so a forged
queue line can at most delete what runner itself could.
"""
import base64
import csv
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time


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


CONF = load_env_file(os.environ.get("GITEA_CACHE_VERIFY_ENV_FILE", "/etc/gitea-runner-cache-verify.env"))


def cfg(name, default):
    return os.environ.get(name, CONF.get(name, default))


RUNNER_USER = cfg("GITEA_RUNNER_USER", "runner")
HOME = cfg("GITEA_RUNNER_HOME", "/home/runner")
NPM_DIR = cfg("GITEA_CACHE_NPM_DIR", HOME + "/.npm/_cacache")
UV_DIR = cfg("GITEA_CACHE_UV_DIR", HOME + "/.cache/uv")
STATE_DIR = cfg("GITEA_CACHE_STATE_DIR", "/var/lib/gitea-runner-cache")
QUEUE = cfg("GITEA_CACHE_PURGE_QUEUE", os.path.join(STATE_DIR, "purge.queue"))
RESULT = os.path.join(STATE_DIR, "result.json")
TEXTFILE_DIR = cfg("GITEA_CACHE_TEXTFILE_DIR", "/var/lib/prometheus/node-exporter")
BEACON = cfg("GITEA_CACHE_BEACON", "1") == "1"
# Where a corrupt uv inode may also be hardlinked (venvs uv built from the cache). name -> root.
LINK_ROOTS = {
    "uvdata": cfg("GITEA_CACHE_LINK_ROOT_UVDATA", HOME + "/.local/share/uv"),
    "pytest": cfg("GITEA_CACHE_LINK_ROOT_PYTEST", "/tmp/pytest-of-" + RUNNER_USER),
    "work": cfg("GITEA_CACHE_LINK_ROOT_WORK", HOME + "/act-runner/work"),
}
RUNUSER = cfg("GITEA_CACHE_RUNUSER", "runuser")   # tests: a recording fake
MAX_POINTER_BYTES = 1 << 20
CHUNK = 1 << 20

NPM_KEY = re.compile(r"^(sha512:[0-9a-f]{128}|sha1:[0-9a-f]{40})$")
UV_KEY = re.compile(r"^[A-Za-z0-9_-]{16,32}$")
REL = re.compile(r"^[A-Za-z0-9._+@=-]+(/[A-Za-z0-9._+@=-]+)*$")   # no '..' component (checked below)


# ---------------------------------------------------------------------------------------------- hashing
def open_regular(path):
    """fd of a regular file, or None. O_NOFOLLOW: a symlinked cache entry is never followed, and
    O_NONBLOCK keeps a FIFO a job planted from hanging the open; the fstat then rejects it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return fd


def digest(path, algo):
    fd = open_regular(path)
    if fd is None:
        return None, None
    h = hashlib.new(algo)
    try:
        st = os.fstat(fd)
        while True:
            b = os.read(fd, CHUNK)
            if not b:
                break
            h.update(b)
    finally:
        os.close(fd)
    return h, st


def safe_rel(rel):
    return bool(REL.match(rel)) and ".." not in rel.split("/")


# ---------------------------------------------------------------------------------------------- verify
def verify_npm():
    checked, bad = 0, []
    for algo, hexlen in (("sha512", 128), ("sha1", 40)):
        base = os.path.join(NPM_DIR, "content-v2", algo)
        if not os.path.isdir(base):
            continue
        for d1 in sorted(os.listdir(base)):
            p1 = os.path.join(base, d1)
            if not (len(d1) == 2 and os.path.isdir(p1) and not os.path.islink(p1)):
                continue
            for d2 in sorted(os.listdir(p1)):
                p2 = os.path.join(p1, d2)
                if not (len(d2) == 2 and os.path.isdir(p2) and not os.path.islink(p2)):
                    continue
                for rest in os.listdir(p2):
                    want = (d1 + d2 + rest).lower()
                    if len(want) != hexlen or not re.fullmatch(r"[0-9a-f]+", want):
                        continue
                    h, st = digest(os.path.join(p2, rest), algo)
                    if h is None:
                        continue
                    checked += 1
                    if h.hexdigest() != want:
                        bad.append({"cache": "npm", "key": f"{algo}:{want}", "nlink": st.st_nlink,
                                    "size": st.st_size, "mtime": int(st.st_mtime), "inplace": None})
    return checked, bad


def find_link_venvs(path):
    """Other hardlinks of a corrupt inode under LINK_ROOTS, as (root_name, venv-or-session rel path)."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return []
    out = set()
    for name, root in LINK_ROOTS.items():
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                p = os.path.join(dirpath, f)
                try:
                    s = os.stat(p, follow_symlinks=False)
                except OSError:
                    continue
                if s.st_ino != st.st_ino or s.st_dev != st.st_dev:
                    continue
                target = venv_root_for(p, root, name)
                if target:
                    out.add((name, os.path.relpath(target, root)))
    return sorted(out)


def venv_root_for(path, root, name):
    """Nearest ancestor holding pyvenv.cfg (a venv), or for the pytest root the pytest-N session dir;
    never the root itself."""
    d = os.path.dirname(path)
    root = os.path.abspath(root)
    while d.startswith(root + os.sep):
        if os.path.isfile(os.path.join(d, "pyvenv.cfg")):
            return d
        if name == "pytest" and os.path.dirname(d) == root and re.fullmatch(r"pytest-\d+", os.path.basename(d)):
            return d
        d = os.path.dirname(d)
    return None


def verify_uv():
    checked, bad, no_record = 0, [], 0
    base = os.path.join(UV_DIR, "archive-v0")
    if not os.path.isdir(base):
        return checked, bad, no_record
    for aid in sorted(os.listdir(base)):
        adir = os.path.join(base, aid)
        if not UV_KEY.match(aid) or os.path.islink(adir) or not os.path.isdir(adir):
            continue
        records = [os.path.join(adir, e, "RECORD") for e in os.listdir(adir) if e.endswith(".dist-info")]
        records = [r for r in records if os.path.isfile(r) and not os.path.islink(r)]
        if not records:
            no_record += 1
            continue
        dir_ctime = os.stat(adir).st_ctime
        for rec in records:
            try:
                with open(rec, encoding="utf-8", newline="") as fh:
                    rows = list(csv.reader(fh))
            except (OSError, UnicodeDecodeError, csv.Error):
                continue
            for row in rows:
                if len(row) < 2 or not row[1].startswith("sha256=") or not safe_rel(row[0]):
                    continue  # RECORD itself, .pyc and other unhashed lines; never a path that escapes
                h, st = digest(os.path.join(adir, row[0]), "sha256")
                if h is None:
                    continue
                checked += 1
                if base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode() == row[1][7:]:
                    continue
                entry = {"cache": "uv", "key": aid, "file": row[0], "nlink": st.st_nlink, "size": st.st_size,
                         "mtime": int(st.st_mtime), "inplace": bool(st.st_ctime > dir_ctime + 1)}
                if st.st_nlink > 1:
                    entry["links"] = find_link_venvs(os.path.join(adir, row[0]))
                bad.append(entry)
    return checked, bad, no_record


def queue_lines(bad):
    lines = []
    for b in bad:
        if b["cache"] == "npm":
            lines.append(f"npm {b['key']}")
        else:
            lines.append(f"uv {b['key']}")
            for name, rel in b.get("links", []):
                lines.append(f"venv {name} {rel}")
    return lines


def cmd_verify():
    t0 = time.time()
    n_checked, n_bad = verify_npm()
    u_checked, u_bad, no_record = verify_uv()
    bad = n_bad + u_bad
    os.makedirs(STATE_DIR, exist_ok=True)
    lines = queue_lines(bad)
    if lines:
        with open(QUEUE, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write("\n".join(lines) + "\n")
    result = {"finished": int(time.time()), "duration": round(time.time() - t0, 1),
              "checked": {"npm": n_checked, "uv": u_checked}, "uv_no_record": no_record,
              "found": {"npm": len(n_bad), "uv": len({b["key"] for b in u_bad})},
              "inplace": {"npm": 0, "uv": sum(1 for b in u_bad if b["inplace"])},
              "bad": bad[:200]}
    tmp = RESULT + f".{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    os.replace(tmp, RESULT)
    for b in bad:
        print(f"corrupt {b['cache']} {b['key']} {b.get('file', '')} nlink={b['nlink']} inplace={b['inplace']}"
              f" links={b.get('links', [])}", flush=True)
    print(f"verified npm={n_checked} uv={u_checked} bad_npm={len(n_bad)} bad_uv_files={len(u_bad)}"
          f" uv_no_record={no_record} in {result['duration']}s", flush=True)
    return 0


# ---------------------------------------------------------------------------------------------- apply
def parse_queue_line(line):
    """-> (kind, path) or None. Every path is built from a fixed root; nothing is taken from the line."""
    parts = line.split()
    if len(parts) == 2 and parts[0] == "npm" and NPM_KEY.match(parts[1]):
        algo, hexd = parts[1].split(":")
        return "npm", os.path.join(NPM_DIR, "content-v2", algo, hexd[:2], hexd[2:4], hexd[4:])
    if len(parts) == 2 and parts[0] == "uv" and UV_KEY.match(parts[1]):
        return "uv", parts[1]
    if len(parts) == 3 and parts[0] == "venv" and parts[1] in LINK_ROOTS and safe_rel(parts[2]):
        root = os.path.abspath(LINK_ROOTS[parts[1]])
        target = os.path.abspath(os.path.join(root, parts[2]))
        if target == root or not target.startswith(root + os.sep):
            return None
        is_venv = os.path.isfile(os.path.join(target, "pyvenv.cfg"))
        is_session = parts[1] == "pytest" and os.path.dirname(target) == root \
            and re.fullmatch(r"pytest-\d+", os.path.basename(target))
        return ("venv", target) if (is_venv or is_session) else None
    return None


def rm_as_runner(path):
    if not os.path.lexists(path):
        return True
    r = subprocess.run([RUNUSER, "-u", RUNNER_USER, "--", "rm", "-rf", "--one-file-system", "--", path],
                       timeout=60, check=False, capture_output=True, text=True)
    return r.returncode == 0


def uv_pointer_files(aid):
    """Non-archive uv cache files that name the archive id (wheel index pointers, whatever their bucket
    version). Version-agnostic on purpose: the host uv is older than the setup-uv versions that write
    this cache, so `uv cache clean` could miss newer buckets."""
    hits, needle = [], aid.encode()
    for top in os.listdir(UV_DIR) if os.path.isdir(UV_DIR) else []:
        if top.startswith("archive-") or top.startswith("."):
            continue
        for dirpath, _dirs, files in os.walk(os.path.join(UV_DIR, top)):
            for f in files:
                p = os.path.join(dirpath, f)
                fd = open_regular(p)
                if fd is None:
                    continue
                try:
                    if os.fstat(fd).st_size <= MAX_POINTER_BYTES and needle in os.read(fd, MAX_POINTER_BYTES):
                        hits.append(p)
                finally:
                    os.close(fd)
    return hits


def cmd_apply():
    if not os.path.exists(QUEUE):
        write_beacon()
        return 0
    with open(QUEUE, "r+", encoding="utf-8", errors="replace") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        lines = [l.strip() for l in fh.read().splitlines() if l.strip()]
        keep = []
        for line in dict.fromkeys(lines):
            parsed = parse_queue_line(line)
            if parsed is None:
                print(f"reject queue line {line[:120]!r}", flush=True)
                continue
            kind, what = parsed
            if kind == "uv":
                targets = [os.path.join(UV_DIR, "archive-v0", what)] + uv_pointer_files(what)
            else:
                targets = [what]
            ok = all(rm_as_runner(t) for t in targets)
            print(f"purge {kind} {line.split()[-1][:80]}: {'ok' if ok else 'FAILED, kept for retry'}", flush=True)
            if not ok:
                keep.append(line)
        fh.seek(0)
        fh.truncate()
        if keep:
            fh.write("\n".join(keep) + "\n")
    write_beacon()
    return 0


# ---------------------------------------------------------------------------------------------- beacon
def pending_counts():
    counts = {"npm": 0, "uv": 0}
    try:
        with open(QUEUE, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if parts and parts[0] in counts:
                    counts[parts[0]] += 1
    except OSError:
        pass
    return counts


def write_beacon():
    if not BEACON or not os.path.isdir(TEXTFILE_DIR):
        return
    try:
        with open(RESULT, encoding="utf-8") as fh:
            r = json.load(fh)
        last = int(r["finished"])
        dur = float(r["duration"])
        checked = {k: int(r["checked"][k]) for k in ("npm", "uv")}
        found = {k: int(r["found"][k]) for k in ("npm", "uv")}
        inplace = {k: int(r["inplace"][k]) for k in ("npm", "uv")}
    except (OSError, ValueError, KeyError, TypeError):
        return  # no (valid) verify result yet: publish nothing, so the beacon-missing alert can fire
    pend = pending_counts()
    out = []

    def g(name, help_, samples):
        out.append(f"# HELP gitea_runner_cache_{name} {help_}")
        out.append(f"# TYPE gitea_runner_cache_{name} gauge")
        out.extend(samples)
    g("verify_last_run_seconds", "Unix time the last cache verification finished.", [f"gitea_runner_cache_verify_last_run_seconds {last}"])
    g("verify_duration_seconds", "Wall time of the last cache verification.", [f"gitea_runner_cache_verify_duration_seconds {dur}"])
    g("checked", "Files hashed by the last verification, by cache.", [f'gitea_runner_cache_checked{{cache="{k}"}} {v}' for k, v in checked.items()])
    g("corrupt_found", "Entries the last verification found corrupt (uv: archives), by cache. Points at the host.",
      [f'gitea_runner_cache_corrupt_found{{cache="{k}"}} {v}' for k, v in found.items()])
    g("corrupt_pending", "Queued corrupt entries not yet purged, by cache.",
      [f'gitea_runner_cache_corrupt_pending{{cache="{k}"}} {v}' for k, v in pend.items()])
    g("corrupt_inplace_evidence", "Corrupt uv files changed after their archive was unpacked (a job wrote through a hardlink).",
      [f'gitea_runner_cache_corrupt_inplace_evidence{{cache="{k}"}} {v}' for k, v in inplace.items()])
    tmp = os.path.join(TEXTFILE_DIR, f"gitea_runner_cache_verify.prom.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(TEXTFILE_DIR, "gitea_runner_cache_verify.prom"))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "verify"
    if cmd == "verify":
        return cmd_verify()
    if cmd == "publish":
        write_beacon()
        return 0
    if cmd == "apply":
        return cmd_apply()
    print(f"usage: {argv[0]} verify|publish|apply", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
