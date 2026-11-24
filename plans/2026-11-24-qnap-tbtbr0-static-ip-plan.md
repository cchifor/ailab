# Give the QNAP Thunderbolt bridge its service IP at boot (static `tbtbr0`), proven by one planned NAS reboot

Status: prepared, window not scheduled · Created: 2026-11-24 · Script: `scripts/qnap-tbtbr0-static-ip.sh`
Follows the 2026-11-24 power cut, the second outage of this kind. The first was 2026-10-30. Runbook:
`docs/runbooks/qnap-storage-setup.md` §2 and §12. Backstop already live: the iSCSI portal watchdog (#1974).

## Root cause (measured on the NAS, 2026-11-24 boot)

QNAP's own network config (`/etc/config/nm.conf`, section `[tbtbr0]`) has the Thunderbolt bridge as `ip4_type = DHCP`.
The service IP `10.55.0.254` was never in QNAP's config: our cron reconciler `/etc/config/tb-storage-ip.sh` adds it, once
a minute (runbook §2 chose this because `qcli_network` cannot address the system bridge). The boot order, from `dmesg`,
`/var/log/network/bootup.log` and `/etc/rcS.d/`:

| Uptime | Step | `tbtbr0` |
|---|---|---|
| ~86 s | `S45network`: QNAP's network manager creates the bridge (member `dummy0`). Its `setup_ipv4` reads `ip4_type: DHCP` and starts `avahi-autoipd` + `dhclient` | link-local `169.254.6.247` (no DHCP server on the link) |
| 116–191 s | `S46iSCSItrgt`: `scst_util --load` resolves each target's interface binding (`tbtbr0,lo`) to IPs **once** (`iscsiadmin add_allow_portal`) | all 44 targets bound to `169.254.6.247` + `127.0.0.1` |
| ~191 s+ | `S70crond` starts; the reconciler adds `10.55.0.254` | link-local + `10.55.0.254` |
| 261 s | `S99nfs`, then `S99thunderbolt.sh`: `ip link del tbtbr0`, then `brctl addbr tbtbr0`. This happens outside the network manager, so **no address is applied** (the bridge's ifindex changes from ~7 to 11) | no address until the next cron minute, then `10.55.0.254` |

Every initiator then gets "No portals found" and a refused login, so no `qnap-iscsi` volume mounts. The reconciler's other
job, a once-per-boot `nfs restart` for "a QNAP boot-race drops the per-subnet rw rule", is very likely the same race
seen from NFS.

## The change

`scripts/qnap-tbtbr0-static-ip.sh` writes `ip4_type=STATIC ip=10.55.0.254 subnet_mask=255.255.255.0` into
`[tbtbr0]` through QNAP's network CLI (`/usr/local/bin/nacli setcfg`). The layout is the same as QNAP's other static
virtual switches (`lxcbr0`, `docker0`). With it, `S45network` puts `10.55.0.254` on the first bridge at ~86 s, before
`S46iSCSItrgt` binds the portals and before `S99nfs`.

What it does not change, on purpose:
- **The cron reconciler stays.** `S99thunderbolt.sh` recreates the bridge with no address. The iSCSI portal binding is an
  IP filter, so it stays valid once the reconciler re-adds the same IP (≤ 60 s).
- **The watchdog stays**, as the backstop for anything else that leaves the binding stale.
- **The NFS re-export in the reconciler stays** until the window shows whether the rw-rule race is gone.

Measured before the window (read-only, and the interlock): `nacli getcfg` reads the section
(`now: ip4_type=DHCP ip= subnet_mask=255.255.0.0`). `APPLY=1` with the cluster up stopped at
`interlock: NAS-wide iSCSI sessions=30`, wrote no backup, and left the config at `DHCP`.

### Why a window, and not "just write it now"

- **Writing:** whether QNAP's network daemon re-applies a written value to the live bridge is unknown. A live re-apply can
  flush `10.55.0.254`, and that address carries the control-plane disks (NFS, ADR 0043) and every iSCSI session.
  So the script refuses `APPLY=1` unless the NAS-wide iSCSI session count is 0 (cluster down).
- **Proving:** the only proof is a NAS boot. Since 2026-11-20 a NAS boot is a full-cluster outage. The window doubles as
  the first rehearsal of a planned NAS outage, which `node-maintenance.md` lists as an open item ("write and
  rehearse one before the first planned NAS reboot").

The alternative is to wait for the next unplanned power cut, with the watchdog covering it. But the config cannot be
written safely while the cluster is up, and an unplanned boot does not give a clean before/after. Recommended: the window.

## Window procedure (est. 45–60 min full outage)

Paths relative to the ops checkout `C:\Users\chifo\work\home\ailab` (it holds `.env` and `kubernetes/infra/_out/`).
`T=kubernetes/infra/_out/talosctl-1142.exe --talosconfig kubernetes/infra/_out/talosconfig`,
`K="kubectl --context admin@ai"`.

### 0. Before the day

- The owner picks the time. Open a `rollout-freeze` issue in `cchifor/platform`, and tell dev-worker users: the llm-router,
  Gitea and CI are down for the window.
- `python scripts/qnap-ssh.py --sudo "for d in 0 1 2 3 4; do nvme smart-log /dev/nvme\$d | grep unsafe_shutdowns; done"`.
  Note the value (11 on 2026-11-24). A clean NAS reboot must not raise it.

### 1. Pre-checks (cluster up)

```bash
$T -n 192.168.0.41,192.168.0.42,192.168.0.43 etcd status            # 3/3 in sync
$T -n 192.168.0.41 -e 192.168.0.41 etcd snapshot kubernetes/infra/_out/etcd-snapshots/pre-nas-reboot-<date>.db
$K get clusters.postgresql.cnpg.io -A                               # 5/5 healthy
$K get backups.postgresql.cnpg.io -A --sort-by=.metadata.creationTimestamp | tail -5   # a recent completed backup per cluster
python scripts/qnap-ssh.py --sudo "sh /share/ZFS2_DATA/.iscsi-portal-watchdog/watchdog.sh --check"   # ok, sessions=N
APPLY=0 bash scripts/qnap-tbtbr0-static-ip.sh                       # read-only: now DHCP, want STATIC
```

Wait for in-flight CI runs to end (Gitea Actions queue empty). A run cut mid-job is reruns, not damage.

### 2. Stop the cluster without moving volumes

Cordon **all seven** nodes first. Each `talosctl shutdown` drains its own node. With nowhere to schedule, the evicted
pods go Pending: they do NOT re-attach their iSCSI volumes on another node, which is the 2026-10-09 reformat-on-reattach
path.

```bash
for n in talos-cp1 talos-cp2 talos-cp3 talos-agent-node-1 talos-agent-node-2 talos-agent-node-3 relay-worker-1; do $K cordon $n; done
$T shutdown -n 192.168.0.47,192.168.0.48,192.168.0.49,192.168.0.52  # workers first; drain timeout 5 min
# poll until stopped: 4301 on .2, 4302 on .3, 4303 + 4304 on .4
python scripts/node-ssh.py 192.168.0.4 "for i in \$(seq 1 90); do qm status 4303 | grep -q stopped && qm status 4304 | grep -q stopped && break; sleep 5; done; qm status 4303; qm status 4304"
$T shutdown -n 192.168.0.41,192.168.0.42,192.168.0.43               # then all three CPs
# poll 4001 on .2, 4002 on .3, 4003 on .4. If one is still up after 10 min (the last CP has lost quorum, so its drain
# cannot reach the API), `qm stop <vmid>` it: the other two have stopped, and its etcd data is fsynced.
```

Then stop the NAS's other consumers on the hosts:

```bash
python scripts/node-ssh.py 192.168.0.3 "pct shutdown 5002 --timeout 120; pct status 5002"   # ai-llm-2: models on hard NFS
python scripts/node-ssh.py 192.168.0.4 "pct shutdown 5003 --timeout 120; pct status 5003"   # ai-llm-3
```

Leave the hosts' `qnap-nfs` storage mounted (hard mount; it resumes). Runners, dev-workers, reviewers and the registry
are on `local-lvm` and stay up. They only lose Gitea and the cluster for the window. The cloudlab LLM hosts (`.26`–`.28`)
also read models from this NAS: loaded models keep serving, and their `qnap-nfs-mount.service` handles the gap.

Gate: `python scripts/qnap-ssh.py --sudo "iscsiadmin get_target | grep -c 'sess_cnt = [1-9]'"` → `0`.

### 3. Write the config and reboot the NAS

```bash
APPLY=1 bash scripts/qnap-tbtbr0-static-ip.sh        # interlock passes (sessions=0); writes and reads back STATIC
python scripts/qnap-ssh.py --sudo "cat /etc/config/nm.conf" > kubernetes/infra/_out/qnap-nm.conf.<date>-after-static
python scripts/qnap-ssh.py --sudo "( trap '' HUP; exec /sbin/reboot ) >/dev/null 2>&1 < /dev/null &"
```

`/sbin/reboot` is busybox. The NAS's `/etc/inittab` has `null::shutdown:/etc/init.d/rcK`, so it runs QNAP's 57
`/etc/rcK.d/K*` scripts: a clean shutdown (the last one logged `K60network` in `/var/log/network/shutdown.log`).

Poll `ping 10.55.0.254` and `python scripts/qnap-ssh.py "uptime"`. The S99 steps finish ~4.5 min after kernel start.
Wait until uptime > 6 min.

### 4. Verify on the NAS (this is the test)

| Check | Command (via `qnap-ssh.py --sudo`) | Pass |
|---|---|---|
| Static applied at boot | `grep 'Interface : tbtbr0 , ip4_type' /var/log/network/bootup.log` | `ip4_type: STATIC` |
| Portals bound right **from boot** | `grep add_allow_portal /var/log/hero_iscsi_scst.log \| tail -3` and `iscsiadmin get_allow_portal <any iqn>` | `10.55.0.254`, no `169.254.*` |
| Root fix, not the backstop | `cat /share/ZFS2_DATA/.iscsi-portal-watchdog/watchdog.log` (absent or no new lines) and `sh …/watchdog.sh --check` | no restart this boot; `ok: 44 targets` |
| Rebuilt bridge got the IP | `ip -4 addr show tbtbr0`; `dmesg \| grep -E 'Remove tbtbr0\|S99'` | `10.55.0.254/24` after the S99 rebuild |
| NFS | `exportfs -v \| grep pve-nfs`; from each host `findmnt /mnt/pve/qnap-nfs && ls /mnt/pve/qnap-nfs/images` | rw for `10.55.0.0/24` and `10.55.1.0/24`; listing answers |
| NFS rw race (observation) | `exportfs -v` **before** the reconciler's first run, if you are fast | record whether the rw rule is present without the re-export |
| versitygw | `grep ' sdb$' /proc/partitions; netstat -lnt \| grep ':7070 '` | present, listening |
| Clean shutdown | `nvme smart-log /dev/nvme0` … `unsafe_shutdowns` | unchanged from step 0 |

Also record the uptime at which `10.55.0.254` first appeared (`bootup.log` setup_ipv4 time vs `dmesg` boot), for the runbook.

### 5. Bring the cluster back

```bash
for h in 192.168.0.2 192.168.0.3 192.168.0.4; do python scripts/node-ssh.py $h "findmnt -n /mnt/pve/qnap-nfs"; done
python scripts/node-ssh.py 192.168.0.2 "qm start 4001"; python scripts/node-ssh.py 192.168.0.3 "qm start 4002"; python scripts/node-ssh.py 192.168.0.4 "qm start 4003"
$T -n 192.168.0.41,192.168.0.42,192.168.0.43 etcd status            # wait for 3/3 in sync
python scripts/node-ssh.py 192.168.0.2 "qm start 4301"; python scripts/node-ssh.py 192.168.0.3 "qm start 4302"; python scripts/node-ssh.py 192.168.0.4 "qm start 4303; qm start 4304"
$K get nodes                                                         # all 7 Ready
for n in talos-cp1 talos-cp2 talos-cp3 talos-agent-node-1 talos-agent-node-2 talos-agent-node-3 relay-worker-1; do $K uncordon $n; done
python scripts/node-ssh.py 192.168.0.3 "pct start 5002"; python scripts/node-ssh.py 192.168.0.4 "pct start 5003"
```

### 6. Post-checks

These are the same as after a power cut (`qnap-storage-setup.md` §12 and `node-maintenance.md`'s post-maintenance checklist):
- no pod outside Running/Completed (crashloop backoffs settle in ≤ 5 min);
- CNPG 5/5 healthy with `ContinuousArchiving=True`;
- every Flux kustomization Ready;
- ExternalSecrets synced (force-sync any that refreshed during the window);
- the ext4 `s_mkfs_time` check on every mounted iSCSI volume;
- the public endpoints (`git.`, `sso.`, `home.`, `grafana.`, `router.chifor.me`);
- CI runners `active`;
- the AI LXCs serve `:8082/v1/models`.

Close the rollout-freeze issue.

### 7. Rollback

- **The bridge or NFS misbehaves after boot** (no `10.55.0.254`, exports wrong): the reconciler re-adds the IP within a
  minute anyway. If something is still wrong, `REVERT=1 bash scripts/qnap-tbtbr0-static-ip.sh` (interlock: run it before
  step 5) and reboot the NAS once more. That is the old behaviour, with the watchdog in place.
- **Portals still bound to `169.254.*`:** the static setting did not reach `S46` in time. The watchdog fixes the binding
  within ~3 min (check `watchdog.log`). Record it, keep or revert the static config (harmless either way), and re-open
  the root-cause question.
- **A CP does not come back:** follow `node-maintenance.md` (boot-order guard, `qm start`). Restore from the step 1 etcd
  snapshot only per the Talos disaster-recovery docs, never by hand.

## After the window

- Record the results here (the step 4 table, timings, unsafe_shutdowns) and set the status line.
- Update `qnap-storage-setup.md` §2/§12 from "pending the window" to the measured result.
- Promote steps 1–6 into `node-maintenance.md` as the rehearsed planned-NAS-outage procedure, and close its open item.
- If the NFS rw rule held without the re-export, a follow-up PR can drop the reconciler's `nfs restart`.
