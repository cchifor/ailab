# P2 sandbox redesign — the real boundary (separate ephemeral Kata pod + broker + redaction)

## Codex Design Review

- **Verdict — not implementation-ready yet.** The separate Kata pod is the correct trust boundary and resolves the central same-pod/DinD flaw, but the shared-filesystem import, broker transport, PSA exemption, OpenBao provisioning authority, and crash cleanup remain design-level blockers.
- **Blockers #1–2 — partially closed.** A fresh trusted checkout and no shared `.git` close the original hook/config escape only if the readback is a race-free, content-only import with a normative validator; the current per-workspace RWX layout does not yet guarantee job isolation.
- **Blockers #3–4 — partially closed.** Keeping durable inference credentials outside the sandbox is correct, but a Unix socket cannot be bind-mounted from an orchestrator container into a separate Kata pod. Use an authenticated network broker, strictly reconstruct model requests, and treat redaction only as defense-in-depth—not as a DLP boundary.
- **Blockers #5–6 — directionally closed.** Separate network policy, ESO `v1`, and the expanded admission/RBAC allowlists are implementable. The cluster-wide PSA runtimeClass exemption is unnecessary and dangerously broad for an unprivileged sandbox; use `restricted` PSA plus a fail-closed sandbox policy instead.
- **Blocker #7 — partially closed.** Per-workspace OpenBao roles and paths fix the identity mismatch, but the design must name a dedicated ESO-auth SA and a narrowly exposed trusted provisioner. Giving a CP-reachable job authority over both ACL policies and Kubernetes-auth roles concentrates effective OpenBao-admin power.
- **KEDA and writable home are closed at design level.** Keeping `maxReplicaCount == 1` until the epoch-safe lease exists is the correct fail-closed choice. Before rollout, add resource/tenant quotas, per-job storage isolation, broker availability limits, and independent Job/PVC garbage collection after orchestrator failure.

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

<!-- codex: The design now addresses every original blocker, but “fixes” is premature. The boundary remains incomplete until the hostile-PVC import protocol, feasible broker transport, fail-closed admission/PSA shape, and privileged OpenBao provisioning path are specified below. -->

## Approach — two-pod boundary

**Trusted orchestrator pod** (`agentforge serve`): holds forge PATs, git-push token, OpenBao SA, the
**credential broker**, and does ALL forge writes. Runs NO untrusted code. Creates + reaps ephemeral
sandbox pods via the k8s API (scoped RBAC, admission-pinned).

<!-- codex: ESO should eliminate the orchestrator's need for an OpenBao identity: mount only the ESO-produced forge/config Secret. Also do not colocate the broker's durable OAuth and network attack surface with the forge-credentialed orchestrator. Prefer a separately deployed broker pod containing only its model credential, with no forge/CP access and model-only egress. -->

**Ephemeral sandbox pod** (per untrusted job — one agent-CLI run or one `test_cmd`): `runtimeClassName:
kata`; `automountServiceAccountToken: false` + a **tokenless SA** with no Secret RBAC; **no** forge/
OpenBao/CP egress (model-only Cilium policy + a DNS proxy for FQDN); read-only rootfs + a writable
per-pod home + the job I/O volume; **no durable credentials**. Admission pins the exact shape. It runs
one command and is deleted.

<!-- codex: Pin the full restricted security context: non-root fixed UID/GID, allowPrivilegeEscalation:false, capabilities.drop:[ALL], seccomp RuntimeDefault, readOnlyRootFilesystem, no host PID/IPC/network, no unsafe sysctls, no procMount override, no device/resource claims, and only explicit volume types. Agent jobs may reach only the authenticated broker; test/setup jobs require a distinct admission/network-policy profile with no broker or DNS egress. -->

### Job I/O — no shared trusted git
- A per-workspace **RWX PVC** (qnap-nfs `nfs-csi`, RWX) is the ONLY thing the orchestrator and the
  sandbox pod share. The orchestrator writes a **content-only export** (the repo tree it wants the
  agent to work on, plus task inputs) to a per-job subdir — **never** a `.git` the orchestrator will
  later trust.

<!-- codex: Mount only the unique job directory into the sandbox using a bind-style `subPath`, not the workspace PVC root. Job IDs must be unguessable, and admission must prevent selecting another job's claim/subPath. A per-job PVC is stronger and avoids cross-job reads, writer races, capacity contention, and cleanup ambiguity; if PVC-create RBAC is undesirable, specify how pre-provisioned workspace storage provides equivalent isolation and quota enforcement. -->

- The sandbox pod runs the agent/test against that subdir and writes its output (modified tree, stdout,
  a structured result) back to the same subdir.
