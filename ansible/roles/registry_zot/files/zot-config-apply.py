#!/usr/bin/env python3
"""Apply a rendered zot config: hot-reload it in place when only reload-safe keys changed, else ask for a restart.

Why: a zot restart re-parses the whole store before it listens again. On 2026-11-17 that was a 19 min registry
outage and ~265 GB written to ai-node1's worn NVMe (BoltDB metaDB rebuild). zot v2.1.x can reload part of its config
live, but only under narrow conditions, verified in the v2.1.18 source:

- pkg/cli/server/config_reloader.go watches the config FILE (not its directory) and acts only on fsnotify WRITE.
  Rename/Remove are ignored, so an atomic replace (what Ansible's `template` does) silently detaches the watch until
  the next restart. A reload therefore has to write the SAME inode.
- It logs "config file changed, trying to reload config" on every WRITE event, "failed to reload config, retry
  writing it." when the file did not load, and "loaded new configuration settings" when it did (seen live).
- pkg/api/config/config.go `UpdateReloadableConfig` copies only some fields. This helper allows a deliberately SMALL
  subset of them (RELOADABLE below); any other difference means a restart.

Usage (as root, from the registry_zot role):
  zot-config-apply.py --current /etc/zot/config.json --candidate /etc/zot/config.json.candidate \\
      [--unit zot.service] [--port 443]
Prints one JSON line {"mode": "none"|"reload"|"restart", "paths": [...], "reason": "..."} and exits 0, or exits 2
when a reload could not be proven (zot keeps its old in-memory config; the play must stop). It prints key PATHS,
never values: config.json embeds secrets.
"""
import argparse
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# Dotted key paths whose changes zot v2.1.x applies live (UpdateReloadableConfig). A changed path is reload-safe if it
# equals one of these or sits below one. Kept minimal on purpose: auth, dedupe, gc on/off, sync, scrub and
# everything else restart.
RELOADABLE = (
    "storage.retention",  # only when retention is already active on both sides, see classify()
    "storage.gcDelay",
    "storage.gcInterval",
    "http.accessControl.repositories",
    "http.accessControl.adminPolicy",
    "http.accessControl.groups",
    "http.accessControl.metrics",
)
MSG_CHANGED = "config file changed, trying to reload config"
MSG_FAILED = "failed to reload config, retry writing it."
# Logged by LoadNewConfig after a successful reload (observed live on v2.1.18, 2026-11-18, after an in-place rewrite).
MSG_LOADED = "loaded new configuration settings"


