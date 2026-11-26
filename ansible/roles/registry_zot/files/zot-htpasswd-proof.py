#!/usr/bin/env python3
"""Prove that a running zot reloaded its htpasswd file in place, so an htpasswd change never needs a restart.

A zot restart re-parses the whole store before :443 binds again (2026-11-17: a 19 min registry outage and ~265 GB
written to the host NVMe). zot v2.1.23 does not need one for htpasswd: pkg/api/htpasswd.go runs an HTPasswdWatcher
whenever htpasswd auth is enabled (fsnotify on the file and its directory, plus a 1 s stat-fingerprint poll as a
backstop). Each reload logs {"message": "loaded htpasswd file", "users": <n>, "time": <RFC 3339>} and swaps the
credentials atomically; a failed open logs "failed to reload htpasswd" and keeps the old ones. Measured 2026-11-26 on
the v2.1.23 binary: a user added by an in-place write and by temp+rename could log in within 1 s, same PID.

  snapshot --unit U
      Prints {"pid": <MainPID or 0>, "cursor": <journal cursor or null>}: the state BEFORE the htpasswd tasks.
  prove --unit U --file F --pid P --cursor C [--timeout S]
      Waits up to S seconds for a "loaded htpasswd file" line in U's journal AFTER cursor C whose time is not earlier
      than F's last modification and whose user count equals F's; and checks that U's MainPID is still P.
      Prints {"mode": "reloaded", ...} and exits 0, or {"mode": "unproven", "reason": ...} and exits 2.
It prints counts and times only, never a user name or hash.
"""
import argparse
import datetime as dt
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


def journal_records(unit, cursor):
    """zot's JSON log records after `cursor` ([] when journalctl fails: nothing can then be proven)."""
    proc = _run("journalctl", "-u", unit, "-o", "cat", "--no-pager", f"--after-cursor={cursor}")
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
    """Judge the journal records after the cursor. Returns ("reloaded", detail) when one 'loaded' record is at or
    after the file's last write with the expected user count; ("failed", detail) when zot reported a failed reload
    after the last write and nothing loaded since; else ("pending", detail)."""
    last_failed = None
    for rec in records:
        msg = rec.get("message", "")
        t = parse_time_ns(rec.get("time"))
        if t is None or t < mtime_ns:
            continue
        if msg == MSG_LOADED and rec.get("users") == users:
            return "reloaded", {"users": users, "loadedAt": rec.get("time")}
        if msg == MSG_FAILED:
            last_failed = rec.get("time")
    if last_failed:
        return "failed", {"failedAt": last_failed}
    return "pending", {}


def prove(unit, path, pid, cursor, timeout, sleep=time.sleep, clock=time.time):
    if pid <= 0:
        return 2, {"mode": "unproven", "reason": "zot was not running before the change (nothing to prove)"}
    if not cursor:
        return 2, {"mode": "unproven", "reason": "no journal cursor from the snapshot"}
    deadline = clock() + timeout
    while True:
        try:
            st = os.stat(path)
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError as e:
            return 2, {"mode": "unproven", "reason": f"cannot read the htpasswd file: {e.strerror}"}
        users = count_users(text)
        now_pid = main_pid(unit)
        if now_pid != pid:
            return 2, {"mode": "unproven", "reason": f"zot MainPID changed ({pid} -> {now_pid}): a restart, not a reload"}
        state, detail = verdict(journal_records(unit, cursor), users, st.st_mtime_ns)
        if state == "reloaded":
            return 0, {"mode": "reloaded", "pid": pid, **detail}
        if state == "failed":
            return 2, {"mode": "unproven", "reason": "zot logged a failed htpasswd reload", **detail}
        if clock() >= deadline:
            return 2, {"mode": "unproven", "users": users,
                       "reason": f"no 'loaded htpasswd file' line with {users} users after the last write within {timeout}s"}
        sleep(1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--unit", required=True)
    p = sub.add_parser("prove")
    p.add_argument("--unit", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--cursor", required=True)
    p.add_argument("--timeout", type=int, default=30)
    a = ap.parse_args(argv)
    if a.cmd == "snapshot":
        print(json.dumps({"pid": main_pid(a.unit), "cursor": journal_cursor(a.unit)}))
        return 0
    rc, out = prove(a.unit, a.file, a.pid, a.cursor, a.timeout)
    print(json.dumps(out))
    return rc


if __name__ == "__main__":
    sys.exit(main())
