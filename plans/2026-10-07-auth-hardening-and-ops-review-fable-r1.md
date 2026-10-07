# Fable review — round 1 — auth-hardening-and-ops

Plan: `C:/Users/chifo/work/ailab-wt-plan/plans/2026-10-07-auth-hardening-and-ops-plan.md` (branch `plan/auth-hardening-and-ops`, f3f782eb).
Refs checked: platform `5b453c03f` (the plan's pin; `gitea/main` is now `f922cd6f0`, 4 ADR-033 CI commits
later, all on top of the pin — citations still hold, and the live release is already
`0.2.0+f922cd6f0fc3`); ailab `origin/main` `c5a397a1`; live `admin@ai` read-only; traefik chart 36.3.0 and
bitnami valkey 1.0.3 pulled and rendered locally. No Secret value read or printed.

## Verdict

**SIGN WITH CHANGES.** The plan is well-evidenced (I could confirm 50 of its 57 facts from code, charts or the
live cluster, and found no fact that is wrong in a way that changes a step), the fixes do close F-01, F-02, F-04
and F-06 as the spec defines "resolved", and F-03/F-05 get the rehearsed procedures the spec asks for. The
drill and rotation steps have real abort paths and the freeze/resume ordering is right for the Figure-17 race.
What stops me signing as-is are five concrete gaps, none architectural: (1) E1a is written as "ServiceMonitor
label, and *if* no target appears add the metrics Service" — the live Service has no `metrics` port, so the
Service is required, not contingent, and E1a gates everything else; (2) A1.2/A1.4 verify only gatekeeper's
fallback metric, while a NetworkPolicy block or a bad password is a *timeout or warning* in airlock, mcp and the
workers, and A1.4 is a platform-wide roll with no window; (3) the F-06 cookie design accumulates one
`Path=/callback` cookie per login start with nothing that clears stale ones, and subresource 302s can pile them
up until `/callback` fails on header size; (4) A6.4's rollback ("remove the attribute first") goes through
keycloak-sync, which re-applies a hand-removed attribute within 10–20 minutes, so the rollback note needs both
halves; (5) item 2 names no break-glass: `chifor` is an OAuth2-only login (Authelia), so an Authelia or Keycloak
outage leaves the owner with no Gitea web login, and the plan deletes 39 `gitea_admin` tokens without stating
that the `gitea_admin` local password (escrow) plus `kubectl exec` minting is the break-glass. I would sign once
findings 1–5 below are folded in (each is a few lines). I also recommend, but do not require, changing D13 to a
config-only drill 2 that no longer gates AG1 (finding 6), and splitting AG1 (finding 7).

## Findings

1. **[important] E1a must add the dedicated metrics Service; the "if" is a certainty** — Location: item 4, E1a.
   Problem: LIVE `svc/traefik` (platform-edge) exposes only `web, websecure` (plan V22; `ports.metrics.expose.default: false`
   at `platform:deploy/components/traefik/helmrelease.yaml:54-57 @5b453c03f`). The chart's ServiceMonitor uses
   `targetPort: metrics` (LIVE ServiceMonitor spec); with no Service port named `metrics` there is no Endpoints
   port, so adding `release: kube-prometheus-stack` alone yields zero targets. Rendering traefik 36.3.0 with the
   plan's values confirms the mechanism: with `metrics.prometheus.service.enabled: true` the chart renders Service
   `traefik-metrics` (port `metrics` 9100) and the ServiceMonitor selector gains
   `app.kubernetes.io/component: metrics`, selecting only that Service (local render in
   `scratchpad/plan-review/traefik-render.yaml`). Fix: E1a = `serviceMonitor.additionalLabels.release` **and**
   `metrics.prometheus.service.enabled: true` in one PR; acceptance `up{namespace="platform-edge",job="traefik-metrics"} == 1`.
   Also state that E1a does not restart Traefik (both are new objects only) — true, but say it since the drill's
   precondition depends on it.

