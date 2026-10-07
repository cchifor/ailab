# Strive auth hardening (F-01 to F-06), owner workstation credentials, S2S rollback drill 2, edge 5xx observability

**Status: FINALIZED** after two review rounds by Codex and Fable (the response logs are at the end). Nothing in this plan has
been executed except E0 (read-only evidence preservation). Every live observation below was read-only (`kubectl --context admin@ai` get/describe/logs,
`get --raw` through the API-server service proxy to Prometheus and Loki, read-only `psql` on the Gitea
database, public HTTPS endpoints). No Secret value, token or password was read or printed.

## Context

The Strive platform authentication specification (draft, 2026-10-06; `docs-cchifor/specifications/platform-authentication/`,
pages `findings.md`, `keys-and-rotation.md`, `user-authentication.md`, `service-to-service.md`,
`kubernetes-and-delivery.md`) lists five High findings and one Medium finding that the owner put in
scope, plus three operational follow-ups of the S2S identity work:

1. **Auth security findings**: F-01 (Valkey has no authentication and admits any source), F-02 (forgeable
   API-key records, header-trusting management API), F-03 (signing key never rotated; rotation runbook and
   automation broken), F-04 (test bypass enabled in production), F-05 (roots of trust without rotation
   procedures), F-06 (no PKCE, no nonce, `state` is the return path).
2. **Owner workstation credentials** (finding F-30 and the "Still open" list in `ailab:docs/runbooks/s2s-identity.md:342-354`).
3. **Rollback drill 2** of the signed S2S plan (`ailab:plans/2026-10-06-s2s-identity-openbao-plan.md`,
   "Verification and drills"; runbook `ailab:docs/runbooks/s2s-identity.md:598-646`), not yet run.
4. **Edge 5xx observability** (finding F-31): edge 5xx on 2026-10-06 18:10Z could not be attributed.

**Since the draft:** platform #2125 (D.8) merged as `a1014dfed` and is LIVE since 2026-10-07 07:03Z: the ten
Python services authenticate to gatekeeper with projected ServiceAccount tokens and the SOPS base registry is
`services: []` (V61). Facts and steps that depend on it say **after #2125**. It removes the live preshared
rotation problem (D5, A5.4), widens the TokenReview limiter's scope (D16) and changes what a config-only rollback
must restore (D13, item 3).

