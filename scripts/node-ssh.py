#!/usr/bin/env python3
"""Run a command on a Proxmox node over SSH (key auth, fallback to NODE_ROOT_PASSWORD).

    python scripts/node-ssh.py 192.168.0.2 "ip -br addr"
    echo "uname -a" | python scripts/node-ssh.py 192.168.0.2
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
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if len(sys.argv) < 2:
        print("usage: node-ssh.py <host> '<command>'", file=sys.stderr)
        return 2
    host = sys.argv[1]
    cmd = " ".join(sys.argv[2:]).strip() or sys.stdin.read()
    env = load_env(REPO / ".env")
    import paramiko

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = dict(timeout=12, look_for_keys=False, allow_agent=False)
    try:
        pkey = paramiko.Ed25519Key.from_private_key_file(os.path.expanduser("~/.ssh/id_ed25519"))
        c.connect(host, username="root", pkey=pkey, **kw)
    except Exception:
        c.connect(host, username="root", password=env.get("NODE_ROOT_PASSWORD", ""), **kw)
    _in, out, err = c.exec_command(cmd, timeout=180)
    sys.stdout.write(out.read().decode(errors="replace"))
    sys.stderr.write(err.read().decode(errors="replace"))
    rc = out.channel.recv_exit_status()
    c.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
