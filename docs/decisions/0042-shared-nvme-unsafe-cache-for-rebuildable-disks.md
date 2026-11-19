# ADR 0042: Rebuildable guest disks run `cache=unsafe`; on-prem runners go to 60 MiB/s

**Status:** ACCEPTED (2026-11-19), owner-directed. The owner's constraints and decisions, in their words: the
drives *"are really expensive. We cannot replace them now"*; *"Assume there will be no power losses and take into
consideration that the data we store on these drives is mostly temporary data, i.e. worst case if something is
corrupted, we can redeploy"*. The owner then chose cache=unsafe on the CI runner and reviewer disks on all nodes at
once, and node1 and node2 runners at 60 MiB/s.

**Review:** the plan was reviewed by Fable. Codex was unavailable that morning: the router's Codex pool returned 429
three times, and the LiteLLM fallback returned `model_not_found`. Its brief is reproduced under Context.

**Related:** the 2026-11-18 RCA (shared QLC NVMe, etcd stalls); #1857 and #1858 (runner disk cleanup and thin-pool
trim); the D1 write caps (kubernetes/infra/runners/variables.tf).

## Context

Each Proxmox node has ONE Kingston OM8TAP42048K1 2 TB QLC DRAM-less NVMe:
- volatile write cache (vwc 0x7), no power-loss protection;
- wear on 2026-11-19: 90% / 86% / 55% (node1/2/3).

That one drive carries the node's Talos control-plane VM (an etcd member), its CI runners, dev workers, an agent node,
LLM LXCs, and on node1 the registry.

Every VM disk ran `cache=none`. Every guest fsync therefore became a device-wide FLUSH. That includes the CI test
databases, docker, builds and etcd's own WAL. The 24 h averages on 2026-11-19:

| Node | Flushes/s | etcd minutes with WAL fsync p99 > 0.5 s (7 d) |
|---|---|---|
| node1 | 564 | 678 |
| node2 | 109 | 392 |
| node3 | 139 | 70 |

Stalls of 1-8 s happened even at low load. One leader stall (03:31Z) cost the Relay agent its lease. etcd already runs
heartbeat 250 ms and election timeout 5000 ms.

The 30 MiB/s runner caps (D1, 2026-11-17) made etcd quieter, but they starved CI. Over 48 h, workbricks `verify`
results were:

| Runners | Write cap | Failed |
|---|---|---|
| node2 | 30 MiB/s | 14 of 32 |
| node1 | 30 MiB/s | 3 of 14 |
| node3 | 60 MiB/s | 0 of 37 |

The test Postgres dies when its fsyncs and writes are throttled. The throughput cap, not the IOPS cap, was the cause.

## Decision

1. **`cache=unsafe` on rebuildable guest disks: the on-prem CI runners and the two reviewer VMs.**
   - QEMU acknowledges the guest's flushes without passing them to the host. Their fsyncs stop being device FLUSHes,
     and short-lived files may never reach NAND.
   - Runners: `var.runner_disk_cache` (default `unsafe`) in `kubernetes/infra/runners`, with
     `reboot_after_update = false`. A cache change takes effect only on a full stop/start, so each runner is drained
     and restarted by hand (`docs/runbooks/ci-runners.md`, "Disk cache mode").
   - Reviewers (4501/4502, out-of-band, not in tofu): `qm set <vmid> --scsi0 <existing line>,cache=unsafe`, then a
     stop/start while idle. Record the change in `docs/runbooks/ci-runners.md` "Disk cache mode".
2. **Host writeback bounds** (`pve_base_writeback_sysctl`): `vm.dirty_background_bytes` 256 MiB and `vm.dirty_bytes`
   2 GiB. Without them, the host could build a multi-GiB dirty backlog and flush it in one burst onto the shared NVMe.
3. **On-prem runner caps:** all at 60 MiB/s, with no IOPS cap. The cap stays as the host-memory guard: it bounds how
   fast a runner can dirty the host's page cache.

## What this does NOT do, and why

- **No `cache=unsafe` or `--unsafe-no-fsync` for etcd or the control-plane VMs.** A host crash, QEMU kill or OOM can
  happen without a power loss. Any of them would drop raft entries that etcd had acknowledged. A member that forgets
  its vote or log can diverge silently, and all three members share the exposure.
- **No `cache=unsafe` on dev workers.** Their uncommitted work is not temporary.
- **No tmpfs scratch.** The hosts have ~20-25 GiB free next to the LLM LXCs.

## Consequences

- **A host crash** (not a power loss) can corrupt a runner's or reviewer's filesystem.
  - Recovery is a rebuild: tofu plus the gitea_runner role, or the reviewer playbook.
  - A runner whose filesystem shows errors after a host crash is rebuilt, not repaired.
- **Wear.** node1's drive has ~36 TB of rated endurance left, roughly 12 days at the 2026-11-19 node-wide rate.
  - Most of that rate is dev workers and the registry, not CI.
  - Passing 100% is not failure, but the risk rises. Watch `percentage_used` with `nvme smart-log`.
- **NVMe APST:** a separate change, trialled live on node3 from 2026-11-19 07:26Z.
- **Rollback:** set `runner_disk_cache = "none"` (and `qm set ... cache=none` on the reviewers), then restart each guest
  drained. To restore the caps, set the runner's `disk_write_*` fields back.