2. **[important] A1.2/A1.4 verification covers gatekeeper only; the "superset" argument is overstated; A1.4 has no window** — Location: item 1, A1.1–A1.4.
   Problem: `gatekeeper_redis_fallback_total` and the `NOAUTH|WRONGPASS|AuthenticationError` Loki count are
   gatekeeper-shaped signals. A NetworkPolicy block is a connect *timeout* (redis-py `TimeoutError`/`Error 110`),
   and airlock (`AIRLOCK_ALLOW_MEMORY_RATE_LIMIT=1`, `ailab.yaml:1778-1781`) and mcp
   (`services/mcp/src/app/services/capability_events.py:70,157`: "Redis client unavailable" warning) degrade
   silently like gatekeeper. "The NetworkPolicy still admits all 16 (a superset), so a missed lazy client cannot
   break" is only true for pods whose `strive.io/service` label is in the 16-value list; CronJob pods carry other
   labels (`workflow-artifact-expiry`, `ci-objectstore-expiry`, `ci-objectstore-init` — LIVE), so the argument
   rests on V5's pod-env scan having seen them, not on the policy. Separately, A1.4 step 2 changes `_helpers.tpl`,
   so every chart that includes `strive.envFromGatekeeper` (24 includes) plus the four workers roll in one Helm
   upgrade — a platform-wide roll — and the plan only puts A1.5 in a window. Fix: (a) A1.1: sample `CLIENT LIST`
   at least across one 5-minute CronJob cycle (or grep each CronJob image's code for Redis use) and record the
   label of every client pod; (b) A1.2 and A1.4 acceptance: Loki count over 24 h, namespace-wide, of
   `Error 110|Timeout connecting|ConnectionError|Redis.*unavailable|rate limit.*memory` = 0, plus
   `airlock` and `mcp` log checks by name; (c) A1.4 step 2 merges in V52 quiet hours with the same
   "not on the same day as another gatekeeper roll" rule; (d) A1.3: assert the failing class is a subclass of
   `redis.ConnectionError` (redis-py: `AuthenticationError(ConnectionError)`) — that is what makes V8's silent
   fallback apply (`redis.py:34-39,468`), and it is the property A1.4's checks rely on.

3. **[important] F-06 pre-login cookies accumulate and nothing clears them** — Location: item 1, F-06 design and A6.3.
   Problem: one `gk_oidc_<hash>` cookie per login start, `Path=/callback`, `Max-Age=900`. `/auth/login` never
   receives `/callback`-path cookies, so it cannot prune; `/callback` only deletes the one it matched. Today every
   non-API ForwardAuth miss is a 302 to login (`routes.py:1625-1645 @5b453c03f`), including page subresources
   (images, fonts, scripts not cached), so an expired session on a loaded page can start N logins and set N
   cookies; at ~400 B each, 40 of them exceed Cloudflare's per-header limit and `/callback` then fails for that
   browser until the cookies expire — and the "second failure within 60 s returns 400" rule turns that into a
   dead end. Fix: (a) `/callback` deletes every `gk_oidc_*` cookie it receives, not just the match; (b) in
   `_redirect_to_login`, only a navigation (`Sec-Fetch-Mode: navigate`, or `Accept` containing `text/html`) gets
   the 302 to `/auth/login`; everything else gets the API-style 401 — this also removes most of today's
   pointless Keycloak redirects for subresources; (c) a unit test that 5 concurrent starts then one callback leave
   zero `gk_oidc_*` cookies. Note for the design text: research-user-auth.md:93 and the live middleware's
   `authResponseHeaders` (which lists `Set-Cookie`) say Traefik relays a 302's `Set-Cookie`, so the extra hop
   through `/auth/login` is a belt-and-braces choice, not a necessity — fine to keep, but say so.

4. **[important] A6.4 rollback needs both the hand removal and the sync change** — Location: item 1, A6.4 and AG2 rollback note.
   Problem: the attribute is reconciled by keycloak-sync (`sync_client_redirect_uris` pattern, `main.py:626-700
   @5b453c03f`), whose Job `platform-identity` re-applies every 10–20 minutes (`docs/runbooks/ailab-pin-bump.md:17-19`).
   "Remove the attribute first" done in the admin console is undone on the next sync run; done through a
   keycloak-sync PR plus pin bump it takes an owner-ack cycle during which every login fails if the gatekeeper
   image was already rolled back. Fix: the rollback note reads "suspend Kustomization `platform-identity` (or the
   sync Job), remove `pkce.code.challenge.method` in the console, verify a login, then re-pin gatekeeper; land
   the keycloak-sync revert before resuming the sync". Add the same to the risks list under "PKCE enforcement
   lock-in".

