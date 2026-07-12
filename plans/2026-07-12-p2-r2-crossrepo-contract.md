# P2 R-2 cross-repo coordination contract (revises merged R-1)

One shared contract for the coordinated change across agentforge (off `main`), agentforge-platform (off
`main`), and ailab (branch `feat/p2-unlock`). All three MUST agree byte-for-byte on the sandbox pod shape,
the NFS identity, and the OpenBao paths. Design authority: `plans/2026-07-12-p2-r2-broker-design.md`
(§2 delivery, §6 credential split, §7 provisioner) + `plans/2026-07-12-p2-r2-round2-fixspec.md` history.

## A. Capability delivery — replace the un-populatable broker-cap emptyDir with a workspace file
The merged agent profile mounts a read-only `broker-cap` emptyDir at `/var/run/af/broker` that NOTHING can
populate (no init container / projected volume is admitted). Replace it:
- **agentforge** (`src/agentforge/adapters/exec/sandbox.py`):
  - **Mint** the per-job capability in the orchestrator run path for the AGENT profile BEFORE creating the
    Job: use `agentforge.broker.capability.mint(private_key_pem, kid, CapabilityClaims(...), ttl_s)` with
    claims from the TYPED ExecSpec (engine/provider/account→`aud`, tenant/workspace/job_id/pool, model→
    `model_set`, budget→`token_budget`, allowed routes/methods, ttl bounded by the Job deadline). The
    orchestrator's Ed25519 PRIVATE signing key + `kid` + the per-(provider,account) broker audience/URL
    come from config (new settings: `AF_CAPABILITY_SIGNING_KEY` [PEM, from orchestrator-creds via ESO],
    `AF_CAPABILITY_KID`, and a per-account broker map — or reuse the existing `AF_BROKER_URL`). A missing
    signing key on the agent path is a fail-fast error (never an unsigned/absent capability).
  - **Deliver** it: write the signed JWT to `<job_dir>/.af/broker-cap.jwt` (same reserved `.af/` mechanism
    as `.af/stdin`: O_NOFOLLOW+O_EXCL+0600, import-skipped, cleaned on pre-Job failure). Set
    `AF_BROKER_CAPABILITY_FILE=/workspace/.af/broker-cap.jwt` (NOT the old `/var/run/af/broker/token`).
  - **DROP** the `broker-cap` emptyDir volume + mount from `build_job_manifest` (and `BROKER_CAP_MOUNT` /
    `broker_cap_size_limit`). The agent profile's volumes become EXACTLY `{workspace, home}`.
- Result: agent and test profiles share the SAME 2-volume shape `{workspace, home}`.

## B. VAP 2-volume shape (drop broker-cap) — must land ATOMICALLY with A
- **ailab** (`kubernetes/apps/infrastructure/agentforge-sandbox/sandbox-guard.yaml` + `sandbox-job-guard.yaml`):
  the per-trust-class volume/mount rule now requires EXACTLY `{workspace PVC, home emptyDir}` for BOTH
  `test` AND `agent` (2 volumes, 2 mounts) — remove the `broker-cap` branch and the agent=3 case entirely.
  Keep the home sizeLimit + `/home/nonroot` mount + workspace subPath==job-id pins. Both VAPs mirror.
- The agentforge `build_job_manifest` (A) and these VAPs MUST match: an agent Job with `{workspace, home}`
  must be ADMITTED; one still carrying `broker-cap` must be REJECTED.

## C. Org-qualified NFS identity (multi-tenant isolation) — all three repos
Key sandbox storage on **org+workspace**, not workspace slug alone, with per-org/workspace export dirs:
- **Names:** workspace PVC `af-sbx-ws-<org>-<ws>`, staging PVC `af-sbx-stage-<org>-<ws>`, PVs likewise.
- **platform** (`renderer.py`): `sandbox_workspace_pvc`/`sandbox_staging_pvc`/`sandbox_staging_pv` →
  `af-sbx-ws-<org>-<ws>` / `af-sbx-stage-<org>-<ws>` (org from `spec.org_slug`). `AF_SANDBOX_WORKSPACE_PVC`
  env follows. Update the allowlist/assert + tests.
- **ailab** (`workspace-pvc.yaml`, `staging-pv.yaml`, `reaper-storage.yaml`): static PV+PVC names org-
  qualified for the tenant-zero org (`tenant-zero`/`playground` → `af-sbx-ws-tenant-zero-playground`); each
  PV's `nfs.path` points at a **per-org/workspace subdir** `/pve-nfs/agentforge-sandbox/<org>/<ws>` (so one
  tenant orchestrator's PV cannot reach another org's tree). Note the export-dir provisioning as an
  operator/flip prerequisite.
- **ailab VAPs:** the workspace-PVC-name pin + the subPath pin key on **org+workspace** (the pod carries an
  `agentforge.io/org` label OR the workspace label encodes org — pick one, pin it; the claimName must equal
  `af-sbx-ws-<org>-<ws>` derived from the pod's org+workspace labels).
- **agentforge:** `AF_SANDBOX_WORKSPACE_PVC` is consumed as-is from the rendered env (already), and the pod
  must carry the `agentforge.io/org` label if the VAP keys on it — add it to `build_job_manifest`'s labels.

## D. Reserved OpenBao path prefixes (closes the wildcard-overlap) — platform (+ provisioner in Wave B)
- **platform** (`renderer.py` `openbao_orchestrator_key` + the SecretStore/ExternalSecret): tenant data
  moves under `tenants/<org>/<workspace>/orchestrator` (full vault path `af/data/tenants/<org>/<ws>/*`);
  the lease/broker paths (operator) live under `operator/...`. RESERVE `tenants` and `operator` (and every
  operator top-level slug) as FORBIDDEN `org_slug` values — reject at PoolSpec construction / render. Update
  the tenant SecretStore/ExternalSecret key + the assert + tests.

## Ownership / branches / gates
- agentforge (A + the org label + the AF_BROKER_CAPABILITY_FILE change) → NEW branch off `main`; keep
  LocalExecutor + all tests green; `uv run pytest` + ruff + mypy; the broker-app tests unaffected.
- platform (C + D) → NEW branch off `main`; the cross-repo Settings test still builds real agentforge
  Settings; pytest (PG up) + ruff + mypy.
- ailab (B + C VAP/PV) → `feat/p2-unlock`; `kubectl kustomize` clean + (if cluster reachable) both VAPs
  server-dry-run.

## Cross-repo consistency (the verify gate)
The Job that agentforge `build_job_manifest` produces (agent profile: `{workspace, home}`, claimName
`af-sbx-ws-<org>-<ws>`, org label, `.af/broker-cap.jwt` delivery, `AF_BROKER_CAPABILITY_FILE=/workspace/
.af/broker-cap.jwt`, NO broker-cap volume) must satisfy BOTH updated ailab VAPs byte-for-byte; the PVC name
agentforge mounts == what platform renders == the ailab static PV; the OpenBao orchestrator key platform
renders is under `tenants/`. A hostile agent still cannot read the OAuth (broker holds it) — the capability
file is short-TTL/single-job and import-skipped.
