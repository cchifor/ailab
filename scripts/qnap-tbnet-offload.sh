#!/bin/sh
# Runs ON THE QNAP (root cron, every minute): keep TSO/GSO OFF on the Thunderbolt network ports.
# Installed by scripts/qnap-tbnet-offload-install.sh — do not hand-edit the deployed copy.
#
# WHY (2026-09-29). The NAS's Thunderbolt bridge tbtbr0 (members tbtnet0p0/tbtnet1p0) runs MTU 65522
# with TSO/GSO on, so it sends up-to-64 KB segmentation-offloaded frames across Thunderbolt. ai-node1/2
# receive them on thunderbolt0 as single oversized packets (the kernel logs "Driver has suspect GRO
# implementation"). Delivered locally that is harmless — but those hosts ROUTE the Talos CP VMs' iSCSI
# traffic (ADR 0011: routed + SNAT, VM MTU 1500), and a DF packet larger than the VM bridge's MTU cannot
# be forwarded: node1 had counted 60.6M IpFragFails. Every such drop is a TCP retransmit, so iSCSI from
# cp1/cp2 crawled: 150-420 ms write latency, ~6 MB/s sequential, a 700 KB HTTP GET from a cp1 pod
# 0.5 s (vs 2-6 ms from cp3, whose host reaches the NAS over ethernet). With TSO/GSO off here the NAS
# emits MSS-sized (<=1460 B) segments: the same GET took 0.009 s, IpFragFails +0, the infra-pg re-clone
# went 6 -> 89 MB/s. NAS CPU cost is negligible (it idles at ~5%).
#
# Every minute, not only at boot: a Thunderbolt link flap or re-plug re-creates tbtnetNpM with default
# (on) offloads, and QNAP has no supported boot hook that survives firmware updates. Idempotent and
# silent when already correct; logs (syslog tag tbnet-offload) when it changes something, and at
# ERROR every minute if it cannot.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
for i in tbtbr0 $(ls /sys/class/net 2>/dev/null | grep -E '^tbtnet[0-9]+p[0-9]+$'); do
  [ -e "/sys/class/net/$i" ] || continue
  if ethtool -k "$i" 2>/dev/null | grep -qE '^(tcp-segmentation-offload|generic-segmentation-offload): on'; then
    if ethtool -K "$i" tso off gso off 2>/dev/null        && ! ethtool -k "$i" 2>/dev/null | grep -qE '^(tcp-segmentation-offload|generic-segmentation-offload): on'; then
      logger -t tbnet-offload "disabled tso/gso on $i" 2>/dev/null || true
    else
      # Loud on purpose: a firmware update can make the feature `on [fixed]` or move ethtool, and a
      # silent retry loop would let the exact iSCSI slowdown this prevents return unnoticed.
      logger -p user.err -t tbnet-offload "FAILED to disable tso/gso on $i - cp1/cp2 iSCSI will crawl (runbook qnap-storage-setup.md section 10)" 2>/dev/null || true
    fi
  fi
done
exit 0
