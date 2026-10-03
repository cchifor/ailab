# cloud-power: AUTO OFF and AUTO ON, switchable from home.chifor.me

## Codex Review

- Reusing the existing drain state machine, resourceVersion-checked persistence, certificate pinning, and credential-free WoL pod provides a sound foundation.
- **Blocking scope mismatch:** this draft describes daily 22:00/08:00 controls, while the supplied requirements specify `fireEvent`-based ON with backoff, OFF nine hours before the next event, and a 16:00 UTC fallback.
- The main controller risks are ambiguous writes, automatic OFF immediately undoing manual ON/CANCEL, and undefined DST and overlapping OFF/ON behavior.
- The pool-comment channel needs explicit quorum, convergence, and failure semantics; a successful pool write does not prove that hosts have programmed their RTC alarms.
- Missing release gates include effective-permission checks, new-route security tests, actual shutdown-order verification, and a rollback procedure that preserves automation settings and runner ownership.

## Context

The cloud GPU cluster (`pve`: cloud1/2/3, 192.168.0.20-22, cloudlab repo) wakes every morning by
itself: each host's `cloud-rtc-wake.service` runs `/usr/local/sbin/cloud-arm-rtc-wake` from
`ExecStop`, which arms the CMOS RTC alarm for the next 08:00 (`/etc/default/cloud-power` may move it,
or opt out with `CLOUD_RTC_WAKE=off`; that file does not exist on any host today). Nothing turns the
cluster OFF automatically: verified 2026-10-03 - no poweroff timer or cron on any host, no ailab
CronJob touches cloud-power, and every `OFF SCHEDULED` line in the cloud-power log is
`by chifor@gmail.com` (the dashboard button).

<!-- codex: [P1] This baseline conflicts with the supplied trusted fact that OFF already fires daily at 16:00 UTC; the reviewed ac152cb4 controller also contains no daily trigger. Identify the deployed revision and existing scheduler owner before defining migration, so this change neither removes the existing behavior nor introduces a competing scheduler. -->

The operator wants, in the home.chifor.me "cloud GPU" widget (cloud-power, iframe):

- **Auto OFF**: switch + editable time (default 22:00, every day). Must use the same path as the
  OFF button: pause the `cloud-ci-*` runners in Gitea, wait for in-flight jobs, shut the hosts down
  through the PVE API, re-enable the runners once the hosts are down.
- **Auto ON**: switch + editable time (default 08:00, every day), controlling the existing RTC
  alarm.

<!-- codex: [P1] These daily controls do not describe the supplied fireEvent-based requirements, and the proposed schema has no event identity, timestamp, or wake-retry state. Reconcile the intended contract before implementation, including the slot source and whether RTC is the primary mechanism or a fallback to scheduled WoL. -->

Verified facts this design relies on:

- `pve` is ONE Proxmox cluster (pvecm: 3 nodes, quorate), so `/etc/pve` (pmxcfs) is shared.
- The cloud-power PVE identity `cloudpower@pve!ailab` (privsep 0) holds role `CloudPower`
  (`Sys.Audit,Sys.PowerMgmt,VM.Audit`) on `/nodes` and `/vms`. That role, user and ACL were created
  by hand and are NOT codified anywhere (grep of cloudlab + ailab).
- PVE 9.2.10: `PUT /pools` (update_pool, `poolid` + `comment`) checks
  `['perm', '/pool/{poolid}', ['Pool.Allocate']]`; `GET /pools/{poolid}` checks `Pool.Audit`.
  Adding members additionally needs rights on the member objects, which the token does not have.

<!-- codex: [P1] Pool.Allocate is required by the selected endpoint, but grants pool administration beyond comment updates; privsep 0 also means the token inherits the user's effective permissions. Document that additional authority and verify the actual token cannot modify other pools, add members without the required rights, or administer users/ACLs; keep its secret confined to the API pod. -->

