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
| Agent → non-allowlisted host denied, allowlisted (broker) succeeds | **exact matrix**: world/metadata/alt-DNS/other-pool-broker/OpenBao DENIED **and** the pool broker (by pod IP) REACHABLE (positive control) | live (agent canary) |
| `uname -r` = Kata **guest** kernel (microVM) | guest kernel differs from the host node's kernel | live (agent canary) |
| Privileged DinD escapes the microVM | unconstructible — no `privileged`, no added caps, masked `/proc`, read-only rootfs; admission REJECTS a non-kata runtimeClass / privileged / hostPath / hostNetwork / automount-token | unit + live negative-admission |

The **positive broker control** is what stops a false pass: a totally netless pod would also "block"
every denied target, so the agent canary must additionally PROVE it can reach its own pool broker
(any HTTP response = reachable). A Kata pod can't use the broker ClusterIP, so the harness injects a
ready broker **pod IP** (`AF_CANARY_BROKER_IP`).

## Run it (live, activation-time)

```bash
# from an ailab checkout, with kube context admin@ai merged (see docs/runbooks/00-access-prereqs.md)
scripts/verify-sandbox-boundary.sh                 # full matrix against tenant-zero/playground
KUBECTL_CONTEXT=admin@ai scripts/verify-sandbox-boundary.sh
AF_CANARY_KEEP=1 scripts/verify-sandbox-boundary.sh   # leave the canary Jobs for inspection
```

- **Disposable + safe:** runs ONLY against the throwaway `tenant-zero/playground` workspace (a fresh
  random 32-char job-id subPath) and **HARD-REFUSES `platform-dev`** (the real-repo tenant). Both
  canary Jobs are torn down on exit.
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
