# P2 R-1 round-2 fix contract (closes codex round-2 findings)

Single shared contract across the three repos. codex round-2 verdict: F1+F5 OPEN (blocker),
F2/F3/F4 PARTIALLY-CLOSED, one NEW important. All accepted (no pushback — real defects).

## Cross-cutting decision — unify the NFS identity on uid/gid 65532 (`nonroot`)
Root cause of F1b: the orchestrator stages as uid 1000, the sandbox runs as 65532, and the importer
now rejects entries not owned by 65532. A `0777` export *root* does not change the owner/mode of files
created under it (AUTH_SYS NFS creates them owned by the writer, 0755/0644). Resolution: **orchestrator,
sandbox, and reaper ALL run as uid/gid 65532** so every staged/produced/reaped entry is 65532-owned and
mutually writable, and the importer's `expected_uid=65532` is correct AND enforceable (the sandbox runs
65532 and cannot chown). This replaces any "chmod 0777 every file" hack.

## Image-gating reality (bounds what "closed" can mean for F5a)
`registry.chifor.me/agentforge/{p1-worker,sandbox}` are `@sha256:REPLACE_ME` placeholders — the images
are NOT built, the registry has no `agentforge/*` repo, and `POST /provision` 503s on an unpinned digest.
Therefore the reaper Deployment CANNOT be un-gated in this tranche (no pullable image), and the live
`env→Settings→Job→VAP→quiesce/import/reaper` integration proof is inherently post-image-build. R-1's
deliverable is the **correct, reviewed boundary core (code + manifests + unit/policy tests)**; enabling
the reaper and running the integration proof are **explicit v1.1-flip prerequisites** (tracked), same
class as the 6 boundary proofs. Make the code provably race-free so that when the image lands, enabling
the reaper is a digest-pin + kustomization un-comment, nothing more.

---

## F1a — provision the tenant-ns lease Secret (blocker) — agentforge-platform (+ ailab doc)
The orchestrator references `af-sbx-lease` (secretKeyRef) but no manifest provisions it in the tenant ns;
Secret refs are namespace-local so the pod would fail to start. Switch to a single OpenBao source via ESO
(retire the SOPS-copy stub):
- **renderer**: render a tenant-ns `ExternalSecret` named `af-sbx-lease` (target Secret `af-sbx-lease`,
  key `AF_LEASE_HMAC_KEY`, creationPolicy Owner) from OpenBao **`af/sandbox/lease-hmac`** (a shared,
  cluster-constant path — one reaper verifies all tenants' leases, so the HMAC is shared; orchestrators
  are trusted, so sharing is sound), via the EXISTING per-tenant `eso_sa` SecretStore. The per-tenant
  OpenBao role must also get read on `af/sandbox/lease-hmac` (note in the tenant role policy).
- **allowlist**: add `af-sbx-lease` (ExternalSecret) to the tenant manifest allowlist / `assert_allowlisted`.
- **cross-repo test**: stop simulating `valueFrom` away — assert the render actually EMITS the
  `af-sbx-lease` ExternalSecret and that the orchestrator's `secretKeyRef.name` matches it; still build a
  real `Settings` with `AF_LEASE_HMAC_KEY` supplied from a fixture value.
- **ailab (doc + gated wiring)**: convert `reaper-lease.sops.yaml` → a reaper `ExternalSecret` from the
  SAME `af/sandbox/lease-hmac` (+ a minimal `agentforge`-ns SecretStore), kept COMMENTED alongside
  `reaper-deployment.yaml` (gated on the image). Document `af/sandbox/lease-hmac` as the single
  authoritative source and add "seed OpenBao `af/sandbox/lease-hmac`" to the v1.1-flip prerequisites.

## F1b — unify uid/gid 65532 across NFS consumers (blocker) — all three repos
- **agentforge** `deploy/orchestrator.Dockerfile` (and any worker Dockerfile): `USER 65532:65532`; chown
  `/app`, the state_dir, and the jobs_root to 65532 so the app runs writable as nonroot.
- **agentforge-platform renderer**: set the orchestrator Deployment pod `securityContext`
  `runAsUser/runAsGroup/fsGroup: 65532` (and `runAsNonRoot: true`) in the P2 render path.
