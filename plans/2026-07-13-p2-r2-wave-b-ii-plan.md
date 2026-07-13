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
segments (syntactic only, NOT correlated to the namespace). Close ALL of these by DETERMINISTIC
correlation (NO operator mapping ConfigMap needed — the CP already renders deterministic names):
- Add CEL vars deriving the expected names from the ns labels: `org = nsLabels['agentforge.io/org']`,
  `ws = nsLabels['agentforge.io/workspace']`, `tenant = 'af-tenant-' + org + '-' + ws`.
- Require `namespaceObject.metadata.name == variables.tenant` (labels are authoritative ONLY because
  the immutable ns name equals the label-derived name — a mutated label mismatches the name → reject;
  same derive-and-compare the provisioner + sandbox-guard use). Reject if org/ws are empty/absent.
- On `SecretStore`: pin `metadata.name == variables.tenant`, the vault k8s-auth `role == variables.tenant`,
  and `serviceAccountRef.name == 'agentforge-eso'` (the operator-fixed eso-auth SA — matches the
  provisioner template). Keep the existing mountPath/path-prefix pins.
- Pin the SecretStore's OpenBao path/role→key containment: the auth path + any `path`/`remoteRef.key`
  the CP may set must be under `tenants/<org>/<workspace>/` (STARTS-WITH the reserved tenants prefix
  for THIS ns's org/ws) — closes the §6 wildcard/overlap so no tenant ExternalSecret can name a key
  outside its subtree or reach `operator/*`.
- On `ExternalSecret`: pin `spec.secretStoreRef.name == variables.tenant`, `spec.secretStoreRef.kind
  == 'SecretStore'` (namespaced), `target.name` ∈ the per-pool allowed creds names, and every
  `data[].remoteRef.key` / `dataFrom[].extract.key` STARTS-WITH `tenants/<org>/<workspace>/`.
- Enforce on BOTH CREATE and UPDATE (the merged VAP's matchConstraints already include both — assert
  it, add UPDATE tests). Keep the negative proofs (a foreign SA / cross-tenant key / wrong store name
  is rejected). Admission is defense-in-depth; the OpenBao per-workspace role ACL remains mandatory.

### B. broker operator manifests (§5, §6) — new dir `kubernetes/apps/infrastructure/agentforge-broker/`
- `namespace.yaml`: `agentforge-broker` (baseline PSA; NOT the sandbox ns).
- `serviceaccount.yaml`: `agentforge-broker` SA, `automountServiceAccountToken: false`.
- `deployment.yaml` (GATED — placeholder image, unlisted in kustomization until activation, like the
  reaper): `agentforge broker` from the DEDICATED broker image (the `broker` extra ONLY — no
  OpenBao/k8s client), 2 replicas + a `PodDisruptionBudget` + anti-affinity + topology-spread across
  nodes (§5 HA), read-only-rootfs/nonroot/cap-drop-ALL, mounts ONLY `broker-oauth` + the kid-registry,
  reload-on-change (watch the mounted files), env from a non-secret ConfigMap (audience, ledger DSN
  via ESO, worst-case tokens, ttls). AF_* wiring per `agentforge broker`'s Settings.
- `service.yaml`: ClusterIP (the DIRECT broker the sandbox reaches for source-IP TOFU).
- `externalsecret-oauth.yaml`: a SEPARATE operator-owned `SecretStore` + `ExternalSecret` syncing
  `broker-oauth` from `af/data/operator/broker/<provider>/<account>/oauth` via an operator auth role
  that NO tenant SecretStore's policy can read (distinct from any `tenants/*` role). Ledger DSN ESO too.
- `externalsecret-kids.yaml`: sync the broker's kid public-key registry from
  `af/data/operator/broker/<provider>/<account>/kids/*` (public keys ONLY — matches the provisioner's
  publish path + the `broker.config` KidRegistry schema).
- `rbac.yaml`: minimal namespaced Role (only what the app needs; NO cross-ns, NO cluster).
- `reaper close-role DB grant` (§3): the reaper's narrow close-only Postgres role grant (EXECUTE on
  `close_session` only) — a SOPS-managed SQL/ESO addition alongside the existing infra-pg wiring.

### C. Cilium agent→broker + broker→upstream (§8) — R-2 blockers, not R-3
- agent-profile egress (sandbox pods): ONLY the matching-pool broker ClusterIP + DNS L7 restricted to
  the broker's EXACT name; deny direct-IP/IPv6/alt-DNS/metadata/node-local/service-CIDR.
- broker ingress: admit ONLY sandbox pods of the matching pool/tenant via a STABLE per-pool/tenant
  identity label (NOT the high-cardinality per-job label — keep per-job labels OUT of Cilium identity).
- broker egress: the enumerated upstream API + auth/OAuth hosts (FQDN L7 + explicit deny of
  private/link-local/service/node ranges) + infra-pg (ledger) + ESO/OpenBao for the mounted secret sync.

## Critical files
- `kubernetes/apps/apps/agentforge/admission/tenant-guard.yaml` (tighten; + its test/validation).
- NEW `kubernetes/apps/infrastructure/agentforge-broker/**` (ns/sa/deploy[gated]/svc/eso/rbac/kustomization).
- NEW/edit Cilium policy files under `kubernetes/apps/infrastructure/agentforge-sandbox/cilium-egress.yaml`
  (+ a broker ingress/egress CNP).
- reaper close-role DB grant (SOPS + the databases wiring).
- `kubernetes/apps/infrastructure/*/kustomization.yaml` wiring (broker dir; deployment.yaml UNLISTED).

## Verification
- **tenant-guard**: kubeconform + a CEL/admission test matrix — a CP-shaped ExternalSecret/SecretStore
  with the CORRECT deterministic names + tenants/<org>/<ws> key is ADMITTED; each of {wrong store name,
  wrong role, foreign SA, target.name outside the pool set, remoteRef.key under another tenant or under
  operator/*, ns-name≠derive(labels), missing org/ws label} is REJECTED; UPDATE as well as CREATE.
- **broker manifests**: kubeconform valid; the deployment is UNLISTED in kustomization (gated); the
  broker SA has automount off; broker-oauth/kids ExternalSecrets reference the operator (non-tenant)
  SecretStore + paths under `operator/broker/*`; PDB + anti-affinity + topology-spread present.
- **Cilium**: policy lints; agent egress allows only the broker + DNS-to-broker-name; broker ingress
  keys on the stable pool/tenant label; broker egress enumerates upstream + infra-pg + ESO/OpenBao.
- **No activation**: nothing here flips `privilege_hardening` or lists the gated Deployments; Flux
  would reconcile the policies/ns/SA/ESO wiring but the broker Deployment + provisioner stay gated
  until the operator's manual activation (image pin + OpenBao init + the live compat gate).

## Notes
- All on `feat/p2-unlock` (the deferred P2 activation umbrella) — NOT a standalone PR to main; it
  extends the umbrella that lands as the deliberate operator-activation milestone.
- Codex Phase A on THIS plan (the "how": correlation-by-deterministic-naming vs mapping-ConfigMap,
  broker HA shape, Cilium identity labels), then Phase B on the rendered manifests (cap 3 each).

<!-- codex-review-status: pending -->