def diff_paths(old, new, prefix=""):
    """Sorted dotted paths where `old` and `new` differ. Lists compare whole; a key on one side only is reported."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for key in sorted(set(old) | set(new)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in old or key not in new:
                out.append(path)
            else:
                out.extend(diff_paths(old[key], new[key], path))
        return out
    return [] if old == new else [prefix or "<root>"]


_TAG_RULES = ("mostRecentlyPulledCount", "mostRecentlyPushedCount", "pulledWithin", "pushedWithin")


def _retention_active(cfg):
    """zot v2.1.18 `isRetentionEnabledInternal`: some keepTags entry (top-level or a subPath's policies) sets a
    count or age rule. deleteUntagged/deleteReferrers do not count. UpdateReloadableConfig copies Retention only
    when this holds for the RUNNING config; a pattern-only policy would 'reload' while retention is ignored."""
    storage = cfg.get("storage") or {}
    retentions = [storage.get("retention") or {}]
    retentions += [(sp or {}).get("retention") or {} for sp in (storage.get("subPaths") or {}).values()]
    for retention in retentions:
        for policy in retention.get("policies") or []:
            for keep in policy.get("keepTags") or []:
                if any(keep.get(rule) not in (None, 0, "") for rule in _TAG_RULES):
                    return True
    return False


def classify(current, candidate):
    """Return (mode, paths, reason) for applying `candidate` over `current` (both parsed JSON, current may be None)."""
    if current is None:
        return "restart", [], "no deployed config yet"
    paths = diff_paths(current, candidate)
    if not paths:
        return "none", [], "unchanged"
    unsafe = [p for p in paths if not any(p == r or p.startswith(r + ".") for r in RELOADABLE)]
    if unsafe:
        return "restart", paths, "not reload-safe: " + ", ".join(unsafe)
    if any(p == "storage.retention" or p.startswith("storage.retention.") for p in paths):
        if not (_retention_active(current) and _retention_active(candidate)):
            return "restart", paths, "retention is only reloaded while it is already active on both sides"
    return "reload", paths, "only reload-safe keys changed"


def parse_inotify_fdinfo(text):
    """(ino, sdev) pairs from a /proc/<pid>/fdinfo/<fd> of an inotify fd (both ints, parsed from hex)."""
    pairs = []
    for line in text.splitlines():
        if not line.startswith("inotify "):
            continue
        fields = dict(part.split(":", 1) for part in line.split()[1:] if ":" in part)
        try:
            pairs.append((int(fields["ino"], 16), int(fields["sdev"], 16)))
        except (KeyError, ValueError):
            continue
    return pairs


def kernel_sdev(st_dev):
    """The kernel's s_dev encoding (major << 20 | minor), as printed in inotify fdinfo."""
    return (os.major(st_dev) << 20) | os.minor(st_dev)


def watch_alive(pid, path):
    """True when process `pid` holds an inotify watch on the inode `path` points at now."""
    st = os.stat(path)
    want = (st.st_ino, kernel_sdev(st.st_dev))
    fd_dir = f"/proc/{pid}/fd"
    for fd in os.listdir(fd_dir):
        try:
            if os.readlink(os.path.join(fd_dir, fd)) != "anon_inode:inotify":
                continue
            with open(f"/proc/{pid}/fdinfo/{fd}") as fh:
                if want in parse_inotify_fdinfo(fh.read()):
                    return True
        except OSError:
            continue
    return False


def reload_verdict(messages):
    """Judge the reload attempts in `messages` (zot journal lines after our write), by the LAST attempt only:
    'ok' = a LOADED after the last CHANGED and no FAILED; 'failed' = a FAILED after it; 'pending' = CHANGED but
    neither yet; 'none' = zot has not reacted at all."""
    relevant = [m for m in messages if m in (MSG_CHANGED, MSG_FAILED, MSG_LOADED)]
    if MSG_CHANGED not in relevant:
        return "none"
    last = len(relevant) - 1 - relevant[::-1].index(MSG_CHANGED)
    tail = relevant[last + 1:]
    if MSG_FAILED in tail:
        return "failed"
    return "ok" if MSG_LOADED in tail else "pending"


def write_in_place(path, data):
    """Overwrite `path` keeping its inode (zot's watch). One write at offset 0, then truncate, then fsync."""
    fd = os.open(path, os.O_WRONLY)
    try:
        done = 0
        while done < len(data):  # pwrite may write less than asked
            n = os.pwrite(fd, data[done:], done)
            if n <= 0:
                raise OSError(f"short write to {path} at {done}/{len(data)} bytes")
            done += n
        os.ftruncate(fd, len(data))
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path, data, like=None):
    """Replace `path` atomically, keeping the owner/mode of `like` (default: the current file, if any)."""
    ref = like or path
    st = os.stat(ref) if os.path.exists(ref) else None
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".zot-config-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if st is not None:
            os.chown(tmp, st.st_uid, st.st_gid)
            os.chmod(tmp, st.st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _match_attributes(path, like):
    """Give `path` the owner/group/mode of `like` (the rendered candidate), so role changes to them converge."""
    st = os.stat(like)
    os.chown(path, st.st_uid, st.st_gid)
    os.chmod(path, st.st_mode & 0o7777)


def _touch_marker(path):
    """Root-only marker of an apply that is not proven yet (no secrets in it)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, f"{int(time.time())}\n".encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def _run(*cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)


def main_pid(unit):
    out = _run("systemctl", "show", "-p", "MainPID", "--value", unit).stdout.strip()
    return int(out) if out.isdigit() else 0


def journal_cursor(unit):
    """The journal position to read zot's reaction from, or None. Without one, a reload cannot be proven
    (an older CHANGED/LOADED pair could be mistaken for this one), so the caller must not write."""
    proc = _run("journalctl", "-u", unit, "-n", "0", "--show-cursor", "-q")
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("-- cursor: "):
            return line[len("-- cursor: "):].strip() or None
    return None


def journal_messages(unit, cursor):
    """zot's log messages after `cursor` ([] if journalctl fails: the verdict then stays 'none'/'pending')."""
    proc = _run("journalctl", "-u", unit, "-o", "cat", "--no-pager", f"--after-cursor={cursor}")
    if proc.returncode != 0:
        return []
    msgs = []
    for line in proc.stdout.splitlines():
        try:
            msgs.append(json.loads(line).get("message", ""))
        except ValueError:
            continue
    return msgs


def http_alive(port):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # localhost probe; the cert names registry.chifor.me
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://127.0.0.1:{port}/v2/"), context=ctx, timeout=10)
        return True
    except urllib.error.HTTPError as err:
        return err.code < 500
    except OSError:
        return False


def apply_reload(args, data):
    """Write in place and prove zot reloaded it. Returns (ok, reason)."""
    pid = main_pid(args.unit)
    if not pid or not watch_alive(pid, args.current):
        return None, "zot is not running or no longer watches this config inode (replaced since its start)"
    for attempt in (1, 2):
        cursor = journal_cursor(args.unit)
        if cursor is None:
            return False, "cannot read zot's journal position; a reload could not be proven (nothing written)"
        write_in_place(args.current, data)
        deadline = time.time() + args.wait
        verdict = "none"
        while time.time() < deadline:
            time.sleep(1)
            verdict = reload_verdict(journal_messages(args.unit, cursor))
            if verdict == "ok":
                # Settle: a later event (e.g. the truncate after the write) must also end in LOADED. A FAILED is
                # NOT final either: zot may have read the file between pwrite and ftruncate, then reload cleanly.
                time.sleep(args.settle)
                verdict = reload_verdict(journal_messages(args.unit, cursor))
                if verdict == "ok":
                    break
        if verdict == "ok":
            break
        if attempt == 2:
            return False, f"zot did not reload the config ({verdict}); it keeps the previous one in memory"
    if main_pid(args.unit) != pid:
        return False, "zot restarted during the reload (PID changed)"
    if not http_alive(args.port):
        return False, "zot reloaded but /v2/ does not answer"
    return True, "reloaded in place (same PID, /v2/ answers)"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--current", required=True)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--unit", default="zot.service")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--wait", type=int, default=30, help="seconds to wait for zot's reload log line")
    ap.add_argument("--settle", type=int, default=5, help="seconds to watch for a later failure line")
    ap.add_argument("--dry-run", action="store_true", help="classify only (Ansible check mode); write nothing")
    args = ap.parse_args(argv)

    with open(args.candidate, "rb") as fh:
        data = fh.read()
    candidate = json.loads(data)
    current = None
    if os.path.exists(args.current):
        with open(args.current, "rb") as fh:
            current = json.loads(fh.read())

    mode, paths, reason = classify(current, candidate)
    # An earlier apply that was never proven (failed reload, or the helper was killed mid-apply) leaves this marker.
    # Disk equality is then NOT convergence: zot may still run an older config, so re-prove the file on disk.
    marker = args.current + ".unconfirmed"
    if os.path.exists(marker) and mode == "none":
        mode, reason = "reload", "a previous apply was not confirmed; re-proving the config on disk"
    if args.dry_run:
        if mode == "reload":
            pid = main_pid(args.unit)
            if not pid or not watch_alive(pid, args.current):
                mode, reason = "restart", "zot is not running or no longer watches this config inode"
        os.unlink(args.candidate)
        print(json.dumps({"mode": mode, "paths": paths, "reason": reason, "dry_run": True}))
        return 0
    if mode in ("reload", "restart"):
        # Marker first: if anything after this point fails (reload unproven, helper killed, a later task failing
        # before the role's `restart zot` handler runs), the next run re-proves instead of answering "none".
        _touch_marker(marker)
    if mode == "reload":
        with open(args.current, "rb") as fh:
            previous = fh.read()
        ok, why = apply_reload(args, data)
        if ok is None:  # the watch is gone: a restart is the only way to apply it
            mode, reason = "restart", why
        elif not ok:
            # Put the previous bytes back so the file again matches what zot most likely runs, and keep the marker:
            # the next run must not mistake disk equality for convergence.
            with open(args.current, "rb") as fh:
                if fh.read() != previous:
                    write_in_place(args.current, previous)
            print(json.dumps({"mode": "reload", "paths": paths,
                              "reason": why + "; previous config restored on disk, marker kept"}))
            return 2
        else:
            reason = why
    if mode == "restart":
        # The marker STAYS: the role's `restart zot` handler chain removes it only after zot restarted on this file.
        write_atomic(args.current, data, like=args.candidate)  # the candidate carries the role's owner/mode
    if mode == "reload":
        _match_attributes(args.current, args.candidate)  # chown/chmod emit no WRITE: no second reload
        if os.path.exists(marker):
            os.unlink(marker)  # proven reload
    os.unlink(args.candidate)
    print(json.dumps({"mode": mode, "paths": paths, "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