**Pinned references.** platform `gitea/main` = `a1014dfed` (2026-10-07, the #2125 merge); ailab `origin/main` =
`c5a397a1`. Re-checked at platform `b5b70c0c4` (round 2): its four later commits touch only `services/workflow` tests and
`scripts/packs`/`packs/**`, no cited path. The gatekeeper code, charts, Flux files and scripts cited are unchanged since the draft's `5b453c03f`;
`ailab.yaml` and worker-manifest line numbers are re-pinned to `a1014dfed`. Citations: `platform:<path>:<lines>`
and `ailab:<path>:<lines>` at those commits; **LIVE** = read on 2026-10-07 unless dated; **UNVERIFIED** = not
confirmed from code or the live system.

**Change-control rules that shape every step** (from the S2S work, not repeated per step):
- Platform owner-protected file patterns (26, `ailab:docs/runbooks/s2s-identity.md:137-164`) can only be
  merged by the owner in person (admin override). Of the files this plan touches, PROTECTED today are:
  `deploy/helm/templates/_helpers.tpl`, `deploy/helm/charts/gatekeeper/**`, `deploy/secrets/ailab/**`,
  `deploy/gitops/flux/clusters/ailab/**`, `infra/gatekeeper/src/app/gatekeeper/config.py`,
  `infra/gatekeeper/src/app/gatekeeper/tokenreview_verifier.py`, `infra/gatekeeper/src/app/core/lifecycle.py`,
  `infra/gatekeeper/src/app/main.py`. NOT protected today (bot-approvable): `routes.py`, `routes_session.py`,
  `helpers.py`, `jwks.py`, `oidc.py`, `server_session.py`, `redis.py`, `apikeys*.py`, `metrics.py`,
  `infra/gatekeeper/src/app/api/v1/api.py`, `deploy/components/**`, `infra/keycloak-sync/**`,
  `deploy/helm/values/providers/ailab.yaml` (but `ailab.yaml` and `deploy/components/workers/*.yaml` need the
  owner's `approve-pin`, `platform:docs/runbooks/owner-ack.md:12-62`). D15 protects most of these before AG1a.
- Every image-pin move in `ailab.yaml` needs a `<!-- pin-bump:v1 -->` block (`platform:docs/runbooks/ailab-pin-bump.md`).
- Reviewbot rule: never open a dependent PR before its prerequisite is live.
- A SOPS file is never restored by `git revert` once another commit has touched it since (a rotation, a re-encryption,
  a recipient change): the old ciphertext would undo those changes or no longer decrypt. Edit the current file with
  `sops` instead, changing only the intended key, and recompute any checksum from the new ciphertext.
- "Owner" means `chifor` acting in person. "Owner merge" defaults to the Gitea web UI (no token on disk), per item 2.

**Out of scope**: F-07 to F-31 except where an item needs them (F-17 and F-20 are touched by F-02 and F-01; F-25 by
D16; F-30 is item 2; F-31 is item 4); Valkey TLS and in-cluster transport encryption (F-16); the owner's kubeconfig,
talosconfig and age key as workstation credentials (a residual in item 2); the hand-applied Strive tunnel (F-12);
the Forge realm.

## Verified facts

| # | Fact | Evidence |
| --- | --- | --- |
| **F-01 Valkey** | | |
| V1 | Valkey runs `bitnamilegacy/valkey:8.0.1` via chart `valkey` 1.0.3, standalone, `auth.enabled: false`; the comment says the helper builds a password-less URL for every consumer, that gatekeeper "blocked at boot" with auth on (historical, see V8), and that Valkey is "NetworkPolicy-gated". | `platform:deploy/components/valkey/helmrelease.yaml:38-82`; LIVE HelmRelease Ready, 1.0.3 |
| V2 | NetworkPolicy `valkey` ingress rule has no `from` (any source on 6379); egress `{}`; no CiliumClusterwideNetworkPolicies. | LIVE `get netpol valkey -o yaml`; `get ciliumclusterwidenetworkpolicies` empty |
| V3 | Chart default `networkPolicy.allowExternal: true`. With `false` the rule admits only pods labelled `valkey-client: "true"`, Valkey's own pods, and anything in `networkPolicy.extraIngress`. | bitnami `valkey` 1.0.3 `values.yaml:1492-1552`, `templates/networkpolicy.yaml` (pulled from OCI; the cluster uses the HTTPS index of the same version; byte equality UNVERIFIED, A1.2 renders from the index) |
| V4 | `REDIS_URL` is a literal, password-less `redis://valkey-master.<ns>.svc.cluster.local:6379` rendered by the owner-protected helper for every chart that includes `strive.envFromGatekeeper` (24 includes, gatekeeper among them); the four worker manifests and airlock's `APP__AIRLOCK__RATE_LIMIT_REDIS_URL` set the same literal. | `platform:deploy/helm/templates/_helpers.tpl:293-294`; `deploy/helm/charts/gatekeeper/templates/deployment.yaml:97`; `deploy/components/workers/{digest,integration,mcp,workflow}-worker.yaml` (`REDIS_URL` at :111, :106, :94, :179); `ailab.yaml:1932-1933` |
| V5 | 16 workloads (17 pods) carry a Redis URL env: airlock, deepagent, digest, digest-worker, gatekeeper (x2), integration, integration-worker, knowledge, mcp, mcp-worker, notification, profile, sentinel, tms, workflow, workflow-worker, all labelled `strive.io/service=<name>`. No CronJob or Job in `strive-ailab` carries a Redis env name (airlock-reaper, ci-objectstore-expiry, workflow-artifact-expiry, e2e-runner, k6-weekly-soak, openbao-platform-pg-sync); their pods carry other labels or none. No pod outside `strive-ailab` targets this Valkey. | LIVE pod and CronJob/Job env NAMES and labels |
| V6 | TMS reads `APP__TMS__REDIS_URL` (default `redis://redis:6379`) and has no such env; no Service `redis` exists, so TMS most likely never reaches Valkey (spec open question 2). Inferred. | `platform:infra/tms/src/app/core/config/domain.py:119`, `loader.py:35-36`, `infra/tms/config/default.yaml:108`; LIVE |
| V7 | Gatekeeper logs the full Redis URL at INFO twice; a password in the URL would reach Loki. | `platform:infra/gatekeeper/src/app/gatekeeper/redis.py:467,664` |
| V58 | Gatekeeper logs the full bearer `session_id` at INFO on issue and delete (and at WARNING on decrypt/limit paths). Loki holds 147 `server_session_issued session_id=` lines from the last 24 h and is reachable from the LAN without authentication (NodePort `monitoring/loki-lan` 30310). | `server_session.py:148-150,311,351-380`; `routes_session.py:80`; LIVE Loki `count_over_time` (count only), `get svc -A` |
| V8 | Gatekeeper silently falls back to per-process memory on any `redis.ConnectionError`/`TimeoutError`/`OSError` and reconnects with exponential backoff capped at 60 s; readiness always answers 200, so pods stay Ready. Fallback logins live in one pod's memory and are lost on reconnect; fallback deletes are not replayed. The counters `gatekeeper_redis_fallback_total` and `gatekeeper_redis_reconnections_total` are declared but **never incremented** (series exist, always 0): today nothing shows a gatekeeper in fallback. | `redis.py:34-39,455-548`; `metrics.py:110-118` (no other reference); `api/v1/endpoints/health.py:58-77`; LIVE Prometheus |
| V63 | Every Python consumer uses redis-py: 7.4.0 in the `uv.lock` of gatekeeper, tms, airlock, deepagent, integration, knowledge, mcp, notification, profile, sentinel, workflow and the weld SDKs; **8.0.0 in digest**. airlock and mcp also degrade silently without Redis (`AIRLOCK_ALLOW_MEMORY_RATE_LIMIT=1`; mcp warns "Redis client unavailable"). | `uv.lock` files at `a1014dfed`; `ailab.yaml:1930-1933`; `services/mcp/src/app/services/capability_events.py:70` |
| V9 | Valkey persistence: AOF on (`appendonly yes`, `save ""`), PVC 8Gi `nfs-csi`; one replica, so any StatefulSet template change restarts the only pod. | LIVE ConfigMap `valkey-configuration`, PVC, StatefulSet |
| V10 | While user `default` is `nopass`, two-argument `AUTH default <anything>` succeeds and one-argument `AUTH <x>` errors (Redis ACL docs). UNVERIFIED for this Valkey build; A1.3 proves it. | Redis ACL documentation |
| **F-02 API keys** | | |
| V11 | The management API takes tenant and owner from `X-Gatekeeper-Tenant`/`X-Gatekeeper-User-Id` with no role check, contrary to its docstring; it is mounted under `/api/v1`. | `platform:infra/gatekeeper/src/app/gatekeeper/apikeys_api.py:1-9,117-196`; `api/v1/api.py:4,9`; `main.py:72` |
| V12 | Nothing in `apps/web`, `services/*` or `infra/tms` calls `/api-keys`; no `/api/v1/api-keys` route in gatekeeper's RED metrics for 7 days; spec: no `api_key` `/auth` traffic in 30 days. API-key tokens carry a slug weld rejects (F-17); `platform__app_import` is unreachable on ailab. | `git grep`; LIVE Prometheus by route; `ailab.yaml:188-189` |
| V13 | The `X-API-Key` track runs first in `/auth`; when the header is present only that track runs. | `routes.py:741-784` |
| **F-03 signing key** | | |
| V14 | Public JWKS has one ES256 key, `kid` `8b77549a4272d160`; the SOPS file last changed 2026-06-18 (`5065f9982`). | LIVE `GET https://strive.place/auth/jwks`; `git log` |
| V15 | `FileKeyRing` reads `active.pem` (required), `retiring.pem`, `pending.pem` once at startup (no hot reload); all loaded keys are published; only `active` signs; `kid` = first 16 hex of sha256 of the public key's SPKI DER. | `platform:infra/gatekeeper/src/app/gatekeeper/key_store.py:99-110,113-205` |
| V16 | The whole Secret `gatekeeper-signing-keys` is mounted (no `items`), `optional: true`; no checksum annotation follows it. Gatekeeper rolls `maxUnavailable: 0`, 2 replicas, PDB `minAvailable: 1`. | `platform:deploy/helm/charts/gatekeeper/templates/deployment.yaml:19,184-216` |
| V17 | Every gatekeeper-side verification uses the full published set, so a `pending` or `retiring` key verifies; a cached token whose `kid` left the ring is re-minted. | `internal_token_cache.py:70-103`; `service_token.py:558-564` |
| V18 | Verifier caches: weld 600 s lifespan, 1800 s stale (only while gatekeeper is unreachable), unknown-`kid` refetch without cooldown; harness 600 s, and after a successful unknown-`kid` refetch further unknown kids are refused for 60 s. Both fetch gatekeeper's JWKS in-cluster (no HTTP cache). Internal JWT `exp - iat` <= 300 s, weld skew 30 s. | `platform:sdks/weld-auth/src/weld/auth/jwks.py:33-34`; `services/harness/src/plugins/identity-gatekeeper/jwks.ts:17-23,73-86`; `deploy/helm/charts/harness/values.yaml:125` |
| V19 | The keygen CronJob is disabled on ailab; the values comment's self-generation claim is false (the kustomization says pre-seed). The Secret is Flux-applied from SOPS by `platform-secrets`, so an in-cluster patch would be reverted. | `ailab.yaml:444-449`; `platform:deploy/secrets/ailab/kustomization.yaml:33-37` |
| **F-04 test bypass** | | |
| V20 | `TEST_BYPASS_ENABLED=true`, token from `gatekeeper-secrets/test-bypass-token`, tenant allowlist = operator tenant, paths `/sandbox/,/api/airlock/,/api/v1/apps/`; evaluated before the session; 165 successes in 7 days (spec). | `ailab.yaml:415-441`; `routes.py:454-472,786-855` |
| V21 | Sentinel reaches `strive.place` through the in-cluster Traefik: Chromium maps it to `traefik.platform-edge.svc`, httpx uses `hostAliases` 10.97.5.57. | `ailab.yaml:2237,2256`; LIVE `svc/traefik` |
| V22 | The platform-edge Traefik is `ClusterIP` only; no NodePort or LoadBalancer Service in the cluster targets it; internet traffic arrives through the two `edge` cloudflared Deployments (`cloudflared`, `cloudflared-strive`). Cloudflare adds `CF-Ray`, `CF-Connecting-IP` and `CDN-Loop` to every proxied request (the guest limiter already relies on `cf-connecting-ip`, `ailab.yaml:314-359`). | LIVE `get svc -A`, `get deploy -n edge`; `deploy/components/traefik/helmrelease.yaml:35-38` |
| V23 | The `gatekeeper-auth` middleware sets only `authResponseHeaders`; Traefik forwards every request header to `/auth` (Traefik docs; UNVERIFIED in-repo, A4.2 proves it). | LIVE middleware |
| **F-05 roots of trust** | | |
| V24 | One age recipient (`age1nfa6hh...`) encrypts all 74 ailab SOPS files and the 23 platform `deploy/secrets/ailab/*.enc.yaml`. The key file is at `C:\Users\chifo\work\home\ailab\kubernetes\infra\_out\age.agekey`; `flux-system/sops-age` holds one key `age.agekey`, hand-applied. Other holders are not inventoried (A5.2 step 0). | `.sops.yaml` (both repos); LIVE Secret metadata |
| V25 | Talos `rotate-ca --kubernetes` rotates only the Kubernetes API CA; no Talos procedure exists for the ServiceAccount signing key. | Talos v1.11 "CA rotation" docs |
| V26 | Realm `strive` publishes one RS256 signing key and one RSA-OAEP key. Gatekeeper caches Keycloak JWKS 900 s; an unknown `kid` forces one refetch, and the 60 s cooldown is armed **only when the kid is still absent after that refetch** (a fabricated kid arms it, a real new kid does not); a token that does not verify after the refetch terminates the session. | LIVE realm certs; `platform:infra/gatekeeper/src/app/gatekeeper/jwks.py:100-167` (arming at :160-166) |
| V27 | **After #2125:** no ailab client is preshared (V61). Any `gatekeeper-secrets` edit must bump `gatekeeper.serviceRegistry.checksum` (sha256 of the whole ciphertext, CI-guarded), which rolls gatekeeper; consumer pods are not rolled by a Secret change. | `deploy/helm/scripts/check-service-registry-checksum.sh:1-45`; spec |
| V28 | Session, guest and delegation encryption use single `Fernet` objects (no `MultiFernet`); rotating a key invalidates every session or grant. | `platform:infra/gatekeeper/src/app/core/lifecycle.py:163,181,210`; `config.py:328-333` |
| **F-06 OIDC** | | |
| V29 | The authorize redirect carries no `code_challenge`, no `nonce`, and `state` is the return path; built in `build_login_url` (which also passes `kc_idp_hint`) from `GET /auth/login` and the ForwardAuth login redirect. | LIVE `curl -sD- https://strive.place/`; `helpers.py:119-165`; `routes.py:358-401,1611-1650` |
| V30 | `/callback` only applies the open-redirect check to `state`, exchanges the code, decodes the access token without verification, stores the ID token unverified, and may refresh tokens after the tenant-assignment Admin API hook. | `routes.py:1652-1810`; `helpers.py:409-422` |
| V62 | `exchange_code` has no `code_verifier` parameter and sends none. `validate_state` only rejects a missing leading `/` and a `//` prefix, so `/\evil.example` passes. Today's only sink is `RedirectResponse(url=safe_state)`, and Starlette percent-encodes `\`, so the browser gets `Location: /%5Cevil.example`, a same-host path: not exploitable today. The F-06 error page's link would be a sink where `/\` reads as `//`, so the hardening ships with it in AG1b. | `oidc.py:26-76`; `helpers.py:409-422`; `routes.py:1831` |
| V31 | `verify_token` passes audience but no issuer; the same `issuer_url` argument is the JWKS fetch base. Live issuer `https://auth.strive.place/realms/strive`. | `jwks.py:180-225`; spec (LIVE) |
| V32 | keycloak-sync reconciles client redirect URIs (`sync_client_redirect_uris`) and its bootstrap demands S256 PKCE for public clients: the pattern for enforcing PKCE on `gatekeeper`. Its Job is re-applied by Kustomization `platform-identity` every 10-20 minutes. | `platform:infra/keycloak-sync/src/keycloak_realm_sync/main.py:576-729`; `bootstrap.py:119-121`; `docs/runbooks/ailab-pin-bump.md:17-19` |
| V33 | Login volume: 395 `/callback` and 80 `/auth/login` answers in 7 days (about 2.4 logins per hour). | LIVE gatekeeper RED metrics |
| **Item 2 credentials** | | |
| V34 | Gitea users: `gitea_admin` (uid 1, site admin), `cchifor` (uid 2, organization, a converted user), `chifor` (uid 3, site admin, `login_type` 6 = OAuth2 via Authelia). | Gitea DB `"user"` |
| V35 | Token counts: `cchifor` **26** (spec and runbook say 21: drift), `chifor` 1 (`cc-admin-20260913`, `read:organization,write:issue,write:repository,read:user`, last used 2026-10-07 06:06Z), `gitea_admin` 41. | Gitea DB `access_token` (names, scopes, dates) |
| V36 | Of the 26 `cchifor` tokens only `ver040-1786337710` (`write:issue,write:repository`) was used in 30 days; the other 25 were last used 2026-08-10 to 2026-09-03; three carry `write:organization`. | same |
| V37 | `gitea_admin`: only `af-ci-scaler-2941` (`read:admin`) and `flux-ailab-read` (`read:repository`) are documented consumers; 39 others were last used 2026-07-12 to 2026-08-11, including `stage0-ops-1784874768` with `write:admin`. | same; `ailab:docs/runbooks/s2s-identity.md:351-353` |
| V38 | Gitea access tokens cannot expire here (no expiry column). Whether `updated_unix` moves on every use is UNVERIFIED (B6 tests it). | DB schema |
| V39 | `chifor` has an OAuth2 grant to the built-in "Git Credential Manager" app; the workstation holds GCM entries `git:https://refresh_token.git.chifor.me` and `git:https://oauth2@git.chifor.me`. | DB `oauth2_grant`; `cmdkey /list` (names) |
| V40 | Workstation Gitea credentials (names only): WCM `git:https://git.chifor.me` (user `cchifor`, the default), `git:https://cchifor@git.chifor.me`, `git:https://chifor@git.chifor.me`; `~/.git-credentials` one entry `chifor@git.chifor.me`; files `~/.gitea_tok`, `~/.gitea_cred_tmp`; `credential.helper=manager`, `credential.https://chifor@git.chifor.me.helper=store`; no `tea` config, no `.netrc`. | `cmdkey /list`; `git config --get-regexp credential`; `ls -la` |
| V41 | Six Gitea Actions secrets have token-like names; all but `FORGE_RELEASE_TOKEN` predate the first `cchifor` token. | DB `secret` (names, dates) |
| V42 | `pr_reviewer_merge_authors` = `cchifor, chifor, agentforge-ci-bot, renovate-bot, dev-worker-bot` (global); repo-scoped authors exist separately. | `ailab:ansible/roles/pr_reviewer/defaults/main.yml:216-219` |
| V43 | infra-pg (CNPG) runs the default custom-queries ConfigMap, scraped through PodMonitor `infra-pg-metrics`. | LIVE; `ailab:kubernetes/apps/databases/infra-pg.yaml:286-300` |
| V59 | Break-glass exists: Secret `gitea/gitea-admin` (SOPS `kubernetes/apps/apps/gitea/gitea-admin.sops.yaml`, "break-glass local admin") holds the `gitea_admin` password. Gitea runs one replica (1.26.1-rootless). Token deletion through the API needs basic auth of that user or of a site admin (`reqSelfOrAdmin`), so `gitea_admin` can delete any user's tokens by the supported path. Deleting a token's DB row revoked it at once on 2026-10-07 (the next request answered 401; Gitea's success cache re-reads the row by id). `scripts/forge.sh`, named in `CLAUDE.md`, does not exist. | `ailab:kubernetes/apps/apps/gitea/kustomization.yaml:5`, `gitea.yaml:93`; `docs/runbooks/agentforge-platform-activation.md:316-320`; LIVE deploy |
| **Item 3 drill 2** | | |
| V44 | HelmRelease `strive` and HelmChart `strive-ailab-strive` list three `valuesFiles`, the registry file last; upgrade remediation `retries: 3` (default strategy rollback), `cleanupOnFail: true`. | `platform:deploy/gitops/flux/clusters/ailab/app/helmrelease.yaml:22-34`; LIVE |
| V45 | Gatekeeper is pinned to `sha256:3adaf0be...` (`sha-1e1e33775`), unchanged by #2125. | `ailab.yaml:191-202` |
| V46 | Darkening the harness requires `WEB_AGENT_PANEL=legacy` first; the current web image honours `legacy` "for one release". | `ailab.yaml:1580-1583,2340-2346` |
| V47 | TokenReview RBAC renders only with composite on; the apiserver CNP renders regardless. | `charts/gatekeeper/templates/tokenreview-rbac.yaml:1`; `ailab.yaml:220-231` |
| V48 | With the registry file not listed, the S2S Authority Guard runs (a), (b), (b0), (b2); (b1) only when listed. | `platform:scripts/ci/check-s2s-authority.py:13-60,322-324` |
| V49 | The pre-Phase-3 image (`45abbd52...`) rejects `SVC_AUTH_BACKEND=composite` (Literal) and ignores unknown env. | signed plan "Misconfiguration contract"; `ailab.yaml:357` |
| V50 | helm-controller v1.5.5 embeds Helm v4.2.0 and server-side apply for new HelmReleases; platform CI renders with Helm 3, which (unlike Helm 4) ignores a parent `null` over a subchart default. The apply method `strive` uses is UNVERIFIED. | LIVE image tags; helm-controller v1.5.5; `_helpers.tpl:350-352`; `check-harness-chart-contract.sh:41-55` |
| V51 | `flux-resume.sh` gates: 0 target, 1 source artifact, 2 Kustomization `lastAppliedRevision`, 3 HelmRelease chart version, 4 end state. A STOP at gates 0-1 leaves everything suspended; a STOP at gate 2 or later leaves the Kustomization (and from gate 3 the HelmRelease) **resumed**, and the script says "freeze again (Kustomization first)". | `ailab:scripts/s2s/flux-resume.sh:14-29,186-217` |
| V60 | `platform-app` and `platform-workers` `dependsOn` `platform-secrets` (same source); `platform-identity` and `platform-workers` depend on `platform-app`; `platform-secrets` interval 10 m. Dependents wait while a same-source dependency has not applied the current revision (Flux semantics, UNVERIFIED here; C5 records it). | `platform:deploy/gitops/flux/clusters/ailab/kustomizations.yaml:25-30,192-206,210-245` |
| V61 | **After #2125 (LIVE 07:03Z):** gatekeeper logs `service_registry loaded ... clients=0` and `service_registry_extras loaded ... merged=11 refused=0`; the ten Python services and four workers present projected tokens (`serviceAccountToken.gatekeeper`); their `gatekeeper-client-secret` keys stay in each `<svc>-secrets`, inert; tms keeps one but has no registry entry. Gatekeeper refuses an extras entry whose `client_id` is also in the base. Every S2S mint now passes one process-wide TokenReview limiter (4 concurrent, 10/s, burst 20); ADR-034 accepted F-25 partly because "preshared clients are unaffected", no longer true. LIVE: 0 limited reviews; no gatekeeper or TokenReview alert exists. | LIVE logs; `platform:deploy/secrets/ailab/SECRETS.md:111-176`; `ailab.yaml` D.8 blocks; `config.py:363-367`; `ADR-034:303-318`; LIVE Prometheus, `get prometheusrule -A` |
| V52 | Lowest-traffic hours (gatekeeper `/auth` <= 5 per hour): 22:00-01:00Z and 03:00-07:00Z (one day of data). | LIVE Prometheus |
| **Item 4 edge** | | |
| V53 | Traefik exposes metrics on entrypoint `metrics` (:9100) and the chart creates ServiceMonitor `platform-edge/traefik` (`targetPort: metrics`), but (1) without label `release: kube-prometheus-stack`, which Prometheus requires, and (2) `svc/traefik` exposes only `web, websecure`, so the label alone yields no target. `metrics.prometheus.service.enabled: true` renders Service `traefik-metrics` (port `metrics`) and the ServiceMonitor selects it (Fable's local render of 36.3.0). Today: zero Traefik series. | LIVE; `platform:deploy/components/traefik/helmrelease.yaml:54-75`; chart 36.3.0 `values.yaml:427-441` |
| V54 | Access logs are JSON with every request header dropped, shipped by the Alloy DaemonSet to single-binary Loki, retention 168 h. | LIVE args; `ailab:kubernetes/apps/infrastructure/monitoring/alloy.yaml`, `loki.yaml:39` |
| V55 | **The "partially in Loki" premise did not reproduce.** For each of the last 30 hours Traefik access lines in Loki >= gatekeeper `/auth` answers. E0 (2026-10-07) preserved the incident: 18 5xx between 18:06:40 and 18:10:31Z, all through the one Traefik pod. Six 500s on the airlock, integration and notification routers were generated by Traefik (OriginStatus 0, no service, no matching application 5xx); the other twelve carried an upstream status (4x 503 web, 7x 503 via the sandbox error page, 1x 502 airlock after 7.5 s). There were no restarts, readiness or EndpointSlice changes, and gatekeeper logged no errors; cloudflared logged 120 stream-cancel errors. The logs (Traefik at INFO) do not say which stage produced the six 500s, so E1b and E3 are what attribute the next occurrence. Events and Traefik or cloudflared metrics for the window never existed or have expired. | LIVE Loki, Prometheus; `plans/2026-10-07-edge-5xx-evidence.md` |
| V56 | Gatekeeper OTLP RED metrics: 7-day `/auth` 5xx = 0; `/auth/token` 503 = 107 (drill 4). | LIVE Prometheus |
| V57 | No strive PrometheusRule exists. ailab owns monitoring: rules with promtool fixtures (`.gitea/workflows/rules-lint.yaml`), Alertmanager to ntfy routed on `severity`, the `strive-red` dashboard, Gatus (generic `GatusEndpointDown`). The Prometheus `ruleSelector` requires `release: kube-prometheus-stack`. | LIVE; `ailab:kubernetes/apps/apps/gatus/prometheusrule.yaml:17-45` |

## Owner decisions (required before execution)

Each decision lists the options, the recommendation and its cost. Steps that depend on a decision name it.

- **D1 (F-01) Valkey hardening depth.** (a) NetworkPolicy only. (b) NetworkPolicy plus AUTH. (c) b plus TLS now.
  (d) b with per-service ACL users. **Recommend b**; TLS follows F-16. Accepted blast radius: one shared `default`
  password gives each of the 16 consumers every key, in plaintext on the pod network; (d) would need a password and
  key-prefix ACL per consumer and is not worth it while the threat is anonymous access. Cost: one owner-merged helper
  change, a password rotation, one platform-wide roll and one Valkey restart in quiet hours; AUTH only after AG1a.
- **D2 (F-02) API keys.** (a) Retire on ailab: a flag turns off the `X-API-Key` track and the management API; records
  are purged. (b) Harden (integrity-protected records, verified-bearer admin API, slug mapping). **Recommend a** (zero use
  in 30 days, keys cannot work against weld backends, `platform__app_import` unreachable). The owner's acceptance is
  recorded in the spec; `GatekeeperApiKeyUsed` (E3) fires if the track ever answers again (flag flipped or image
  rolled back below AG1a). Cost: one protected `config.py` field; re-enabling needs plan b.
- **D3 (F-04) Test bypass.** (a) Keep it, but refuse it for any request that came through Cloudflare (markers
  `CF-Ray`, `CF-Connecting-IP`, `CDN-Loop`; Sentinel runs in-cluster, V21), enumerate every path into Traefik (A4.2),
  rotate the token, alert on edge attempts. (b) Disable permanently (Sentinel "Validate" stops working). (c) Toggle per
  test window. **Recommend a.** Cost: an unprotected code change and one token rotation.
- **D4 (F-03) Signing-key rotation model.** (a) Keygen CronJob stays off; rotate by owner SOPS commits in three phases,
  roll by `kubectl rollout restart`, every 180 days and on suspicion; each rotation ends by opening a due-dated platform
  issue for the next. (b) Build the keygen image (incompatible with a Flux-managed Secret, V19). (c) Roll through a chart
  checksum (protected change, two-controller race). **Recommend a.** Cost: about 1 hour of owner time per rotation, in
  one window (the three phases are one controlled operation under the calendar rule, see Sequencing).
- **D5 (F-05) Preshared S2S secrets, after #2125.** No ailab client uses one (V61); the ten services' and tms's
  `gatekeeper-client-secret` keys are inert, and they are exactly the material the config rollback (C4) needs: C4
  restores their hashes, which only work while the matching secrets exist. (a) Keep them as rollback material for as
  long as the config rollback is the documented one; delete them only when that rollback is retired (a dated ADR-034
  note), and at that point C11's runbook names the replacement: new secrets and hashes for each of the ten (A5.4 x 10),
  then C4, with an unmeasured window. (b) Delete them after drill 2 and accept that longer, unrehearsed rollback now.
  **Recommend a**: an inert secret only works if its hash is back in the registry, which is the rollback itself. The
  preshared rotation procedure is written for a re-added client (A5.4) but not rehearsed; drill 2 measures the same swap
  window.
- **D6 (F-05) Session and delegation Fernet keys.** (a) Announced-logout procedure only. (b) `MultiFernet` overlap
  (protected `lifecycle.py`, `config.py`). **Recommend a.** The key opens session and grant ciphertext wherever it lies
  (Valkey, its AOF on the NFS PVC, backups), so F-01 narrows but does not remove its value; A6 therefore uses an
  HKDF-derived key, not this one. Cost: a rotation logs every user out (repeatedly during the roll), voids long-running
  grants, and is followed by a purge so a rollback cannot revive old records (A5.5).
- **D7 (F-05) Kubernetes ServiceAccount signing key.** (a) Research and rehearse on a disposable Talos 1.11 cluster,
  time-boxed to one day including building that cluster; a documented "not feasible without downtime" is an acceptable
  outcome; it never delays A1-A4 or A6. (b) Record "no procedure" as an accepted residual. **Recommend a.**
- **D8 (F-05) age key.** (a) Real dual-recipient rotation, preceded by a holder inventory and a 10-minute scratch
  rehearsal; the old key is archived (encrypted to the new recipient), not destroyed, so git-history ciphertext stays
  recoverable. (b) Procedure plus scratch rehearsal only. **Recommend a.** Cost: re-encryption PRs in both repositories
  (platform owner-merged), a hand update of `flux-system/sops-age`. Neither option revokes what the old key decrypts
  from history: after a suspected compromise every value must be rotated too.
- **D9 (F-06) PKCE and nonce rollout.** (a) Expand (AG1b, with a canary), contract (AG2), then Keycloak enforcement.
  (b) One release, accepting that a login started on a new pod and finished on an old pod fails once. **Recommend a.**
  Cost: one extra pin bump; after enforcement a rollback below AG2 needs A6.4's rollback first.
- **D10 (item 2) Routine identity.** (a) New non-admin user `workstation-bot` in team `automation`. (b) Reuse
  `dev-worker-bot`. **Recommend a** (separate revocation and attribution). Cost: one user, one token, one merge-author entry.
- **D11 (item 2) Owner actions.** (a) Web UI by default. A scripted owner operation runs in the owner's own terminal
  from a session-prepared script that reads the token with `Read-Host -AsSecureString` into process memory only; the
  owner mints it in the UI immediately before and deletes it immediately after. The lifetime is manual: the token is
  valid until deleted, and `GiteaOwnerTokenStanding` detects one older than 4 hours. (b) a plus a CronJob deleting
  `owner-eph-*` tokens older than 2 hours through the database (a real bound, but a standing DB writer on tokens).
  **Recommend a.** Cost: the owner performs merges and approvals in the browser. Transport rule for (a): the decrypted
  token never appears in a native process's arguments (`curl -H "Authorization: token ..."` is visible in the process
  list); the script uses an in-process client (`Invoke-RestMethod -Headers`) or `curl -H @-` on stdin, suppresses
  request and error dumps, runs with PowerShell transcription off, and clears its variables at the end. Its error paths
  are exercised with a dummy token before the owner enters a real one.
- **D12 (item 2) Cleanup scope.** Include `chifor`'s GCM OAuth grant and the 39 undocumented `gitea_admin` tokens; keep
  the two documented ones. **Recommend yes**, with: B0 proves the `gitea_admin` break-glass login first; every token
  slated for deletion is watched 7 days and deleted from a fresh snapshot (B6); a consumer that breaks gets a fresh
  scoped token. The workstation's kubeconfig, talosconfig and age key stay out of scope.
- **D13 (item 3) Drill 2 (changed after review).** The signed drill 2 (image plus config rollback) is **retired**: no
  image older than AG1a may run once the fixes are live, the pre-Phase-3 image rejects `composite` (V49), and after A6.4
  an older image cannot log users in. (a) **Config-only rollback drill**, the procedure operators would actually use:
  one owner PR drops the registry `valuesFiles` entry, sets `harness.enabled: false`, keeps every image, and, **after
  #2125**, restores the ten preshared clients (reverts #2125's values changes and re-adds the ten hashes to the current
  Secret by a `sops` edit), because with an empty base registry dropping the registry file alone would leave no S2S
  client. The re-forward runs in two PRs (registry file back first, then the switch to tokens) so that neither
  direction can deploy an artifact built from the wrong `valuesFiles` list with an empty base. It runs after E1a in a
  quiet window and gates nothing (not AG1a, AG1b or AG2). (b) Harness-only kill switch (`harness.enabled: false`,
  registry kept) plus offline Helm 3/4 renders of (a): no S2S window, but the real rollback stays unexercised.
  **Recommend a.** Cost: two S2S mint-failure windows (rollback and re-forward, each one gatekeeper-plus-consumer roll,
  expected a few minutes, measured) plus one gatekeeper-only roll without a client change, the assistant on the legacy
  panel and platform deploys frozen for the window (about 2 hours).
- **D14 (item 4) Edge telemetry detail.** Enable Traefik router labels, keep `CF-Ray` in access logs, Loki retention
  stays 168 h. **Recommend yes, yes, keep.** Cost: one Traefik surge restart; the extra series (routers x codes x
  methods x protocols, plus duration-histogram buckets) and log bytes are measured in E1b, and router labels are
  reverted if they add more than 5% to Prometheus head series.
- **D15 (all) Protect the hardened files, before AG1a opens** (AG1a already needs an owner merge, so protecting now
  costs little): add to platform `protected_file_patterns`
  `infra/gatekeeper/src/app/gatekeeper/{routes,routes_session,helpers,jwks,oidc,key_store,server_session,redis,apikeys,apikeys_api}.py`,
  `infra/gatekeeper/src/app/api/v1/api.py`, `infra/keycloak-sync/**` (code, `Dockerfile`, `pyproject.toml`, `uv.lock`:
  the whole execution path of a Job that holds the Keycloak `master` admin password),
  `deploy/components/keycloak-realm-seed/realm-configmap.yaml`, `deploy/components/valkey/**`,
  `deploy/components/traefik/**`, and `scripts/ci/owner_ack.py` with `scripts/ci/test_owner_ack.py` (today a bot PR
  could shrink the owner-ack list). Braces expanded. **And** one platform PR, landed first, adds
  `deploy/components/keycloak-realm-seed/*.yaml` to `owner_ack.py`'s `PROTECTED_GLOBS` (today only
  `deploy/helm/values/providers/*.yaml` and `deploy/components/workers/*.yaml`) with its test, so a change to
  `sync-job.yaml` (image, command, destination, env), `job.yaml` or `kustomization.yaml` needs the owner's head-bound
  `approve-pin` while fleet pin bumps stay bot-mergeable (`check-keycloak-realm-sync-deployed.sh` only checks that some
  realm-sync image is wired). **Recommend yes.** Verify: a bot-authored, non-draft no-op PR with green checks touching
  one protected file is refused by the bot's merge with the "Changed protected files" message (a draft could never
  merge, so it proves nothing), and one touching `sync-job.yaml` fails `owner-ack` until `approve-pin`. Residual (a D17 issue):
  the `owner-ack` job lives in `.github/workflows/ci.yml`, which stays unprotected because most CI changes touch it; a
  bot-mergeable edit of that job could neutralise `approve-pin`. The fix is to move the job to its own protected
  workflow file, as the S2S Authority Guard is, and update the required check name. Cost: 3 commits in the last 30
  days touched the protected paths (about 2-3 extra owner merges a month), realm-seed changes need `approve-pin`, plus
  this plan's PRs.
