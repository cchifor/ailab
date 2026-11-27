# Give the QNAP Thunderbolt bridge its service IP at boot (static `tbtbr0`)

Status: **the first NAS boot after the write (2026-11-27) FAILED**: the written config was incomplete (DNS type), so the
bridge was not created. Fixed the same evening through QNAP's own validation path, and the script below was deleted
(see "Results"; runbook §2 is current) · Created: 2026-11-24 · Script: `scripts/qnap-tbtbr0-static-ip.sh` (deleted)
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
| 245 s | `S70crond` starts | link-local |
| 261 s | `S99nfs` starts NFS, then `S99thunderbolt.sh` loads the Thunderbolt driver and brings `tbtbr0` up; the ports join at ~281 s | link-local |
| ~301 s | the reconciler's first run, at the next cron minute (its re-export flag `/tmp/.ailab-nfs-reexported` has mtime uptime 301 s): adds `10.55.0.254`, re-exports NFS. `avahi-autoipd` then releases its link-local (it sleeps while a routable address exists) | `10.55.0.254` |

**Correction (same day):** an earlier version of this table said `S99thunderbolt.sh` deletes and recreates the bridge
without an address. That was wrong. The script logs `Remove tbtbr0` unconditionally, but it runs `ip link del` only when
the loop-prevention flag `/tmp/nasconfig_tmp/tbtbr_disable` exists (absent here), and `brctl addbr` only when
`/mnt/ext/opt/netmgr` is absent (present here). Evidence: `dummy0` is still port 1 from 86 s, and `tbtnet0p0` /
`tbtnet1p0` joined as ports 2 and 3 at 281 s. A recreated bridge would not contain `dummy0`. The ifindex reasoning
behind the original claim was mistaken: index 11 is simply the order in which the network manager created its switches.

Every initiator then gets "No portals found" and a refused login, so no `qnap-iscsi` volume mounts. The reconciler's other
job, a once-per-boot `nfs restart` for "a QNAP boot-race drops the per-subnet rw rule", is very likely the same race
seen from NFS.

## The change

`scripts/qnap-tbtbr0-static-ip.sh` writes `ip4_type=STATIC ip=10.55.0.254 subnet_mask=255.255.255.0` into
`[tbtbr0]` through QNAP's network CLI (`/usr/local/bin/nacli setcfg`). The layout is the same as QNAP's other static
virtual switches (`lxcbr0`, `docker0`). With it, `S45network` puts `10.55.0.254` on the first bridge at ~86 s, before
`S46iSCSItrgt` binds the portals and before `S99nfs`.

What it does not change yet:
- **The cron reconciler stays until a boot proves the static config.** Today's running bridge still has the DHCP config
  (`dhclient` and `avahi-autoipd` run on it), and the reconciler is what holds `10.55.0.254` on it. Since S99 does not
  rebuild the bridge, the reconciler's address line has nothing left to do at boot once the static config applies.
  Drop it after a boot that passes step 4.
- **The watchdog stays**, as the backstop for anything else that leaves the binding stale.
- **The NFS re-export in the reconciler stays** until a boot shows whether the rw-rule race is gone. The timing fits
  the same root cause: `S99nfs` started NFS at 261 s while the bridge had only its link-local address, and the
  reconciler added `10.55.0.254` and re-exported at ~301 s. With the static config, NFS starts with the address in
  place. **But no observation available today can prove the re-export unnecessary.**
  - The reconciler runs on the first cron minute after `crond` starts (anywhere ~1–60 s later), so no uptime is a
    safe cutoff.
  - An export table read later shows the state after the re-export, not before.

  A valid test needs a timestamped `exportfs -v` snapshot taken after `S99nfs` and **immediately before** the
  re-export, i.e. inside the reconciler. A boot without that snapshot, or one where the re-export flag already
  existed, is inconclusive. Follow-up: add the snapshot to the reconciler and give it its own installer
  (`qnap-setup.sh` also re-applies eth1 and the share, so it is not a safe way to redeploy it). Until a snapshot shows
  the rw rule present before the re-export, the re-export stays.

Measured before the window, on the live NAS:
- Read-only: `nacli getcfg` reads the section (`now: ip4_type=DHCP ip= subnet_mask=255.255.0.0`).
- Interlock: `APPLY=1` with the cluster up stopped at `interlock: NAS-wide iSCSI sessions=30`. It wrote nothing, and
  the config is still `DHCP`.
