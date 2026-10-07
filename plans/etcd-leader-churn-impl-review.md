# Implementation review — etcd-leader-churn — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- Scope matches the approved D/E work and B’s code changes. The optional csi-nfs/CNPG omissions are justified; hardware work and B’s pending roll are not implementation omissions.
- The trim script needs fixes before unattended use, including a host-privilege boundary violation, ignored command failures, a pressure guard that permits trimming on errors, and unreliable stop-file handling.
- Alert expressions, labels, registration, and durations match the plan; existing etcd alerts and `ControlPlaneRestartWave` remain enabled. Kyverno 3.3.6 supports the [replica count and argument](https://raw.githubusercontent.com/kyverno/kyverno/kyverno-chart-3.3.6/charts/kyverno/templates/admission-controller/deployment.yaml) and [automatic PDB](https://raw.githubusercontent.com/kyverno/kyverno/kyverno-chart-3.3.6/charts/kyverno/templates/admission-controller/poddisruptionbudget.yaml). Its [node anti-affinity is preferred](https://raw.githubusercontent.com/kyverno/kyverno/kyverno-chart-3.3.6/charts/kyverno/values.yaml), so separate-node readiness and admission continuity still require verification after the owner merges #2137.
- Applying only the etcd delta per node is a sound response to the unrelated provider/image drift. Use an explicit single-node target, inspect the patch dry-run, and retain every backup, shutdown, quorum, and CNPG gate; provider/state reconciliation remains separate. The code correctly uses Talos CLI [`--mode=no-reboot`](https://raw.githubusercontent.com/siderolabs/talos/v1.11.2/cmd/talosctl/pkg/talos/helpers/mode.go), distinct from the provider’s `no_reboot`.
- Bash syntax and diff whitespace checks passed; read-only, in-memory stub executions reproduced the failure-handling and stop-file defects. Full shell fixtures, promtool, Helm rendering, and live maintenance tests were not rerun here; the stopped sizing run does not satisfy E’s real-trim/WAL verification gate.

## Findings

### Container executables run with host-root authority

**Location:** `scripts/lxc-fstrim-chunked.sh:39`, `scripts/lxc-fstrim-chunked.sh:58`  
**Severity:** blocker

<!-- codex: The root service enters only the mount namespace, so df/fstrim and their loader/libraries resolve inside the unprivileged registry LXC while retaining host user-namespace credentials; container root can replace them and obtain host-root execution ([Linux namespace switch](https://raw.githubusercontent.com/torvalds/linux/v6.14/fs/namespace.c), [nsenter execution path](https://raw.githubusercontent.com/util-linux/util-linux/v2.41/sys-utils/nsenter.c)). Suspend the installed timer until both operations use trusted host-side code operating on a validated mount/file descriptor, without executing container-controlled binaries or libraries. -->

### Failed trims are reported as successful service runs

**Location:** `scripts/lxc-fstrim-chunked.sh:58`  
**Severity:** important

<!-- codex: The exit status of nsenter/fstrim is discarded: set -uo pipefail does not enable errexit, an error message produces an empty bytes value, and the script eventually exits 0 even when every chunk fails, which I reproduced with stubs. Explicitly check each trim’s status, stop with a nonzero exit and diagnostic context, and test failures on both the first and a later chunk so systemd cannot report unsuccessful reclamation as success. -->

### Invalid PSI data permits trimming

**Location:** `scripts/lxc-fstrim-chunked.sh:34`, `scripts/lxc-fstrim-chunked.sh:50`  
**Severity:** important

<!-- codex: Missing, empty, or malformed PSI data makes the integer comparison fail, which exits the while condition as though pressure were acceptable; a reproduction with PSI_FILE=/dev/null still issued all three trim calls. Parse and validate the explicit some avg10 decimal value, refuse trimming when it cannot be read, and add error/boundary fixtures including 45.01%, which currently truncates to 45 and bypasses the documented threshold. -->

### A stop request during the pressure wait permits another chunk

**Location:** `scripts/lxc-fstrim-chunked.sh:48`  
**Severity:** important

<!-- codex: STOP_FILE is checked only before entering the pressure loop, so creating it during that wait still allows another chunk once pressure drops, or waits up to 30 minutes before returning the pressure-timeout error; the extra trim was reproduced with stubs. Check the stop condition during the wait and immediately before trimming, and add a fixture that introduces the stop request during sleep rather than only before startup. -->

### Filesystem capacity is not the correct trim address limit

**Location:** `scripts/lxc-fstrim-chunked.sh:39`  
**Severity:** important

<!-- codex: On ext4, df reports filesystem blocks minus metadata overhead, while FITRIM offsets address the full filesystem block range, so rounding df’s capacity up to a chunk boundary can still omit trailing block groups when overhead exceeds that rounding allowance ([statfs implementation](https://raw.githubusercontent.com/torvalds/linux/v6.14/fs/ext4/super.c), [trim implementation](https://raw.githubusercontent.com/torvalds/linux/v6.14/fs/ext4/mballoc.c)). Derive the endpoint from verified filesystem geometry and test metadata-excluding capacity values; the full-length final request itself is acceptable because ext4 clamps it to the filesystem end. -->

### The trim runbook omits the required WAL verification

**Location:** `docs/runbooks/registry-cache.md:193`  
**Severity:** important

<!-- codex: The verification checklist checks scheduling, logs, and pool usage but omits plan E’s WAL-latency watch and reclamation-without-WAL-spikes gate; host-wide avg10 PSI below 45% does not exclude a short, disruptive etcd fsync stall. Add concrete per-member WAL/backend latency and leader-change checks, stop criteria, and recorded results from a supervised real trim before treating E as verified, since the stop-file sizing run never exercised discard. -->

### Invalid numeric settings can silently disable trimming or hang

**Location:** `scripts/lxc-fstrim-chunked.sh:25`, `scripts/lxc-fstrim-chunked.sh:42`  
**Severity:** nit

<!-- codex: Environment settings enter arithmetic without validation: CHUNK_GB=-1 reproducibly exits 0 without trimming, while PAUSE_S=0 makes a high-pressure wait spin indefinitely because waited never advances. Validate positive integer chunk/pause values and sensible pressure/wait limits before use, check multiplication bounds, and add rejected-input fixtures. -->

### The cp1 fixture does not protect the hold duration

**Location:** `kubernetes/apps/infrastructure/monitoring/control-plane-storage-rules.test.yaml:37`  
**Severity:** nit

<!-- codex: The short-leadership fixture evaluates at 12m, after its series has already returned to zero, so it still passes if EtcdLeaderOnWorstMember loses its for clause and fires during the preceding brief leadership period. Assert absence while leadership remains true before 15m, and add continuous-leadership assertions immediately before and at the 15m boundary. -->

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