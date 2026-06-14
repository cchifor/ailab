#!/usr/bin/env python3
"""Run a command on the QNAP over SSH using password auth from .env.

Read-only unless you pass mutating commands. Reusable for discovery and (later) the
storage-setup runbook automation.

    python scripts/qnap-ssh.py "zfs list"
    echo "zpool status" | python scripts/qnap-ssh.py
"""
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]


def load_env(p: pathlib.Path) -> dict:
    e = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                e[k.strip()] = v.strip()
    return e


def main() -> int:
    env = load_env(REPO / ".env")
    cmd = " ".join(sys.argv[1:]).strip() or sys.stdin.read()
    if not cmd.strip():
        print("usage: qnap-ssh.py '<command>'", file=sys.stderr)
        return 2
    # Substitute {{QNAP_USER}} / {{QNAP_PW}} from .env so secrets never appear on our cmdline.
    cmd = cmd.replace("{{QNAP_USER}}", env.get("QNAP_SSH_USER", ""))
    cmd = cmd.replace("{{QNAP_PW}}", env.get("QNAP_ADMIN_PASSWORD", ""))
    import paramiko

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        env.get("QNAP_SSH_HOST", "192.168.1.225"),
        username=env.get("QNAP_SSH_USER", "admin"),
        password=env.get("QNAP_ADMIN_PASSWORD", ""),
        timeout=12, look_for_keys=False, allow_agent=False,
    )
    _in, out, err = c.exec_command(cmd, timeout=180)
    sys.stdout.write(out.read().decode(errors="replace"))
    sys.stderr.write(err.read().decode(errors="replace"))
    rc = out.channel.recv_exit_status()
    c.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
