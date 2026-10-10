# Runbook — QNAP QuTS hero storage setup

QuTS hero h5.2.9 on `ai-storage`. Most of this is scripted via `qcli` (persistent); exactly
**one** step needs the QNAP UI (the Thunderbolt bridge IP — qcli has no interface ID for the
system bridge). Nothing here destroys the existing **`zpool1`** RAID-Z1 pool (kept per ADR 0002).

## State captured at build time
- Pool **`zpool1`**: RAID-Z1, 5× 2 TB (Kingston KC3000, PCIe Gen3×2), ~6 TB usable, `dedup=off`. Kept.
- Shared folder **`pve-nfs`** created on it (ZFS dataset `zpool1/zfs18`), `compress=on`, thin, exported at **`/pve-nfs`** (NFS **v4.0**).
- NFS host access: `10.55.0.0/24` and `10.55.1.0/24` → `rw, no_root_squash`.

## 1. Scripted part (network + share) — `scripts/qnap-setup.sh`
```bash
bash scripts/qnap-setup.sh        # idempotent; uses qcli over SSH (.env creds)
```
This does, via `qcli`:
- `eth1` (10GbE → node3) → static **`10.55.1.254/24`**
- enable NFS (v3 + v4)
- create shared folder `pve-nfs` on `poolID=1` (thin, `compress=1`, `dedup=0`)
- NFS host access for `10.55.0.0/24` + `10.55.1.0/24` (`rw`, `no_root_squash`)

Equivalent raw qcli (for reference):
```
qcli -l user=<admin> pw=<pw> saveauthsid=yes
qcli_network -m interfaceID=eth1 IPType=STATIC IP=10.55.1.254 netmask=255.255.255.0 dns_type=manual
qcli_networkservice -n nfsServerEnabled=Enabled nfsServerEnabledV4=Enabled
qcli_sharedfolder -s sharename=pve-nfs poolID=1 comment=ProxmoxNFS guest=deny compress=1 dedup=0 type=1 size=5497558138880
qcli_sharedfolder -N sharename=pve-nfs Access=Enabled
qcli_sharedfolder -T sharename=pve-nfs HostIP=10.55.0.0/24 Permission=rw Squash=no_root_squash secure=1 sync=1 wdelay=0
qcli_sharedfolder -T sharename=pve-nfs HostIP=10.55.1.0/24 Permission=rw Squash=no_root_squash secure=1 sync=1 wdelay=0
```

## 2. Thunderbolt bridge service IP — persisted as code (no UI)
The two TB ports auto-bridge into the **"Thunderbolt Bridge (System Default)"** (`tbtbr0`), and
T2E activates automatically when a node's Thunderbolt interface comes up
(`/etc/init.d/thunderbolt_net.sh` adds the port to `tbtbr0`) — so no manual "enable T2E" step.

`qcli_network` can't target the system bridge (no interface ID), so `scripts/qnap-setup.sh`
installs an **idempotent reconciler** on the persistent DOM and a cron entry:
- `/etc/config/tb-storage-ip.sh` → `ip addr add 10.55.0.254/24 dev tbtbr0` (only if missing)
- `* * * * * /etc/config/tb-storage-ip.sh` in `/etc/config/crontab` (survives reboot)

The IP self-heals within ~1 min of any QNAP reboot — this is the **NFS service IP** the whole
cluster mounts.

*Alternative (manual, also persistent):* Control Panel → Network & Virtual Switch → Interfaces →
Thunderbolt → Static `10.55.0.254/255.255.255.0`, no gateway. If you set this, remove the cron
reconciler to avoid duplication.

## 3. Optional cleanup of default datasets
The factory left empty datasets (`ZFS1_DATA`, `ZFS530_DATA`, `Public`, `zfs1107`). They're harmless
(~51 MB). Remove via the UI (Control Panel → Shared Folders) if you want a tidy box — not required.

## Outputs consumed by the rest of the IaaC
| Value | Where |
|---|---|
| NFS server IP `10.55.0.254` | `tofu` `qnap_nfs_server`, `inventory` `storage_service_ip` |
| Export path `/pve-nfs` | `tofu` `qnap_nfs_export` |
| NFS version `vers=4.0` | `tofu` `qnap_nfs_options` |

## Validation
```bash
python scripts/node-ssh.py 192.168.0.2 'mount -t nfs -o vers=4.0 10.55.0.254:/pve-nfs /mnt/t && \
  dd if=/dev/zero of=/mnt/t/x bs=1M count=3072 conv=fdatasync; rm /mnt/t/x; umount /mnt/t'
# expect ~1.1 GB/s write over Thunderbolt
```

