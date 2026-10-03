# cloud-power: AUTO OFF and AUTO ON, switchable from home.chifor.me

## Context

The cloud GPU cluster (`pve`: cloud1/2/3, 192.168.0.20-22, cloudlab repo) wakes every morning by
itself: each host's `cloud-rtc-wake.service` runs `/usr/local/sbin/cloud-arm-rtc-wake` from
`ExecStop`, which arms the CMOS RTC alarm for the next 08:00 (`/etc/default/cloud-power` may move it,
or opt out with `CLOUD_RTC_WAKE=off`). Nothing turns the cluster OFF automatically: verified
2026-10-03 - no poweroff timer or cron on any host, no ailab CronJob touches cloud-power, and every
`OFF SCHEDULED` line in the cloud-power log is `by chifor@gmail.com` (the dashboard button).

The operator wants, in the home.chifor.me "Cloud GPU" widget (cloud-power, iframe):

- **Auto OFF**: switch + editable time (default 22:00, every day), using the same path as the OFF
  button: pause the `cloud-ci-*` runners in Gitea, wait for in-flight jobs, shut the hosts down
  through the PVE API, re-enable the runners once the hosts are down.
- **Auto ON**: switch + editable time (default 08:00, every day). This is a **shutdown-time wake
  policy**: the RTC alarm is one-shot and is programmed by each host as it goes down. A change made
  while the hosts are already off takes effect at their NEXT shutdown; whatever alarm was armed
  at the last shutdown still fires (or does not).

Verified facts this design relies on:

- `pve` is ONE Proxmox cluster (pvecm: 3 nodes, quorate), so `/etc/pve` (pmxcfs) is shared.
- The cloud-power PVE identity `cloudpower@pve!ailab` (privsep 0, so the token = the user's
  effective rights) holds role `CloudPower` (`Sys.Audit,Sys.PowerMgmt,VM.Audit`) on `/nodes` and
  `/vms`. That role, user and ACL were created by hand and are NOT codified anywhere.
- PVE 9.2.10: `PUT /pools` (update_pool, `poolid` + `comment`) and `DELETE /pools/{poolid}` check
  `Pool.Allocate` on `/pool/{poolid}`; `GET /pools/{poolid}` checks `Pool.Audit`. Adding/removing
  members additionally needs rights on the member objects (the token has none). **Accepted scope of
  the new grant:** with `Pool.Allocate,Pool.Audit` on `/pool/cloud-power` only (propagate 0) the
  token can edit, delete or re-create THAT pool and nothing else. Deleting it is equivalent to
  "auto-on falls back to host defaults" (= wake at 08:00), i.e. fails toward waking.
- Pools are stored in `/etc/pve/user.cfg` as `pool:<id>:<comment>:<vms>:<storage>:`; the comment is
  `PVE::ParseUtils::encode_text` = URI-escape of control/hi-bit chars, `:` and `%` only.
- Hosts and cluster timezone: `Europe/Bucharest`. The cloud-power pod runs UTC; the
  `python:3.14-slim` image has `/usr/share/zoneinfo` (listed in the running pod).
- cloudlab `main` (2dd8ab3) has `Conflicts=shutdown.target` and `Before=pve-guests.service` on
  `cloud-rtc-wake.service` (ExecStop really runs at poweroff, after the guests stopped), and
  `scripts/cloud-drain-poweroff.sh` (used by `cluster-power.sh down`) which REFUSES to power off
  unless a future alarm is armed while `CLOUD_RTC_WAKE=on`. All cloudlab work branches from
  `gitea/main`, never from the operator's checkout (which is on an older feature branch).
