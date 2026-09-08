# Implementation plan — DR remediation (keys, Gitea backup, USB, OpenBao, backup chain)

## Context

The 2026-09-08 solidity audit found the estate solid at *running* and red at *recovering*. Five
findings were independently re-verified by hand before this plan was written:

| finding | verified evidence |
|---|---|
| No complete Velero backup since 2026-09-06 | `velero-daily-20260908020031` = `Failed`, 55 errors, no completionTimestamp; previous night `PartiallyFailed`. `VeleroBackupStale` critical firing |
| `infra-pg` has no backup at all | `spec.backup` empty; zero `ScheduledBackup` resources repo-wide |
| Its standby is silently dead | slot `_cnpg_infra_pg_3` → `active=f`, `wal_status=lost`, while CNPG reports 2/2 ready |
| OpenBao unseal escrow is for a different vault | git escrow `cluster_id fdd2e119`; live `522a7c2c` |
| The USB disk is failing | 6 usb 4-1 reset/disconnect events this boot; `blk_update_request: I/O error` on sdc; re-enumerated sdb→sdc; `Errors behavior: Continue`; `Last checked: Jun 18` (creation), `Maximum mount count: -1`, `Check interval: 0` |

`infra-pg` holds **8 databases, gitea = 5936 MB**, plus authelia, agentforge_platform, openwebui,
grafana, litellm, agentforge_broker. It is the database behind the forge, SSO, dashboards and the
LLM proxy. It has never been backed up.

Execution order requested by the operator: **1 → 3 → 4 → 5 → 2**.

## 1. Key escrow to `C:\Users\chifo\work\keys\backup`

Copy the three DR decryption keys plus the escrow README:

| file | bytes | what it decrypts |
|---|---|---|
| `age.agekey` | 189 | every SOPS secret in the repo |
| `talos-backup-age.key` | 189 | the 12.5 GB of etcd snapshots |
| `rclone-crypt-escrow.txt` | 214 | the entire off-site Google Drive copy |

**State the residual gap rather than hide it.** The destination is on the SAME laptop as `_out/`.
This protects against the failure that has actually happened before — `_out/` being swept or lost
mid-session, which `ESCROW-README.txt` records — but it does **not** protect against loss of the
machine, which is the scenario that makes the off-site copy matter. Off-machine escrow (paper, or a
hardware token stored elsewhere) remains outstanding and must be reported as such, not marked done.

Verify by checksum, not by "the copy command returned 0": compute sha256 of source and destination
and compare. Set the destination read-only. Do **not** print key material at any point.

## 2. Backup-chain re-drive — solution now, executed after item 3

Provide the solution here; implement after Gitea has a backup, because the chain currently writes to
the disk that is failing.

1. **Diagnose the failure first.** `velero-daily-20260908` uploaded 3,951/3,951 items and then failed
   writing its own metadata — consistent with the USB disconnects. Read the Velero logs for that
   backup and confirm the failure is I/O against versitygw rather than a Velero defect. If it is the
   disk, item 4 must land first or the retry fails the same way.
2. **Off-site catch-up FIRST.** The on-site copy lives on the failing disk, so the off-site leg is
   the more valuable of the two. Trigger the `rclone-offsite` CronJob manually and confirm it
   completes.
3. **Then an on-demand Velero backup** from the daily schedule, and confirm a `velero-backup.json`
   object actually lands rather than trusting the CR phase.
4. **Do not hand-delete the Failed CR** — its TTL expires it and Kopia reclaims the orphaned
   PodVolumeBackups. Deleting by hand risks orphaning repository state.

## 3. Gitea must have a backup — the priority

Three parts, in this order. Part (a) is the safety net and comes before anything that touches the
cluster.

### 3a. Immediate logical dump (manual, now)

`pg_dump -Fc` every database in `infra-pg`, plus role definitions, to a location that is **not** the
USB disk and **not** versitygw. Verify each dump with `pg_restore --list` (a dump that cannot be
listed is not a backup) and record byte sizes.

There is no `infra-pg-superuser` secret — CNPG only creates one when `enableSuperuserAccess` is on.
Two options, and the plan deliberately picks the second:

- Enable superuser access and take one `pg_dumpall`. Simpler manifest, but it creates a
  network-reachable superuser credential purely for backups.
- **Per-database dumps using the seven existing owner secrets** (`infra-pg-gitea`, `-authelia`,
  `-grafana`, `-litellm`, `-openwebui`, `-afp-app`, `-afp-admin`). No new privilege, no cluster spec
  change, no restart. Roles are **not** captured this way — that is acceptable because CNPG recreates
  roles declaratively from those same secrets on restore, so the restore path does not need
  `pg_dumpall --globals-only`. This must be written down, because a dump set that silently lacks
  globals looks complete.

### 3b. Re-clone the dead standby

`_cnpg_infra_pg_3` is `wal_status=lost`: the primary has already recycled the WAL the standby needs,
so it can never catch up on its own. It will sit there reporting healthy forever, and `infra-pg-ro`
routes reads to it.