---

# Ongoing operation (added by the 2026-08-16 storage audit)

The build steps above leave a working NAS but an **unmaintained and unwatched** one. These four items
are what the audit added; all are on the box itself, so a factory reset or a firmware update that
resets config can silently undo them — re-check them after either.

## 4. Pool scrubbing — monthly

The pool had **never been scrubbed** in the 68 days since `zpool create` (`zpool status` read
`scan: none requested`; `zpool history` held only the create line). Note that `auto_data_scrubbing = 1`
*was* set in `uLinux.conf` and meant nothing — do not trust that flag as evidence a scrub runs.

On RAID-Z1 a scrub is the only thing that finds latent corruption before a second fault makes it
unrecoverable. Now scheduled in the persistent QNAP crontab:

```
0 5 1 * * /sbin/zpool scrub zpool1     # /etc/config/crontab, 05:00 on the 1st
```

Added idempotently and reloaded with `crontab /etc/config/crontab`. Original saved as
`/etc/config/crontab.bak.audit`. First run took **3m43s** on this pool (985 G allocated, all NVMe) and
repaired 0 with 0 errors — cheap enough that monthly is not worth debating.

```bash
python scripts/qnap-ssh.py "zpool status zpool1 | head -4"   # check scan: line
```

## 5. SNMP — the NAS's only Prometheus scrape

The NAS was the one major component with **no metrics scrape at all** (only a blackbox TCP probe to
2049/3260), and it has no SMTP either, so a failed drive in the RAID-Z1 would have alerted **nobody**.

SNMP is now enabled, and `kubernetes/apps/infrastructure/monitoring/qnap-snmp-exporter.yaml` scrapes
it. Security matters here: the factory `/etc/config/snmpd.conf` ships **`rwcommunity public`** — a
writable agent — so it was rewritten before the service was started:

- `rwcommunity` removed entirely; a single random read-only community
- source-restricted to `192.168.0.0/23` + `10.55.0.0/24` + `10.55.1.0/24`
- community lives in the SOPS secret `qnap-snmp-auth` (monitoring ns); original config saved as
  `/etc/config/snmpd.conf.bak.audit`

**If you ever re-enable SNMP from the QNAP UI, re-check that `rwcommunity` did not come back.**

Covered: per-disk SMART verdict + temperature, disk model/capacity, fan RPM, system + CPU temperature,
RAM. **Not covered** — the QNAP MIB does not expose ZFS pool state or capacity at all; the only
"volume" table it serves describes the 1 GB system volume. Pool DEGRADED is caught indirectly via the
per-disk SMART metric. Verify with:

```bash
python scripts/qnap-ssh.py 'getcfg SNMP "Service Enable"'    # TRUE
# then, in-cluster:
kubectl --context admin@ai -n monitoring logs deploy/qnap-snmp-exporter
```

## 6. Thin-LUN reclaim — UNRESOLVED, and why

The `qnap-iscsi` LUNs are thin zvols (`refreservation=none`). Nothing ever tells the array which
blocks the guest freed, so allocation only ratchets up: **103.5 GB allocated on the NAS against
55.4 GiB actually used** inside the filesystems (~48 GB stale). Worst case is the Prometheus LUN at
43.1 G of a 48 G volsize (90%) while holding 17.2 GiB — the source of the recurring `LUN has reached
the threshold (90%)` events (275 since June, across several LUNs, seen by no one until the audit).

**`mountOptions: [discard]` does not work on this backend.** It was implemented, measured, and
reverted. The Kubernetes half is fine — kubelet passes the option down faithfully:

```
volume_capability:<mount:<fs_type:"ext4" mount_flags:"discard" > >    # NodeStageVolume request
```

but `csi.trident.qnap.io` drops it. Its `NodeStageVolume` performs no mount at all (`target=` empty);
the real mount happens during publish and logs as:

```
mount_linux.MountDevice device=/dev/sdf mountpoint=/var/lib/kubelet/... options=
```

`options=` is empty. The driver mounts using its own `publish_context.mountOptions`, which is `""`,
and there is no knob for it in either the StorageClass parameters or the TridentBackendConfig — the
`qnap_config` storage pools expose only `serviceLevel` / `labels` / `features`, with no `defaults`
block. Confirm for yourself with:

```bash
kubectl --context admin@ai -n trident logs trident-node-linux-<pod> -c trident-main \
  | grep -E "MountDevice|mount_flags"
```

