# Implementation review — etcd-leader-churn — round 2

<!-- codex-impl-review-status: complete -->

## Round 2 verdict

- All eight round-1 code/documentation fixes are accepted. The former blocker is resolved: commands, device resolution and mount-target parents are host-controlled; container symlinks are not traversed. The configured unprivileged container has no raw block-device access to forge the metadata consumed by `tune2fs`.
- Mounting the same ext4 device shares its superblock and journal. The added restrictions apply to the new mount; a container's per-mount read-only restriction remains intact, while a superblock rw/ro mismatch is rejected. Ordinary start/stop races and concurrent mounts do not create independent journals. Concurrent script invocations can still overlap discard work; the documented systemd unit serializes its own starts.
- One new important issue remains: failed unmounts can leave a host mount behind while reporting success. Fix cleanup status handling and add a failure fixture before unattended use.
- Both Bash syntax checks, diff whitespace checks and a minimal EXIT-trap reproduction passed. The fixtures were reviewed but not rerun in this read-only workspace; live mount behavior and the pending supervised run's outcome were not independently verified.

## Findings

### Container executables run with host-root authority

**Location:** `scripts/lxc-fstrim-chunked.sh:39`, `scripts/lxc-fstrim-chunked.sh:58`  
**Severity:** blocker

**Resolution (accepted, fixed in a5dc71c4):** the script no longer enters the container's namespaces. It resolves `mpN` with `pct config` + `pvesm path`, reads ext4 geometry with `tune2fs`, mounts the volume privately on the host (`nosuid,nodev,noexec`), and runs the host's `fstrim`. The timer was disabled and the installed script made non-executable within minutes of the finding. The fixed script was redeployed and dry-run on ai-node1 (geometry 412316860416 bytes, clean mount/unmount, container unaffected).

### Failed trims are reported as successful service runs

**Location:** `scripts/lxc-fstrim-chunked.sh:58`  
**Severity:** important

**Resolution (accepted, fixed in a5dc71c4):** each fstrim's status is checked. A failure exits 5 with the chunk, offset and error. Fixture: failure on chunk 2, so exit 5, 2 calls, still unmounted.

### Invalid PSI data permits trimming

**Location:** `scripts/lxc-fstrim-chunked.sh:34`, `scripts/lxc-fstrim-chunked.sh:50`  
**Severity:** important

**Resolution (accepted, fixed in a5dc71c4):** `some avg10` is parsed as a decimal with a strict regex. Unreadable pressure exits 4 without trimming, and the comparison is a float compare, so 45.01 > 45. Fixtures: empty PSI file gives exit 4 and 0 trims; 45.01 waits and then gives up with exit 3.

### A stop request during the pressure wait permits another chunk

**Location:** `scripts/lxc-fstrim-chunked.sh:48`  
**Severity:** important

**Resolution (accepted, fixed in a5dc71c4):** the stop file is checked before each chunk and after every pressure-wait sleep. Fixture: a stop file created during the wait leaves 0 trims and exit 0.

### Filesystem capacity is not the correct trim address limit

**Location:** `scripts/lxc-fstrim-chunked.sh:39`  
**Severity:** important

**Resolution (accepted, fixed in a5dc71c4):** the range is ext4 `Block count` x `Block size` from `tune2fs -l`. On ai-node1 that is 412316860416 bytes, vs df's 405240442880 (about 7 GB of trailing range the old version could miss).

### The trim runbook omits the required WAL verification

**Location:** `docs/runbooks/registry-cache.md:193`  
**Severity:** important

**Resolution (accepted, fixed in a1fec2cc):** the runbook now has per-member WAL fsync p50/p99, fsyncs over 1.024 s, and leader-change checks, with explicit stop criteria and the recorded 2026-10-06 supervised run. A supervised real run of the fixed script is in progress with an automated stop-criteria guard; its result goes into the PR.

### Invalid numeric settings can silently disable trimming or hang

**Location:** `scripts/lxc-fstrim-chunked.sh:25`, `scripts/lxc-fstrim-chunked.sh:42`  
**Severity:** nit

**Resolution (accepted, fixed in a5dc71c4):** CHUNK_GB (1-1024), PAUSE_S and PSI_WAIT_MAX_S (positive integers) and PSI_MAX (0-100) are validated before anything is mounted (exit 2). Fixtures: CHUNK_GB=-1/0, PAUSE_S=0, PSI_MAX=abc and PSI_WAIT_MAX_S=x all give exit 2 with nothing mounted.

### The cp1 fixture does not protect the hold duration

**Location:** `kubernetes/apps/infrastructure/monitoring/control-plane-storage-rules.test.yaml:37`  
**Severity:** nit

**Resolution (accepted, fixed in aa296a23):** asserts no alert at 9m while cp1 still leads, and no alert at 14m / alert at 16m for continuous leadership. Mutation check: with `for:` removed, the fixture fails at 9m and 14m.

### Failed unmounts are reported as successful service runs

**Location:** `scripts/lxc-fstrim-chunked.sh:78`, `scripts/tests/test-lxc-fstrim-chunked.sh:55`  
**Severity:** important

<!-- codex: round-2: The EXIT trap preserves the status that caused the shell to exit; a failing command inside it does not replace that status. Consequently, after normal completion or STOP_FILE, an umount failure still produces exit 0 and systemd success. For example, another host process holding a working directory under the temporary mount can cause EBUSY: rmdir is then skipped and the filesystem remains mounted in the host namespace, keeping the LV busy even after the container stops. This is a separate mount path, not an isolated mount namespace that disappears with the script. Replace the trap with explicit cleanup status handling: capture the original status, report unmount/removal failures with the device and mount path, and return nonzero on cleanup failure when the original status was zero, preserving an existing failure otherwise. Remove the directory only after successful unmount. Add an umount-failure fixture asserting a failed run and diagnostic; the current stub always succeeds and cannot detect this defect. Early die calls before mount do not encounter an uninitialized trap, and successful cleanup correctly preserves the existing trim/pressure failure codes. -->

## Diff stat

```text
 .gitea/workflows/manifests.yaml                    |  11 ++
 docs/runbooks/registry-cache.md                    |  44 ++++++++
 .../control-plane-storage-rules.test.yaml          |  99 ++++++++++++++++++
 .../monitoring/control-plane-storage-rules.yaml    |  44 ++++++++
 .../infrastructure/monitoring/kustomization.yaml   |   1 +
 .../infra/machine-config/controlplane.yaml.tftpl   |  24 ++++-
 kubernetes/infra/talos.tf                          |   7 ++
 scripts/lxc-fstrim-chunked.sh                      |  66 ++++++++++++
 scripts/tests/test-lxc-fstrim-chunked.sh           | 113 +++++++++++++++++++++
 9 files changed, 408 insertions(+), 1 deletion(-)
```