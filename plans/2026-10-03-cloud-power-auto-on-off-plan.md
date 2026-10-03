# cloud-power: AUTO OFF and AUTO ON, switchable from home.chifor.me

## Context

The cloud GPU cluster (`pve`: cloud1/2/3, 192.168.0.20-22, cloudlab repo) wakes every morning by
itself: each host's `cloud-rtc-wake.service` runs `/usr/local/sbin/cloud-arm-rtc-wake` from
`ExecStop`, which arms the CMOS RTC alarm for the next 08:00 (`/etc/default/cloud-power` may move it,
or opt out with `CLOUD_RTC_WAKE=off`; that file does not exist on any host today). Nothing turns the
cluster OFF automatically: verified 2026-10-03 - no poweroff timer or cron on any host, no ailab
CronJob touches cloud-power, and every `OFF SCHEDULED` line in the cloud-power log is
`by chifor@gmail.com` (the dashboard button).

The operator wants, in the home.chifor.me "cloud GPU" widget (cloud-power, iframe):

- **Auto OFF**: switch + editable time (default 22:00, every day). Must use the same path as the
  OFF button: pause the `cloud-ci-*` runners in Gitea, wait for in-flight jobs, shut the hosts down
  through the PVE API, re-enable the runners once the hosts are down.
- **Auto ON**: switch + editable time (default 08:00, every day), controlling the existing RTC
  alarm.

Verified facts this design relies on:

- `pve` is ONE Proxmox cluster (pvecm: 3 nodes, quorate), so `/etc/pve` (pmxcfs) is shared.
- The cloud-power PVE identity `cloudpower@pve!ailab` (privsep 0) holds role `CloudPower`
  (`Sys.Audit,Sys.PowerMgmt,VM.Audit`) on `/nodes` and `/vms`. That role, user and ACL were created
  by hand and are NOT codified anywhere (grep of cloudlab + ailab).
- PVE 9.2.10: `PUT /pools` (update_pool, `poolid` + `comment`) checks
  `['perm', '/pool/{poolid}', ['Pool.Allocate']]`; `GET /pools/{poolid}` checks `Pool.Audit`.
  Adding members additionally needs rights on the member objects, which the token does not have.
- Pools are stored in `/etc/pve/user.cfg` as `pool:<id>:<comment>:<vms>:<storage>:`; the comment is
  `PVE::ParseUtils::encode_text` = URI-escape of control/hi-bit chars, `:` and `%` only.
- Hosts and cluster timezone: `Europe/Bucharest`. The cloud-power pod runs UTC; `python:3.14-slim`
  ships `/usr/share/zoneinfo`.
- cloudlab `main` (2026-09-19 fixes) already has `Conflicts=shutdown.target` and
  `Before=pve-guests.service` on `cloud-rtc-wake.service`, so ExecStop really runs at poweroff and
  runs after the guests stopped. Journal on cloud3 confirms `RTC wake armed for ... 08:00` on the
  2026-10-02 shutdown.
- **Side finding (must fix here):** `NODES` in cloud-power `app.py` carries stale WoL MACs for
  cloud1 (`00:e2:59:01:a6:62` - the dark igc twin; correct `...:63`, fixed in cloudlab 2026-09-06)
  and cloud2 (`00:e2:59:01:a6:52` - the pre-rebuild board; correct `b4:2e:99:a8:a3:a7`, rebuilt
  2026-09-04). cloudlab `scripts/wol.py` is the source of truth. Once AUTO ON can be switched off,
  the ON button is the only way back, so it must target the right NICs.

## Approach

### Where each setting lives