- The orchestrator reads ONLY the content back, computes the diff by applying it to a **fresh trusted
  checkout** it controls (its own clone with a clean `.git`), validates paths/symlinks/ownership/size,
  **redacts** (below), and commits + pushes from the trusted checkout. `git status/commit/push` NEVER
  runs in the shared/untrusted dir → closes blocker #1.

<!-- codex: Make the validator normative. Stop and delete the sandbox before import and ensure no other writer can mutate the job directory. Traverse descriptor-relatively (`openat2` with RESOLVE_BENEATH/NO_MAGICLINKS or an equivalent no-follow construction), never by check-then-open pathname logic. Accept only directories, regular files, and—if required—relative symlinks whose lexical target remains inside the exported tree; never dereference links while importing. Reject hardlinks (`nlink != 1`), absolute/escaping links, devices, FIFOs, sockets, `.git` path components, NUL/`..`/absolute paths, normalization or case-collision duplicates, unexpected ownership, xattrs, ACLs, file capabilities, and setuid/setgid/sticky bits. Enforce per-file, logical-size, total-size, file-count, depth, path-length, and stdout/result limits. Create new destination files with normalized 0644/0755 modes rather than preserving hostile metadata. -->

<!-- codex: Parse the structured result as hostile bounded input with an exact schema. It must never supply an argv element, shell fragment, filesystem path, URL, object name, template, or format string without independent validation. Derive exit state from the Kubernetes container status, not a sandbox-written result field. Use a clean orchestrator HOME, disable system/global Git config and signing/external helpers, set `core.hooksPath` to a root-owned empty directory, and invoke Git with argv plus an explicit `--` pathspec—never a shell. -->

### Executor port change
`SandboxExecutor.run(ExecSpec)` becomes: (1) stage `spec.cwd` content into the job PVC subdir; (2)
create a sandbox Pod (kata, tokenless, model-only, digest-pinned sandbox image) whose command is the
`spec.argv`/shell, env limited by trust class, home writable, the job subdir mounted; (3) wait
(timeout → delete pod); (4) collect stdout + the result file; (5) delete the pod. `LocalExecutor`
stays the default. The DinD sidecar is **removed** entirely.

<!-- codex: A direct Pod has no TTL controller, so orchestrator crash can leak a running pod indefinitely. Prefer a Kubernetes Job with `backoffLimit: 0`, `activeDeadlineSeconds`, a short `terminationGracePeriodSeconds`, and `ttlSecondsAfterFinished`, plus an independent operator/reaper that deletes expired Jobs, Pods, job directories, and PVCs by signed/validated expiry metadata. Add CPU, memory, ephemeral-storage, PID, output, runtime, and per-tenant concurrent-job limits through pod resources, LimitRange/ResourceQuota, and storage quotas. -->

### Credential broker (durable creds never enter the sandbox) — blocker #3
- A **model-proxy broker** runs in the orchestrator pod, listening on a Unix socket (or localhost) that
  is bind-mounted into the sandbox pod. It authenticates upstream (Anthropic/OpenAI/litellm) with the
  durable OAuth **on the orchestrator side** and forwards the agent's requests. The agent CLI is
  configured (`ANTHROPIC_BASE_URL`/`OPENAI_BASE_URL` → the broker; a dummy/short-lived token) so the
  **durable OAuth is never present in the sandbox pod** — the agent can't copy it into the diff/output.

<!-- codex: This transport is not feasible across separate pods. `emptyDir` is pod-local, an NFS/PVC socket inode does not bridge separate kernels/Kata guests, and hostPath would violate the stated admission boundary and pin execution to a node. Use a ClusterIP backed by a separate broker Deployment (preferred) or the orchestrator Service. Restrict Cilium egress to broker namespace+pod identity and port, and require a one-job, one-model, short-TTL capability or mTLS identity so other pods cannot consume the broker. The token may enter the sandbox because it is narrowly scoped and disposable; it must not authorize any non-model operation. Validate that both Claude Code and Codex support the required base-URL, streaming, and authentication behavior before implementation. -->

- The broker also enforces model-only routing (rejects non-model destinations) and is the single
  outbound path for the sandbox (its Cilium policy allows egress only to the broker + DNS).
- The forge (issue/PR) creds stay entirely in the orchestrator; the sandbox never touches the forge.