- Rehearsal on a scratch copy (`NM_CONF=/tmp/nm.conf.rehearse`, which the network daemon does not read):
  - `APPLY=1` changed exactly `ip`, `ip4_type` and `subnet_mask` in `[tbtbr0]` (the `diff` against the live file
    shows nothing else).
  - `REVERT=1` wrote `ip = ` (an empty value works through `nacli setcfg`), and the copy came back **byte-identical**
    to the live file.
  - Each write left its own timestamped pre-image.
  - The live `nm.conf` md5 (`37acce9d…`) and the bridge's `10.55.0.254` were unchanged throughout.

### Decision (2026-11-24): write it live, prove it at the next boot

The first version of this plan wanted a 45–60 min full-cluster window for two reasons. The owner asked why a static
address needs that.
- **Writing:** it was unknown whether QNAP's network daemon re-applies a written value to the live bridge. A live re-apply
  could flush `10.55.0.254`, the address that carries the control-plane disks (NFS, ADR 0043) and every iSCSI session.
  Settled by measurement:
  - `nacli setcfg` is only a load / change one key / save through `ncaas`, QNAP's network config store. Its own debug
    log in `/var/log/network/main.log` shows no apply step; applying is the separate `nacli setup_by_config` /
    `reconfig`.
  - `ncaas`'s API (`cfgapi`: `read_ini`, `write_ini`, `sync_ini`, `write_back`, `write_key`) has no hook that applies.
  - The only inotify watchers on the config directory are `hda_monitor` (it checks the partition stays writable) and
    `qmmsd`. Neither belongs to the network stack.
  - `nm.conf`'s mtime stays at its boot-time write.
- **Proving:** the only proof is a NAS boot, which is a full-cluster outage. The NAS has had four unplanned boots in four
  weeks, and the watchdog covers iSCSI if the static setting does not, so the proof can come from the next natural boot.

So the script gained `LIVE_WRITE=1`: a write with sessions logged in. A 30 s check follows, immediately and then every
5 s: the bridge must keep `10.55.0.254`, or the run fails and re-adds it at once. A lower session count is only
reported, because counts lag a real flush and move for unrelated reasons. An unknown session count still refuses.

## Results

- **Live write: done 2026-11-24 13:48:07Z** (from main `ff401821`, #1977; script md5
  `b3a85d452d822ff35c54c66c1d449739`). Times are from the NAS clock. The NAS runs EET (UTC+2), so its file names and
  logs read 15:48. The workstation clock ran ~38 s behind the NAS and the cluster.
  - Before: the live `nm.conf` md5 was `37acce9db4da2d38670a01292f9cccde`, and a scratch-copy APPLY/REVERT rehearsal
    came back byte-identical.
  - Run:
    - interlock `sessions=30`;
    - pre-image `/etc/config/nm.conf.bak-tbtbr0-20261124-154806` (15:48:06 EET);
    - the three `nacli setcfg` calls in `/var/log/network/main.log` at 15:48:06.938–15:48:07.004 EET;
    - read-back `STATIC|10.55.0.254|255.255.255.0`;
    - live check every 5 s from t+0 to t+30 s (to ~15:48:38 EET): `10.55.0.254/24 sessions=30` at every step;
      `OK`, rc 0.
  - **The backup to revert to is the on-NAS pre-image.** Its md5 was verified as `37acce9db4da2d38670a01292f9cccde`,
    equal to the pre-write live file. The operator's `_out/qnap-nm.conf.20261124-before-tbtbr0-static` has the same
    md5, but `_out/` is gitignored (local only).
  - After:
    - QNAP's network logs show 0 `setup_ipv4` / `setup_by_config` / `reconfig` / `br_delbr` / `disable_ipv4` events
      between 15:48:06 and 15:48:38 EET;
    - the read-only mode says `already as wanted`;
    - the watchdog check reports `ok: 44 targets … sessions=30`;
    - 7/7 nodes are Ready, with no unhealthy pod;
    - etcd shows the same raft index on all three members.
