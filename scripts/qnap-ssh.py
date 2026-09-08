#!/usr/bin/env python3
"""Run a command on the QNAP over SSH using password auth from .env.

Read-only unless you pass mutating commands. Reusable for discovery and (later) the
storage-setup runbook automation.

    python scripts/qnap-ssh.py "zfs list"
    echo "zpool status" | python scripts/qnap-ssh.py
    python scripts/qnap-ssh.py --sudo "cat /etc/config/crontab"

The account in .env (QNAP_SSH_USER) is an ordinary user -- it cannot write outside its own
shares and cannot touch /etc/config -- so anything that manages system state needs --sudo.
The sudo password is written to the remote sudo process's STDIN over the encrypted channel,
never interpolated into the command string: a password in the command string would be visible
in the NAS process table to any other logged-in user.
"""
import pathlib
import shlex
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
    env = load_env(REPO / ".env")
    args = sys.argv[1:]
    use_sudo = "--sudo" in args
    if use_sudo:
        args = [a for a in args if a != "--sudo"]
    cmd = " ".join(args).strip() or sys.stdin.read()
    if not cmd.strip():
        print("usage: qnap-ssh.py [--sudo] '<command>'", file=sys.stderr)
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
    password = env.get("QNAP_ADMIN_PASSWORD", "")
    if use_sudo:
        # -S reads the password from stdin; -p '' suppresses the prompt so it never mixes into the
        # captured output. bash -c (not -lc) keeps the login profile out of the way -- the QNAP
        # profile prints a banner that would corrupt machine-readable output.
        cmd = "sudo -S -p '' /bin/bash -c " + shlex.quote(cmd)
    _in, out, err = c.exec_command(cmd, timeout=180)
    if use_sudo:
        _in.write(password + "\n")
        _in.flush()
        _in.channel.shutdown_write()
    sys.stdout.write(out.read().decode(errors="replace"))
    sys.stderr.write(err.read().decode(errors="replace"))
    rc = out.channel.recv_exit_status()
    c.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