- Pools are stored in `/etc/pve/user.cfg` as `pool:<id>:<comment>:<vms>:<storage>:`; the comment is
  `PVE::ParseUtils::encode_text` = URI-escape of control/hi-bit chars, `:` and `%` only.
- Hosts and cluster timezone: `Europe/Bucharest`. The cloud-power pod runs UTC; `python:3.14-slim`
  ships `/usr/share/zoneinfo`.
- cloudlab `main` (2026-09-19 fixes) already has `Conflicts=shutdown.target` and
  `Before=pve-guests.service` on `cloud-rtc-wake.service`, so ExecStop really runs at poweroff and
  runs after the guests stopped. Journal on cloud3 confirms `RTC wake armed for ... 08:00` on the
  2026-10-02 shutdown.

<!-- codex: [P2] The available cloudlab reference checkout at 72e4c335 lacks both of these unit directives, so it is not the stated deployment baseline. Pin the cloudlab implementation and rollout to a revision containing the trusted live fixes rather than provisioning from this divergent checkout. -->

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

<!-- codex: [P2] Independence from the cloud hosts does not make the Kubernetes API always readable or writable. Define how the page distinguishes an acknowledged save from cached settings, unavailable state, and an uncertain write, without presenting defaults as authoritative during an outage. -->

- **Auto ON is mirrored into PVE** as the comment of an empty pool `cloud-power`:
  `auto-on=1 wake=08:00` (or `auto-on=0 wake=08:00`). This is the channel to the hosts: each host
  reads it locally from `/etc/pve/user.cfg` at shutdown - no network, no dependency on ailab at
  shutdown time.

<!-- codex: [P1] A powered-off host cannot consume a changed pool comment: enabling auto-on after shutdown with its RTC disabled cannot cause the next requested wake. Specify the manual-wake requirement or the missing fireEvent-driven WoL path, including bounded retries and recovery after a controller restart. -->

### ailab: cloud-power (`kubernetes/apps/apps/cloud-power/app.py`)

1. **Persistence**: `Scheduler` gains `self.auto` (loaded with schedule/last in `load()`, written by
   `_save()` together with `schedule`/`last` - one resourceVersion-checked object, so the existing
   409-means-re-read discipline covers it). A malformed `auto` value does NOT mark the whole state
   corrupt (that would disable OFF); it falls back to defaults and shows a note.

<!-- codex: [P1] Falling back to defaults actively enables auto-on and can overwrite a previously disabled pool setting after corruption or an unsupported schema version. Separate absent-key migration from invalid data: preserve manual drain/release recovery while withholding automation and mirror writes whose desired state is untrusted. -->