**What is left.** Reclaim on this backend needs a periodic `fstrim` against the kubelet mount paths,
which requires a **privileged pod with hostPath** — a real security-posture decision on a cluster
that runs `baseline` PSA and deliberately set `nodeAgent.disableHostPath` on Velero. That trade-off
is the operator's to make, so nothing was built. Weigh it against the actual impact: this is wasted
capacity and alert noise on a pool with 5.62 TB free, not a availability risk — ZFS is copy-on-write,
so a thin LUN that reaches its volsize keeps serving writes normally.

**What was done here:** set `compression=lz4` on all 15 LUN zvols (they were `compression=off` while
the parent share had it on). Applies to newly written blocks only.

## 7. QNAP-native notifications — cluster side DONE, NAS side is a UI step

Prometheus can tell you the NAS is sick; it cannot tell you anything when the *cluster* is down.
That is why the NAS needs its own outbound alerting, independent of Kubernetes. Two channels, doing
different jobs — set up **both**:

### 7a. ntfy (rich, self-hosted, open source — but shares the cluster's fate)

Everything on the cluster side is built and verified:

- ntfy user **`qnap`**, **write-only** on topic **`qnap-alerts`** — seeded declaratively by the
  `postStart` hook in `monitoring/ntfy.yaml`, password in `ntfy-qnap-auth.sops.yaml`. Write-only is
  load-bearing: the credential necessarily rides in a URL query string (below), so it must not be
  able to read alerts back. Verified: publish `200`, read `403`.
- Verified reachable **from the NAS itself** (`curl` → `200`), so DNS, egress, Cloudflare TLS and
  the credential are all proven from the box that will actually send.

The remaining step is UI-only. QuTS hero exposes **no CLI and no usable API** for Notification
Center: there is no `qcli_notification`, `qsh nc.*` is undocumented and silent, and `nc.cgi` returns
403 for CGI-derived sessions (`authLogin.cgi` yields `authPassed=1` but the sid then fails with
`authPassed=0`). Its config lives in a multi-table MyISAM schema under `/etc/config/nc/db/nc/`
(`event_channel`, `event_receiver`, `policy`, `policy_apps`, …) — hand-writing rows there is how you
get notification config that looks correct and never fires, so don't.

**Notification Center → Service Account and Device Pairing → SMS → Add SMSC Service → provider
`custom`**, then paste the URL below. Leave the provider's own Username/Password fields blank — ntfy
needs its credential inside the `auth` query parameter, and QNAP cannot send an `Authorization`
header.

```
https://ntfy.chifor.me/qnap-alerts/publish?message=@@Text@@&title=ai-storage%20@@SEVERITY@@&priority=high&tags=floppy_disk&auth=<AUTH>
```

`<AUTH>` = `base64url("Basic " + base64("qnap:<password>"))`, `=` padding stripped.

> **Rotating `ntfy-qnap-auth` is a TWO-SIDED operation.** The postStart hook reconciles ntfy to the
> Secret on every pod start, so changing the Secret alone re-points ntfy and leaves the NAS holding
> the old credential — its publishes then 401. The NAS's `?auth=` value is entered by hand in
> Notification Center and exists nowhere in git, so nothing will reconcile it for you. Rotate the
> Secret **and** re-paste the URL below, or don't rotate. This is deliberate: before the hook
> reconciled, a rotation would have left the two silently diverged instead, which is worse — but it
> is a coupling that did not exist when the hook only created users.

Regenerate `<AUTH>` after any password rotation:

```bash
python - <<'EOF'
import base64
pw = "<the ntfy-qnap-auth password>"
print(base64.urlsafe_b64encode(b"Basic " + base64.b64encode(f"qnap:{pw}".encode())).decode().rstrip("="))
EOF
```

Then a notification rule (**Notification Center → Notification Rules → Alert Notifications**) scoped
to Storage & Snapshots / Hardware at severity Warning+, delivering to that SMS service.

Available placeholders: `@@Text@@`, `@@SEVERITY@@`, `@@SERVER_NAME@@`, `@@DATE@@`, `@@TIME@@`,
`@@APP@@`, `@@MODEL@@`. Check with the test-send button that `@@Text@@` arrives URL-encoded — if the
NAS does not encode it, spaces and newlines will truncate the URL.

Subscribe on the phone to `qnap-alerts` on the ntfy.chifor.me server you already use, as `ailab`.

### 7b. myQNAPcloud push (the one that survives the cluster being down)

