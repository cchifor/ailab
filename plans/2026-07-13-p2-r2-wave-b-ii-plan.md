# P2 R-2 Wave B-ii — operator activation manifests (broker + tenant-guard tightening + Cilium)

## Context

R-2 code is merged + dormant: broker ASGI app + ledger + capability crypto (agentforge), capability
delivery + cross-repo wiring (agentforge + platform), and the OpenBao provisioner controller +
keypair lifecycle (agentforge, Wave B-i, PR#8). This tranche is the OPERATOR-owned ailab manifests
(all on `feat/p2-unlock`, the P2 activation umbrella) that wire those into the cluster and CLOSE the
provisioner's documented activation gate. Design authority: `plans/2026-07-12-p2-r2-broker-design.md`
§5 (broker deploy/HA), §6 (credential split + tenant-guard tightening + no-ESO-wildcard), §8 (Cilium).

Still deferred (not here): the operator refresh controller (§6 — its core out-of-band OAuth refresh
is PREFLIGHT-gated, §10, so only its plumbing is buildable and it waits on preflight); the CP
broker-ref render (platform — largely satisfied by the merged `AF_SANDBOX_BROKER_URLS`; re-scope
after this lands). Live activation (worker image build+pin, Kata reboots, OpenBao init/unseal, the
live OpenBao+ESO compatibility gate) remains the operator's manual step AFTER this merges.

## Approach

### A. tenant-guard VAP tightening + ns↔key correlation (§6) — the provisioner activation gate
The merged `agentforge-tenant-guard` leaves UNPINNED (its own validations 10-11 note): the
`ExternalSecret.target.name`, `ExternalSecret.spec.secretStoreRef.name`, the `SecretStore.metadata.name`,
the OpenBao k8s-auth `role`, the `serviceAccountRef.name` VALUE, and the source-key `<org>/<workspace>`
segments (syntactic only, NOT correlated to the namespace).

**Correlation is OPERATOR-OWNED, not a CEL label-derive (codex Phase A blocker).** The tenant
reconciler can create/patch Namespaces, so `agentforge.io/org|workspace|pool` labels are CP-WRITTEN
(untrusted), and `af-tenant-<org>-<ws>` is NOT injective when both slugs contain hyphens
(`(a-b,c)` vs `(a,b-c)` collide). So the VAP MUST NOT trust ns labels to decide which org/ws a
namespace legitimately owns. Instead the VAP uses a **`paramRef` to an OPERATOR-OWNED mapping
ConfigMap** `agentforge-tenant-map` (in the operator-owned `agentforge` ns; the tenant repo /
tenant-reconciler CANNOT write it — a separate VAP already blocks tenant writes outside `af-tenant-*`
and the operator commits this file directly). Each entry is authoritative:
`data["<namespace-name>"] = "<org>/<workspace>/<comma-sep-allowed-creds-names>"`. For P2 this holds
ONE operator-committed entry (`af-tenant-tenant-zero-playground` → `tenant-zero/playground/...`);
multi-tenant P3 populates it from the provisioner (operator-owned, CP-untrusted-input-validated) —
NOT from CP labels. Wire the paramRef EXPLICITLY fail-closed: `spec.paramKind: {apiVersion: v1, kind: ConfigMap}`; the
ValidatingAdmissionPolicyBinding `paramRef: {name: agentforge-tenant-map, namespace: agentforge,
parameterNotFoundAction: Deny}` (the `namespace` is REQUIRED — without it the apiserver resolves the
param in the REQUEST namespace, letting a tenant supply its own map; `Deny` makes a missing map
fail-closed), plus a `params != null` guard in every validation. The VAP correlates every ESO object
against the paramRef entry for its namespace:
- Look up `params.data[object.metadata.namespace]`; **reject if `params == null` or the entry is
  absent** (an un-onboarded namespace gets no ESO). Split it into `(org, ws, allowedCreds)`.
- `SecretStore` (namespaced): pin `metadata.name == 'af-tenant-'+org+'-'+ws`, the vault kubernetes-auth
  `role == 'af-tenant-'+org+'-'+ws`, and `serviceAccountRef.name ==` the CP/provisioner-rendered eso SA
  (VERIFY the exact value from `platform renderer.eso_auth_sa_name` + the provisioner template — do NOT
  assume `agentforge-eso`), and FORBID/pin `serviceAccountRef.namespace` (must be THIS ns if the field
  exists). Leave `spec.provider.vault.path == 'af'` and the kubernetes-auth `mountPath == 'kubernetes'`
  as-is (those are NOT under the tenant prefix — only ESO SOURCE KEYS are).
- `ExternalSecret`: pin `spec.secretStoreRef.name == 'af-tenant-'+org+'-'+ws`, `secretStoreRef.kind ==
  'SecretStore'`, `target.name ∈ allowedCreds`, and every `data[].remoteRef.key` / `dataFrom[].extract.key`
  matches a STRICT regex `^tenants/<org>/<ws>/[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*$` for THIS
  entry's org/ws, and additionally rejects any SEGMENT equal to `.` or `..` (split on `/` and check
  each segment — a substring `../` check misses a TERMINAL `..` like `tenants/o/w/..` and a `./`
  segment; these must be rejected segment-wise). Also REJECT `dataFrom[].find` entirely, `//`, a
  leading `/`, empty segments, and any legacy `af/data/<org>/<ws>` (non-`tenants/`) key. Prefix-only
  startsWith is NOT sufficient.