2. **Settings API**: `POST /api/auto` `{"kind":"off"|"on","enabled":bool,"at":"HH:MM"}` (MODE=api
   only, behind the existing `_peer_ok` + `_same_origin` CSRF gate and oauth2-proxy). Strict
   validation (`^([01]\d|2[0-3]):[0-5]\d$`, real bools, known kind). Logged with identity:
   `AUTO-OFF set by <email>: enabled 22:00`. Under the scheduler lock; StateConflict -> reload +
   retry once, otherwise 503. Changing the auto-off time or re-enabling it does NOT clear `fired`
   (a same-day re-enable after tonight's run must not fire twice) - except that `fired` is keyed by
   the target date, so moving the time to later the same day is a new slot only if that slot's date
   has not fired yet (see 3).

<!-- codex: [P1] The existing _require_usable() does not reject dirty state or reconcile all pending rejected/cancel_wanted intents. Define a settings-write protocol that resolves those conditions first, and on 409 reapplies only the requested setting to freshly reconciled state without overwriting runner ownership or resurrecting a cancelled OFF. -->

<!-- codex: [P2] oauth2-proxy currently permits every Authelia-authenticated dashboard user, while forwarded identity is attribution only and _same_origin() accepts requests lacking browser headers. State that authorization policy explicitly and test the new route through the proxy, from unauthorized pods, and against MODE=wol; retain the existing trusted-node exception in the threat model. -->

3. **Auto-OFF trigger** (in `tick()`, after the phase machine, only when: loaded, not corrupt, not
   dirty, idle (`self.state is None`), auto-off enabled):
   - slot = most recent local `at` instant <= now (today's, or yesterday's if today's is still in
     the future - handles times just before midnight); due when `now - slot < AUTO_OFF_GRACE_SEC`
     (default 3600) and `fired != slot.date()`.

<!-- codex: [P1] This arithmetic does not implement OFF nine hours before the next fireEvent or the no-slots 16:00 UTC fallback. Specify next-event selection, whether unavailable/stale slot data differs from an empty schedule, and subtraction of nine elapsed hours across DST and midnight. -->

<!-- codex: [P2] Enabling auto-off at 22:30 with at=22:00 would immediately start a drain, as would moving the time into the preceding grace window. Define whether edits may authorize an already-due slot and show the effective next action before the setting is accepted. -->

   - If every host is already down -> record `fired=slot date, fired_result="skipped: hosts
     already off"` and do nothing.

<!-- codex: [P2] The existing host probe only checks TCP :8006, so a transient network failure or stopped pveproxy can consume the day's slot while machines remain powered on. Use a documented confirmation policy or report reachability as unknown and retry within the grace window. -->

   - Otherwise record `fired` + `fired_result="scheduled"` on `self.auto` and call the SAME
     `self.schedule("auto-off (22:00)")` used by the button; `schedule()`'s first `_save()` persists
     `fired` atomically with the `pausing` record, so a restart can never fire the slot twice.
     `Refused` (e.g. no cloud runner registered) -> persist `fired_result="refused: ..."` (no
     retry spam); `GiteaError`/`StateError` before anything was saved -> roll `fired` back in
     memory and retry next tick while the grace window lasts.

<!-- codex: [P1] StateError does not prove that nothing was saved: the current transport can commit a write and lose its reply, and schedule() exposes that ambiguity through StateUncertain. Re-read and reconcile the durable attempt and fired marker before deciding to retry; distinguish pre-save failure from failed pausing/release and do not roll back a marker using stale memory. -->

<!-- codex: [P2] Persisting fired_result="scheduled" with pausing also records success for attempts that restart into rollback or fail while pausing runners. Associate the result with the attempt and publish its eventual cancelled, failed, stalled, or completed outcome. -->

   - Missed window (pod down past slot + grace) -> no late power-off; the page shows the last
     result.

<!-- codex: [P1] The grace window limits drain creation, not shutdown time: an admitted drain may continue for 3h15m and power off after the next ON time, causing the shutdown hook to arm tomorrow's alarm. Define an OFF cutoff relative to the next wake and safely release runners when that cutoff is reached. -->

   - The manual-OFF preflight (guest list + confirm token) is a human-UI safeguard and is not
     replayed for auto-off; PVE's own `pve-guests` stops guests at shutdown exactly as for the
     button.

<!-- codex: [P2] The drain protects Gitea jobs only; GPU inference and other guest workloads can still be interrupted without the manual preflight warning. Make enabling recurring OFF explicitly authorize that behavior, or define the additional workload checks required. -->

   - ON (button) during an auto-off drain cancels it exactly as today (`cancel_for_wake`).

<!-- codex: [P1] This misses ON while idle in the grace window and CANCEL of a manual OFF that overlapped the slot: fired is still unset, so the next tick can immediately schedule automatic OFF. Define a durable per-slot override so manual ON/CANCEL remains effective after release and restart. -->

<!-- codex: [P2] Specify whether disabling auto-off or changing its time withdraws an already-created automatic drain. The proposed trigger guard only prevents new schedules, so switching it off currently leaves an existing drain authorized to shut down. -->

   - Time: `AUTO_TZ` env (default `Europe/Bucharest`) via `zoneinfo`; the Scheduler takes an
     injectable `tz` so tests use a fixed offset (Windows has no system tz database).

<!-- codex: [P1] Define handling of nonexistent and repeated local times, and compare elapsed grace using UTC timestamps rather than same-ZoneInfo wall-time subtraction. Fixed-offset tests cannot validate DST; exercise actual Europe/Bucharest transitions with tzdata or in Linux CI and align the host date calculation with the chosen policy. -->

4. **Auto-ON mirror** (in `tick()`, any phase, rate-limited):
   - `pve_pool_get()` / `pve_pool_set(comment)` call any UP node (`GET /pools/cloud-power`,
     `PUT /pools` with `poolid=cloud-power&comment=...`), with the existing cert pinning.

<!-- codex: [P1] One reachable node in a three-node cluster does not imply quorum or writable pmxcfs, especially during partial startup and shutdown. Treat quorum/write failures as unapplied state, select a usable API peer, and never equate a successful TCP probe with the ability to persist the setting. -->

   - When any host is up and (desired != last verified pool comment OR last verify older than
     `AUTO_ON_VERIFY_SEC`=300): read; if different, write; record `{applied: bool, verified_at,
     error}` in memory for the page.

<!-- codex: [P2] A persistent mismatch or failure keeps this condition true on every tick, so the verification interval alone does not rate-limit retries. Specify separate attempt/backoff timing, bounded network timeouts, and a total lock-held I/O budget so mirror failures do not starve CANCEL or drain processing. -->

   - `POST /api/auto kind=on` writes the pool synchronously when a host is up, so the answer can
     say "applied to the hosts" vs "saved; applies when the hosts are next up".

<!-- codex: [P1] ConfigMap persistence and the PVE write are separate transactions: save and confirm the desired setting before mirroring, and report desired-versus-applied state separately if either reply is lost. Serialize HTTP and worker mirror operations against the same settings revision so an older request cannot overwrite or verify a newer value. -->

   - `_begin_power_off` pushes the mirror once more (best-effort, logged) right before
     `shutdown_fn`, so a toggle made a moment before the OFF is honoured.

<!-- codex: [P1] Best-effort failure cannot guarantee that the toggle is honoured: stale auto-on=0 can strand hosts, while stale auto-on=1 can wake them against the saved preference. Define whether automatic shutdown stalls pending verified synchronization, how runners are recovered, and the latest phase in which a setting change can affect this shutdown. -->

   - Missing pool / 403 -> page note ("pool cloud-power missing - run cloudlab provisioning");
     the hosts then fall back to their local defaults (= arm 08:00).

<!-- codex: [P1] A 403 does not make the pool missing: hosts can still read its previous valid comment, including auto-on=0. Distinguish missing pool, denied access, transport failure, and stale stored settings; only an absent or invalid local comment invokes the host fallback. -->

5. **Status**: `/api/status` gains `auto: {off:{enabled,at,next_at,fired,fired_result},
   on:{enabled,at,applied,verified_at,note}}` (from the published snapshot, never a live PVE call
   on the request path).

<!-- codex: [P1] A verified pool comment is not verification of installed hook versions, isolated hosts' local copies, or RTC alarms already armed on powered-off hosts. Define applied as pool synchronization only, invalidate it on desired-setting changes, and expose the distinction from an effective hardware alarm. -->

6. **Page**: two rows under the buttons: `[x] Auto OFF at [22:00]` and `[x] Auto ON at [08:00]`
   (checkbox + `<input type=time>`; a change POSTs immediately, the result line confirms).

<!-- codex: [P2] Serialize or coalesce edits per setting and reconcile responses with the authoritative snapshot; rapid checkbox/time changes can otherwise arrive out of order and persist the opposite final intent. Label the schedule timezone explicitly because the existing page formats timestamps in the browser's timezone. -->

   Status text: next auto-off time / last auto-off result; auto-on "applied to hosts" or "saved -
   applies at the next shutdown; an alarm already armed in the hosts still fires". Disabling
   auto-on shows: "hosts stay off until ON is pressed". All rendered with textContent.

<!-- codex: [P1] The disabling message contradicts the already-armed-alarm caveat and also ignores direct WoL calls, which bypass scheduler intent through the existing hostNetwork service. Say that future RTC arming is disabled only after application; do not promise that hosts will remain off. -->

7. **WoL MACs**: fix cloud1/cloud2 to the cloudlab `wol.py` values.
8. Homepage iframe height (`homepage/configmap.yaml`, `h-32`) raised so the two new rows fit;
   the class must exist in Homepage's compiled CSS (verify against the served stylesheet).
9. `deployment.yaml`: env `AUTO_TZ=Europe/Bucharest`, `AUTO_OFF_GRACE_SEC=3600`,
   `AUTO_POOL=cloud-power`; comment block updated.

<!-- codex: [P2] AUTO_POOL is configurable here while the host parser and ACLs hardcode cloud-power; AUTO_TZ can likewise diverge from the hosts' local timezone. Either keep these protocol values fixed or define coordinated configuration and reject unsupported mismatches. -->

### cloudlab: hosts (`scripts/cloud-arm-rtc-wake.sh`, `host/systemd/cloud-rtc-wake.service`, `scripts/provision-host.sh`)

1. `cloud-arm-rtc-wake`: before arming, read `pool:cloud-power:` from `/etc/pve/user.cfg`, URI-decode
   the comment (python3 `urllib.parse.unquote`, present on PVE), parse `auto-on=0|1` and
   `wake=HH:MM` (strict regex). Precedence: pool value > `/etc/default/cloud-power` > built-in
   (on, 08:00). Unreadable/missing/garbled -> fall back and log which source won.

<!-- codex: [P2] Define an exact, versioned comment grammar and extract the encoded comment field before decoding it once; reject duplicate keys, decoded delimiters/control characters, and partial records without evaluating any comment as shell code. Add a real API-write-to-user.cfg-to-parser round trip because handwritten fixtures alone do not validate this custom cross-repository protocol. -->

<!-- codex: [P2] Pool precedence also overrides a deliberate host-local CLOUD_RTC_WAKE=off maintenance setting. Explicitly document that change in authority or retain a host-local inhibit that automation cannot override. -->

   - `auto-on=0` -> CLEAR the alarm (`echo 0 > wakealarm`) and log it. Today the
     `CLOUD_RTC_WAKE=off` path exits WITHOUT clearing, so an alarm armed by an earlier reboot (the
     hook also runs on reboot) or by `cluster-power.sh down` would still fire - fixed for both paths.

<!-- codex: [P1] Clearing must check the write result and read back the alarm before logging success; the current script deliberately tolerates RTC write failures. Define the observable failure state when disabling fails, since continuing shutdown with a stale alarm defeats the requested setting. -->

2. `cloud-rtc-wake.service`: add `After=pve-cluster.service` so at shutdown the hook runs while
   pmxcfs (`/etc/pve`) is still mounted (reverse ordering). Keeps the existing
   `Before=pve-guests.service` / `Conflicts=shutdown.target`.

<!-- codex: [P1] The intended reverse order is pve-guests stop, RTC hook, then pve-cluster stop, but ordering alone does not guarantee a healthy mount or a current replicated comment. Check the complete installed dependency graph for cycles and bound the read/parse work within TimeoutStopSec, including degraded pmxcfs and quorum-loss cases. -->

3. `provision-host.sh`: new idempotent section codifying the PVE access objects (cluster-wide;
   harmless to repeat per host): role `CloudPower` (existing privs), new role `CloudPowerAutoOn`
   (`Pool.Allocate,Pool.Audit`), user `cloudpower@pve` (exists), ACLs `/nodes` + `/vms` ->
   CloudPower (existing), pool `cloud-power` created ONLY if missing (never overwrite the comment
   cloud-power owns), ACL `/pool/cloud-power` -> CloudPowerAutoOn with `--propagate 0`. The token
   secret stays manual (documented in ailab `secret.sops.yaml`).

<!-- codex: [P2] “Harmless to repeat” needs explicit handling of existing objects, concurrent per-host creation, and lack of quorum without silently swallowing ACL failures. Verify effective access after provisioning and preserve the existing token and unrelated grants. -->

4. Docs: runbook section "RTC alarm" + README Day/night: the hour/switch now comes from the
   home.chifor.me widget via the pool comment; `/etc/default/cloud-power` is the fallback.
   `cluster-power.sh down HH:MM` note: the shutdown hook decides the final alarm.

## Rollout order

1. cloudlab PR merged, then provisioning applied to all three hosts (`provision-host.sh` per host;
   the PVE objects are created once, the hook files + unit on each). Verify with
   `systemctl stop cloud-rtc-wake && systemctl start cloud-rtc-wake` (runs the same ExecStop) for
   pool comment absent / `auto-on=1 wake=08:00` / `auto-on=0`: the journal line and
   `/sys/class/rtc/rtc0/wakealarm` must match each case; finish with the alarm re-armed.

<!-- codex: [P2] The referenced provision-host.sh also changes APT configuration, driver/DKMS state, GPU power limits, and other host agents. Provide a targeted hook/ACL deployment path or account for those additional changes in the rollout and rollback scope. -->

<!-- codex: [P1] Manually stopping the hook while every dependency is running does not exercise shutdown ordering or replication as the cluster loses quorum. Add a controlled real shutdown with the changed hook, journal evidence of the stop order, and the resulting wake behavior before enabling recurring automation. -->

2. ailab PR merged -> Flux. Verify `/api/status` `auto` block, toggle via the page, pool comment
   appears (`pveum pool list` / user.cfg), `applied: true`.
3. Hosts-side order dependency: if ailab deploys first, the pool write 403s/404s until cloudlab
   provisioning runs - the page shows the note, nothing breaks (hosts keep 08:00).

<!-- codex: [P1] Partial provisioning can create the pool and ACLs before every host has the new hook, allowing applied=true while some hosts ignore the setting. Gate activation on the installed hook/unit revision on all three hosts, not merely successful PVE access. -->

<!-- codex: [P1] Add an explicit rollback procedure: the old controller's _save() rewrites only schedule/last and drops auto, so rollback can erase preferences and a subsequent upgrade can re-enable default auto-on. Preserve settings and runner ownership, and specify how the pool comment, host hooks, and any already-armed alarms are reconciled across both repositories. -->

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

<!-- codex: [P1] Extend the existing ambiguous-write tests to auto: writes that land but lose replies, unreadable reconciliation, 409 during settings updates, restart between pausing/draining/result persistence, and updates while dirty or cancellation is pending. Add concurrent manual ON/CANCEL/settings-versus-tick cases, including a manual schedule ending inside the automatic grace window, and assert both durable markers and runner ownership. -->

<!-- codex: [P2] Add HTTP-level rejection tests for malformed/non-object JSON, oversized or incomplete bodies, wrong types, disallowed origins, and the new route on MODE=wol. Render the manifests and verify that the WoL pod still receives neither PVE/Gitea credentials nor a service-account token and that the API NetworkPolicy remains effective. -->

- Host script: run against fixture `user.cfg` files (absent pool, empty comment, on, off, garbage,
  `%3A`-encoded) with the RTC path pointed at a temp file (`RTC=` override for tests), on cloud3.

<!-- codex: [P2] Include duplicate records/keys, encoded newlines and percent signs, missing mount/read failures, failed RTC clear/write, reboot with an existing alarm, and DST boundary times. A regular file does not reproduce wakealarm's clear-before-set or rejection semantics, so retain targeted checks against the actual sysfs interface. -->

- Live: steps under Rollout. The first real auto-off is tonight's 22:00 slot once enabled; the
  operator decides whether to enable it immediately.

<!-- codex: [P1] Make the first automatic run a controlled near-future canary with an in-flight CI job, cancellation, persisted slot/result verification, runner release, and the subsequent wake outcome. Test the newly introduced automation and pool channel before relying on the first unattended nightly run; the trusted existing wake path need not be re-derived. -->

<!-- codex-review-status: complete -->