# ADR 0004 — Thunderbolt-net ↔ QNAP T2E on Linux (primary risk)

**Status:** accepted (with validation gate) · **Date:** 2026-06-14

## Context
QNAP's Thunderbolt-to-Ethernet (T2E) is officially documented only for macOS/Windows. The Linux
`thunderbolt-net` module implements the same ThunderboltIP/XDomain protocol (mainline since
kernel 4.15), so interop *should* work — but it is unverified by QNAP and there is a known
**T2E-on-port-2 driver bug**. On Strix Halo, `thunderbolt-net` is also driver-bound to
~10–11 Gbps/dir (not 40G).

## Decision
- Treat TB-to-QNAP as the primary storage path **but gate it on real-hardware validation**
  (Phase 4): bring up `thunderbolt0`/`en05`, assign static IPs, `iperf3 --bidir`, test **both** TB
  ports, reboot-persistence test.
- Use the scyto / `pieter-v-n/pmx-cluster-tb` method for stable interface naming + persistence.
- Pin a recent PVE kernel (≈6.17+/6.19+) required for USB4 stability (and later gfx1151 ROCm).

## Consequences / fallback
- If a TB port is flaky or T2E doesn't present a stable Linux interface: fall back to the QNAP
  **10GbE** for storage (+ a dedicated 10G switch later). Since TB ≈ 10GbE here, the bandwidth hit
  is small; the loss is the two *extra* dedicated links.
- Bandwidth expectations are set from Strix-Halo Linux measurements, not generic Intel-TB figures.