<!-- codex: Implement an application-layer model gateway, not a generic HTTP/TCP proxy. Hard-code upstream scheme/host/port and verified TLS; reconstruct requests; reject CONNECT, absolute-form targets, redirects, proxy headers, hop-by-hop headers, conflicting Content-Length/Transfer-Encoding, unsupported upgrades, and arbitrary upstream headers. Bound request/response size, models, token budgets, concurrency, rate, duration, and spend per job/account. A broker compromise exposes its durable OAuth, so isolate it from forge/CP credentials, audit it, rotate credentials, and define readiness/fail-closed behavior and capacity so it is not an unbounded single point of failure. Exfiltration of sandbox-visible source through the permitted model prompt is inherent; document that this design protects platform credentials but is not a confidentiality boundary between repository content and the selected model provider. -->

### Outbound redaction — blocker #3
Before ANY forge publication (PR body, review comment, commit message, issue comment, event, log line),
the orchestrator runs a redactor over the text: strips anything matching known secret shapes (the
inference OAuth, PATs, bearer tokens, `sk-`/`ghp_`/`bao`-style keys, high-entropy blobs). A single
`redact()` gate on the forge-publish path.

<!-- codex: Regex and entropy scanning are trivially bypassed by encoding, splitting, substitution, or steganography and cannot be part of the claimed security boundary. The primary defense is that platform secrets never enter sandbox input; scan exact known values and common encodings where possible, but treat this solely as defense-in-depth. Apply strict schemas and size caps to every publication path, including errors and observability exporters. For changed repository blobs/diffs, detection should block and quarantine publication rather than silently mutate source code. State explicitly whether pre-existing repository secrets are in scope: an agent can redistribute data already present in its input, and generic redaction cannot provide reliable DLP. -->

### Egress — blocker #4
- P2 **removes** the broad P1 K8s NetworkPolicy on the worker; the orchestrator pod gets a Cilium
  policy: egress to forge, OpenBao, CP, litellm, model — as needed.
- The **sandbox pod** gets a Cilium policy: egress ONLY to the broker socket's node-local path (or a
  ClusterIP if socket isn't feasible) + DNS proxy; default-deny everything else. The container also
  runs `--network`-equivalent isolation via the pod (no docker anymore).

<!-- codex: Select the ClusterIP broker design; there is no safe node-local socket path in this model. The sandbox policy should allow TCP only to broker-labeled endpoints and UDP/TCP 53 only to the actual cluster DNS endpoints; test/setup selectors get no egress. FQDN enforcement belongs on the broker's upstream policy: permit DNS only to cluster DNS and TLS only to exact model domains/ports, accounting explicitly for CNAMEs and preventing additive Kubernetes policies from broadening access. Add canaries for direct IPs, alternate DNS, IPv6, service IPs, node-local services, metadata endpoints, and every destination allowed to the orchestrator or broker. -->

### Admission / PSA / RBAC — blockers #5, #6
- A **trusted, operator-created sandbox namespace** `af-sandbox` (or per-tenant `af-sbx-<org>-<ws>`)
  labeled `pod-security.kubernetes.io/enforce: privileged`-exempt-via-kata: add
  `PodSecurityConfiguration.exemptions.runtimeClasses: ["kata"]` to the API-server admission config
  (documented operator step) so a kata pod isn't blocked by baseline PSA. (No privileged container
  remains anyway — the sandbox pod is unprivileged; the DinD-privileged problem disappears.)

<!-- codex: Remove this exemption. RuntimeClass selection is not forbidden by restricted PSA, and the redesigned pod requires no privileged fields. A runtimeClass exemption is cluster-wide: any principal able to create a `kata` pod elsewhere could bypass PSA unless a separate policy happened to catch it. Label sandbox namespaces `pod-security.kubernetes.io/enforce: restricted` (with pinned version), keep them operator-owned, and make the pod conform. Do not label them `privileged`. -->

- A **P2 ValidatingAdmissionPolicy** `agentforge-sandbox-guard` pins the sandbox pod shape for the
  orchestrator SA's pod-creates: `runtimeClassName == kata`, `automountServiceAccountToken == false`,
  the SA is the tokenless sandbox SA, image is the pinned sandbox digest, no host* / hostPath /
  privileged / nodeName, only the allowed volumes (job PVC + broker socket + writable home), and the
  model-only network label. Reject anything else.

<!-- codex: Remove the broker-socket volume. Bind the guard fail-closed to every Pod CREATE in the sandbox namespace, not merely to a mutable label or a best-effort user match; protect the policy/binding and namespace in the CP-unwritable operator repo. Pin containers/initContainers, count and names, digest, command trust class, resources, security contexts, ServiceAccount, scheduler fields, tolerations/nodeSelector, DNS settings, volumes/mounts/subPaths, no secret/configMap/projected/CSI/hostPath volumes, no ephemeral containers, no image changes on UPDATE, and exact labels used by Cilium. Verify admission failurePolicy is `Fail` and that no other webhook can mutate the pod after validation into a broader form. -->

