# Runbook — Validation & expected numbers

Automated by the Ansible `validate_links` role (`just validate`); also runnable by hand. Results
land in `docs/_generated/`.

## Link layer (per node)
```bash
ip -br link show                       # en05 / enstor UP, correct MTU
ethtool en05 2>/dev/null || true       # TB often reports "Unknown!" speed — rely on iperf3
boltctl list                           # Thunderbolt device authorized
dmesg | grep -i -E 'thunderbolt|amdgpu' | tail
ip route get 10.55.0.254               # confirms traffic pins to the right interface
```

## Throughput — iperf3 (`--bidir`)
Run `iperf3 -s` on the QNAP side or a peer node; from each node:
```bash
iperf3 -c 10.55.0.254 --bidir -t 20
```
| Link | Expected (per dir) | Red flag |
|---|---|---|
| TB/USB4 (Strix Halo, Linux) | **~10–11 Gbps** | < 8 Gbps or unstable → driver/cable/port |
| node3 temp USB→2.5GbE | **~2.35 Gbps** | < 2.2 Gbps → adapter/driver |
| future 10GbE | **~9.4 Gbps** | < 9 Gbps → MTU/negotiation |

Tuning that helped in references: `tc qdisc replace dev en05 root fq_codel` (cuts retries on TB).
MTU made little difference on Strix Halo TB — keep 4000, don't expect gains from jumbo.

## Reboot persistence (critical for TB)
```bash
reboot
# after: re-run the link-layer + iperf3 checks; interface name, IP, route must survive.
```

## NFS / storage
```bash
showmount -e 10.55.0.254
mount -t nfs4 10.55.0.254:/share/pve-nfs /mnt/test    # path from qnap runbook
fio --name=rw --rw=randrw --bs=64k --size=4G --numjobs=4 --runtime=30 \
    --group_reporting --directory=/mnt/test
# expect aggregate bounded by ~Gen3x2 per drive and the ~10G link, not SSD datasheet numbers
umount /mnt/test
```

## Proxmox end-to-end (after `tofu apply`)
```bash
pvesm status                           # qnap-nfs Active on every node
# create a small test VM disk on qnap-nfs, take a snapshot, run a vzdump backup,
# live-migrate the test VM between nodes -> confirms the single-target/per-link design.
```

## Sign-off (Phase 4 gate)
- [ ] all three links UP at expected throughput, survive reboot
- [ ] T2E stable on **both** TB ports (or fallback recorded)
- [ ] NFS mounts from `10.55.0.254` on all nodes; fio sane
- [ ] `pvesm status` healthy; snapshot + backup + live-migration succeed