Free with the QNAP ID this NAS is already registered under (`ai-storage`). This is the channel that
still works when Kubernetes is the thing that has failed — ntfy cannot cover that case, because it
runs in the cluster on a PVC backed by this very NAS. Enable **Notification Center → Service Account
and Device Pairing → Push Service**, pair the Qmanager mobile app, and route the same
Storage/Hardware rules to it. Not open source and event metadata transits QNAP's cloud, which is the
price of the independence.

## 8. versitygw supervisor — the USB failure domain (W3, 2026-09-08)

versitygw is the S3 gateway serving `talos-etcd-backups` and Velero, and it is a **single-node POSIX
gateway on a USB disk** (`/dev/sdb2` → `/share/external/DEV3302_2`). It is not managed by Flux or
OpenTofu; that is a recorded residual risk in `plans/2026-09-08-usb-failure-domain-plan.md`. It is
started from **root's crontab** — there is no init script and no QPKG.

### What went wrong on 2026-09-08

A USB stall wedged the gateway and took the git forge down for ~4.5 h. The watchdog that was supposed
to catch it was itself part of the failure. It lived at `<USB>/versitygw/watchdog.sh` — **on the disk
it was watching** — and cron ran it every 3 minutes with no mutual exclusion:

- it tested `ps | grep versitygw`, so a gateway that accepted TCP but answered nothing looked healthy;
- `[ -f "$f" ]` (a cached stat) still succeeded while `/bin/sh "$f"` (a read) blocked, so a fresh copy
  wedged every 3 minutes — **25 accumulated in D-state**;
- it appended to a **478 MB unrotated log on the same USB disk**, so the gateway blocked on its own
  logging (`ls -l /proc/<pid>/fd/1` confirmed the fd).

### What is deployed now

| | |
|---|---|
| Source of truth | `scripts/qnap-versitygw-watchdog.sh` (in git — **do not hand-edit the NAS copy**) |
| Installer | `scripts/qnap-versitygw-install.sh` (idempotent; `DRY_RUN=1` to preview) |
| Tests | `just test-versitygw-watchdog` (91 cases, run under WSL) |
| Deployed to | `/share/ZFS2_DATA/.versitygw-supervisor/` — the **internal** pool, never the USB |
| Cron | `*/3 * * * * /bin/bash /share/ZFS2_DATA/.versitygw-supervisor/watchdog.sh` |
| Retired | `<USB>/versitygw/watchdog.sh.retired` (kept for reference; nothing invokes it) |

The installer refuses to run if the supervisor base resolves to the same block device as the gateway,
so a symlink or remount can never quietly re-arm the original bug. It also refuses if the USB is not
mounted at `VGW_MOUNT` (default `/share/external/DEV3302_2`) — checked in `/proc/mounts`, because a
bare "does the directory exist" check is fooled by exactly the 2026-09-29 litter described below.
Before it contacts the NAS at all, it refuses the same `VGW_MOUNT`/`VGW_DIR` spellings the supervisor
refuses (`/`, a trailing slash, a `VGW_DIR` outside `VGW_MOUNT`), so a typo reads as a config error
rather than as "the USB disk is missing".

### How it decides

It probes the **disk before** deciding anything, because that determines whether a restart is even
capable of helping. "The disk" is identified by its **mountpoint**: `VGW_DIR` must resolve, in
`/proc/mounts` (never `statfs`, which blocks on a wedged USB), to exactly `VGW_MOUNT`. The device
name is deliberately *not* pinned — a re-enumerated USB disk can come back as `sdc` instead of `sdb`.

| Disk | Gateway answers | Action |
|---|---|---|
| not mounted | either | `disk-missing` — **never probe, never restart, create nothing under `VGW_DIR`**, raise a QuLog event |
| mounted, but `VGW_DIR` absent | either | `gwdir-missing` — never restart (there is no `start.sh`), create nothing, raise a QuLog event |
| ok | yes | nothing (`healthy`); restart ledger cleared |
| ok | no | stop the stale process, restart, confirm it answers — up to **3 times per 30 min** |
| ok | no, budget spent | `restart-budget-exhausted` — stop trying, raise a QuLog event |
| bad | either | `disk-unhealthy` / `disk-wedged` — **never restart**, raise a QuLog event |

A restart that never answers is also reaped, and if its startup is itself stuck on the USB the
status is `start-wedged`. The mount is re-checked right before a stop/restart, so a disk that
disappears after the probe passed still ends in `disk-missing`, not an `exec` off the bare mountpoint.