5. **[important] Item 2 states no break-glass path** — Location: item 2, target state and D12.
   Problem: `chifor` is `login_type` 6 (OAuth2 via Authelia, V34). After B3/B6 the workstation holds no owner
   token and no OAuth grant; if Authelia, its Keycloak/LDAP backend, or the SSO chain is down, the owner cannot
   reach the Gitea UI at all, and the plan's "default mechanism is the web UI" has no fallback. D12 also deletes
   39 `gitea_admin` tokens. The real break-glass is (a) the `gitea_admin` local password (is it escrowed in
   OpenBao `af/estate/...`? the plan does not say) and (b) cluster-admin `kubectl exec` into
   `gitea/gitea-*` to mint a token — which the plan lists only as a residual risk. Fix: a "Break-glass" paragraph
   in B8's runbook: where the `gitea_admin` password lives, that `gitea_admin` keeps password login, that
   `GiteaAdminTokenNotAllowlisted` will fire on any break-glass token and that it is deleted afterwards; and B6
   verifies the `gitea_admin` local login works *before* revoking anything.

6. **[important] D13 as written gates High-severity fixes on an ops drill that C11 immediately retires** — Location: Sequencing 4 and 6; D13; C4; C11.
   Problem: AG1 (F-01 log redaction, F-02, F-04, F-06 expand) waits for C1–C11. The image+config rollback being
   rehearsed is declared obsolete in C11 the same day ("once AG1 is pinned, rollback is config-only"), so its
   only lasting value is exercising the freeze/resume tooling and the config-only deactivation — which a
   config-only drill exercises equally. The image downgrade is also the sole source of the disruptive abort path
   (old image receiving `composite`: `svc_auth_backend: Literal["preshared","k8s","mtls"]` at
   `19363455156f:config.py:341`, verified), and it costs two extra owner-acked pin PRs (C4 digest, C8 digest).
   Fix (recommendation, see D13 below): run drill 2 as *config-only* (drop the registry `valuesFiles` entry,
   `harness.enabled: false`, keep the image), which is exactly the procedure the runbook will carry forward;
   remove "after C11" from AG1's preconditions; keep C1–C3, C5–C7, C9–C11 unchanged. If the owner insists on
   the image variant, keep the plan but add to C4: "the CI harness contract renders the three files from a
   hardcoded list (`check-harness-chart-contract.sh:84`), so CI stays green with the entry dropped; the
   S2S guard's (b1) is skipped (V48) — both expected".

7. **[important] AG1 bundles the largest change (A6.1) with three one-liners** — Location: Shared AG1.
   Problem: A6.1 is new security-critical callback logic (cookie, PKCE exchange, ID-token verification, restart
   loop guard); A1.0/A2.1/A4.1 are small. One owner-merged PR means one review of mixed size and one rollback
   ("re-pin the previous digest") that would also re-open F-04 and F-02 to revert an OIDC bug, and after A1.4
   that rollback is forbidden outright. Fix: AG1a (A1.0, A2.1, A4.1; protected only via `config.py`) and AG1b
   (A6.1; unprotected files, bot-approvable) with their own pin bumps. D9 already budgets an extra pin bump;
   this is one more. A1.4's "never roll back past AG1" then binds to AG1a only.

8. **[important] A5.2 needs a holder inventory before the old recipient is removed** — Location: item 1, A5.2 steps 3–4.
   Problem: V24 names two holders (workstation file, `flux-system/sops-age`). `platform:.sops.yaml:16-22` describes a
   `SOPS_AGE_KEY` CI secret and an `external-secrets/age-key` Secret for the *test* recipient; V41 lists six
   token-like Gitea Actions secrets but no age-key secret. Nothing in the plan checks that no CI job or runner
   decrypts an ailab-rule file with the old key, or that no second copy exists (dev workers hold none, F9).
   Fix: step 0 of A5.2: enumerate holders (Gitea Actions secrets on ailab/platform/cloudlab by name, the
   `external-secrets` namespace, `~/.config/sops/age/` on the workstation, backups) and record them; step 4 only
   after each is either updated or confirmed to use another recipient.

9. **[important] D15 leaves the new OIDC authority surface bot-approvable** — Location: D15; A6.4.
   Problem: after A6.4, `infra/keycloak-sync/**` and `deploy/components/keycloak-realm-seed/**` decide whether
   PKCE is enforced and which redirect URIs the `gatekeeper` client accepts (`sync_client_redirect_uris`); a
   bot-approvable PR there can drop enforcement or add a redirect URI. Fix: add both paths to the D15 list, or
   record their omission as an explicit D2-style residual in the risks section.

