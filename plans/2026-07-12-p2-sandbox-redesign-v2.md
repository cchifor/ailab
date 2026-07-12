# P2 sandbox — implementation-ready design v2 (three-pod boundary + model gateway + mediated I/O)

Incorporates codex Phase B (`...-p2-review.md`) + the Phase A design review (`...-p2-sandbox-redesign.md`).
Supersedes the per-pod-DinD build. The boundary protects **platform credentials + the cluster/node**
from untrusted agent/test code. It is explicitly **not** a confidentiality boundary between repo
content and the chosen model provider (the agent must see the code to work on it), nor a DLP boundary
for secrets already in the repo.

## Trust topology — THREE isolated pods

1. **Orchestrator pod** (trusted): `agentforge serve`. Mounts ONLY the ESO-produced Secret with the
   forge PATs + git-push token + CP bearer (no OpenBao identity of its own). Creates/reaps sandbox
   **Jobs**, does the trusted-checkout export + redaction + all forge writes. Does NOT hold the
   inference OAuth and does NOT run the model gateway.
2. **Broker pod** (trusted-but-isolated, separate `Deployment` + `ClusterIP`): the **model gateway**.
   Mounts ONLY the `broker-oauth` Secret (the durable inference credential). No forge/CP/OpenBao
   access; egress only to cluster DNS + the exact model domains. The orchestrator cannot read the
   broker's Secret; the broker cannot read the orchestrator's.
3. **Sandbox Job** (untrusted, ephemeral, one per agent-run or test_cmd): `runtimeClassName: kata`,
   restricted-PSA-conformant, tokenless SA, no durable creds. Two admission/network profiles:
   - **agent** profile: egress to the broker ClusterIP + cluster DNS ONLY; mounts the job subPath +
     a writable home + a per-job broker capability (short-TTL token). Runs the agent CLI pointed at the
     broker.
   - **test/setup** profile: **NO egress at all** (no broker, no DNS); mounts the job subPath + writable
     home only. Runs `test_cmd`/`setup_cmd`.

## Sandbox execution (SandboxExecutor rewrite; DinD removed)
`SandboxExecutor.run(ExecSpec)`:
1. Allocate an unguessable `job_id`; stage `spec.cwd` **content only** into a per-job dir (subPath on
   the workspace RWX PVC — admission binds the pod to exactly that subPath; a per-job PVC is the
   stronger alternative if PVC-create RBAC is acceptable) with normalized modes and no `.git`.
2. Create a **k8s Job** (`backoffLimit: 0`, `activeDeadlineSeconds`, `ttlSecondsAfterFinished`, small
   `terminationGracePeriodSeconds`) with the profile (agent|test) for this ExecSpec's trust class. The
   command is `spec.argv`/shell; env is trust-class-limited (agent: broker base-URL + the per-job
   capability; test: none).
