# Implementation review — p2-r2-wave-b-ii — round 1

<!-- codex-impl-review-status: pending -->

## Summary
- Tenant-map correlation is fail-closed: `paramRef.namespace: agentforge`, `parameterNotFoundAction: Deny`, `params != null`, and no-map namespaces are rejected for ESO.
- CEL shape looks valid for Kubernetes 1.31: `split` is in the 1.30+ extended strings library and `matches` has runtime regex cost accounting; I found no constant-regex requirement in Kubernetes CEL docs: https://kubernetes.io/docs/reference/using-api/cel/
- The source-key regex and renderer-name pins mostly match the contract, including `af-eso-<ws>`, `af-tenant-<org>-<ws>`, `af-creds-<ws>-<pool>`, and `tenants/<org>/<ws>/...`.
- Broker/reaper Deployments remain dormant with placeholder digests, and broker runtime has no RBAC, automount off, default-deny, and no OpenBao/ESO egress.
- Findings below cover the remaining isolation gaps: ESO source/auth escape hatches, broker/reaper credential fail-closed issues, service-only Cilium egress, and declarative DB grant proof.

## Findings
### ExternalSecret can bypass the pinned SecretStore via per-source sourceRef
**Location:** kubernetes/apps/apps/agentforge/admission/tenant-guard.yaml:251
**Severity:** blocker
<!-- codex: Validation 14 pins only top-level spec.secretStoreRef, and validation 15 checks only dataFrom.extract.key / data.remoteRef.key. ESO supports per-source sourceRef on spec.data[].sourceRef.storeRef, including ClusterSecretStore references, so a tenant ExternalSecret can satisfy the top-level SecretStore pin while fetching an item through a different store. That defeats the "secretStoreRef.kind == SecretStore" confinement the VAP claims. Fix by rejecting sourceRef on every spec.data[] and spec.dataFrom[] entry, and by rejecting generatorRef/sourceRef shapes outright for tenant ExternalSecrets. -->

### SecretStore still permits additional Vault auth methods
**Location:** kubernetes/apps/apps/agentforge/admission/tenant-guard.yaml:175
**Severity:** important
<!-- codex: The SecretStore validation requires vault.auth.kubernetes.serviceAccountRef.name, but it does not reject sibling auth methods such as tokenSecretRef, appRole, cert, or kubernetes.secretRef. ESO's Vault SecretStore API exposes those auth fields, so the VAP is relying on ESO/runtime behavior instead of enforcing the stated "kubernetes-auth via serviceAccountRef, no static credential" invariant. Fix with explicit negative checks for every non-kubernetes Vault auth method and for vault.auth.kubernetes.secretRef; keep serviceAccountRef.name/namespace pinned as implemented. -->

### Agent egress allows direct broker PodIP traffic
**Location:** kubernetes/apps/infrastructure/agentforge-sandbox/cilium-egress.yaml:64
**Severity:** important
<!-- codex: The plan says the agent may reach only the pool broker ClusterIP, with direct-IP denied. This rule uses toEndpoints for every pod labeled app.kubernetes.io/name=agentforge-broker, which allows a sandbox that learns or scans the broker PodIP to bypass the Service/ClusterIP path. That also bypasses Service readiness endpoint removal, weakening the "readiness fails closed" operational boundary. Use a service-scoped Cilium rule, e.g. toServices for agentforge-broker/agentforge-broker on 8700, or otherwise constrain to the Service ClusterIP rather than backend endpoints. Keep broker ingress label checks as the second layer. -->

### Reaper can start without the close-only ledger credential
**Location:** kubernetes/apps/infrastructure/agentforge-sandbox/reaper-deployment.yaml:87
**Severity:** important
<!-- codex: AF_REAPER_LEDGER_DSN is optional:true. When the gated reaper Deployment is eventually listed, a missing or unsynced agentforge-reaper-ledger Secret will not block startup; the reaper can run and reap without close authority, leaving copied capabilities usable until TTL instead of closing at Job-end. Because the Deployment is already unlisted for dormancy, optional:true is not needed as a gate. Remove optional:true so activation fails closed until the close-only credential exists, or add an explicit startup/readiness gate that refuses to run reclaim without the DSN. -->

### Broker ledger credential is env-only, not reloadable mounted secret material
**Location:** kubernetes/apps/infrastructure/agentforge-broker/deployment.yaml:90
**Severity:** important
<!-- codex: The contract says broker OAuth, kids, and ledger credential are ESO-written mounted files with reload-on-change and readiness failing closed on credential loss. The manifest uses a secretKeyRef env var for AF_BROKER_LEDGER_DSN and mounts only broker-oauth/broker-kids. Env-sourced Secrets do not update on rotation and cannot disappear from a running process when the Secret is deleted, so the broker cannot satisfy the reload/credential-loss invariant for the ledger credential. Mount broker-ledger read-only as a file and pass a path or add code/config support for file-based DSN reload; readiness should check the current mounted credential and ledger connectivity. -->

