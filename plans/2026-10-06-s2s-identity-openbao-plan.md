# S2S service identities from OpenBao (no SOPS step per new service)

Source: dev-worker-1's proposal (2026-10-06): add a second, automatable path for new platform
service identities on ailab: OpenBao generation, External Secrets rendering, and a gatekeeper
"extras" registry. The existing SOPS set stays untouched. First consumer: `svc-harness`
(platform PR #2092, H9.1b, blocked on owner runbook steps 9 and 10).

Repos: **ailab** (this repo, origin/main 2a201189) and **platform** (`cchifor/platform`, gitea
main 78c0f56b3; a read-only checkout is at
`C:/Users/chifo/AppData/Local/Temp/claude/C--Users-chifo-work-ailab/28ad994b-62ac-44f1-8418-fdb632fe1a57/scratchpad/pmain`).

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
2. **Grants stay in the platform repo.** The gatekeeper chart renders the extras ExternalSecret
   (apiVersion `v1`, its own template file) from `gatekeeper.serviceRegistry.extras` values. A CI
   test pins each ailab extras entry's grants to the dev registry
   (`infra/gatekeeper/secrets/service_registry.yaml`). ailab owns only credential plumbing:
   OpenBao objects, SecretStores, and the harness credential Secret.
3. **A human gate on grants.** Any change to `serviceRegistry.extras` entries or to the s2s
   provision Job's service list requires an approval from the owner, not the reviewer bot.
   This uses Gitea protected file patterns or a required-approver CI check; the mechanism is
   chosen in Phase 0. It replaces the human gate that SOPS provided implicitly (F8).
4. **An honest rotation story.** v1 rotation: `bao kv patch`, then force-sync both
   ExternalSecrets, then gatekeeper hot-reloads, then `kubectl rollout restart deploy/harness`.
   That leaves a harness S2S outage of seconds. Dual-secret overlap (`client-secret` +
   `previous-client-secret` rendered as `secret_hashes: [..]`) is a follow-up, not v1.
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

### Phase 1: ailab PR, the OpenBao side and the harness credential Secret
1. `kubernetes/apps/infrastructure/security/openbao/strive-s2s-provision-job.yaml`, modelled
   line for line on `strive-pg-harness-provision-job.yaml`: breakglass auth, drift guard on
   policies, roles written whole, `-cas=0` generate-once. It covers:
   - `SERVICES="harness"`. For each entry, it creates `af/strive/s2s/<svc>` once with field
     `client-secret` = `od -An -tx1 -N32 /dev/urandom` (64 hex characters).
   - Policy and role `af-app-strive-s2s-harness`: read `af/data/strive/s2s/harness` and
     `af/data/strive/pg-harness`. Bound to SA `strive-s2s-harness-eso` in `strive-ailab`.
   - Policy and role `af-app-strive-gatekeeper-registry`: read `af/data/strive/s2s/*`. Bound to
     SA `strive-gatekeeper-registry-eso` in `strive-ailab`. It reads plaintexts in order to hash
     them. That is accepted because gatekeeper holds the token-signing key, so its tier
     compromise is total anyway.
   - Registered in `security/openbao/kustomization.yaml`, with a unit test like
     `scripts/tests/test-strive-pg-harness-provision.sh`, wired into `.gitea/workflows/manifests.yaml`.
2. `kubernetes/apps/infrastructure/strive-s2s/` (a new Flux Kustomization, `wait: false`,
   `clusters/ai/strive-s2s.yaml`) contains:
   - Two SAs (`automountServiceAccountToken: false`), the cert-manager CA leaf, and two
     SecretStores (`strive-s2s-harness-store`, `strive-gatekeeper-registry-store`).
   - An ExternalSecret `strive-s2s-harness`, which renders a Secret of the **same name**. The
     `strive-` prefix keeps ailab-owned objects from colliding with chart-rendered names.
     `target.template` produces the keys `client-secret` and
     `database-url: postgres://harness:{{ .password }}@strive-pg-rw.strive-ailab.svc.cluster.local:5432/harness`.
3. Docs:
   - `docs/runbooks/infra-pg.md` (the strive-pg section) and `openbao-recovery.md` get a
     GENERATED-ONCE row for `strive/s2s/*`. A wipe regenerates the value; both sides re-render
     consistently, and the harness needs a restart.
   - A new `docs/runbooks/strive-s2s.md` covers adding a service and the rotation steps.

### Phase 2: platform PR, gatekeeper code
- `service_registry.py`:
  - `load_registry_with_extras(base, extras_dir)`. Each `*.yaml` holds exactly one `services`
    entry, and the file stem must equal its `client_id`.
  - A `client_id` that is already in the base registry, or duplicated across extras files, is
    **refused** for that entry and logged. The base registry is never shadowed.
  - `secret_hash` is `$argon2id$…` or `sha256:<64 lowercase hex>`. The `sha256:` form is legal
    **only** in extras.
- `service_verifier.py`: `PreSharedSecretVerifier` reads through a `RegistryHolder`, the swap
  point for an atomic reference, instead of a registry captured at construction. The `sha256:`
  branch is `hmac.compare_digest(sha256(secret), digest)` with a minimum presented length of 43.
- A hot-reload task in `lifecycle.py`:
  - It polls the extras dir every 30 s, keyed on the content hash of each file.
  - On a parse or validation error it keeps the last good copy of that file.
  - A file that disappears removes its entry, which is how revocation works.
  - Metrics: `gatekeeper_service_registry_extras_entries`,
    `gatekeeper_service_registry_reload_errors_total`.
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
  - The deployment mounts Secret `gatekeeper-registry-extras` as `optional: true` at
    `/var/run/secrets/gatekeeper-extras` and sets `SERVICE_REGISTRY_EXTRAS_DIR`.
- `values/providers/ailab.yaml`:
  - Pin the new gatekeeper digest.
  - Enable extras with the `svc-harness` grants copied from the dev registry: mcp, integration,
    airlock, workflow, knowledge, profile, notification `[read, write]`, digest `[digest:read]`,
    plus `may_act_for_audiences`.
  - `storeRef: strive-gatekeeper-registry-store`.
  - Harness `secretEnv` points at Secret `strive-s2s-harness`. `externalSecret` stays `false`.
- A CI test asserts that every ailab extras entry's grants equal the dev registry entry with the
  same client_id, and that no extras client_id appears in the base registry's client list. The
  base check uses the client_id names from the SOPS key structure, which stays unencrypted, or
  falls back to the dev registry.
- Update the overlay comment and `SECRETS.md`: the "no ESO on ailab" line is stale, and there is
  a new "OpenBao-backed identities" section.

### Phase 4: verify, then #2092
- Before the flip, from a throwaway pod in `strive-ailab` (netpol permitting), or with the
  existing persona tooling, `POST /auth/token` with `client_id=svc-harness` and the Secret's
  `client-secret` must return 200, and a wrong secret must return 401.
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
  - `kubectl -n strive-ailab get externalsecret strive-s2s-harness` shows `SecretSynced`.
  - The rendered `database-url` connects: `psql` from a pod as `harness`, or the
    `strive-pg-harness-bootstrap` Job's checks.
- Phase 2: gatekeeper unit tests are green in CI. A local compose run with an extras dir
  mints for an extras client and refuses a colliding one.
- Phase 3:
  - Helm render tests are green.
  - The live gatekeeper pods log `service_registry loaded … extras=1` and the extras gauge
    reads 1.
  - The other S2S paths are unaffected: the existing e2e lane passes and
    `report-ailab-pin-drift` shows 0 torn.
- Rotation drill (before the plan is closed): run the v1 steps against svc-harness and confirm
  the mint is refused only between the gatekeeper reload and the harness restart.
- Revocation drill: remove the extras entry in values, and confirm the mint returns 401 within
  roughly 30 s plus the ESO sync, with no pod roll.

<!-- codex-review-status: pending -->
