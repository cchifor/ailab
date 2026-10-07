# Fable review — round 2 — auth-hardening-and-ops

Plan: `C:/Users/chifo/work/ailab-wt-plan/plans/2026-10-07-auth-hardening-and-ops-plan.md` (branch
`plan/auth-hardening-and-ops`, `1bb56ed5`). Refs checked: platform `a1014dfed` (the plan's pin) and `gitea/main`
`b5b70c0c4` (4 later commits, all ADR-033 pack/test files; `git diff --name-only a1014dfed gitea/main` touches no
cited path); ailab `origin/main` `c5a397a1`; spec `platform-authentication/` as on disk; live `admin@ai` read-only
(Secret key NAMES only, CronJob schedules, HelmChart `valuesFiles`, gatekeeper env names, `loki-lan`). No Secret
value read or printed.

## Verdict

**SIGN WITH CHANGES.** All five round-1 sign conditions are genuinely resolved in the plan text (E1a carries the
metrics Service; A1.1–A1.4 have the client sample, namespace-wide log acceptance and a quiet-hours window; the
`__Host-`/`Path=/` cookie with pruning, all-cookie deletion and navigation-only redirects closes the accumulation;
A6.4's rollback suspends `platform-identity` first; `gitea/gitea-admin` is a real, manifest-backed break-glass and
B0 proves it before anything is revoked). The 14 other round-1 items are also addressed, the two modified
adoptions are acceptable (#17 corrects me; #9 is right about *content* but wrong about the pin, finding 2), and
I side with the plan on all four Codex pushbacks, having verified the code each one rests on. The config-only
drill is the right D13: the two S2S windows are inherent to the registry's collision rule (a client cannot be in
the base and the extras at once, `service_registry.py:406-427`), so option (b) could not rehearse the one thing an
operator would need. What stops me signing as-is are three small gaps, two of them introduced by the revision:
(1) C4/C8 are written as a **git revert of `gatekeeper-secrets.enc.yaml`**, which after A4.3 silently restores
the old internet-exposed test-bypass token and after A5.2 steps 3–4 produces a ciphertext Flux cannot decrypt
mid-drill — C4/C8 must be a `sops` edit of the *current* file; (2) D15's narrowing leaves the realm-sync image
pin in `sync-job.yaml` neither owner-acked (`owner_ack.py` covers only `providers/*.yaml` and `workers/*.yaml`)
nor digest-checked (`check-keycloak-realm-sync-deployed.sh` only greps for *a* `keycloak-realm-sync` image), so
"its code is protected" is not the whole justification — add the file to the owner-ack list or record the
residual; (3) the revision replaced the original AUTH-error log regex with my timeout regex instead of merging,
so A1.4/A1.5 no longer count `NOAUTH|WRONGPASS|AuthenticationError`, the exact class a missed consumer produces
once A1.5 is live. I sign once findings 1–3 are folded in (each is a few lines); finding 4 (the rehearsed
rollback stops working after D5 deletes its material) is strongly recommended but not a condition.

## Round-1 findings: resolution check

| # | Resolved? | Note |
| --- | --- | --- |
| 1 E1a metrics Service | yes | E1a has both values, the pre-merge render check, "Deployment unchanged (no restart)", acceptance `up{service="traefik-metrics"}`; V53 updated. |
| 2 A1.1–A1.4 checks, window | yes, one regression | A1.1 samples 10 min = two `airlock-reaper` cycles (LIVE schedule `*/5 * * * *`); V5 records no CronJob/Job with a Redis env; A1.2 acceptance is namespace-wide with airlock/mcp by name; A1.4 in V52 hours, "not on the day of another gatekeeper roll"; A1.3 asserts `issubclass(AuthenticationError, ConnectionError)`. **But** the regex now lacks the AUTH-error class → finding 3. |
| 3 OIDC cookies accumulate | yes | `__Host-gk_oidc_<hash>`, `Path=/`, `/auth/login` prunes to 3, `/callback` deletes every `gk_oidc` cookie, only navigations get the 302 (same test as `denials._is_navigation`), unit test "five starts then one callback leave none". Two-tab consequence → nit 11. |
| 4 A6.4 rollback halves | yes | A6.4: suspend `platform-identity` → remove attribute → verify legacy authorize → re-pin → land the sync revert before resuming; risks list "PKCE enforcement lock-in" matches. |
| 5 Break-glass | yes | V59/B0/B8. Verified `kubernetes/apps/apps/gitea/gitea-admin.sops.yaml` in the kustomization ("break-glass local admin") and `gitea.yaml:92-93` `admin.existingSecret: gitea-admin` with `passwordMode keepUpdated`; B0 runs before any revocation; D12 names it. |
| 6 D13 image drill gates AG1 | yes (amended) | Config-only, gates none of AG1a/AG1b/AG2; amended for #2125 (restore the base). The amendment is correct in substance; its *mechanism* (git revert of a SOPS file) is finding 1. |
| 7 Split AG1 | yes | AG1a = A1.0, A1.0b, A2.1, A4.1; AG1b = A6.1 + `validate_state`; A1.4's "never below" binds to AG1a. |
| 8 age-key holder inventory | yes | A5.2 step 0 (Actions secrets on three repos, `external-secrets`, `flux-system`, workstation paths, backups) gates step 5; scratch rehearsal added. |
| 9 Protect keycloak paths | adopted with change | `main.py` + `realm-configmap.yaml`. Verified the redirect-URI *source* is the realm JSON (`extract_client_redirect_uris`, `main.py:576-600`, read from `KC_REALM_JSON_PATH=/realm/realm.json` = the ConfigMap) and the reconciler is `main.py:626-729`, so the content surface is covered. The image pin in `sync-job.yaml` is not → finding 2. |
| 10 Stale pin | yes | Re-pinned to `a1014dfed`; now 4 commits stale again but none touch a cited path (checked) → nit 13. |
| 11 V1/V8 comment | yes | V1 marks the comment historical; A1.5 replaces it; A1.3 adds the gatekeeper-shaped wrong-password check. |
| 12 E3 labels | yes | `release: kube-prometheus-stack`, `severity`, fixture asserts the label. |
| 13 E2 feasibility | yes | One curl per URL from the Gatus pod, `cf-cache-status` check, Gatus UA kept. |
| 14 Simpler owner terminal | yes | DPAPI helper dropped; `Read-Host -AsSecureString`, mint-before/delete-after, `GiteaOwnerTokenStanding` 4 h. Transport detail → nit 10. |
| 15 B5 fallback role | yes | Stated as superuser-over-socket exporter; fallback is an aggregate-only view. |
| 16 `scripts/forge.sh` | yes | V59 and B8 (verified: absent at `origin/main`). |
| 17 `CDN-Loop`, why headers | adopted with correction | The correction is right: Sentinel's peer is its own pod IP (hostAliases → ClusterIP, V21), cloudflared's for internet traffic; both dynamic, so no address rule. V22 now lists all three markers. |
| 18 C1 web image | yes | Drill precondition "pinned web image still honours `WEB_AGENT_PANEL=legacy`; else wait". |
| 19 A3.2 one-write gate | yes | "one Flux apply writes all keys at once", 10-minute interval bound, plus Codex's revision/fingerprint gate. |

19/19 addressed; #2 carries one regression (finding 3); #9 carries one gap (finding 2).

## Codex pushbacks (1–4): my position

1. **D9 — third stage with a drain (Codex) vs expand/contract (plan).** **With the plan.** The only flows AG2
   can break are legacy-shape logins in flight at the roll; Keycloak's SSO session survives the failed
   callback, so the "Sign in again" link completes silently (no second password or MFA), and at V33's 2.4
   logins/hour in V52's hours the expected count is well under one. A third stage would keep the legacy callback
   — the code-injection surface F-06 is about — alive for a drain period plus another pin cycle. Not worth it.
2. **D10 — repository-limited permissions (Codex).** **With the plan.** `workstation-bot` replaces an org-owner
   token (`cchifor`) and a site admin (`chifor`), both already global merge authors (V42); a non-admin team-write
   member with the same eligibility is a strict reduction and the pattern already accepted for `dev-worker-bot`.
   Per-repository scoping would add four collaborator grants and four merge-author entries without removing any
   real grant. B4's negative checks are the right proof.
3. **A3.2 P1 — the wait does not preload caches (Codex).** **With the plan, verified.** weld refetches on an
   unknown `kid` (`sdks/weld-auth/src/weld/auth/jwks.py:95-105`); the harness refetches at once for a kid absent
   from a fresh document and arms its 60 s cooldown only after a *successful* miss-refetch
   (`services/harness/src/plugins/identity-gatekeeper/jwks.ts:73-86`); both fetch
   `http://gatekeeper.<ns>.svc:5000/auth/jwks` directly (`charts/harness/values.yaml:125`), no HTTP cache in
   between. Promotion therefore never depends on preloading; the 11-minute wait is margin and P2's probes, journey
   and `invalid_token` baseline are the end-to-end proof. One bounded edge case → nit 14.
4. **C5 — artifact-generation gate (Codex, P1).** **With the plan, verified.** The worst case is an upgrade from
   the stale 3-file HelmChart artifact at the C4 commit: gatekeeper stays composite, the restored base holds the
   ten `client_id`s, and `load_registry_with_extras` rule 5 refuses each colliding extras entry *alone* while
   the rest merge and the base is never shadowed (`service_registry.py:406-427`, `_admit_extras:361-400`), so the
   reverted (preshared) services authenticate against the base, the harness is off, and nothing breaks; the only
   effect is a second, zero-downtime gatekeeper roll when the 2-file artifact lands, which C5's `valuesFiles` and
   `clients=/merged=` checks expose. The inverse race (a 2-file spec built from the pre-C4 tree, which *would*
   strand the ten token-mode services on a preshared-only gatekeeper) is excluded by `flux-resume.sh` gate 1
   (source artifact at the target sha before the Kustomization resumes, `flux-resume.sh:190-194`). A gate in the
   script is not needed; a record is (nit 7).

## New findings

1. **[important] C4/C8 revert `gatekeeper-secrets.enc.yaml` by git, which is wrong whenever the file has moved
   since #2125** — Location: C4 first bullet ("revert #2125's deploy changes ... `gatekeeper-secrets.enc.yaml`
   with its checksum"), C8, A5.2 step 5's "Drill 2 must run before this step", sequencing 6.
   Problem: LIVE `gatekeeper-secrets` holds `service-registry`, `session-fernet-key`, `delegation-grant-fernet-key`,
   `test-bypass-token`, `gatekeeper-client-secret`, `secret-key` in one SOPS document. The sequencing lets A4.3
   (test-bypass rotation, step 3), A5.5 (Fernet rotation, step 11) and A5.2 (steps 1–4, "after AG1a") run before
   the drill (step 6, "any time after E1a"). A `git revert` of the file then (a) restores the old, internet-exposed
   test-bypass token (A4.3 undone, silently, while F-04 is reported resolved), (b) restores the old Fernet keys
   (every session dies again), and (c) after A5.2 step 3 drops the new recipient stanza and after step 4 yields a
   ciphertext `sops-age` can no longer open, so `platform-secrets` goes not-Ready and C5's first wait never
   completes with the freeze half-released. The "before A5.2 step 5" guard does not cover steps 3–4.
   Fix: C4 and C8 edit the *current* ciphertext with `sops` (re-add / remove the ten `services:` entries whose
   argon2 hashes are taken from the pre-#2125 revision decrypted locally — hashes only, never secrets), recompute
   the checksum from the new ciphertext, and never `git revert` a `.enc.yaml`; the only git reverts are the YAML
   values blocks (`ailab.yaml`, the four worker manifests) and the `valuesFiles` entry. With that, the A5.2
   ordering constraint can be dropped (keep the calendar rule).
2. **[important] D15's narrowing leaves the realm-sync image pin unacked and unchecked** — Location: D15
   ("`sync-job.yaml` stays unprotected so fleet pin bumps ... stay bot-mergeable (its code is protected)"),
   response log (b) #9, risks "Unprotected security code".
   Problem: `owner-ack` protects only `deploy/helm/values/providers/*.yaml` and `deploy/components/workers/*.yaml`
   (`docs/runbooks/owner-ack.md:21-23`), and the CI guard requires only that *some* `keycloak-realm-sync` image
   is wired (`scripts/ci/check-keycloak-realm-sync-deployed.sh:52`). So a bot-approvable PR can pin any digest in
   `registry.chifor.me/strive/keycloak-realm-sync` for a Job that holds the Keycloak `admin` password
   (`sync-job.yaml:169-172`) with no owner touch — including a pre-A6.4 image that never asserts PKCE. (A
   downgrade would not *remove* the attribute: `sync_client_redirect_uris` PUTs the live representation's
   attributes merged, so this is "stops re-asserting", not "drops enforcement" — but the arbitrary-image point
   stands and the plan's justification is incomplete.)
   Fix (either): add `deploy/components/keycloak-realm-seed/sync-job.yaml` to `owner_ack.py`'s protected list
   (one bot-mergeable platform PR with its test; fleet pin PRs already need `approve-pin` for `ailab.yaml`, so the
   owner comments once on the same PR), or add the file to D15, or state the residual explicitly in the risks
   list. The same reasoning applies to `job.yaml`/`kustomization.yaml` (they could re-point the realm import), but
   the ConfigMap content is what matters and that is protected.
3. **[important] The AUTH-error log class fell out of the acceptance regex** — Location: A1.2 acceptance (reused by
   A1.4 step 3 "A1.2's Loki error count ... unchanged") and A1.5 (no Loki check at all).
   Problem: the draft counted `NOAUTH|WRONGPASS|AuthenticationError`; the revision replaced it with the timeout
   terms from my finding 2 instead of merging. A NetworkPolicy miss is a timeout, but a consumer with a missing or
   wrong `REDIS_PASSWORD` after A1.5 is `NOAUTH`/`WRONGPASS`/`AuthenticationError` — silent in gatekeeper (gauge
   covers it), a warning in airlock/mcp/workers (no gauge). A1.5's acceptance is PING semantics + gatekeeper's
   gauge + persistence; the other 14 consumers have nothing.
   Fix: one regex for A1.2/A1.4/A1.5: `Error 110|Timeout connecting|Connection refused|Redis client unavailable|in-memory fallback|NOAUTH|WRONGPASS|AuthenticationError`,
   counted namespace-wide over 24 h, and A1.5 lists it explicitly.
4. **[important] After D5 deletes the inert client secrets, the rehearsed rollback no longer works as written** —
   Location: D5 (a), sequencing 12 ("D5's deletion ... after C11"), C11 ("rollback after #2125 is C4's
   config-and-secret PR").
   Problem: C4 restores the ten argon2 *hashes*; they are only useful while each `<svc>-secrets` still holds the
   matching `gatekeeper-client-secret` (LIVE: present, e.g. `airlock-secrets`; SECRETS.md:160-166). D5 (a) deletes
   exactly that material once the drill has run, so the runbook's kept rollback becomes "A5.4 for ten clients (new
   secrets + hashes + checksum) and then C4", a longer procedure with a different window than the one measured.
   Fix: either D5 = keep the keys (they are inert and cost nothing; delete them at the next rotation window
   instead), or C11's runbook text states the post-D5 rollback is A5.4 × 10 then C4, with the window unmeasured.
5. **[nit] V62 overstates "an open redirect today"; keep the hardening in AG1b and test the new sink** — Location:
   V62, F-06 design (`validate_state`), A6.1 tests.
   `validate_state("/\\evil.example")` does return it unchanged (`helpers.py:409-422`), but the only sink today is
   `RedirectResponse(url=safe_state)` (`routes.py:1831`), and Starlette percent-encodes `\` (not in its `safe`
   set) so the browser gets `Location: /%5Cevil.example`, a same-host path. The new design's error page
   (`<a href="/auth/login?redirect_uri=...">`) *is* a sink where `/\` reads as `//`. Fix: reword V62; keep the
   fix in AG1b (where the sink appears); add "the error-page link is built from the validated path and
   attribute-escaped; `/\evil.example` renders a same-host link" to A6.1's tests.
6. **[nit] C4 should name the contract it must flip, and C8 must not be an open dependent PR** — Location: C4
   "plus any CI contract lines that assert token mode"; preconditions "C4 and C8 PRs prepared".
   `deploy/helm/scripts/tests/check-s2s-token-mode-contract.sh:30-34` asserts "ailab as committed (D.8) ... exactly
   that split" and `check-s2s-authority.py` (b0) pins the backend baseline; both fail a bare revert. Name them.
   C8 reverts C4, so it stacks on C4's branch: prepare it as a branch with a CI run, open the PR only after C4 is
   live (the reviewbot rule the plan itself cites).
7. **[nit] Record the artifact path and measure the workers separately** — Location: C5, C6, C7.
   Add to C5's record: HelmChart `strive-ailab-strive` `.metadata.generation`/`.status.observedGeneration` and
   `.status.artifact.revision` before and after resume, and whether gatekeeper rolled once or twice (pushback 4's
   benign case changes the measured window). The four workers switch only when `platform-workers` applies after
   `platform-app` (V60), so their mint-failure window is structurally longer than the services'; C6 should
   record it as its own number.
8. **[nit] Three B5 alerts fire from B5 until B6, not one** — Location: B5 ("`GiteaOrgAccountHasTokens` fires
   until B6"). `GiteaOwnerTokenStanding` fires for `cc-admin-20260913` (older than 4 h) and
   `GiteaAdminTokenNotAllowlisted` for the 39 from the moment the rules load, 7+ days before B6. Say so and plan
   an Alertmanager silence scoped to those three until B6 (or load them with B6).
9. **[nit] B6 can delete `cchifor`'s tokens through the API too** — Location: B6 ("deletes `cchifor`'s 26 in one DB
   transaction and then restarts Gitea"). `DELETE /api/v1/users/{username}/tokens/{token}` is `reqSelfOrAdmin()` +
   basic auth (`docs/runbooks/agentforge-platform-activation.md:316-320`), so `gitea_admin`'s basic auth deletes
   another user's tokens by the supported path; no DB write or restart. Keep the DB path only as the fallback if
   the API refuses.
10. **[nit] D11 script transport** — Location: Owner actions 2. Say the token never appears on a command line
    (`curl -H "Authorization: token $t"` is visible to process listing): use in-process HTTP
    (`Invoke-RestMethod -Headers`) or `curl -H @-` on stdin, convert the SecureString only into a local variable
    that is cleared at the end, and run with PowerShell transcription off.
11. **[nit] Deleting every `gk_oidc` cookie at `/callback` costs the second tab one error page** — Location: F-06
    design, A6.1 matrix "two tabs". Tab A's callback deletes tab B's transaction cookie; B's callback then shows the
    error page although A's session already exists. Either state that as accepted (it is a single click, no loop)
    or delete the matched cookie plus any that fail to decrypt or are expired, leaving valid siblings — pruning at
    `/auth/login` already bounds the count. Also: reuse `denials._is_navigation` (`denials.py:55-58`) for the
    navigation test rather than a second copy.
12. **[nit] `GatekeeperTokenReviewLimited` will fire on any planned platform-wide roll** — Location: E3, D16.
    The rule is correct (`gatekeeper_tokenreview_limited_total` and `gatekeeper_tokenreview_total{outcome="unavailable"}`
    exist, `metrics.py:384-421`), but A1.4's and C4/C8's rolls can burst more than 20 uncached mints; the silence
    rule says "drills" — extend it to every planned platform-wide roll, or the owner learns to ignore it.
13. **[nit] Pin is four commits stale again** — Location: Pinned references. LIVE release is
    `0.2.0+b5b70c0c47b9.2`; `a1014dfed..b5b70c0c4` changes 49 files, all `services/workflow` tests and
    `scripts/packs/*`; add "verified unchanged for every cited path at b5b70c0c4".
14. **[nit] A3.2 P2 harness edge case** — Location: A3.2 P2, pushback 3. If the harness refetched for a
    *fabricated* kid within the 60 s before the first NEW-signed token arrives, NEW is refused until the cooldown
    ends (`jwks.ts:84`): at most 60 s of `IDENTITY` 401s for harness-bound calls, self-healing. Note it in the
    rotation runbook so a short blip during P2 is not read as a failed promotion.

## Owner decisions: position per changed D-n

- **D5 (a, rescoped)** — agree that no ailab client is preshared and that the drill measures the window; but (a)'s
  deletion step conflicts with the rollback it rehearses (finding 4). Prefer: keep the inert keys until the next
  rotation window, or state the post-D5 rollback honestly in C11.
- **D9 (a)** — agree; Codex's drain stage is not worth a third release (pushback 1).
- **D10 (a)** — agree (pushback 2).
- **D11 (a, owner terminal)** — agree; add the transport sentence (nit 10). (b)'s CronJob is a standing DB writer on
  tokens for a bound the alert already gives — rightly not recommended.
- **D12 (yes, with B0)** — agree; B6 can use the admin API for every user (nit 9).
- **D13 (a, config-only, restores the base)** — agree, with finding 1 (sops edit, not git revert), nit 6 and nit 7.
  (b) cannot rehearse the window that the collision rule makes inherent; (a)'s cost is honest and measured.
- **D14 (yes/yes/keep)** — agree; E1a is now complete.
- **D15 (yes, before AG1a, wider list)** — agree, with finding 2 for the `sync-job.yaml` pin. The verification
  step (bot no-op PR cannot be merged) is the right proof.
- **D16 (a)** — agree: 0 limited reviews so far, 60 s positive cache, attacker must be an admitted workload; the E3
  rule is well-formed against the real metric names; correct ADR-034:303-318 and F-25's "Accepted" rationale
  ("preshared clients are unaffected" is false on ailab since #2125). Nit 12 for the silence policy.
- **D17 (a)** — agree; F-05 stays "partially addressed" until the four issues close, which is the honest status.

## Claims verified this round

Code at `a1014dfed`: `validate_state` and `exchange_code` (V62); `svc_auth_backend` Literal and extras semantics
(`config.py:341-351`); `load_registry_with_extras` rules 1–5 and `_admit_extras`; `_redirect_to_login`
(`routes.py:1611-1645`) and the callback's `RedirectResponse` (`:1831`); the bypass track (`:789-811`); session-id
log sites (exactly `server_session.py:149,311,352,360,379` and `routes_session.py:80` — A1.0b's file list is
complete) and the `gk:session:<id>:{body,active}` keys; TokenReview metric names and labels; weld and harness JWKS
refetch logic and the harness JWKS URL; keycloak-sync module layout (`bootstrap.py`, `main.py` only),
`extract_client_redirect_uris` reading the realm JSON, `sync-job.yaml` env and digest; `owner_ack` scope;
`check-keycloak-realm-sync-deployed.sh`; `list-ailab-pins.py` listing the sync Job; `check-s2s-token-mode-contract.sh`
D.8 assertion; #2125's file list and `ailab.yaml` hunks; SECRETS.md:111-176; Flux `dependsOn` (V60);
ADR-034:298-318. ailab `origin/main`: `gitea-admin.sops.yaml` + `gitea.yaml:92-93`; `loki-lan.yaml` trade-off
header; `flux-resume.sh` gates 1–4; `s2s-identity.md:137-164,342-354,598-646`; activation runbook `:316-320`.
LIVE: `airlock-secrets`/`gatekeeper-secrets` key names; CronJob schedules (`airlock-reaper` `*/5`); `loki-lan`
NodePort 30310; HelmChart `strive-ailab-strive` three `valuesFiles`, `0.2.0+b5b70c0c47b9.2`; gatekeeper env has
`SVC_AUTH_BACKEND`, `SERVICE_REGISTRY_EXTRAS_PATH`, `TEST_BYPASS_ENABLED`; ConfigMap `gatekeeper-registry-extras`;
gatekeeper image `3adaf0be...` (V45). Spec: F-05/F-06 text, user-auth invariants 1–5, S2S rollback paragraph
(`service-to-service.md:600-603`, which C11/A5.7 must update alongside the runbook).