**Why `disk-missing` exists (2026-09-29).** The USB disk dropped off the bus (`usb4-port1: Cannot
enable. Maybe the USB cable is bad?`) and QTS unmounted it, leaving `/share/external/DEV3302_2` an
empty directory on the `/share` **tmpfs**. The supervisor of the time had no notion of "mounted": the
same-device guard resolved `VGW_DIR` to that tmpfs (not the base's device, so it passed), and the
disk probe's `mkdir -p` *created* `VGW_DIR/.health` in RAM and read its own write back — `disk=ok`
for ~6.5 h while it "restarted" a `start.sh` that did not exist (`execvp: No such file or directory`
in `versitygw.log`) until `restart-budget-exhausted`. Had a `start.sh` existed there, it would have
served an empty S3 store out of RAM. The probe now creates only `.health` itself (no `-p`), and
nothing under `VGW_DIR` is touched unless the mount is present.

**A restart cannot repair uninterruptible disk I/O**, so a bad disk suppresses restarts entirely;
retrying would only add more processes that hang. The supervisor **never reboots the NAS** — it is
shared infrastructure (etcd backups, Velero, the `pve-nfs` export).

### The pile-up guard

A `mkdir` lock, because the NAS has no `flock`. It names an **owner**, and which owner changes over
the run:

- `kind=supervisor` — the script itself, by pid + `/proc` start-time (a recycled pid cannot make a
  dead lock look alive). Held for the **whole** run including the stop/restart sequence; naming only
  the probe would make the lock look stale the moment the probe exited, while the supervisor was
  still mid-restart — a window for two gateways on `:7070`.
- `kind=abandoned-probe` / `abandoned-start` — owned by a **process group**. A process in D-state
  ignores `SIGKILL` (kubelet ignored it for 6 minutes in this incident), so the parent **polls and
  abandons** rather than `wait`ing, and deliberately **leaves the lock held**. It must be the group,
  not the pid: `mkdir`/`cat`/`rm` are children of the probe subshell, so `SIGKILL` can reap the
  subshell while the command actually stuck on the disk lives on.

That turns the old 25-copy pile-up into a hard ceiling of one, and it self-heals: when the disk
recovers the abandoned group empties and the next run reclaims the lock.

Two subtleties worth knowing before changing any of it:

- **Reclaiming a stale lock is serialised by a second mutex** (`state/reclaim`), and re-checks
  ownership while holding it. Without that, two runs that both saw a dead holder could both proceed
   — the second deleting the *winner's freshly created* lock. The guard is held for milliseconds; an
  older one means a run died inside it and is broken with a logged message.
- **The `/proc` scan skips unreadable entries** rather than aborting. Globbing `/proc/[0-9]*/stat`
  into one `awk` looks equivalent but is not: the shell expands the glob before `awk` opens
  anything, so a process exiting in between makes `awk` exit non-zero, which reads as "the group is
  gone" and releases an abandoned probe's lock while its D-state child is still alive.
  `PROC_ROOT` exists solely so this is unit-testable; never set it in operation. Likewise
  `VGW_MOUNTS_FILE`, which lets the tests feed a synthetic mount table while the process scanners
  keep reading the real `/proc`. The two are independent: `PROC_ROOT` alone does not move the mount
  table, which stays `/proc/mounts`.

### Triage

```bash
python scripts/qnap-ssh.py --sudo "cat /share/ZFS2_DATA/.versitygw-supervisor/state/status"
python scripts/qnap-ssh.py --sudo "tail -40 /share/ZFS2_DATA/.versitygw-supervisor/watchdog.log"
```

A `disk-wedged` status means **storage intervention**, not a restart: the USB bridge has stopped
answering and only re-seating the device (or a NAS reboot, which is an operator decision) clears it.

A `disk-missing` status means the disk is **not mounted at all** — it needs **physical attention**:

```bash
python scripts/qnap-ssh.py --sudo "grep DEV3302_2 /proc/mounts; cat /proc/partitions | grep sd"
python scripts/qnap-ssh.py --sudo "dmesg | grep -iE 'usb|sd[b-z]' | tail -20"
```

- `usb usbN-portM: Cannot enable. Maybe the USB cable is bad?` repeating, and only `sda` in
  `/proc/partitions` → the device has fallen off the bus. **Re-seat the USB cable / power-cycle the
  enclosure.** Nothing on the supervisor's side can help, and a hand remount is not the fix. (A NAS
  reboot is an operator decision, as for `disk-wedged`.)
- On re-attach, QTS re-enumerates (`sdb: sdb1 sdb2`, `EXT4-fs (sdb2): recovery complete`) and mounts
  it back at `/share/external/DEV3302_2` by itself. The next cron run (≤ 3 min) sees the mount, probes
  it, and restarts the gateway; the restart ledger was not charged while the disk was missing.