Delete `infra-pg-3`'s PVC and pod so CNPG re-bootstraps it from the primary via `pg_basebackup`.
Only after 3a, so a full dump exists before touching the cluster. Expect load on the primary during
the re-clone. Verify afterwards that the slot returns to `active=t, wal_status=reserved` and that
`pg_stat_replication` shows a streaming standby.

### 3c. Codify a recurring dump (GitOps)

A `CronJob` in the `backup` Kustomization — the layer created today that nothing depends on, so a
backup failure still cannot gate `apps`.

- dumps every database nightly, before the 04:00 off-site sync
- writes to a **`qnap-iscsi` PVC on the ZFS pool** (RAID-Z1, scrubbed Sep 1, healthy) — explicitly
  not the USB disk, and not through versitygw
- prunes dumps older than N days
- pushes the dump directory to `gdrive-crypt` using the existing `backup-offsite` rclone config, so
  the off-site copy of the database **does not depend on versitygw at all**
- `activeDeadlineSeconds` and `concurrencyPolicy: Forbid`, per the W2 pattern
- an alert on staleness — a dump CronJob that silently stops is the same failure class this whole
  effort exists to remove

### 3d. Prove a restore

A dump nobody has restored is a hypothesis. Restore the gitea dump into a scratch database in the
same cluster and assert row counts on a couple of core tables against the live database. Drop the
scratch database afterwards.

## 4. Stop the USB disk accepting writes after an error

`tune2fs -e remount-ro /dev/sdc2`. Free, no downtime, no data movement. Today the filesystem is
mounted `Errors behavior: Continue`, so an aborted journal keeps accepting writes — which is how a
disconnect becomes corruption.

This deliberately makes the gateway **fail closed**. That is now the correct behaviour rather than a
regression, because the W3 watchdog will not restart on a bad disk, the W4 probe will alert, and
Gitea no longer depends on the gateway at all.

Then, and separately: the enclosure (JMicron 152d:0562) is the actual fault — 6 reset/disconnect
events this boot. `e2fsck` on a link that drops twice a day can itself be interrupted mid-write, so
**fsck is not part of this change**. Recommend reseating/replacing the enclosure first; record fsck
as a follow-up to be done offline with versitygw stopped and the filesystem unmounted.

## 5. Repair the OpenBao unseal escrow

`kubernetes/infra/openbao-unseal.sops.yaml` decrypts to `cluster_id fdd2e119`; the live vault is
`522a7c2c`. The 2026-08-30 re-bootstrap skipped step 6 of `openbao-recovery.md` and it has drifted
undetected. This is the one cold DR path that touches neither Velero, versitygw, the QNAP nor the
USB disk, and it is currently dead.

- Rewrite it from the live `openbao/openbao-keys` secret.
- Write it as a `kind: Secret` with `stringData`, **not** a flat YAML document: a flat document does
  not match the SOPS `encrypted_regex` and passes through in PLAINTEXT — that is exactly what caused
  the 2026-07-24 leak.
- Assert every leaf starts with `ENC[` before committing. Verify by decrypting and comparing the
  `cluster_id` to the live vault.
- Correct the file's own ROTATE instruction: `bao operator rekey` and `generate-root` are compiled
  out of 2.5.5 and return 405, so the documented rotation procedure is impossible as written.
- Add a `cluster_id` comparison to the existing OpenBao probe so this cannot drift silently again.

## Rollback

- **1** — copies only; nothing to roll back. Never move the originals.
- **3a** — read-only against the database.
- **3b** — the riskiest step. If the re-clone fails, the cluster continues on the primary alone (it
  is already effectively single-instance today, since the standby cannot serve current data). Do not
  proceed without a verified dump from 3a.
- **3c** — a new CronJob in a layer nothing depends on; revert the commit.
- **4** — `tune2fs -e continue` restores the previous behaviour.
- **5** — the current file is already useless, so any correct replacement is an improvement; keep the
  old file in git history.

## Acceptance criteria

- [ ] Three keys present at the destination with sha256 matching source, and the off-machine gap
      explicitly reported as still open
- [ ] A verified `pg_dump` of all 8 databases exists off the USB disk, each listable by `pg_restore`
- [ ] `_cnpg_infra_pg_3` back to `active=t`, `wal_status=reserved`, streaming
- [ ] A committed, reconciled CronJob producing dated dumps, with an off-site copy that does not
      transit versitygw
- [ ] The gitea dump demonstrably restores into a scratch database with matching row counts
- [ ] `tune2fs -l` shows `Errors behavior: Remount read-only`
- [ ] The OpenBao escrow decrypts to `cluster_id 522a7c2c`, with every leaf `ENC[`
- [ ] A Velero backup reaching `Completed`, and an off-site sync newer than the failure
- [ ] Estate still green: all Kustomizations Ready, forge serving

<!-- codex-review-status: pending -->