10. **[nit] Pinned platform ref is four commits stale** — Location: Pinned references. `gitea/main` = `f922cd6f0`
    (ADR-033 pack-host CI only); the live HelmRelease already runs `0.2.0+f922cd6f0fc3`. Fix: re-pin or add
    "verified unchanged for every cited path at f922cd6f0" (true: `git log 5b453c03f..f922cd6f0` touches none).

11. **[nit] V1's chart comment contradicts V8 and the plan should say which is current** — Location: V1/V8, A1.5.
    `valkey/helmrelease.yaml:73-81` says gatekeeper "blocked at boot on an endless 'Authentication required'
    retry and never bound its port" with auth on. Today `ResilientRedis.connect()` catches `ConnectionError`
    (incl. `AuthenticationError`) and the readiness probe always returns 200 (`health.py:69-71 @5b453c03f`), so
    pods stay Ready through the A1.5 restart — the plan's impact statement is right, the chart comment is
    historical. Fix: A1.5's "comment fixed" should replace that paragraph, and A1.3 should include a
    gatekeeper-shaped check (settings with a wrong password → pod Ready, fallback metric +1).

12. **[nit] E3 rules need the kps selector label and routing labels** — Location: E3. The Prometheus CR selects
    rules by `release: kube-prometheus-stack` (LIVE `ruleSelector`; precedent `gatus/prometheusrule.yaml:17-18`)
    and Alertmanager routes on `severity`. Fix: state both in E3; add a fixture that asserts the PrometheusRule
    carries the label (rules-lint only lints the rule bodies).

13. **[nit] E2 feasibility is asserted, not checked** — Location: E2. `ailab.yaml:2066-2068` calls the public path
    from inside the cluster a "dead public hairpin" (why Sentinel maps `strive.place` to the Traefik Service).
    Gatus already probes `https://agentforge.chifor.me/healthz` and `registry.chifor.me` through Cloudflare
    (configmap.yaml:420,482), so it probably works for `strive.place` too, but the plan should say "verified by
    one manual `curl` from the Gatus pod" and note Cloudflare's UA ban (memory: python-urllib 1010) so the probe
    UA is Gatus's own.

14. **[nit] D11 helper is more machinery than the use case needs** — Location: item 2, "Owner actions" 2. The only
    scripted owner operation named is the branch-protection re-apply. Simpler: the owner runs that block in
    their own terminal with `Read-Host -AsSecureString` into a process variable and never persists it; the
    session prepares the exact commands. That removes the DPAPI file, the 2-hour expiry logic, `begin/end`, and
    the "deleted the file but not the token" state. Keep `GiteaOwnerTokenStanding` either way.

15. **[nit] B5's fallback role is probably unnecessary; say why** — Location: B5. CNPG's exporter runs inside the
    instance pod as the superuser over the local socket and `target_databases: [gitea]` is a supported
    custom-query key, so it can read `access_token`/`"user"`/`oauth2_grant` without a new role. Fix: state that,
    keep the fallback as a one-liner.

16. **[nit] B8/CLAUDE.md names a script that does not exist** — Location: B8. `CLAUDE.md` (origin/main) tells
    sessions to use `scripts/forge.sh`; it exists in neither `work/ailab` nor `work/home/ailab` nor origin/main.
    Fix: B8 replaces that reference with `scripts/gitea-api.sh`, and B3 inventories any other local tooling that
    reads `~/.gitea_tok` (none found in the repo; `tea` config and `.netrc` are absent on the workstation).

17. **[nit] A4.1: add `CDN-Loop` as a third marker and say why header presence is the only discriminator** — Location: A4.1.
    Cloudflare always sets `CDN-Loop: cloudflare` (RFC 8586) as well as `CF-Ray`/`CF-Connecting-IP`; a client
    cannot strip any of them. Since Traefik rewrites `X-Forwarded-For` to the cloudflared pod IP for both internet
    and Sentinel traffic, no peer-address rule can separate them — worth one sentence so a future reader does not
    "simplify" to a CIDR check. Also check A4.2 against every Sentinel validation kind (Validate on `strive.place`
    only; `hostAliases` maps only that host).

18. **[nit] C1's dependency on the web image is time-limited** — Location: C1/V46. `ailab.yaml:2177-2178`:
    "this image still honours `legacy` for one release". A fleet pin before the drill can remove it; C1's abort
    covers it, but the plan should either freeze the web pin until drill 2 or make C1 a pre-check of the pinned
    web image's `WEB_AGENT_PANEL` handling.

