#!/usr/bin/env python3
# MANAGED BY ANSIBLE (role: gitea_runner). Shared-cache integrity check for the host-mode runner.
# Plan: plans/2026-10-12-runner-between-job-cleanup-plan.md (PR 3); review: ailab#1288.
"""Verify the runner's npm and uv caches against their OWN hashes, queue bad entries, purge them safely.

Every job on a runner shares ~/.npm/_cacache and ~/.cache/uv. A corrupt uv archive fails every later job
that installs it (uv hardlinks it as-is): a corrupt cryptography-50.0.2 `_rust.abi3.so` in cloud-ci-5's
cache segfaulted every kartra-ai job that installed it (2026-10-12). npm checks integrity on read and
re-fetches, so a corrupt npm entry costs a download, but cloud-ci-7 held 14 of them, which points at the
host. Nothing checked either.

  verify   (systemd User=runner, nightly) - hash every npm content file against the digest its path
           encodes, and every file of every uv archive against its *.dist-info/RECORD sha256. A file
           RECORD lists that is missing or not a regular file, or a RECORD that cannot be read, makes the
           archive unusable too (0 such files in 95k real archives on 3 runners, so this does not mass-
           purge). Evidence per bad file: nlink, whether its CONTENT changed after unpack (mtime against
           the archive dir's ctime; ctime would move on every hardlink/unlink), and which venvs share
           the inode. New entries are appended to the purge queue (deduplicated).
  publish  (root, ExecStartPost=+) - validate the result and write the node_exporter beacon.
  apply    (root) - purge queued entries. Runs only while act_runner is DOWN (MainPID 0): the reclaim
           runs it then, and gitea-runner-cleanup.sh restarts act_runner in an idle gap to get there.

The state dir is runner-owned, so its files are untrusted input to root: they are opened with
O_NOFOLLOW (and O_NONBLOCK) and must be regular files owned by the runner uid; each queue line must
match a strict pattern; every path is built here from fixed roots; deletion runs as `runner` (runuser),
so a forged line can at most delete what runner itself could.
"""
import base64
import csv
import fcntl
import hashlib
import io
import json
import math
import os
import pwd
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
LINK_ROOTS = {
    "uvdata": cfg("GITEA_CACHE_LINK_ROOT_UVDATA", HOME + "/.local/share/uv"),
    "pytest": cfg("GITEA_CACHE_LINK_ROOT_PYTEST", "/tmp/pytest-of-" + RUNNER_USER),
    "work": cfg("GITEA_CACHE_LINK_ROOT_WORK", HOME + "/act-runner/work"),
}
RUNUSER = os.environ.get("GITEA_CACHE_RUNUSER", "runuser")   # tests: a recording fake (process env only)
MAX_POINTER_BYTES = 1 << 20
MAX_STATE_BYTES = 8 << 20
CHUNK = 1 << 20

NPM_KEY = re.compile(r"^(sha512:[0-9a-f]{128}|sha1:[0-9a-f]{40})$")
UV_KEY = re.compile(r"^[A-Za-z0-9_-]{16,32}$")
REL = re.compile(r"^[A-Za-z0-9._+@=-]+(/[A-Za-z0-9._+@=-]+)*$")
SKIPPED = {"vanished": 0}


def runner_uid():
    try:
        return pwd.getpwnam(RUNNER_USER).pw_uid
    except KeyError:
        return None


# ---------------------------------------------------------------------------------------------- files
def open_regular(path, flags=os.O_RDONLY, owner=None):
    """fd of a regular file (optionally owned by `owner`), or None. lstat first, so a device node or a
    FIFO is never even opened; then O_NOFOLLOW|O_NONBLOCK, and the opened inode must be the one that
    was lstat'ed (a swap between the two calls is refused)."""
    try:
        pre = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(pre.st_mode) or (owner is not None and pre.st_uid != owner):
        return None
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)
    except OSError:
        return None
    st = os.fstat(fd)
    if (not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (pre.st_dev, pre.st_ino)
            or (owner is not None and st.st_uid != owner)):
        os.close(fd)
        return None
    return fd


