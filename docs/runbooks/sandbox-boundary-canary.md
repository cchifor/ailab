# Runbook: sandbox-boundary CANARY (the `privilege_hardening: v1.1` gate proof)

Adversarial proof that the AgentForge v2 sandbox boundary (ADR 0018 §threat-model, ADR 0019
§Verification) holds against a REAL flipped-v1.1 sandbox. Tenant-zero already runs agents under
`privilege_hardening: v1.1` on real repos (`cchifor/ailab`, `cchifor/platform`), so the boundary must
be provably airtight. **A green suite is the standing gate; ANY breach is a P0** — stop, do not
flip/keep v1.1, and escalate with the exact reproduction.

The boundary is the POD, not the container: each agent / `test_cmd` run is its own ephemeral **Kata**
Job — tokenless SA, `automountServiceAccountToken:false`, restricted PSA, default-deny + per-trust-class
Cilium egress, no orchestrator creds. (The old per-pod privileged-DinD sidecar design was **superseded**;
there is no DinD and no capability to nest one.)

## What proves what

| Threat vector (ADR 0018/0019) | Assertion | Where |
|---|---|---|
| Read the orchestrator bot PATs / HMAC / git-push token | no cred-shaped env or volume in either tier; agent tier carries only the broker route + capability **file path**, never a bearer | unit `test_sandbox_boundary.py` + live (pod introspection) |
| Read the inference OAuth from the `test_cmd` tier | test tier carries NOTHING — no broker wiring, no capability, no creds | unit + live (test canary) |
| Read OpenBao (SA token / network path) | tokenless SA with **no RoleBinding**; agent egress allowlist excludes the `openbao` ns and resolves no OpenBao FQDN; both canaries fail to reach `openbao.openbao.svc:8200` | unit + live (both canaries) |
| Read orchestrator `/proc/1/environ` | separate PID namespace — the sandbox pid-1 environ carries no orchestrator credential marker | live (agent canary) |
| Egress from `test_cmd` (`--network none`) | **zero egress** — even the broker is unreachable | live (test canary) |
| Agent → non-allowlisted host denied, allowlisted (broker) succeeds | **exact matrix**: world/metadata/alt-DNS/other-pool-broker/OpenBao DENIED, a broker that does not admit the identity UNREACHABLE by pod IP, **and** the admitting broker (by pod IP) REACHABLE (positive control) | live (agent canary) |
| `uname -r` = Kata **guest** kernel (microVM) | guest kernel differs from the host node's kernel | live (agent canary) |
| Privileged DinD escapes the microVM | unconstructible — no `privileged`, no added caps, masked `/proc`, read-only rootfs; admission REJECTS a non-kata runtimeClass / privileged / hostPath / hostNetwork / automount-token | unit + live negative-admission |

The **positive broker control** is what stops a false pass: a totally netless pod would also "block"
every denied target, so the agent canary must additionally PROVE it can reach its own pool broker
(any HTTP response = reachable). A Kata pod can't use the broker ClusterIP, so the harness injects a
ready broker **pod IP** (`AF_CANARY_BROKER_IP`).

## Run it (live, activation-time)

```bash
# from an ailab checkout, with kube context admin@ai merged (see docs/runbooks/00-access-prereqs.md)
scripts/verify-sandbox-boundary.sh                 # full matrix against tenant-zero/boundary-canary
KUBECTL_CONTEXT=admin@ai scripts/verify-sandbox-boundary.sh
AF_CANARY_KEEP=1 scripts/verify-sandbox-boundary.sh   # leave the canary Jobs for inspection
AF_CANARY_PREFLIGHT_ONLY=1 scripts/verify-sandbox-boundary.sh   # resolve image + brokers, print, stop
```

