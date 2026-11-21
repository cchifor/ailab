# Runbook — Node maintenance & node loss (Talos)

Owner-facing procedure for taking any one node down (planned) and for recovering from a node dying
(unplanned), written after the 2026-07-06 carve→GTT reboots took the strive platform down
(issue [#100](https://github.com/cchifor/ailab/issues/100)). The availability model behind this —
which workloads must survive a node loss (Tier A) and which singletons are *accepted* to blip
(Tier B) — is **ADR 0016**.

## Ground rules

- **All 3 nodes are control planes AND workers.** etcd quorum needs 2/3 → strictly **ONE node at a
  time**, and verify quorum before *and* after each node:
  `_out/talosctl-1142.exe -n 192.168.0.41 etcd status` → **3/3 in-sync**.
  Use `_out/talosctl-1142.exe` (v1.14.2) — the system `talosctl` is v1.6.2 and UNSAFE.
- **`qm shutdown` / ACPI does NOT stop Talos** (falls back to a hard stop). Graceful =
  `talosctl shutdown -n <cp-ip>` — it **drains first** (evicts pods, 5-min drain timeout), then powers
  off. Expect ~5 min, poll `qm status <vmid>` for `stopped`.
- kubectl: `--context admin@ai` or `KUBECONFIG=kubernetes/infra/_out/kubeconfig` (the default context
  is a DIFFERENT cluster).
- **The CP disks are on the QNAP** (`scsi0` on `qnap-nfs` since 2026-11-20, ADR 0043). A host reboot or loss
  still takes ONE CP down. A QNAP outage takes all THREE: see "QNAP maintenance is control-plane maintenance".

| Proxmox host | mgmt IP | Talos CP | CP IP | CP vmid | AI LXC ctid |
|---|---|---|---|---|---|
| ai-node1 | 192.168.0.2 | talos-cp1 | 192.168.0.41 | 4001 | 5001 |
| ai-node2 | 192.168.0.3 | talos-cp2 | 192.168.0.42 | 4002 | 5002 |
| ai-node3 | 192.168.0.4 | talos-cp3 | 192.168.0.43 | 4003 | 5003 |

## QNAP maintenance is control-plane maintenance (since 2026-11-20)

All three etcd members' disks are raw files on the QNAP (`qnap-nfs`, ADR 0043). Anything that takes the share away
stalls the whole control plane at once, not one node:
- a NAS reboot, including a QuTS firmware update, which reboots it;
- pool work that takes the pool or the `/pve-nfs` export offline;
- work on its Thunderbolt or network (the NFS service IP `10.55.0.254` lives on its `tbtbr0` bridge).

Source: by construction (ADR 0043 risk 1). The disks are files on that share, so not having the share is not having
the disks. It has not been observed since the move.

For the outage list above: every database, Gitea and Prometheus were already on its iSCSI LUNs. Now the API server,
the scheduling and every lease go with them.

- **Plan it as a full-cluster outage**, with the owner, never as routine NAS upkeep.
- **Take an etcd snapshot first:**
  `_out/talosctl-1142.exe -n <cp-ip> -e <cp-ip> etcd snapshot _out/etcd-snapshots/<name>.db`. talos-backup's own
  copies land on versitygw, which runs in the NAS chassis itself.
- **What the CPs see:** the mount is `hard,timeo=600,retrans=2`, so their disk I/O blocks while the NAS is away and
  resumes when it answers. How Talos rides out a multi-minute stall (guest SCSI timeouts, the XFS on EPHEMERAL) has
  not been tested.
- **Open item:** there is no rehearsed procedure yet for a planned NAS outage (stopping the CPs before, starting them
  after the share is mounted on every host). Write and rehearse one before the first planned NAS reboot.
- **Afterwards, on every host:** `findmnt /mnt/pve/qnap-nfs`; `qm status <vmid>` running for each CP (`qm start` any
  that is not); etcd 3/3 in sync; then the post-maintenance checklist.
- **After a host boot**, check that its CP is running (`qm status <vmid>`). The boot-order guard (#1899; its state
  is under "Implementation state" in ADR 0043) holds the start until the share is ready, and logs that it did.

**A scrub, a resilver or a large copy is a latency event, not an outage** (ADR 0043 risk 5). Nothing has measured one
stalling etcd: no scrub has run since the move, and the first scrub on this pool took 3m43s (`qnap-storage-setup.md`
§4). Run them off-hours: the monthly scrub is already a NAS cron job at 05:00 on the 1st. Watch the etcd alerts while
they run: `etcdHighFsyncDurations` (WAL fsync p99 > 0.5 s / 1 s for 10 min), `etcdHighCommitDurations` (> 0.25 s) and
`etcdHighNumberOfLeaderChanges` (4 or more in 15 min). If one fires, stop the scrub with
`python scripts/qnap-ssh.py --sudo "zpool scrub -s zpool1"`. It needs no owner-planned outage.

## Planned drain / host reboot

```bash
# 0. quorum green?                                   3/3 in-sync
_out/talosctl-1142.exe -n 192.168.0.41 etcd status

# 1. where do the CNPG primaries sit? If one is on the node you're taking down, you can (optionally)
#    switch it away first for a deterministic hand-off. The operator ALSO does this automatically
#    ahead of the drain — the manual step just makes the timing yours. (infra-pg in the databases ns,
#    strive-pg in strive-ailab.)
kubectl --context admin@ai get pods -A -l cnpg.io/instanceRole=primary -o wide
# NB: kubectl rejects global flags BEFORE a plugin name ("flags cannot be placed before plugin
# name") — for `cnpg` commands the --context goes AFTER:
kubectl cnpg status strive-pg -n strive-ailab --context admin@ai           # needs the cnpg kubectl plugin
kubectl cnpg promote strive-pg <replica-not-on-that-node> -n strive-ailab --context admin@ai
# 1b. strive-pg runs REQUIRED per-hostname anti-affinity (platform cluster.yaml, since 2026-09-27):
#     the instance evicted from this node stays Pending until the node is back — that is expected,
#     and the reason to roll ONE CP at a time. Before the drain the cluster must be healthy and
#     spread, or 2/3 does not hold:
kubectl --context admin@ai -n strive-ailab get cluster strive-pg            # 3/3, "Cluster in healthy state"
kubectl --context admin@ai -n strive-ailab get pods -l cnpg.io/cluster=strive-pg -o wide   # one per CP
kubectl --context admin@ai -n strive-ailab get pdb                          # strive-pg ALLOWED DISRUPTIONS 1
kubectl --context admin@ai -n strive-ailab exec <primary-pod> -c postgres -- \
  psql -U postgres -Atc "select application_name, state, replay_lag from pg_stat_replication"  # 2 rows streaming, lag < 1s
# 1c. Trident attach/detach takes 2–5 min per volume here (QNAP per-LUN sweep; runbook
#     qnap-storage-setup.md §9) — a replaced instance needs up to ~8 min. Confirm the timeout policy
#     is live before draining: `kubectl --context admin@ai get clusterpolicy trident-attacher-timeout`.
# 1d. BREAK-GLASS if a CP never comes back (hardware loss): with required anti-affinity the cluster
#     runs 2/3 with ONE streaming replica until someone acts — flip `podAntiAffinityType` back to
#     `preferred` in platform deploy/components/cnpg-cluster/cluster.yaml, deliberately, keeping
#     `instances: 3`, so the third instance can double up on a survivor. Lowering `instances` to 2
#     is NOT the same lever: it only re-declares the degraded state as nominal (2/2, still one
#     streaming replica) — the sole reason to do it is a survivor that cannot fit a third instance
#     (the 1e gate), and it must be reverted just the same. Revert the flip only after the replaced
#     node holds the third instance and it streams — status 3/3 and `pg_stat_replication` shows two
#     rows streaming (the 1b check; attach can take ~8 min, 1c) — otherwise restoring `required`
#     evicts or blocks the instance that just recovered. Do not leave it at 2/3 "until the node is fixed".
# 1e. HEADROOM GATE, learned 2026-09-27 during the required-anti-affinity roll (platform#1686): a
#     replaced instance can only go back to ITS node, which must then hold ≥500m CPU AND ≥2Gi memory
#     free — memory was the one that bit (cp1 at 1862Mi free vs 2Gi). Pods pinned to that node by a
#     local-path PV (ai/text-embeddings 4Gi, dsh) CANNOT be moved to make room — cordoning just
#     leaves them Pending until the node reopens. Free the node with a movable Deployment pod
#     instead (e.g. ai/litellm, 1Gi, no volumes): `kubectl --context admin@ai cordon talos-cpN`,
#     delete that pod so its replacement lands elsewhere, `uncordon` — seconds, and the Pending
#     instance schedules at once. Check with:
kubectl --context admin@ai describe node talos-cpN | grep -A6 'Allocated resources'

# 2. know your alerting blind spot: if alertmanager/ntfy live on this node, pushes pause during the
#    move — watch gatus (status.chifor.me) instead.
kubectl --context admin@ai get pods -n monitoring -o wide | grep -E 'alertmanager|ntfy'

# 3. graceful stop of that host's CP (drains ~5 min, then powers off; poll for stopped).
#    `qm` only exists ON the Proxmox host -> run the poll through node-ssh.py:
_out/talosctl-1142.exe shutdown -n <cp-ip>
python scripts/node-ssh.py <host-ip> "for i in \$(seq 1 90); do qm status <vmid> | grep -q stopped && break; sleep 5; done; qm status <vmid>"

# 4. host work (BIOS / kernel / Proxmox upgrade / hardware), then reboot the host.

# 5. boot race: the AI LXC often fails its first autostart (amdgpu device appears ~6 s after LXC start).
python scripts/node-ssh.py <host-ip> "pct start <ctid>"

# 5b. the CP's disk is on qnap-nfs: the boot-order guard must have said "ready" (section "Boot-order guard").
#     A WARNING instead means the CP may be stopped: `qm start <vmid>` once the share is back.
python scripts/node-ssh.py <host-ip> "journalctl -b -u pve-guests --no-pager | grep pve-nfs-disk-gate; qm status <vmid>"

# 6. gates before the next node: see the post-maintenance checklist below.
```

### ⚠️ A wedged AI LXC turns step 4's reboot into a physical visit (2026-09-21)

If the node's AI LXC has a process stuck in **D state** (uninterruptible), `reboot` **hangs
forever** and takes SSH with it. `pve-guests.service` has `TimeoutSec=infinity`, and its
`ExecStop` runs `pvesh create /nodes/localhost/stopall`, which stops a container with
`lxc-stop --kill` — and per `/usr/share/perl5/PVE/LXC.pm` that path *"doesn't allow timeouts"*.
Container init cannot exit while a D-state child sits in its pid namespace, so systemd waits
forever with sshd and pveproxy already stopped. The host answers ping and nothing else; with no
BMC on these boxes that is a **physical power cycle**. ai-node3 sat like that for ~27 minutes on
2026-09-21 before someone pressed the button.

Spot it before you reboot:

```bash
python scripts/node-ssh.py <host-ip> "ps -eo pid,stat,wchan:24,comm | awk '\$2 ~ /D/'"
# a D-state llama-server with wchan drm_suballoc_new is the amdgpu wedge; /proc/<pid>/stack confirms
```

**The sequence that works** — stop every guest yourself, then reboot with the unit stops skipped
**while sshd is still alive**:

```bash
# 1. Talos guests (CP + any agent node on this host) -- these DRAIN, so they go first
_out/talosctl-1142.exe shutdown -n <cp-ip>            # repeat per Talos guest on the host

# 2. QEMU VMs (runners, dev-workers)
python scripts/node-ssh.py <host-ip> "for v in <ubuntu vmids>; do qm shutdown \$v --timeout 300 & done"

# 3. CONTAINERS -- every LXC that CAN stop must be stopped here, or step 5 hard-stops it.
#    `pct shutdown` asks init politely and gives up at --timeout; the WEDGED container is the one
#    that will not go, and that is expected -- it is the whole reason for step 5.
#    Do NOT fall back to `pct stop` on it: that is lxc-stop --kill, the call that hangs forever.
python scripts/node-ssh.py <host-ip> "for c in \$(pct list | awk 'NR>1{print \$1}'); do pct shutdown \$c --timeout 120 & done; wait; pct list"

# 4. GATE: every VM `stopped`, and every LXC `stopped` EXCEPT the wedged one
python scripts/node-ssh.py <host-ip> "qm list; pct list"

# 5. reboot with the unit stops skipped, while sshd is still alive
python scripts/node-ssh.py <host-ip> "sync; nohup sh -c 'sleep 1; systemctl reboot --force' >/dev/null 2>&1 &"
```

`--force` skips the unit stops (so `lxc-stop --kill` never runs) while still syncing filesystems;
the D-state task disappears with the kernel. Guests autostart on boot; then run the
post-maintenance checklist below. **Step 3 is not optional**: `--force` is a hard stop for
anything still running, so the only guest that may still be up at step 5 is the one that
physically cannot be stopped.

Two things that do **not** fix a D-state task, both tried on 2026-09-21: killing it (SIGKILL is
pending but never delivered in D state), and resetting the GPU. The reset is worth knowing anyway:
this kernel runs `integrity` lockdown (Secure Boot), so *writing*
`/sys/kernel/debug/dri/<pci>/amdgpu_gpu_recover` is refused while **`cat` of it triggers the
reset** — both nodes logged `GPU reset(1) succeeded … device wedged, but recovered through reset`
and neither released the process, because the wait is on the DRM suballocator's list, not the GPU.

**Workloads-only variant** (node stays up, e.g. testing eviction behaviour):
`kubectl --context admin@ai drain <node> --ignore-daemonsets --delete-emptydir-data` … then
`kubectl --context admin@ai uncordon <node>`.

## Expected blips during a planned drain (Tier B accepted singletons — ADR 0016)

Tier A (cloudflared ×2, and after the #100 companion PRs: infra-pg, grafana ×2, authelia ×2,
strive-pg ×3) rides through a drain with **zero downtime**. Everything below is a deliberate
singleton; a drain moves it once:

| Workload | Kind / storage | Drain blip | Notes |
|---|---|---|---|
| prometheus | STS, qnap-iscsi RWO | ~1–3 min | metrics gap; rules re-evaluate on start |
| alertmanager | STS, **ephemeral** | ~1 min | **silences + dedup state LOST** — recreate silences |
| loki | STS, nfs-csi | ~1–2 min | alloy buffers & retries; no detach wall (NFS) |
| tempo | STS, qnap-iscsi RWO | ~1–3 min | trace ingest gap |
| grafana / authelia (until their HA PRs) | Deploy, qnap-iscsi RWO | ~1–3 min | SSO logins fail during the authelia move |
| gitea | Deploy, qnap-iscsi RWO | ~1–3 min | git push/pull fails briefly |
| vaultwarden | Deploy, qnap-iscsi RWO | ~1–3 min | clients have local cache |
| ntfy | Deploy, qnap-iscsi RWO | ~1–3 min | pushes delayed — Alertmanager webhook retries |
| open-webui | Deploy, nfs-csi RWX | ~1 min | |
| litellm / homepage / gatus / headlamp / oauth2-proxy | stateless | ~30–60 s | |
| trivy-server | STS, qnap-iscsi RWO | ~1–3 min | scans pause |
| valkey-master (strive) | STS, nfs-csi | ~1–2 min | airlock falls back to in-memory rate limiting |

## Unplanned node loss (host dies / hangs)

Default timeline for a hard loss, so you know what you're looking at:

1. **~40 s** — node goes `NotReady` (node-monitor-grace-period); NoExecute taints applied.
2. **+ tolerationSeconds** — Deployment pods get evicted: **60 s** for Tier-A stateless pods
   (cloudflared, later auth-valkey/authelia/grafana — set per ADR 0016), **300 s (default)** for
   everything else.
3. **RWO iSCSI volumes** — the new pod can't attach until the old attachment force-detaches:
   **~6 min** (`maxWaitForUnmountDuration`, hard-coded in the attach-detach controller — not
   configurable; this is why we don't tune controller-manager flags, see ADR 0016).
4. **StatefulSet pods stick in `Terminating` indefinitely** — the STS controller waits for confirmed
   kubelet deletion that never comes. Tolerations do NOT help here; the taint below is the mechanism.
5. CNPG clusters don't wait for any of this: a replica is **promoted in ~40 s–2 min** (no volume
   movement — each instance has its own PVCs).

**Decision matrix:**

- Node will be back shortly (reboot in progress) → **do nothing**; everything reconverges.
- Node is **CONFIRMED dead or powered off** (VM `stopped` in Proxmox / host offline) → apply the
  out-of-service taint. **Never while the node might still be running** — forcing detach against a
  live node that still has the LUN mounted is an iSCSI double-mount / ext4-corruption risk:

```bash
kubectl --context admin@ai taint nodes <node> node.kubernetes.io/out-of-service=nodeshutdown:NoExecute
```

Effect: immediate eviction (including stuck-`Terminating` STS pods) + immediate volume detach —
Tier-B RWO singletons recover in ~2–3 min instead of ~13. **Remove the taint once the node is healthy:**

```bash
kubectl --context admin@ai taint nodes <node> node.kubernetes.io/out-of-service=nodeshutdown:NoExecute-
```

## Boot-order guard (`pve_nfs_disk_gate`, all three ai-nodes)

Since 2026-11-20 each host's Talos CP disk is a raw file on `qnap-nfs` (ADR 0043). `pve-guests.service` starts the
onboot guests once per boot and never retries. PVE refuses to start a VM whose storage is offline (`storage
'qnap-nfs' is not online`). The hosts reach the NAS over the storage link (Thunderbolt on node1/2, the 10G adapter
on node3), which comes up during boot. After the 2026-10-30 power cut the NAS served NFS ~6 min after the hosts.
Either way a CP could stay stopped until someone started it by hand.

`pve_base` (tag `nfs_disk_gate`, on by default) adds a drop-in, `pve-guests.service.d/10-ailab-nfs-disk-gate.conf`.
It runs `/usr/local/sbin/pve-nfs-disk-gate` as a `-` ExecStartPre, after the stock `pve-startall-delay` and before
startall. For each storage in `pve_nfs_disk_gate_storages` that holds a disk of an onboot VM on the host, the gate
waits until the storage is:

1. **mounted:** an NFS mount exactly at its path, read from `/proc/self/mountinfo`, never a stat. pvestatd mounts the
   storage once the server answers. Meanwhile the gate asks PVE to activate it (`pvesm status --storage`), as
   `qm start` would.
2. **writable:** `<path>/.pve-nfs-disk-gate/<node>` is written, fsync'ed and read back.
3. **serving the disk:** the first 4 KiB of each such disk image is read, so an empty export does not pass.

The wait is bounded by `pve_nfs_disk_gate_timeout_s` (600 s) across all storages. Each probe runs in a process group of
its own: a hung one is SIGKILLed with everything it started (a `dd`, pvesm's `mount.nfs`) and never waited for, so
only a process stuck in uninterruptible NFS I/O can outlive it. With the share up it passes in seconds. A host with nothing on these storages passes at once.

**After a timeout the CPs are started anyway, not skipped.** The gate logs a WARNING and pve-guests starts every
guest as before. PVE's own storage check then fails a CP's start while the share is offline, so a CP never boots
against a missing disk. If the share came back after the deadline, the CP starts with nobody involved. On PVE 9.2,
startall runs starts in parallel worker slots (up to 8), so a CP start stuck on a hung share holds no other guest.
The full reasoning is in `ansible/roles/pve_base/defaults/main.yml`. **A CP that PVE refused stays stopped:** run
`qm start <vmid>` once the share is back, then check etcd 3/3.

Reading it after any host boot:

```bash
python scripts/node-ssh.py <host-ip> "journalctl -b -u pve-guests -o short-iso --no-pager | grep -E 'pve-nfs-disk-gate|Starting (VM|CT)'"
# ready:   pve-nfs-disk-gate: waiting up to 600s for qnap-nfs at /mnt/pve/qnap-nfs (VM 4001)
#          pve-nfs-disk-gate: qnap-nfs ready after 7s: mounted, writable, serving the disks of VM 4001
#          ...then the "Starting VM/CT" lines
# timeout: pve-nfs-disk-gate: WARNING: qnap-nfs not ready after 600s: <the last check that failed>. Handing over ...
#          (priority warning: `journalctl -b -u pve-guests -p warning` shows it alone)
```

**Rollout, one host at a time.** The drop-in only takes effect at the next boot. Nothing is restarted: the role runs
a daemon-reload and never touches pve-guests or a guest.

```bash
# 0. What the host runs now: the stock unit, no drop-in yet.
python scripts/node-ssh.py <host-ip> "systemctl show -p ExecStartPre --value pve-guests.service; ls /etc/systemd/system/pve-guests.service.d/ 2>&1"
# 1. Preview (from WSL, ANSIBLE_CONFIG set; see the ailab-ansible notes in CLAUDE.md): the script and the drop-in.
just nfs-disk-gate --limit ai-node1 --check --diff
# 2. Apply, then a second run must change nothing.
just nfs-disk-gate --limit ai-node1
just nfs-disk-gate --limit ai-node1                          # changed=0
# 3. Verify that systemd loaded it (the role asserts this too), and dry-run the gate on the live host. The run is
#    safe: it writes only its probe file on the share and reads the first 4 KiB of the running CP's disk image.
python scripts/node-ssh.py <host-ip> "systemctl show -p ExecStartPre --value pve-guests.service | grep -c 'pve-nfs-disk-gate ; ignore_errors=yes'; WAIT_S=30 /usr/local/sbin/pve-nfs-disk-gate; echo rc=\$?"
#    expect 1, "qnap-nfs ready after 0s: mounted, writable, serving the disks of VM 400N", rc=0
# 4. The next host.
```

Rollback: `pve_nfs_disk_gate_enabled: false` (removes the drop-in and the script), or remove the drop-in by hand and
`systemctl daemon-reload`.

**The owner-gated test: one host reboot.** The owner picks the host and the window: the ai-nodes are processing
data. Run so far: **ai-node3, 2026-11-21, pass.** The gate logged "waiting" at 07:07:23.8Z and "ready after 5s" at
07:07:28.7Z, and the first "Starting VM 4003" came at 07:07:29.1Z. The share itself mounted at 07:07:28.4Z, after
pve-guests had begun, so the gate held cp3 for real. N = 5 s on ai-node3. ai-node1 and ai-node2 have not had a guarded
boot yet. The timeout path is covered only by the Docker test (below).

1. The rollout above is done on that host, and step 3 said ready.
2. Take the host down as in "Planned drain / host reboot" (steps 0-4, with the CP shut down gracefully), and reboot it.
3. Read the boot as above. **Pass** = the gate's "waiting" and "ready after Ns" lines come before every
   "Starting VM/CT" line, the CP is `running` without a hand `qm start`, and etcd is 3/3 (post-maintenance
   checklist). Record N, the storage link's bring-up time on that host.
4. **Fail** = no gate line (the drop-in was not loaded) or a WARNING. Start the CP by hand if it is stopped, finish
   the checklist, and report before the next host.

Tests: `scripts/tests/test_pve_nfs_disk_gate.py` (the real script against a fake `/etc/pve`, mountinfo and `pvesm`;
in script-tests CI) and `bash ansible/roles/pve_base/tests/test-nfs-disk-gate-converge-docker.sh` (manual: the role
converged into a systemd container, then a stand-in pve-guests booted with the share ready, late and never there).

## Thin pool fill (`pve_thin_textfile`, all three ai-nodes)

Every guest disk on a host is a thin LV in `pve/data` (`local-lvm`), except the Talos CP's boot disk, which is on
`qnap-nfs` since 2026-11-20 (ADR 0043). The CP's cloud-init drive is still here, and PVE rewrites it on every start,
so a full pool can also keep the CP from starting. A full pool fails every guest's writes
on that host, and a pool with full metadata goes read-only. Space a guest frees stays allocated until it is
trimmed. On 2026-11-20 about 1.1 TiB of the three pools was freed-but-untrimmed space, and nothing showed
it. `pve_base` (tag `thin_textfile`, on by default) installs a read-only exporter.
`/usr/local/sbin/pve-thin-textfile` runs every 2 min and turns one `lvs` call into node_exporter textfile
metrics (`pve_thin.prom`, job `proxmox-node`):

- `pve_thin_pool_data_percent`, `pve_thin_pool_metadata_percent` and the pool's sizes;
- per thin volume, `pve_thin_lv_size_bytes`, `pve_thin_lv_data_percent` and `pve_thin_lv_allocated_bytes`
  (labels `lv` and `vmid`), and `pve_thin_lv_open` (1 while its device is open: an attached disk or a mounted
  volume; 0 for a source kept after a disk move or an unused disk);
- `pve_thin_textfile_read_errors` and `pve_thin_textfile_timestamp_seconds`.

The recording rule `pve:lxc_volume_untrimmed_bytes` is, for a container volume whose guest runs
node_exporter (the LLM containers), the pool allocation minus the guest filesystem's use. That is what a
trim would give back, joined on the `/dev/mapper/pve-vm--<id>--disk--<n>` name both sides see. For a VM,
compare `pve_thin_lv_allocated_bytes{vmid="<id>"}` with the guest's own `node_filesystem_*`. The Local Drives
dashboard (uid `ailab-local-drives`) shows that per guest as the "untrimmed gap", for a VM only when its disks
are provably its open local volumes.

Alerts (`kubernetes/apps/infrastructure/monitoring/thin-pool-rules.yaml`):

- `ProxmoxThinPoolDataHigh` (data over 85 % for 15m, warning) and `...DataCritical` (over 95 % for 5m).
- `ProxmoxThinPoolMetadataHigh` (metadata over 80 % for 15m) and `...MetadataCritical` (over 90 % for 5m).
- `ProxmoxThinTextfileStale` (no update for 15 min), `ProxmoxThinTextfileMissing` (the role's
  `pve_thin_textfile_enabled` marker is there but the file's series are not: never written, removed or
  unparsable) and `ProxmoxThinTextfileReadErrors`. A read error leaves the affected series out, so the fill
  alerts could not fire for them; the exporter says why on stderr (`journalctl -u pve-thin-textfile`), with
  lvs's message or the rejected row.
- `ProxmoxLxcTrimNotSucceeding` and `DevWorkerTrimNotSucceeding`: the nightly LXC volume trim, or a dev
  worker's daily trim, has not completed a pass for 50 h. Each keys on its `enabled` marker, whose mtime
  gives a new install 50 h first. The Talos node trim alerts from kube-state-metrics
  (`node-fstrim-rules.yaml`).

When the pool fills, find what holds it, then free it:

```bash
python scripts/node-ssh.py <host-ip> "lvs -o lv_name,lv_size,data_percent --sort -data_percent pve | head -20; lvs -o lv_name,data_percent,metadata_percent pve/data"
```

In Prometheus, `topk(10, pve:lxc_volume_untrimmed_bytes)` and
`topk(10, pve_thin_lv_allocated_bytes{job="proxmox-node"})` show the same thing.

- Untrimmed space: check the trims ran (`journalctl -u pve-lxc-fstrim` on the host, the `node-fstrim`
  CronJobs, a worker's `dev-worker-fstrim`, a runner's `gitea-runner-fstrim`), or trim by hand off-hours.
- Metadata: grow it online, `lvextend --poolmetadatasize +1G pve/data`.

Rollout: `ansible-playbook site.yml --limit <host> --tags thin_textfile --check --diff`, then without `--check`.
Then `cat /var/lib/prometheus/node-exporter/pve_thin.prom` on the host. Off: `pve_thin_textfile_enabled: false`
removes the timer, the script and the file. Tests: `scripts/tests/test_pve_thin_textfile.py` (script-tests),
promtool fixtures in `thin-pool-rules.test.yaml`, and the manual Docker converge
`bash ansible/roles/pve_base/tests/test-thin-textfile-converge-docker.sh`. Docker Desktop's kernel has no
dm-thin-pool, so the converge runs the real `lvs` against a real VG with a linear LV (its output format, and
the unit's hardening under real LVM locking) and takes the thin numbers from a stub thin listing in that
format: it is not thin-pool coverage.

## Thin-pool zeroing (`pve_thin_pool_zero`, OFF on ai-node1/2 since 2026-11-17)

The LVM thin pool `pve/data` (64 KiB chunks) zero-fills the rest of each newly provisioned chunk
when zeroing is on. These drives have no Write Zeroes, and the guests discard online, so freed
chunks are reused and zeroed all day: about 0.7 TB/day across the three drives. node1/2 run with
zeroing OFF (host_vars); node3 keeps it ON for the relay-worker pool. The isolation trade-off and
its limits are in `ansible/roles/pve_base/defaults/main.yml`.

Changing the flag reloads the pool's table ONLINE. LVM suspends the pool and its active thin
volumes, commits, and resumes, so every guest on that host pauses I/O briefly; a slow or busy
drive can make it longer. So, one host at a time:

```bash
# 0. Pool healthy, with data AND metadata headroom; no other storage maintenance on that host.
python scripts/node-ssh.py <host-ip> "lvs -o lv_name,data_percent,metadata_percent,zero pve/data; dmsetup table | grep thin-pool"
#    (zeroing ON: the thin-pool line has no feature args, e.g. `... 128 0 0`)
_out/talosctl-1142.exe -n 192.168.0.41 etcd status          # 3/3 in-sync before you start
# 1. Converge ONE host (from WSL, ANSIBLE_CONFIG set; see ailab-ansible notes in CLAUDE.md):
just thinpool --limit ai-node1
# 2. Verify: the flag, the live table, and that a second run changes nothing.
python scripts/node-ssh.py <host-ip> "lvs --noheadings --binary -o zero pve/data; dmsetup table | grep thin-pool"
#    expect `0` and `... 128 0 1 skip_block_zeroing`
just thinpool --limit ai-node1                              # changed=0
# 3. Watch guest latency / etcd for a few minutes (etcd 3/3), then the next host.
```

Rollback: `pve_thin_pool_zero: true` in that host's host_vars (or `lvchange --zero y pve/data`).
Re-enabling does NOT scrub chunks provisioned while zeroing was off.

## LXC volume trim (nightly; `pve_lxc_fstrim`, all three ai-nodes)

The host's `fstrim.timer` never reaches a container's volumes, so what a container frees stays allocated
in `pve/data`. On 2026-11-20 the LLM containers held 62 GiB (5002, ai-node2) and 134 GiB (5003, ai-node3)
of freed-but-untrimmed blocks, mostly from model swaps on `/models-local`. `pve_base` (tag `lxc_fstrim`)
installs a nightly sweep that trims them:

- `/usr/local/sbin/pve-lxc-fstrim` (`ansible/roles/pve_base/files/pve-lxc-fstrim.sh`) picks the
  `rootfs` and `mpN` volumes on `local-lvm` of every **running** container without a lock, minus
  `pve_lxc_fstrim_exclude`. Bind mounts, read-only mount points and stopped containers are never touched.
- Each volume goes through `/usr/local/sbin/lxc-fstrim-chunked.sh` (`scripts/lxc-fstrim-chunked.sh`, the
  registry's weekly trim, now installed by the role): host-side only, 4 GiB chunks, waiting while host IO
  pressure is above 45 %, 15 s between chunks (1 s after a chunk that trimmed nothing).
- A deadline: no chunk starts after the slot's 13 min unless it can end by then. Progress is kept in
  `/var/lib/pve-lxc-fstrim` (the unit's `StateDirectory`): per volume a cursor, so a cut-short night is
  continued at that chunk and every pass reaches the end of the volume, and the time of its last complete
  pass. Volumes go least recently completed first, so no volume is starved by another that keeps running out
  of time. ext4 also skips the block groups it has trimmed since the container mounted the volume, so a
  quiet night costs seconds.
- It shares `/run/lxc-fstrim-chunked.lock` and `/run/lxc-fstrim-chunked.stop` with the registry trim:
  the two never trim at once, and `touch /run/lxc-fstrim-chunked.stop` stops both. **Remove it afterwards**
  (`rm -f /run/lxc-fstrim-chunked.stop`): a stop file still there when the sweep starts fails it (exit 3)
  instead of letting every night skip quietly. `/run` is a tmpfs, so a reboot clears it too.
- The inner script's chunk lines go to the unit's journal as they happen (`journalctl -fu pve-lxc-fstrim`).

**Slots (UTC, every day).** The hosts take turns, 20 min each including the unit's stop allowance:

| Host | Sweep | What it trims today |
|---|---|---|
| ai-node2 | 04:30-04:49 | 5002 (rootfs, `/models-local`) |
| ai-node3 | 04:50-05:09 | 5003 (rootfs, `/models-local`) |
| ai-node1 | 05:10-05:29 | nothing: 5004 is excluded, its weekly unit trims it ([registry-cache.md](registry-cache.md)) |

`scripts/tests/test_pve_lxc_fstrim_schedule.py` (run by script-tests) fails if a slot leaves 00:30-05:30,
two hosts overlap (worst case: TimeoutStartSec plus TimeoutStopSec), the timeouts do not cover a chunk, or
the registry container is not excluded on its host.

**Other trims on the same drives.** The CI runners trim inside their VMs every night
([ci-runners.md](ci-runners.md), "Thin-pool trim"). On ai-node2 (ci-runner-10, 03:40-05:25) and ai-node1
(ci-runner-9, 04:15-05:25, not Sundays) a runner window covers this slot; ai-node2 has no runner-free
time between 00:00 and 05:30 at all. The sweep does not see a runner's slices; it waits while host IO
pressure is high, which a runner slice raises. After the one-time cleanup a night's work is small (what
a container freed that day). That overlap is the same class of residual risk as the runners' own, and the
interlock that ci-runners.md requires on its triggers would cover this sweep too.

**Rollout, one host at a time** (from WSL with `ANSIBLE_CONFIG` set):

```bash
# 0. What is untrimmed now: a container's thin LV against its filesystem's use.
python scripts/node-ssh.py 192.168.0.3 "lvs -o lv_name,lv_size,data_percent pve; pct df 5002"
# 1. Preview. On ai-node1 this also shows the diff against the hand-installed lxc-fstrim-chunked.sh.
just lxc-fstrim --limit ai-node2 --check --diff
# 2. Apply; a second run changes nothing. The converge never starts a trim, only the timer.
just lxc-fstrim --limit ai-node2
just lxc-fstrim --limit ai-node2                       # changed=0
python scripts/node-ssh.py 192.168.0.3 "systemctl list-timers pve-lxc-fstrim.timer; cat /etc/default/pve-lxc-fstrim"
# 3. After its first night: the sweep's log, the beacon, the LV.
python scripts/node-ssh.py 192.168.0.3 "journalctl -u pve-lxc-fstrim --since -1d --no-pager | tail -30; cat /var/lib/prometheus/node-exporter/pve_lxc_fstrim.prom; lvs pve/vm-5002-disk-1"
```

Verify it did not hurt etcd the same way as the registry trim (registry-cache.md, "Verify a run"): WAL
fsync p99 per member over the slot against the slot before, and leader changes. Exit codes are in the
script headers: the sweep exits 1 when a volume's trim failed (the beacon's
`pve_lxc_fstrim_volume_exit_code` names it; 3 pressure never dropped, 4 pressure unreadable, 5 fstrim
failed, 6 mount not released), and exits 3 when the stop file was already there at the start.
`pve_lxc_fstrim_last_success_timestamp_seconds` is the oldest of the volumes' last complete passes
(`pve_lxc_fstrim_volume_last_complete_timestamp_seconds`): every volume has completed a pass since then. It does not move on a night
whose discovery skipped a container (a lock, or a config that could not be read), whose volumes it
cannot vouch for.

Kill switch: `pve_lxc_fstrim_enabled: false` in the host's host_vars, then `just lxc-fstrim --limit
<host>`. It stops a running sweep cleanly and removes the timer, the unit, the settings and the metrics;
`lxc-fstrim-chunked.sh` stays for the registry unit.

Tests: `bash scripts/tests/test-lxc-fstrim-chunked.sh` and `bash ansible/roles/pve_base/tests/test-pve-lxc-fstrim.sh`
(stubs, run in CI by manifests.yaml), the schedule test above, and the manual Docker converge
`bash ansible/roles/pve_base/tests/test-lxc-fstrim-converge-docker.sh` (systemd, idempotence, `--check`, a real
trim of a loop-backed ext4 volume, disable).

## EC fan curve (`pve_ec_fan`, all three ai-nodes since 2026-11-19)

The Bosgame M5's embedded controller (ITE IT5570E on the Sixunited AXB35-02 board) runs the fans on its
own firmware curve. The `pve_ec_fan` role (`ansible/roles/pve_ec_fan`, defaults hold the why) replaces
that curve for the two CPU fans and sets the APU power mode:

- **Driver:** the out-of-tree `ec_su_axb35` module from github.com/cmetz/ec-su_axb35-linux, pinned to a
  reviewed full commit SHA and built by DKMS (`/usr/src/ec_su_axb35-<version>`, installed under
  `/lib/modules/<kernel>/updates/dkms/`). The role builds it for the running kernel and for every other
  installed kernel that has its headers. DKMS builds it for each kernel installed later
  (`proxmox-default-headers` brings the headers with the default kernel). Loaded at boot by
  `/etc/modules-load.d/ec_su_axb35.conf`. Loading it only reads the EC; unloading it resets all three
  fans to auto.
- **Curve:** `ec-fan-profile.service` (oneshot, after `systemd-modules-load`) writes the rampup and
  rampdown curves of each fan in `pve_ec_fan_fans` (fan1 and fan2), then sets it to `curve`. A CPU fan
  dropped from that list goes back to `auto`. From then on the driver re-reads the EC temperature every
  second and moves one level at a time. Stopping the service sets both CPU fans back to `auto`. The
  default is the quiet curve the owner asked for on 2026-11-19 (the fans were too loud): 20 % from 40 C,
  40 % from 55, 60 % from 80, 80 % from 90, and 100 % from 95, with 5 C of hysteresis. Level 4 only
  engages from 90 C EC. With it and the quiet power mode, node1/node2 went from EC 85-92 C at level 4-5
  (4150-4790 rpm) to EC 67-69 C at level 2 (~2650 rpm), NVMe drives at 43-56 C.
- **APU power mode:** the service writes `pve_ec_fan_power_mode` (default `quiet`; also `balanced`, the
  AXB35's factory EC default, or `performance`) to `apu/power_mode` on start, and sets `balanced` on stop
  (it does not restore a mode that was set before the role). Measured on ai-node1 under CI load
  (2026-11-19): `quiet` caps the APU at 54 W and costs ~16 % clock against `balanced` (82 W), for EC 73 C
  instead of 86 C, fans at level 2 instead of 3, and ~6 dB less noise; `performance` gains nothing at this
  load; the NVMe drives are unaffected (the table is in the role defaults).
- **Converge on the live state:** each converge runs `ec-fan-profile check`, which reads the sysfs, and
  restarts the service when the fans are not on the profile. That covers a module reloaded under the
  active service, a start that failed at boot, and a value written by hand. The role fetches the driver
  source only when the checkout is missing, not at the pin, or edited, so converging a current host does
  not need github.com.
- **fan3**, the small system fan, is controllable through the driver as well (measured on ai-node2: fixed
  100 % = 2557 rpm, 20 % = 625, auto = 970). It is not managed by this role yet and stays on the EC's own
  curve (owner decision pending), so the role never writes it and only reports it. ai-node1's fan3 reads
  0 rpm even at a fixed 100 %: that fan is unplugged or failed, a hardware fault unrelated to the driver.
  No alert covers fan3 for that reason; a per-node fan3 alert is a possible follow-up.
- **Metrics:** `ec-fan-textfile.timer` writes
  `/var/lib/prometheus/node-exporter/ec_fan.prom` every 30 s. It reports `ec_fan_rpm`, `ec_fan_level` and
  `ec_fan_mode_info` per fan, `ec_fan_managed` per CPU fan, plus `ec_temp_celsius`,
  `ec_apu_power_mode_info` and `ec_fan_driver_loaded`. The alerts are in `kubernetes/apps/infrastructure/monitoring/pve-ec-fan-rules.yaml`:
  a CPU fan under 1000 rpm above 70 C, a managed CPU fan not in curve mode for 15 min, the file going stale,
  and the driver not loaded.

**Secure Boot must be off.** The module is unsigned. With Secure Boot on, the Proxmox kernel locks down
(integrity mode) and refuses it. All three nodes have had Secure Boot off (lockdown `none`) since
2026-11-19. On a host with it on, the role's first check fails the play before anything changes.
To add a host: disable Secure Boot in its firmware during a planned host reboot (the drain procedure
above). Check that `cat /sys/kernel/security/lockdown` shows `[none]`, then set
`pve_ec_fan_enabled: true` in its host_vars. (Keeping Secure Boot on would need DKMS's signing key,
`/var/lib/dkms/mok.pub`, enrolled with `mokutil` at the console. The role does not do that, and it
refuses a Secure Boot host either way.)

One host at a time: ai-node1 (`<host-ip>` 192.168.0.2), ai-node2 (192.168.0.3), ai-node3 (192.168.0.4).

```bash
# 0. What the host runs now.
#    The role removes every OTHER ec_su_axb35 DKMS version and /usr/src/ec_su_axb35-* tree (an earlier pin,
#    or a hand install with the upstream DKMS script), so look at what `dkms status` lists first.
python scripts/node-ssh.py <host-ip> "cat /sys/kernel/security/lockdown; lsmod | grep ec_su_axb35; dkms status 2>&1; dpkg -l dkms git build-essential 'proxmox-headers-*' 2>&1 | grep ^ii; grep -rs ec_su_axb35 /etc/modules /etc/modules-load.d; cat /sys/class/ec_su_axb35/fan{1,2}/mode"
#    node2 has NO build toolchain, kernel headers or git yet (dpkg lists none): the first apply installs dkms,
#    build-essential, git, proxmox-default-headers and proxmox-headers-$(uname -r) (plus the newest
#    headers of the default series, which proxmox-default-headers pulls in) before it builds.
# 1. Preview. A first --check stops before the DKMS steps: it cannot look at packages it has not installed yet.
just ec-fan --limit <host> --check --diff
# 2. FIRST ROLLOUT ON node1 AND node2: the module running on each was loaded by hand with insmod from
#    /root/ec-su_axb35/ec_su_axb35.ko (node2's is a copy of node1's build), not from the DKMS path. Unload it, so
#    the apply loads the DKMS build. Unloading puts all three fans on the firmware curve (safe) until the apply
#    sets the curve again, a few minutes later on node2 (the packages and the build come first). The role also
#    reloads on its own when the loaded module's srcversion differs from the build (another commit), but a hand
#    build of the same commit has the same srcversion, and the role leaves that one running.
python scripts/node-ssh.py <host-ip> "rmmod ec_su_axb35; lsmod | grep -c ec_su_axb35; dmesg | grep ec_su_axb35 | tail -4"
#    expect 0 and "fan1/2/3 reset to AUTO mode", "Module unloaded"
# 3. Apply, then a second run must change nothing.
just ec-fan --limit <host>
just ec-fan --limit <host>                                      # changed=0
# 4. Verify: the DKMS build is what modprobe resolves and what is loaded, the curve is set, metrics are written.
python scripts/node-ssh.py <host-ip> "modinfo -n ec_su_axb35; dkms status ec_su_axb35; find /lib/modules/\$(uname -r) -name 'ec_su_axb35.ko*'; cat /sys/module/ec_su_axb35/srcversion; modinfo -F srcversion ec_su_axb35"
python scripts/node-ssh.py <host-ip> "systemctl is-active ec-fan-profile ec-fan-textfile.timer; cat /sys/class/ec_su_axb35/fan*/rpm; cat /sys/class/ec_su_axb35/fan{1,2}/{mode,level,rampup_curve,rampdown_curve}; cat /sys/class/ec_su_axb35/temp1/temp"
python scripts/node-ssh.py <host-ip> "cat /var/lib/prometheus/node-exporter/ec_fan.prom"
#    expect only updates/dkms/ec_su_axb35.ko*, the same srcversion twice, both units active, fan1/fan2
#    `curve` with the role's curves, and ec_fan_textfile_timestamp_seconds within the last 30 s.
#    If the role refused with "modinfo resolves ec_su_axb35 ... not the DKMS build", a `make install` from
#    the hand checkout left a copy: remove that file, run `depmod`, and converge again.
```

**Checking a host:** `cat /sys/class/ec_su_axb35/fan*/rpm` (fan1/fan2 ~1540 rpm at 20 %, ~2650 at 40 %,
~4200 at 80 %;
fan3 on the EC's own curve, ~970 rpm on node2, always 0 on node1 because of its failed fan),
`cat /sys/class/ec_su_axb35/fan{1,2}/{mode,level}`, `cat /sys/class/ec_su_axb35/temp1/temp` and
`cat /sys/class/ec_su_axb35/apu/power_mode`.
`journalctl -u ec-fan-profile` shows each write. In Grafana or Prometheus, query `ec_fan_rpm` and
`ec_temp_celsius` with `job="proxmox-node"`.

**Changing the curve:** set `pve_ec_fan_rampup_curve` / `pve_ec_fan_rampdown_curve` (five integers
0-100, strictly rising, each rampdown value below the rampup value of its level). Put them in that
host's host_vars for one host, or in the role defaults for all. Then run `just ec-fan --limit <host>`.
The role refuses a curve that breaks those rules, and any fan list other than 1 and 2. A changed config
restarts `ec-fan-profile`, so the fans are on auto for a moment and then on the new curve. Dropping a fan
from `pve_ec_fan_fans` puts it back on auto. The APU power mode is `pve_ec_fan_power_mode` (quiet, balanced
or performance; the role refuses anything else) and changes the same way. To try a curve by hand first, write it to
`/sys/class/ec_su_axb35/fan{1,2}/rampup_curve` (and `rampdown_curve`) on the host. The next converge
(which sees the fans off the profile) or the next reboot puts the role's curve back.

**Rollback:** set `pve_ec_fan_enabled: false` (or delete the line) in host_vars and run
`just ec-fan --limit <host>`. The role stops `ec-fan-profile` (its stop sets fan1/fan2 to auto and the APU
power mode to balanced). It also
stops the metrics timer, unloads the module (which resets all three fans to auto), and removes the units,
config, scripts, boot load and `ec_fan.prom`, so no alert keeps watching the host. The DKMS build stays;
to purge it run `dkms remove ec_su_axb35/<version> --all && rm -rf /usr/src/ec_su_axb35-<version>`. For
an immediate stop on the host, `systemctl stop ec-fan-profile` hands the fans back to the firmware and
sets the power mode to balanced, the factory EC default (`rmmod ec_su_axb35` resets the fans but leaves the
power mode as it was), but the next
converge or boot loads the module and the profile again until the role is switched off.

**Kernel updates:** the kernel's postinst hook runs DKMS autoinstall, and a converge also builds for every
installed kernel that has its headers. Before rebooting into a new kernel, check that
`dkms status ec_su_axb35` lists it as `installed`. A kernel without the module boots with the fans on the
firmware curve (safe), and `ProxmoxEcFanDriverNotLoaded` fires. To fix it, install
`proxmox-headers-<kernel>` and run `dkms autoinstall -k <kernel>`, or converge after the reboot. A host on
an opt-in kernel series also needs that series' headers meta package in `pve_ec_fan_packages`.

**A new driver pin:** review the new commit (every `ec_write()` in `src/ec_su_axb35.c`), then set the full
SHA in `pve_ec_fan_commit`. The converge builds the new version first; only when that build succeeds does
it remove every other DKMS version (DKMS will not install a module whose twin is still installed for the
kernel) and install the new one, so a failed build leaves the previous module installed for the next boot.
It also builds the new version for every other installed kernel that has its headers. If the loaded
module's srcversion then differs, the role
reloads it, so the fans are on auto for a moment. Before converging a new pin, run the Docker converge
harness against it: `bash ansible/roles/pve_ec_fan/tests/test-converge-docker.sh`. It builds the module
against the Proxmox headers, with systemd as PID 1 and a fake sysfs. It covers apply (the profile runs
once), idempotence, `--check`, a converge without the network, a second installed kernel, a curve change
next to a failing build for another kernel, a dropped fan, recovery after a module reload, the APU power
mode, refusals, disable (also after an apply that stopped part-way), re-pin, another DKMS version already
installed, a new pin that does not build (the installed module stays), and the lockdown refusal.

**Caveat:** the curve runs in the driver, not in the EC. If the host kernel hangs, the EC keeps the last
level it was given. A clean shutdown or reboot stops `ec-fan-profile` first, so the fans are back on auto
before the kernel goes down.

## Talos node trim (nightly; `kubernetes/apps/node-fstrim`, the agent nodes and relay-worker-1)

Each trimmed Talos VM's disk is a thin LV on its host's `local-lvm`, `discard=on`, on the host's QLC NVMe.
Talos mounts `/var` (XFS) without `discard`, and nothing trimmed it, so what a node freed (images, logs,
emptyDirs) stayed allocated in the thin pool: about 195 GiB across the seven Talos nodes on 2026-11-20. The
Flux Kustomization `node-fstrim` runs one CronJob per node whose disk is on `local-lvm`.

**Not the control planes.** Since 2026-11-20 the disks of talos-cp1/2/3 are raw files on `qnap-nfs` (ADR 0043).
There is no `local-lvm` thin space left to reclaim for them, and a guest discard there becomes hole-punching over
NFS on the share that etcd's WAL fsync depends on. So they have no CronJob.
`scripts/tests/test_node_fstrim_manifests.py` takes the trimmed set from the tofu modules' `vm_datastore`
(`local-lvm` only) and fails on a CronJob for any control plane. If the CP disks ever move back (ADR 0043,
"Rollback"), restore their overlays from git history, with slots of their own.

- The pod is pinned to its node by hostname and tolerates only the trimmed nodes' own taints
  (`dedicated=agent`, `dedicated=relay-worker`). It runs `node-fstrim.sh /host/var` from a ConfigMap in a
  digest-pinned Debian slim image. `/var` is mounted read-only, and the pod has CAP_SYS_ADMIN only (FITRIM),
  no service-account token, a read-only root and no network. The namespace is PSA `privileged` for that
  capability and the hostPath.
- `node-fstrim.sh` trims 4 GiB chunks from statfs, with `-m 64K`. The last chunk has no `-l` and runs to the
  real end, which statfs understates (XFS's log). It waits while the node's IO pressure is above 45 %
  (gives up after 3 min: exit 3), and after each chunk it rests as long as the chunk took (2-30 s).
  No chunk starts after 6 min unless it can end by then (a partial night, exit 0). The pressure wait
  counts against those 6 min.
- Progress is kept on the node in `/var/lib/node-fstrim` (the pod's only writable host path, `STATE_DIR`): a
  cursor, so a night the deadline cuts is continued by the next one and a pass always reaches the end of
  `/var`, and the time of the last complete pass. A partial night fails with **exit 9** once `/var` has
  had no complete pass for 7 days, so chronic partial nights reach the alert. Losing the directory only
  restarts the pass at the first chunk.
- XFS keeps no record of what it trimmed, so every night discards every free extent again. The thin
  pool drops what is already unmapped at once, so only what the node freed since the last night costs the
  NVMe anything.

**Slots (UTC, daily), one node at a time, 12 min each:** 02:30 talos-agent-node-1 (ai-node1), 03:18
talos-agent-node-2 (ai-node2), 03:30 talos-agent-node-3 (ai-node3), 03:42 relay-worker-1 (ai-node3), done
by 03:54. 02:42-03:18 held the control planes' slots until they were taken out. That is after Velero (02:00) and before the off-site sync
(04:00). Each slot is a hard bound: a start at most 60 s late (`startingDeadlineSeconds`), the Job
killed at 480 s (`activeDeadlineSeconds`), then 150 s for the chunk in flight
(`terminationGracePeriodSeconds`), so 690 s at worst. There are no retries (`backoffLimit: 0`).
`scripts/tests/test_node_fstrim_manifests.py` checks that arithmetic, the node set against the tofu
modules (the nodes on `local-lvm`, never a control plane), the tolerations against the nodes' taints, and the
pod's privileges.

**Other trims on the same drives.** The CI runners trim inside their VMs every night (ci-runners.md,
"Thin-pool trim"), and their windows cover every slot here but relay-worker-1's (ai-node1: ci-runner-4
to 03:10; ai-node2 and ai-node3: ci-runner-5 and -6 to 03:35). A runner slice raises the node's IO pressure
only when it slows the node's own disk, so the pressure wait is a partial guard, not an interlock. That overlap is the same class of residual risk as the runners' own (ci-runners.md
"Residual risk"). After the one-time cleanup a node frees little per night. No etcd member is on these drives
any more (ADR 0043).

**Rollout.** Merging deploys the four CronJobs; the first run is that night's slot. To watch the first one
by hand without waiting, trigger a single node:

```bash
kubectl --context admin@ai -n node-fstrim get cronjobs
kubectl --context admin@ai -n node-fstrim create job --from=cronjob/node-fstrim-talos-agent-node-1 fstrim-manual-1
kubectl --context admin@ai -n node-fstrim logs -f job/fstrim-manual-1   # one line per chunk, then `complete - exit 0 (done)`
# then the host side: the node's thin LV (vmid 4301) should have dropped
python scripts/node-ssh.py 192.168.0.2 "lvs pve/vm-4301-disk-0"
```

No slot touches a control plane's disk, so a run needs no etcd watch beyond the usual alerts.

**Alert.** `TalosNodeFstrimNotSucceeding` (warning,
`kubernetes/apps/infrastructure/monitoring/node-fstrim-rules.yaml`) fires when a node's CronJob has not
succeeded for 50 h (two nights), or is older than 50 h and has never succeeded. A suspended CronJob is never
alerted on. One failed night is not alerted. The pod log says why: exit 3 means pressure stayed high, 4
pressure unreadable, 5 fstrim failed, 6 the progress could not be written to `/var/lib/node-fstrim`
(full or read-only), 9 partial nights with no complete pass for 7 days, and
DeadlineExceeded means the slot ran out. `kubectl debug node/<node>` and `cat /host/var/lib/node-fstrim/*`
shows the cursor and the last complete pass.

**Kill switch.** Set `suspend: true` on the CronJobs. In git, set it in `base/cronjob.yaml`. Live, run
`flux suspend kustomization node-fstrim` first, then `kubectl -n node-fstrim patch cronjob <name> -p
'{"spec":{"suspend":true}}'`. Deleting a running pod is clean: it finishes its chunk first.

Tests: `bash scripts/tests/test-node-fstrim.sh` (stubs, CI: manifests.yaml),
`scripts/tests/test_node_fstrim_manifests.py` (script-tests), and the manual
`bash scripts/tests/test-node-fstrim-kernel-docker.sh`. The manual test runs the script in the CronJob's
image, with CAP_SYS_ADMIN only, against read-only binds of a real XFS and ext4 on loop devices, and checks
that the freed space comes back out of the sparse backing files.

## Post-maintenance checklist (gates before touching the next node)

```bash
_out/talosctl-1142.exe -n 192.168.0.41 etcd status                  # 3/3 in-sync
kubectl --context admin@ai get nodes                                # all Ready, none SchedulingDisabled
                                                                    # (Talos uncordons on boot; `kubectl uncordon` if stuck)
curl -s -m 10 http://<ai-lxc-ip>:8082/v1/models                     # AI LXC: llama-swap answering
# CONTENT check -- a wedged backend serves /v1/models perfectly well, so a 200 there proves
# nothing. This must print `OK stop` (allow a cold model load: ~24 s on node2, ~70 s on node3):
curl -s -m 300 http://<ai-lxc-ip>:8082/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"<served-name>","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":8,"temperature":0,"chat_template_kwargs":{"enable_thinking":false}}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["choices"][0]; print(d["message"]["content"], d["finish_reason"])'
# Expect exactly `OK stop`. **`enable_thinking:false` is load-bearing**: qwen3.8-27b-ailab runs
# --reasoning-format deepseek --reasoning-budget 16384, so WITHOUT it this same request spends
# the budget thinking and returns EMPTY content with finish_reason=length on a perfectly
# HEALTHY backend -- measured on node2 2026-09-21, direct to the backend, not through the proxy.
# finish_reason is printed so an empty answer can never be read as a pass.
python scripts/node-ssh.py <host-ip> "pct status <ctid>"           # AI LXC running (pct start if not)
kubectl --context admin@ai get pods -A | grep -vE 'Running|Completed'   # nothing stuck
kubectl --context admin@ai get pdb -A                               # ALLOWED DISRUPTIONS back >0 (except *-primary: 0 by design)
kubectl --context admin@ai get cluster -A 2>/dev/null               # CNPG: "Cluster in healthy state"
# strive-pg specifically: all THREE instances back (the evicted one re-attaches its two volumes,
# 2–5 min each) and both replicas streaming again before the next CP — required anti-affinity
# means a still-Pending instance has nowhere else to go, so a second drain now would leave 1/3.
kubectl --context admin@ai -n strive-ailab get pods -l cnpg.io/cluster=strive-pg -o wide   # 3 Running, one per CP
kubectl --context admin@ai get volumeattachments | grep -vE 'true'  # nothing stuck detaching/attaching
```

- gatus green (status.chifor.me); Prometheus targets up; `ha` PrometheusRule alerts resolved.
- out-of-service taint removed (if used); Alertmanager silences cleaned up (if created).
- CNPG note: k8s never rebalances on its own — if a CNPG instance doubled up on a node during the
  window (preferred anti-affinity), delete that pod (**a replica, never the primary**) once the node
  is back; it reschedules onto the empty node.