def read_state_file(path, owner):
    """Contents of a runner-written state file, read as root: regular, runner-owned, bounded."""
    fd = open_regular(path, owner=owner)
    if fd is None:
        return None
    try:
        return os.read(fd, MAX_STATE_BYTES).decode("utf-8", "replace")
    finally:
        os.close(fd)


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


def listdir(path):
    """os.listdir that tolerates the cache changing under the walk (a job's prune, uv replacing an
    archive): a vanished directory is skipped and counted, never fatal to the whole pass."""
    try:
        return sorted(os.listdir(path))
    except FileNotFoundError:
        SKIPPED["vanished"] += 1
        return []


# ---------------------------------------------------------------------------------------------- verify
def verify_npm():
    checked, bad = 0, []
    for algo, hexlen in (("sha512", 128), ("sha1", 40)):
        base = os.path.join(NPM_DIR, "content-v2", algo)
        for d1 in listdir(base) if os.path.isdir(base) else []:
            p1 = os.path.join(base, d1)
            if len(d1) != 2 or os.path.islink(p1) or not os.path.isdir(p1):
                continue
            for d2 in listdir(p1):
                p2 = os.path.join(p1, d2)
                if len(d2) != 2 or os.path.islink(p2) or not os.path.isdir(p2):
                    continue
                for rest in listdir(p2):
                    want = (d1 + d2 + rest).lower()
                    if len(want) != hexlen or not re.fullmatch(r"[0-9a-f]+", want):
                        continue
                    try:
                        h, st = digest(os.path.join(p2, rest), algo)
                    except OSError:
                        SKIPPED["vanished"] += 1
                        continue
                    if h is None:
                        continue
                    checked += 1
                    if h.hexdigest() != want:
                        bad.append({"cache": "npm", "key": f"{algo}:{want}", "nlink": st.st_nlink,
                                    "size": st.st_size, "mtime": int(st.st_mtime), "inplace": None})
    return checked, bad


def find_links(bad_inodes):
    """{(dev, ino): [(kind, name, rel)]} for the given bad inodes only, in ONE walk of the link roots
    plus the cached uv ENVIRONMENTS (archive-v0/<id> dirs holding a pyvenv.cfg: uvx / `uv run --with`
    envs, hardlinked from wheel archives). Only files with nlink > 1 are even lstat-matched."""
    out = {k: set() for k in bad_inodes}
    if not bad_inodes:
        return out
    roots = [(name, root) for name, root in LINK_ROOTS.items() if os.path.isdir(root)]
    abase = os.path.join(UV_DIR, "archive-v0")
    for eid in listdir(abase) if os.path.isdir(abase) else []:
        ed = os.path.join(abase, eid)
        if UV_KEY.match(eid) and not os.path.islink(ed) and os.path.isfile(os.path.join(ed, "pyvenv.cfg")):
            roots.append(("uvenv:" + eid, ed))
    for name, root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                p = os.path.join(dirpath, f)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                key = (st.st_dev, st.st_ino)
                if st.st_nlink < 2 or key not in out:
                    continue
                if name.startswith("uvenv:"):
                    out[key].add(("uv", name.split(":", 1)[1], ""))
                    continue
                target = venv_root_for(p, root, name)
                if target:
                    rel = os.path.relpath(target, root)
                    if safe_rel(rel):
                        out[key].add(("venv", name, rel))
                    else:
                        SKIPPED["unsafe_link_paths"] = SKIPPED.get("unsafe_link_paths", 0) + 1
    return out


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
    linked = {}   # (dev, ino) -> entries whose corrupt inode is shared
    base = os.path.join(UV_DIR, "archive-v0")
    for aid in listdir(base) if os.path.isdir(base) else []:
        adir = os.path.join(base, aid)
        try:
            if not UV_KEY.match(aid) or os.path.islink(adir) or not os.path.isdir(adir):
                continue
            records = [os.path.join(adir, e, "RECORD") for e in listdir(adir) if e.endswith(".dist-info")]
            records = [r for r in records if os.path.isfile(r) and not os.path.islink(r)]
            if not records:
                no_record += 1
                continue
            dir_ctime = os.stat(adir).st_ctime
        except OSError:
            SKIPPED["vanished"] += 1
            continue
        for rec in records:
            fd = open_regular(rec)
            rows = None
            if fd is not None:
                try:
                    rows = list(csv.reader(io.StringIO(os.read(fd, MAX_STATE_BYTES).decode("utf-8"))))
                except (OSError, UnicodeDecodeError, csv.Error):
                    rows = None
                finally:
                    os.close(fd)
            if rows is None:
                if os.path.lexists(rec):   # vanished mid-walk is a skip; present but unreadable is corrupt
                    bad.append({"cache": "uv", "key": aid, "file": os.path.relpath(rec, adir), "nlink": 0,
                                "size": 0, "mtime": 0, "inplace": False, "reason": "unreadable RECORD"})
                continue
            for row in rows:
                if len(row) < 2 or not row[1].startswith("sha256=") or not safe_rel(row[0]):
                    continue  # RECORD itself, .pyc and other unhashed lines; never a path that escapes
                path = os.path.join(adir, row[0])
                try:
                    h, st = digest(path, "sha256")
                except OSError:
                    SKIPPED["vanished"] += 1
                    continue
                if h is None:
                    if not os.path.lexists(adir):
                        SKIPPED["vanished"] += 1
                        break
                    bad.append({"cache": "uv", "key": aid, "file": row[0], "nlink": 0, "size": 0, "mtime": 0,
                                "inplace": False, "reason": "missing or not a regular file"})
                    continue
                checked += 1
                if base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode() == row[1][7:]:
                    continue
                # mtime, not ctime: link()/unlink() of another hardlink also bumps ctime, and every uv
                # install hardlinks from the archive. Only a content write moves mtime past the unpack.
                entry = {"cache": "uv", "key": aid, "file": row[0], "nlink": st.st_nlink, "size": st.st_size,
                         "mtime": int(st.st_mtime), "inplace": bool(st.st_mtime > dir_ctime + 1),
                         "reason": "hash mismatch"}
                if st.st_nlink > 1:
                    linked.setdefault((st.st_dev, st.st_ino), []).append(entry)
                bad.append(entry)
    for key, hits in find_links(set(linked)).items():
        for entry in linked[key]:
            entry["links"] = sorted(h for h in hits if not (h[0] == "uv" and h[1] == entry["key"]))
    return checked, bad, no_record


