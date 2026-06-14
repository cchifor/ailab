# Runbook — Thunderbolt/USB4 link bring-up (node side)

This is what the Ansible `thunderbolt_net` + `storage_net` roles automate. Documented here for
understanding and manual debugging. Method follows scyto / `pieter-v-n/pmx-cluster-tb`, adapted
to **hub-spoke** (node ↔ QNAP), so **no FRR/OpenFabric** is needed — each link is a simple
point-to-point static route.

## 0. Prereqs
- Recent PVE kernel (≈6.17+/6.19+) — needed for USB4 stability on Strix Halo. Pin it (`pve_base`).
- Package `bolt` installed (fixes some retimer speed negotiation).

## 1. Load modules (persistent)
`/etc/modules` gets:
```
thunderbolt
thunderbolt-net
```
Verify after reboot: `lsmod | grep -E 'thunderbolt|thunderbolt_net'`.

## 2. Discover the real PCI path (per node — do NOT copy someone else's)
Plug the cable, then:
```bash
udevadm monitor --udev --subsystem-match=net &   # watch for the thunderbolt netdev appearing
boltctl list                                     # TB device + auth state
ls -l /sys/class/net/ | grep thunderbolt         # current kernel name (e.g. thunderbolt0)
udevadm info /sys/class/net/thunderbolt0 | grep ID_PATH
```
Record `ID_PATH` into `ansible/host_vars/<node>.yml` (`tb_pci_path`).

## 3. Stable rename via systemd .link
`/etc/systemd/network/00-thunderbolt.link` (templated per node by PCI path):
```ini
[Match]
Driver=thunderbolt-net
Path=<tb_pci_path>

[Link]
Name=en05
MTUBytes=4000
```
(Stable name lets Proxmox `ifupdown2` and the GUI see it and keeps statics across reboot.)

## 4. udev RUN + retry (link can appear after boot when peer/cable comes up)
`/etc/udev/rules.d/10-tb-up.rules` runs a small script that `ip link set en05 up` + applies the
address when the interface shows up. (Templated by the role.)

## 5. Static address + route (`/etc/network/interfaces.d/`)
```
auto en05
iface en05 inet static
    address 10.55.0.1/30
    mtu 4000
    up   ip route add 10.55.0.254/32 via 10.55.0.2 dev en05 || true
    down ip route del 10.55.0.254/32 via 10.55.0.2 dev en05 || true
```
(node2 uses `10.55.0.5/30` via `10.55.0.6`; node3's `enstor` uses `10.55.0.9/30` via `10.55.0.10`.)

## 6. Apply
```bash
update-initramfs -u -k all
reboot
```

## 7. Verify (see validation.md)
```bash
ip -br addr show en05
boltctl list
dmesg | grep -i thunderbolt
ping -c3 10.55.0.2 ; ping -c3 10.55.0.254
```

## Troubleshooting
- **No interface:** module not loaded / kernel too old / device not authorized (`boltctl enroll`).
- **Stuck at TB2 speed:** install `bolt`, reseat cable.
- **Port-2 issues on QNAP T2E:** known bug — prefer port 1; if node2's port flaps, fall back to 10GbE.
- **Name not persisting:** `.link` Path didn't match — re-check `ID_PATH`; `udevadm test`.