- If it comes back under a **different** `/share/external/DEV33xx_y` name, the status stays
  `disk-missing` on purpose — the supervisor's `VGW_DIR`/`VGW_MOUNT` defaults (which the cron line
  runs with) name `DEV3302_2`. Find out why it moved before re-pointing anything; a disk that
  silently changes path is itself a finding.
- **Litter from before this fix:** the old probe left `versitygw/.health/` on the `/share` tmpfs under
  the empty mountpoint. The re-mounted disk hides it; it lives in RAM, is a few bytes, and disappears
  at the next NAS reboot. Nothing to clean up.

A `gwdir-missing` status means the disk **is** mounted and answering — the probe listed its root —
but `VGW_DIR` is not on it. This is **not** a USB wedge; do not go hunting for D-state in `dmesg`.
Look at what is actually on the mount:

```bash
python scripts/qnap-ssh.py --sudo "grep DEV3302_2 /proc/mounts; ls -la /share/external/DEV3302_2/"
```

- An empty or unfamiliar filesystem → the disk was wiped or replaced, or a **different** disk now
  sits at that mountpoint. The S3 data went with it: that is a restore/re-provisioning decision for
  an operator, not something the supervisor attempts (it creates nothing there).
- `versitygw/` there when you look → the next run (≤ 3 min) probes it normally; `watchdog.log` shows
  when it reappeared.
- If the mount root could **not** be listed either, the status is `disk-unhealthy` with
  `mountroot-unreadable` instead — `[ -d ]` is false on an I/O error too, so absence alone is never
  taken as proof that the disk is fine.

This supervisor is **not** what pages you. Detection and alerting are cluster-side: the
`versitygw-probe` CronJob (§ `kubernetes/apps/backup/talos-backup/versitygw-probe.yaml`)
does an authenticated PUT → GET → verify → DELETE every 10 minutes and raises `VersitygwProbeFailed`.
That alert compares the newest failed probe against the **CronJob's** last success, not against the
probe Jobs' completion times: on 2026-09-29 the Job-based form went silent an hour into the outage,
when `ttlSecondsAfterFinished: 3600` deleted the last successful Job, leaving only the warning-level
`VersitygwProbeStale`.
The split is deliberate — cluster-side answers *"is the object store usable?"*, the NAS-side answers
*"can a local restart fix it?"*. The NAS-side check stops short of a signed S3 round-trip on purpose:
the NAS ships **bash 3.2.57 with no `flock`, `timeout`, or `pgrep`**, and hand-rolling SigV4 there
would put fragile code in the one path that must never fail.

> **Gotcha — the NAS's `base64` rejects a trailing newline.** `echo "$b64" | base64 -d` decodes
> correctly but **exits 1**, which silently aborts any `set -e` script. Use `printf '%s'`.

## 9. Attach/detach timeout — the per-LUN sweep, the Kyverno policy, stale transactions (2026-09-27)