def queue_lines(bad):
    lines = []
    for b in bad:
        if b["cache"] == "npm":
            lines.append(f"npm {b['key']}")
        else:
            lines.append(f"uv {b['key']}")
            for kind, name, rel in b.get("links", []):
                lines.append(f"uv {name}" if kind == "uv" else f"venv {name} {rel}")
    return list(dict.fromkeys(lines))


def cmd_verify():
    t0 = time.time()
    n_checked, n_bad = verify_npm()
    u_checked, u_bad, no_record = verify_uv()
    bad = n_bad + u_bad
    os.makedirs(STATE_DIR, exist_ok=True)
    new = queue_lines(bad)
    if new:
        fd = os.open(QUEUE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "r+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            have = {l.strip() for l in fh.read().splitlines() if l.strip()}
            add = [l for l in new if l not in have]   # a deferred purge must not grow the queue nightly
            if add:
                fh.seek(0, os.SEEK_END)
                fh.write("\n".join(add) + "\n")
    result = {"finished": int(time.time()), "duration": round(time.time() - t0, 1),
              "checked": {"npm": n_checked, "uv": u_checked}, "uv_no_record": no_record,
              "skipped_vanished": SKIPPED["vanished"],
              "found": {"npm": len(n_bad), "uv": len({b["key"] for b in u_bad})},
              "inplace": {"npm": 0, "uv": sum(1 for b in u_bad if b["inplace"])},
              "bad": bad[:200]}
    tmp = RESULT + f".{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    os.replace(tmp, RESULT)
    for b in bad:
        print(f"corrupt {b['cache']} {b['key']} {b.get('file', '')} ({b.get('reason', 'hash mismatch')})"
              f" nlink={b['nlink']} inplace={b['inplace']} links={b.get('links', [])}", flush=True)
    print(f"verified npm={n_checked} uv={u_checked} bad_npm={len(n_bad)} bad_uv_files={len(u_bad)}"
          f" uv_no_record={no_record} skipped_vanished={SKIPPED['vanished']} in {result['duration']}s", flush=True)
    return 0


# ---------------------------------------------------------------------------------------------- apply
def parse_queue_line(line):
    """-> (kind, path-or-id) or None. npm/uv paths are built from fixed roots and a validated digest or
    archive id; a venv entry's relative path comes from the line, so it must pass safe_rel, resolve
    strictly inside its fixed root, and really be a venv (pyvenv.cfg) or a pytest-N session dir."""
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


def uv_pointer_files(ids):
    """{archive id: [non-archive uv cache files that name it]} in ONE walk for all ids (wheel index
    pointers, whatever their bucket version). Version-agnostic on purpose: the host uv is older than
    the setup-uv versions that write this cache, so `uv cache clean` could miss newer buckets."""
    hits = {i: [] for i in ids}
    if not ids or not os.path.isdir(UV_DIR):
        return hits
    needles = {i: i.encode() for i in ids}
    for top in listdir(UV_DIR):
        tp = os.path.join(UV_DIR, top)
        if top.startswith("archive-") or top.startswith(".") or os.path.islink(tp) or not os.path.isdir(tp):
            continue  # never walk through a symlinked bucket
        for dirpath, _dirs, files in os.walk(os.path.join(UV_DIR, top)):
            for f in files:
                p = os.path.join(dirpath, f)
                fd = open_regular(p)
                if fd is None:
                    continue
                try:
                    if os.fstat(fd).st_size > MAX_POINTER_BYTES:
                        continue
                    data = os.read(fd, MAX_POINTER_BYTES)
                finally:
                    os.close(fd)
                for i, n in needles.items():
                    if n in data:
                        hits[i].append(p)
    return hits


def cmd_apply():
    owner = runner_uid()
    fd = open_regular(QUEUE, os.O_RDWR, owner=owner) if owner is not None else None
    if fd is None:   # absent, a symlink, not a regular file, or not runner's: never touched as root
        if os.path.lexists(QUEUE):
            print("refuse: purge queue is not a regular file owned by the runner user", flush=True)
        write_beacon()
        return 0
    with os.fdopen(fd, "r+", encoding="utf-8", errors="replace") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        lines = list(dict.fromkeys(l.strip() for l in fh.read(MAX_STATE_BYTES).splitlines() if l.strip()))
        parsed = []
        for line in lines:
            p = parse_queue_line(line)
            if p is None:
                print(f"reject queue line {line[:120]!r}", flush=True)
            else:
                parsed.append((line, p))
        pointers = uv_pointer_files([what for _l, (kind, what) in parsed if kind == "uv"])
        keep = []
        for line, (kind, what) in parsed:
            targets = ([os.path.join(UV_DIR, "archive-v0", what)] + pointers.get(what, [])) if kind == "uv" else [what]
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
def pending_counts(owner):
    counts = {"npm": 0, "uv": 0}
    text = read_state_file(QUEUE, owner) if owner is not None else None
    for line in dict.fromkeys((text or "").splitlines()):
        parts = line.split()
        if parts and parts[0] in counts:
            counts[parts[0]] += 1
    return counts


def write_beacon():
    if not BEACON or not os.path.isdir(TEXTFILE_DIR):
        return
    owner = runner_uid()
    try:
        text = read_state_file(RESULT, owner) if owner is not None else None
        r = json.loads(text) if text else None
        last = int(r["finished"])
        dur = float(r["duration"])
        checked = {k: int(r["checked"][k]) for k in ("npm", "uv")}
        found = {k: int(r["found"][k]) for k in ("npm", "uv")}
        inplace = {k: int(r["inplace"][k]) for k in ("npm", "uv")}
        if not all(math.isfinite(x) and x >= 0 for x in [last, dur, *checked.values(), *found.values(), *inplace.values()]):
            return
    except (ValueError, KeyError, TypeError, OverflowError):
        return  # no (valid) verify result yet: publish nothing, so the beacon-missing alert can fire
    pend = pending_counts(owner)
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
    g("corrupt_inplace_evidence", "Corrupt uv files whose content changed after unpack (a job wrote through a hardlink).",
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