19. **[nit] A3.2 ordering detail** — Location: A3.2 P2. The "wait until the live Secret's key NAMES match" gate works
    for P1→P2 (`retiring` appears, `pending` vanishes) and P2→P3, but P2 also changes `active.pem`'s content;
    say that `platform-secrets`' reconcile applies both in one Secret write, so a name match implies the content
    is there (true: one Flux apply), and record the `platform-secrets` interval so the wait has a bound.

## Owner decisions

- **D1 (b)** — agree. NetworkPolicy alone leaves anything with namespace write a free Valkey client; AUTH is cheap once the helper carries it. TLS deferred to F-16 is right.
- **D2 (a)** — agree. Zero use in 30 days, slugs rejected by weld (F-17), `platform__app_import` unreachable; (b) is a real redesign.
- **D3 (a)** — agree, with finding 17 (`CDN-Loop`) and the rotation in A4.3. (b) would lose Sentinel; (c) is a standing owner-ack tax.
- **D4 (a)** — agree. Hot reload does not exist (`reload()` is called only from `__post_init__`, verified), so a Flux-managed Secret plus `rollout restart` is the honest model; (c) races two controllers for no gain.
- **D5 (a)** — agree; measure the window as planned.
- **D6 (a)** — agree. After F-01 the Fernet key is a read key only with Valkey access; an announced logout is acceptable at this scale.
- **D7 (a)** — agree, timebox it (one day) and accept "no procedure" if it needs downtime.
- **D8 (a)** — agree, with finding 8 (holder inventory first).
- **D9 (a)** — agree; the expand/contract keeps mixed-pod logins working and is the only way to make A6.4 safe.
- **D10 (a)** — agree (separate revocation and attribution).
- **D11 (a)** — agree in substance; recommend the simpler owner-terminal variant (finding 14) over the DPAPI helper.
- **D12 yes** — agree, with the break-glass statement (finding 5) and a check that `gitea_admin`'s local login works before the 39 deletions.
- **D13 (a)** — **disagree.** Recommend (c): a config-only drill 2 now (registry entry dropped, harness off, image kept), decoupled from AG1, in the same window with the same freeze/resume/abort structure. Rationale in finding 6: the image variant rehearses a procedure C11 retires the same day, adds the only disruptive abort path and two owner-acked pin PRs, and delays four High-severity fixes. If the owner wants the image variant for completeness, keep (a) but it should not gate AG1a (finding 7).
- **D14 yes/yes/keep** — agree; E1a needs the metrics Service (finding 1). Router-label cardinality (about 20 routers × codes × methods) is fine for this Prometheus.
- **D15 yes** — agree; add `infra/keycloak-sync/**` and `deploy/components/keycloak-realm-seed/**` or record their omission (finding 9).

## Claims you verified / could not verify

**Verified from code at platform `5b453c03f` / ailab `origin/main`:** V1 (valkey HelmRelease `auth.enabled: false`,
no `networkPolicy` values, the comment); V3 (chart 1.0.3 `allowExternal`, `valkey-client: "true"`, `extraIngress`
rendered after the built-in rules; chart pulled from OCI); V4 (`_helpers.tpl:293-294` literal `REDIS_URL`;
`deployment.yaml:97`; four worker manifests at :103/:98/:86/:171; `ailab.yaml:1780-1781`); V6 (TMS `redis_url`
default `redis://redis:6379`); V7 (`redis.py:467,664` log the URL); V8 (`_RECONNECT_ERRORS` and `_exec` fallback,
`redis.py:34-39,455-549`; readiness always 200); V11 (`apikeys_api.py:5-8,117-196`, `api.py:4,9`, `main.py:72`);
V13 (`routes.py:741-784`); V15 (`key_store.py:113-205`; `reload()` only from `__post_init__`); V16
(`deployment.yaml:19,184-216`); V17 (`internal_token_cache.py:70-103`, `service_token.py:558-564`); V18 (weld
600/1800 s, harness 600/1800/60 s); V19 (`ailab.yaml:444-450` vs `secrets/kustomization.yaml:33-37`); V20
(`ailab.yaml:427-443`, `routes.py:454-472,786-855`); V21 (`ailab.yaml:2069,2088`); V22 (Traefik `ClusterIP`,
chart values); V26 (`jwks.py:100-167`, `jwks_cache_ttl=900`, cooldown 60); V28 (`lifecycle.py:163,181,210`,
`config.py:328-333`); V29/V30/V31 (`helpers.py:119-165,409-422`; `routes.py:357-401,1611-1760`;
`jwks.py:180-225` no `issuer`); V32 (`bootstrap.py:119`, `sync_client_redirect_uris` at `main.py:626`); V42
(`pr_reviewer/defaults/main.yml:216`); V43 (`infra-pg.yaml:286-301`); V44 (`helmrelease.yaml:22-25`); V45
(`ailab.yaml:191-202`); V46 (`ailab.yaml:1444-1447,2172-2178`); V47 (`tokenreview-rbac.yaml:1`,
`ailab.yaml:220-231`); V48 (`check-s2s-authority.py:13-60,321-324`); V49 (old image Literal lacks `composite`);
V50 (`_helpers.tpl:349-352`, `check-harness-chart-contract.sh:54-56,454-493`); V51 (`flux-resume.sh:14-29`);
V53 (traefik chart keys `serviceMonitor.additionalLabels`, `addRoutersLabels`, `service.enabled`,
`logs.access.fields.headers.names`); V54 (Loki `retention_period: 168h`); V57 (`gatus/prometheusrule.yaml:23-45`,
`rules-lint.yaml` and `scripts/rules-lint.sh` exist); the 26 protected patterns and the runbook's drill-2 text
(`s2s-identity.md:137-164,598-646`); the prior plan's rollback definition ("not image-only").

