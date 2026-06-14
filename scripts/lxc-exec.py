#!/usr/bin/env python3
"""Provision a Proxmox LXC: push the ai-lxc provisioning scripts into the container
and run provision.sh, streaming output live.

    python scripts/lxc-exec.py <host-ip> <vmid> [--env KEY=VAL ...] [--no-run]
    python scripts/lxc-exec.py 192.168.0.2 5001
    python scripts/lxc-exec.py 192.168.0.2 5001 --env MODEL=/models/gpt-oss-120b/gpt-oss-120b-mxfp4-00001-of-00003.gguf --env MODEL_ALIAS=gpt-oss-120b --env CTX=0 --env PARALLEL=1

Pushes provision.sh + llama-warmup.sh + amdgpu-textfile.sh from
kubernetes/infra/ai-lxc/ to /root/ in the CT (via the host's `pct push`), then runs
`<env> bash /root/provision.sh`. Auth mirrors scripts/node-ssh.py (key, then password).
"""
import os
import pathlib
import shlex
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "kubernetes" / "infra" / "ai-lxc"
FILES = ["provision.sh", "llama-warmup.sh", "amdgpu-textfile.sh"]


def load_env(p: pathlib.Path) -> dict:
    e = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                e[k.strip()] = v.strip()
    return e


def connect(host: str, env: dict):
    import paramiko

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = dict(timeout=12, look_for_keys=False, allow_agent=False)
    try:
        pkey = paramiko.Ed25519Key.from_private_key_file(os.path.expanduser("~/.ssh/id_ed25519"))
        c.connect(host, username="root", pkey=pkey, **kw)
    except Exception:
        c.connect(host, username="root", password=env.get("NODE_ROOT_PASSWORD", ""), **kw)
    return c


def run(c, cmd: str, stream: bool = True) -> int:
    chan = c.get_transport().open_session()
    chan.exec_command(cmd)
    while True:
        while chan.recv_ready():
            sys.stdout.write(chan.recv(8192).decode(errors="replace"))
            sys.stdout.flush()
        while chan.recv_stderr_ready():
            sys.stderr.write(chan.recv_stderr(8192).decode(errors="replace"))
            sys.stderr.flush()
        if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
            break
    # drain any tail
    while chan.recv_ready():
        sys.stdout.write(chan.recv(8192).decode(errors="replace"))
    while chan.recv_stderr_ready():
        sys.stderr.write(chan.recv_stderr(8192).decode(errors="replace"))
    return chan.recv_exit_status()


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = sys.argv[1:]
    if len(args) < 2:
        print("usage: lxc-exec.py <host-ip> <vmid> [--env KEY=VAL ...] [--no-run]", file=sys.stderr)
        return 2
    host, vmid = args[0], args[1]
    envs, no_run = [], False
    i = 2
    while i < len(args):
        if args[i] == "--env" and i + 1 < len(args):
            envs.append(args[i + 1]); i += 2
        elif args[i] == "--no-run":
            no_run = True; i += 1
        else:
            print(f"unknown arg: {args[i]}", file=sys.stderr); return 2

    env = load_env(REPO / ".env")
    c = connect(host, env)
    sftp = c.open_sftp()
    sftp.mkdir("/tmp/ai-lxc") if "ai-lxc" not in sftp.listdir("/tmp") else None
    for f in FILES:
        local = SRC / f
        sftp.put(str(local), f"/tmp/ai-lxc/{f}")
        print(f"-> staged /tmp/ai-lxc/{f} on {host}")
    sftp.close()

    # Push each file into the CT and make executable.
    push = " && ".join(
        [f"pct push {vmid} /tmp/ai-lxc/{f} /root/{f} --perms 0755" for f in FILES]
    )
    rc = run(c, push)
    if rc != 0:
        print(f"pct push failed (rc={rc})", file=sys.stderr); c.close(); return rc
    print(f"-> pushed {len(FILES)} files into CT {vmid}")

    if no_run:
        c.close(); return 0

    # Shell-quote each env VALUE so multi-word values (e.g. EXTRA_ARGS="--image-min-tokens 1024")
    # survive; shlex.quote the whole inner command for the outer remote shell (handles nesting).
    assignments = []
    for e in envs:
        k, sep, v = e.partition("=")
        assignments.append(f"{k}={shlex.quote(v)}" if sep else shlex.quote(k))
    inner = " ".join(assignments + ["bash /root/provision.sh"])
    cmd = f"pct exec {vmid} -- bash -lc {shlex.quote(inner)}"
    print(f"-> running provision.sh in CT {vmid} ...\n")
    rc = run(c, cmd)
    c.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