- **D16 (F-25, after #2125) TokenReview limiter scope.** (a) Keep the shared limiter; add `GatekeeperTokenReviewLimited`
  (E3); correct ADR-034's and F-25's reasoning; build a per-client throttle only if the alert fires outside a drill.
  (b) A per-client sub-bucket ahead of the shared limiter now (protected `tokenreview_verifier.py`, owner merge, one
  gatekeeper release). **Recommend a**: 0 limited reviews so far, the positive cache serves steady-state mints for 60 s,
  and an attacker must already run as an admitted in-cluster workload.
- **D17 (F-05) The remaining "no procedure" rows** (Keycloak client secret, Flux deploy keys, tunnel credentials,
  service database passwords). (a) One tracked issue each (owner, due date); F-05 is reported "partially addressed"
  until they close. (b) Record them as accepted residuals in the spec. **Recommend a.**

## Approach

Step IDs: **A** = item 1 (A1 = F-01 ... A6 = F-06; AG = shared gatekeeper releases), **B** = item 2, **C** = item 3,
**E** = item 4. Commands are indicative; secret values are always piped, never echoed. Artefacts that could hold one go
to the main checkout's gitignored `kubernetes/infra/_out/` (`C:\Users\chifo\work\home\ailab`): private keys and
plaintext are restricted to the user (`icacls <f> /inheritance:r /grant:r "%USERNAME%:F"`) and deleted after use unless
a step keeps them; before the first such step, confirm `_out/` is not inside a synced or backed-up folder.

### Shared: gatekeeper releases AG1a, AG1b and AG2

- **AG1a (containment; the minimum safe release)**: A1.0, A1.0b, A2.1, A4.1. One code PR (owner-merged: `config.py`) and
  a pin PR opened after the image exists. Rollback: re-pin the previous digest only before A1.4; after A1.4 never below
  AG1a (the old image logs the Valkey password; if forced, rotate it). A defect in one feature is contained without a
  rollback: F-04 by `TEST_BYPASS_ENABLED=false`, F-02 needs none (the flag), logging or metrics by a fix forward.
- **AG1b (OIDC expand)**: A6.1 and the `validate_state` hardening, after AG1a is live. Own code and pin PRs. Rollback:
  re-pin AG1a.
- **AG2 (contract)**: A6.3, after AG1b has soaked 24 hours with the canary (A6.2). Rollback: re-pin AG1b; after A6.4,
  A6.4's rollback first.

### Item 1, F-01: Valkey authentication and ingress (D1)

- **A1.0 (AG1a).** `redis.py` logs `scheme://host:port/db` only (both lines in V7). Instrument the fallback:
  `gatekeeper_redis_fallback_total` +1 on every switch to memory (connect failure and `_on_redis_failure`),
  `gatekeeper_redis_reconnections_total` +1 on reconnect, and a new gauge `gatekeeper_redis_connected` (1/0; an idle
  replica can sit in fallback without a failing operation). Unit tests: a `default:secret@` URL logs no `secret`; the
  counters and gauge move on a simulated failure and reconnect. Live: the gauge reads 1 on both pods after AG1a.
- **A1.0b (AG1a).** `server_session.py` and `routes_session.py` log the first 12 hex of sha256(session id), never the
  id (V58). In the first quiet hour after AG1a, end every session issued before it (`UNLINK` of `gk:session:*`, counts
  only; announced as a logout; re-login is silent while the Keycloak SSO cookie lives), because their ids sit in Loki.
- **A1.1 Measure the real client set** (owner, read-only): sample `valkey-cli CLIENT LIST` every minute for at least 10
  minutes (two airlock-reaper cycles), map `addr` to pods, and record each client pod's `strive.io/service` label. Any
  client whose label is not among V5's 16 stops A1.2 until it is understood (V5 found no CronJob with a Redis env).
- **A1.2 NetworkPolicy** (platform PR, `deploy/components/valkey/helmrelease.yaml`): `networkPolicy.allowExternal: false`
  and one `extraIngress` rule on 6379 from `podSelector` `strive.io/service In [airlock, deepagent, digest, digest-worker, gatekeeper, integration, integration-worker, knowledge, mcp, mcp-worker, notification, profile, sentinel, tms, workflow, workflow-worker]`.
  Before merge: render chart 1.0.3 from the same HTTPS index the HelmRepository uses; only the NetworkPolicy may differ
  (the StatefulSet byte-identical, so no restart). The chart also admits pods labelled `valkey-client: "true"` (namespace
  writers only, the same trust as `strive.io/service`). After merge: 6379 times out from an unlabelled pod in
  `strive-ailab` and from a pod in another namespace, and answers from a labelled consumer; StatefulSet generation
  unchanged. Acceptance over 24 hours: Loki count in `strive-ailab` of the **F-01 error regex**
  `Error 110|Timeout connecting|Connection refused|Redis client unavailable|in-memory fallback|NOAUTH|WRONGPASS|AuthenticationError`
  (timeouts are what a NetworkPolicy miss produces; the AUTH terms are what a consumer with a missing or wrong password
  produces after A1.5) at the pre-change baseline, airlock and mcp logs checked by name, `gatekeeper_redis_connected` 1 on both pods
  (after AG1a). Rollback: revert the PR.
- **A1.3 Prove the AUTH semantics** (dev worker or CI, docker, no cluster), with `bitnamilegacy/valkey:8.0.1-debian-12-r1`
  and redis-py **7.4.0 and 8.0.0** (V63): with an empty password, `AUTH default x` returns OK, `AUTH x` errors and
  `from_url("redis://default:x@host")` pings; with `requirepass x`, the same URL pings, a wrong password raises
  `AuthenticationError`, `issubclass(AuthenticationError, ConnectionError)` holds (what makes gatekeeper's fallback catch
  it, V8), and a client reconnects after a server restart; an AG1a gatekeeper container given a wrong password stays
  Ready and its fallback counter moves. Record the outputs. Any difference: stop; A1.4 would then need every consumer to
  read a separate `REDIS_PASSWORD`, re-planned. TMS is excluded (V6).
- **A1.4 Password and clients** (after AG1a and A1.3; merged in V52 quiet hours, not on the day of another gatekeeper
  roll, because it rolls every consumer):
  1. Owner rotates `valkey-password` in `deploy/secrets/ailab/valkey-auth.enc.yaml` (protected) to 64 hex characters
     (URL-safe). No consumer reads it yet.
  2. Platform PR (owner-merged, `_helpers.tpl` protected): when `global.valkey.auth.enabled` (default `false`, other
     providers untouched) the helper renders `REDIS_PASSWORD` from `secretKeyRef valkey-auth/valkey-password` and then
     `REDIS_URL=redis://default:$(REDIS_PASSWORD)@valkey-master.<ns>.svc.cluster.local:6379` (Kubernetes expands
     `$(VAR)` only for variables defined earlier in the list; the pod spec keeps the literal). Same PR: `ailab.yaml` sets
     the flag and rewrites airlock's `APP__AIRLOCK__RATE_LIMIT_REDIS_URL`; the four worker manifests add both variables.
     Render tests (Helm 3 and Helm 4, V50): `REDIS_PASSWORD` precedes every URL that uses it, no literal password, flag
     off renders today's output byte-identically.
  3. Rollout. A `nopass` server accepts any password, so before A1.5 prove the effective value without printing it: in
     each consumer pod `python -c` prints the first 12 hex of sha256(`REDIS_PASSWORD`) and the owner compares it with
     the same fingerprint of the Secret value (read into a shell variable). Also: no literal password in any rendered
     manifest; the F-01 error regex's Loki count (A1.2) over 24 hours unchanged; the count (never the lines) of the substring
     `redis://default:` in Loki = 0.
  Rollback: revert the PR (only while A1.5 is not live).
- **A1.5 Server AUTH** (platform PR, `deploy/components/valkey/helmrelease.yaml`: `auth: {enabled: true, existingSecret: valkey-auth, existingSecretPasswordKey: valkey-password}`;
  the stale comment at `:73-81` is replaced: gatekeeper no longer blocks at boot). Merged at the start of an announced
  window in V52's quiet hours. One Valkey restart: until each gatekeeper reconnects (backoff up to 60 s), it serves from
  per-pod memory, so logins made then are lost after reconnect (users re-login once) and logouts or grant revocations
  made then are not persisted; the announcement asks for none, and any made are repeated afterwards. Session rows
  survive (AOF, V9). After: an unauthenticated `PING` gets `NOAUTH`, a wrong password `WRONGPASS`, the right one
  `PONG`; `gatekeeper_redis_connected` is 1 on both pods; a session created before the window still works; one login,
  one refresh and one logout through the public host persist (the logout removes the session's keys, by count); the
  F-01 error regex's Loki count over the next 24 hours, namespace-wide and for airlock, mcp and the four workers by name
  (no gauge covers them), at the pre-window baseline apart from the restart minute.
  Rollback: revert (AUTH off); clients keep working because `nopass` accepts their AUTH.
- **Ordering inside F-01:** A1.2 any time after E1a; A1.4 after AG1a and A1.3; A1.5 after A1.4 is verified. Rollback in
  reverse order.

### Item 1, F-02: API keys (D2 = a)

- **A2.1 (AG1a).** `config.py` field `api_keys_enabled: bool = True` (env `API_KEYS_ENABLED`). When false:
  `api/v1/api.py` does not mount the `/api-keys` router (404), and `/auth` ignores `X-API-Key`, counting
  `gatekeeper_auth_requests_total{method="api_key",status="disabled"}` and continuing with the other tracks. The AG1a pin
  PR sets `API_KEYS_ENABLED=false` in gatekeeper `extraEnv`. Tests: flag off gives 404, no mint for a planted record,
  the session track works with the header present; flag on unchanged.
- **A2.2 Purge** (owner, after AG1a): first confirm both pods run AG1a with `API_KEYS_ENABLED=false` (env names and
  value) and answer 404 on `/api/v1/api-keys`. Then count `apikey:*` and `apikeys_by_tenant:*` with `SCAN` (count only).
  Expected 0. If non-zero, stop and identify the tenant and creator before deleting; otherwise `UNLINK` and count again.
- **A2.3 Docs.** Fix the `apikeys_api.py:1-9` docstring and the gatekeeper README; the spec records F-02 and F-17 closed
  on ailab by retirement.
- If D2 = b, this item becomes a separate plan; nothing below depends on it.
- **Status:** F-02 closes only through A2.1 and A2.2. F-01's work narrows who can write records but does not touch the
  header-trusting management API.

### Item 1, F-03: signing-key rotation (D4 = a)

- **A3.1 Docs PR** (owner-merged; chart protected): new `platform:docs/runbooks/gatekeeper-key-rotation-ailab.md`
  (the procedure below); mark `gatekeeper-key-emergency-rotation.md` "not for ailab"; correct the hot-reload comments in
  `charts/gatekeeper/templates/keygen-cronjob.yaml:13-21`, `charts/gatekeeper/values.yaml:316-318`,
  `charts/gatekeeper/templates/deployment.yaml:185-190`, and the false self-generation claim at `ailab.yaml:444-448`.
- **A3.2 Rotation, done for real as the rehearsal.** Each phase is one owner SOPS commit to
  `deploy/secrets/ailab/gatekeeper-signing-keys.enc.yaml` (protected). Gate before the restart: `platform-secrets`
  `lastAppliedRevision` is the phase commit (one Flux apply writes all keys at once, so names and contents arrive
  together; the 10-minute interval bounds the wait, or request a reconcile) and the live Secret's key NAMES match. Then
  `kubectl rollout restart deployment/gatekeeper` and `rollout status`; each pod's `FileKeyRing loaded` line must list
  exactly the expected kids, computed locally (`openssl pkey -in <f> -pubout -outform DER | sha256sum | cut -c1-16`, V15).
  The restart annotation is not chart-managed; check that the next platform upgrade does not re-roll gatekeeper
  (UNVERIFIED with Helm 4 server-side apply).
  - **P1 pending.** Generate a P-256 key into `_out/` (`openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256`),
    add it as `pending.pem`. Check: both pods list `('active', '8b77549a4272d160'), ('pending', '<NEW>')`; `/auth/jwks`
    from each pod (exec, `127.0.0.1:5000`) lists both kids. **Wait at least 11 minutes** after the second pod is Ready
    (V18: 600 s cache lifespan plus margin).
  - **P2 promote.** `active.pem` := NEW, `retiring.pem` := OLD, no `pending.pem`. During the roll one pod signs with OLD,
    the other with NEW; both publish and verify both (V17). Check: `('active', NEW), ('retiring', OLD)`;
    `scripts/s2s/phase4-probes.sh --gatekeeper-only` PASS; a minted token's `kid` is NEW; one `@api` journey passes;
    no `invalid_token` rise on the backends. Known edge (into the runbook): if the harness refetched for a fabricated
    kid in the 60 s before its first NEW-signed token, it refuses NEW until that cooldown ends (`jwks.ts:84`), so up to
    60 s of `IDENTITY` 401s on harness-bound calls is a self-healing blip, not a failed promotion.
  - **P3 retire.** **Wait at least 15 minutes** after P2's rollout (330 s token-life bound plus margin), then remove
    `retiring.pem`. Check: one kid (NEW) on both pods and on the public JWKS; probes and a journey pass.
  - Rollback: one phase back at a time (`git revert` of the phase commit, valid because nothing else touches this file
    inside the rotation window; otherwise a `sops` edit back), then restart; never remove a key that signed within the
    last 15 minutes.
- **Emergency variant** (documented, not rehearsed): put NEW as the only key at once. During the roll the two pods sign
  with different single keys, so verifiers may refuse the other pod's tokens until the roll completes; outstanding
  tokens fail for at most 330 s and are re-minted (V17); that bound covers tokens gatekeeper minted, not tokens forged
  with a stolen OLD key. Verifiers keep OLD cached for up to 600 s after their last successful fetch, or up to 1800 s
  from that fetch in total while gatekeeper is unreachable (not the sum), so a holder of OLD can forge accepted tokens
  that long: on a compromise, after the roll,
  `kubectl rollout restart` every weld service and the harness to evict their caches; the bound is the roll plus those
  restarts.
- Cadence (D4): every 180 days and on suspicion; the runbook records each rotation's date and kids, and each rotation
  opens the due-dated issue for the next.

### Item 1, F-04: test bypass (D3 = a)

- **A4.1 (AG1a).** In the bypass track (`routes.py:786-855`), before the token comparison: if the request carries
  `cf-ray`, `cf-connecting-ip` or `cdn-loop`, count `status="edge_refused"` and answer the existing 401 body. A module
  constant in `routes.py` names the headers (no `config.py` change). A client that adds them itself is only refused;
  a client cannot remove what Cloudflare adds. Header presence is the discriminator because both paths reach Traefik
  from dynamic pod IPs (cloudflared's or Sentinel's), so an address rule cannot separate them. Tests: with any marker,
  401 and the metric even with the right token; without them, today's behaviour.
- **A4.2 Live acceptance** (after AG1a): every path into Traefik is enumerated (read-only): Services of type
  NodePort/LoadBalancer (none today, V22), the ingress rules of both cloudflared tunnels, any in-cluster proxy that
  forwards to `traefik.platform-edge`; each internet path must carry the markers to `/auth`. The owner sends one internet
  request with the real token to an allowlisted path (token read into a shell variable, never printed): 401 and
  `edge_refused` +1 (this also proves V23). `gatekeeper_auth_requests_total{method="test_bypass",status="success"}` keeps
  rising with Sentinel's runs, for every Sentinel validation kind (Validate targets `strive.place` only).
- **A4.3 Rotate the token** (usable from the internet for months): one owner commit changing `test-bypass-token` in
  `gatekeeper-secrets.enc.yaml` and `sentinel-secrets.enc.yaml` and bumping the checksum (V27), then
  `kubectl rollout restart deployment/sentinel`. Sentinel validations fail between the two rolls (expected under 5
  minutes). The procedure goes into the A5.1 platform runbook.
- **A4.4** Alert `StriveTestBypassFromEdge` (E3).
- Rollback: `TEST_BYPASS_ENABLED=false` first (D3 b's cost: Sentinel Validate stops); never an image rollback below AG1a.

### Item 1, F-05: roots-of-trust procedures

- **A5.1 Runbooks.** ailab `docs/runbooks/roots-of-trust-rotation.md` (age key, Kubernetes SA key, Keycloak realm keys)
  and platform `docs/runbooks/ailab-credential-rotation.md` (signing key = A3.2, test-bypass = A4.3, preshared for a
  re-added client = A5.4, Fernet = A5.5). Each states holders, order, overlap, user impact, verification and rollback.
  The spec's rotation inventory and `platform:deploy/secrets/ailab/SECRETS.md` (protected) link them.
- **A5.2 age key (D8).** Each step reversible until step 5:
  0. Inventory holders (names only): Gitea Actions secrets on ailab, platform and cloudlab; Secrets in `external-secrets`
     and `flux-system`; the workstation's `_out/`, `~/.config/sops/age/`, `SOPS_AGE_KEY*` variables; backups. Record
     each; step 5 waits until every holder is updated or shown to use another recipient. Then a 10-minute scratch
     rehearsal of steps 3-5 on a throwaway file.
  1. Generate the new key into `_out/` (`age-keygen -o _out/age-<date>.agekey`, ACL-restricted).
  2. Add it as a second identity to `flux-system/sops-age` (`kubectl create secret generic sops-age --from-file=age.agekey=<old> --from-file=age-<date>.agekey=<new> --dry-run=client -o yaml | kubectl apply -f -`).
     Check: every SOPS Kustomization stays Ready.
  3. Add the new recipient next to the old one in `ailab:.sops.yaml` and the ailab rule of `platform:.sops.yaml`; `sops
     updatekeys -y` on all 74 ailab and 23 platform files (platform PR owner-merged, with the `serviceRegistry.checksum`
     bump). Check each file decrypts with the new key alone and nothing else available, in an emptied environment (one
     sops binary, the WSL one, so no Windows `%APPDATA%\sops\age\keys.txt` is consulted):
     `env -i PATH=/usr/bin:/bin HOME=<empty dir> XDG_CONFIG_HOME=<empty dir> SOPS_AGE_KEY_FILE=<new> sops -d <f> >/dev/null`
     (exit code only; `env -i` also drops `SOPS_AGE_KEY`, `SOPS_AGE_KEY_CMD` and any SSH-key variable). Negative control
     first: the same command with `SOPS_AGE_KEY_FILE=<a freshly generated unrelated key>` must fail on every file, which
     proves no other identity source is reachable.
  4. Prove Flux with the new key alone: remove the old identity from `sops-age`, force-reconcile every SOPS
     Kustomization, all Ready (reversible: add it back).
  5. Remove the old recipient **and rotate each file's data key** (`sops rotate -i --rm-age <old>`), because
     `updatekeys` keeps the data key, which an old-key holder can recover from git history; checksum bump again; checks
     as in step 3. No ordering against drill 2: C4 and C8 never apply a historical ciphertext; they `sops`-edit the
     current file (Change-control rules) with registry entries read locally from the pre-#2125 revision, which the old
     key (kept, then archived in step 6) still opens.
  6. The new key moves to the fixed path `_out/age.agekey` (key name `age.agekey` in `sops-age`), so CLAUDE.md, README
     and tooling stay true; the old key is archived encrypted to the new recipient
     (`age -r <new> -o _out/age-retired-<date>.agekey.age <old>`), then the plaintext old key is deleted.
  Residual in the runbook: git history holds ciphertexts the old key opens; after a suspected compromise rotate the values.
- **A5.3 Keycloak realm keys** (owner, Admin console; if `kcadm` is used, `--config` points to a temporary file deleted
  afterwards, `master` admin password piped from the Secret). First read, without secrets, the realm lifetimes
  (`ssoSessionMaxLifespan`, `offlineSessionMaxLifespan`, remember-me, action-token lifespans) and each client's offline
  session count; the retention for step 3 is the longest lifetime that has live artifacts (11 hours if no offline
  sessions or remember-me exist).
  1. Add `rsa-generated` provider `rsa-<date>`, `active=false`, `enabled=true` (passive). Check the public certs list
     both RS256 kids (UNVERIFIED for 26.0.0; checked here). Wait at least 16 minutes: gatekeeper's 900 s cache refresh,
     not an unknown-kid refetch, brings the kid in, so the cooldown (V26) cannot interfere.
  2. Make it active with a higher priority. New tokens carry the new `kid`; the old key keeps verifying.
  3. `hmac-generated` (refresh tokens) and `aes-generated`: add new providers with higher priority; keep the old ones
     enabled for the retention above.
  4. Old providers passive, then disabled, then deleted a day later (deletion is the irreversible boundary).
     `rsa-enc-generated` the same way.
  Check at each step: a test session logged in before step 2 still refreshes after steps 2 and 3; a fresh login;
  `gatekeeper_auth_requests_total` refresh-failure and termination statuses at baseline. Rollback: re-activate the old
  provider (possible until its deletion).
- **A5.4 Preshared S2S secrets (D5 = a), after #2125.** No ailab client uses one. The procedure for a re-added client:
  new 32-byte secret and its argon2id hash (`SECRETS.md`, "Bootstrap checklist"); one owner commit changing the client's
  `gatekeeper-client-secret`, its hash in `gatekeeper-secrets.enc.yaml` and the checksum; after gatekeeper's roll,
  `kubectl rollout restart` the consumer. Expected failure window: drill 2's measured swap window (C6). No rehearsal.
- **A5.5 Fernet keys (D6 = a).** Owner commit changing `session-fernet-key` (or `delegation-grant-fernet-key`) plus the
  checksum, in V52's quiet hours, announced as a logout. During the roll (about 2 minutes) old and new pods cannot read
  each other's sessions, so users may be logged out more than once; log in after the roll. Then purge, by count, the
  session, guest and delegation-grant records (key prefixes listed in the runbook) so a rollback to the old key cannot
  revive them. Expected: every session ends (silent re-login while the Keycloak SSO cookie lives), guest sessions end,
  long-running grants fail (deepagent falls back to its 300 s path, `ailab.yaml:373-376`), in-flight A6 logins restart
  (their key is derived from this one).
- **A5.6 Kubernetes SA signing key (D7 = a).** Research then rehearse on a disposable Talos 1.11.2 cluster (never on
  `admin@ai`). The procedure to prove: add the new key as a verifier on every control plane first (a second
  `--service-account-key-file` through `cluster.apiServer.extraArgs`/`extraVolumes`); then switch the signer
  (`cluster.serviceAccount.key`) one CP at a time with `talosctl` 1.11.2 and etcd 3/3 between CPs; keep the old verifier
  until every token it signed has been replaced (projected tokens refresh within about an hour; legacy
  `kubernetes.io/service-account-token` Secrets never: inventory them on `admin@ai`). After #2125 every S2S mint depends
  on TokenReview of projected tokens, and gatekeeper's own automount token calls TokenReview (F-26), so removing the old
  verifier early breaks all S2S. Output: the procedure, or a recorded "not feasible without downtime".
- **A5.7** Update the spec's rotation inventory and F-05's status (with links and rehearsal dates); open D17's issues.

### Item 1, F-06: PKCE, nonce, bound state, issuer (D9 = a)

Design (stateless, no Valkey write per anonymous hit):
- **Login start** (`/auth/login`) creates `state` (32 random bytes, base64url, 43 characters), `nonce` (32 bytes) and
  `code_verifier` (64 bytes), and sends `code_challenge=BASE64URL(SHA256(verifier))`, `code_challenge_method=S256`,
  `nonce`, `state` and any vetted `kc_idp_hint`.
- **Transaction cookie** `__Host-gk_oidc_<first 16 hex of sha256(state)>`: Fernet with a key derived by HKDF-SHA256 from
  the session key (info `gk-oidc-tx-v1`); payload `{v, state, nonce, code_verifier, return_path (at most 1024 bytes,
  else "/"), iss, client_id, redirect_uri, iat}`; `HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=900`. The `__Host-`
  prefix stops a sibling host (apps on `apps.strive.place`, F-13) from planting one for `strive.place`; `Path=/` lets
  every `/auth/login` see them all. **Pruning:** each start deletes every `gk_oidc` cookie that fails to decrypt or is
  expired, keeps the 2 newest valid ones by their encrypted `iat` and deletes the rest, then sets its own, so sequential
  starts leave at most 3. Starts that run concurrently on the same cookie jar can each add one before any sees the
  others; that temporary overshoot is accepted (each cookie lives at most 900 s and the next start prunes back to 3).
- **Only navigations start a login.** The ForwardAuth miss (`routes.py:1611-1650`) answers 302 to
  `/auth/login?redirect_uri=<path>` on the same host only for navigations, decided by the existing
  `denials._is_navigation` (`denials.py:55-58`, not a second copy: `Sec-Fetch-Mode: navigate`, or, without that header,
  an `Accept` containing `text/html`); everything else gets the API-style 401, which also stops subresources from starting
  logins. The hop through `/auth/login` is belt-and-braces (Traefik relays a 302's `Set-Cookie`, which
  `authResponseHeaders` lists), kept so one code path sets the cookie; `kc_idp_hint` and percent-encoded paths survive it.
  The spec's validation item 1 changes accordingly.
- **`/callback`.** A `state` that does not start with `/` is a new-flow callback (a legacy state is a path: that is the
  discriminator, so a missing or bad cookie never falls through to the legacy exchange). It decrypts the matching cookie
  (TTL 900 s), compares `state` in constant time, checks `iss`, `client_id` and `redirect_uri` against the tenant config,
  exchanges the code with `code_verifier` (`oidc.py:exchange_code` gains the parameter, V62), verifies the access token
  (`verify_token` with the expected issuer) before the tenant-assignment hook and verifies any token that hook returns
  (V30), verifies the ID token with required `iss`, `aud`, `exp`, `iat`, `sub`, `nonce` (and `azp` = client id when
  `aud` has several values), deletes the matched cookie and any `gk_oidc` cookie that fails to decrypt or is expired
  (valid siblings stay, so a second tab's login still completes), and redirects to the validated return path.
- **Every failure** (missing, expired or mismatched cookie, `error=access_denied`, missing or invalid ID token,
  token-endpoint timeout, `invalid_grant` including a replayed code) creates no session, deletes the same cookies as a
  success, and answers a small 400 page with a "Sign in again" link to `/auth/login?redirect_uri=<validated path>`,
  built from the validated path and attribute-escaped. There is no automatic retry, so no loop is possible, with or
  without cookies.
- `validate_state` also rejects backslashes, control characters, and, after percent-decoding up to twice, any `//` or
  `\` prefix or scheme (V62).
- `verify_token` gains `expected_issuer=` (the tenant's public issuer), separate from `issuer_url` (the back-channel
  JWKS fetch base).

Steps:
- **A6.1 (AG1b, expand).** `/callback` accepts both shapes (legacy = a path `state`, unchanged). Login still sends the
  legacy request by default; `/auth/login?gk_flow=pkce` starts the new flow (the canary). The issuer check is
  report-only: `gatekeeper_oidc_issuer_mismatch_total{token="access"|"id"}`; `gatekeeper_oidc_callback_total{flow,result}`
  uses fixed label values. Tests: every branch above, split by stage: pre-exchange rejections (cookie missing,
  expired or mismatched, `state` mismatch, `iss`/`client_id`/`redirect_uri` mismatch, `error=access_denied`) make
  **zero** token-endpoint calls; exchange-stage and token-validation failures (timeout, `invalid_grant`, replayed code,
  missing or invalid ID token, issuer or nonce mismatch) make exactly one call, create no session, delete the cookies
  and show the error page. Cookie attributes; pruning: five genuinely concurrent starts on the same jar snapshot, then
  one more start, leave at most 3 valid `gk_oidc` cookies, and an expired or undecryptable one is always deleted; two
  tabs: both callbacks succeed in either order; `code_verifier` in the token request payload; the `validate_state`
  cases, and the error page for `redirect_uri=/\evil.example` renders a same-host, escaped link; the matrix (AG1b start
  with AG2 callback and the reverse, a callback after 15 minutes); legacy unchanged.
- **A6.2 Soak** at least 24 hours: issuer mismatches 0; `/callback` 5xx 0; at least one successful canary login served
  by each gatekeeper pod (`gatekeeper_oidc_callback_total{flow="pkce",result="success"}` > 0 per pod; repeat until both).
- **A6.3 (AG2, contract).** `/auth/login` sends the new flow by default; the ForwardAuth miss redirects navigations to
  `/auth/login`; `/callback` refuses the legacy shape (error page, no exchange); the issuer check enforces. A login
  started on either release finishes on either (same cookie format); a legacy login finished on an AG2 pod during the
  roll sees the error page once (V33). Live checks: the anonymous `GET https://strive.place/` chain ends at an authorize
  URL with `code_challenge_method=S256`, `nonce=` and a 43-character `state`; `GET /callback?code=x&state=y` without a
  cookie answers the 400 page and gatekeeper logs no token exchange; `/auth/login` and `/callback` answer directly
  (not through ForwardAuth) on both hosts; an anonymous subresource request gets 401, not 302; login journeys on both
  hosts pass.
- **A6.4 Enforce at Keycloak.** keycloak-sync reconciles `pkce.code.challenge.method=S256` on client `gatekeeper` (as
  `sync_client_redirect_uris`); the realm seed (`deploy/components/keycloak-realm-seed/realm-configmap.yaml`) carries it
  for fresh imports; keycloak-sync pin bump. Check: an authorize request without `code_challenge`, or with `plain`, is
  refused; a token request with a missing or wrong verifier gets `invalid_grant`; logins pass. **Rollback** (before any
  gatekeeper rollback below AG2): suspend Kustomization `platform-identity` (it re-applies the sync Job every 10-20
  minutes, V32), remove the attribute in the console, verify a legacy-shape authorize request is accepted, then re-pin;
  land the keycloak-sync revert and its pin before resuming `platform-identity`.

### Item 2: owner workstation credentials (D10, D11, D12)

Target state: the workstation holds one non-admin routine credential (`workstation-bot`); no `cchifor` token exists; no
standing `chifor` token or OAuth grant exists; owner actions happen in the web UI or from the owner's own terminal with a
token held only in process memory and deleted right after; break-glass is documented and tested; any re-accumulation
alerts.

- **B0 Break-glass first** (before anything is revoked): `gitea_admin` keeps password login (V59). The owner checks
  it: basic-auth `GET /api/v1/user` with the password read from Secret `gitea/gitea-admin` into a shell variable (status
  only). The second path is cluster-admin `kubectl exec` into the Gitea pod to mint a token. Either way the token fires
  `GiteaAdminTokenNotAllowlisted` and is deleted after use (B8 records this).
- **B1 Identity.** Owner, in the Gitea pod: `gitea admin user create --username workstation-bot ...` (non-admin, random
  password never used), add it to org team `automation` (id 37, write, never admin). Mint `ws-<yyyymmdd>` with
  `write:repository,write:issue,read:organization,read:user` via `gitea admin user generate-access-token ... --raw`, piped
  straight into `git credential approve` (`protocol=https`, `host=git.chifor.me`, `username=workstation-bot`), never echoed.
- **B2 Merge eligibility** (ailab PR): add `workstation-bot` to `pr_reviewer_merge_authors` (V42); converge the reviewers
  as the D1 record did (reviewers first). Lands before B3 (reviewbot rule).
- **B3 Switch the workstation.**
  - Inventory first, redacted inside the process (an `extraheader` value can hold a token and a `url.` rewrite can embed
    one, so raw output never reaches the session): `git config --show-origin --get-regexp '^(credential|http\..*extraheader|url\.)'`
    at system and global level and in every active checkout and worktree, piped through a filter that prints the
    origin file, the key name and, for values, only the URL origin without userinfo or `<redacted>`; WCM target names;
    `~/.git-credentials` as origin and username only; tools that read `~/.gitea_tok` (none found in the repositories).
    Record.
  - `git config --global credential.https://git.chifor.me.username workstation-bot`; replace the WCM default
    `git:https://git.chifor.me` (`cchifor`) by the B1 token (`git credential reject` then `approve`).
  - New `ailab:scripts/gitea-api.sh METHOD PATH [BODY_FILE]`: fixed origin `https://git.chifor.me/api/v1`; PATH must
    match `^/[A-Za-z0-9/_.,-]+(\?[A-Za-z0-9=&_.,-]*)?$`; reads the credential with `git credential fill` inside the
    process and passes the header to `curl --proto =https --max-redirs 0 -H @-` on stdin, never with `-v` or tracing;
    prints status and body; refuses unless `/user` is `workstation-bot` with `is_admin=false`.
  - Remove the `credential.https://chifor@git.chifor.me.helper` entries and erase the `chifor` line from
    `~/.git-credentials` (`git credential-store erase`); delete WCM targets `git:https://cchifor@git.chifor.me`,
    `git:https://chifor@git.chifor.me`, `git:https://refresh_token.git.chifor.me`, `git:https://oauth2@git.chifor.me`
    (`cmdkey /delete:`). Keep `~/.gitea_tok` and `~/.gitea_cred_tmp` only until B6's 401 check.
- **B4 Verify.** Positive: `scripts/gitea-api.sh GET /user` prints `workstation-bot is_admin=False`; `git credential fill`
  resolves `workstation-bot` for every active remote (only the `username=` line is kept, `| sed -n 's/^username=//p'`;
  the `password=` line never leaves the pipe); a test push and PR from a Claude session is reviewed and automerged.
  Negative: a push to protected `main` is refused; `PATCH` of branch protection answers 403; `GET /api/v1/admin/users`
  answers 403; a non-draft no-op PR with green checks that touches an owner-protected file is refused with "Changed
  protected files" when `workstation-bot` tries to merge it (closed afterwards; a draft could never merge).
- **B5 Inventory metrics and alerts.** ailab: a custom-queries ConfigMap for infra-pg (`target_databases: [gitea]`) added
  to `monitoring.customQueriesConfigMap` next to the default one (V43). CNPG's exporter runs these inside the instance
  pod (as a superuser over the local socket per CNPG docs; UNVERIFIED for this version, proven by the series
  appearing). The queries return only counts and ages, never token hashes: per owner (`cchifor`, `chifor`, `gitea_admin`,
  every `is_admin` user, zero-filled through a left join from `"user"`) the token count, the oldest token's age by
  `created_unix`, the count of tokens outside an allowlist keyed by (id, name, scopes) (`gitea_admin`: the two documented
  ids), and the OAuth2 grant count. Rules `kubernetes/apps/infrastructure/monitoring/gitea-credential-rules.yaml`,
  labelled `release: kube-prometheus-stack` with a `severity` per rule (as E3), with promtool fixtures:
  `GiteaOrgAccountHasTokens` (`cchifor` > 0), `GiteaOwnerTokenStanding` (any `chifor` token older than 4 h),
  `GiteaAdminTokenNotAllowlisted`, `GiteaOwnerOAuthGrant` (`chifor` grants > 0), `GiteaAdminUserCountChanged` (site
  admins != 2), `GiteaCredentialInventoryMissing`: for each required metric family and each required owner
  (`cchifor`, `chifor`, `gitea_admin`) the series is absent for 30 m, so a missing owner row or query family fires
  while other inventory series are present. Fixtures distinguish a valid zero-token row (no alert) from a missing owner
  or family (alert). Three of these fire from the moment they load until B6 (`GiteaOrgAccountHasTokens` for the 26,
  `GiteaOwnerTokenStanding` for `cc-admin-20260913`, `GiteaAdminTokenNotAllowlisted` for the 39): an Alertmanager
  silence matching exactly those three alerts and owners, expiring at B6's planned date, is created with the rules.
  Fallback if the exporter cannot read the tables: a superuser-owned view returning only these aggregates, granted to
  the exporter's role.
- **B6 Observe, then revoke.** For 7 days after B3, every token slated for deletion (26 `cchifor`, `cc-admin-20260913`, the
  39 `gitea_admin`) is checked daily for an `updated_unix` change. First confirm the semantics, on a token at least
  10 minutes old (so a use cannot share its creation second or fall inside a write throttle): record its
  `updated_unix`, use it once through the API and once as a Git credential (a fetch), wait 10 minutes, read it again;
  it must have moved after each kind of use. V35's "last used" dates suggest it does. If it does not move, no per-token
  signal exists: Gitea's request logs here are not shown to name the credential (no token audit log in
  `gitea.yaml`), so before any fallback is used one known request must be found in them with usable attribution, and
  for `gitea_admin` (whose two kept tokens are active) account-level activity cannot clear any of the 39. Ambiguous
  activity blocks deletion: those tokens stay, listed for the owner, until a consumer is identified or ruled out. A
  moving token: find the consumer before revoking. Then the owner: takes a fresh snapshot of (id, name, scopes) to
  `_out/` and compares it with the watched set; deletes `gitea_admin`'s 39 and `cchifor`'s 26 through the API with
  `gitea_admin`'s basic auth (`DELETE /api/v1/users/{username}/tokens/{id}`, `reqSelfOrAdmin`, V59; one DB transaction
  only as the fallback if the API refuses); deletes `cc-admin-20260913` and revokes the GCM grant in the UI (Settings,
  Applications). Verify: the local copies get 401 (status only); a `git fetch` through the old GCM path fails or
  prompts; then delete the copies. The three silenced alerts resolve; the silence is removed.
- **B7** Remove `cchifor` from `pr_reviewer_merge_authors`; keep `chifor`.
- **B8 Docs.** New `ailab:docs/runbooks/owner-credentials.md` (identities, the owner-action procedure below, break-glass
  from B0, alerts, rules); update `s2s-identity.md:342-354` (count 26, closed items) and the spec's F-30. The owner
  updates `CLAUDE.md`'s forge paragraph (its `scripts/forge.sh` does not exist: point to `scripts/gitea-api.sh`) and the
  stale auto-memory note `trueswarm-admin-merge-identity.md`. The branch-protection re-apply block in `s2s-identity.md`
  (which reads `$OWNER_TOKEN`) is rewritten for the owner-terminal procedure.

**Owner actions from a Claude session (D11 = a).**
1. Default: the owner merges (admin override), approves, posts `approve-pin`, or edits branch protection in the Gitea web
   UI (Authelia login). The session prepares the exact action (PR, head SHA, expected checks) and holds no owner credential.
2. Scripted owner operation (for example the branch-protection re-apply): the session writes the script and shows it;
   the owner mints `owner-eph-<yyyymmddhhmm>-<purpose>` in the UI with the smallest scopes, runs the script in their own
   terminal (token via `Read-Host -AsSecureString`, kept in process memory, sent only to the fixed origin under D11's
   transport rule, never in a native command's arguments, output limited to status and non-secret fields), then deletes
   the token in the UI. Nothing reaches disk or the session.
   `GiteaOwnerTokenStanding` fires if a token survives 4 hours. The existing branch-protection block
   (`s2s-identity.md:241`, `curl -H "Authorization: token ..."`) breaks this rule and is rewritten in B8.
3. Never: a token in the chat, in a repository, in an env file, in `~/.git-credentials`, or on a dev worker.

### Item 3: rollback drill 2 (D13 = a: config-only)

Preconditions (all, or do not start): E1a live; the window announced ("assistant on the legacy panel; deploys frozen;
brief S2S failures"; about 2 hours); V52 quiet hours; the owner present throughout; open platform PRs labelled
`no-automerge` so main does not move during the window; the C4 PR open and CI-green before the window, its `sops` edit
redone if `gatekeeper-secrets.enc.yaml` changed on main since it was prepared; C8a and C8b prepared as branches stacked
on C4 (Helm 3 and Helm 4 renders and the contract scripts passing locally), each opened as a PR (`no-automerge`) only
once its prerequisite is live (reviewbot rule); the pinned web image still honours `WEB_AGENT_PANEL=legacy` (V46; if a
fleet pin removed it, the drill waits); not on the day of another gatekeeper roll. The window is one controlled
operation under the calendar rule (Sequencing).

- **C1 Legacy panel** (platform PR, `ailab.yaml` web env `WEB_AGENT_PANEL=legacy`, owner `approve-pin`), landed while the
  harness is still up. Check: the side panel and `/assistant` answer through the legacy agent (abort if not). Then wait
  until the harness's request rate has been 0 for 5 minutes.
- **C2 Baseline** (record; active probes, because quiet hours carry little traffic): `scripts/s2s/phase4-probes.sh`
  PASS; one e2e lane run; per gatekeeper pod the image digest, `service_token_minted` per client over 30 minutes,
  `gatekeeper_tokenreview_*`, restart count; edge 5xx ratio of the last hour; HelmChart `strive-ailab-strive`
  `.spec.valuesFiles`, `.metadata.generation`, `.status.observedGeneration` and `.status.artifact.revision`; gatekeeper
  `/auth` and `/auth/token` RED rates.
- **C3 Freeze**: suspend Kustomization `platform-secrets`, then `platform-app`, then the HelmRelease, then scale the
  harness to 0; times recorded. `platform-workers` and `platform-identity` wait on `platform-app` (V60).
- **C4 Rollback PR** (platform, one PR, owner-merged: `helmrelease.yaml` and `gatekeeper-secrets.enc.yaml` are protected;
  owner `approve-pin`):
  - values: revert #2125's `serviceAccountToken.gatekeeper` blocks in `ailab.yaml` and the four worker manifests (plain
    YAML, so a `git revert` of those hunks), and adjust the CI contracts that assert token mode:
    `deploy/helm/scripts/tests/check-s2s-token-mode-contract.sh` (its "ailab as committed (D.8)" split) and
    `scripts/ci/check-s2s-authority.py` (b0 backend baseline);
  - Secret: a `sops` edit of the **current** `gatekeeper-secrets.enc.yaml` that changes only `service-registry`, setting
    it to the pre-#2125 registry (the ten entries, read locally from #2125's parent revision with whichever key opens
    it, hashes only); every other key and every recipient stays as on main (A4.3's, A5.5's and A5.2's changes are kept).
    In-process checks, printing only names and OK/FAIL: the decrypted documents before and after differ only in
    `service-registry`; each of the ten argon2 hashes verifies against the matching `<svc>-secrets`
    `gatekeeper-client-secret` (D5 keeps them). The checksum is recomputed from the new ciphertext;
  - `ailab.yaml`: `harness.enabled: false`; every image digest unchanged;
  - `helmrelease.yaml`: drop the `ailab-s2s-registry.yaml` entry (the file stays in the tree).
  - **Helm-3 values caveat:** no value is set to `null`. CI renders with Helm 3 and helm-controller applies with Helm 4
    (V50), which treat a parent `null` over a subchart default differently; every switch is a positive value or a
    dropped `valuesFiles` entry.
  - Before merge, render offline with Helm 3 and Helm 4 using the two remaining files: gatekeeper
    `SVC_AUTH_BACKEND=preshared`, no `SERVICE_REGISTRY_EXTRAS_PATH`, no extras ConfigMap, no TokenReview ClusterRole or
    binding, no harness object; each of the ten services and four workers renders `GATEKEEPER_CLIENT_SECRET` and no
    projected token; the apiserver CNP is still present (V47).
  - Required checks: CI, E2E, contract, S2S Authority Guard ((b1) skipped, V48), `ailab-pins`, `owner-ack`.
- **C5 Resume**, only after the merge: resume `platform-secrets` and wait until its `lastAppliedRevision` is the merge
  sha and the live Secret's `service-registry` hash equals the committed one; then `scripts/s2s/flux-resume.sh
  --after-revert --sha <merge sha>`; record each gate's time. Then confirm what the script does not: HelmChart
  `.spec.valuesFiles` has two entries and `observedGeneration` equals `generation`; each gatekeeper pod logs
  `service_registry loaded ... clients=10` and no extras line; `platform-workers` applied the merge sha. Artifact race,
  this direction: an upgrade built from a stale three-file artifact at the C4 commit leaves gatekeeper composite with
  the restored base, which refuses each colliding extras entry alone (`service_registry.py:406-427`) and serves the ten
  as preshared, so nothing breaks and a second gatekeeper roll follows when the two-file artifact lands; the record
  says whether gatekeeper rolled once or twice. The inverse (a two-file spec built from the pre-C4 tree) is excluded by
  `flux-resume.sh` gate 1 (source artifact at the target sha before the Kustomization resumes). The re-forward's
  dangerous direction is removed by construction (C8a, C8b).
- **C6 Verify** (runbook block): `--expect-refused` PASS with its skips recorded (SA `harness` is gone) and the other-SA and
  no-token cases for `svc-harness` explicitly passing; ClusterRole and binding NotFound; `service_token_minted` > 0 on
  each pod for the restored preshared clients; `strive-pg-harness-dsn` stays `SecretSynced`; the e2e lane passes; edge
  and gatekeeper 5xx within C2's baseline. Measure the swap window from the first observed failed mint or auth-mode
  transition (a consumer or worker can switch before the first new gatekeeper is Ready: `platform-app` does not wait)
  to the verified recovery of every affected client; the ten services and the four workers (which switch only when
  `platform-workers` applies, V60) are recorded as separate numbers, with the `/auth/token` 401s per client and how
  each recovered (this is A5.4's and D5's measurement). Hold at least 30 minutes and record.
- **Abort criteria and actions.**
  - Before the C4 merge: resume `platform-secrets`, then `flux-resume.sh --after-drill`; nothing changed.
  - A `flux-resume.sh` STOP at any gate: first read and record the actual state of `platform-secrets` (already resumed
    and applied by C5), `platform-app`, `platform-workers` and the HelmRelease (`platform-app` re-applies it and can
    clear its suspend while gate 2 waits, V51); if diagnosis needs a stable state, re-freeze in C3's order; never
    un-freeze by hand.
  - The Helm upgrade fails: helm-controller retries 3 times, then rolls the release back to the pre-drill revision
    (V44). The Secret then holds the restored base while the release runs D.8, so any gatekeeper pod that restarts
    refuses the ten extras and their token mints: record it and re-forward at once, C8a and C8b back to back (the
    release already runs the registry file, so C8a's artifact case cannot arise); the owner may merge them with the
    admin override once their renders pass, without waiting for E2E, recorded as such.
  - S2S mint failures beyond 10 minutes after the roll, gatekeeper 5xx, a login failure, or an edge 5xx ratio above
    twice the baseline for 10 minutes: re-forward with C8a then C8b.
- **C7** Record: freeze to merge, merge to source artifact, each Kustomization applied, HelmChart generation and
  artifact revision before and after resume, HelmRelease upgraded, gatekeeper rolls (one or two) and their durations,
  both swap windows (services, workers), harness dark time, mints per pod and client, refusal results, edge 5xx in the
  window.
- **C8a Re-forward, registry file back** (owner-merged: `helmrelease.yaml`): restore the `ailab-s2s-registry.yaml`
  `valuesFiles` entry; nothing else (the base keeps the ten, consumers stay preshared, `harness.enabled` stays
  `false`). Same freeze (`platform-secrets` included, the harness scale answers NotFound), then
  `flux-resume.sh --after-revert --sha <sha>`. Expected: one gatekeeper roll and no client change; gatekeeper composite,
  `clients=10` and the ten colliding extras refused (`refused=10`); mints continue
  as preshared with no failure window. Its stale-artifact case (two files at this commit) is exactly C4's state, so
  harmless.
- **C8b Re-forward, switch to tokens** (owner-merged: `gatekeeper-secrets.enc.yaml`): a `sops` edit of the current
  file setting `service-registry` to `services: []` (only that key, the C4 checks reversed), the checksum recomputed,
  and the C4 values hunks and CI contract lines reverted; `valuesFiles` unchanged, so no artifact-versus-spec race
  exists. Same freeze; `platform-secrets` resumed first, then `flux-resume.sh --after-revert --sha <sha>`. Then the
  pre-flip acceptance: gatekeeper logs `clients=0` and `merged=11`, `extras_sha` equals the ConfigMap hash, each of the
  ten services mints through its projected token, the e2e lane passes, `report-ailab-pin-drift` shows 0 torn; measure
  the swap window again as in C6.
- **C9 Re-forward, the flip:** `harness.enabled: true`; then `scripts/s2s/phase4-probes.sh` PASS, #2092's checks, the
  `@api` journeys.
- **C10** Revert C1 (`WEB_AGENT_PANEL=harness`); check the panel.
- **C11** Record the drill in `s2s-identity.md` (drill 2 record with the measured windows), ADR-034 (dated note), the
  spec (including the S2S rollback paragraph, `service-to-service.md:600-603`); rewrite the runbook's drill-2 text:
  the image rollback is retired (reasons in D13), rollback after #2125 is C4's registry-only Secret edit plus values
  PR, and the re-forward is C8a then C8b. While D5 (a) holds, the inert client secrets stay as that rollback's
  material.

### Item 4: edge 5xx observability (D14)

Ownership: Traefik values live in platform `deploy/components/traefik/helmrelease.yaml` (Kustomization `platform-edge`);
Prometheus, Loki, Alloy, rules, dashboards and Gatus live in ailab.

- **E0 Preserve the 2026-10-06 evidence now** (read-only; Loki drops it about 2026-10-13 18:10Z): the redacted
  Traefik 5xx lines of 17:50-18:30Z (time, router, service, method, path without query, downstream and origin status,
  durations, Traefik pod; no client address or header), per-minute request and 5xx counts per service for that day,
  the warning and error message shapes in `strive-ailab` for 18:00-18:15Z, and Prometheus restarts, readiness and
  endpoint series for the window; what can no longer be recovered (Kubernetes events) is recorded as a limitation.
  Kept in the ailab repository as a dated record next to this plan. **Done 2026-10-07T07:59Z**:
  `plans/2026-10-07-edge-5xx-evidence.md` (findings in V55).
- **E1a Scrape Traefik** (platform PR): `metrics.prometheus.serviceMonitor.additionalLabels: {release: kube-prometheus-stack}`
  **and** `metrics.prometheus.service.enabled: true` (V53). Before merge: render chart 36.3.0 with these values and check
  the ServiceMonitor selector (`app.kubernetes.io/component: metrics`), the new Service's `metrics` port, and that the
  Traefik Deployment is unchanged (no restart; the drill's precondition depends on it). After: `up{namespace="platform-edge",service="traefik-metrics"} == 1`
  for every Traefik pod, samples younger than two scrape intervals, `traefik_entrypoint_requests_total` present.
- **E1b Attribution detail** (platform PR, after E1a, in quiet hours): `metrics.prometheus.addRoutersLabels: true`;
  `logs.access.fields.headers.names: {Cf-Ray: keep}` (every other header stays dropped). One surge restart. Record
  Traefik series and Loki bytes per day for Traefik before and after (D14's measurement and revert rule).
- **E2 Gatus external probes** (ailab `kubernetes/apps/apps/gatus/configmap.yaml`). First one `curl` from the Gatus pod
  per URL: it must reach the public path (`ailab.yaml:2234-2236` calls that a "dead public hairpin" for Sentinel; Gatus
  already probes other Cloudflare hosts) and Cloudflare must not serve it from cache (`cf-cache-status` DYNAMIC, BYPASS
  or MISS). Probes keep Gatus's own User-Agent (Cloudflare bans `Python-urllib`). Endpoints and conditions:
  `https://strive.place/auth/jwks` (`[STATUS] == 200`, `len([BODY].keys) > 0`),
  `https://auth.strive.place/realms/strive/.well-known/openid-configuration` (`[BODY].issuer == https://auth.strive.place/realms/strive`),
  and one uncacheable origin endpoint on `apps.strive.place` chosen by that curl. They prove edge-to-origin
  reachability, not login health; the generic `GatusEndpointDown` covers them.
- **E3 Rules** (ailab `kubernetes/apps/infrastructure/monitoring/strive-edge-rules.yaml` plus `.test.yaml`, listed in the
  kustomization, linted by `rules-lint`), labelled `release: kube-prometheus-stack` (the `ruleSelector`) with a
  `severity` per rule; a fixture asserts the label. Expressions sum across pods. Initial thresholds, retuned after E4:
  - `TraefikScrapeMissing`: `absent(up{namespace="platform-edge",service="traefik-metrics"}) or up{namespace="platform-edge",service="traefik-metrics"} == 0` for 15m.
  - `StriveEdge5xxRatioHigh`: entrypoint `web` 5xx / all > 5% for 10m while traffic > 0.01 req/s; `StriveEdge5xxBurst`:
    at least 10 5xx in 10m whatever the ratio. A complete low-traffic outage is also caught by E2.
  - `StriveEdgeGenerated5xx` (D14 router labels): router 5xx minus service 5xx per service over 10m > 3, with the service
    side zero-filled (`or <router expression> * 0`) so a missing service series does not drop the result. Not deployed
    until E4's test proves the arithmetic (UNVERIFIED for Traefik 3.4.3).
  - `GatekeeperForwardAuth5xx`: `sum(increase(http_server_request_duration_seconds_count{job="gatekeeper",http_route="/auth",http_status_code=~"5.."}[10m])) > 3`.
  - `GatekeeperHttp5xx`: the same for every other route, and for `/auth/token` every 5xx except 503 (fixture: sustained
    `/auth/token` 500s fire it). `GatekeeperTokenUnavailable`: any `/auth/token` 503 in each of the last 15 minutes
    (sustained, low volume included). Drills and every other planned platform-wide roll (A1.4, A1.5, C4, C8a, C8b,
    A3.2, A5.5) run under an Alertmanager silence scoped to the expected alerts (these two and
    `GatekeeperTokenReviewLimited`) and the window, instead of a permanent exemption.
  - Two rules (needs A1.0), because `for:` applies to a whole expression: `GatekeeperRedisDisconnected`:
    `max by (pod) (gatekeeper_redis_connected) == 0` for 2m (per pod, so one failed replica beside a healthy one fires,
    and a pod that starts in fallback before any counter moves is caught); `GatekeeperRedisFallbackEvent`:
    `sum by (pod) (increase(gatekeeper_redis_fallback_total[5m])) > 0`, no `for`. Fixtures: startup already in
    fallback, a brief fallback then reconnect (the event fires, the disconnected rule does not), sustained memory mode,
    one failed replica beside a healthy one.
  - `StriveTestBypassFromEdge`: `increase(gatekeeper_auth_requests_total{method="test_bypass",status="edge_refused"}[15m]) > 0` (after AG1a).
  - `GatekeeperApiKeyUsed` (D2): `increase(gatekeeper_auth_requests_total{method="api_key",status!="disabled"}[15m]) > 0` (after AG1a).
  - `GatekeeperTokenReviewLimited` (D16): `increase(gatekeeper_tokenreview_limited_total[10m]) > 0 or increase(gatekeeper_tokenreview_total{outcome="unavailable"}[10m]) > 0`.
  Each rule gets fixtures that fire and fixtures that must not (no traffic, one isolated 5xx, a missing series).
- **E4 Baseline and tests** (7 days from E1a): daily p50, p95 and maximum of the edge 5xx ratio per Traefik service; the
  count of 5xx with `OriginStatus` 0; Gatus success. One controlled test in a scratch namespace: an IngressRoute whose
  ForwardAuth points to a closed port (edge-generated), one whose origin answers 500, one never-hit service (zero-fill),
  and one Traefik restart, to confirm the router-versus-service arithmetic; enable `StriveEdgeGenerated5xx` only if it
  holds. `RequestCount` continuity per Traefik pod (resets, restarts and replicas accounted) from daily summaries
  collected during the 7 days (Loki keeps 168 h, so the summaries, not the raw lines, are the record). V55 is settled
  from E0's preserved evidence, not from Loki. Then retune the thresholds in one ailab PR.
- **E5 Dashboard** (ailab `kubernetes/apps/infrastructure/monitoring/strive-edge-dashboard.yaml`, `grafana_dashboard: "1"`):
  requests and 5xx by entrypoint and service, edge-generated 5xx, gatekeeper `/auth` outcomes by method and status,
  gatekeeper RED by route, Gatus results, and a Loki panel `{namespace="platform-edge",container="traefik"} | json | DownstreamStatus >= 500`.
- **Collector finding.** The collector is the Alloy DaemonSet writing to Loki; V55 found no partial ingestion
  (provisional until E4). The real gaps were the missing scrape (E1a), no attribution between edge and origin (E1b, E3),
  no view of failures before Traefik (E2), and 7-day retention against late investigations (accepted under D14).

## Sequencing and dependencies

Calendar rule: at most one gatekeeper-rolling or platform-wide change per day (the drill window, AG1a, AG1b, AG2,
A1.0b's purge, A1.4, A1.5, an A3.2 rotation, A4.3, A5.2 PRs, A5.5), each in quiet hours with the previous 24 hours as
its baseline (a C2-style snapshot), so each effect is attributable. Two explicit exceptions: a controlled multi-phase
operation with its own step records counts as one change and runs in one window (drill 2, C1 to C10, about 2 hours;
one A3.2 rotation, P1 to P3, about 1 hour; A5.3's steps 1-3), and an abort or emergency recovery is never held back by
the calendar. Shared control planes: no platform merges during C's window; reviewer changes (B2, B7) not on a day that
relies on a bot merge; A5.2 touches both repositories' Secrets and runs alone.

0. **E0**: done 2026-10-07 (V55).
1. **D15** protection (the `owner_ack.py` PR, then the owner's UI change), **E1a**, **E2**, **B0** to **B5**: first; the
   E4 baseline starts with E1a.
2. **E3** (rules that need neither AG1a nor E1b), **E5**, **A1.1**, **A1.2**, **A1.3**, **E1b**.
3. **AG1a**, then **A2.2**, **A4.2**, **A4.3**, **A1.0b**'s purge, and E3's AG1a rules (Redis, bypass, API key).
4. **AG1b**, then the **A6.2** soak with the canary.
5. **A1.4**, then **A1.5** in a window (both need A1.0's instrumentation live).
6. **C1 to C11** (drill 2): any time after E1a; independent of AG1a, AG1b, AG2 and A5.2.
7. **AG2** after the soak, then **A6.4**.
8. **A2.3**, **A3.1** and **A5.1** docs any time; **A3.2** after E1a.
9. **E4** test and retune 7 days after E1a; `StriveEdgeGenerated5xx` only after the E4 test.
10. **B6**, **B7**, **B8**: 7 days after B3.
11. **A5.2** to **A5.6** after AG1a, one at a time; A5.6 time-boxed (D7).
12. **A5.7** and D17's issues last (D5 = a keeps the inert client secrets).

## Critical files

**platform** (`cchifor/platform`):

| Path | Role | Protected |
| --- | --- | --- |
| `deploy/components/valkey/helmrelease.yaml` | Valkey NetworkPolicy (A1.2) and AUTH (A1.5) | after D15 |
| `deploy/helm/templates/_helpers.tpl` | `REDIS_PASSWORD` and `REDIS_URL` (A1.4) | yes |
| `deploy/components/workers/{digest,integration,mcp,workflow}-worker.yaml` | worker Redis env (A1.4); D.8 revert (C4, C8b) | no (owner-ack) |
| `deploy/helm/values/providers/ailab.yaml` | flags, pins, airlock URL, web panel, harness, D.8 blocks (A1.4, AG1a, AG1b, AG2, C1, C4, C8b-C10) | no (owner-ack) |
| `deploy/secrets/ailab/{valkey-auth,gatekeeper-signing-keys,gatekeeper-secrets,sentinel-secrets,<svc>-secrets}.enc.yaml` | rotations (A1.4, A3.2, A4.3, A5.5), drill (`service-registry` only: C4, C8b) | yes |
| `deploy/gitops/flux/clusters/ailab/app/helmrelease.yaml` | `valuesFiles` (C4, C8a) | yes |
| `infra/gatekeeper/src/app/gatekeeper/{redis,metrics}.py` | URL redaction and Redis instrumentation (A1.0) | `redis.py` after D15 |
| `infra/gatekeeper/src/app/gatekeeper/{server_session,routes_session}.py` | session-id redaction (A1.0b) | after D15 |
| `infra/gatekeeper/src/app/gatekeeper/config.py` | `api_keys_enabled` (A2.1) | yes |
| `infra/gatekeeper/src/app/api/v1/api.py`, `gatekeeper/routes.py`, `gatekeeper/apikeys_api.py` | API-key flag (A2.1), edge refusal (A4.1), login redirect and callback (A6) | after D15 |
| `infra/gatekeeper/src/app/gatekeeper/{helpers,jwks,oidc}.py` | PKCE, nonce, cookie, `validate_state`, `code_verifier` in the exchange, expected issuer (A6) | after D15 |
| `infra/keycloak-sync/src/keycloak_realm_sync/main.py`, `deploy/components/keycloak-realm-seed/realm-configmap.yaml` | PKCE enforcement (A6.4) | after D15 |
| `scripts/ci/owner_ack.py`, `scripts/ci/test_owner_ack.py` | realm-seed glob (D15) | after D15 |
| `deploy/helm/scripts/tests/check-s2s-token-mode-contract.sh`, `scripts/ci/check-s2s-authority.py` | token-mode contracts (C4, C8b) | authority script yes |
| `deploy/helm/charts/gatekeeper/{values.yaml,templates/keygen-cronjob.yaml,templates/deployment.yaml}` | comment fixes (A3.1) | yes |
| `docs/runbooks/gatekeeper-key-rotation-ailab.md`, `docs/runbooks/ailab-credential-rotation.md`, `docs/runbooks/gatekeeper-key-emergency-rotation.md` | procedures (A3.1, A5.1) | no |
| `deploy/components/traefik/helmrelease.yaml` | ServiceMonitor label and metrics Service, router labels, `Cf-Ray` (E1a, E1b) | after D15 |

**ailab** (`cchifor/ailab`):

| Path | Role |
| --- | --- |
| `ansible/roles/pr_reviewer/defaults/main.yml` | merge authors (B2, B7) |
| `scripts/gitea-api.sh` | routine credential helper (B3) |
| `kubernetes/apps/databases/infra-pg.yaml` plus a new custom-queries ConfigMap | token inventory metrics (B5) |
| `kubernetes/apps/infrastructure/monitoring/{gitea-credential-rules,strive-edge-rules}.yaml` and `.test.yaml`, `strive-edge-dashboard.yaml`, `kustomization.yaml` | alerts and dashboard (B5, E3, E5) |
| `kubernetes/apps/apps/gatus/configmap.yaml` | external probes (E2) |
| `docs/runbooks/{owner-credentials,roots-of-trust-rotation}.md`, `docs/runbooks/s2s-identity.md`, `CLAUDE.md` (owner) | procedures and records (A5.1, B8, C11) |
| `plans/2026-10-07-edge-5xx-evidence.md` | preserved 2026-10-06 evidence (E0) |
| `scripts/s2s/{flux-resume.sh,phase4-probes.sh}` | used unchanged by C |

## Verification

**F-01.** A1.2's reachability tests and 24-hour log counts. A1.4: the per-pod password fingerprints match; no literal
password rendered; Loki counts 0. A1.5: unauthenticated `PING` gets `NOAUTH`, a wrong password `WRONGPASS`, the right one
`PONG`; `gatekeeper_redis_connected` 1 on both pods; a session created before the window still valid; a login, refresh and
logout after it persist; edge 5xx within the pre-window baseline. Resolved per the spec's criterion: the NetworkPolicy
names its sources and Valkey refuses unauthenticated commands.

**F-02.** `POST /api/v1/api-keys` from an admitted pod: 404 on both pods. `/auth` with `X-API-Key` and a planted test
record: no mint (`status="disabled"` counted). `SCAN` counts 0. Spec F-02 and F-17 marked resolved by retirement.

**F-03.** The JWKS sequence `{OLD}`, `{OLD,NEW}`, `{NEW,OLD}`, `{NEW}` observed on both pods with timestamps and the
locally computed kids; probes and a journey pass after each phase; 0 gatekeeper 5xx and no `invalid_token` rise on
backends. Resolved when `/auth/jwks` has shown a key change with an overlap and the procedure is recorded.

**F-04.** Every path into Traefik enumerated; A4.2's internet request gets 401 and `edge_refused` +1; Sentinel successes
continue for every validation kind; after A4.3 the old token is refused in-cluster (401 `invalid_key`) and the new one
accepted. Resolved per the spec: an `X-Test-Token` request from the internet is no longer accepted.

**F-05.** Each procedure exists and states order, overlap, impact and rollback. Rehearsal records: A3.2; A5.2 (holders
inventoried, Flux Ready with the new key alone, data keys rotated, old key archived); A5.3 (both kids published during
the overlap, the pre-promotion session still refreshing, no terminations above baseline); drill 2's measured swap
windows (in place of A5.4's rehearsal); A5.6 (research note and outcome). D17's issues exist; F-05 is reported
"partially addressed" until they close.

**F-06.** Unit tests per A6.1; at least one canary success per pod in A6.2; live checks per A6.3 and A6.4 (including
`plain` and wrong-verifier refusals); login journeys on `strive.place` and `apps.strive.place`; `/callback` 4xx/5xx and
`/auth/login` rates within baseline the day after AG2; issuer mismatches 0.

**Item 2.** `workstation-bot is_admin=False` is the only Gitea identity the workstation resolves, and B4's negative
checks hold; `cmdkey /list` shows no `chifor`, `cchifor` or OAuth Gitea targets; the files are gone; DB: `cchifor` 0
tokens, `chifor` 0 standing tokens and no OAuth grant, `gitea_admin` only the two allowlisted ids; the B5 alerts are
loaded and their fixtures pass; the break-glass login (B0) and one owner-terminal operation (mint, run, delete, alert
silent) are recorded.

**Item 3.** C6 results, C7 measurements (services and workers separately, gatekeeper roll count, HelmChart
generations), C8a, C8b and C9 results, and the C11 records.

**Item 4.** Traefik targets up for every pod; E3 fixtures pass in `rules-lint`; one synthetic alert posted to
Alertmanager's `/api/v2/alerts` with E3's labels reaches ntfy and resolves; the E4 controlled-test outcome; the 7-day
baseline table in the dashboard description and the spec's F-31; Gatus probes green.

## Risks and residuals

- **Silent Valkey degradation.** A missed consumer or a wrong password makes consumers fall back to memory without an
  error (V8, V63). Mitigation: A1.0's instrumentation, A1.3, A1.4's fingerprint check, the F-01 error regex over
  24 h after A1.2, A1.4 and A1.5, `GatekeeperRedisDisconnected` and `GatekeeperRedisFallbackEvent`.
  Residual: F-20's silent fallback stays (fail-closed is a separate decision).
- **Secrets in logs.** Any library outside gatekeeper that logs its connection URL would leak the Valkey password into
  Loki, which the LAN can read; A1.4's substring count is the check. Session ids logged before AG1a are voided by
  A1.0b's purge. A gatekeeper rollback below AG1a after A1.4 reintroduces both leaks (AG1a's note).
- **Label-based NetworkPolicy.** Any pod in `strive-ailab` with an admitted `strive.io/service` label, or with
  `valkey-client: "true"`, reaches Valkey; creating one needs namespace write (Flux cluster-admin path, accepted D2 of
  the S2S plan). AUTH is the second layer.
- **Cloudflare-header test (F-04).** The refusal assumes every internet path transits Cloudflare (V22, A4.2). A future
  LoadBalancer or second ingress without Cloudflare would bypass it; D15 protects the Traefik component, and the
  in-cluster bypass still requires the token.
- **Helm 3 versus Helm 4.** Null semantics and server-side apply differ (V50). Mitigation: no null-based switches, dual
  renders in C4 and A1.4. Whether a `rollout restart` annotation survives a Helm 4 upgrade is UNVERIFIED (A3.2 checks).
- **Drill 2 after #2125.** Two S2S mint-failure windows by design (measured). A Helm remediation rollback mid-drill
  leaves the Secret and the release disagreeing (C8a and C8b at once). Between `platform-secrets` resuming and the
  HelmRelease upgrade, a gatekeeper restart would load the restored base early and refuse the ten extras; that gap is
  minutes. The re-forward's artifact race is removed by construction (C8a changes only `valuesFiles` while the base
  still serves everyone; C8b changes no `valuesFiles`); C4's direction is benign (C5).
- **Rollback material.** The config rollback depends on the ten inert client secrets (D5 = a keeps them) and on the
  pre-#2125 registry staying readable (the old age key is archived, A5.2 step 6).
- **PKCE enforcement lock-in.** After A6.4, a rollback below AG2 breaks every login until the attribute is removed, which
  needs `platform-identity` suspended first (A6.4's rollback).
- **Login error page.** A browser that refuses cookies cannot log in (already true of the `session_id` cookie); it sees
  the error page, never a loop.
- **Owner-equivalent credentials outside Gitea.** The workstation keeps cluster-admin (`admin@ai`), talosconfig and the
  age key; with them anyone can exec into Gitea and mint tokens (also the documented break-glass). Item 2 removes
  standing Gitea owner credentials only.
- **`updated_unix` as "last used".** B6 tests it on a 10-minute-old token for API and Git use; without a per-token
  signal, ambiguous tokens are kept and listed, not deleted.
- **API-key retirement** removes a documented (but unreachable on ailab) path for `platform__app_import`.
- **Talos SA key.** May prove infeasible without downtime; then an accepted residual with a recorded reason. After
  #2125 a mistake there breaks every S2S mint.
- **TokenReview limiter (D16).** A replay loop from an admitted pod can now turn every uncached S2S mint into 503s;
  alerted, not prevented.
- **Unprotected security code.** Closed by D15 before AG1a if decided: the hardened gatekeeper files, the whole
  keycloak-sync tree and `owner_ack.py` become owner-merged, and every realm-seed manifest (including `sync-job.yaml`)
  needs `approve-pin`. Residual (a D17 issue): the `owner-ack` job itself lives in the unprotected
  `.github/workflows/ci.yml`.
- **Not addressed here:** Valkey TLS and transport encryption (F-16); F-07 back-channel logout; F-09 credential
  concentration; F-10 tenant-scoped client credentials; F-12 tunnel drift; F-13 isolated app origin; the remaining
  "no procedure" rows of F-05, tracked by D17.

## Review response log (round 1)

Reviewers: Codex (84 inline markers, `f13de4db`) and Fable (`plans/2026-10-07-auth-hardening-and-ops-review-fable-r1.md`,
SIGN WITH CHANGES). Codex claims checked against code at `a1014dfed` before deciding; "verified" marks the ones
confirmed directly.

**(a) Codex markers: 80 ACCEPT, 4 PUSHBACK**

| # | Marker (location, gist) | Decision | Resolved in |
| --- | --- | --- | --- |
| 1 | V7: `server_session.py` logs full session ids | ACCEPT (verified: 147 lines in 24 h, Loki on a LAN NodePort) | V58, A1.0b |
| 2 | V26: cooldown arms only if the kid stays absent | ACCEPT (verified `jwks.py:160-166`) | V26, A5.3 step 1 |
| 3 | V55: completeness and incident cause provisional | ACCEPT | V55, E4 |
| 4 | D1: shared-password blast radius | ACCEPT (accepted explicitly; ACL users as option d) | D1 |
| 5 | D2: acceptance; silent re-enable | ACCEPT | D2, AG1a, E3 `GatekeeperApiKeyUsed` |
| 6 | D3: header absence; ingress paths | ACCEPT (enumeration plus `CDN-Loop`; a dedicated internal route was rejected: Sentinel must use the same host and ForwardAuth path) | D3, A4.1, A4.2 |
| 7 | D4: nothing enforces the cadence | ACCEPT (due-dated issue) | D4, A3.2 |
| 8 | D5: window estimate | ACCEPT (moot after #2125; measured in drill 2) | D5, A5.4, C6 |
| 9 | D6: premise incomplete | ACCEPT (premise reworded; A6 uses an HKDF-derived key) | D6, F-06 design |
| 10 | D7: time-box | ACCEPT | D7 |
| 11 | D8: scratch rehearsal, recovery | ACCEPT | D8, A5.2 steps 0 and 6 |
| 12 | D9: third stage with a drain | PUSHBACK | D9 |
| 13 | D10: repository-limited permissions | PUSHBACK | D10 |
| 14 | D11: helper timeout is not a lifetime | ACCEPT (lifetime stated as manual; DPAPI helper dropped) | D11, owner actions |
| 15 | D12: map consumers before cleanup | ACCEPT | D12, B6 |
| 16 | D13: gating fixes on a retired drill | ACCEPT (coordinator ruling, amended for #2125) | D13, item 3 |
| 17 | D14: series estimate | ACCEPT (measured, revert rule) | D14, E1b |
| 18 | D15: timing and list | ACCEPT (before AG1a; `oidc.py`, `key_store.py`, `server_session.py`, keycloak paths added) | D15 |
| 19 | Approach: `_out/` is not protected storage | ACCEPT | Approach |
| 20 | AG1 rollback reopens F-02/F-04 | ACCEPT (minimum safe release, feature containment, AG1 split) | AG1a, AG1b |
| 21 | A1.0: Redis counters never incremented | ACCEPT (verified: only declared, series flat at 0) | V8, A1.0, E3 |
| 22 | A1.2: render the Flux chart, test other namespaces | ACCEPT | A1.2 |
| 23 | A1.3: other consumers' libraries | ACCEPT (found digest on redis-py 8.0.0; both versions tested) | V63, A1.3 |
| 24 | A1.4: a wrong password passes against nopass | ACCEPT (fingerprint comparison) | A1.4 step 3 |
| 25 | A1.5: fallback loses logins, logouts | ACCEPT (impact stated, window rule, after-window persistence checks) | A1.5 |
| 26 | A2.2: purge preconditions | ACCEPT | A2.2 |
| 27 | F-02 stays open until retirement | ACCEPT | F-02 status |
| 28 | A3.2: gate on revision and fingerprints | ACCEPT | A3.2 |
| 29 | A3.2 P1: the wait does not preload caches | PUSHBACK | A3.2 P1 |
| 30 | A3.2: chained reverts | ACCEPT | A3.2 rollback |
| 31 | Emergency variant: cache revocation bound | ACCEPT | A3.2 emergency |
| 32 | F-04 rollback: disable the flag first | ACCEPT | A4 rollback, AG1a |
| 33-35 | A5.2: new-only check isolation; data-key rotation; archive and fixed path | ACCEPT | A5.2 steps 3-6 |
| 36-38 | A5.3: kcadm config; realm-wide retention; old-key continuity | ACCEPT | A5.3 |
| 39 | A5.4: non-atomic swap | ACCEPT (moot after #2125; the gates live in C5/C6) | A5.4, C5, C6 |
| 40 | A5.5: rolling single-key change; resurrection | ACCEPT | A5.5 |
| 41 | A5.6: add-verifier phase first | ACCEPT | A5.6 |
| 42-49 | F-06 design: cookie bounds; binding and `__Host-`; routing and `idp_hint`; retry bound; required claims and tenant hook; error branches; `validate_state` cases; issuer vs fetch URL | ACCEPT (no auto-retry; `validate_state` verified to pass `/\host`) | F-06 design, V62 |
| 50 | A6.1: discriminator and fixed labels | ACCEPT | F-06 design, A6.1 |
| 51 | A6.2: soak never exercises the new path | ACCEPT (canary per pod) | A6.1, A6.2 |
| 52 | A6.3: start/callback matrix | ACCEPT | A6.1 tests, A6.3 |
| 53 | A6.4: reconciler rollback; token-endpoint tests | ACCEPT | A6.4 |
| 54-56 | B3/B4: helper origin; Git config inventory; negative checks | ACCEPT | B3, B4 |
| 57-59 | B5: exporter role; zero-fill and absence; OAuth grants and allowlist by id | ACCEPT | B5 |
| 60-61 | B6: `updated_unix` semantics; cache invalidation | ACCEPT (one replica; API delete or restart) | V59, B6 |
| 62 | DPAPI boundary | ACCEPT (helper dropped) | D11, owner actions |
| 63 | C3: freeze misses `platform-secrets`, merges | ACCEPT | item 3 preconditions, C3, C5 |
| 64 | C3: drain harness traffic | ACCEPT | C1 |
| 65 | C5: valuesFiles race gate | PUSHBACK | C5 |
| 66 | C6: probes skip harness cases | ACCEPT | C6 |
| 67 | STOP at gates 2-4 leaves Flux resumed | ACCEPT (verified `flux-resume.sh:198-217`) | V51, C abort |
| 68 | `maxUnavailable` vs remediation | ACCEPT (config-only removes the image path; remediation recorded) | V44, C abort |
| 69 | Quiet-hour baseline; prepared recovery PR | ACCEPT | preconditions, C2, C abort |
| 70 | C11: historical rollback not the kept one | ACCEPT (ruling) | D13, C11 |
| 71 | E1a: inspect the render | ACCEPT (with Fable 1) | V53, E1a |
| 72 | E2: cached 200s | ACCEPT | E2 |
| 73-76 | E3: scope; traffic floor; zero-fill; aggregation, low volume, silences | ACCEPT | E3 |
| 77 | E4: more controls, per-pod continuity | ACCEPT | E4 |
| 78-80 | Sequencing: missing steps; calendar; shared control planes | ACCEPT | Sequencing |
| 81 | `oidc.py` lacks `code_verifier` | ACCEPT (verified `oidc.py:26-76`) | V62, F-06 design, critical files, D15 |
| 82 | F-01 verification: +1 reconnection unreliable | ACCEPT | Verification F-01 |
| 83 | Alert delivery unproven | ACCEPT | Verification item 4 |
| 84 | Remaining F-05 rows need decisions | ACCEPT | D17, A5.7, Verification F-05 |

**(b) Fable findings and owner-decision opinions: 19 of 19 addressed, none rejected (2 adopted with changes)**

| # | Finding | Disposition | Resolved in |
| --- | --- | --- | --- |
| 1 | E1a needs the metrics Service | Adopted | V53, E1a |
| 2 | A1.1-A1.4 checks gatekeeper only; no window | Adopted; LIVE adds that no CronJob/Job carries a Redis env | V5, A1.1-A1.4 |
| 3 | OIDC cookies accumulate | Adopted; the cookie becomes `__Host-`, `Path=/` so `/auth/login` prunes as well | F-06 design, A6.1 |
| 4 | A6.4 rollback undone by keycloak-sync | Adopted | A6.4, risks |
| 5 | No break-glass | Adopted (break-glass found: `gitea/gitea-admin`) | V59, B0, B8 |
| 6 | D13 gates fixes on a retired drill | Adopted (ruling); amended: after #2125 the config-only rollback must restore the preshared base | D13, item 3 |
| 7 | Split AG1 | Adopted | AG1a, AG1b |
| 8 | age-key holder inventory | Adopted | A5.2 step 0 |
| 9 | Protect keycloak-sync and realm seed | Adopted with a change: `main.py` and `realm-configmap.yaml` only, so `sync-job.yaml` pin bumps stay bot-mergeable | D15 |
| 10 | Stale pin | Adopted (`a1014dfed`) | Pinned references |
| 11 | Stale Valkey comment; gatekeeper-shaped A1.3 | Adopted | V1, V8, A1.3, A1.5 |
| 12 | E3 rule label and severity | Adopted | E3 |
| 13 | E2 feasibility, UA | Adopted | E2 |
| 14 | Simpler owner-terminal procedure | Adopted | D11, owner actions |
| 15 | B5 fallback role unnecessary | Adopted (fallback is an aggregate-only view) | B5 |
| 16 | `scripts/forge.sh` does not exist | Adopted (verified) | V59, B8 |
| 17 | `CDN-Loop`; why headers | Adopted with a correction: Sentinel's peer is its own pod IP, not cloudflared's; both are dynamic pod IPs | A4.1, A4.2 |
| 18 | C1 depends on the web image | Adopted (precondition) | item 3 preconditions |
| 19 | A3.2 one-write gate and bound | Adopted (plus Codex's fingerprint gate) | A3.2 |
| D1-D4 | agree | unchanged; D2 and D4 gain an alert and a due-dated issue (Codex) | D1-D4 |
| D5 | agree | superseded by #2125: rescoped to deleting inert secrets | D5 |
| D6 | agree (a) | kept; premise reworded per Codex | D6 |
| D7, D8 | agree with time-box, inventory | adopted | D7, D8 |
| D9, D10 | agree | unchanged (Codex pushbacks recorded) | D9, D10 |
| D11 | prefer the owner-terminal variant | adopted | D11 |
| D12 | add break-glass | adopted | D12, B0 |
| D13 | disagree; config-only drill | adopted | D13 |
| D14 | agree | adopted with measurement | D14 |
| D15 | add keycloak paths | adopted (narrowed, see 9) | D15 |

New or changed owner decisions: D5 (rescoped after #2125), D11 (owner terminal, no DPAPI helper), D13 (config-only
drill that restores the preshared base, gates nothing), D15 (before AG1a, wider list), D16 (new: TokenReview limiter
after #2125), D17 (new: remaining F-05 rows).

## Review response log (round 2)

Reviewers: Codex round 2 (verdict DO NOT SIGN). The dispatch ran the same prompt twice in parallel: one run edited the
plan in its worktree (18 markers, committed as `197e1206`), the other returned the file as its final message (13
markers: the same findings reworded, plus one new one, row 19). Fable round 2
(`plans/2026-10-07-auth-hardening-and-ops-review-fable-r2.md`, SIGN WITH CHANGES once its findings 1-3 are folded in).

Round-1 pushbacks: Codex dropped D9, D10 and A3.2 P1, and Fable sided with the plan on all four after checking the
code. Codex strengthened C5 (row 12), accepted below with a different mechanism.

**(a) Codex round 2: all ACCEPT**

| # | Marker (location, gist) | Decision | Resolved in |
| --- | --- | --- | --- |
| 1 | D5: deleting the inert secrets breaks the kept rollback | ACCEPT (same as Fable 4): D5 (a) now keeps them while the config rollback is documented | D5, C11, Sequencing 12 |
| 2 | D15: `sync-job.yaml`, `Dockerfile`, `pyproject.toml` still bypass | ACCEPT: `infra/keycloak-sync/**` protected; `keycloak-realm-seed/*.yaml` under `owner-ack` | D15, Critical files, Risks |
| 3 | A5.2 step 3: the new-key-only check is not isolated | ACCEPT: `env -i`, empty config dirs, one sops binary, negative control first | A5.2 step 3 |
| 4 | A5.2 step 4, not step 5, blocks C4 | ACCEPT, resolved at the root: C4 and C8b never apply a historical ciphertext, so A5.2 and drill 2 are independent | Change-control rules, A5.2 step 5, C4, Sequencing 6 |
| 5 | Cookie bound under concurrent starts | ACCEPT: prune by encrypted `iat` to 2 plus the new one, concurrent overshoot accepted and tested | F-06 design, A6.1 |
| 6 | "No exchange call on any failure" is impossible | ACCEPT: zero calls before the exchange, exactly one for exchange-stage failures | A6.1 |
| 7 | B3 and B4 could print credentials | ACCEPT: redaction inside the process; only `username=` kept | B3, B4 |
| 8 | B5 labels; a missing owner row goes unseen | ACCEPT: E3's labels; `InventoryMissing` per family and owner, with fixtures | B5 |
| 9 | B6 `updated_unix` test; attribution | ACCEPT: 10-minute-old token, API and Git use; ambiguous activity blocks deletion | B6, Risks |
| 10 | D11: the SecureString can still reach argv | ACCEPT (same as Fable nit 10) | D11, Owner actions 2 |
| 11 | C4 whole-file restore undoes A4.3, A5.5, A5.2 | ACCEPT (same as Fable 1): a `sops` edit of `service-registry` only, with in-process checks | Change-control rules, C4, C8b |
| 12 | C5: C8's inverse artifact race strands every client | ACCEPT, by construction instead of a gate: C8a changes only `valuesFiles` while the base still serves everyone; C8b changes the Secret and consumers but no `valuesFiles`. Neither step's stale-artifact case can strand a client | D13, C5, C8a, C8b, Risks |
| 13 | C6: the swap window can start before gatekeeper rolls | ACCEPT: measure from the first failed mint or auth-mode change; workers separately | C6, C7 |
| 14 | Abort: STOP states assume too much | ACCEPT: record the actual controller states, then re-freeze | Abort criteria |
| 15 | `GatekeeperHttp5xx` misses `/auth/token` 500/502/504 | ACCEPT | E3 |
| 16 | `for:` cannot apply to one side of `or` | ACCEPT: two rules, per-pod gauge | E3, Risks |
| 17 | The 2026-10-06 evidence expires before E4 | ACCEPT: new step E0, done on 2026-10-07 (`plans/2026-10-07-edge-5xx-evidence.md`) | E0, E4, V55, Sequencing 0 |
| 18 | The calendar conflicts with drill 2 and A3.2 | ACCEPT: explicit exceptions for controlled multi-phase operations and for aborts | Sequencing, D4, item 3 preconditions |
| 19 | A draft PR's refused merge proves nothing (final-message run) | ACCEPT | D15, B4 |
| - | Summary: the emergency JWKS bound is not 600 + 1800 s; 330 s covers only minted tokens | ACCEPT | A3.2 emergency variant |

**(b) Fable round 2: all ACCEPT**

| # | Finding | Decision | Resolved in |
| --- | --- | --- | --- |
| 1 | C4/C8: a `git revert` of the SOPS file (sign condition) | ACCEPT | Change-control rules, C4, C8b, A5.2 step 5 |
| 2 | D15: the realm-sync pin is neither acked nor checked (sign condition) | ACCEPT: `owner_ack.py` glob, landed before the UI change | D15 |
| 3 | The AUTH-error terms fell out of the log regex (sign condition) | ACCEPT: one F-01 error regex for A1.2, A1.4 and A1.5 | A1.2, A1.4, A1.5, Risks |
| 4 | Rollback after D5's deletion | ACCEPT (Fable's preferred option: keep the keys) | D5 |
| 5 | V62 overstated; test the new sink | ACCEPT | V62, F-06 design, A6.1 |
| 6 | Name the contracts; C8 is not an open dependent PR | ACCEPT | C4, item 3 preconditions |
| 7 | Record the artifact path; workers separately | ACCEPT | C2, C5, C6, C7 |
| 8 | Three B5 alerts fire until B6 | ACCEPT: one scoped silence until B6 | B5, B6 |
| 9 | B6 can delete through the API | ACCEPT (V59 updated; the DB transaction only as the fallback) | V59, B6 |
| 10 | D11 transport | ACCEPT | D11 |
| 11 | The second tab loses its cookie | ACCEPT: delete the matched and invalid cookies only; reuse `denials._is_navigation` | F-06 design, A6.1 |
| 12 | `GatekeeperTokenReviewLimited` on planned rolls | ACCEPT: the silence covers every planned platform-wide roll | E3 |
| 13 | The pin is stale again | ACCEPT (verified: `b5b70c0c4` touches no cited path) | Pinned references |
| 14 | The harness's 60 s edge during A3.2 P2 | ACCEPT | A3.2 P2 |

**(c) Added while folding:** `scripts/ci/owner_ack.py` and its test were themselves bot-mergeable, so a bot PR could
shrink the owner-ack list. They are now in D15. The `owner-ack` job in `.github/workflows/ci.yml` is recorded as a
D17-style residual.

**Convergence.** No review markers remain. Codex's DO NOT SIGN rested on five points, and each is now addressed in the
plan text:

| Codex's reason | Now addressed by |
| --- | --- |
| Obsolete secrets restored | Registry-only `sops` edit (C4, C8b) |
| An empty S2S registry deployed | The C8a/C8b split |
| The age-key prerequisite | Removed, since no historical ciphertext is applied |
| Recovery after cleanup | D5 keeps the keys |
| The Keycloak bypass | D15 widened |

Fable's three sign conditions are folded in. Neither reviewer has read this revision: the two-round cap applies.

<!-- codex-review-status: finalized -->
