# ADR 0002 — QNAP runs QuTS hero (ZFS), lz4, no dedup

**Status:** accepted · **Date:** 2026-06-14

## Context
QNAP TBS-h574TX with 5× NVMe (PCIe Gen3 ×2 each) and **16 GB soldered, non-expandable RAM**.
The NAS backs a Proxmox + future Kubernetes/AI cluster (snapshots, clones, integrity desirable).

## Decision
- Run **QuTS hero (ZFS)**, not QTS (ext4).
- `compression=lz4` ON (extends SSD life, often improves throughput).
- **dedup OFF** — the ZFS dedup table would exhaust 16 GB RAM.
- Pool geometry **decided after discovery** (mirror/RAID10-style vs 5-wide RAID-Z1) with explicit
  owner approval — ZFS cannot migrate RAID level after creation (one-way door).

## Consequences
- Gains: block snapshots/clones (surfaced by the QNAP CSI driver later), checksums/self-healing,
  inline compression.
- Constraints: limited ARC (16 GB) caps cache effectiveness under many concurrent sessions; no dedup.
- Per-drive throughput ~1.6 GB/s (Gen3 ×2), so size pool throughput expectations modestly.

## Alternatives rejected
- **QTS / ext4**: simpler, lower RAM pressure, but loses snapshots/clones/compression/integrity.
