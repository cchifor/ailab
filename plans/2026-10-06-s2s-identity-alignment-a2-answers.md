# S2S identity alignment: round A2 answers

## Codex

C3′: AMEND — Also guard `SERVICE_REGISTRY_PATH` and `gatekeeper.envFromGatekeeper.secretName`, checking rendered environment and registry mounts; either can redirect base authority despite protected extras.
C5′: AGREE — Live 401 refusals match precheck/dispatch behavior; mock the completed-review 403 and assert `sub`/`azp`.
C9′: AGREE — Scaling down or disabling prevents replacement; the stated deletion, cache, expiry and consumer-skew bounds correctly describe revocation.
F-a′: AGREE — The single TokenReview-create permission is sufficient, and namespace-prefixing both resources avoids cross-namespace collisions.
F-c′: AGREE — TokenReview remains authoritative; completed-refusal-only caching with bounded TTL/capacity and client-specific keys prevents poisoning and outage caching.
F-e′: AMEND — Explicitly accept effective-grant bypasses through the listed residuals plus unprotected build/tag publication; identify actual pin-check wiring in `ci.yml` (`ailab-pin-guard.yml` is a manual audit).
X-d′: AGREE — Per-replica effective hashes and successful mints establish activation; failed post-flip checks require darkening the harness.
K2′: AGREE — Rejecting the entire extras document preserves validated base authentication while failing closed for extras; unknown backends remain fatal.
K3′: AGREE — Phase 3 hash/preshared checks followed by mandatory post-flip k8s probes avoid needing a temporary ServiceAccount.
N1: AGREE — Share a process-wide semaphore (e.g. 4 concurrent) and token bucket (e.g. 10/s, bounded burst) across all uncached TokenReviews; saturation immediately returns uncached 503 `temporarily_unavailable`.
N2: AGREE — Protect separate `s2s-authority-guard.yml` and its script, add its context to `status_check_contexts`, emit it on every PR, and prove a red guard blocks merging in Phase 0.
K1′: AMEND — Endorse the scoped protection with the amendments above and below; signature requires explicit owner acceptance of the remaining effective-authority bypasses.
K1′.1: AGREE — The expanded protected set covers the identified direct edit paths; Phase 0 must prove every installed pattern against actual bot merge routes.
K1′.2: AMEND — Retain the existing checker as a retention/ancestry filter and explicitly accept unverified build/tag provenance; `ailab`-family tags pass without ancestry validation (`scripts/ci/check-ailab-pins.py:140`).
K1′.3: AMEND — Separate automation identities and remove/revoke workers’ owner credentials before Phase 0 passes; the exception requires directly confirmed owner acceptance of worker-approvable grants, since a shared-login ADR entry cannot establish human approval.
K1′.4: AGREE — Record and obtain explicit owner acceptance of F-e′ as amended before activating B.
NEW: V6’s inference that every accepted digest necessarily derives from merged main is unsupported by the checker; K1′.2 and F-e′ above resolve this sign-off blocker.
SIGN: yes — I endorse B with all items as agreed/amended above as the best solution
## Fable

SIGN: yes. AMEND C3' (also SERVICE_REGISTRY_PATH, envFromGatekeeper.refMap.service-registry, volume overrides), F-e' (add build.yml, protect-ailab-images.yml), N1 (settings fields, Retry-After, metric, preshared unaffected), N2 (no paths filter; prove unrelated PR merges), K1'.1 (add main/__main__/cli/core-config/pyproject/uv.lock). AGREE on all other items.