- Independently regex-validate the `org`/`ws` from the paramRef entry as canonical slugs and reject the
  reserved set (`tenants`, `operator`) — defense-in-depth even though the operator wrote the map.
- Enforce on BOTH CREATE and UPDATE (assert matchConstraints include UPDATE; reject an UPDATE that swaps
  source shape). Admission is defense-in-depth; the OpenBao per-workspace role ACL (exact ns+SA+audience
  +TTL binding) remains the mandatory confinement.
- Also TIGHTEN `sandbox-guard`/`sandbox-job-guard` to PIN the stable `agentforge.io/pool` label (a
  canonical slug) — the guards today validate org/workspace/trust-class/job-id but NOT pool, so the
  per-pool broker Cilium selector (§C) has no enforced input yet. The provisioner already requires pool.

### B. broker operator manifests (§5, §6) — new dir `kubernetes/apps/infrastructure/agentforge-broker/`
- `namespace.yaml`: `agentforge-broker`, PSA enforce=restricted, + a **default-deny NetworkPolicy**
  (isolated tier; NOT baseline-only).
- `serviceaccount.yaml`: `agentforge-broker` SA, `automountServiceAccountToken: false`. **No RBAC
  Role** — with automount off + file-based secret reloads the broker needs ZERO k8s API access; grant
  a Role only if a concrete future use appears.
- `deployment.yaml` (GATED — placeholder image, unlisted in kustomization until activation, like the
  reaper): `agentforge broker` from the DEDICATED broker image (the `broker` extra ONLY — no
  OpenBao/k8s client), 2 replicas + `PodDisruptionBudget` + anti-affinity + topology-spread across
  nodes (§5 HA), read-only-rootfs/nonroot/cap-drop-ALL/seccomp-RuntimeDefault + resource requests/limits.
  Mounts ONLY the ESO-written Secrets (`broker-oauth`, the kid-registry public keys, and the ledger
  credential) as files; env = a NON-secret ConfigMap (audience, worst-case tokens, ttls, the ledger
  HOST/DB/USER — NOT the password) + the ledger PASSWORD/DSN-with-auth from the mounted ledger Secret
  (secret material NEVER in the ConfigMap). Reload-on-change (watch the mounted files). Readiness FAILS
  CLOSED on ledger/upstream-credential loss. The broker itself has NO OpenBao/ESO client and NO egress
  to either — ESO (the controller, out-of-band) writes the Secrets; the broker only READS the mounts.
- `service.yaml`: ClusterIP (the DIRECT broker the sandbox reaches for source-IP TOFU).
- `externalsecret-oauth.yaml`: a SEPARATE operator-owned `SecretStore` + `ExternalSecret` (reconciled
  by the ESO CONTROLLER, not the broker) syncing `broker-oauth` from
  `af/data/operator/broker/<provider>/<account>/oauth` via an operator auth role that NO `tenants/*`
  role can read. A separate `externalsecret-ledger.yaml` syncs the broker's ledger credential — a DB
  user with **open/reserve/reconcile privileges ONLY, NEVER `close`** (§5 disjoint authority: the
  reaper alone holds close, via its own close-only DB role — so a compromised broker can never close
  a session out from under the reaper).
- `externalsecret-kids.yaml`: sync the broker's kid PUBLIC-key registry from
  `af/data/operator/broker/<provider>/<account>/kids/*` (public keys ONLY — matches the provisioner's
  publish path + the `broker.config` KidRegistry schema).
- **reaper close-authority wiring (§3/§5)**: NOT just a DB grant — update `reaper-netpol.yaml` to allow
  the reaper→`infra-pg` egress, deliver the reaper's close-only DB credential (an ESO-synced Secret
  mounted into the reaper), and DOCUMENT the `infra-pg` failover posture: either CNPG runs a failover
  topology OR an `infra-pg` outage is a fail-closed service DoS (acceptable for tenant-zero, a P3
  hardening — fail-closed rejects, never a credential/data-integrity risk).

### C. Cilium agent→broker + broker→upstream (§8) — R-2 blockers, not R-3
- agent-profile egress (sandbox pods): ONLY the matching-pool broker ClusterIP + DNS L7 restricted to
  the broker's EXACT name; deny direct-IP/IPv6/alt-DNS/metadata/node-local/service-CIDR.
