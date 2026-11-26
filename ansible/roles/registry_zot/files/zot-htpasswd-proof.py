#!/usr/bin/env python3
"""Prove that the running zot has loaded the current htpasswd file, so an htpasswd change never needs a restart.

A zot restart re-parses the whole store before :443 binds again (2026-11-17: a 19 min registry outage and ~265 GB
written to the host NVMe). zot v2.1.23 does not need one for htpasswd: pkg/api/htpasswd.go runs an HTPasswdWatcher
whenever htpasswd auth is enabled (fsnotify on the file and its directory, plus a 1 s stat-fingerprint poll as a
backstop). Each reload, and each start, logs {"message": "loaded htpasswd file", "users": <n>, "time": <RFC 3339>}
and swaps the credentials atomically; a failed open logs "failed to reload htpasswd" and keeps the old ones.
Measured 2026-11-26 on the v2.1.23 binary: a user added by an in-place write and by temp+rename logged in within 1 s,
same PID; `htpasswd -D` reloaded and the removed user got 401; rewriting the file's own bytes in place reloaded it.

  snapshot --unit U --file F
      Prints {"pid": <MainPID or 0>, "cursor": <journal cursor or null>, "timeNs": <now>, "sha256": <F's digest or
      null>}: the state BEFORE the htpasswd tasks.
  prove --unit U --file F --pid P --cursor C --since-ns T --before-sha256 H [--timeout S]
      Run at the END of the play, after the handlers (so any restart the play queued has happened).
      - zot is a different process than P (restarted, or first started, during the play): "restarted" at once. A new
        zot loads F in Controller.Init only after the store parse (InitMetaDB, up to ~19 min) and watches it from then
        on, so it cannot hold stale credentials; it is not serving yet, so the caller skips its login checks.
      - still process P: it proves P loaded F as it is now: the LATEST reload outcome zot logged (after cursor C,
        else since T) at or after F's last modification must be a load with F's user count, the MainPID unchanged
        while it waits (S seconds). When F's content is unchanged (digest H) there is no fresh load to judge, and an
        earlier unproven write (a removal, say) could still be pending, so it first rewrites F's own bytes in place
        (same inode, same length, identical content at every instant) to make zot reload, then judges that reload.
        A "failed to reload htpasswd" line does not end the wait: a later success wins.
      Prints {"mode": "reloaded"|"reloaded-forced"|"restarted", ...} and exits 0, or {"mode": "unproven",
      "reason": ...} and exits 2.
A matching line shows that zot loaded the file after its last write, not which bytes it read (a reload that began
before a same-count rotation can log after it): the role's login checks are the ground truth for added and rotated
users. It prints counts, times and PIDs only, never a user name, hash or file digest.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time

MSG_LOADED = "loaded htpasswd file"
MSG_FAILED = "failed to reload htpasswd"


def _run(*cmd):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")


def main_pid(unit):
    out = _run("systemctl", "show", "-p", "MainPID", "--value", unit).stdout.strip()
    return int(out) if out.isdigit() else 0


def journal_cursor(unit):
    proc = _run("journalctl", "-u", unit, "-n", "0", "--show-cursor", "-q")
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("-- cursor: "):
            return line[len("-- cursor: "):].strip() or None
    return None


def journal_records(unit, cursor, since_ns):
    """zot's JSON log records after `cursor`, or (no cursor: e.g. the unit had never logged) since `since_ns`.
    [] when journalctl fails: nothing can then be proven."""
    where = [f"--after-cursor={cursor}"] if cursor else [f"--since=@{since_ns // 1_000_000_000}"]
    proc = _run("journalctl", "-u", unit, "-o", "cat", "--no-pager", *where)
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def file_sha256(path):
    """The content digest of `path`, or None when it cannot be read (e.g. not created yet)."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def force_reload(path):
    """Rewrite `path`'s own bytes in place: same inode, same length, so the content is identical at every instant.
    zot's watcher sees the WRITE (and the new mtime) and reloads the file."""
    with open(path, "r+b") as f:
        data = f.read()
        f.seek(0)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def count_users(text):
    """The number of users zot's HTPasswd.Reload loads from `text`: distinct names of lines with a ':'."""
    return len({line.split(":", 1)[0] for line in text.splitlines() if ":" in line})


