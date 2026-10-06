# S2S identity alignment: round A1 answers

## Codex (gpt-6-astra via codex exec)

Q0: AGREE — **B with fixes**: it removes long-lived credential provisioning and rotation, while fitting the existing verifier interface. A-lite preserves current clients and local authentication during Kubernetes API outages. A-full additionally supports credential and grant changes without pod rolls through its reload machinery. I would switch to A-lite if measured API availability/latency misses the S2S SLO or near-term consumers cannot support projected tokens; A-full needs evidence that restart-free rotation justifies its additional complexity.

C1: AGREE — Separate reviewer authentication from the reviewed credential; rereading both rotating tokens prevents eventual authentication outages.

C2: AGREE — Completed authentication refusals are caller verdicts; API credential, RBAC, throttling and transport failures are operational failures, including malformed/incomplete responses.

C3: AMEND: Protect both files; reject extras elsewhere and backend/registry-path overrides through ailab inputs, including `env`/`extraEnv`, while permitting fixed chart defaults — extras-only checking misses environment overrides (`platform/deploy/helm/charts/gatekeeper/templates/deployment.yaml:93`).

C4: AGREE — Test every enabled merge route and actual automation credentials; documented owner/worker account sharing makes identity separation essential (`platform/docs/runbooks/owner-ack.md:102`).

C5: AMEND: Expect 401 for preshared `svc-deepagent` and for a second k8s entry rejected by F-c’s subject precheck; test completed-review subject mismatch separately with a mocked response; assert `sub`/`azp` — F-c/F-d make the proposed live subject-mismatch 403 inconsistent (`plans/2026-10-06-s2s-identity-alignment.md:89`).

C6: AGREE — Templating the existing entry avoids duplicate environment variables and preserves default-off behavior.

C7: AGREE — Helm map merging retains inherited secrets unless explicitly removed; validate the enabled, fully merged container environment.

C8: AGREE — Startup validation and every authentication transport must support token-file mode, with no secret fallback after read failure.

C9: AMEND: Scale to zero or disable; pod deletion permits replacement; old-bearer rejection follows deletion leeway/removal plus cache, capped by token expiry; issued JWTs expire within 300s of the last mint, but rejection additionally allows consumer clock skew — weld-auth defaults to 30s (`platform/sdks/weld-auth/src/weld/auth/auth_guard.py:98`, `:228`).

F-a: AGREE — `authentication.k8s.io/tokenreviews:create` is sufficient; namespace-prefix both chart-owned cluster resource names to avoid cross-namespace ownership collisions.

F-b: AGREE — `strive-gatekeeper` identifies this verifier more clearly; enforce the identical audience in projection and review.

F-c: AMEND: Keep the checks and authoritative TokenReview; bound negative-cache capacity/TTL, include `client_id` in request-dependent negative keys, and exclude operational failures — a subject-precheck refusal must not poison another client’s valid token (`plans/2026-10-06-s2s-identity-alignment.md:89`).

F-d: AGREE — Identical generic authentication failures avoid exposing registry membership or verifier-selection details.

F-e: AMEND: Document bot-editable image/code paths as bypasses of effective grants; retaining that weaker gate requires explicit owner acceptance — the deployed image controls enforcement regardless of protected registry contents (`platform/deploy/helm/charts/gatekeeper/templates/deployment.yaml:85`).

X-a: AGREE — Cached authentication must remain bound to reviewed identity, requested client, audience and a fixed expiration.

X-b: AGREE — One combined load and one shared registry prevent inconsistent authentication and authorization views after base-load failure.

X-c: AGREE — Test collision rejection across base/extras and within extras; first-match subject lookup otherwise makes identity ambiguous.

X-d: AMEND: Before the initial flip, require per-replica intended extras hashes, matching base hashes and preshared mints; immediately after flipping, require per-replica k8s mints before accepting activation; subsequent active rolls require both — the dark harness has no SA (`platform/deploy/helm/Chart.yaml:76`).

X-e: AGREE — Rollback must restore compatible image and configuration together, after darkening the harness.

X-f: AGREE — Choose the permitted post-flip alternative; an unmanaged temporary SA adds unnecessary ownership and cleanup work.

X-g: AGREE — Add `composite` to settings and replace contradictory startup wording with K2’s explicit contract.

X-h: AGREE — Exercise all authentication call sites, cross-client grant ownership and operational failures; prove dispatch never invokes the other branch.

X-i: AGREE — Template only `database-url`, preserve hex passwords, await fresh reconciliation of both ExternalSecrets, and update rotation/recovery instructions.

X-j: AGREE — Required CI must reject missing identities and every listed grant drift; parity remains a consistency check, not authorization.

X-k: AGREE — Observe fingerprint changes without logging bearer values, bypass both caches, and exercise reviewer-token rotation too.

X-l: AGREE — A surviving owner-run probe must retain the old bearer and test both replicas through rejection, using C9’s corrected timing.

X-m: AGREE — B bounds credential exposure without removing cross-tenant authority; deferring grant restrictions and encrypted transport needs explicit owner acceptance.

K1: DISAGREE — **Codex’s side:** protect the complete deployment/build/authentication path and CI enforcement, or require owner approval on effective `main`; Opus’s list alone is insufficient — an unprotected shared Helm helper still controls the deployed image (`platform/deploy/helm/templates/_helpers.tpl:147`).

K2: AGREE — **Option 1:** reject the entire extras document before merging, boot with validated base only, log an error and expose rejection state; this preserves user authentication while failing closed for extras.

K3: AMEND: Drop the temporary pre-flip probe; retain Phase 3 hash/preshared checks, then mandatory post-flip per-replica k8s probes and #2092 checks, darkening on failure — requiring Phase 3 k8s success contradicts dropping its only credential source (`plans/2026-10-06-s2s-identity-alignment.md:160`).

NEW: Require bounded TokenReview concurrency/QPS before rollout — attackers can forge every unverified precheck claim and vary signatures to defeat negative caching; the current mint route invokes authentication without a request limiter (`platform/infra/gatekeeper/src/app/gatekeeper/service_token.py:152`).
## Fable

(Recorded verbatim in the session transcript; summary: Q0 B; AGREE on all items except AMEND F-a (prefix ClusterRole too), F-c (negative cache only for completed refusals, shorter than 60 s), X-d (per-replica k8s mint is post-flip); K1 sides with the Opus minimal set plus protecting the CI guard itself; K2 option 1; K3 drop the pre-flip probe; NEW: the guard must be a required status context.)