- Orchestrator SA RBAC: `create/get/delete pods` **in the sandbox namespace only**, admission-pinned.

<!-- codex: This prevents a compromised orchestrator from creating a non-sandbox pod only if its complete effective RBAC has no pod-create rights elsewhere, impersonation, controller creation, pod update/patch, `pods/exec`/attach/portforward, Service/EndpointSlice creation, PVC mutation, TokenRequest, Secret access, or admission-policy mutation. Add an authorization test using SelfSubjectRulesReview plus rejected create attempts. `list/watch` may be needed operationally; if granted, scope them to the namespace and avoid exposing unrelated tenant job metadata. A compromised orchestrator already owns its mounted forge credentials, so this control limits Kubernetes/node escalation rather than repairing orchestrator credential compromise. -->

- `tenant-guard.yaml`: add `external-secrets.io/v1` (SecretStore/ExternalSecret) + `keda.sh/v1alpha1`
  (ScaledObject) to the GVK allowlist WITH field validations (SecretStore provider/role pinned to this
  ns+SA; ExternalSecret path/target pinned; ScaledObject target/query pinned). `reconciler-rbac.yaml`:
  add the ESO/KEDA API groups (namespaced, scoped verbs).
- Renderer: emit **`external-secrets.io/v1`** (ESO 2.7.0 serves `v1`, not `v1beta1`) — blocker #6.

<!-- codex: This closes the API-version/discovery mismatch. Contract-test the rendered objects against the installed CRD schemas and server-side dry-run, then prove the tenant reconciler cannot select ClusterSecretStore, another namespace/SA/path/target, a foreign scale target, an arbitrary Prometheus server, or a query that drops required org/workspace/account/pool labels. -->

### OpenBao tenant provisioning — blocker #7
- A **trusted provisioning path** (a CP job or an operator-run step invoked at workspace-provision
  time) creates, per workspace: an OpenBao **role** bound to exactly `af-sbx-<org>-<ws>` namespace +
  the tenant SA, and a **policy** granting read on `af/data/<org>/<workspace>/*` only. The SecretStore
  references the **tenant SA** (not the ESO controller SA).

<!-- codex: Use a dedicated per-workspace `eso-auth` SA, not the sandbox SA and preferably not the orchestrator SA. `automountServiceAccountToken:false` does not prevent ESO from requesting a TokenRequest token for that identity; the ESO controller therefore needs narrowly scoped `serviceaccounts/token` authorization for the named SA, while the SA itself needs no Kubernetes Secret permissions. Bind the OpenBao role to that exact namespace+SA and audience, use short token TTLs, and test cross-namespace and cross-workspace authentication failures. -->

<!-- codex: “CP job or operator step” is not sufficiently defined. An identity able to write both OpenBao ACL policy documents and Kubernetes-auth roles can create a policy granting broader paths and bind it to an attacker-controlled SA; OpenBao ACLs do not safely constrain arbitrary policy content. Keep this credential out of the CP and orchestrator. Use a network-isolated, audited provisioning controller/service that accepts only validated workspace identifiers and renders fixed policy/role templates, or an operator-run GitOps workflow with short-lived admin authority. Specify idempotency, ordering before SecretStore reconciliation, rotation, tenant deletion/revocation, orphan reconciliation, and audit alerts. -->

- OpenBao endpoint: use the actual internal **HTTP** endpoint (or configure verified TLS + CA); the
  renderer's `https://` with no TLS listener is wrong — fix the address/scheme.
