# P2 sandbox redesign — the real boundary (separate ephemeral Kata pod + broker + redaction)

## Context

Codex Phase B (`plans/2026-07-11-agentforge-v2-p2-review.md`) rejected the built P2 sandbox: the
per-pod DinD sidecar is **not a boundary** — untrusted agent/test code runs in a container inside a
DinD daemon that shares the **same Kata pod as the credentialed orchestrator**, so a container/guest
escape lands beside the forge PATs + push token. Kata only isolates the pod from the *node*. This
contradicts the plan's **governing principle** (post-Phase-A-R1): "untrusted agent/test code runs in a
container that mounts ZERO high-value secrets, in a **separate** PID/mount namespace from the
orchestrator, inside a Kata microVM, behind default-deny egress." The parallel build followed the
plan's stale "Executor port" paragraph (per-pod DinD) instead of the governing principle. This redesign
implements the governing principle and fixes all 7 blockers.

## Approach — two-pod boundary

**Trusted orchestrator pod** (`agentforge serve`): holds forge PATs, git-push token, OpenBao SA, the
**credential broker**, and does ALL forge writes. Runs NO untrusted code. Creates + reaps ephemeral
sandbox pods via the k8s API (scoped RBAC, admission-pinned).

**Ephemeral sandbox pod** (per untrusted job — one agent-CLI run or one `test_cmd`): `runtimeClassName:
kata`; `automountServiceAccountToken: false` + a **tokenless SA** with no Secret RBAC; **no** forge/
OpenBao/CP egress (model-only Cilium policy + a DNS proxy for FQDN); read-only rootfs + a writable
per-pod home + the job I/O volume; **no durable credentials**. Admission pins the exact shape. It runs
one command and is deleted.

### Job I/O — no shared trusted git
- A per-workspace **RWX PVC** (qnap-nfs `nfs-csi`, RWX) is the ONLY thing the orchestrator and the
  sandbox pod share. The orchestrator writes a **content-only export** (the repo tree it wants the
  agent to work on, plus task inputs) to a per-job subdir — **never** a `.git` the orchestrator will
  later trust.
- The sandbox pod runs the agent/test against that subdir and writes its output (modified tree, stdout,
  a structured result) back to the same subdir.
- The orchestrator reads ONLY the content back, computes the diff by applying it to a **fresh trusted
  checkout** it controls (its own clone with a clean `.git`), validates paths/symlinks/ownership/size,
  **redacts** (below), and commits + pushes from the trusted checkout. `git status/commit/push` NEVER
  runs in the shared/untrusted dir → closes blocker #1.

### Executor port change
`SandboxExecutor.run(ExecSpec)` becomes: (1) stage `spec.cwd` content into the job PVC subdir; (2)
create a sandbox Pod (kata, tokenless, model-only, digest-pinned sandbox image) whose command is the
`spec.argv`/shell, env limited by trust class, home writable, the job subdir mounted; (3) wait
(timeout → delete pod); (4) collect stdout + the result file; (5) delete the pod. `LocalExecutor`
stays the default. The DinD sidecar is **removed** entirely.

### Credential broker (durable creds never enter the sandbox) — blocker #3
- A **model-proxy broker** runs in the orchestrator pod, listening on a Unix socket (or localhost) that
  is bind-mounted into the sandbox pod. It authenticates upstream (Anthropic/OpenAI/litellm) with the
  durable OAuth **on the orchestrator side** and forwards the agent's requests. The agent CLI is
  configured (`ANTHROPIC_BASE_URL`/`OPENAI_BASE_URL` → the broker; a dummy/short-lived token) so the
  **durable OAuth is never present in the sandbox pod** — the agent can't copy it into the diff/output.
- The broker also enforces model-only routing (rejects non-model destinations) and is the single
  outbound path for the sandbox (its Cilium policy allows egress only to the broker + DNS).
- The forge (issue/PR) creds stay entirely in the orchestrator; the sandbox never touches the forge.

### Outbound redaction — blocker #3
Before ANY forge publication (PR body, review comment, commit message, issue comment, event, log line),
the orchestrator runs a redactor over the text: strips anything matching known secret shapes (the
inference OAuth, PATs, bearer tokens, `sk-`/`ghp_`/`bao`-style keys, high-entropy blobs). A single
`redact()` gate on the forge-publish path.

### Egress — blocker #4
- P2 **removes** the broad P1 K8s NetworkPolicy on the worker; the orchestrator pod gets a Cilium
  policy: egress to forge, OpenBao, CP, litellm, model — as needed.
- The **sandbox pod** gets a Cilium policy: egress ONLY to the broker socket's node-local path (or a
  ClusterIP if socket isn't feasible) + DNS proxy; default-deny everything else. The container also
  runs `--network`-equivalent isolation via the pod (no docker anymore).

### Admission / PSA / RBAC — blockers #5, #6
- A **trusted, operator-created sandbox namespace** `af-sandbox` (or per-tenant `af-sbx-<org>-<ws>`)
  labeled `pod-security.kubernetes.io/enforce: privileged`-exempt-via-kata: add
  `PodSecurityConfiguration.exemptions.runtimeClasses: ["kata"]` to the API-server admission config
  (documented operator step) so a kata pod isn't blocked by baseline PSA. (No privileged container
  remains anyway — the sandbox pod is unprivileged; the DinD-privileged problem disappears.)