- **WoL MACs are stale** in cloud-power `NODES`: cloud1 `00:e2:59:01:a6:62` (dark igc twin; correct
  `...:63`, fixed in cloudlab 2026-09-06) and cloud2 `00:e2:59:01:a6:52` (pre-rebuild board;
  correct `b4:2e:99:a8:a3:a7`, 2026-09-04). cloudlab `scripts/wol.py` is the source of truth.
  **WoL from full power-off has NOT been proven for the rebuilt cloud2 or for cloud1's `:63` port**
  (runbook's only success, 2026-08-20, predates both). With Auto ON disabled the ON button is the
  only way back, so the page must say so and a controlled WoL test is an operator decision.
- Observed, not in scope: cloud1 did not wake at 08:00 on 2026-10-01 (down 09-30 22:33 ->
  10-02 13:47); its journal for that shutdown ends before any hook line. Reported separately.

## Approach

### Where each setting lives

- **Desired settings** live in the existing runtime ConfigMap `cloud-power/cloud-power-state`, new
  key `auto`:
  `{"v":1,"off":{"enabled":bool,"at":"HH:MM","slot":"<ISO local slot>"|null,"result":str,"result_at":ts},
    "on":{"enabled":bool,"at":"HH:MM"}}`.
  - **Absent key** (first rollout, or the ConfigMap was deleted/restored from before this change)
    -> defaults: auto-off disabled 22:00, auto-on enabled 08:00. Both are the fail-safe directions
    (nothing powers off; hosts keep waking) and equal today's behaviour.
  - **Present but invalid** (bad JSON, unknown `v`, wrong shapes) -> NOT defaulted: the value is
    kept untouched for diagnosis, auto-off does not fire, the pool mirror does not write, the page
    shows "automation settings unreadable", `POST /api/auto` may overwrite it with a valid value.
    Manual OFF/CANCEL/ON and drain/release keep working (they never depended on `auto`).
  - The page distinguishes: settings loaded / state unavailable (ConfigMap unreadable) / save
    outcome unknown.
  No RBAC change (same object).
- **Auto ON is mirrored into PVE** as the comment of an empty pool `cloud-power` owned exclusively
  by cloud-power: `auto-on=1 wake=08:00` / `auto-on=0 wake=08:00`. Manual edits are overwritten by
  reconciliation (documented in the runbook). Each host reads it locally from `/etc/pve/user.cfg`
  at shutdown - no network, no dependency on ailab at shutdown time. A host partitioned from the
  cluster reads its last replicated copy; that is the accepted staleness bound.
- The pool id is a constant (`cloud-power`) in both repos, never configurable, never a request
  parameter.

### ailab: cloud-power (`kubernetes/apps/apps/cloud-power/app.py`)

1. **Persistence**: `Scheduler` gains `self.auto` + `self.auto_raw` + `self.auto_error`, loaded in
   `load()` and written by `_save()` together with `schedule`/`last` (one resourceVersion-checked
   object). `_save()` writes the RAW stored `auto` string back unchanged when it is invalid, so no
   code path silently replaces an operator's setting. Validation: exact keys, real bools, ASCII
   `HH:MM` via `re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", s, re.ASCII)`.
2. **Settings API**: `POST /api/auto` `{"kind":"off"|"on","enabled":bool,"at":"HH:MM"}`, routed
   AFTER the existing `MODE=wol` rejection (the hostNetwork pod gains nothing), behind `_peer_ok`,
   `_same_origin` (CSRF) and oauth2-proxy + the NetworkPolicy (the real authn). Body must be a JSON
   object, Content-Length <= 1024. Logged with identity. Under the scheduler lock:
   - refuse 503 while `dirty` (unsaved scheduler state must be flushed by the worker, not here);
   - apply ONLY the requested kind on top of the freshly loaded `auto`, save;
   - `StateConflict` -> reload, re-apply the same single change, retry once;
   - `StateError` (ambiguous: may have landed) -> `loaded=False`, answer 503 "outcome unknown -
     the page will show what is stored", never mirror it.
   - **A settings change only affects FUTURE slots**: when auto-off is enabled or its time changed,
     any slot under the NEW time whose instant is already <= now is marked consumed
     (`result="not run: settings changed after this slot"`), so a save can never trigger an
     immediate shutdown. Disabling auto-off does not touch an OFF already in flight (CANCEL does).
3. **Auto-OFF trigger**, evaluated in `tick()` BEFORE the phase machine (so cleanup finishing in
   the same tick cannot race it), when loaded, not corrupt, `auto` valid, not dirty, auto-off
   enabled and the tz loaded:
   - slot = the most recent local `at` instant <= now, built as an aware datetime in `AUTO_TZ`
     for today and yesterday (handles times before midnight). Elapsed = `now_ts - slot.timestamp()`
     (UTC arithmetic), due when `0 <= elapsed < AUTO_OFF_GRACE_SEC` (3600) and `slot != auto.off.slot`.
     DST: a nonexistent local time (spring-forward 03:xx) resolves via fold=0 per zoneinfo (fires
     at the equivalent instant, i.e. an hour later on the wall clock); a repeated one (autumn) fires
     once, at fold=0, because the slot key is the local date+time.
   - **ONE durable attempt per slot.** The slot is CLAIMED first (`auto.off.slot=<slot>`,
     `result="claimed"`, saved on its own); only after that write is confirmed does anything act.
     A failed/ambiguous claim write -> nothing happens this tick; the next tick re-reads and either
     sees the claim (done) or retries the claim while the window lasts.
   - After the claim: if an OFF is already in progress (manual or otherwise) -> result
     `"not run: an OFF was already <phase>"`. If no host answers on :8006 -> `"not run: no host
     answered (already off?)"`. Otherwise `self.schedule("auto-off <HH:MM>")` - the SAME path as the
     button - and the result is `"scheduled"`, or `"failed: <error>"` for Refused / GiteaError /
     StateError (no retry: schedule()'s own rollback paths - rejected/cancel_wanted/pausing ->
     releasing - handle whatever it may have persisted). The result is saved best-effort.
   - The power-off outcome is NOT tracked in `auto`: it is the schedule's existing `last` record
     (off / cancelled / error / stalled), which the page already shows.
   - Missed window (pod down past slot + grace) -> nothing late; next slot tomorrow.
   - The manual-OFF guest preflight is a human-UI safeguard and is deliberately not replayed: the
     page states next to the switch that Auto OFF drains cloud CI jobs only and stops every other
     guest on the cluster without asking.
   - ON (button) cancels a draining auto-off exactly as today (`cancel_for_wake`); it also claims
     the current slot if one is due and unclaimed, so ON at 22:00:05 is not followed by an OFF.
     RTC wake and direct LAN WoL do not pass through the scheduler (accepted).
   - Time zone: `AUTO_TZ` (default `Europe/Bucharest`) loaded at startup via `zoneinfo`. If it
     cannot be loaded: log loudly, auto-off is DISABLED (never silently UTC), page shows why, the
     rest of the API and the wol pod start normally (the wol pod never loads it).
4. **Auto-ON mirror** (worker, outside the scheduler lock, own lock, bounded):
   - only with loaded, valid, durably saved settings (`auto_error` empty, not dirty);
   - `GET /pools/cloud-power` then, if the comment differs, `PUT /pools`
     (`poolid=cloud-power&comment=...`) and a READ-BACK; `applied` only when the read-back matches.
   - nodes tried in order, only those answering :8006, each call timeout 8 s, existing pin-before-
     token; errors classified for the page: pin mismatch / HTTP 403 (ACL) / HTTP 404 (pool missing)
     / write refused (no quorum, 5xx) / unreachable.
   - cadence: on change (desired != last verified) retry at most every 60 s; otherwise re-verify
     every 300 s. `last_attempt_at`, `verified_at`, `verified_comment`, `error` are tracked
     separately; a settings change invalidates `applied`.
   - `POST /api/auto kind=on` saves first, then triggers one mirror attempt OUTSIDE the scheduler
     lock (bounded ~20 s total) and answers `saved` + `applied: true|false (reason)`.
   - Before power-off: `_tick_draining` runs one mirror attempt just before `_begin_power_off`,
     wrapped so any exception is logged and IGNORED (shutdown proceeds with whatever policy the
     pool holds; the page/log say so). Never inside the persisted powering_off transition.
   - A 403 does not mean the pool is missing: an existing (possibly old) comment keeps winning on
     the hosts until a write succeeds; the page says "hosts use the LAST APPLIED policy: ..." from
     the last successful read.
5. **Status**: `/api/status.auto` (from the published snapshot, never a live PVE call):
   `off: {enabled, at, tz, next_at (next unclaimed future slot, null when disabled),
   slot, result, result_at, standing_note}`, `on: {enabled, at, applied, verified_at,
   verified_policy, error, wol_unproven: true}`, `error` (settings unreadable) and `tz_error`.
6. **Page**: two rows under the buttons: `[x] Auto OFF at [22:00]` and `[x] Auto ON at [08:00]`
   (Europe/Bucharest, labelled). Checkbox change and time `change` POST immediately; controls are
   disabled while a save is in flight (serialised), the status poll never overwrites a control
   with a pending edit, the reply's stored value is re-rendered, errors shown. Status lines:
   next auto-off / last auto-off result; auto-on `applied to the hosts` or `saved, NOT yet applied
   (<reason>)`, plus: "takes effect at the next shutdown"; when disabled: "after the next shutdown
   the hosts stay off until ON is pressed - WoL on cloud1/cloud2 is unproven since their NIC/board
   changes". All Gitea/PVE strings via textContent. The page keeps the existing scrollable `#out`.
7. **WoL MACs**: cloud1/cloud2 fixed to the cloudlab `wol.py` values; a test compares NODES to a
   sibling cloudlab checkout's `wol.py` when present (skipped otherwise).
8. Homepage iframe `classes`: every breakpoint `h-32` -> `h-48` (all present in the served CSS,
   checked 2026-10-03).
9. `deployment.yaml`: env `AUTO_TZ=Europe/Bucharest`, `AUTO_OFF_GRACE_SEC=3600`; comment block.

### cloudlab: hosts

1. `scripts/cloud-arm-rtc-wake.sh` gains a resolver shared by every consumer:
   - `cloud-arm-rtc-wake --print` -> prints `on <HH:MM> <source>` or `off - <source>` with NO side
     effects; plain invocation resolves, then arms or CLEARS the alarm.
   - Precedence: (1) one-shot override `/run/cloud-power/wake-override` (written only by an
     explicit `cluster-power.sh down HH:MM`; tmpfs, gone after boot) > (2) the pool comment > (3)
     `/etc/default/cloud-power` > (4) built-in on/08:00.
   - Pool parsing (python3, data only, never sourced/eval'd): take the exact 3rd field of the
     single `pool:cloud-power:` line, `urllib.parse.unquote` once, split on whitespace, require
     exactly the keys `auto-on` (`0|1`) and `wake` (strict HH:MM), no duplicates; anything else ->
     invalid -> next source, logged with the reason.
   - Next occurrence computed in python with zoneinfo (system tzdata): today/tomorrow/day after at
     HH:MM local (fold=0 for nonexistent/repeated), first instant >= now + 120 s; if the target
     would be within 120 s (a drain that ran up to the wake time), arm now + 120 s instead of
     skipping a whole day.
   - `off` -> write `0` to wakealarm, re-read, log `cleared` or `ERROR: clear failed`.
   - Test seams: `CLOUD_POWER_USER_CFG`, `CLOUD_POWER_DEFAULTS`, `CLOUD_POWER_OVERRIDE`,
     `CLOUD_POWER_RTC`, `CLOUD_POWER_NOW` (epoch), `CLOUD_POWER_SKIP_ETHTOOL=1`.
   - Prints a `rev=<n>` marker so provisioning can verify the installed revision.
2. `scripts/cloud-drain-poweroff.sh`: decide via `cloud-arm-rtc-wake --print` instead of
   `CLOUD_RTC_WAKE`; `on` -> arm + verify future alarm (unchanged refusal); `off` -> clear, log
   "auto-on is OFF (<source>): powering off with no alarm".
3. `scripts/cluster-power.sh down [HH:MM]`: with an explicit HH:MM write the one-shot override
   (validated) instead of persisting `CLOUD_WAKE_AT`; without it, write nothing (the dashboard
   policy decides). Header comment updated.
4. `host/systemd/cloud-rtc-wake.service`: add `After=pve-cluster.service` so at shutdown the hook
   runs while pmxcfs is mounted (reverse ordering); keep `Before=pve-guests.service` and
   `Conflicts=shutdown.target`.
5. `scripts/provision-host.sh`: new section codifying the PVE access objects, each command checked
   (the block has no errexit) and the end state verified: role `CloudPower` (add or modify to the
   exact existing privs), role `CloudPowerAutoOn` (`Pool.Allocate,Pool.Audit`), user
   `cloudpower@pve` (add only if missing; the token is never touched), ACLs `/nodes` + `/vms` ->
   CloudPower, pool `cloud-power` created ONLY if missing (comment never written by provisioning),
   ACL `/pool/cloud-power` -> CloudPowerAutoOn `--propagate 0`; verify with
   `pveum user permissions cloudpower@pve` that the only pool path is `/pool/cloud-power` and the
   pool has no members. Plus the installed hook's `rev=` and the unit's `After=` include
   `pve-cluster.service`.
6. Tests: `scripts/tests/test_cloud_arm_rtc_wake.sh` (bash, fixtures + seams) covering absent pool,
   empty comment, on, off, `%3A`-encoded, garbage, duplicate keys, unreadable file, override wins,
   defaults fallback, clear, 120 s lead clamp, DST day. Wired into the existing CI if a suitable
   job exists, otherwise run in the PR and on the host.
7. Docs: runbook "RTC alarm" + README Day/night describe the precedence and the dashboard switch.

## Rollout order

1. cloudlab PR merged (from `gitea/main`), provisioning applied to ALL three hosts, verified on
   each: installed `rev=`, unit active + `After=pve-cluster.service`, `--print` resolves, effective
   permissions as above. Hook check with a pool comment set by hand to `auto-on=1 wake=<now+10m>`:
   `systemctl stop cloud-rtc-wake` arms it, then `systemctl start` (unit active again); then
   `auto-on=0` -> cleared; finish by removing the test comment and re-arming the 08:00 alarm.
2. ailab PR merged -> Flux. Verify through the deployed token: `/api/status.auto`, toggles from the
   page, pool comment replicated on all hosts (`user.cfg` on each), `applied: true`, then each
   host's `--print` shows `source=pool`.
3. Only then is Auto ON presented as effective. Auto OFF stays disabled until the operator enables
   it; the first real cycle (22:00 drain -> shutdown with the intended alarm -> runners re-enabled
   -> 08:00 wake on all three) is checked from the cloud-power log, the hosts' journals and their
   boot times.
4. WoL test (cloud1 + cloud2 from full power-off via the dashboard ON button) is proposed to the
   operator; it is required before Auto ON = off is relied upon.

## Rollback

The old binary's `_save()` writes only `schedule`/`last`, which DROPS `auto` on its next write, and
it never touches the pool. So before reverting the ailab change: set Auto ON enabled (pool
`auto-on=1`) from the page, let a drain in flight finish or CANCEL it, then revert. If the pool must
be neutralised by hand: `pveum pool modify cloud-power --comment ''` (hosts fall back to defaults).
Reverting cloudlab alone restores the old hook, which ignores the pool.

## Critical files

- ailab `kubernetes/apps/apps/cloud-power/app.py`, `deployment.yaml`;
  `kubernetes/apps/apps/homepage/configmap.yaml`; `scripts/tests/test_cloud_power.py`.
- cloudlab `scripts/cloud-arm-rtc-wake.sh`, `scripts/cloud-drain-poweroff.sh`,
  `scripts/cluster-power.sh`, `host/systemd/cloud-rtc-wake.service`, `scripts/provision-host.sh`,
  `scripts/tests/test_cloud_arm_rtc_wake.sh`, `docs/runbooks/cloud-gpu-cluster.md`, `README.md`.

## Verification

- ailab unit tests (`python -m unittest discover -s scripts/tests -p "test_*.py"`): slot math
  (midnight, exact grace boundaries, real Europe/Bucharest DST 2026-03-29 / 2026-10-25 when tzdata
  is available, fixed-offset otherwise); claim-before-act and one attempt per slot across restart,
  409, ambiguous claim write; OFF already in progress; no host answering; schedule Refused /
  GiteaError -> recorded, not retried; ON claims a due slot; settings change never fires the
  current slot; invalid `auto` preserved and suspends automation without blocking manual OFF;
  `_save` round-trips `auto`; mirror read/write/read-back, errors classified, cadence, never
  writes untrusted settings, exception before power-off ignored; HTTP: `/api/auto` 404 in
  MODE=wol, cross-site refused, malformed/oversized body 400; MACs match wol.py when present.
- cloudlab: `test_cloud_arm_rtc_wake.sh` locally (Git Bash + python3) and on cloud3.
- Live: Rollout steps 1-4.

<!-- codex-review-status: finalized -->
