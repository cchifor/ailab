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

| Proxmox host | mgmt IP | Talos CP | CP IP | CP vmid | AI LXC ctid |
|---|---|---|---|---|---|
| ai-node1 | 192.168.0.2 | talos-cp1 | 192.168.0.41 | 4001 | 5001 |
| ai-node2 | 192.168.0.3 | talos-cp2 | 192.168.0.42 | 4002 | 5002 |
| ai-node3 | 192.168.0.4 | talos-cp3 | 192.168.0.43 | 4003 | 5003 |

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