- A **P2 ValidatingAdmissionPolicy** `agentforge-sandbox-guard` pins the sandbox pod shape for the
  orchestrator SA's pod-creates: `runtimeClassName == kata`, `automountServiceAccountToken == false`,
  the SA is the tokenless sandbox SA, image is the pinned sandbox digest, no host* / hostPath /
  privileged / nodeName, only the allowed volumes (job PVC + broker socket + writable home), and the
  model-only network label. Reject anything else.
- Orchestrator SA RBAC: `create/get/delete pods` **in the sandbox namespace only**, admission-pinned.
- `tenant-guard.yaml`: add `external-secrets.io/v1` (SecretStore/ExternalSecret) + `keda.sh/v1alpha1`
  (ScaledObject) to the GVK allowlist WITH field validations (SecretStore provider/role pinned to this
  ns+SA; ExternalSecret path/target pinned; ScaledObject target/query pinned). `reconciler-rbac.yaml`:
  add the ESO/KEDA API groups (namespaced, scoped verbs).
- Renderer: emit **`external-secrets.io/v1`** (ESO 2.7.0 serves `v1`, not `v1beta1`) — blocker #6.

### OpenBao tenant provisioning — blocker #7
- A **trusted provisioning path** (a CP job or an operator-run step invoked at workspace-provision
  time) creates, per workspace: an OpenBao **role** bound to exactly `af-sbx-<org>-<ws>` namespace +
  the tenant SA, and a **policy** granting read on `af/data/<org>/<workspace>/*` only. The SecretStore
  references the **tenant SA** (not the ESO controller SA).
- OpenBao endpoint: use the actual internal **HTTP** endpoint (or configure verified TLS + CA); the
  renderer's `https://` with no TLS listener is wrong — fix the address/scheme.
- ESO ExternalSecret still syncs ONLY the creds the ORCHESTRATOR needs (forge PATs, CP bearer) into the
  orchestrator's Secret. The inference OAuth goes to the BROKER's secret, not the sandbox. Split the
  secrets (blocker #3): orchestrator-creds vs broker-oauth, in separate OpenBao paths + ExternalSecrets.

### KEDA — blocker #8-scaling
- Render an **always-on `af-dispatcher` Deployment + Service + Prometheus scrape** (ServiceMonitor with
  `release: kube-prometheus-stack`) exporting `forge_pending{role,repo,pool}`; the ScaledObject query
  includes pool/account labels. Until a **downward-API per-pod claim id + the epoch-safe account lease**
  are implemented, keep `maxReplicaCount == 1` (the P1 stable-identity cap) — lifting to >1 is a
  tracked follow-on within P2.

### Inner home — blocker #8
The sandbox pod gets its OWN writable home (emptyDir) containing only broker config (base URL + socket)
— never the orchestrator home or durable creds.

## Phasing (this redesign)
- **R-1 boundary core**: SandboxExecutor → separate-pod (remove DinD); the sandbox pod renderer
  (tokenless, kata, model-only, admission-pinned); orchestrator pod-create RBAC + the sandbox VAP; PSA
  kata exemption; the RWX job I/O + trusted-checkout export. **This alone makes it a boundary.**
- **R-2 creds**: the model-proxy broker + secret split + OpenBao per-tenant role/policy provisioning +
  ESO v1 + SecretStore tenant-SA fix.
- **R-3 redaction + egress**: the `redact()` forge-publish gate + the distinct orchestrator/sandbox
  Cilium policies + tenant-guard/reconciler-rbac ESO/KEDA + the af-dispatcher deploy/scrape.
- Then the sandbox-boundary CANARY tests (malicious repo/prompt tries to read PATs/OAuth/OpenBao/
  /proc/1/environ + egress the internet — all must fail) gate the `privilege_hardening: v1.1` flip.

## Critical files
- agentforge: `adapters/exec/sandbox.py` (rewrite to pod-create), NEW `adapters/exec/broker.py` +
  `redact.py`, `app/handlers/roles.py` + `app/workspace.py` (trusted-checkout export, remove untrusted
  git), `infra/settings.py`.
- agentforge-platform: `adapters/gitops/renderer.py` (sandbox pod spec, ESO v1, af-dispatcher, Cilium
  split, broker sidecar), `settings.py`, the OpenBao provisioning path.
- ailab: `admission/tenant-guard.yaml` (+ESO v1/KEDA GVKs + field pins), NEW
  `admission/sandbox-guard.yaml`, `agentforge-tenants-bootstrap/reconciler-rbac.yaml`, the `af-sandbox`
  namespace + PSA kata exemption doc, the af-dispatcher Deployment/Service/ServiceMonitor,
  OpenBao k8s-auth + per-tenant-role provisioning notes.

## Verification
The 4 boundary proofs codex requires before the v1.1 flip: (1) **admission** — the CP's P2 commit is
ACCEPTED by tenant-guard + sandbox-guard and a malformed sandbox pod is REJECTED; (2) **cred-exfil** —
a canary agent + `test_cmd` cannot read any PAT/OAuth/OpenBao value or `/proc/1/environ`, and the
durable OAuth is provably absent from the sandbox pod; (3) **egress** — the sandbox reaches ONLY the
broker + DNS; the internet + forge + OpenBao are denied; (4) **live Kata** — `uname -r` shows the Kata
guest kernel and a container escape stays inside the microVM.

<!-- codex-review-status: pending -->