### DB privilege split is documented but not declaratively materialized
**Location:** kubernetes/apps/infrastructure/agentforge-broker/externalsecret-ledger.yaml:13
**Severity:** important
<!-- codex: The manifests fetch broker/reaper DSNs from OpenBao and comment that SCHEMA_SQL, BROKER_GRANTS_SQL, and REAPER_GRANTS_SQL are applied manually. The diff does not add CNPG managed roles, password material, or a migration/grant Job under databases, so this declarative review cannot prove that agentforge_broker is open/reserve/reconcile-only and agentforge_reaper is close-only. Either add declarative DB role/grant wiring for activation, or explicitly move that proof out of the Wave B-ii manifest contract and keep activation blocked on a named manual runbook step. -->

### Sandbox pool slug regex is looser than the canonical slug contract
**Location:** kubernetes/apps/infrastructure/agentforge-sandbox/sandbox-guard.yaml:91
**Severity:** nit
<!-- codex: sandbox-guard and sandbox-job-guard now require agentforge.io/pool, but the regex ^[a-z0-9][a-z0-9-]{0,62}$ allows a trailing hyphen. Kubernetes label validation will reject that before admission, so this is not a live bypass, but it does not match the renderer/provisioner canonical slug shape used elsewhere. Use ^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$ for org/workspace/pool in both sandbox guards. -->

## Diff stat
 .../apps/agentforge/admission/kustomization.yaml   |   3 +-
 .../apps/agentforge/admission/tenant-guard.yaml    | 137 ++++++++++++++++-
 .../apps/apps/agentforge/admission/tenant-map.yaml |  39 +++++
 kubernetes/apps/clusters/ai/agentforge-broker.yaml |  32 ++++
 .../agentforge-broker/ciliumnetworkpolicy.yaml     |  99 +++++++++++++
 .../agentforge-broker/configmap.yaml               |  44 ++++++
 .../agentforge-broker/deployment.yaml              | 164 +++++++++++++++++++++
 .../agentforge-broker/externalsecret-kids.yaml     |  35 +++++
 .../agentforge-broker/externalsecret-ledger.yaml   |  40 +++++
 .../agentforge-broker/externalsecret-oauth.yaml    |  73 +++++++++
 .../agentforge-broker/kustomization.yaml           |  21 +++
 .../agentforge-broker/namespace.yaml               |  43 ++++++
 .../infrastructure/agentforge-broker/service.yaml  |  27 ++++
 .../agentforge-broker/serviceaccount.yaml          |  16 ++
 .../agentforge-sandbox/cilium-egress.yaml          |  88 +++++++----
 .../agentforge-sandbox/kustomization.yaml          |   5 +-
 .../agentforge-sandbox/reaper-deployment.yaml      |  11 ++
 .../agentforge-sandbox/reaper-ledger.yaml          |  73 +++++++++
 .../agentforge-sandbox/reaper-netpol.yaml          |  30 +++-
 .../agentforge-sandbox/sandbox-guard.yaml          |   9 +-
 .../agentforge-sandbox/sandbox-job-guard.yaml      |   9 +-
 21 files changed, 955 insertions(+), 43 deletions(-)
---
## Round-1 addressed (all 7 — commits 0e9ced7..203ac2f)
- [FIXED blocker] per-source ESO store override: validation 14 forbids data[].sourceRef +
  dataFrom[].{sourceRef,generatorRef,find} (CP never sets them) — closes the secretStoreRef bypass.
- [FIXED] SecretStore vault auth is EXACTLY kubernetes (size(auth)==1 && 'kubernetes' in auth).
- [FIXED] agent→broker egress via the broker ClusterIP Service (toServices), not a direct PodIP.
- [FIXED] reaper AF_REAPER_LEDGER_DSN secretKeyRef optional:false (fail-closed; gated).
- [FIXED] broker ledger DSN mounted as a read-only file (/var/run/broker/ledger 0440) + AF_BROKER_LEDGER_DSN_FILE
  path via ConfigMap; NOT env/ConfigMap. FOLLOW-UP (activation): agentforge broker/config.py currently reads
  AF_BROKER_LEDGER_DSN from env only — it must add *_FILE reading (mirroring oauth/kids reload) before activation.
- [FIXED] DB role split materialized in databases/infra-pg.yaml: agentforge_broker DB + agentforge_broker_admin
  (owns schema/close_session fn) / agentforge_broker (open/reserve/reconcile) / agentforge_reaper (close-only)
  via postInitSQL + managed.roles; the disjoint GRANTs live in the agentforge ledger migration (referenced).
- [FIXED nit] pool label = canonical both-ends-anchored slug.
ON-CLUSTER CONFIRM (activation): CEL split()/dynamic matches() under k8s 1.31 (codex: valid); toServices+toPorts
needs Cilium 1.14+ (drop toPorts if rejected). Both gated Deployments still unlisted; privilege_hardening untouched.