- **Desired settings** (both switches + times) live in the existing runtime ConfigMap
  `cloud-power/cloud-power-state`, new key `auto`:
  `{"v":1,"off":{"enabled":bool,"at":"HH:MM","fired":"YYYY-MM-DD"|null,"fired_result":str},
    "on":{"enabled":bool,"at":"HH:MM"}}`.
  The ConfigMap is always readable, so the page can show and change both while the hosts are off.
  Absent key -> defaults: auto-off **disabled** at 22:00 (nothing powers off until the operator
  flips it), auto-on **enabled** at 08:00 (= today's behaviour). No RBAC change (same object).
- **Auto ON is mirrored into PVE** as the comment of an empty pool `cloud-power`:
  `auto-on=1 wake=08:00` (or `auto-on=0 wake=08:00`). This is the channel to the hosts: each host
  reads it locally from `/etc/pve/user.cfg` at shutdown - no network, no dependency on ailab at
  shutdown time.

### ailab: cloud-power (`kubernetes/apps/apps/cloud-power/app.py`)

1. **Persistence**: `Scheduler` gains `self.auto` (loaded with schedule/last in `load()`, written by
   `_save()` together with `schedule`/`last` - one resourceVersion-checked object, so the existing
   409-means-re-read discipline covers it). A malformed `auto` value does NOT mark the whole state
   corrupt (that would disable OFF); it falls back to defaults and shows a note.
2. **Settings API**: `POST /api/auto` `{"kind":"off"|"on","enabled":bool,"at":"HH:MM"}` (MODE=api
   only, behind the existing `_peer_ok` + `_same_origin` CSRF gate and oauth2-proxy). Strict
   validation (`^([01]\d|2[0-3]):[0-5]\d$`, real bools, known kind). Logged with identity:
   `AUTO-OFF set by <email>: enabled 22:00`. Under the scheduler lock; StateConflict -> reload +
   retry once, otherwise 503. Changing the auto-off time or re-enabling it does NOT clear `fired`
   (a same-day re-enable after tonight's run must not fire twice) - except that `fired` is keyed by
   the target date, so moving the time to later the same day is a new slot only if that slot's date
   has not fired yet (see 3).
3. **Auto-OFF trigger** (in `tick()`, after the phase machine, only when: loaded, not corrupt, not
   dirty, idle (`self.state is None`), auto-off enabled):
   - slot = most recent local `at` instant <= now (today's, or yesterday's if today's is still in
     the future - handles times just before midnight); due when `now - slot < AUTO_OFF_GRACE_SEC`
     (default 3600) and `fired != slot.date()`.
   - If every host is already down -> record `fired=slot date, fired_result="skipped: hosts
     already off"` and do nothing.
   - Otherwise record `fired` + `fired_result="scheduled"` on `self.auto` and call the SAME
     `self.schedule("auto-off (22:00)")` used by the button; `schedule()`'s first `_save()` persists
     `fired` atomically with the `pausing` record, so a restart can never fire the slot twice.
     `Refused` (e.g. no cloud runner registered) -> persist `fired_result="refused: ..."` (no
     retry spam); `GiteaError`/`StateError` before anything was saved -> roll `fired` back in
     memory and retry next tick while the grace window lasts.
   - Missed window (pod down past slot + grace) -> no late power-off; the page shows the last
     result.
   - The manual-OFF preflight (guest list + confirm token) is a human-UI safeguard and is not
     replayed for auto-off; PVE's own `pve-guests` stops guests at shutdown exactly as for the
     button.
   - ON (button) during an auto-off drain cancels it exactly as today (`cancel_for_wake`).
   - Time: `AUTO_TZ` env (default `Europe/Bucharest`) via `zoneinfo`; the Scheduler takes an
     injectable `tz` so tests use a fixed offset (Windows has no system tz database).
4. **Auto-ON mirror** (in `tick()`, any phase, rate-limited):
   - `pve_pool_get()` / `pve_pool_set(comment)` call any UP node (`GET /pools/cloud-power`,
     `PUT /pools` with `poolid=cloud-power&comment=...`), with the existing cert pinning.
   - When any host is up and (desired != last verified pool comment OR last verify older than
     `AUTO_ON_VERIFY_SEC`=300): read; if different, write; record `{applied: bool, verified_at,
     error}` in memory for the page.
   - `POST /api/auto kind=on` writes the pool synchronously when a host is up, so the answer can
     say "applied to the hosts" vs "saved; applies when the hosts are next up".
   - `_begin_power_off` pushes the mirror once more (best-effort, logged) right before
     `shutdown_fn`, so a toggle made a moment before the OFF is honoured.
   - Missing pool / 403 -> page note ("pool cloud-power missing - run cloudlab provisioning");
     the hosts then fall back to their local defaults (= arm 08:00).
5. **Status**: `/api/status` gains `auto: {off:{enabled,at,next_at,fired,fired_result},
   on:{enabled,at,applied,verified_at,note}}` (from the published snapshot, never a live PVE call
   on the request path).
6. **Page**: two rows under the buttons: `[x] Auto OFF at [22:00]` and `[x] Auto ON at [08:00]`
   (checkbox + `<input type=time>`; a change POSTs immediately, the result line confirms).
   Status text: next auto-off time / last auto-off result; auto-on "applied to hosts" or "saved -
   applies at the next shutdown; an alarm already armed in the hosts still fires". Disabling
   auto-on shows: "hosts stay off until ON is pressed". All rendered with textContent.
7. **WoL MACs**: fix cloud1/cloud2 to the cloudlab `wol.py` values.
8. Homepage iframe height (`homepage/configmap.yaml`, `h-32`) raised so the two new rows fit;
   the class must exist in Homepage's compiled CSS (verify against the served stylesheet).
9. `deployment.yaml`: env `AUTO_TZ=Europe/Bucharest`, `AUTO_OFF_GRACE_SEC=3600`,
   `AUTO_POOL=cloud-power`; comment block updated.

### cloudlab: hosts (`scripts/cloud-arm-rtc-wake.sh`, `host/systemd/cloud-rtc-wake.service`, `scripts/provision-host.sh`)

1. `cloud-arm-rtc-wake`: before arming, read `pool:cloud-power:` from `/etc/pve/user.cfg`, URI-decode
   the comment (python3 `urllib.parse.unquote`, present on PVE), parse `auto-on=0|1` and
   `wake=HH:MM` (strict regex). Precedence: pool value > `/etc/default/cloud-power` > built-in
   (on, 08:00). Unreadable/missing/garbled -> fall back and log which source won.
   - `auto-on=0` -> CLEAR the alarm (`echo 0 > wakealarm`) and log it. Today the
     `CLOUD_RTC_WAKE=off` path exits WITHOUT clearing, so an alarm armed by an earlier reboot (the
     hook also runs on reboot) or by `cluster-power.sh down` would still fire - fixed for both paths.
2. `cloud-rtc-wake.service`: add `After=pve-cluster.service` so at shutdown the hook runs while
   pmxcfs (`/etc/pve`) is still mounted (reverse ordering). Keeps the existing
   `Before=pve-guests.service` / `Conflicts=shutdown.target`.
3. `provision-host.sh`: new idempotent section codifying the PVE access objects (cluster-wide;
   harmless to repeat per host): role `CloudPower` (existing privs), new role `CloudPowerAutoOn`
   (`Pool.Allocate,Pool.Audit`), user `cloudpower@pve` (exists), ACLs `/nodes` + `/vms` ->
   CloudPower (existing), pool `cloud-power` created ONLY if missing (never overwrite the comment
   cloud-power owns), ACL `/pool/cloud-power` -> CloudPowerAutoOn with `--propagate 0`. The token
   secret stays manual (documented in ailab `secret.sops.yaml`).
4. Docs: runbook section "RTC alarm" + README Day/night: the hour/switch now comes from the
   home.chifor.me widget via the pool comment; `/etc/default/cloud-power` is the fallback.
   `cluster-power.sh down HH:MM` note: the shutdown hook decides the final alarm.

## Rollout order

1. cloudlab PR merged, then provisioning applied to all three hosts (`provision-host.sh` per host;
   the PVE objects are created once, the hook files + unit on each). Verify with
   `systemctl stop cloud-rtc-wake && systemctl start cloud-rtc-wake` (runs the same ExecStop) for
   pool comment absent / `auto-on=1 wake=08:00` / `auto-on=0`: the journal line and
   `/sys/class/rtc/rtc0/wakealarm` must match each case; finish with the alarm re-armed.
2. ailab PR merged -> Flux. Verify `/api/status` `auto` block, toggle via the page, pool comment
   appears (`pveum pool list` / user.cfg), `applied: true`.
3. Hosts-side order dependency: if ailab deploys first, the pool write 403s/404s until cloudlab
   provisioning runs - the page shows the note, nothing breaks (hosts keep 08:00).

## Critical files

- ailab `kubernetes/apps/apps/cloud-power/app.py` - settings, auto-off trigger, pool mirror, page,
  MAC fix.
- ailab `kubernetes/apps/apps/cloud-power/deployment.yaml` - env.
- ailab `kubernetes/apps/apps/homepage/configmap.yaml` - iframe height.
- ailab `scripts/tests/test_cloud_power.py` - tests.
- cloudlab `scripts/cloud-arm-rtc-wake.sh`, `host/systemd/cloud-rtc-wake.service`,
  `scripts/provision-host.sh`, `docs/runbooks/cloud-gpu-cluster.md`, `README.md`.

## Verification

- Unit (ailab, `python -m unittest discover -s scripts/tests -p "test_*.py"`): slot math incl.
  around midnight and DST; fires once per slot; restart after fire does not refire; grace window
  expiry; already-off skip; Refused persisted, Gitea error retried; ON cancels an auto-off drain;
  settings validation + persistence alongside schedule/last; malformed `auto` -> defaults without
  disabling OFF; pool mirror write/verify/rate-limit, pushed before shutdown; MACs equal wol.py's.
- Host script: run against fixture `user.cfg` files (absent pool, empty comment, on, off, garbage,
  `%3A`-encoded) with the RTC path pointed at a temp file (`RTC=` override for tests), on cloud3.
- Live: steps under Rollout. The first real auto-off is tonight's 22:00 slot once enabled; the
  operator decides whether to enable it immediately.

<!-- codex-review-status: pending -->