3. Wait on the Job/Pod status (deadline → the Job's own `activeDeadlineSeconds` + orchestrator delete).
   **Derive exit state from the k8s container status**, never a sandbox-written field.
4. **Quiesce** (Job terminated, no writer can mutate the dir), then **import** the content via the
   normative validator (below). Collect bounded stdout + a schema-validated result.
5. Delete the Job. An independent **reaper** (a controller or a periodic CP job keyed on signed expiry
   metadata) GC's leaked Jobs/Pods/dirs/PVCs after an orchestrator crash.

### Normative hostile-import validator (the RWX filesystem is an untrusted channel)
Import descriptor-relatively (`openat2` `RESOLVE_BENEATH|RESOLVE_NO_MAGICLINKS`, or an equivalent
no-follow build), never check-then-open. Accept only directories, regular files, and (if needed)
relative symlinks whose lexical target stays inside the tree — never dereferenced during import.
**Reject**: hardlinks (`nlink!=1`), absolute/escaping symlinks, devices/FIFOs/sockets, any `.git` path
component, NUL/`..`/absolute paths, normalization/case-collision duplicates, unexpected ownership,
xattrs/ACLs/file-caps, setuid/setgid/sticky. Enforce per-file, total, file-count, depth, path-length,
stdout, and result-size limits. Create destination files with normalized `0644`/`0755` — never preserve
hostile metadata. Parse the result JSON against an **exact bounded schema**; it may never supply an
argv/shell/path/URL/name/template/format without independent validation.

### Trusted commit/push (closes the .git escape)
Apply the imported content to a **fresh orchestrator-controlled checkout** (clean clone). Git runs with:
a clean `HOME`, `GIT_CONFIG_GLOBAL=/dev/null` + `GIT_CONFIG_SYSTEM=/dev/null` (no external helpers/
signing), `core.hooksPath` → a root-owned empty dir, and argv + explicit `-- <pathspec>` (never a
shell). `git status/commit/push` NEVER touches the untrusted dir.

## Model gateway broker (durable OAuth never in the sandbox)
A standalone application-layer gateway (NOT a generic HTTP/TCP proxy, NOT an orchestrator sidecar):
- Hard-codes upstream scheme/host/port + verified TLS (pinned CA); **reconstructs** each request from a
  validated allowlist of path/method/headers. Rejects CONNECT, absolute-form targets, redirects, proxy/
  hop-by-hop headers, conflicting Content-Length/Transfer-Encoding, unsupported upgrades, arbitrary
  upstream headers.
- **Per-job capability**: the orchestrator mints a short-TTL, one-job/one-model token (or mTLS
  identity); the broker accepts only a valid, unexpired, unreplayed capability. Cilium restricts the
  broker's ingress to sandbox pods carrying that job's identity.
- Bounds request/response size, model set, token budget, concurrency, rate, duration, and **spend** per
  job/account; fail-closed + readiness + capacity so it is not an unbounded SPOF. Audited.
- Preflight: verify Claude Code + Codex actually support the base-URL + streaming + auth behavior the
  gateway requires before implementation.

## Redaction (defense-in-depth ONLY)
Primary defense = platform secrets are never in sandbox input (broker holds the OAuth; orchestrator
holds forge creds; the sandbox gets neither). On every publish sink (PR/comment/commit/issue/event/log/
metric/error), scan for **exact known secret values + common encodings** and **block + quarantine**
(never silently mutate source); apply strict schemas + size caps. Scanner failure fails closed.
Documented non-goal: generic DLP / repo-content confidentiality vs the model.

## Admission / PSA / RBAC (operator-owned, CP-unwritable)
- Operator-created sandbox namespace(s) labeled `pod-security.kubernetes.io/enforce: restricted` (pinned
  version). **No `runtimeClass` PSA exemption** — the pod is unprivileged and conforms; runtimeClass
  selection isn't blocked by restricted PSA.
- `agentforge-sandbox-guard` **ValidatingAdmissionPolicy** (in the CP-unwritable ailab/operator repo,
  `failurePolicy: Fail`) binds to **every Pod CREATE/UPDATE in the sandbox ns** and pins: container/
  initContainer count + names, digest-pinned image (no image change on UPDATE), the trust-class command
  profile, resources, the full restricted securityContext, the tokenless SA, scheduler/tolerations/
  nodeSelector/DNS, exactly the allowed volumes/mounts/subPaths (job dir + writable home + broker-cap;
  **no secret/configMap/projected/CSI/hostPath volumes**, no ephemeral containers), and the exact Cilium
  identity labels. No other webhook may mutate the pod after validation.
- Orchestrator SA RBAC: `create/get/delete` Jobs+Pods **in the sandbox ns only** — and provably NOTHING
  else (no impersonation, controller-create, pod update/patch, `pods/exec|attach|portforward`, Service/
  EndpointSlice create, PVC mutate, TokenRequest, Secret get, admission mutate). Assert via
  SelfSubjectRulesReview + rejected-create tests.
- `tenant-guard.yaml`: add `external-secrets.io/v1` + `keda.sh/v1alpha1` GVKs WITH field pins
  (SecretStore provider/role→this ns+SA; ExternalSecret path/target; ScaledObject target/query keep
  org/workspace/account/pool labels; reject ClusterSecretStore/foreign ns/SA/path/scale-target/
  prometheus). `reconciler-rbac.yaml`: add those API groups (namespaced, scoped). Contract-test rendered
  objects vs installed CRD schemas + server-side dry-run.

## OpenBao tenant provisioning (isolated authority)
- Per workspace: a dedicated **`eso-auth` SA** (not the sandbox SA, not the orchestrator SA); the ESO
  controller gets narrowly-scoped `serviceaccounts/token` for exactly that SA (short TTL, audience).
  OpenBao role bound to that exact ns+SA+audience; policy read-only on `af/data/<org>/<workspace>/*`.
- The identity that writes OpenBao ACL policies + k8s-auth roles is OpenBao-admin-equivalent → it lives
  in a **network-isolated, audited provisioning controller** (NOT the CP/orchestrator), or an
  operator-run GitOps flow with short-lived admin, accepting only validated workspace ids → fixed
  policy/role templates. Idempotent; ordered before SecretStore reconciliation; with rotation, tenant
  deletion/revocation, orphan reconciliation, audit alerts.
- Endpoint: verified internal **TLS** (pinned CA); plain HTTP only if constrained to a trusted net + a
  recorded caveat. Split secrets: `orchestrator-creds` (forge/CP) → orchestrator only; `broker-oauth` →
  broker only.

## KEDA (fail-closed scaling)
`af-dispatcher` Deployment + Service + ServiceMonitor (`release: kube-prometheus-stack`) exporting
`forge_pending{role,repo,pool,account}`. ScaledObject query includes account/pool. **`maxReplicaCount:
1` is admission-enforced** (not a renderer default) until a downward-API per-pod claim id + the
epoch-safe account lease pass crash/late-release tests; only then lift >1.

## Phasing (each tranche codex-reviewed before the next)
- **R-1 boundary core**: SandboxExecutor→Job (DinD removed); restricted-PSA sandbox pod renderer +
  the sandbox-guard VAP (fail-closed) + orchestrator Job/Pod-only RBAC; per-job subPath I/O + writer
  quiescence + the normative import validator + the trusted-checkout/git-hardening + the reaper +
  resource/quota limits. Proves the credential + node boundary WITHOUT the broker (agent jobs run with
  no model access yet; only test/setup + import prove out).
- **R-2 creds/model**: the standalone broker gateway + per-job capability + secret split + the isolated
  OpenBao provisioner + `eso-auth` SA + ESO v1 + Cilium (broker ingress/egress).
- **R-3 redaction + scaling + egress hardening**: the `redact()`/quarantine gate; the af-dispatcher
  deploy/scrape + admission-enforced max=1; the sandbox agent/test Cilium profiles + FQDN-on-broker +
  the egress canary matrix.

## Verification — 6 boundary proofs (gate the `privilege_hardening: v1.1` flip)
1. **Admission**: CP P2 commit ACCEPTED by tenant-guard + sandbox-guard; a malformed sandbox pod
   (other SA/image/runtimeClass/volume/subPath/securityContext, an extra container, a controller/
   endpoint create, a PSA bypass) REJECTED; effective-RBAC assertions pass.
2. **Cred-exfil**: a canary agent + test_cmd cannot read any PAT/OAuth/OpenBao value or `/proc/1/
   environ`; the durable OAuth is provably absent from the sandbox; hostile result schemas + unsafe
   git-metadata/symlink/hardlink/device/FIFO/socket/setuid/xattr/Unicode-collision/oversized trees +
   a concurrent-mutation attempt are all rejected by the importer.
3. **Egress**: sandbox reaches ONLY the broker + DNS (agent) / nothing (test); direct-IP/IPv6/alt-DNS/
   metadata/service/node-local all denied; broker SSRF/smuggling/redirect/CONNECT/replayed-capability
   denied; a test-job is denied the broker.
4. **Live Kata**: `uname -r` shows the Kata guest kernel; a container escape stays in the microVM.
5. **Crash/reaper**: kill the orchestrator at every lifecycle stage → the deadline/reaper removes the
   Job/Pod/home/dir/PVC + broker authorization, without deleting another job.
6. **DoS/quota**: exhaust CPU/mem/PIDs/disk/output/model-budget/job-count → tenant-scoped limits + the
   broker fail-closed hold.

## Critical files
- agentforge: `adapters/exec/sandbox.py` (Job create + import), NEW `adapters/exec/import_validator.py` +
  `redact.py`; NEW broker service (own module/entrypoint) `adapters/broker/` + `agentforge broker` CLI;
  `app/handlers/roles.py` + `app/workspace.py` (trusted-checkout export, git hardening, remove untrusted
  git); `infra/settings.py`.
- agentforge-platform: `adapters/gitops/renderer.py` (sandbox Job/pod, broker Deployment+Service, ESO
  v1 + eso-auth SA, af-dispatcher, Cilium profiles), `settings.py`; the OpenBao provisioner integration
  (call-out, not in-CP authority).
- ailab: `admission/tenant-guard.yaml` (+GVKs/field pins), NEW `admission/sandbox-guard.yaml`,
  `agentforge-tenants-bootstrap/reconciler-rbac.yaml`, operator-owned `af-sandbox` ns (restricted PSA),
  the broker Deployment/Service + NetworkPolicy, the af-dispatcher Deployment/Service/ServiceMonitor,
  the reaper, and the isolated OpenBao-provisioner + `eso-auth` wiring + notes.

<!-- codex-review-status: pending -->
