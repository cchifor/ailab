#!/usr/bin/env python3
"""Activation gate: assert AMD nested virtualization is live on every Proxmox host.

Kata's QEMU microVM needs /dev/kvm INSIDE the Talos agent-node worker, which requires
`kvm_amd nested=1` on the host. The persistent config is owned by Ansible `pve_base`
(/etc/modprobe.d/kvm-nested.conf); THIS script only PROBES the running state (read-only) so
`just agent-nodes-apply` never provisions the Kata pool against a host where nested virt is off.

    python scripts/check-nested-virt.py            # probe the default host list
    python scripts/check-nested-virt.py 192.168.0.2 192.168.0.3

Exit 0 = all hosts nested-virt ON; exit 1 = at least one host OFF/unknown (Kata blocked).
Accepts any kernel truthy repr (Y/y/1) — sysfs bool params render as `Y`, module params as `1`.
"""
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_HOSTS = ["192.168.0.2", "192.168.0.3", "192.168.0.4"]
TRUTHY = {"Y", "y", "1"}
# AMD Strix Halo cluster → kvm_amd. Kept generic so an Intel host would probe kvm_intel too.
PROBE = (
    "for m in kvm_amd kvm_intel; do "
    'p=/sys/module/$m/parameters/nested; '
    "[ -f $p ] && { echo -n \"$m=\"; cat $p; }; done"
)


def load_env(p: pathlib.Path) -> dict:
    e = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                e[k.strip()] = v.strip()
    return e


def probe(host: str, password: str) -> str:
    import paramiko

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = dict(timeout=12, look_for_keys=False, allow_agent=False)
    try:
        pkey = paramiko.Ed25519Key.from_private_key_file(
            os.path.expanduser("~/.ssh/id_ed25519")
        )
        c.connect(host, username="root", pkey=pkey, **kw)
    except Exception:
        c.connect(host, username="root", password=password, **kw)
    _in, out, _err = c.exec_command(PROBE, timeout=30)
    text = out.read().decode(errors="replace").strip()
    c.close()
    return text


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    hosts = sys.argv[1:] or DEFAULT_HOSTS
    password = load_env(REPO / ".env").get("NODE_ROOT_PASSWORD", "")
    ok = True
    for host in hosts:
        try:
            text = probe(host, password)
        except Exception as exc:  # unreachable host = gate failure, not a skip
            print(f"[FAIL] {host}: unreachable ({exc})")
            ok = False
            continue
        # any probed module reporting a truthy `nested` value satisfies the gate
        enabled = any(
            line.split("=", 1)[1].strip() in TRUTHY
            for line in text.splitlines()
            if "=" in line
        )
        if enabled:
            print(f"[ OK ] {host}: {text.replace(chr(10), ', ')}")
        else:
            print(f"[FAIL] {host}: nested virt OFF/unknown ({text or 'no kvm module'})")
            ok = False
    print("nested-virt gate:", "PASS" if ok else "FAIL — Kata pool blocked")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