- **ailab** `reaper-deployment.yaml`: `runAsUser/runAsGroup/fsGroup: 65532` (was 1000) so the reaper can
  rmtree 65532-owned job dirs; update the `# the p1-worker image's USER` comment.
- Keep the importer `expected_uid=65532` (already rendered). Add/adjust a `stage()` test asserting staged
  entries are created under the unified uid on a real tmp tree.

## F2 — require the home emptyDir exactly (important) — ailab both VAPs
Both `sandbox-guard.yaml` and `sandbox-job-guard.yaml` currently accept `size(volumes) >= 1` and only
require `workspace`. Tighten: require EXACTLY the `workspace` PVC + the `home` `emptyDir` (with its
`sizeLimit`) + the agent-only broker-cap `emptyDir` per the pod-shape contract — enforce the exact volume
set and the matching `/home/nonroot` mount (keep the existing sizeLimit checks on the now-mandatory home).

## F3 — list+watch quiescence proof (important) — agentforge
Replace the `_quiesce()` polling seam with list-plus-watch on both the Job and the labeled Pods:
- add a watch to `kube.py` (`KubernetesJobClient`) with resourceVersion handling;
- `_quiesce()` foreground-deletes the Job, then watches until the Job object is gone AND no Pod with
  `agentforge.io/job-id=<id>` remains; on watch EOF / 410 / RV-loss, restart from a fresh list+RV and
  treat every inconclusive closure as failure (raise `QuiescenceTimeoutError`), never success;
- unit tests for: clean quiescence, 410/RV-restart, and timeout-raises.

## F4 — full st_mode drift check (important) — agentforge
`_verify_unchanged()` compares inode/device/type/nlink/owner but NOT the full `st_mode`. Add a
`st_mode` comparison (mask the permission + type bits) between the pre-open lstat and the post-open
fstat; reject any drift. Add a mode-drift regression test alongside the inode/type/nlink/owner tests.

## F5b — renew the lease through the ENTIRE run→quiesce→import→apply interval (blocker) — agentforge
Today the lease is renewed only inside `_await_terminal` (while polling the running Job); after
quiescence `run()` writes it once and does a synchronous import over a `hard` NFS mount that can block
past the 900s TTL. Fix: run a background renewal daemon (thread/task) that atomically renews the lease on
a period `< TTL` for the WHOLE operation (run + quiesce + import + result-parse + apply-back), stopped
only in `finally` after the work (or failure) completes. Test: a slow import does not let the lease lapse.

## F5c — atomic reclaim + importer aborts on lost claim (blocker) — agentforge
`Reaper.reclaim()` deletes the Job then rmtrees without atomically claiming ownership or rechecking.
Fix: reclaim must (1) atomically CLAIM the lease (e.g. `os.replace` a reaper-owned marker / CAS on an
owner+epoch field) BEFORE acting, (2) recheck Job `.status` AND no labeled Pod exists (foreground delete
+ wait), (3) only THEN rmtree. The orchestrator's importer must detect a lost claim (its lease
marker/epoch was replaced) and ABORT before apply-back. Tests: reaper-vs-live-importer race → importer
aborts, reaper does not rmtree a still-claimed dir.

## NEW — lease file must actually be removed on success (important) — agentforge
`RealSandboxFS.remove()` calls `shutil.rmtree(path, ignore_errors=True)`, which silently no-ops on the
regular `.leases/<job_id>` file, so leases accumulate forever. Fix: `remove()` must distinguish a regular
file (`os.unlink`, atomic) from a directory (`rmtree`), or add a dedicated lease-unlink. Add a real-FS
success-path test asserting `.leases/<job_id>` is gone after a successful run.

## Verification (round 3)
Every repo's gate green (pytest+ruff+mypy for the python repos; `kubectl kustomize` for ailab); the
cross-repo env→Settings→VAP test asserts the lease ExternalSecret is really rendered; unit/policy tests
cover the watch, the st_mode drift, the through-import renewal, the atomic reclaim/abort, and the lease
unlink. Reaper-enable + live NFS integration proof are recorded as v1.1-flip prerequisites (image-gated).