- broker ingress: admit ONLY sandbox pods of the matching pool/tenant via a STABLE per-pool/tenant
  identity label (the `agentforge.io/pool` + org/workspace labels now PINNED by §A's sandbox-guard
  tightening — NOT the high-cardinality per-job label, which stays OUT of Cilium security-identity).
- broker egress: the enumerated upstream API + auth/OAuth hosts ONLY (FQDN L7 + explicit deny of
  private/link-local/service/node/IPv6 ranges) + DNS + `infra-pg` (the ledger). **NO ESO/OpenBao
  egress** — the broker never talks to OpenBao/ESO (that would break §6 isolation; ESO sync is the
  controller's job, the broker only reads mounted Secrets).

## Critical files
- `kubernetes/apps/apps/agentforge/admission/tenant-guard.yaml` (tighten via a `paramRef` to the map)
  + NEW `kubernetes/apps/apps/agentforge/admission/tenant-map.yaml` (the OPERATOR-owned
  `agentforge-tenant-map` ConfigMap + the VAP `paramKind`/binding `paramRef`).
- `kubernetes/apps/infrastructure/agentforge-sandbox/{sandbox-guard,sandbox-job-guard}.yaml` (pin the
  `agentforge.io/pool` label).
- NEW `kubernetes/apps/infrastructure/agentforge-broker/**` (ns[+default-deny netpol]/sa/deploy[gated]/
  svc/configmap/externalsecret-{oauth,kids,ledger}/kustomization — NO rbac Role).
- NEW/edit Cilium: agent→broker egress + a broker ingress/egress CNP (broker egress = upstream+DNS+
  infra-pg ONLY, no ESO/OpenBao).
- `kubernetes/apps/infrastructure/agentforge-sandbox/reaper-netpol.yaml` (+ reaper→infra-pg egress) +
  the reaper close-only DB credential (ESO Secret + mount) + the SQL grant (SOPS/databases wiring).
- `kubernetes/apps/infrastructure/*/kustomization.yaml` wiring (broker dir; deployment.yaml UNLISTED).

## Verification
- **tenant-guard**: kubeconform + a CEL/admission test matrix. ADMIT a CP-shaped ExternalSecret/
  SecretStore whose ns has an operator-map entry and whose names/role/SA + `tenants/<org>/<ws>/…` key
  all match that entry. REJECT each of: no map entry for the ns; wrong SecretStore/role/store name;
  foreign or wrong-namespace `serviceAccountRef`; `target.name` outside the entry's allowed creds;
  `remoteRef.key` under another tenant / under `operator/*` / legacy `af/data/<org>/<ws>` / with a
  `..` or `.` SEGMENT (incl. TERMINAL `tenants/o/w/..`) / `//` / leading-slash / empty segment; a
  `dataFrom.find`; a reserved/non-slug org/ws in the entry; a missing/`null` param map (fail-closed);
  and hyphen-collision ns names (two entries can't alias). CREATE and UPDATE (incl. an UPDATE that
  swaps source shape). Also: a sandbox pod/Job WITHOUT a canonical `agentforge.io/pool` label is
  rejected by the tightened sandbox guards.
- **broker manifests**: kubeconform valid; deployment UNLISTED (gated); broker SA automount off + NO
  Role; broker-oauth/kids/ledger ExternalSecrets reference the operator (non-tenant) SecretStore +
  paths under `operator/broker/*`; the ledger PASSWORD is in a Secret (never the ConfigMap); readiness
  fails closed on credential loss; PDB + anti-affinity + topology-spread + default-deny netpol present.
- **Cilium**: policy lints; agent egress = only the pool broker + DNS-to-broker-name; broker ingress
  keys on the stable pool/org/workspace labels; broker egress = upstream + DNS + infra-pg ONLY (assert
  NO ESO/OpenBao). Negative canaries: other-pool broker, internet/direct-IP, alternate DNS, metadata,
  node-local, service-CIDR, IPv6, private/link-local upstream — all denied.
- **No activation**: nothing here flips `privilege_hardening` or lists the gated Deployments; Flux
  reconciles the policies/ns/SA/ESO/map wiring but the broker Deployment + provisioner stay gated until
  the operator's manual activation (image pin + OpenBao init + the live OpenBao/ESO compat gate). The
  broker never gets OpenBao egress to compensate for the still-deferred refresh controller.

## Notes
- All on `feat/p2-unlock` (the deferred P2 activation umbrella) — NOT a standalone PR to main; it
  extends the umbrella that lands as the deliberate operator-activation milestone.
- Codex Phase A on THIS plan (the "how": correlation-by-deterministic-naming vs mapping-ConfigMap,
  broker HA shape, Cilium identity labels), then Phase B on the rendered manifests (cap 3 each).

<!-- codex-review-status: finalized -->

<!-- Phase A: codex round 1 (3 blockers + important) + round 2 (blockers resolved; 3 residuals: segment-based key traversal, disjoint broker/reaper DB privileges, explicit fail-closed VAP paramRef wiring) — all accepted + incorporated; no pushback. Finalized for implementation. -->