def parse_time_ns(value):
    """zot's RFC 3339 'time' (nanosecond fraction, Z or offset) as epoch nanoseconds, or None."""
    if not isinstance(value, str) or "T" not in value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Split off the zone, keep up to 9 fraction digits as an integer: datetime only takes 6.
    zone_at = max(s.rfind("+"), s.rfind("-"))
    if zone_at <= s.index("T"):
        return None
    main, zone = s[:zone_at], s[zone_at:]
    frac_ns = 0
    if "." in main:
        main, frac = main.split(".", 1)
        if not frac.isdigit():
            return None
        frac_ns = int((frac + "000000000")[:9])
    try:
        base = dt.datetime.fromisoformat(main + zone)
    except ValueError:
        return None
    return int(base.timestamp()) * 1_000_000_000 + frac_ns


def verdict(records, users, mtime_ns):
    """Judge the LATEST reload outcome zot logged at or after the file's last write (journal order). Returns
    ("reloaded", detail) when it is a 'loaded' record with the file's user count; ("failed", detail) when it is a
    failed reload; else ("pending", detail): nothing yet, or a load with another count (an earlier write's)."""
    latest = None
    for rec in records:
        msg = rec.get("message", "")
        t = parse_time_ns(rec.get("time"))
        if t is None or t < mtime_ns or msg not in (MSG_LOADED, MSG_FAILED):
            continue
        latest = rec
    if latest is None:
        return "pending", {}
    if latest.get("message") == MSG_FAILED:
        return "failed", {"failedAt": latest.get("time")}
    if latest.get("users") == users:
        return "reloaded", {"users": users, "loadedAt": latest.get("time")}
    return "pending", {"lastLoadedUsers": latest.get("users")}


def prove(unit, path, pid_before, cursor, since_ns, before_sha256, timeout, sleep=time.sleep, clock=time.time):
    pid = main_pid(unit)
    if pid <= 0:
        return 2, {"mode": "unproven", "reason": "zot is not running"}
    if not cursor and not since_ns:
        return 2, {"mode": "unproven", "reason": "no journal position from the snapshot"}
    if pid != pid_before:
        # A zot (re)started during the play loads the file in Controller.Init only AFTER InitMetaDB (the store parse,
        # up to ~19 min), and then watches it: it can only ever hold the file as it is from that point on, never
        # stale credentials. Nothing to wait for here; it is not serving yet, so the caller skips the logins.
        return 0, {"mode": "restarted", "pid": pid, "pidBefore": pid_before}
    forced = False
    if before_sha256 and file_sha256(path) == before_sha256:
        try:
            force_reload(path)
        except OSError as e:
            return 2, {"mode": "unproven", "reason": f"cannot rewrite the htpasswd file to force a reload: {e.strerror}"}
        forced = True
    deadline = clock() + timeout
    while True:
        try:
            st = os.stat(path)
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            return 2, {"mode": "unproven", "reason": f"cannot read the htpasswd file: {e.strerror}"}
        users = count_users(text)
        now_pid = main_pid(unit)
        if now_pid != pid:
            return 2, {"mode": "unproven", "reason": f"zot MainPID changed during the check ({pid} -> {now_pid})"}
        state, detail = verdict(journal_records(unit, cursor, since_ns), users, st.st_mtime_ns)
        if state == "reloaded":
            mode = "reloaded-forced" if forced else "reloaded"
            return 0, {"mode": mode, "pid": pid, "pidBefore": pid_before, **detail}
        if clock() >= deadline:
            if state == "failed":
                return 2, {"mode": "unproven", "reason": "zot logged a failed htpasswd reload and no later success",
                           **detail}
            return 2, {"mode": "unproven", "users": users, **detail,
                       "reason": f"no 'loaded htpasswd file' line with {users} users after the last write within {timeout}s"}
        sleep(1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--unit", required=True)
    s.add_argument("--file", required=True)
    p = sub.add_parser("prove")
    p.add_argument("--unit", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--cursor", default="")
    p.add_argument("--since-ns", type=int, default=0)
    p.add_argument("--before-sha256", default="")
    p.add_argument("--timeout", type=int, default=30)
    a = ap.parse_args(argv)
    if a.cmd == "snapshot":
        print(json.dumps({"pid": main_pid(a.unit), "cursor": journal_cursor(a.unit), "timeNs": time.time_ns(),
                          "sha256": file_sha256(a.file)}))
        return 0
    rc, out = prove(a.unit, a.file, a.pid, a.cursor, a.since_ns, a.before_sha256, a.timeout)
    print(json.dumps(out))
    return rc


if __name__ == "__main__":
    sys.exit(main())
