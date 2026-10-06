# S2S service identities from OpenBao (no SOPS step per new service)

## Codex Review

- **Verdict: OpenBao credentials + ESO + gatekeeper extras hot-reload is the best fit for this estate now, with corrections below.** Projected tokens require broader client and verifier changes; a second age key or direct Bao access adds unnecessary responsibility. Prefer one atomically replaced extras document over per-file last-good state; automated pod rolls would retain the documented startup race.
- **The human gate is not yet an enforceable security boundary.** Protect credential routing, rendering and provisioning as well as grants, and prove owner approval is required before Phase 1 introduces the new access paths.
- **Revocation is underspecified.** ESO failures, malformed replacements, deletion of the final entry and kubelet propagation can retain old authority; already-issued tokens also survive registry removal.
- **Rotation does not guarantee an outage of seconds.** Both gatekeeper replicas and both ExternalSecrets converge independently, while harness uses a single-replica `Recreate` deployment with migrations. Define synchronization barriers, maintenance expectations and recovery.
- **The proposed collision CI check cannot work as written.** The SOPS registry is one encrypted scalar, so its client IDs are unavailable; substituting the dev registry would reject `svc-harness` itself. Keep runtime collision enforcement and use an independently verified inventory if CI coverage is required.

Source: dev-worker-1's proposal (2026-10-06): add a second, automatable path for new platform
service identities on ailab: OpenBao generation, External Secrets rendering, and a gatekeeper
"extras" registry. The existing SOPS set stays untouched. First consumer: `svc-harness`
(platform PR #2092, H9.1b, blocked on owner runbook steps 9 and 10).

Repos: **ailab** (this repo, origin/main 2a201189) and **platform** (`cchifor/platform`, gitea
main 78c0f56b3; a read-only checkout is at
`C:/Users/chifo/AppData/Local/Temp/claude/C--Users-chifo-work-ailab/28ad994b-62ac-44f1-8418-fdb632fe1a57/scratchpad/pmain`).

<!-- codex: Review evidence is the supplied repository snapshots; gatekeeper source and deploy/helm are absent from this ailab worktree and were inspected in the platform checkout above. No live cluster checks or operational drills were performed. -->

## Context

Today, adding an S2S identity on ailab takes the age private key twice:
1. `deploy/secrets/ailab/gatekeeper-secrets.enc.yaml` gets the registry entry (argon2id
   `secret_hash` + grants). Gatekeeper loads the registry once at startup
   (`infra/gatekeeper/src/app/core/lifecycle.py:129`), so `gatekeeper.serviceRegistry.checksum`
   in `deploy/helm/values/providers/ailab.yaml` has to be bumped to roll the pods.
2. `harness-secrets.enc.yaml` holds the plaintext client secret plus a DSN whose password lives
   in OpenBao `af/strive/pg-harness`. Only someone who can read OpenBao *and* encrypt with age
   can assemble it.

No agent holds the age key, and none should. The result is a human step for every new service
and every rotation.

### Verified facts the proposal gets wrong or leaves out (checked 2026-10-06)

| # | Fact | Consequence for the proposal |
|---|---|---|
| F1 | Registry `secret_hash` is **argon2id** (`service_verifier.py` `PreSharedSecretVerifier`, argon2-cffi). The provision Jobs run `quay.io/openbao/openbao` (bao + busybox) and have no argon2 tool. | "The Job computes its hash" needs a different image or a different hash scheme. |
| F2 | The secret and its hash are **two fields**. A rotation with `bao kv patch` has to recompute argon2 by hand or the fields diverge. | "Both read the same OpenBao field, so they cannot disagree" is false as written. |
| F3 | The harness reads `HARNESS_CLIENT_SECRET` from **env at startup** (`services/harness/src/env.ts:194`), and the cluster has **no Reloader**. | Rotation is not "picked up within the refresh interval" on the client side. The harness has to restart. |
| F4 | The cluster's ESO (chart 2.7.0) serves **only `external-secrets.io/v1`**; `v1beta1` has `served=false`. Every platform chart's `templates/externalsecret.yaml` uses `v1beta1`, and none supports `target.template`. | The charts' built-in ExternalSecret cannot be switched on as it is. A composed `database-url` needs a template anyway. |
| F5 | Gatekeeper already stores **API keys as SHA-256** of high-entropy keys (`apikeys.py:49`). | Precedent for a `sha256:` credential digest for machine-generated secrets. |
| F6 | `strive-pg-harness` already runs the full pattern: a provision Job (policy, k8s-auth role, generate-once KV), then a SecretStore, then an ExternalSecret (`SecretSynced`, Ready). No agent policy can write `af/data/strive/*`. | The ailab half is a copy of a proven shape. |
| F7 | Gatekeeper on ailab runs 2 replicas with PDB minAvailable 1. The registry is a projected Secret volume mounted without `subPath`, so kubelet propagates updates. | A file-watching reload works. Pod rolls cost no downtime. |
| F8 | PRs on these repos are merged by reviewer bots once approved. Today the age-key step is, in practice, the **only human gate** on a new S2S identity and its grants on ailab. | Removing it without a replacement lets a bot-approved PR mint a new privileged identity. The proposal treats only the OpenBao policy as the human decision, but the grants are one too. |

<!-- codex: [P2] F7's replica, PDB and volume facts do not establish zero downtime: platform deploy/helm/charts/gatekeeper/templates/deployment.yaml uses maxUnavailable: 0 for rollout availability, while lifecycle.py permits an empty registry at startup. Verify functional token minting on replacement pods; PDB protection alone does not establish that the new registry was loaded. -->

## Decision

**Adopt the proposal's direction with five corrections.** Freeze SOPS for existing identities.
New identities get a generated credential in OpenBao, ESO renders it, and gatekeeper loads it
from an additive extras registry that it hot-reloads. The corrections:

1. **One source value.** OpenBao holds only the plaintext `client-secret` (32 random bytes, hex).
   Each consumer derives what it needs from that single field. The harness Secret copies it. The
   gatekeeper extras Secret is rendered by an ESO template as `secret_hash: "sha256:{{ .x | sha256sum }}"`.
   Gatekeeper learns to verify `sha256:<64 hex>` digests with a constant-time compare, only for
   **extras** entries, and refuses a presented secret shorter than 43 characters. This removes
   F1 and F2: no argon2 tooling, nothing that can diverge, and a wipe-and-regenerate leaves both
   sides consistent. argon2 stays mandatory for the SOPS base registry.
   <!-- codex: [P2] One source removes independent hash maintenance, but byte normalization still matters: platform services/harness/src/env.ts:113 trims the secret, whereas ESO hashes its source value. Preserve the existing provisioner's whitespace stripping, validate exactly 64 lowercase hex characters before rendering, and require CSPRNG generation for rotations too; a 43-character floor does not establish entropy. -->
2. **Grants stay in the platform repo.** The gatekeeper chart renders the extras ExternalSecret
   (apiVersion `v1`, its own template file) from `gatekeeper.serviceRegistry.extras` values. A CI
   test pins each ailab extras entry's grants to the dev registry
   (`infra/gatekeeper/secrets/service_registry.yaml`). ailab owns only credential plumbing:
   OpenBao objects, SecretStores, and the harness credential Secret.
3. **A human gate on grants.** Any change to `serviceRegistry.extras` entries or to the s2s
   provision Job's service list requires an approval from the owner, not the reviewer bot.
   This uses Gitea protected file patterns or a required-approver CI check; the mechanism is
   chosen in Phase 0. It replaces the human gate that SOPS provided implicitly (F8).
   <!-- codex: [P1] Protect the full authority path, including the provision script/image and role bindings, SecretStores, ExternalSecret templates and destinations, and the approval check itself: the existing provision Job mounts openbao-breakglass-token, and platform charts can render credentials without changing extras values. Otherwise an unchanged, approved grant can be paired with an attacker-controlled secret or its plaintext routed elsewhere by a bot-approved plumbing change. -->
4. **An honest rotation story.** v1 rotation: `bao kv patch`, then force-sync both
   ExternalSecrets, then gatekeeper hot-reloads, then `kubectl rollout restart deploy/harness`.
   That leaves a harness S2S outage of seconds. Dual-secret overlap (`client-secret` +
   `previous-client-secret` rendered as `secret_hashes: [..]`) is a follow-up, not v1.
   <!-- codex: [P1] Platform deploy/helm/charts/harness/templates/deployment.yaml:18-21 uses Recreate because turns have in-memory locks, and startup includes a migration container, so restart interrupts the whole harness and has no seconds-long bound. Serialize rotations, confirm fresh target Secret contents and the intended generation on every gatekeeper replica before restarting, then wait for harness readiness and fresh minting, with explicit timeout/recovery steps if either ESO reconciliation fails. -->
5. **Robust bring-up.** The extras volume is `optional: true`, so gatekeeper boots with only the
   base registry if the ExternalSecret is not Ready yet. Hot reload covers the race between the
   Helm-rendered values and the ESO sync (the same cross-controller race documented in
   `charts/gatekeeper/templates/deployment.yaml:45-68`).

### Alternatives considered

- **Projected ServiceAccount tokens** (`ProjectedSATokenVerifier`, already in gatekeeper). This
  is the better end state: no shared secret at all. Not now, because:
  - `SVC_AUTH_BACKEND` is global. Switching one client needs a per-entry composite verifier on
    the auth hot path.
  - The JWKS fetch is an unauthenticated `httpx.get` with default CA trust, which cannot reach
    a Talos apiserver's `/openid/v1/jwks` as written.
  - The harness client speaks only `client_secret_post` (`s2s.ts:268`).

  The extras registry is designed so an entry carrying `k8s_subject` and no secret can drop in
  later.
- **A second age key for "agent-writable" SOPS files.** Rejected. It still puts a long-lived
  decryption key in agent hands, still needs the checksum bump, and still leaves the DSN
  assembly manual.
- **Gatekeeper reads OpenBao directly.** Rejected. It adds a vault client and auth to the auth
  gateway for no gain over ESO.

## Approach

PRs land strictly in order. Per the reviewbot rule, a dependent PR is not opened before its
prerequisite is live.

### Phase 0: the gate (decision, no code)
- Pick the human-approval mechanism for grant changes (Gitea `protected_file_patterns` on
  `main`, or a CI job that requires an owner approval when protected paths change). Apply it to
  both repos' relevant paths before Phase 3 merges.
  <!-- codex: [P1] Make this an implemented, tested prerequisite before Phase 1: ailab ansible/roles/pr_reviewer/files/reviewbot.py:2190 guard_findings is conditional on configured unattended authors and guarded paths, not a universal owner-approval requirement. Prove the selected Gitea mechanism rejects bot-only approval, rejects approval made stale by a new commit, and cannot be bypassed by editing its own workflow or using the existing merge credentials. -->

### Phase 1: ailab PR, the OpenBao side and the harness credential Secret
1. `kubernetes/apps/infrastructure/security/openbao/strive-s2s-provision-job.yaml`, modelled
   line for line on `strive-pg-harness-provision-job.yaml`: breakglass auth, drift guard on
   policies, roles written whole, `-cas=0` generate-once. It covers:
   - `SERVICES="harness"`. For each entry, it creates `af/strive/s2s/<svc>` once with field
     `client-secret` = `od -An -tx1 -N32 /dev/urandom` (64 hex characters).
     <!-- codex: [P2] The source provision script exits the entire process when its KV already exists (strive-pg-harness-provision-job.yaml:178-180); copying that branch into a SERVICES loop would skip later new services once harness exists. Use a per-service return/continue and test an existing first service followed by a missing second service, including a rerun after partial failure. -->
   - Policy and role `af-app-strive-s2s-harness`: read `af/data/strive/s2s/harness` and
     `af/data/strive/pg-harness`. Bound to SA `strive-s2s-harness-eso` in `strive-ailab`.
   - Policy and role `af-app-strive-gatekeeper-registry`: read `af/data/strive/s2s/*`. Bound to
     SA `strive-gatekeeper-registry-eso` in `strive-ailab`. It reads plaintexts in order to hash
     them. That is accepted because gatekeeper holds the token-signing key, so its tier
     compromise is total anyway.
     <!-- codex: [P1] ESO and this Kubernetes-auth identity are separate from the signing-key holder: the copied strive-pg-harness/eso.yaml authenticates through serviceAccountRef, so permission to mint that SA's token or author an ExternalSecret using its store exposes plaintext credentials without compromising gatekeeper. Grant explicit approved credential paths rather than a future-wide wildcard, and document/test the RBAC boundary around SecretStore use, TokenRequest, pod creation and rendered Secret access. -->
   - Registered in `security/openbao/kustomization.yaml`, with a unit test like
     `scripts/tests/test-strive-pg-harness-provision.sh`, wired into `.gitea/workflows/manifests.yaml`.
     <!-- codex: [P2] The source Job references a fixed-name ConfigMap and has ttlSecondsAfterFinished: 86400, so editing its script alone does not rerun a completed Job; force replacement only helps when the Job specification changes. Use a script hash in the pod template or specify an owned rerun procedure, and verify execution of the new revision before calling its prerequisite live. -->
2. `kubernetes/apps/infrastructure/strive-s2s/` (a new Flux Kustomization, `wait: false`,
   `clusters/ai/strive-s2s.yaml`) contains:
   <!-- codex: [P2] Copy the external-secrets dependsOn from kubernetes/apps/clusters/ai/strive-pg-harness.yaml and add the new path to scripts/tests/test_manifest_paths.py EXPECTED_LOCAL_PATHS; otherwise bootstrap can race the CRDs and the manifest-discovery test will reject the new tree. wait: false only establishes application of manifests, so later phases still need explicit SecretStore/ExternalSecret readiness checks. -->
   - Two SAs (`automountServiceAccountToken: false`), the cert-manager CA leaf, and two
     SecretStores (`strive-s2s-harness-store`, `strive-gatekeeper-registry-store`).
   - An ExternalSecret `strive-s2s-harness`, which renders a Secret of the **same name**. The
     `strive-` prefix keeps ailab-owned objects from colliding with chart-rendered names.
     `target.template` produces the keys `client-secret` and
     `database-url: postgres://harness:{{ .password }}@strive-pg-rw.strive-ailab.svc.cluster.local:5432/harness`.
     <!-- codex: [P1] Database rotation/recovery now spans two ExternalSecrets, the database bootstrap Job and harness startup: strive-pg-harness/eso.yaml refreshes hourly, while bootstrap-job.yaml samples its password from env and reruns roughly every 25 minutes. Force-sync both password consumers, rerun and verify bootstrap against the new password, then restart harness; a freshly rendered DSN alone does not prove PostgreSQL accepts it. -->
3. Docs:
   - `docs/runbooks/infra-pg.md` (the strive-pg section) and `openbao-recovery.md` get a
     GENERATED-ONCE row for `strive/s2s/*`. A wipe regenerates the value; both sides re-render
     consistently, and the harness needs a restart.
     <!-- codex: [P2] Distinguish complete loss of KV history from soft deletion or destruction of the current version: strive-pg-harness-provision-job.yaml deliberately fails when history prevents -cas=0 recreation. Add a recovery drill for each state and specify whether to restore the prior value or perform coordinated regeneration. -->
   - A new `docs/runbooks/strive-s2s.md` covers adding a service and the rotation steps.
     <!-- codex: [P2] Also own retirement and rollback: the existing provision Job explicitly states that removing its manifest does not undo Bao policies, roles or KV writes. Define cleanup ordering after registry revocation, including issued Bao tokens and suspension of reconciliation so an emergency change is not recreated by the next run. -->

### Phase 2: platform PR, gatekeeper code
- `service_registry.py`:
  - `load_registry_with_extras(base, extras_dir)`. Each `*.yaml` holds exactly one `services`
    entry, and the file stem must equal its `client_id`.
    <!-- codex: [P2] One ESO resource already produces the whole extras Secret, so per-file last-good state adds complexity without isolating upstream ESO failures. Prefer one services document using the existing ServiceRegistry schema, validate it completely and swap one immutable snapshot, including an explicit services: [] representation. -->
  - A `client_id` that is already in the base registry, or duplicated across extras files, is
    **refused** for that entry and logged. The base registry is never shadowed.
    <!-- codex: [P1] lifecycle.py:129-135 currently substitutes an empty registry on base-load failure, which would erase the set of protected base IDs if extras were then merged. Keep extras inactive until the base has been validated, or retain a previously validated base snapshot; test startup with absent/malformed base data and an extras entry claiming a base identity. -->
  - `secret_hash` is `$argon2id$…` or `sha256:<64 lowercase hex>`. The `sha256:` form is legal
    **only** in extras.
    <!-- codex: [P2] ServiceClient currently permits secret_hash: null for k8s/mTLS identities and carries no source/provenance field (service_registry.py), so enforce the SHA restriction before merging or preserve trusted origin metadata. Preserve valid secret-less entries for other backends rather than making argon2 mandatory for every base entry. -->
- `service_verifier.py`: `PreSharedSecretVerifier` reads through a `RegistryHolder`, the swap
  point for an atomic reference, instead of a registry captured at construction. The `sha256:`
  branch is `hmac.compare_digest(sha256(secret), digest)` with a minimum presented length of 43.
  <!-- codex: [P1] An atomic holder swap is not sufficient if authentication and authorization dereference it separately: service_token.py captures state, awaits verify(), then looks up grants, and repeats this pattern for delegation issue/exchange/revoke. Pin one immutable snapshot for each complete request, and either adapt ProjectedSATokenVerifier/MtlsVerifier too (both capture registries) or explicitly reject extras configuration with those backends. -->
- A hot-reload task in `lifecycle.py`:
  - It polls the extras dir every 30 s, keyed on the content hash of each file.
    <!-- codex: [P1] The no-subPath Secret mount in deploy/helm/charts/gatekeeper/templates/deployment.yaml is updated through kubelet's symlink generations; a scan can race a generation change and combine files from different revisions or encounter ENOENT. Read a coherent generation and retry interrupted scans, testing real ..data symlink replacement rather than only overwriting ordinary files. -->
  - On a parse or validation error it keeps the last good copy of that file.
    <!-- codex: [P1] Last-good retention means a malformed credential replacement or grant reduction leaves the old authority valid indefinitely, unlike the current lifecycle.py startup fallback to no clients. Specify the stale-authority policy and emergency revocation route, and test malformed replacement followed by restart, since in-memory last-good state disappears on restart. -->
  - A file that disappears removes its entry, which is how revocation works.
    <!-- codex: [P1] Distinguish an authoritative empty registry from read/enumeration failure and removal of the Secret object: optional: true permits boot without a Secret but does not by itself prove that deletion empties every running mount. Keep a valid empty document in the existing target Secret when removing the final identity, and separately drill extras.enabled: false and Secret/ExternalSecret deletion. -->
  - Metrics: `gatekeeper_service_registry_extras_entries`,
    `gatekeeper_service_registry_reload_errors_total`.
    <!-- codex: [P2] Entry count and parse-error count cannot distinguish two replicas holding different valid credentials or an ESO failure that leaves unchanged files. Expose the applied nonsecret revision and last successful reload, alert on ESO readiness/staleness, and supervise/cancel the reload task through lifecycle.py's lifespan so a dead task cannot silently freeze authorization. -->
  - `service_token.py` reads `app.state.service_registry` through the holder.
- Config: `SERVICE_REGISTRY_EXTRAS_DIR` (unset means today's behaviour exactly).
- Tests: merge, collision refusal, the `sha256:` path and its length floor, `sha256:` rejected in
  the base registry, reload, last-good retention on error, removal, and a missing dir.

### Phase 3: platform PR, chart and ailab values (after the Phase 2 image is built)
- `charts/gatekeeper`:
  - New `templates/registry-extras-externalsecret.yaml` (`external-secrets.io/v1`), gated by
    `serviceRegistry.extras.enabled`.
  - `storeRef`, plus `entries: {svc-harness: {remoteKey: strive/s2s/harness, property: client-secret, audiences: …, may_act_for_audiences: …}}`.
    The template renders one data key `<client_id>.yaml` per entry. Helm escapes the ESO
    delimiters. The only dynamic value is the `sha256sum` hex, so the template cannot be
    injected into.
    <!-- codex: [P1] Multiple remoteRefs in one ExternalSecret share a reconciliation outcome; an absent or unreadable new credential can prevent the entire target update, including another identity's revocation, and per-file reload cannot repair that upstream coupling. Define this failure domain, provision/validate inputs before enabling them, and test revocation while an unrelated remaining remoteRef fails; use separate resources only if independent availability is required. -->
  - The deployment mounts Secret `gatekeeper-registry-extras` as `optional: true` at
    `/var/run/secrets/gatekeeper-extras` and sets `SERVICE_REGISTRY_EXTRAS_DIR`.
- `values/providers/ailab.yaml`:
  - Pin the new gatekeeper digest.
  - Enable extras with the `svc-harness` grants copied from the dev registry: mcp, integration,
    airlock, workflow, knowledge, profile, notification `[read, write]`, digest `[digest:read]`,
    plus `may_act_for_audiences`.
    <!-- codex: [P1] Review these as client_credentials grants as well as OBO grants: service_token.py:202-245 accepts a caller-supplied tenant_id and mints registry scopes without a user token, despite the dev registry's nearby comment claiming a stolen harness credential needs a live person. Grant parity is not a confinement proof; explicitly approve that authority or add and test the intended grant-type restriction. -->
  - `storeRef: strive-gatekeeper-registry-store`.
  - Harness `secretEnv` points at Secret `strive-s2s-harness`. `externalSecret` stays `false`.
    <!-- codex: [P1] The credential remains plaintext on the harness-to-gatekeeper hop: charts/harness/values.yaml:126 selects HTTP and services/harness/src/plugins/identity-gatekeeper/s2s.ts sends client_secret in the form body; ailab's checked-in Cilium bootstrap values do not enable encryption. Record this existing transport exposure explicitly and either verify an actual encrypted transport or own the TLS/network-encryption work separately, rather than implying OpenBao TLS protects the entire path. -->
- A CI test asserts that every ailab extras entry's grants equal the dev registry entry with the
  same client_id, and that no extras client_id appears in the base registry's client list. The
  base check uses the client_id names from the SOPS key structure, which stays unencrypted, or
  falls back to the dev registry.
  <!-- codex: [P1] deploy/secrets/ailab/gatekeeper-secrets.enc.yaml:10 encrypts the entire service-registry scalar, so no nested client_id names are visible, and infra/gatekeeper/secrets/service_registry.yaml:281 already contains svc-harness, making the fallback reject the intended first consumer. Use runtime collision validation plus an owner-verified public base-ID inventory if needed, and perform a live collision check without exposing registry secret hashes. -->
- Update the overlay comment and `SECRETS.md`: the "no ESO on ailab" line is stale, and there is
  a new "OpenBao-backed identities" section.

### Phase 4: verify, then #2092
- Before the flip, from a throwaway pod in `strive-ailab` (netpol permitting), or with the
  existing persona tooling, `POST /auth/token` with `client_id=svc-harness` and the Secret's
  `client-secret` must return 200, and a wrong secret must return 401.
  <!-- codex: [P2] service_token.py:128-214 also requires grant_type and audience, plus tenant_id for client_credentials; the stated request otherwise returns validation errors rather than 200. Supply a complete request and verify JWT identity, scopes and tenant, then exercise denied audience/scope and OBO scope intersection against each replica. -->
  <!-- codex: [P2] ailab kubernetes/apps/infrastructure/platform-access/rbac.yaml deliberately denies dev workers Secret reads, pod creation/exec, deployment patches and serviceaccounts/token, so neither this throwaway-pod check nor the rotation commands are currently worker-executable. Name an operator or a reviewed, narrowly scoped verification Job, and give its network policy the required access without placing the probe in harness Service endpoints. -->
- Rebase #2092 (`harness.enabled: true`), rewrite its precondition section, and merge it.
  Then run the post-merge checks already listed in #2092.

## Critical files

ailab:
- `kubernetes/apps/infrastructure/security/openbao/strive-s2s-provision-job.yaml` (new)
- `security/openbao/kustomization.yaml`
- `kubernetes/apps/infrastructure/strive-s2s/{eso.yaml,kustomization.yaml}` (new)
- `kubernetes/apps/clusters/ai/strive-s2s.yaml` (new)
- `scripts/tests/test-strive-s2s-provision.sh` (new)
- `.gitea/workflows/manifests.yaml`
- `docs/runbooks/{infra-pg,openbao-recovery,strive-s2s}.md`

platform:
- `infra/gatekeeper/src/app/gatekeeper/{service_registry,service_verifier,service_token,config}.py`
- `infra/gatekeeper/src/app/core/lifecycle.py`
- `infra/gatekeeper/tests/unit/…`
- `deploy/helm/charts/gatekeeper/{values.yaml,templates/deployment.yaml,templates/registry-extras-externalsecret.yaml}`
- `deploy/helm/values/providers/ailab.yaml`
- `deploy/helm/scripts/tests/…` (the grants-parity check)
- `deploy/secrets/ailab/SECRETS.md`

## Verification

- Phase 1:
  - The provision Job's logs show the policy and role written and verified, and the KV created
    once. A re-run is a no-op.
    <!-- codex: [P2] scripts/tests/test-strive-pg-harness-provision.sh is a Docker-backed integration test, and its least-privilege check creates a policy token directly instead of exercising Kubernetes authentication. Extend it for concurrent CAS creation and malformed existing documents, then separately prove real ESO SA login succeeds while wrong-SA/wrong-namespace login and sibling credential reads fail. -->
  - `kubectl -n strive-ailab get externalsecret strive-s2s-harness` shows `SecretSynced`.
  - The rendered `database-url` connects: `psql` from a pod as `harness`, or the
    `strive-pg-harness-bootstrap` Job's checks.
- Phase 2: gatekeeper unit tests are green in CI. A local compose run with an extras dir
  mints for an extras client and refuses a colliding one.
- Phase 3:
  - Helm render tests are green.
    <!-- codex: [P2] Neither the copied strive-pg-harness/eso.yaml nor the existing platform harness ExternalSecret exercises ESO templating, so Helm success does not prove the new two-stage template works. Run an ESO 2.7.0 integration check with engineVersion: v2 and mergePolicy: Replace explicitly set, assert the SHA matches the exact harness bytes, and assert the registry Secret contains only intended YAML keys without plaintext input aliases. -->
  - The live gatekeeper pods log `service_registry loaded … extras=1` and the extras gauge
    reads 1.
  - The other S2S paths are unaffected: the existing e2e lane passes and
    `report-ailab-pin-drift` shows 0 torn.
    <!-- codex: [P2] Add cold-start and rollback drills for the documented cross-controller race: start with the extras Secret absent, publish it after both replicas are Ready, and prove minting converges without a roll. Also verify behavior with ESO/Bao unavailable and a retained Secret, and document that reverting to the pre-Phase-2 image drops extras support and therefore disables harness S2S. -->
- Rotation drill (before the plan is closed): run the v1 steps against svc-harness and confirm
  the mint is refused only between the gatekeeper reload and the harness restart.
  <!-- codex: [P1] services/harness/src/plugins/identity-gatekeeper/s2s.ts caches issued tokens, so a successful ordinary request can hide a broken credential after rotation. Force fresh minting with old/new credentials against every gatekeeper replica, deliberately delay one ESO reconciliation and one replica's reload, and measure recovery through harness readiness rather than through issuance of the restart command. -->
- Revocation drill: remove the extras entry in values, and confirm the mint returns 401 within
  roughly 30 s plus the ESO sync, with no pod roll.
  <!-- codex: [P1] The latency also includes Git/Helm reconciliation and kubelet propagation before each replica's poll, while service_token.py mints bearer JWTs with the configured TTL (300 seconds in the gatekeeper chart), which registry removal does not invalidate. Measure rejection of new mints on every replica separately from expiry of previously issued tokens, including the final-entry case and a failed ESO sync; state the emergency procedure when normal convergence stalls. -->

<!-- codex-review-status: complete -->