- **First NAS boot after it (2026-11-27, cold boot; the hosts were powered on ~6 min before the NAS): FAILED.**
  - `bootup.log` at `S45network`: `check_config_value() >>> Incompatible dns_type: 0 due to ipv4 type is STATIC` →
    `setup_by_config tbtbr0 failed`. **The bridge was not created at all.** The write had set `ip4_type`, `ip` and
    `subnet_mask`, but every STATIC interface on the NAS (eth1, lxcbr0, docker0, lxdbr0) also has
    `domain_name_server_type = 1` and `domain_name_server4_type = 1`; `/lib/libnetworkinterface.so` contains "With
    'STATIC' IPv4_type, force setting dns_type to manual(1)", which QNAP's UI path does and `nacli setcfg` does not.
    The claim above that the layout is "the same as QNAP's other static virtual switches" was never diffed.
  - Consequences: the cron reconciler had no bridge to address; every host's NFS gate timed out, so all three CPs stayed
    stopped; QNAP's own Thunderbolt hotplug (`add_thunderbolt_to_vswitch`) failed with `-0x1105`; and `S46` bound every
    iSCSI target to `127.0.0.1` only, which the watchdog read as healthy.
  - Recovery that night (~10 min): the bridge built by hand (`ip link add tbtbr0 type bridge`, members, address), an
    `iscsitrgt.sh restart` at 0 sessions, `qm start` of the CPs.
  - Fix (22:31–22:34 EET, cluster up, 31 iSCSI sessions, no interruption: 0 failures in ~200 one-second probes from
    each host): QNAP's UI edit (`PUT /netmgr/api.fcgi/api/vswitch/n/tbtbr0`, which runs `qs_util -M vswitch -A edit`)
    was refused ("Invalid interface name": its interface list still had the boot's failed state). `nacli setup_by_config
    tbtbr0` takes the in-place `alterdev` path but first validates the boot-time record in `nm.curr`, failed on the
    same DNS type, and copied that record back over `nm.conf`. With the two DNS keys set to 1 in both `nm.conf` and
    `nm.curr`, it passed (`check_config_value() >>> Exit 0`, rc 0) and committed `nm.conf`. QNAP's hotplug command then
    succeeded on a port ("Setup by Config Success : 0x0"). Runbook §2 has the procedure.
  - Still to prove at the next NAS boot: `bootup.log` has no "Incompatible", `tbtbr0` comes up at `S45` with
    `10.55.0.254`, the portals are on `10.55.0.254` from boot, `watchdog.log` and `grep tb-storage-ip
    /var/log/messages` show no action, and the CPs start without help.

The window procedure below is kept as the first written planned-NAS-outage procedure (for firmware updates or pool work),
not as a precondition of this change. Its steps 3–4 apply to any planned NAS reboot.

## Window procedure (est. 45–60 min full outage; kept for planned NAS outages)

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
# The whole sequence is ONE function that returns at the first failed check, so no shutdown can run after a failed
# cordon or gate. (A pasted list of commands carries on past a printed "STOP".)
NODES="talos-cp1 talos-cp2 talos-cp3 talos-agent-node-1 talos-agent-node-2 talos-agent-node-3 relay-worker-1"
wait_stopped() {  # $1 = host, $2 = vmid, $3 = attempts of 5 s; succeeds only on "status: stopped"
  python scripts/node-ssh.py "$1" "for i in \$(seq 1 $3); do qm status $2 | grep -q 'status: stopped' && break; sleep 5; done; qm status $2" \
    | grep -q 'status: stopped'
}
stop_cluster() {
  for n in $NODES; do $K cordon "$n" || { echo "STOP: cordon $n failed"; return 1; }; done
  # GATE: exactly these 7 nodes, every one unschedulable. One schedulable node would receive the evicted pods and
  # re-attach their volumes, which is what this step exists to prevent.
  total=$($K get nodes --no-headers | wc -l)
  cordoned=$($K get nodes -o jsonpath='{range .items[*]}{.spec.unschedulable}{"\n"}{end}' | grep -c '^true$')
  echo "cordoned $cordoned of $total"
  [ "$total" = 7 ] && [ "$cordoned" = 7 ] || { echo "STOP: not 7 of 7 cordoned"; return 1; }

  $T shutdown -n 192.168.0.47,192.168.0.48,192.168.0.49,192.168.0.52 || { echo "STOP: worker shutdown failed"; return 1; }
  for hv in "192.168.0.2 4301" "192.168.0.3 4302" "192.168.0.4 4303" "192.168.0.4 4304"; do
    wait_stopped $hv 120 || { echo "STOP: worker VM $hv not stopped after 10 min; CPs left up"; return 1; }
  done
  echo "workers stopped"

  $T shutdown -n 192.168.0.41,192.168.0.42,192.168.0.43 || echo "note: talosctl reported an error; checking the VMs"
  for hv in "192.168.0.2 4001" "192.168.0.3 4002" "192.168.0.4 4003"; do
    wait_stopped $hv 120 || echo "WARN: CP $hv still up after 10 min (see below)"
  done
}
stop_cluster && echo "CLUSTER STOPPED"
```

A CP still up after 10 min is the last one: it has lost quorum, so its drain cannot reach the API. Once the other two
show `stopped`, `qm stop <vmid>` it; its etcd data is fsynced. Do not move on until all seven Talos VMs are `stopped`.

Then stop the NAS's other consumers on the hosts:

```bash
python scripts/node-ssh.py 192.168.0.3 "pct shutdown 5002 --timeout 120; pct status 5002"   # ai-llm-2: models on hard NFS
python scripts/node-ssh.py 192.168.0.4 "pct shutdown 5003 --timeout 120; pct status 5003"   # ai-llm-3
```

Leave the hosts' `qnap-nfs` storage mounted (hard mount; it resumes). Runners, dev-workers, reviewers and the registry
are on `local-lvm` and stay up. They only lose Gitea and the cluster for the window. The cloudlab LLM hosts (`.26`–`.28`)
also read models from this NAS: loaded models keep serving, and their `qnap-nfs-mount.service` handles the gap.

Gate: `python scripts/qnap-ssh.py --sudo "sh /share/ZFS2_DATA/.iscsi-portal-watchdog/watchdog.sh --check"` reports
`sessions=0` (exit 0, or 1 if a binding is stale; either way the count is NAS-wide, and `unknown` means stop). The
`APPLY=1` interlock below checks it again.

### 3. Write the config and reboot the NAS

```bash
# rehearse on a scratch copy first (seconds; proves nacli still behaves after any QuTS update since 2026-11-24)
python scripts/qnap-ssh.py --sudo "cp /etc/config/nm.conf /tmp/nm.conf.rehearse"
NM_CONF=/tmp/nm.conf.rehearse APPLY=1 bash scripts/qnap-tbtbr0-static-ip.sh
NM_CONF=/tmp/nm.conf.rehearse REVERT=1 bash scripts/qnap-tbtbr0-static-ip.sh
python scripts/qnap-ssh.py --sudo "diff /etc/config/nm.conf /tmp/nm.conf.rehearse && echo IDENTICAL"   # must say IDENTICAL
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
| The address held through S99 | `ip -4 addr show tbtbr0`; `brctl show tbtbr0`; `dmesg \| grep 'tbtbr0: port'` | `10.55.0.254/24`; `dummy0` is still port 1 |
| The reconciler had nothing to do | `ps \| grep -E 'autoipd\|dhclient' \| grep tbtbr0` | neither runs (static config); then the reconciler's address line can go |
| NFS | `exportfs -v \| grep pve-nfs`; from each host `findmnt /mnt/pve/qnap-nfs && ls /mnt/pve/qnap-nfs/images` | rw for `10.55.0.0/24` and `10.55.1.0/24`; listing answers |
| NFS rw race | only the reconciler's pre-re-export snapshot can show it (follow-up above); a manual `exportfs -v` is not evidence | without that snapshot: inconclusive, and the re-export stays |
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
  step 5) and reboot the NAS once more. That is the old behaviour, with the watchdog in place. REVERT was rehearsed
  on a scratch copy and is byte-identical to the original. The pre-image of every write is also kept next to the file
  (`/etc/config/nm.conf.bak-tbtbr0-<timestamp>`) for a plain `cp` back.
- **Portals still bound to `169.254.*`:** the static setting did not reach `S46` in time. The watchdog fixes the binding
  within ~3 min (check `watchdog.log`). Record it, keep or revert the static config (harmless either way), and re-open
  the root-cause question.
- **A CP does not come back:** follow `node-maintenance.md` (boot-order guard, `qm start`). Restore from the step 1 etcd
  snapshot only per the Talos disaster-recovery docs, never by hand.

## After the window

- Record the results here (the step 4 table, timings, unsafe_shutdowns) and set the status line.
- Update `qnap-storage-setup.md` §2/§12 from "pending the window" to the measured result.
- Promote steps 1–6 into `node-maintenance.md` as the rehearsed planned-NAS-outage procedure, and close its open item.
- Drop the reconciler's `nfs restart` only after the reconciler's own pre-re-export snapshot (follow-up above) shows the
  rw rule present at a boot with the static config.
