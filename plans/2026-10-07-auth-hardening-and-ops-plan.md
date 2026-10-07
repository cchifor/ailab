# Strive auth hardening (F-01 to F-06), owner workstation credentials, S2S rollback drill 2, edge 5xx observability

**Status: DRAFT for cross-review** (Codex and a second model must both sign). Nothing in this plan has
been executed. Every live observation below was read-only (`kubectl --context admin@ai` get/describe,
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

**Pinned references.** platform `gitea/main` = `5b453c03f8a3` (2026-10-07); ailab `origin/main` =
`c5a397a1`. Citations: `platform:<path>:<lines>` and `ailab:<path>:<lines>` at those commits; **LIVE** = read
on 2026-10-07 unless dated; **UNVERIFIED** = not confirmed from code or the live system.

**Change-control rules that shape every step** (from the S2S work, not repeated per step):
- Platform owner-protected file patterns (26, `ailab:docs/runbooks/s2s-identity.md:137-164`) can only be
  merged by the owner in person (admin override). Of the files this plan touches, PROTECTED are:
  `deploy/helm/templates/_helpers.tpl`, `deploy/helm/charts/gatekeeper/**`, `deploy/secrets/ailab/**`,
  `deploy/gitops/flux/clusters/ailab/**`, `infra/gatekeeper/src/app/gatekeeper/config.py`,
  `infra/gatekeeper/src/app/core/lifecycle.py`, `infra/gatekeeper/src/app/main.py`. NOT protected (bot-approvable,
  D2 residual): `routes.py`, `helpers.py`, `jwks.py`, `redis.py`, `apikeys*.py`, `metrics.py`,
  `infra/gatekeeper/src/app/api/v1/api.py`, `deploy/components/**`, `deploy/helm/values/providers/ailab.yaml`
  (but `ailab.yaml` and `deploy/components/workers/*.yaml` need the owner's `approve-pin`, `platform:docs/runbooks/owner-ack.md:12-62`).
- Every image-pin move in `ailab.yaml` needs a `<!-- pin-bump:v1 -->` block (`platform:docs/runbooks/ailab-pin-bump.md`).
- Reviewbot rule: never open a dependent PR before its prerequisite is live.
- "Owner" means `chifor` acting in person. Where this plan says "owner merge" the default mechanism is the
  Gitea web UI (no token on disk), per item 2.

**Out of scope**: F-07 to F-31 except where an item needs them (F-17 and F-20 are touched by F-02 and F-01; F-30 is
item 2; F-31 is item 4); Valkey TLS and in-cluster transport encryption (F-16); moving preshared clients to `k8s`
identities; the owner's kubeconfig, talosconfig and age key as workstation credentials (named as a residual in
item 2); the hand-applied Strive tunnel (F-12); the Forge realm.

## Verified facts

| # | Fact | Evidence |
| --- | --- | --- |
| **F-01 Valkey** | | |
| V1 | Valkey runs `bitnamilegacy/valkey:8.0.1` via chart `valkey` 1.0.3, standalone, `auth.enabled: false`; the comment says the umbrella helper builds a password-less URL for every consumer and that Valkey is "NetworkPolicy-gated". | `platform:deploy/components/valkey/helmrelease.yaml:38-82`; LIVE HelmRelease Ready, 1.0.3 |
| V2 | NetworkPolicy `valkey` ingress rule has no `from` (any source on 6379); egress `{}`; no CiliumClusterwideNetworkPolicies. | LIVE `get netpol valkey -o yaml`; `get ciliumclusterwidenetworkpolicies` empty |
| V3 | Chart default `networkPolicy.allowExternal: true`. With `false` the rule admits only pods labelled `valkey-client: "true"`, Valkey's own pods, and anything in `networkPolicy.extraIngress`. | bitnami `valkey` 1.0.3 `values.yaml:1492-1552`, `templates/networkpolicy.yaml` (pulled from `oci://registry-1.docker.io/bitnamicharts/valkey:1.0.3`; the cluster uses the HTTPS index of the same version; byte equality UNVERIFIED) |
| V4 | `REDIS_URL` is a literal, password-less `redis://valkey-master.<ns>.svc.cluster.local:6379` rendered by the owner-protected helper for every chart that includes `strive.envFromGatekeeper` (24 includes, gatekeeper among them); the four worker manifests and airlock's `APP__AIRLOCK__RATE_LIMIT_REDIS_URL` set the same literal. | `platform:deploy/helm/templates/_helpers.tpl:293-294`; `deploy/helm/charts/gatekeeper/templates/deployment.yaml:97`; `deploy/components/workers/{digest,integration,mcp,workflow}-worker.yaml` (`REDIS_URL` at :103, :98, :86, :171); `ailab.yaml:1780-1781` |
| V5 | 16 workloads (17 pods) carry a Redis URL env: airlock, deepagent, digest, digest-worker, gatekeeper (x2), integration, integration-worker, knowledge, mcp, mcp-worker, notification, profile, sentinel, tms, workflow, workflow-worker. All carry label `strive.io/service=<name>`. No pod outside `strive-ailab` targets this Valkey (open-webui's URLs point elsewhere). | LIVE pod env NAMES and labels |
| V6 | TMS reads `APP__TMS__REDIS_URL` (default `redis://redis:6379`) and has no such env; no Service `redis` exists. TMS therefore most likely never reaches Valkey, so no `tenant-route:*` rows are written on ailab (spec open question 2). Inferred, not observed on the wire. | `platform:infra/tms/src/app/core/config/domain.py:119`, `loader.py:35-36`, `infra/tms/config/default.yaml:108`; LIVE env names, `get svc redis` NotFound |
| V7 | Gatekeeper logs the full Redis URL at INFO twice. A password embedded in the URL would reach Loki, which is unauthenticated on the LAN. | `platform:infra/gatekeeper/src/app/gatekeeper/redis.py:467,664`; `ailab:kubernetes/apps/infrastructure/monitoring/alloy.yaml:98` |
| V8 | Gatekeeper silently falls back to per-process memory on any `redis.ConnectionError`/`TimeoutError`/`OSError` and reconnects with exponential backoff capped at 60 s. redis-py's `AuthenticationError` subclasses `ConnectionError`, so a wrong password also degrades silently (library behaviour for the pinned redis-py: UNVERIFIED). | `redis.py:34-39,456-548` |
| V9 | Valkey persistence: AOF on (`appendonly yes`, `save ""`), PVC 8Gi `nfs-csi`; one replica, so any change to the StatefulSet template restarts the only pod. | LIVE ConfigMap `valkey-configuration` (non-secret), PVC, StatefulSet |
| V10 | Redis/Valkey ACL semantics this plan relies on: while user `default` is `nopass`, two-argument `AUTH default <anything>` succeeds and one-argument `AUTH <x>` errors. UNVERIFIED for `bitnamilegacy/valkey:8.0.1`; step A1.3 proves it before use. | Redis ACL documentation (`nopass`) |
| **F-02 API keys** | | |
| V11 | The management API takes tenant and owner from `X-Gatekeeper-Tenant`/`X-Gatekeeper-User-Id` with no role check, contrary to its docstring; it is mounted under `/api/v1`. | `platform:infra/gatekeeper/src/app/gatekeeper/apikeys_api.py:1-9,117-196`; `api/v1/api.py:4,9`; `main.py:72` |
| V12 | Nothing in `apps/web`, `services/*` or `infra/tms` calls `/api-keys`; gatekeeper's RED metrics show no `/api/v1/api-keys` route in 7 days; spec: no `api_key` `/auth` traffic in 30 days. API-key tokens carry a slug that weld rejects (F-17), and `platform__app_import` is unreachable on ailab anyway. | `git grep` at 5b453c03f; LIVE Prometheus `http_server_request_duration_seconds_count{job="gatekeeper"}` by route; `ailab.yaml:188-189` |
| V13 | The `X-API-Key` track runs first in `/auth`; when the header is present only that track runs. | `routes.py:741-784` |
| **F-03 signing key** | | |
| V14 | Public JWKS has one ES256 key, `kid` `8b77549a4272d160`; the SOPS file last changed 2026-06-18 (`5065f9982`). | LIVE `GET https://strive.place/auth/jwks`; `git log` of `deploy/secrets/ailab/gatekeeper-signing-keys.enc.yaml` |
| V15 | `FileKeyRing` reads `active.pem` (required), `retiring.pem`, `pending.pem` once at startup; all loaded keys are published; only `active` signs. | `platform:infra/gatekeeper/src/app/gatekeeper/key_store.py:113-205` |
| V16 | The whole Secret `gatekeeper-signing-keys` is mounted (no `items`) at `/var/run/secrets/gatekeeper-signing`, `optional: true`; no checksum annotation follows it. Gatekeeper rolls `maxUnavailable: 0`, 2 replicas, PDB `minAvailable: 1`. | `platform:deploy/helm/charts/gatekeeper/templates/deployment.yaml:184-216,19`; spec |
| V17 | Every gatekeeper-side verification (cached internal JWT re-check, token-exchange subject token) uses the full published set, so a `pending` or `retiring` key verifies. A cached token whose `kid` left the ring is treated as a miss and re-minted. | `internal_token_cache.py:70-103`; `service_token.py:558-564` |
| V18 | Verifier caches: weld 600 s lifespan, 1800 s stale, unknown-`kid` refetch without cooldown; harness 600 s lifespan, and after a successful unknown-`kid` refetch further unknown kids are refused for 60 s. Internal JWT `exp - iat` <= 300 s, weld skew 30 s. | `platform:sdks/weld-auth/src/weld/auth/jwks.py:33-34`; `services/harness/src/plugins/identity-gatekeeper/jwks.ts:17-23,62-64,85-86`; spec |
| V19 | The keygen CronJob is disabled on ailab; the values comment claims gatekeeper self-generates its key, which is false (the kustomization says it must be pre-seeded). The Secret is Flux-applied from SOPS by `platform-secrets`, so an in-cluster patch (the CronJob's design) would be reverted on the next reconcile (inferred from Flux apply semantics). | `ailab.yaml:444-449`; `platform:deploy/secrets/ailab/kustomization.yaml:33-37` |
| **F-04 test bypass** | | |
| V20 | `TEST_BYPASS_ENABLED=true`, token from `gatekeeper-secrets/test-bypass-token`, tenant allowlist = operator tenant, paths `/sandbox/,/api/airlock/,/api/v1/apps/`; evaluated before the session; 165 successes in 7 days (spec). | `ailab.yaml:415-441`; `routes.py:454-472,786-855` |
| V21 | Sentinel reaches `strive.place` through the in-cluster Traefik, not Cloudflare: Chromium maps `strive.place` to `traefik.platform-edge.svc`, httpx uses `hostAliases` 10.97.5.57 (the Traefik ClusterIP). | `ailab.yaml:2069,2088`; LIVE `svc/traefik` 10.97.5.57 |
| V22 | The platform-edge Traefik is `ClusterIP` only; internet traffic arrives only through Cloudflare tunnels, and Cloudflare sets `CF-Ray`/`CF-Connecting-IP` on every proxied request (the guest limiter already relies on `cf-connecting-ip`, `ailab.yaml:314-359`). That no non-Cloudflare path reaches Traefik from outside the cluster is inferred from the Service type and the absence of an LB. | LIVE `get svc,deploy -n platform-edge`; `deploy/components/traefik/helmrelease.yaml:35-38` |
| V23 | The `gatekeeper-auth` middleware sets only `authResponseHeaders`; Traefik then forwards every request header to `/auth` (Traefik documentation; UNVERIFIED in-repo, tested in A4.2). | LIVE middleware; spec |
| **F-05 roots of trust** | | |
| V24 | One age recipient (`age1nfa6hh...`) encrypts all 74 ailab SOPS files and the 23 platform `deploy/secrets/ailab/*.enc.yaml`. The key file exists only at `C:\Users\chifo\work\home\ailab\kubernetes\infra\_out\age.agekey` on the workstation; `flux-system/sops-age` holds one key `age.agekey`, hand-applied (last-applied annotation, no labels). | `ailab:.sops.yaml`; `platform:.sops.yaml` (ailab rule); LIVE Secret metadata (key names only) |
| V25 | Talos `rotate-ca --kubernetes` rotates only the Kubernetes API CA; "other Kubernetes secrets might need to be rotated manually". No Talos procedure exists for the ServiceAccount signing key (`cluster.serviceAccount.key`). | docs.siderolabs.com, Talos v1.11 "CA rotation" |
| V26 | Realm `strive` publishes one RS256 signing key and one RSA-OAEP key. Gatekeeper caches Keycloak JWKS 900 s; an unknown `kid` forces one refetch, then a 60 s cooldown; a token that does not verify after the refetch terminates the session. | LIVE `GET https://auth.strive.place/realms/strive/protocol/openid-connect/certs`; `platform:infra/gatekeeper/src/app/gatekeeper/jwks.py:100-167`; spec |
| V27 | Preshared clients: one argon2id hash per client, no overlap; any `gatekeeper-secrets` edit must bump `gatekeeper.serviceRegistry.checksum` (CI guard), which rolls gatekeeper; consumer pods are not rolled by a Secret change (UNVERIFIED, spec). | `ailab.yaml:205-219`; spec |
| V28 | Session, guest and delegation encryption use single `Fernet` objects (no `MultiFernet`); rotating a key invalidates every session or grant. | `platform:infra/gatekeeper/src/app/core/lifecycle.py:163,181,210`; `config.py:328-333` |
| **F-06 OIDC** | | |
| V29 | The authorize redirect carries no `code_challenge`, no `nonce`, and `state` is the return path. It is built in `build_login_url` from two call sites: `GET /auth/login` and the ForwardAuth login redirect. | LIVE `curl -sD- https://strive.place/`; `helpers.py:119-165`; `routes.py:358-401,1611-1650` |
| V30 | `/callback` only applies the open-redirect check to `state`, exchanges the code, decodes the access token without verification, and stores the ID token unverified. | `routes.py:1652-1760`; `helpers.py:409-422` |
| V31 | `verify_token` passes audience but no issuer. The live issuer is `https://auth.strive.place/realms/strive`, also on back-channel calls. | `jwks.py:180-225`; spec (LIVE) |
| V32 | keycloak-sync already reconciles client redirect URIs, and its bootstrap demands S256 PKCE for public clients: the pattern for enforcing PKCE on `gatekeeper`. | `platform:infra/keycloak-sync/src/keycloak_realm_sync/main.py:576-729`; `bootstrap.py:119-121` |
| V33 | Login volume: 395 `/callback` and 80 `/auth/login` answers in 7 days (about 2.4 logins per hour). | LIVE gatekeeper RED metrics |
| **Item 2 credentials** | | |
| V34 | Gitea users: `gitea_admin` (uid 1, site admin), `cchifor` (uid 2, type 1 = organization, a converted user), `chifor` (uid 3, site admin, `login_type` 6 = OAuth2 via Authelia). | Gitea DB `"user"` |
| V35 | Token counts: `cchifor` **26** (the spec and runbook say 21: drift), `chifor` 1 (`cc-admin-20260913`: `read:organization,write:issue,write:repository,read:user`, last used 2026-10-07 06:06Z), `gitea_admin` 41. | Gitea DB `access_token` (names, scopes, dates only) |
| V36 | Of the 26 `cchifor` tokens, only `ver040-1786337710` (`write:issue,write:repository`) was used in the last 30 days (2026-10-07 06:06Z). The other 25 were last used between 2026-08-10 and 2026-09-03; three carry `write:organization` (`reviewbot-setup2-2026-09`, `reviewbot-2026-09`, `cloudlab-bootstrap`). | same |
| V37 | `gitea_admin`: only `af-ci-scaler-2941` (`read:admin`) and `flux-ailab-read` (`read:repository`) are documented consumers (last used 2026-09-17); 39 others were last used 2026-07-12 to 2026-08-11, including `stage0-ops-1784874768` with `write:admin`. | same; `ailab:docs/runbooks/s2s-identity.md:351-353` |
| V38 | Gitea access tokens cannot expire in this deployment (no expiry column). DB-row deletion revokes at once (D1 precedent, verified 401). Whether `updated_unix` moves on every use is assumed from Gitea's auth code (UNVERIFIED); the timestamps above are consistent with known consumers. | DB schema; `ailab:docs/runbooks/s2s-identity.md:71-72` |
| V39 | `chifor` has an OAuth2 grant to the built-in "Git Credential Manager" app (2026-08-05); the workstation holds GCM OAuth entries `git:https://refresh_token.git.chifor.me` and `git:https://oauth2@git.chifor.me`. Whether that refresh token is still valid is UNVERIFIED. | DB `oauth2_grant`; `cmdkey /list` (target names) |
| V40 | Workstation Gitea credentials (names only): WCM `git:https://git.chifor.me` (user `cchifor`, the default), `git:https://cchifor@git.chifor.me`, `git:https://chifor@git.chifor.me`; `~/.git-credentials` one entry `chifor@git.chifor.me`; files `~/.gitea_tok`, `~/.gitea_cred_tmp` (contents not read); `credential.helper=manager`, `credential.https://chifor@git.chifor.me.helper=store`. Which token each holds is taken from the brief (OBSERVED by the coordinator), not re-read. | `cmdkey /list`; `git config --get-regexp credential`; `ls -la` |
| V41 | Six Gitea Actions secrets have token-like names; all but `FORGE_RELEASE_TOKEN` (release-bot, 2026-10-05) were created before the first `cchifor` token (2026-08-10), so none holds a `cchifor` token unless re-set later (the table has no update column). | DB `secret` (names and dates) |
| V42 | `pr_reviewer_merge_authors` includes `cchifor` and `chifor`. | `ailab:ansible/roles/pr_reviewer/defaults/main.yml:216` |
| V43 | infra-pg (CNPG) runs the default custom-queries ConfigMap; its metrics port is scraped through the hand-written PodMonitor `infra-pg-metrics`. | LIVE Cluster spec; `ailab:kubernetes/apps/databases/infra-pg.yaml:286-300` |
| **Item 3 drill 2** | | |
| V44 | HelmRelease `strive` and HelmChart `strive-ailab-strive` list three `valuesFiles`, the registry file last. | `platform:deploy/gitops/flux/clusters/ailab/app/helmrelease.yaml:21-24`; LIVE |
| V45 | Gatekeeper is pinned to `sha256:3adaf0be...` (`sha-1e1e33775`). The pre-Phase-3 rollback target `sha256:45abbd52...` (`sha-19363455156f`) is still served by the registry (HEAD 200). Between them gatekeeper changed only by #2101 (S2S), #2105 (S2S) and a test-flake fix. | `ailab.yaml:191-202`; registry HEAD 2026-10-07 |
| V46 | Darkening the harness requires `WEB_AGENT_PANEL=legacy` first; the current web image still honours `legacy`. | `ailab.yaml:1444-1447,2172-2178` |
| V47 | TokenReview RBAC renders only with composite on; the apiserver CNP renders regardless (allow rule only). | `charts/gatekeeper/templates/tokenreview-rbac.yaml:1`; `ailab.yaml:220-231` |
| V48 | With the registry file not listed, the S2S Authority Guard runs (a), (b), (b0), (b2); (b1) runs only when it is listed. | `platform:scripts/ci/check-s2s-authority.py:13-60,322-324` |
| V49 | The pre-Phase-3 image rejects `SVC_AUTH_BACKEND=composite` (Literal) and ignores unknown env (`extra="ignore"`). | signed plan "Misconfiguration contract"; `ailab.yaml:357` |
| V50 | helm-controller is v1.5.5, which embeds **Helm v4.2.0**; v1.5.0 switched new HelmReleases to server-side apply, existing ones keep client-side apply until their `.spec` changes. Platform CI renders with Helm 3, which (unlike Helm 4) ignores a parent overlay's `null` over a subchart default. Which apply method `strive` uses now is UNVERIFIED. | LIVE image tags; `fluxcd/helm-controller` v1.5.5 `go.mod`, CHANGELOG v1.5.0; `platform:deploy/helm/templates/_helpers.tpl:350-352`, `deploy/helm/scripts/tests/check-harness-chart-contract.sh:41-55` |
| V51 | `flux-resume.sh` gates: target, source artifact, Kustomization `lastAppliedRevision`, HelmRelease chart version, end state (`--after-revert`: harness gone; `--after-drill`: scale to 1). | `ailab:scripts/s2s/flux-resume.sh:14-29` |
| V52 | Lowest-traffic hours (gatekeeper `/auth` <= 5 per hour): 22:00-01:00Z and 03:00-07:00Z on 2026-10-06/07 (one day of data). | LIVE Prometheus `increase(gatekeeper_auth_requests_total[1h])` |
| **Item 4 edge** | | |
| V53 | Traefik already exposes Prometheus metrics on entrypoint `metrics` (:9100) and the chart already creates ServiceMonitor `platform-edge/traefik`, but without label `release: kube-prometheus-stack`, which the Prometheus `serviceMonitorSelector` requires. Result: zero Traefik series (`count(traefik_entrypoint_requests_total)` empty, no `up` target in `platform-edge`). | LIVE Traefik args, ServiceMonitor labels, Prometheus CR selectors, queries; `platform:deploy/components/traefik/helmrelease.yaml:54-75`; traefik chart 36.3.0 `values.yaml:427-441` (`serviceMonitor.additionalLabels`) |
| V54 | Access logs are JSON with every request header dropped (`--accesslog.fields.headers.defaultmode=drop`), shipped by the Alloy DaemonSet (`monitoring/alloy`, all pods) to single-binary Loki, retention 168 h. | LIVE args; `ailab:kubernetes/apps/infrastructure/monitoring/alloy.yaml`, `loki.yaml:39` |
| V55 | **The "partially in Loki" premise did not reproduce.** For each of the last 30 hours, Traefik access lines in Loki >= gatekeeper `/auth` answers (ratio 1.1 to 3.9 in hours with traffic); the 2026-10-06 18:06-18:10Z 5xx lines (sandbox and web 503, airlock 500/502, integration and notification 500 with `OriginStatus` 0) are present. No strive pod was created between 17:55 and 18:15Z (only Jobs), so those 5xx were not a rollout. A 7-day `RequestCount` continuity check is recorded in E4. | LIVE Loki `count_over_time`, Prometheus `kube_pod_created` |
| V56 | Gatekeeper exports OTLP RED metrics with `http_route` and `http_status_code`: 7-day `/auth` 5xx = 0; `/auth/token` 503 = 107 (drill 4). | LIVE Prometheus |
| V57 | No strive PrometheusRule exists. ailab owns monitoring: rules with promtool fixtures (`.gitea/workflows/rules-lint.yaml`), Alertmanager to ntfy, the `strive-red` dashboard, and Gatus (generic `GatusEndpointDown`), which does not probe `strive.place`. | LIVE `get prometheusrule -A`; `ailab:kubernetes/apps/infrastructure/monitoring/kustomization.yaml:37`; `ailab:kubernetes/apps/apps/gatus/prometheusrule.yaml:23-45` |

## Owner decisions (required before execution)

Each decision lists the options, the recommendation and its cost. Steps that depend on a decision name it.

- **D1 (F-01) Valkey hardening depth.** (a) NetworkPolicy only. (b) NetworkPolicy plus AUTH. (c) b plus TLS now.
  **Recommend b**; TLS follows the F-16 transport decision. Cost: one owner-merged helper change, a URL-safe
  password rotation, one Valkey restart in an announced window (about 1 minute of degraded sessions, data kept by AOF),
  and an ordering constraint: AUTH only after the log redaction in release AG1.
- **D2 (F-02) API keys.** (a) Retire on ailab: a flag turns off the `X-API-Key` track and the management API, existing
  records are purged. (b) Harden: integrity-protected records, management API that derives tenant and owner from a
  verified bearer and requires an admin role, slug-to-UUID mapping (F-17). **Recommend a** (zero use in 30 days, keys
  cannot work against weld backends today, `platform__app_import` is unreachable on ailab). Cost: one protected
  `config.py` field (owner merge); re-enabling later needs plan b.
- **D3 (F-04) Test bypass.** (a) Keep it, but refuse it for any request that came through Cloudflare (Sentinel already
  runs in-cluster, V21), rotate the token, alert on edge attempts. (b) Disable permanently (Sentinel "Validate" stops
  working on ailab). (c) Toggle per test window (an owner-ack PR each time). **Recommend a**; no replacement mechanism
  for Sentinel is needed now (revisit with ADR-013). Cost: an unprotected code change and one token rotation (a few
  minutes where Sentinel validations fail).
- **D4 (F-03) Signing-key rotation model.** (a) Keep the keygen CronJob off on ailab, rotate by owner SOPS commits in
  three phases, roll by `kubectl rollout restart`, cadence every 180 days and on suspicion. (b) Build the keygen image
  (incompatible with a Flux-managed Secret, V19). (c) Like a, but roll through a new chart checksum value (protected chart
  change; two-controller race between the Secret and the roll). **Recommend a.** Cost: about 1 hour of owner time per rotation.
- **D5 (F-05) Preshared S2S secrets.** (a) Written procedure with a measured mint-failure window now, migration to `k8s`
  identities as a separate plan. (b) Dual-hash overlap in the registry (protected files). **Recommend a.** Cost: a
  window of failed mints (expected under 2 minutes) per rotated client.
- **D6 (F-05) Session and delegation Fernet keys.** (a) Announced-logout procedure only. (b) `MultiFernet` overlap
  (protected `lifecycle.py` and `config.py`). **Recommend a**: after F-01 the key is useful only together with Valkey
  write access. Cost: a rotation logs every user out and voids long-running delegation grants.
- **D7 (F-05) Kubernetes ServiceAccount signing key.** (a) Research and rehearse on a disposable Talos 1.11 cluster,
  then write the procedure. (b) Record "no procedure" as an accepted residual. **Recommend a.** Cost: a disposable
  cluster (docker or QEMU provider on a dev worker) and about a day of work.
- **D8 (F-05) age key.** (a) Rehearse by a real dual-recipient rotation (reversible at every step). (b) Procedure plus a
  dry run on a scratch repository. **Recommend a.** Cost: two re-encryption PRs per repository (one owner-merged on
  platform) and a hand update of `flux-system/sops-age`. Neither option revokes what the old key can decrypt from git
  history: after a suspected compromise every value must be rotated too.
- **D9 (F-06) PKCE and nonce rollout.** (a) Two gatekeeper releases (expand, then contract) and then PKCE enforcement
  on the Keycloak client. (b) One release, accepting that a login started on a new pod and finished on an old pod during
  the roll fails once. **Recommend a.** Cost: one extra pin bump; after enforcement, any gatekeeper rollback to a
  pre-contract image breaks logins until the Keycloak attribute is reverted first.
- **D10 (item 2) Routine identity.** (a) New non-admin user `workstation-bot` in team `automation`. (b) Reuse
  `dev-worker-bot`. **Recommend a** (separate revocation and attribution). Cost: one user, one token, one merge-author entry.
- **D11 (item 2) Owner actions.** (a) Web UI by default; for scripted owner operations an ephemeral token held
  DPAPI-encrypted for at most 2 hours, deleted by the owner in the UI, with an alert after 4 hours. (b) a plus a CronJob
  that deletes expired ephemeral tokens through the database. **Recommend a.** Cost: the owner performs merges and
  approvals in the browser.
- **D12 (item 2) Cleanup scope.** Include `chifor`'s GCM OAuth grant and the 39 undocumented `gitea_admin` tokens, keep
  the two documented ones. **Recommend yes.** The workstation's kubeconfig, talosconfig and age key stay out of scope.
- **D13 (item 3) Drill 2 timing.** (a) Run it before release AG1, in a 4-hour window at 03:00-07:00Z, so the signed
  rollback (image plus config) is exercised as written; afterwards redefine rollback as config-only. (b) Skip drill 2 and
  redefine rollback as config-only now. **Recommend a.** Cost: the assistant runs on the legacy panel for the window,
  and every `strive-ailab` deploy is frozen while the release is suspended.
- **D14 (item 4) Edge telemetry detail.** Enable Traefik router labels (needed to separate edge-generated 5xx from
  origin 5xx), keep the `CF-Ray` header in access logs, Loki retention stays 168 h. **Recommend yes, yes, keep.** Cost:
  one Traefik restart (surge rollout, `maxUnavailable: 0`), about 20 extra router series per code. The real Loki disk
  usage is UNVERIFIED (NFS reports share-wide usage).
- **D15 (all) Protect the hardened files.** Add to platform `protected_file_patterns`: `infra/gatekeeper/src/app/gatekeeper/{routes,helpers,jwks,redis,apikeys,apikeys_api}.py`,
  `infra/gatekeeper/src/app/api/v1/api.py`, `deploy/components/valkey/**`, `deploy/components/traefik/**` (braces expanded,
  as in the runbook). **Recommend yes**, after the last code PR of this plan merges. Cost: 2 commits in the last 30
  days touched these paths, so about 2 extra owner merges a month.

## Approach

Step IDs: **A** = item 1 (A1 = F-01 ... A6 = F-06; AG = shared gatekeeper releases), **B** = item 2, **C** = item 3,
**E** = item 4. Commands are indicative; secret values are always piped, never echoed, and every artefact that could
hold one goes to the main checkout's gitignored `kubernetes/infra/_out/` (`C:\Users\chifo\work\home\ailab`).

### Shared: gatekeeper releases AG1 and AG2

- **AG1 (expand)**, one platform code PR plus a pin-bump PR. Contents: A1.0 (Redis URL redaction), A2.1 (API-key flag),
  A4.1 (edge refusal), A6.1 (OIDC expand). The code PR touches `config.py` (protected) and is owner-merged; the pin
  PR (`ailab.yaml`, owner `approve-pin`) is opened only after the image is built. Its rollback note: re-pin the previous
  digest; nothing in AG1 migrates data. **After A1.4 lands, never roll gatekeeper back past AG1** (the old image logs the
  Redis URL with the password); if forced, rotate the Valkey password afterwards.
- **AG2 (contract)**, after AG1 has run at least 24 hours: A6.3. Rollback note: re-pin AG1 (AG1 still completes both
  login shapes); after A6.4, revert the Keycloak attribute first.

### Item 1, F-01: Valkey authentication and ingress (D1)

- **A1.0 (in AG1).** `redis.py`: log `scheme://host:port/db` only, never userinfo (both lines in V7). Unit test: a URL with
  `default:secret@` logs no `secret`.
- **A1.1 Measure the real client set** (owner, read-only): `kubectl exec valkey-master-0 -- valkey-cli CLIENT LIST`,
  reduced to `addr` IPs mapped to pod names; record which of the 16 workloads in V5 connect. The NetworkPolicy still
  admits all 16 (a superset), so a missed lazy client cannot break.
- **A1.2 NetworkPolicy** (platform PR, `deploy/components/valkey/helmrelease.yaml`, unprotected): `networkPolicy.allowExternal: false`
  and one `extraIngress` rule on 6379 from `podSelector` `strive.io/service In [airlock, deepagent, digest, digest-worker, gatekeeper, integration, integration-worker, knowledge, mcp, mcp-worker, notification, profile, sentinel, tms, workflow, workflow-worker]`.
  Only the NetworkPolicy changes; the StatefulSet does not restart. Rollback: revert the PR.
- **A1.3 Prove the AUTH semantics** (dev worker or CI, docker, no cluster): against `bitnamilegacy/valkey:8.0.1-debian-12-r1`
  with an empty password, `AUTH default x` returns OK and `AUTH x` errors; redis-py `from_url("redis://default:x@host")`
  pings OK; then with `requirepass x` the same URL pings OK and a wrong password raises. Record the outputs. If any result
  differs, stop: A1.4 would then need clients that read a separate `REDIS_PASSWORD` (code in every consumer), re-planned.
- **A1.4 Password and clients** (after AG1 is live and A1.3 passed):
  1. Owner rotates `valkey-password` in `deploy/secrets/ailab/valkey-auth.enc.yaml` (protected) to 64 hex characters
     (URL-safe), so its current, unknown character set cannot break URL parsing. No consumer reads it yet.
  2. Platform PR (owner-merged, `_helpers.tpl` protected): when `global.valkey.auth.enabled` (default `false`, so other
     providers are untouched) the helper renders `REDIS_PASSWORD` from `secretKeyRef valkey-auth/valkey-password` and then
     `REDIS_URL=redis://default:$(REDIS_PASSWORD)@valkey-master.<ns>.svc.cluster.local:6379`. Kubernetes expands `$(VAR)`
     only for variables defined earlier in the same list, and the pod spec keeps the literal `$(REDIS_PASSWORD)`. The
     same PR: `ailab.yaml` sets the flag and rewrites airlock's `APP__AIRLOCK__RATE_LIMIT_REDIS_URL` the same way; the four
     worker manifests add both variables. Render tests (Helm 3 and Helm 4, V50): `REDIS_PASSWORD` precedes every URL that
     uses it, no literal password in any manifest, flag off renders today's output byte-identically.
  3. Rollout: every consumer rolls once. The server is still `nopass`, so the two-argument AUTH is accepted (A1.3).
  Verify before A1.5: every pod that connected in A1.1 shows `REDIS_PASSWORD` among its env names;
  `gatekeeper_redis_fallback_total` flat; Loki count of `NOAUTH|WRONGPASS|AuthenticationError` in `strive-ailab` = 0;
  Loki count of the substring `redis://default:` = 0 (count only, never the lines).
  Rollback: revert the PR (only while A1.5 is not live).
- **A1.5 Server AUTH** (platform PR, `deploy/components/valkey/helmrelease.yaml`: `auth: {enabled: true, existingSecret: valkey-auth, existingSecretPasswordKey: valkey-password}`
  and the comment fixed), merged at the start of an announced window in V52's quiet hours. One Valkey restart: during it
  gatekeeper falls back to memory (V8) and reconnects within its backoff, so users may see one silent re-login; session
  rows survive (AOF, V9). Rollback: revert (AUTH off); clients keep working because `nopass` accepts their AUTH.
- **Ordering inside F-01:** A1.2 any time after E1; A1.4 after AG1 and A1.3; A1.5 after A1.4 is verified. Rolling back
  runs the reverse order (A1.5 before A1.4).

### Item 1, F-02: API keys (D2 = a)

- **A2.1 (in AG1).** `config.py` field `api_keys_enabled: bool = True` (env `API_KEYS_ENABLED`; protected file). When
  false: `api/v1/api.py` does not mount the `/api-keys` router (404), and `/auth` ignores `X-API-Key`, counting
  `gatekeeper_auth_requests_total{method="api_key",status="disabled"}` and continuing with the remaining tracks.
  `ailab.yaml` gatekeeper `extraEnv` sets `API_KEYS_ENABLED=false` in the AG1 pin PR. Tests: flag off gives 404 on the
  management API, no API-key mint for a planted record, the session track still works with the header present; flag on
  is unchanged.
- **A2.2 Purge** (owner, after AG1): count `apikey:*` and `apikeys_by_tenant:*` keys with `SCAN` (count only), delete them
  with `UNLINK`, count again (expected 0 before and after; any non-zero count is recorded and reported).
- **A2.3 Docs.** Fix the `apikeys_api.py:1-9` docstring and the gatekeeper README; record in the spec that F-02 and F-17
  are closed on ailab by retirement.
- If D2 = b, this item becomes a separate plan (record format change, verified-bearer management API, admin role, slug
  mapping); nothing below depends on it.
- **Dependency on F-01:** none in code. F-02's severity is High only while F-01 is open; A1.2 already narrows Valkey writers.

### Item 1, F-03: signing-key rotation (D4 = a)

- **A3.1 Docs PR** (owner-merged; chart protected): new `platform:docs/runbooks/gatekeeper-key-rotation-ailab.md`
  (the procedure below); mark `gatekeeper-key-emergency-rotation.md` "not for ailab"; correct the hot-reload comments in
  `charts/gatekeeper/templates/keygen-cronjob.yaml:13-21`, `charts/gatekeeper/values.yaml:316-318`,
  `charts/gatekeeper/templates/deployment.yaml:185-190`, and the false self-generation claim at `ailab.yaml:444-448`.
- **A3.2 Rotation, done for real as the rehearsal.** Each phase is one owner SOPS commit to
  `deploy/secrets/ailab/gatekeeper-signing-keys.enc.yaml` (protected), then: wait until the live Secret's key NAMES match
  (`kubectl get secret gatekeeper-signing-keys -o go-template='{{range $k,$v := .data}}{{$k}} {{end}}'`), then
  `kubectl rollout restart deployment/gatekeeper` and `rollout status`. A rollout restart sets a pod-template annotation
  that the Helm chart does not manage, so later upgrades do not re-roll (with Helm 4 server-side apply this is UNVERIFIED;
  check that the next platform upgrade does not roll gatekeeper unexpectedly).
  - **P1 pending.** Generate a P-256 key into `_out/` (`openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256`), add
    it as `pending.pem`. Check: both pods log `FileKeyRing loaded 2 key(s) ... [('active', '8b77549a4272d160'), ('pending', '<NEW>')]`;
    `/auth/jwks` from each pod (exec, `127.0.0.1:5000`) lists both kids. **Wait at least 11 minutes** after the second pod
    is Ready (V18: 600 s cache lifespan plus margin) so every verifier has the new key before anything signs with it.
  - **P2 promote.** `active.pem` := NEW, `retiring.pem` := OLD, no `pending.pem`. During the roll one pod signs with OLD and
    the other with NEW; both publish and verify both (V17), so token exchange across replicas keeps working. Check: logs show
    `('active', NEW), ('retiring', OLD)`; `scripts/s2s/phase4-probes.sh --gatekeeper-only` PASS; a minted token's header `kid`
    is NEW; one `@api` journey passes.
  - **P3 retire.** **Wait at least 15 minutes** after P2's rollout completed (330 s token-life bound plus margin), then
    remove `retiring.pem`. Check: one kid (NEW) on both pods and on the public JWKS; probes and a journey pass.
  - Rollback at any phase: `git revert` the phase commit and restart; the previous phase's key set comes back.
- **Emergency variant** (documented, not rehearsed): skip P1 and put NEW as the only key at once. Every outstanding token
  then fails for at most 330 s; users get a fresh token on their next request (V17); harness verifications can be refused
  for up to 60 s by its cooldown (V18).
- Cadence (D4): every 180 days and on suspicion; the runbook records each rotation's date and kids.

### Item 1, F-04: test bypass (D3 = a)

- **A4.1 (in AG1).** In the bypass track (`routes.py:786-855`), before the token comparison: if the request carries
  `cf-ray` or `cf-connecting-ip`, record `status="edge_refused"` and answer the existing 401 body. A module constant in
  `routes.py` names the two headers (no `config.py` change). A client that adds these headers itself only gets refused.
  Tests: with either header, 401 and the new metric even with the right token; without them, today's behaviour.
- **A4.2 Live acceptance** (after AG1): `gatekeeper_auth_requests_total{method="test_bypass",status="success"}` keeps
  increasing with Sentinel's runs (or the owner triggers one Validate); the owner sends one internet request with the real
  token to an allowlisted path (token read from the Secret into a shell variable, never printed): 401, and `edge_refused`
  +1. This also proves V23 (the CF headers reach `/auth`).
- **A4.3 Rotate the token** (the old one was usable from the internet for months): one owner commit changing
  `test-bypass-token` in `gatekeeper-secrets.enc.yaml` and `sentinel-secrets.enc.yaml` and bumping
  `gatekeeper.serviceRegistry.checksum` (V27, rolls gatekeeper), then `kubectl rollout restart deployment/sentinel`.
  Sentinel validations fail between the two rolls (expected under 5 minutes). The procedure goes into the A5.1 platform runbook.
- **A4.4** Alert `StriveTestBypassFromEdge` (E3).
- Rollback: re-pin the pre-AG1 gatekeeper (re-opens F-04; see AG1's constraint).

### Item 1, F-05: roots-of-trust procedures

- **A5.1 Runbooks.** ailab `docs/runbooks/roots-of-trust-rotation.md` (age key, Kubernetes SA key, Keycloak realm keys)
  and platform `docs/runbooks/ailab-credential-rotation.md` (signing key = A3.2, test-bypass = A4.3, preshared, Fernet).
  Each procedure states holders, order, overlap, expected user impact, verification and rollback. The spec's rotation
  inventory and `platform:deploy/secrets/ailab/SECRETS.md` (protected) link them.
- **A5.2 age key (D8).** Order, each step reversible:
  1. Generate the new key into `_out/` (`age-keygen -o _out/age-<date>.agekey`).
  2. Add it as a second identity to `flux-system/sops-age` (`kubectl create secret generic sops-age --from-file=age.agekey=<old> --from-file=age-<date>.agekey=<new> --dry-run=client -o yaml | kubectl apply -f -`;
     kustomize-controller accepts several `.agekey` entries). Check: every Kustomization with SOPS decryption stays Ready.
  3. Add the new recipient next to the old one in every creation rule of `ailab:.sops.yaml` and the ailab rule of
     `platform:.sops.yaml`; run `sops updatekeys -y` on all 74 ailab files and the 23 platform files. Platform PR is owner-merged
     (protected) and must bump `serviceRegistry.checksum` (the ciphertext of `gatekeeper-secrets.enc.yaml` changes; a harmless
     gatekeeper roll). Check: each decrypts with the NEW key alone (`SOPS_AGE_KEY_FILE=<new> sops -d ... >/dev/null`, exit code only).
  4. Remove the old recipient (`.sops.yaml`, `sops updatekeys` again, same checks), then the old identity from `sops-age`,
     then the old key file and any backup. Check: all decrypting Kustomizations Ready after a forced reconcile.
  Residual stated in the runbook: git history still holds ciphertexts the old key opens.
- **A5.3 Keycloak realm keys** (owner, Admin console or `kcadm`, `master` admin password piped from the Secret):
  1. Add `rsa-generated` provider `rsa-<date>` with `active=false`, `enabled=true` (passive: published, not signing).
     Check the public certs list both RS256 kids (that passive keys are published is UNVERIFIED for 26.0.0; checked here).
     Wait at least 16 minutes (gatekeeper's 900 s JWKS cache, V26), so no session hits an unknown `kid` under the cooldown.
  2. Make it active with a higher priority than the old one. New tokens carry the new `kid`; the old key keeps verifying.
  3. For `hmac-generated` (refresh tokens) and `aes-generated`: add new providers with higher priority, keep the old
     ones enabled for at least 11 hours (SSO maximum 36000 s; gatekeeper requests no `offline_access`).
  4. Then set the old providers passive, then disabled, then delete them a day later. `rsa-enc-generated` the same way.
  Check at each step: `gatekeeper_auth_requests_total` refresh-failure and termination statuses at baseline, one login,
  one refresh past 300 s. Rollback: re-activate the old provider (still enabled until step 4).
- **A5.4 Preshared S2S secrets (D5 = a).** Per client: new 32-byte secret and its argon2id hash (method in
  `platform:deploy/secrets/ailab/SECRETS.md:198-213`); one owner commit changing `<svc>-secrets.enc.yaml`
  `gatekeeper-client-secret`, the client's hash in `gatekeeper-secrets.enc.yaml` and the checksum; when gatekeeper's roll
  completes, `kubectl rollout restart` the consumer Deployments (API and worker). Rehearse on the client with the fewest
  mints in 7 days (chosen at execution from gatekeeper's `service_token_minted` logs) and measure the window: from the first
  new gatekeeper pod Ready to the consumer Ready, count the client's 401s.
- **A5.5 Fernet keys (D6 = a).** Procedure only: owner commit changing `session-fernet-key` (or `delegation-grant-fernet-key`)
  plus the checksum, in V52's quiet hours, announced as a logout. Expected: every session ends (users re-login, silently
  if Keycloak's SSO cookie is alive), guest sessions end, long-running delegation grants fail (deepagent falls back to its
  300 s path, `ailab.yaml:373-376`).
- **A5.6 Kubernetes SA signing key (D7 = a).** Research then rehearse on a disposable Talos 1.11.2 cluster (never on
  `admin@ai`), answering: can `cluster.apiServer.extraArgs`/`extraVolumes` add a second `--service-account-key-file`
  (verification) while `cluster.serviceAccount.key` switches the signing key; how long to keep the old verification key
  (bound projected tokens refresh within about an hour; automount tokens are extended to a year by default, but the kubelet
  replaces them on refresh); which `kubernetes.io/service-account-token` Secrets exist on `admin@ai` (they break when the old
  key goes). Output: a procedure that rolls one CP at a time with `talosctl` 1.11.2 and etcd 3/3 between CPs, or a
  recorded "not feasible without downtime". Until then the row stays "no procedure".
- **A5.7** Update the spec's rotation inventory (and F-05's status) with links to the procedures and rehearsal dates.

### Item 1, F-06: PKCE, nonce, bound state, issuer (D9 = a)

Design (stateless, no Valkey write per anonymous hit):
- Login start (`/auth/login`, which the ForwardAuth redirect also goes through, see below) creates `state` (32 random
  bytes, URL-safe), `nonce` (32 bytes) and `code_verifier` (64 bytes, URL-safe), and sends
  `code_challenge=BASE64URL(SHA256(verifier))`, `code_challenge_method=S256`, `nonce` and `state`.
- It sets one cookie per login attempt, `gk_oidc_<first 16 hex of sha256(state)>`: value = Fernet-encrypted (session key,
  with a fixed type tag for domain separation) JSON `{v, state, nonce, code_verifier, return_path, iat}`; `HttpOnly; Secure;
  SameSite=Lax; Path=/callback; Max-Age=900`. Lax cookies travel on the top-level GET redirect back from Keycloak.
- The ForwardAuth login redirect (`routes.py:1611-1650`) points to `/auth/login?redirect_uri=<path>` on the same host, so
  only the directly routed `/auth/login` creates the cookie (no reliance on ForwardAuth relaying a new `Set-Cookie`).
  The spec's validation item 1 changes accordingly (302 to `/auth/login`, then to Keycloak).
- `/callback` looks up the cookie by the hashed `state`, decrypts it (Fernet TTL 900 s), compares `state` in constant time,
  exchanges the code with `code_verifier`, verifies the ID token (RS256 via the realm JWKS, `iss`, `aud=client_id`, `exp`,
  `nonce`), deletes the cookie, redirects to `return_path` (still passed through `validate_state`). On any mismatch or
  missing cookie it **never exchanges the code**: it redirects once to `/auth/login?redirect_uri=<validated return path or />`;
  a second failure within 60 s (marker cookie) returns a 400 page instead of looping.
- `verify_token` gains `issuer=` (the tenant config's public issuer).

Steps:
- **A6.1 (in AG1, expand).** `/callback` accepts both shapes: with a matching `gk_oidc_*` cookie it runs the full check above;
  without one it behaves as today (legacy). Login still sends the legacy request. The issuer check runs in report-only mode:
  `gatekeeper_oidc_issuer_mismatch_total{token}` counts mismatches, nothing is refused. Tests: every branch above, including
  "no exchange call on mismatch", cookie attributes, the ID-token checks, and legacy unchanged.
- **A6.2 Soak** at least 24 hours: issuer mismatches = 0; `/callback` 5xx = 0 (RED metrics).
- **A6.3 (AG2, contract).** `/auth/login` sends PKCE, nonce and the random `state` and sets the cookie; the ForwardAuth
  login redirect now points to `/auth/login` (design above); `/callback` refuses the legacy shape (restart, no exchange);
  the issuer check enforces. Because AG1 is already on both pods, a login started
  on an AG2 pod and finished on an AG1 pod succeeds; a legacy login finished on an AG2 pod restarts once (V33: under one
  login per roll). Live checks: the anonymous `GET https://strive.place/` chain ends at an authorize URL with
  `code_challenge_method=S256`, `nonce=` and a 43-character `state`; `GET /callback?code=x&state=y` without a cookie answers
  302 to `/auth/login` and gatekeeper logs no token exchange; login journeys on both hosts pass.
- **A6.4 Enforce at Keycloak.** keycloak-sync reconciles `pkce.code.challenge.method=S256` on client `gatekeeper` (same
  pattern as `sync_client_redirect_uris`), and the realm seed (`deploy/components/keycloak-realm-seed/realm-configmap.yaml`)
  gets the attribute for fresh imports; keycloak-sync pin bump. Check: an authorize request without `code_challenge` is
  refused by Keycloak; logins pass. Rollback: remove the attribute first, then any image rollback.

### Item 2: owner workstation credentials (D10, D11, D12)

Target state: the workstation holds one non-admin routine credential (`workstation-bot`); no `cchifor` token exists; no
standing `chifor` token or OAuth grant exists; owner actions happen in the web UI, or through an ephemeral token that
lives at most 2 hours; any re-accumulation alerts.

- **B1 Identity.** Owner, in the Gitea pod: `gitea admin user create --username workstation-bot ...` (non-admin, random
  password never used), add it to org team `automation` (id 37, write, never admin). Mint `ws-<yyyymmdd>` with
  `write:repository,write:issue,read:organization,read:user` via `gitea admin user generate-access-token ... --raw`, piped
  straight into `git credential approve` (`protocol=https`, `host=git.chifor.me`, `username=workstation-bot`) on the
  workstation, never echoed.
- **B2 Merge eligibility** (ailab PR): add `workstation-bot` to `pr_reviewer_merge_authors` (V42); converge the reviewers
  as the D1 record did (reviewers first). Lands before B3 (reviewbot rule).
- **B3 Switch the workstation.**
  - `git config --global credential.https://git.chifor.me.username workstation-bot`; replace the WCM default
    `git:https://git.chifor.me` (`cchifor`) by the B1 token (`git credential reject` then `approve`).
  - New `ailab:scripts/gitea-api.sh METHOD PATH [BODY_FILE]`: reads the routine credential with `git credential fill`
    inside the process, passes the header to `curl -H @-` on stdin, prints only status and body, refuses to run if
    `/user` is not `workstation-bot` with `is_admin=false`.
  - Remove the `credential.https://chifor@git.chifor.me.helper` entries and erase the `chifor` line from `~/.git-credentials`
    (`git credential-store erase`); delete WCM targets `git:https://cchifor@git.chifor.me`, `git:https://chifor@git.chifor.me`,
    `git:https://refresh_token.git.chifor.me`, `git:https://oauth2@git.chifor.me` (`cmdkey /delete:`). Keep `~/.gitea_tok` and
    `~/.gitea_cred_tmp` only until B6's 401 check, then delete them.
- **B4 Verify.** `scripts/gitea-api.sh GET /user` prints `workstation-bot is_admin=False`; a test push and a test PR from a
  Claude session; that PR is reviewed and automerged by the reviewbot.
- **B5 Inventory metrics and alerts (prevents re-accumulation).** ailab: a custom-queries ConfigMap for infra-pg
  (`target_databases: [gitea]`) added to `monitoring.customQueriesConfigMap` next to the default one (V43), exporting per
  owner (`cchifor`, `chifor`, `gitea_admin`, and any `is_admin` user) the token count, the age of the oldest token, and the
  count of names outside an allowlist (`gitea_admin`: `af-ci-scaler-2941`, `flux-ailab-read`). Rules
  `kubernetes/apps/infrastructure/monitoring/gitea-credential-rules.yaml` with promtool fixtures:
  `GiteaOrgAccountHasTokens` (`cchifor` > 0; fires until B6, which validates it), `GiteaOwnerTokenStanding` (any `chifor`
  token older than 4 h), `GiteaAdminTokenNotAllowlisted`, `GiteaAdminUserCountChanged` (site admins != 2). Whether the CNPG
  exporter's connection may read the gitea tables is UNVERIFIED; fallback: a dedicated read-only role (`SELECT` on
  `access_token`, `"user"`, `oauth2_grant`) created like `gitea-db-bootstrap.yaml`.
- **B6 Observe, then revoke.** For 7 days after B3, `ver040-1786337710` and `cc-admin-20260913` must not be used (their
  `updated_unix` frozen; checked daily, read-only). If either moves, find the consumer (Gitea access log, source IP at that
  time) before revoking. Then the owner: snapshots ids and names to `_out/`; deletes all 26 `cchifor` rows in one transaction;
  confirms with the local copies that the old default credential returns 401 (status only); deletes those copies; deletes
  `cc-admin-20260913` and revokes the GCM OAuth grant in the UI (Settings, Applications); per D12 deletes the 39 undocumented
  `gitea_admin` tokens. `GiteaOrgAccountHasTokens` resolves.
- **B7** Remove `cchifor` from `pr_reviewer_merge_authors` (a re-minted org token can no longer automerge); keep `chifor`.
- **B8 Docs.** New `ailab:docs/runbooks/owner-credentials.md` (identities, the two procedures below, alerts, rules);
  update `s2s-identity.md:342-354` (count 26, closed items) and the spec's F-30. The owner updates `CLAUDE.md`'s forge
  paragraph and the stale auto-memory note `trueswarm-admin-merge-identity.md` (a worker can no longer merge as `chifor`).
  The branch-protection re-apply block in `s2s-identity.md` (which reads `$OWNER_TOKEN`) is rewritten to call the helper
  below.

**Owner actions from a Claude session (D11 = a).**
1. Default: the owner merges (admin override), approves, posts `approve-pin`, or edits branch protection in the Gitea web
   UI (Authelia login). The session prepares the exact action (PR, head SHA, expected checks) and holds no owner credential.
2. Scripted owner operation (for example the runbook's branch-protection re-apply): the owner mints
   `owner-eph-<yyyymmddhhmm>-<purpose>` in the UI with the smallest scopes (usually `write:repository`), then runs
   `pwsh scripts/owner-token.ps1 begin` (prompt with `Read-Host -AsSecureString`; stores it DPAPI-encrypted for the user
   under `%LOCALAPPDATA%\ailab-owner-token\` with an expiry of now + 2 h). The session calls
   `pwsh scripts/owner-token.ps1 api METHOD PATH [-Body file]`, which refuses after expiry, decrypts in-process, sends the
   request and prints status plus non-secret fields only. At the end: `owner-token.ps1 end` deletes the file and the owner
   deletes the token in the UI (the token API needs basic auth, which an OAuth-only owner may not have: UNVERIFIED).
   `GiteaOwnerTokenStanding` fires if it survives 4 hours.
3. Never: a token in the chat, in a repository, in an env file, in `~/.git-credentials`, or on a dev worker.

### Item 3: rollback drill 2 (D13 = a)

Preconditions (all, or do not start): E1a live (edge metrics for the window); D13 decided and the window announced
("assistant on the legacy panel; deploys frozen"); no platform PR in flight that the freeze would strand; the rollback
digest still served by the registry and the pin check green; the owner present for the whole window.

- **C1 Legacy panel first** (platform PR, `ailab.yaml` web env `WEB_AGENT_PANEL=legacy`, owner `approve-pin`), landed while
  the harness is still up (V46). Check: the side panel and `/assistant` answer through the legacy agent. Abort if not.
- **C2 Baseline** (record): `scripts/s2s/phase4-probes.sh` PASS; per gatekeeper pod: image digest, `service_token_minted`
  count over 30 minutes, `gatekeeper_tokenreview_*`; edge 5xx ratio of the last hour; HelmChart `strive-ailab-strive`
  `.spec.valuesFiles`; gatekeeper `/auth` and `/auth/token` RED rates.
- **C3 Freeze** with the runbook's three commands (Kustomization, then HelmRelease, then scale), times recorded.
- **C4 Rollback PR** (platform, one PR, owner-merged in the UI because `helmrelease.yaml` is protected; owner `approve-pin`):
  - `ailab.yaml`: `harness.enabled: false`; gatekeeper `image.digest` back to `sha256:45abbd52fd3ea9985372056490078cec54b0a2964ee88ba8718fe402086aee79`
    with a pin-bump block (migration: composite off; rollback: re-pin `3adaf0be...` and re-list the registry file).
  - `helmrelease.yaml`: drop the `ailab-s2s-registry.yaml` entry. The registry file stays in the tree.
  - **Helm-3 values caveat:** no value is set to `null` anywhere. CI renders with Helm 3 and helm-controller applies
    with Helm 4 (V50), which treat a parent `null` over a subchart default differently, so a null-based switch could pass CI
    and behave otherwise in the cluster. Every switch is a positive value or a dropped `valuesFiles` entry.
  - Before merge, render offline with Helm 3 and Helm 4 using the two remaining files: gatekeeper env
    `SVC_AUTH_BACKEND=preshared`, no `SERVICE_REGISTRY_EXTRAS_PATH`, no `gatekeeper-registry-extras` ConfigMap, no
    TokenReview ClusterRole or binding, no harness object; the apiserver CNP is still present (V47, expected).
  - Required checks: CI, E2E, contract, S2S Authority Guard ((b1) skipped, V48), `ailab-pins`, `owner-ack`.
- **C5 Resume** only after the merge: `scripts/s2s/flux-resume.sh --after-revert --sha <merge sha>`; record each gate's time.
  Then confirm what the script does not: HelmChart `.spec.valuesFiles` has two entries, and each gatekeeper pod runs
  digest `45abbd52...` with `SVC_AUTH_BACKEND=preshared` and no extras volume.
- **C6 Verify** (runbook block): `--expect-refused` PASS; ClusterRole and binding NotFound; `service_token_minted` > 0
  on each pod (base preshared mints); `strive-pg-harness-dsn` stays `SecretSynced`; the e2e lane passes; edge 5xx ratio
  and gatekeeper 5xx during C3-C6 within the C2 baseline. Hold at least 30 minutes and record.
- **Abort criteria and actions.**
  - Before the C4 merge (C2 probes fail, CI red, digest missing): `flux-resume.sh --after-drill`; nothing changed.
  - A `flux-resume.sh` gate STOPs: stay frozen, diagnose, never resume by hand.
  - New gatekeeper pods not Ready within 10 minutes or crash-looping on settings validation (the composite value reached
    the old image: the chart and spec race of the spec's Figure 17): the old pods keep serving (`maxUnavailable: 0`);
    let helm-controller's upgrade remediation act, record it, and go straight to C8.
  - Any base-client mint failure, gatekeeper 5xx, login failure, or edge 5xx ratio above twice the baseline for 10
    minutes: roll forward at once with C8.
- **C7** Record: freeze to merge, merge to source artifact, Kustomization applied, HelmRelease upgraded, gatekeeper roll
  duration, harness dark time, mints per pod, refusal results, edge 5xx during the window.
- **C8 Re-activation, part 1 (Phase 3 again):** platform PR restoring the gatekeeper digest (`3adaf0be...`, or the fleet
  pin current at that time) and the registry `valuesFiles` entry while keeping `harness.enabled: false` (owner-merged).
  Land it with a freeze (the scale line answers NotFound: the harness is absent) and
  `flux-resume.sh --after-revert --sha <sha>` (the harness is still absent, which is what that mode checks). Then the
  runbook's pre-flip acceptance: `extras_sha` equals the ConfigMap hash, `base_sha` agrees, preshared mints, e2e lane,
  `report-ailab-pin-drift` 0 torn.
- **C9 Re-activation, part 2 (the flip):** `harness.enabled: true`; then `scripts/s2s/phase4-probes.sh` PASS, #2092's checks,
  the `@api` journeys.
- **C10** Revert C1 (`WEB_AGENT_PANEL=harness`); check the panel.
- **C11** Record the drill in `s2s-identity.md` (drill 2 record), ADR-034 (dated note) and the spec's measured results; then
  rewrite the drill-2 text: once AG1 is pinned, rollback is **config-only** (drop the registry entry and disable the harness,
  keep the image), because older images lack AG1's fixes and, after A6.4, cannot log users in.

### Item 4: edge 5xx observability (D14)

Ownership: Traefik values live in platform `deploy/components/traefik/helmrelease.yaml` (Kustomization `platform-edge`,
unprotected); Prometheus, Loki, Alloy, rules, dashboards and Gatus live in ailab.

- **E1a Scrape Traefik** (platform PR): `metrics.prometheus.serviceMonitor.additionalLabels: {release: kube-prometheus-stack}`.
  No Traefik restart (only the ServiceMonitor changes). Check: `up{namespace="platform-edge"} == 1`;
  `traefik_entrypoint_requests_total` present. If the target does not appear (the ServiceMonitor targets the container port
  `metrics` behind a Service that does not expose it), add `metrics.prometheus.service.enabled: true`.
- **E1b Attribution detail** (platform PR, after E1a, in quiet hours): `metrics.prometheus.addRoutersLabels: true`;
  `logs.access.fields.headers.names: {Cf-Ray: keep}` (every other header stays dropped, so `Authorization`, cookies and
  `X-Test-Token` are never logged). One surge restart of Traefik.
- **E2 Gatus external probes** (ailab `kubernetes/apps/apps/gatus/configmap.yaml`): `https://strive.place/auth/jwks` (200),
  `https://auth.strive.place/realms/strive/.well-known/openid-configuration` (200), `https://apps.strive.place/favicon.ico` (200).
  They cross Cloudflare and the tunnel, so they catch failures Traefik never sees; the generic `GatusEndpointDown` covers them.
- **E3 Rules** (ailab `kubernetes/apps/infrastructure/monitoring/strive-edge-rules.yaml` plus `.test.yaml`, listed in the
  kustomization, linted by `rules-lint`). Initial thresholds, retuned after E4:
  - `TraefikScrapeMissing`: `absent(up{namespace="platform-edge"} == 1)` for 15m (guards E1a against a later revert).
  - `StriveEdge5xxRatioHigh`: entrypoint `web` 5xx rate / all requests > 5% for 10m, only while traffic > 0.1 req/s.
  - `StriveEdgeGenerated5xx` (D14 router labels): `sum by (service)` of router 5xx minus service 5xx over 10m > 3. These
    are answers Traefik produced without an origin response (ForwardAuth unreachable, middleware errors). The arithmetic
    relies on Traefik counting middleware-produced answers at router level only (UNVERIFIED for 3.4.3; E4 tests it).
  - `GatekeeperForwardAuth5xx`: `increase(http_server_request_duration_seconds_count{job="gatekeeper",http_route="/auth",http_status_code=~"5.."}[10m]) > 3`.
  - `GatekeeperHttp5xx`: the same for every other route except `/auth/token` 503; `GatekeeperTokenUnavailable` for
    `/auth/token` 503 > 20 in 10m.
  - `GatekeeperRedisFallback`: `increase(gatekeeper_redis_fallback_total[5m]) > 0` (F-20; also the A1.4 and A1.5 guard).
  - `StriveTestBypassFromEdge`: `increase(gatekeeper_auth_requests_total{method="test_bypass",status="edge_refused"}[15m]) > 0` (after AG1).
  Each rule gets fixtures that fire and fixtures that must not fire (no traffic, one isolated 5xx, the `/auth/token` 503 burst of a revocation drill).
- **E4 Baseline and tests** (7 days from E1a): daily p50, p95 and maximum of the edge 5xx ratio per Traefik service; the
  count of 5xx with `OriginStatus` 0 from access logs; Gatus success. One controlled test in a scratch namespace (a test
  IngressRoute whose ForwardAuth points to a closed port) to confirm the router-versus-service arithmetic; drop or rework
  `StriveEdgeGenerated5xx` if it does not hold. One `RequestCount` continuity check over 7 days of Traefik lines in Loki
  (gaps, Traefik restarts) to close the completeness question in V55. Then retune the thresholds in one ailab PR.
- **E5 Dashboard** (ailab `kubernetes/apps/infrastructure/monitoring/strive-edge-dashboard.yaml`, `grafana_dashboard: "1"`):
  requests and 5xx by entrypoint and service, edge-generated 5xx, gatekeeper `/auth` outcomes
  (`gatekeeper_auth_requests_total` by method and status), gatekeeper RED by route, Gatus results, and a Loki panel
  `{namespace="platform-edge",container="traefik"} | json | DownstreamStatus >= 500`.
- **Collector finding.** The collector is the Alloy DaemonSet (`monitoring/alloy`) writing to Loki; V55 found no partial
  ingestion. The real gaps were the missing scrape label (E1a), no attribution between edge and origin (E1b, E3), no view
  of failures before Traefik (E2), and 7-day retention against investigations started late (accepted under D14).

## Sequencing and dependencies

1. **E1a, E2, E3 (without the AG1-dependent rule)**: first, about one day; the E4 baseline starts with E1a.
2. **B1 to B5**: in parallel with step 1. B5's alert proves itself by firing on `cchifor`.
3. **A1.2** (Valkey NetworkPolicy) and **A1.3** (AUTH proof): in parallel, after E1a.
4. **C1 to C11** (drill 2): after E1a and before AG1 (D13); one window, then C8 to C10 the same day or the next.
5. **A3.1** docs and **A5.1** runbooks: any time; **A3.2** (real signing-key rotation): after E1a, not on the same day as
   another gatekeeper roll.
6. **AG1** (A1.0, A2.1, A4.1, A6.1): after C11. Then **A2.2**, **A4.2**, **A4.3**, **A4.4**, and the A6.2 soak.
7. **A1.4**, then **A1.5** in a window: after AG1.
8. **AG2** (A6.3) after the soak, then **A6.4**.
9. **B6, B7, B8**: 7 days after B3.
10. **A5.2 to A5.6**: after AG1, one at a time, each in its own window; A5.6 is research first.
11. **E4** retune after 7 days; **D15** protection after the last code PR (AG2 and A6.4).

Parallel tracks: B (credentials) and E (observability) are independent of A and C. A1.2 and A1.3 do not touch gatekeeper.
Not in parallel: any two steps that roll gatekeeper (C4, C8, AG1, AG2, A1.4, A3.2, A4.3, A5.x), so that each roll's effect is
attributable.

## Critical files

**platform** (`cchifor/platform`):

| Path | Role | Protected |
| --- | --- | --- |
| `deploy/components/valkey/helmrelease.yaml` | Valkey NetworkPolicy (A1.2) and AUTH (A1.5) | no (yes after D15) |
| `deploy/helm/templates/_helpers.tpl` | `REDIS_PASSWORD` and `REDIS_URL` (A1.4) | yes |
| `deploy/components/workers/{digest,integration,mcp,workflow}-worker.yaml` | worker Redis env (A1.4) | no (owner-ack) |
| `deploy/helm/values/providers/ailab.yaml` | flags, pins, airlock URL, web panel, harness (A1.4, AG1/AG2, C1, C4, C8-C10) | no (owner-ack) |
| `deploy/secrets/ailab/{valkey-auth,gatekeeper-signing-keys,gatekeeper-secrets,sentinel-secrets,<svc>-secrets}.enc.yaml` | rotations (A1.4, A3.2, A4.3, A5.4, A5.5) | yes |
| `deploy/gitops/flux/clusters/ailab/app/helmrelease.yaml` | `valuesFiles` (C4, C8) | yes |
| `infra/gatekeeper/src/app/gatekeeper/redis.py` | URL redaction (A1.0) | no |
| `infra/gatekeeper/src/app/gatekeeper/config.py` | `api_keys_enabled` (A2.1) | yes |
| `infra/gatekeeper/src/app/api/v1/api.py`, `gatekeeper/routes.py`, `gatekeeper/apikeys_api.py` | API-key flag (A2.1), edge refusal (A4.1), login redirect and callback (A6) | no |
| `infra/gatekeeper/src/app/gatekeeper/helpers.py`, `gatekeeper/jwks.py`, `gatekeeper/metrics.py` | PKCE, nonce, cookie (A6), issuer (A6), new metric labels | no |
| `infra/keycloak-sync/src/keycloak_realm_sync/main.py`, `deploy/components/keycloak-realm-seed/realm-configmap.yaml` | PKCE enforcement (A6.4) | no |
| `deploy/helm/charts/gatekeeper/{values.yaml,templates/keygen-cronjob.yaml,templates/deployment.yaml}` | comment fixes (A3.1) | yes |
| `docs/runbooks/gatekeeper-key-rotation-ailab.md`, `docs/runbooks/ailab-credential-rotation.md`, `docs/runbooks/gatekeeper-key-emergency-rotation.md` | procedures (A3.1, A5.1) | no |
| `deploy/components/traefik/helmrelease.yaml` | ServiceMonitor label, router labels, `Cf-Ray` (E1a, E1b) | no (yes after D15) |

**ailab** (`cchifor/ailab`):

| Path | Role |
| --- | --- |
| `ansible/roles/pr_reviewer/defaults/main.yml` | merge authors (B2, B7) |
| `scripts/gitea-api.sh`, `scripts/owner-token.ps1` | routine and ephemeral owner credential helpers (B3, item 2 procedure) |
| `kubernetes/apps/databases/infra-pg.yaml` plus a new custom-queries ConfigMap | token inventory metrics (B5) |
| `kubernetes/apps/infrastructure/monitoring/{gitea-credential-rules,strive-edge-rules}.yaml` and `.test.yaml`, `strive-edge-dashboard.yaml`, `kustomization.yaml` | alerts and dashboard (B5, E3, E5) |
| `kubernetes/apps/apps/gatus/configmap.yaml` | external probes (E2) |
| `docs/runbooks/{owner-credentials,roots-of-trust-rotation}.md`, `docs/runbooks/s2s-identity.md` | procedures and records (A5.1, B8, C11) |
| `scripts/s2s/{flux-resume.sh,phase4-probes.sh}` | used unchanged by C |

## Verification

**F-01.** A1.2: an unlabelled throwaway pod in `strive-ailab` cannot open 6379 (timeout), a labelled consumer can;
`gatekeeper_redis_fallback_total` flat. A1.4: as listed in the step. A1.5: from an admitted pod an unauthenticated `PING` gets
`NOAUTH` and an authenticated one `PONG`; a test session created before the window is still valid after it;
`gatekeeper_redis_reconnections_total` +1 per replica, then fallback flat; edge 5xx within the E4 baseline after reconnect.
Resolved per the spec's own criterion: the NetworkPolicy names its sources and Valkey refuses unauthenticated commands.

**F-02.** `POST /api/v1/api-keys` from an admitted pod: 404. `/auth` with `X-API-Key` and a planted test record: no API-key
mint (`status="disabled"` counted). `SCAN` counts 0. Spec F-02 and F-17 marked resolved by retirement.

**F-03.** The JWKS sequence `{OLD}`, `{OLD,NEW}`, `{NEW,OLD}`, `{NEW}` observed on both pods with the timestamps; probes and
a journey pass after each phase; 0 gatekeeper 5xx and no `invalid_token` spike on backends during the rotation. Resolved when
`/auth/jwks` has shown a key change with an overlap and the procedure is recorded.

**F-04.** A4.2's internet request gets 401 and `edge_refused` +1; Sentinel successes continue; after A4.3 the old token is
refused in-cluster (401 `invalid_key`) and the new one accepted. Resolved per the spec: an `X-Test-Token` request from the
internet is no longer accepted.

**F-05.** Each procedure exists and states order, overlap, impact and rollback. Rehearsal records: A3.2, A5.2 (all
decrypting Kustomizations Ready at each step, old key gone from `sops-age` and the workstation), A5.3 (both kids published
during the overlap, no session terminations above baseline), A5.4 (measured window and 401 count), A5.6 (research note and
disposable-cluster outcome). The spec's validation item "every row marked No procedure has an owner-decided follow-up" holds.

**F-06.** Unit tests per A6.1; live checks per A6.3 and A6.4; login journeys on `strive.place` and `apps.strive.place` pass;
`/callback` 4xx/5xx and `/auth/login` rates within baseline the day after AG2; issuer mismatches 0.

**Item 2.** `workstation-bot is_admin=False` is the only Gitea identity the workstation resolves; `cmdkey /list` shows no
`chifor`, `cchifor` or OAuth Gitea targets; the files are gone; DB: `cchifor` 0 tokens, `chifor` 0 standing tokens and no
OAuth grant, `gitea_admin` only the allowlisted two; the four B5 alerts are loaded and their fixtures pass; one rehearsal of
the ephemeral owner procedure (mint, scripted call, delete, alert silent) is recorded.

**Item 3.** Measurements in C7, results of C6, C8 and C9, and the C11 records.

**Item 4.** Traefik target up; E3 fixtures pass in `rules-lint`; the E4 controlled test outcome; the 7-day baseline table
recorded in the dashboard description and in the spec's F-31; Gatus probes green.

## Risks and residuals

- **Silent Valkey degradation.** A missed consumer or a wrong password makes gatekeeper fall back to per-pod memory without
  an error (V8). Mitigation: A1.3 proof, A1.4 checks before A1.5, `GatekeeperRedisFallback`. Residual: F-20's silent fallback
  itself stays (fail-closed is a separate decision).
- **Password exposure in logs.** Any library outside gatekeeper that logs its connection URL would leak the Valkey password
  into Loki (unauthenticated on the LAN). Only gatekeeper was found doing so; A1.4's Loki substring count is the check. A
  gatekeeper rollback below AG1 after A1.4 reintroduces the leak (AG1's note).
- **Label-based NetworkPolicy.** Any pod in `strive-ailab` that carries an admitted `strive.io/service` label reaches Valkey;
  creating such pods needs namespace write (Flux cluster-admin path, accepted D2 of the S2S plan). AUTH is the second layer.
- **Cloudflare-header test (F-04).** The refusal assumes every internet path transits Cloudflare (V22). A future
  LoadBalancer or a second ingress without Cloudflare would bypass it; D15 protects the Traefik component, and the in-cluster
  bypass still requires the token.
- **Helm 3 versus Helm 4.** CI renders with Helm 3, the cluster applies with Helm 4 (V50); null semantics and server-side
  apply differ. Mitigation: no null-based switches, dual renders in C4 and A1.4. Whether a `kubectl rollout restart`
  annotation survives a Helm 4 server-side upgrade without a re-roll is UNVERIFIED (A3.2 checks it).
- **Chart and spec race during C5 and C8.** Covered by the freeze and `flux-resume.sh`; the remaining failure mode (the old
  image receiving `composite`) is non-disruptive because old pods keep serving, and has an explicit abort path.
- **PKCE enforcement lock-in.** After A6.4, an image rollback below AG2 breaks every login until the Keycloak attribute is
  removed; AG2's rollback note says so.
- **Login restart loops.** Browsers that refuse the `gk_oidc_*` cookie get one restart and then a 400 page instead of a loop;
  such users cannot log in (already true of the `session_id` cookie).
- **Owner-equivalent credentials outside Gitea.** The workstation keeps cluster-admin (`admin@ai`), talosconfig and the age
  key; with them anyone can exec into Gitea and mint tokens. Item 2 removes standing Gitea owner credentials only.
- **`updated_unix` as "last used".** B6's observation depends on Gitea updating it on every token use (V38). If it does not,
  a quiet consumer could break at revocation; the 401 checks and Gitea's access log are the fallback.
- **API-key retirement** removes a documented (but unreachable on ailab) path for `platform__app_import`.
- **Talos SA key.** May prove infeasible without downtime; then it stays an accepted residual with a recorded reason.
- **Unprotected security code until D15.** Between AG1 and D15, a bot-approvable PR could revert A1.0, A2.1 (except
  `config.py`), A4.1 or A6; the reviewers and the E3 alerts are the only guard in that period.
- **Not addressed here:** Valkey TLS and transport encryption (F-16); F-07 back-channel logout; F-09 credential concentration;
  F-10 tenant-scoped client credentials; F-12 tunnel drift; F-13 isolated app origin; the Keycloak client secret, Flux deploy
  keys, tunnel credentials and service database passwords (the remaining "no procedure" rows of F-05 beyond the five named).

<!-- codex-review-status: pending -->