- **Disposable + safe:** runs ONLY on the canary's own identity `tenant-zero/boundary-canary` (pool
  `canary`), with a fresh random 32-char job-id subPath on its own PVC `af-sbx-ws-tenant-zero-boundary-canary`
  (agentforge-sandbox/boundary-canary-workspace-pvc.yaml). No tenant namespace, orchestrator, seat or
  capability exists for that identity. Every other workspace is **refused, with no override** (the old
  `AF_CANARY_DANGEROUS_OVERRIDE` is gone): platform-dev's workspace is live, and the retired playground's
  PV/PVC objects and its NFS subdir are deleted (#1505). Both canary Jobs are torn down on exit.
- **Brokers.** The positive control is a Ready pod of `AF_CANARY_BROKER` (default `broker-openai-codex`), the
  only broker whose CNP admits the canary identity (rule 1c). Its login is revoked and the canary holds no
  capability, so that rule grants reachability, never authorization. The **denied-broker probe** targets a
  Ready pod of `AF_CANARY_DENY_BROKER` (default `broker-anthropic-max1`, which does not admit the identity)
  by pod IP. It proves broker ingress admits only the matching identity, not just that a DNS name is
  refused, and the run is inconclusive if that pod changes or goes unready during the probe.
- **Mutation check.** `AF_CANARY_DENY_BROKER=broker-openai-codex` must FAIL: the denied target is then the
  admitting broker's OTHER replica, which the canary can reach. This proves the denied-broker probe and its
  assertion can see a reachable target.
- **Image.** The sandbox image is `AF_CANARY_IMAGE`, else the `AF_SANDBOX_IMAGE` that every live
  orchestrator (`af-tenant-*`) runs, else the committed canary's digest, and the run prints which. A failed
  lookup, disagreeing orchestrators or a non-digest value stop the run, and so does a digest the registry no
  longer has (the manifest must answer HTTP 200). On 2026-10-31 that was platform-dev's own sandbox digest:
  its CP-rendered pool still points at a GC'd image (#1519). Until it is re-rendered, run with
  `AF_CANARY_IMAGE=registry.chifor.me/agentforge/sandbox@sha256:aa49b808a6289157eb61a2a8ab95edfd536aeea235d70279c1207094040e43b0`
  (the ailab-pinned digest; GREEN 24/24 on 2026-10-31).
- **Retention.** Each run leaves two empty subPath dirs in the canary's NFS subdir. The parent-export mkdir
  Job (`platform-dev-nfs-provisioner-job.yaml`, re-run about hourly) `rmdir`s those older than 2 hours. Until
  2026-10-31 the canary ran on the retired playground's labels and storage, and the reaper collected them
  (#1505, plans/2026-10-31-playground-leftovers-plan.md).
- The two Kata canaries run **sequentially** (one microVM at a time — two concurrent boots on the
  small agent nodes was flaky). Expect ~1–3 min total (Kata boot + 5 s-timeout probes).
- The gated canary manifests are `kubernetes/apps/infrastructure/agentforge-sandbox/{egress-canary,
  test-egress-canary}.yaml` — **deliberately UNLISTED** in the kustomization (Flux never applies
  them); the harness renders its own copy inline so it can inject the job-id / broker IP / image.

The pytest live tier (same matrix, from the agentforge repo) is:

```bash
AF_BOUNDARY_LIVE=1 AF_BOUNDARY_KUBECTL_CONTEXT=admin@ai \
  uv run pytest tests/integration/test_sandbox_boundary.py -m integration -q
```

## Live matrix result (baseline)

`2026-07-24`, `admin@ai`, `tenant-zero/playground` — **23/23 PASS (GREEN)**:

- static boundary (kata RuntimeClass, `sandbox-agent-egress` + `sandbox-test-zero-egress` +
  `default-deny-all`, separate restricted-PSA ns, tokenless SA with no RoleBinding) — PASS
- negative admission — privileged / automount-token / hostNetwork / **non-kata runtimeClass** /
  hostPath all REJECTED — PASS
- AGENT egress — world / metadata / alt-DNS / other-pool-broker / **OpenBao** all blocked; **pool
  broker REACHABLE** (positive control); `/proc/1` clean; BOUNDARY OK — PASS
- KATA — the pod ran `runtimeClassName=kata` (k8s-side) AND guest kernel `6.12.42` **!=** host
  `6.12.48-talos` (microVM isolation in effect) — PASS
- TEST egress — **zero egress**, even the broker unreachable (`test_cmd --network none`) — PASS

## On a BREACH (P0)

A `FAIL` line or a non-zero exit means a vector was reachable OR the run was inconclusive (e.g. the
agent could not reach its broker → a possibly netless/misconfigured path that must not be read as a
pass). This is a **P0**:

1. STOP. Do not flip or keep `privilege_hardening: v1.1` for any real-repo tenant.
2. Re-run with `AF_CANARY_KEEP=1` and capture the canary pod logs + `kubectl -n agentforge-sandbox
   describe` for the failing tier.
3. Escalate with the exact reproduction (which vector, the log, the policy/VAP object involved).
4. Common causes: a drifted `cilium-egress.yaml` (broker allow / DNS matchNames), a relaxed
   `agentforge-sandbox-{guard,job-guard}` VAP, a non-kata scheduling fallback (nested-virt / `/dev/kvm`
   missing on an agent node → Kata fails; DinD/sandbox must fail CLOSED onto Kata, never runc/gvisor).
