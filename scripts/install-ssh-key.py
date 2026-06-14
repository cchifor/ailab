#!/usr/bin/env python3
"""One-time: install the local SSH public key onto the Proxmox nodes using password auth,
so all further access (Ansible/OpenTofu/discovery) is key-based.

Reads credentials from the gitignored .env at the repo root.

Usage:
    pip install paramiko
    python scripts/install-ssh-key.py
"""
import os
import pathlib
import sys


def load_env(path: pathlib.Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def main() -> int:
    repo = pathlib.Path(__file__).resolve().parents[1]
    env = load_env(repo / ".env")

    password = env.get("NODE_ROOT_PASSWORD") or os.environ.get("NODE_ROOT_PASSWORD", "")
    nodes = [n.strip() for n in env.get("PVE_NODES", "192.168.0.2,192.168.0.3,192.168.0.4").split(",") if n.strip()]

    pub = pathlib.Path(os.path.expanduser("~/.ssh/id_ed25519.pub"))
    if not pub.exists():
        print(f"ERROR: public key not found at {pub}", file=sys.stderr)
        return 2
    if not password:
        print("ERROR: NODE_ROOT_PASSWORD is empty in .env", file=sys.stderr)
        return 2
    pubkey = pub.read_text(encoding="utf-8").strip()

    try:
        import paramiko
    except ImportError:
        print("ERROR: paramiko not installed -> pip install paramiko", file=sys.stderr)
        return 2

    install = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        f"(grep -qxF '{pubkey}' ~/.ssh/authorized_keys 2>/dev/null || echo '{pubkey}' >> ~/.ssh/authorized_keys) && "
        "chmod 600 ~/.ssh/authorized_keys && echo INSTALLED"
    )

    rc = 0
    for host in nodes:
        print(f"==> {host}")
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            cli.connect(host, username="root", password=password, timeout=10,
                        look_for_keys=False, allow_agent=False)
        except Exception as e:  # noqa: BLE001
            print(f"   FAILED to connect: {e}")
            rc = 1
            continue
        _in, out, err = cli.exec_command(install)
        msg = (out.read().decode() + err.read().decode()).strip()
        print(f"   {msg or 'no output'}")
        # verify hostname over the same session
        _in, out, _err = cli.exec_command("hostname; pveversion 2>/dev/null | head -1")
        print(f"   {out.read().decode().strip()}")
        cli.close()

    print("\nDone. Verify key-only auth, e.g.:  ssh -o BatchMode=yes root@%s hostname" % nodes[0])
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
