# Runbook — Node maintenance & node loss (Talos)

Owner-facing procedure for taking any one node down (planned) and for recovering from a node dying
(unplanned), written after the 2026-07-06 carve→GTT reboots took the strive platform down
(issue [#100](https://github.com/cchifor/ailab/issues/100)). The availability model behind this —
which workloads must survive a node loss (Tier A) and which singletons are *accepted* to blip
(Tier B) — is **ADR 0016**.

## Ground rules

- **All 3 nodes are control planes AND workers.** etcd quorum needs 2/3 → strictly **ONE node at a
  time**, and verify quorum before *and* after each node:
  `_out/talosctl-1112.exe -n 192.168.0.41 etcd status` → **3/3 in-sync**.
  Use `_out/talosctl-1112.exe` (v1.11.2) — the system `talosctl` is v1.6.2 and UNSAFE.
- **`qm shutdown` / ACPI does NOT stop Talos** (falls back to a hard stop). Graceful =
  `talosctl shutdown -n <cp-ip>` — it **drains first** (evicts pods, 5-min drain timeout), then powers
  off. Expect ~5 min, poll `qm status <vmid>` for `stopped`.
- kubectl: `--context admin@ai` or `KUBECONFIG=kubernetes/infra/_out/kubeconfig` (the default context
  is a DIFFERENT cluster).

| Proxmox host | mgmt IP | Talos CP | CP IP | CP vmid | AI LXC ctid |
|---|---|---|---|---|---|
| ai-node1 | 192.168.0.2 | talos-cp1 | 192.168.0.41 | 4001 | 5001 |
| ai-node2 | 192.168.0.3 | talos-cp2 | 192.168.0.42 | 4002 | 5002 |
| ai-node3 | 192.168.0.4 | talos-cp3 | 192.168.0.43 | 4003 | 5003 |

## Planned drain / host reboot

```bash
# 0. quorum green?                                   3/3 in-sync
_out/talosctl-1112.exe -n 192.168.0.41 etcd status

# 1. where do the CNPG primaries sit? If one is on the node you're taking down, you can (optionally)
#    switch it away first for a deterministic hand-off. The operator ALSO does this automatically
#    ahead of the drain — the manual step just makes the timing yours. (infra-pg in the databases ns,
#    strive-pg in strive-ailab.)
kubectl --context admin@ai get pods -A -l cnpg.io/instanceRole=primary -o wide
# NB: kubectl rejects global flags BEFORE a plugin name ("flags cannot be placed before plugin
# name") — for `cnpg` commands the --context goes AFTER:
kubectl cnpg status strive-pg -n strive-ailab --context admin@ai           # needs the cnpg kubectl plugin
kubectl cnpg promote strive-pg <replica-not-on-that-node> -n strive-ailab --context admin@ai

# 2. know your alerting blind spot: if alertmanager/ntfy live on this node, pushes pause during the
#    move — watch gatus (status.chifor.me) instead.
kubectl --context admin@ai get pods -n monitoring -o wide | grep -E 'alertmanager|ntfy'

# 3. graceful stop of that host's CP (drains ~5 min, then powers off; poll for stopped).
#    `qm` only exists ON the Proxmox host -> run the poll through node-ssh.py:
_out/talosctl-1112.exe shutdown -n <cp-ip>
python scripts/node-ssh.py <host-ip> "for i in \$(seq 1 90); do qm status <vmid> | grep -q stopped && break; sleep 5; done; qm status <vmid>"

# 4. host work (BIOS / kernel / Proxmox upgrade / hardware), then reboot the host.

# 5. boot race: the AI LXC often fails its first autostart (amdgpu device appears ~6 s after LXC start).
python scripts/node-ssh.py <host-ip> "pct start <ctid>"

# 6. gates before the next node: see the post-maintenance checklist below.
```

**Workloads-only variant** (node stays up, e.g. testing eviction behaviour):
`kubectl --context admin@ai drain <node> --ignore-daemonsets --delete-emptydir-data` … then
`kubectl --context admin@ai uncordon <node>`.

## Expected blips during a planned drain (Tier B accepted singletons — ADR 0016)

Tier A (cloudflared ×2, and after the #100 companion PRs: infra-pg, grafana ×2, authelia ×2,
strive-pg ×3) rides through a drain with **zero downtime**. Everything below is a deliberate
singleton; a drain moves it once:

| Workload | Kind / storage | Drain blip | Notes |
|---|---|---|---|
| prometheus | STS, qnap-iscsi RWO | ~1–3 min | metrics gap; rules re-evaluate on start |
| alertmanager | STS, **ephemeral** | ~1 min | **silences + dedup state LOST** — recreate silences |
| loki | STS, nfs-csi | ~1–2 min | alloy buffers & retries; no detach wall (NFS) |
| tempo | STS, qnap-iscsi RWO | ~1–3 min | trace ingest gap |
| grafana / authelia (until their HA PRs) | Deploy, qnap-iscsi RWO | ~1–3 min | SSO logins fail during the authelia move |
| gitea | Deploy, qnap-iscsi RWO | ~1–3 min | git push/pull fails briefly |
| vaultwarden | Deploy, qnap-iscsi RWO | ~1–3 min | clients have local cache |
| ntfy | Deploy, qnap-iscsi RWO | ~1–3 min | pushes delayed — Alertmanager webhook retries |
| open-webui | Deploy, nfs-csi RWX | ~1 min | |
| litellm / homepage / gatus / headlamp / oauth2-proxy | stateless | ~30–60 s | |
| trivy-server | STS, qnap-iscsi RWO | ~1–3 min | scans pause |
| valkey-master (strive) | STS, nfs-csi | ~1–2 min | airlock falls back to in-memory rate limiting |

## Unplanned node loss (host dies / hangs)

Default timeline for a hard loss, so you know what you're looking at:

1. **~40 s** — node goes `NotReady` (node-monitor-grace-period); NoExecute taints applied.
2. **+ tolerationSeconds** — Deployment pods get evicted: **60 s** for Tier-A stateless pods
   (cloudflared, later auth-valkey/authelia/grafana — set per ADR 0016), **300 s (default)** for
   everything else.
3. **RWO iSCSI volumes** — the new pod can't attach until the old attachment force-detaches:
   **~6 min** (`maxWaitForUnmountDuration`, hard-coded in the attach-detach controller — not
   configurable; this is why we don't tune controller-manager flags, see ADR 0016).
4. **StatefulSet pods stick in `Terminating` indefinitely** — the STS controller waits for confirmed
   kubelet deletion that never comes. Tolerations do NOT help here; the taint below is the mechanism.
5. CNPG clusters don't wait for any of this: a replica is **promoted in ~40 s–2 min** (no volume
   movement — each instance has its own PVCs).

**Decision matrix:**

- Node will be back shortly (reboot in progress) → **do nothing**; everything reconverges.
- Node is **CONFIRMED dead or powered off** (VM `stopped` in Proxmox / host offline) → apply the
  out-of-service taint. **Never while the node might still be running** — forcing detach against a
  live node that still has the LUN mounted is an iSCSI double-mount / ext4-corruption risk:

```bash
kubectl --context admin@ai taint nodes <node> node.kubernetes.io/out-of-service=nodeshutdown:NoExecute
```

Effect: immediate eviction (including stuck-`Terminating` STS pods) + immediate volume detach —
Tier-B RWO singletons recover in ~2–3 min instead of ~13. **Remove the taint once the node is healthy:**

```bash
kubectl --context admin@ai taint nodes <node> node.kubernetes.io/out-of-service=nodeshutdown:NoExecute-
```

## Post-maintenance checklist (gates before touching the next node)

```bash
_out/talosctl-1112.exe -n 192.168.0.41 etcd status                  # 3/3 in-sync
kubectl --context admin@ai get nodes                                # all Ready, none SchedulingDisabled
                                                                    # (Talos uncordons on boot; `kubectl uncordon` if stuck)
python scripts/node-ssh.py <host-ip> "pct status <ctid>"           # AI LXC running (pct start if not)
kubectl --context admin@ai get pods -A | grep -vE 'Running|Completed'   # nothing stuck
kubectl --context admin@ai get pdb -A                               # ALLOWED DISRUPTIONS back >0 (except *-primary: 0 by design)
kubectl --context admin@ai get cluster -A 2>/dev/null               # CNPG: "Cluster in healthy state"
```

- gatus green (status.chifor.me); Prometheus targets up; `ha` PrometheusRule alerts resolved.
- out-of-service taint removed (if used); Alertmanager silences cleaned up (if created).
- CNPG note: k8s never rebalances on its own — if a CNPG instance doubled up on a node during the
  window (preferred anti-affinity), delete that pod (**a replica, never the primary**) once the node
  is back; it reschedules onto the empty node.