**Verified live (read-only, 2026-10-07):** NetworkPolicy `valkey` ingress has no `from`, egress `{}`; ServiceMonitor
`platform-edge/traefik` lacks `release` and uses `targetPort: metrics`; `svc/traefik` 10.97.5.57 with only
`web,websecure`; Prometheus selectors `release: kube-prometheus-stack` for ServiceMonitor/PodMonitor/Rule, all
namespaces; HelmRelease `strive` and HelmChart list the three `valuesFiles`, Ready, `0.2.0+f922cd6f0fc3.2`;
helm-controller v1.5.5; gatekeeper env names include `REDIS_URL`, `SVC_AUTH_BACKEND`, `SERVICE_REGISTRY_EXTRAS_PATH`,
`TEST_BYPASS_*`, `KC_ADMIN_PASSWORD`; `gatekeeper-signing-keys` has only `active.pem`; `valkey-auth` exists with key
`valkey-password` (Flux-labelled `platform-secrets`); valkey StatefulSet 1 replica, PVC 8Gi `nfs-csi` (HelmRelease
says 4Gi — pre-existing drift, not this plan's); `sops-age` has one key `age.agekey`; `platform-secrets` and the
other SOPS Kustomizations Ready; pods in `strive-ailab` carry `strive.io/service` (incl. `harness`, CronJob pods
with other labels); Traefik args have no router labels and `headers.defaultmode=drop`; no strive PrometheusRule;
infra-pg uses only `cnpg-default-monitoring`; sentinel env `APP__SENTINEL__TEST_BYPASS_TOKEN`; two hand-run
`cloudflared` Deployments in `edge`; Gitea pod `gitea/gitea-*`; workstation has `~/.gitea_tok`,
`~/.gitea_cred_tmp`, `~/.git-credentials` (names only), no `tea` config, no `.netrc`.

**Could not verify (accepted as the plan marks them, or out of my reach):** V2's "no CiliumClusterwideNetworkPolicies"
(not re-queried); V5's per-pod env scan (I confirmed labels, not env contents); V9's AOF config (ConfigMap not
re-read); V10 AUTH semantics on valkey 8.0.1 (A1.3 proves it); V12/V33/V52/V55/V56's Prometheus and Loki
queries (not re-run); V14's public JWKS kid; V23 (Traefik forwards all request headers to `/auth` — consistent
with research-user-auth.md:788 and Traefik docs; A4.2 proves it); V25 (Talos docs); V27 (consumers not rolled by
a Secret change); V34–V41 (Gitea DB rows and `cmdkey` output — not re-read; the plan's drift note 21→26 is
plausible); whether Keycloak 26 publishes passive keys (A5.3 checks); whether `kubectl rollout restart`'s
annotation survives a Helm-4 server-side upgrade (A3.2 checks); whether `updated_unix` moves on every token use
(Gitea does update it on API/basic auth, but I did not read 1.26's code); the Gatus hairpin (finding 13); the
helm-controller "chart not ready" wait that makes the freeze/resume ordering race-free (consistent with the
2026-10-06 observation and the script's gates; the abort path covers the residual).
