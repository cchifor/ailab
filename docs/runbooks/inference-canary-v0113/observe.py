#!/usr/bin/env python3
"""Read only the fixed canary marker files; never execute the printed commands."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

PROFILES = {
    "codex": "1d9a7d6e-4b26-49fd-9053-7b0a22e1baae",
    "qwen": "f25c187b-1e50-4c7c-860e-52947bfdf39e",
    "claude": "fdb56ca5-908b-40ee-94b3-aac735dd8622",
}
ROOT = Path("/home/c4/.local/state/relay-holder/workspaces")
PHASES = ("baseline", "denied", "approved", "recovered")


def run_id(value):
    try:
        if str(uuid.UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("Use one canonical lowercase UUID for this acceptance run")


def file_evidence(directory_fd, name, expected, uid):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except FileNotFoundError:
        return {"state": "absent"}
    except OSError:
        return {"state": "unsafe-or-unreadable"}
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != uid or s.st_nlink != 1 or stat.S_IMODE(s.st_mode) != 0o600:
            return {"state": "unsafe-or-unreadable"}
        if s.st_size > 4096:
            return {"state": "oversized"}
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(4097)
        if len(data) > 4096:
            return {"state": "oversized"}
        return {"state": "present", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "exactlyOneMarker": data == expected, "markerOccurrences": data.count(expected)}
    finally:
        os.close(fd)


def observe(root, provider, run, phase, uid):
    """root/uid are injectable for offline tests; CLI fixes both to the canary."""
    try:
        run_id(run)
    except argparse.ArgumentTypeError as exc:
        raise ValueError("Invalid observation selection") from exc
    if provider not in PROFILES or phase not in PHASES:
        raise ValueError("Invalid observation selection")
    marker = "relay-pi-" + provider + "-" + run
    names = {"denied": "acceptance-" + run + ".denied", "effects": "acceptance-" + run + ".effects"}
    report = {"version": 1, "observedUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "provider": provider, "profileId": PROFILES[provider], "runId": run, "phase": phase,
              "marker": marker,
              "scope": "Local marker effects only; verify hub permissions, router receipts and native history separately."}
    # Fixed CLI roots contain no shell metacharacters; refuse substituted paths too.
    if not root.is_absolute() or root.resolve() != root or any(c in str(root) for c in "'\n\r"):
        report.update(state="unsafe-workspace", phaseChecksPass=False)
        return report
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        report.update(state="workspace-unavailable", phaseChecksPass=False)
        return report
    try:
        s = os.fstat(fd)
        if s.st_uid != uid or stat.S_IMODE(s.st_mode) != 0o700:
            report.update(state="unsafe-workspace", phaseChecksPass=False)
            return report
        files = {k: file_evidence(fd, name, (marker + "\n").encode(), uid) for k, name in names.items()}
    finally:
        os.close(fd)
    report["commands"] = {
        k: "umask 077; printf '%s\\n' '" + marker + "' >> '" + str(root / name) + "'"
        for k, name in names.items()}
    expected_effect = files["effects"]["state"] == "absent" if phase in ("baseline", "denied") else files["effects"].get("exactlyOneMarker") is True
    report.update(state="observed", files=files,
                  phaseChecksPass=files["denied"]["state"] == "absent" and expected_effect)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=PROFILES)
    parser.add_argument("run", type=run_id)
    parser.add_argument("phase", choices=PHASES)
    args = parser.parse_args()
    if os.getuid() != 1000:
        parser.error("Run as the canary execution user, UID 1000")
    report = observe(ROOT / PROFILES[args.provider], args.provider, args.run, args.phase, 1000)
    print(json.dumps(report))
    return 0 if report["phaseChecksPass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