- ESO ExternalSecret still syncs ONLY the creds the ORCHESTRATOR needs (forge PATs, CP bearer) into the
  orchestrator's Secret. The inference OAuth goes to the BROKER's secret, not the sandbox. Split the
  secrets (blocker #3): orchestrator-creds vs broker-oauth, in separate OpenBao paths + ExternalSecrets.

<!-- codex: Mount `broker-oauth` only into the separately isolated broker pod and ensure the orchestrator cannot read that Kubernetes Secret. Conversely, the broker must not mount orchestrator credentials or an OpenBao/ESO identity. Prefer verified internal TLS with a pinned CA; if HTTP is temporarily unavoidable, constrain it to the trusted network and record that OpenBao credentials and secret material otherwise traverse the cluster network in plaintext. -->

### KEDA — blocker #8-scaling
- Render an **always-on `af-dispatcher` Deployment + Service + Prometheus scrape** (ServiceMonitor with
  `release: kube-prometheus-stack`) exporting `forge_pending{role,repo,pool}`; the ScaledObject query
  includes pool/account labels. Until a **downward-API per-pod claim id + the epoch-safe account lease**
  are implemented, keep `maxReplicaCount == 1` (the P1 stable-identity cap) — lifting to >1 is a
  tracked follow-on within P2.

<!-- codex: Correct. Make `maxReplicaCount: 1` an admission-enforced value, not merely a renderer default, until the epoch-safe lease and unique claim identity pass crash/late-release tests. Align the metric schema and query—the exporter description currently omits `account` while the query requires it—and bound label cardinality. -->

### Inner home — blocker #8
The sandbox pod gets its OWN writable home (emptyDir) containing only broker config (base URL + socket)
— never the orchestrator home or durable creds.

<!-- codex: Replace socket configuration with the broker Service URL and per-job disposable authorization. Cap the emptyDir with `sizeLimit`, mount it only in the sandbox container, and ensure CLI caches, transcripts, crash dumps, and telemetry are deleted with the pod and cannot spill to the shared PVC. -->

## Phasing (this redesign)
- **R-1 boundary core**: SandboxExecutor → separate-pod (remove DinD); the sandbox pod renderer
  (tokenless, kata, model-only, admission-pinned); orchestrator pod-create RBAC + the sandbox VAP; PSA
  kata exemption; the RWX job I/O + trusted-checkout export. **This alone makes it a boundary.**

<!-- codex: Remove the PSA exemption and do not call R-1 complete until the hostile-import validator, writer quiescence/TOCTOU rule, job-level storage isolation, restricted security context, resource limits, and crash-independent GC are part of the design and tests. A separate pod closes direct credential co-residency; the RWX filesystem remains an intentional untrusted channel that must be mediated. -->

- **R-2 creds**: the model-proxy broker + secret split + OpenBao per-tenant role/policy provisioning +
  ESO v1 + SecretStore tenant-SA fix.
- **R-3 redaction + egress**: the `redact()` forge-publish gate + the distinct orchestrator/sandbox
  Cilium policies + tenant-guard/reconciler-rbac ESO/KEDA + the af-dispatcher deploy/scrape.
- Then the sandbox-boundary CANARY tests (malicious repo/prompt tries to read PATs/OAuth/OpenBao/
  /proc/1/environ + egress the internet — all must fail) gate the `privilege_hardening: v1.1` flip.

<!-- codex: R-2 must use the network broker and dedicated ESO/provisioner identities described above. Redaction cannot have a universal “bypass fails” acceptance criterion; instead prove platform secrets are absent from sandbox inputs, exact known-secret canaries are blocked on every sink, and scanner failure fails closed. The v1.1 flip also requires cleanup/recovery, quota/DoS, broker isolation, and hostile-filesystem tests. -->

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

<!-- codex: Update these targets to a standalone broker Deployment/Service, restricted-PSA sandbox namespace with no runtimeClass exemption, a cleanup/reaper controller, dedicated `eso-auth` SAs, and the isolated OpenBao provisioning component. -->

## Verification
The 4 boundary proofs codex requires before the v1.1 flip: (1) **admission** — the CP's P2 commit is
ACCEPTED by tenant-guard + sandbox-guard and a malformed sandbox pod is REJECTED; (2) **cred-exfil** —
a canary agent + `test_cmd` cannot read any PAT/OAuth/OpenBao value or `/proc/1/environ`, and the
durable OAuth is provably absent from the sandbox pod; (3) **egress** — the sandbox reaches ONLY the
broker + DNS; the internet + forge + OpenBao are denied; (4) **live Kata** — `uname -r` shows the Kata
guest kernel and a container escape stays inside the microVM.

<!-- codex: Extend proof (1) with every effective-RBAC and admission mutation listed above, including attempts to use another SA/image/runtimeClass/volume/subPath/security context, create a controller or endpoint, and bypass PSA. Extend proof (2) with hostile result schemas, unsafe Git metadata/config, symlink/hardlink/device/FIFO/socket/setuid/xattr trees, Unicode/path collisions, oversized/sparse files, and a concurrent-mutation attempt. Extend proof (3) with direct IP/IPv6, alternate DNS, node/metadata/service destinations, broker SSRF/request-smuggling/redirect/CONNECT tests, invalid or replayed job capabilities, and test-job broker denial. Add proof (5): kill the orchestrator at every lifecycle stage and verify the deadline/reaper removes the Job, Pod, home, directory/PVC, and broker authorization without deleting another job. Add proof (6): exhaust CPU, memory, PIDs, disk, output, model budget, and job count and verify tenant-scoped limits and broker fail-closed behavior. -->

<!-- codex-review-status: complete -->