**What the driver does (qnap-csi v1.6.0, ailab#865).** Every ControllerPublish/Unpublish/Delete is
served by the `storage-api-server` sidecar as a full `Qvolume List` + one `Get Volume` per LUN,
~2.5–3 s each; each Get is one `disk_manage.cgi` call on the NAS that walks ALL volume labels
(`/var/log/storage_lib.log` shows ~490 `Volume_Get_Label` lines per 3 min, 41 distinct volumes per
CGI pid) — O(n²) per sweep. At 45 LUNs a sweep is ≈110–135 s, so the `csi-attacher` sidecar's stock
`--timeout=60s` fails EVERY attach and detach (`FailedAttachVolume … DeadlineExceeded`, then
Trident logs `client rate limiter Wait returned an error: context canceled` when it finally tries
to save the publication record). Retries re-queue sweeps until the controller is saturated. A
publish was observed to take 5 min; two detaches of a dead pod's volumes sat 3 h.

**The fix is GitOps state, not a hand patch.** `kubernetes/apps/storage-policies/trident-attacher-timeout.yaml`
is a Kyverno mutate policy (Flux Kustomization `storage-policies`, after `platform-kyverno`) that
rewrites the attacher's `--timeout=*` to `--timeout=600s` at admission on `trident/trident-controller`.
It is needed because the Deployment is rendered by `qnap-csi-operator` from a template inside its
image (`TridentOrchestrator` exposes only `debug/namespace/tridentImage`) and the operator reverts a
direct patch within 30 s. Offline fixtures: `python kubernetes/apps/storage-policies/tests/run.py`
(needs the kyverno CLI). 600 s is a mitigation, not a bound: it is ONE RPC budget (the provisioner
already runs 600 s, resizer/snapshotter 300 s), and queueing or LUN growth can still consume it.

```bash
# Post-check (run after any Trident/operator upgrade, and if attaches start failing again):
kubectl --context admin@ai -n trident get deploy trident-controller \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="csi-attacher")].args}'   # must contain --timeout=600s
kubectl --context admin@ai get clusterpolicy trident-attacher-timeout               # READY True
# Integration proof (the live object is already 600s, so a value check alone cannot tell a working
# policy from a dead one): send the operator's 60s through the webhook as a server-side dry-run.
kubectl --context admin@ai -n trident get deploy trident-controller -o json \
  | python -c 'import json,sys; d=json.load(sys.stdin); d.pop("status",None)
[c.__setitem__("args",["--timeout=60s" if a.startswith("--timeout=") else a for a in c["args"]]) for c in d["spec"]["template"]["spec"]["containers"] if c["name"]=="csi-attacher"]
print(json.dumps(d))' > /tmp/dep60.json
kubectl --context admin@ai -n trident replace --dry-run=server -f /tmp/dep60.json -o jsonpath='{.spec.template.spec.containers[?(@.name=="csi-attacher")].args}'
# expect --timeout=600s in the answer, and the live generation/resourceVersion unchanged.
```

**Why `failurePolicy: Ignore`.** With `Fail`, a Kyverno outage blocks every update of
`trident-controller`, including the operator's own repair path. With `Ignore` the worst case is one
un-mutated write during an outage — the post-check above catches it and the next operator write
repairs it. The policy is admission-only (no `mutateExisting`), so installing it does NOT touch the
live Deployment: the operator's next UPDATE (it re-applies on every start) is what gets mutated.

**Operator interaction / rollback.** The operator's write carries 60s; admission turns it into 600s,
which equals the stored object, so nothing is persisted and no ReplicaSet rolls. Rollback trigger:
`resourceVersion` churn on `trident-controller`, a fresh ReplicaSet, or repeating update errors in the
operator log → `kubectl --context admin@ai -n trident scale deploy trident-operator --replicas=0`,
confirm the args are 600s, fix the policy, then scale back. Never delete the policy while the
operator runs: the next reconcile restores 60s and every attach breaks again. Remove the policy only
when a qnap-csi release exposes the sidecar timeout or fixes the per-LUN sweep.

**Stale `tridenttransactions` wedge Trident's bootstrap.** After a controller restart,
`csi-attacher` logs `CSI driver probe failed: Trident initialization failed; error attempting to
clean up volume pvc-… from backend qts: Resource was not found` and NOTHING provisions or attaches
until that one transaction is gone. The error names ONE transaction; it is stale only if ALL of
these hold — `kubectl --context admin@ai -n trident get tridenttransaction <name> -o yaml` is an
`addVolume` for a volume that has no PV, no PVC, no `tridentvolume`, and `qcli_iscsi -l` on the NAS
shows no LUN of that name. Save the CR (`-o yaml > kubernetes/infra/_out/…`) before deleting it. A
transaction whose volume still has a PV/PVC/`tridentvolume` or a LUN is NOT stale and must be left to
Trident. Each failed provisioning retry also leaves an `addVolume` transaction behind
("unable to process the preexisting transaction"); the same test applies.

**Orphan testpool PVs — cleaned up 2026-10-10 (ADR 0037).** The `Released` testpool PVs (reclaim
policy flipped to `Retain` on 2026-09-27, because each failed delete re-queued a full sweep) and
their NAS side went with the test-env pool. What worked, for the next orphan of this kind:
- **NAS first, one LUN at a time.** Resolve the `lunID` from the exact `trident-pvc-<uuid>` name
  right before each call (QNAP reuses indexes), and confirm it is unmapped. Remove it with
  `qcli_iscsi -r lunID=<n>` (in a `qcli -l … saveauthsid=yes` session). Then diff the full LUN and
  target lists against a backup taken before the run: only that entry may be gone, and its zvol and
  SCST device with it. Four testpool LUNs (55 → 51) went this way with no side effects; healthy LUNs
  (pool 1, a real zvol) remove cleanly, unlike the 09-27 dangling LUN 9.
- **Then the PV objects.** With `Retain`, `kubectl delete pv` makes no CSI call. A leftover
  `external-attacher/csi-trident-qnap-io` finalizer is dropped by the attacher itself when no
  VolumeAttachment names the PV ("no VA found, removing finalizer"). Delete one and watch
  `csi-attacher`, `trident-main` and `storage-api-server` before doing the rest.
- **Trident's own records stay** (TridentVolumes/TridentSnapshot listed in ADR 0037). Deleting them
  through Trident re-enters the not-found loop.
- **Health signal = a provisioning probe, not the LUN-list CGI.**
  `iscsi_lun_setting.cgi?func=extra_get&lunList=1` returned `result -1` / `errorcode -22` on
  2026-10-10 while provisioning was healthy, so it is not the 09-27 symptom by itself. Prove the
  NAS with a 1 Gi `qnap-iscsi` PVC + pod, and patch its PV to `Delete` before deleting the PVC
  (`qnap-iscsi` is `Retain`, so a plain delete leaks a LUN).
- **QuTS refuses `zfs destroy` of snapshots from the shell** ("permission denied", even as root, no
  holds). `zpool1/orphan_placeholder_from_lun9_20260927` (84.8K, no LUN, no SCST device, from the
  09-27 repair) is left for that reason. It is harmless; remove it from the QuTS UI if it ever
  matters.

**Recycling airlock sandboxes** (so a new pod picks up new resource defaults): never delete the
pod; `scripts/airlock-recycle-sandbox.sh` runs airlock's own teardown → deploy with a tenant
member's session and verifies the new pod.

## 10. Thunderbolt TSO/GSO must stay OFF on the NAS (2026-09-29)

**Symptom it prevents:** iSCSI from the Thunderbolt-tier CPs (cp1/cp2) at 150-420 ms and ~6 MB/s while the
same LUN from cp3 (ethernet) answers in single-digit ms. Host-to-NAS ping is 0.2 ms and host-local transfers
are fast, so it looks like "the NAS" or "the LUN" — it is neither.

**Mechanism:** `tbtbr0` (members `tbtnet0p0`/`tbtnet1p0`) runs MTU 65522 with TSO/GSO on, so the NAS sends
up-to-64 KB frames over Thunderbolt. ai-node1/2 receive each as one oversized packet (`dmesg`: "thunderbolt0:
Driver has suspect GRO implementation"). Fine for the host itself, but the hosts **route** the CP VMs' iSCSI
(ADR 0011, VM MTU 1500): a DF packet bigger than the VM bridge MTU is dropped + ICMP frag-needed →
`nstat IpFragFails` climbs (60.6M on node1) → TCP retransmits. Disabling GRO on the host side does nothing:
the frames are built at the NAS.

**Fix (as code):** `scripts/qnap-tbnet-offload.sh`, installed by `bash scripts/qnap-tbnet-offload-install.sh`
into `/share/ZFS2_DATA/.tbnet-offload/` with one root cron line (every minute — a link flap/re-plug re-creates
`tbtnetNpM` with offloads on). Measured after: 700 KB GET from a cp1 pod 0.495 s → 0.009 s, IpFragFails +0,
iSCSI 2-4 ms on all three CPs.

```bash
# verify (NAS side): every Thunderbolt port must say off (ports discovered, not hard-coded)
python scripts/qnap-ssh.py --sudo 'for i in tbtbr0 $(ls /sys/class/net | grep -E "^tbtnet[0-9]+p[0-9]+$"); do echo -n "$i "; ethtool -k $i | grep -E "^(tcp-seg|generic-seg)" | tr "\n" " "; echo; done'
# the enforcer logs at ERROR every minute if it cannot disable them:
python scripts/qnap-ssh.py --sudo 'grep tbnet-offload /var/log/messages | tail -5'
# verify (host side): must NOT grow during iSCSI load
python scripts/node-ssh.py 192.168.0.2 "nstat -az IpFragFails"
```

## Open items this audit did NOT close

| Gap | Why it is still open |
|---|---|
| **No UPS** | Needs hardware. All 5 SSDs already report `unsafe_shutdowns: 2`. ZFS tolerates power loss; the exposure is the ext4 filesystems inside the iSCSI LUNs (Postgres, Gitea, Prometheus). |
| **QNAP-native notification** | Cluster side is built and verified (§7); the last mile is two UI steps in Notification Center, because QuTS hero exposes no CLI and no usable API for it. |
| **`/mnt/ext` at 93%** | 29 MB free on the 417 MB QTS app partition (`/dev/md13`). All QNAP system files — nothing safe to prune by hand. Firmware updates and app installs write here, so it is a known wedge point. Not exposed over SNMP; check by hand. |
| **Thin-LUN reclaim** | See §6. Needs a privileged `fstrim` DaemonSet; deliberately not built without an operator decision on the PSA/hostPath trade-off. |
