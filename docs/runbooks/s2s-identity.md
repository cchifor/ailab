# S2S identities from projected ServiceAccount tokens: owner runbook

Owner-only steps for activating secretless service identities (the harness authenticating to
gatekeeper with its pod's projected token). The design, phases and rationale are in
[`plans/2026-10-06-s2s-identity-openbao-plan.md`](../../plans/2026-10-06-s2s-identity-openbao-plan.md);
this page is the checklist and does not repeat them. The DSN side (Phase 1) is in
[`infra-pg.md`](infra-pg.md), section "strive-pg (the PLATFORM's cluster ...)".

Nothing here is done by automation or by a dev worker. Do not activate (Phase 4) until D1, Phase 0
and D2/D3 below are complete and recorded.

## Reference: the TokenReview rule in force

How gatekeeper's `TokenReviewVerifier` (platform `infra/gatekeeper/src/app/gatekeeper/tokenreview_verifier.py`)
turns a TokenReview into a response for an `auth_method: k8s` client. It is the signed plan's rule: the
execution-time amendment that made a `status.error` refusal a 401 was WITHDRAWN. Read probe output and
the `gatekeeper_tokenreview_*` metrics against this table.

| What gatekeeper saw | Response | Cached |
|---|---|---|
| Prechecks fail on the UNVERIFIED token: not a three-part JWT, the audience is missing, `sub` is not the entry's `k8s_subject`, or `exp` is past | generic 401, no API call | no |
| A 2xx review with a non-empty (or non-string) `status.error`, checked BEFORE `authenticated`, even when `authenticated=true` | **503** `temporarily_unavailable` with `Retry-After` | **never** |
| A non-2xx from the API (including its own 401, 403 and 429), a transport error, a timeout, a malformed body, an unreadable own token, or limiter saturation | 503 with `Retry-After` | never |
| A completed review with EMPTY `status.error` and `authenticated` false or omitted, or the audience missing from `status.audiences` | generic 401 (the body an unknown client gets) | negative, per (token, `client_id`), at most 10 s |
| A completed review that is authenticated, with the audience present, but `status.user.username` is not the entry's `k8s_subject` | 403 `unauthorized_client` | negative, same key and TTL |
| A completed review that is authenticated, with the audience present and a matching username | success | positive, per token, at most 60 s and never past the token's `exp` |

- **Why a refusal with `status.error` is a 503.** For a genuine client it is almost always a transient
  authenticator failure. The harness retries a 503 and treats a 401 as final, and a 503 still denies a
  forged or revoked token.
- **Only a completed review is cached.** A 503 never writes or extends a cache entry. A negative entry
  answers without an API call or a limiter slot. Every positive hit re-binds the REQUESTED `client_id`
  (the reviewed username must equal that entry's `k8s_subject`), and `exp` is rechecked after the
  review and on every hit: a token that expired meanwhile is the generic 401 and is not cached.
- **Single-flight.** Concurrent misses for the same token share ONE review (keyed by the token digest;
  only the leader takes a limiter slot, and nothing queues). A waiter binds its OWN `client_id` to the
  shared identity: a shared 503 is the waiter's 503, and a shared refusal is its generic 401 or 403,
  negatively cached under its own key.
- **The 403 is not reachable live.** The `sub` precheck refuses a mismatched subject first, so only a
  mocked review produces it.
- **Metrics.** `gatekeeper_tokenreview_total{outcome}` counts `authenticated`, `refused`, `mismatch` and
  `unavailable` (the 503s) for reviews past the caches and the limiter;
  `gatekeeper_tokenreview_limited_total` counts saturation; `gatekeeper_tokenreview_cache_total{cache,result}`
  counts `positive`/`negative` hits and misses.

## D1. Identity split (Phase 0 step 1): DONE 2026-10-06

Before D1, dev workers authenticated to Gitea as `chifor`, the owner: `ansible/roles/dev_worker/defaults/main.yml`
had `dev_worker_gitea_user: chifor`, and the PAT is OpenBao `af/dev-workers/common` field
`gitea_pat`, seeded from SOPS `kubernetes/apps/infrastructure/security/openbao/devworker-seeds.sops.yaml`
(see [`openbao-dev-workers.md`](openbao-dev-workers.md)). A worker could therefore merge as the owner,
which defeats any owner-review gate. The split was executed on 2026-10-06.

### What was done

- Gitea user `dev-worker-bot` (non-admin), created in-pod with `gitea admin user create`.
- Org team `automation` (id 37): write on every unit (external units read), never admin, all repos,
  can create org repos. `dev-worker-bot` is its only member.
- Bot tokens `dev-workers-20261006`, `dev-workers-package-20261006` and
  `dev-workers-repo-create-20261006`, minted with
  `gitea admin user generate-access-token --username dev-worker-bot ... --raw`.
- ailab #1090:
  - re-encrypted `devworker-seeds.sops.yaml` (three fields) and `ansible/secrets/dev-worker.sops.yaml`
    (`dev_worker_gitea_token`);
  - set `dev_worker_gitea_user: dev-worker-bot`;
  - added `dev-worker-bot` to `pr_reviewer_merge_authors`.
- Converge order: reviewers first, then workers; the provision Job was re-run.
- An out-of-band `~/.gitconfig` `credential.https://git.chifor.me.username chifor` existed on the
  workers. It was repointed to `dev-worker-bot`, and ansible now owns it on both credential paths
  (`ansible/roles/dev_worker/tasks/pin_gitea_username.yml`).
- Revoked the `chifor` tokens with ids 2, 4, 207 and 276 (by deleting the DB rows). The old tokens
  were proven to return 401.
- `.12`/`.13` presented changed SSH host keys and were not touched. Resolved: the dev-worker-5 and
  dev-worker-6 slots were retired on 2026-09-23 and 2026-09-21. Those addresses now belong to the
  cloudlab CI runners `cloud-ci-13` (6113) and `cloud-ci-9` (6109), as `docs/network-plan.md`
  records. The fleet is `dev-worker-1..4` on `.8`–`.11`.
- Known effects, both by design: the owner-ack `approve-pin` flow on platform can no longer be
  posted as the owner by a worker, and "merge as chifor from a worker" no longer works. The owner
  acknowledges and merges in person, from a non-shared login.

### Rotation / re-do procedure

1. As Gitea site admin, create a NON-admin user (`dev-worker-bot`) and put it in the org team
   `automation` (write on the repos the workers need; never admin, never owner).
2. Mint THREE tokens for that user, one per field of `af/dev-workers/common` (`gitea_pat`,
   `gitea_repo_pat`, `gitea_package_pat`), each with the scopes `openbao-dev-workers.md` lists for that
   field (`gitea admin user generate-access-token --username dev-worker-bot ... --raw`, run in the
   Gitea pod). Keep the values out of the terminal history and out of git.
3. Replace the seed, ALL THREE fields. Either edit the SOPS seed (owner-only; age key in
   `kubernetes/infra/_out/age.agekey`; the `common.json` value of
   `kubernetes/apps/infrastructure/security/openbao/devworker-seeds.sops.yaml` carries all three; the
   snippet in `openbao-dev-workers.md` substitutes `gitea_pat` only, so change the other two with
   `sops` as well), or patch the live path, one field per call with the value on stdin:
   ```sh
   for f in gitea_pat gitea_repo_pat gitea_package_pat; do
     printf '%s (new token, not echoed): ' "$f" >&2; IFS= read -rs v; echo >&2
     printf '%s' "$v" | bao kv patch -mount=af dev-workers/common "$f=-"
   done; unset v
   ```
   Do the SOPS edit too, or the next seed run restores the old values. Also re-encrypt
   `ansible/secrets/dev-worker.sops.yaml` (`dev_worker_gitea_token`, the same value as `gitea_pat`).
4. Ensure `dev_worker_gitea_user: dev-worker-bot` (`ansible/roles/dev_worker/defaults/main.yml`, or
   group/host vars) and converge: reviewers first, then workers (`just dev-workers`, see
   [`dev-workers.md`](dev-workers.md)). Run it twice; the second run should report near-zero `changed`.
5. Verify on a worker, per the smoke test in `openbao-dev-workers.md` section (g):
   `for f in gitea_pat gitea_repo_pat gitea_package_pat; do printf '%s ' $f; cred get common $f | wc -c; done`
   (lengths only, all three non-zero), `ls -l ~/.git-credentials` (0600), and
   `git ls-remote https://git.chifor.me/cchifor/ailab.git HEAD`. Then confirm the identity:
   `cred exec common gitea_pat T -- sh -c 'curl -s -H "Authorization: token $T" https://git.chifor.me/api/v1/user'`
   must show `dev-worker-bot` with `is_admin: false` (do not print the token). Only `gitea_pat` can
   be checked this way: the other two carry no user scope, and their owner is fixed by step 2, which
   minted them for `dev-worker-bot`.
6. **Revoke the `chifor` tokens that the workers held** (all three, one per field; Gitea UI as
   `chifor`: Settings, Applications, or delete the DB rows) and any other owner token copied to
   workers. Confirm each old token now gets 401.

## Phase 0: the owner gate on platform `main` (INSTALLED 2026-10-06)

### Record

Installed on `cchifor/platform` `main` on 2026-10-06 (Gitea 1.26.1), after the authority guard PR
(`.github/workflows/s2s-authority-guard.yml` plus `scripts/ci/check-s2s-authority.py`) merged.

- **Protected patterns: 26.** The 23 patterns of the plan (brace-expanded) plus
  `docs/decisions/ADR-034-s2s-projected-token-identities.md`, `scripts/ci/install-uv.sh` and
  `scripts/ci/with-retry.sh`. The list is below.
- **Status contexts:** `S2S Authority Guard / guard*` added; the 4 existing contexts kept.
- **Push:** whitelist `chifor` and `gitea_admin`.
- **Admin override:** `block_admin_merge_override: false` (the owner path).
- **Rule JSON:** saved at
  `home/ailab/kubernetes/infra/_out/platform-main-protection-before-20261006.json` and
  `platform-main-protection-after-20261006.json` (same directory, gitignored).

Gitea matches with gobwas/glob, `.` and `/` as separators, and brace support is unverified, so every
brace is EXPANDED into separate patterns. The API field is one `;`-separated string; the list, one
pattern per line:

```
deploy/helm/values/providers/ailab-s2s-registry.yaml
deploy/gitops/flux/clusters/ailab/**
deploy/helm/charts/gatekeeper/**
deploy/helm/templates/_helpers.tpl
infra/gatekeeper/src/app/gatekeeper/service_registry.py
infra/gatekeeper/src/app/gatekeeper/service_verifier.py
infra/gatekeeper/src/app/gatekeeper/service_token.py
infra/gatekeeper/src/app/gatekeeper/tokenreview_verifier.py
infra/gatekeeper/src/app/gatekeeper/config.py
infra/gatekeeper/src/app/core/lifecycle.py
infra/gatekeeper/src/app/core/config/**
infra/gatekeeper/src/app/main.py
infra/gatekeeper/src/app/__main__.py
infra/gatekeeper/src/app/cli/**
infra/gatekeeper/Dockerfile
infra/gatekeeper/pyproject.toml
infra/gatekeeper/uv.lock
.github/workflows/s2s-authority-guard.yml
scripts/ci/check-s2s-authority.py
scripts/ci/test_check_s2s_authority.py
scripts/ci/check-ailab-pins.py
scripts/ci/list-ailab-pins.py
deploy/secrets/ailab/**
docs/decisions/ADR-034-s2s-projected-token-identities.md
scripts/ci/install-uv.sh
scripts/ci/with-retry.sh
```

Beyond the plan: `deploy/gitops/flux/clusters/ailab/**` (a single listed Flux file could otherwise be
sidestepped by another file in that directory carrying arbitrary kinds) and
`deploy/secrets/ailab/**` (a plaintext Secret there could shadow the SOPS `gatekeeper-secrets`).
Expect about 19 owner reviews a month.

### Re-apply procedure

Use it to restore or extend the rule. It needs a **repo-admin** token for `cchifor/platform` (the
owner's, held outside the workers). Gitea applies only the FIRST matching rule, so patch the
effective one. The PATCH MERGES with the live rule: it unions the live `protected_file_patterns` and
`status_check_contexts` with the documented ones and never replaces them.

```sh
G=https://git.chifor.me/api/v1/repos/cchifor/platform
# Snapshots and the pattern list live in the MAIN checkout's gitignored kubernetes/infra/_out/ (a git
# worktree has no _out/), never in the current directory. Run this from inside any ailab checkout.
OUT="$(cd "$(git rev-parse --git-common-dir)/.." && pwd -P)/kubernetes/infra/_out"
mkdir -p "$OUT"
B="$OUT/main-protection-before.json"
curl -s -H "Authorization: token $OWNER_TOKEN" $G/branch_protections            # find the effective rule for main
RULE=<rule_name of the effective rule from the output above>   # often `main`, but use what the output shows
curl -s -H "Authorization: token $OWNER_TOKEN" $G/branch_protections/$RULE > "$B"
cat > "$OUT/patterns.txt" <<'EOF'
deploy/helm/values/providers/ailab-s2s-registry.yaml
deploy/gitops/flux/clusters/ailab/**
deploy/helm/charts/gatekeeper/**
deploy/helm/templates/_helpers.tpl
infra/gatekeeper/src/app/gatekeeper/service_registry.py
infra/gatekeeper/src/app/gatekeeper/service_verifier.py
infra/gatekeeper/src/app/gatekeeper/service_token.py
infra/gatekeeper/src/app/gatekeeper/tokenreview_verifier.py
infra/gatekeeper/src/app/gatekeeper/config.py
infra/gatekeeper/src/app/core/lifecycle.py
infra/gatekeeper/src/app/core/config/**
infra/gatekeeper/src/app/main.py
infra/gatekeeper/src/app/__main__.py
infra/gatekeeper/src/app/cli/**
infra/gatekeeper/Dockerfile
infra/gatekeeper/pyproject.toml
infra/gatekeeper/uv.lock
.github/workflows/s2s-authority-guard.yml
scripts/ci/check-s2s-authority.py
scripts/ci/test_check_s2s_authority.py
scripts/ci/check-ailab-pins.py
scripts/ci/list-ailab-pins.py
deploy/secrets/ailab/**
docs/decisions/ADR-034-s2s-projected-token-identities.md
scripts/ci/install-uv.sh
scripts/ci/with-retry.sh
EOF
PY=python3   # python3 on Linux hosts; use PY=python in Git Bash
BODY=$($PY - "$OUT/patterns.txt" "$B" <<'PYEOF'
import json, sys
want = [l.strip() for l in open(sys.argv[1], encoding='utf-8') if l.strip()]
live = json.load(open(sys.argv[2], encoding='utf-8'))
pats = [p for p in (live.get('protected_file_patterns') or '').split(';') if p]
for p in want:
    if p not in pats:
        pats.append(p)
ctx = list(live.get('status_check_contexts') or [])
if 'S2S Authority Guard / guard*' not in ctx:
    ctx.append('S2S Authority Guard / guard*')
print(json.dumps({'protected_file_patterns': ';'.join(pats),
                  'enable_status_check': True,
                  'status_check_contexts': ctx}))
PYEOF
)
curl -s -X PATCH -H "Authorization: token $OWNER_TOKEN" -H "Content-Type: application/json" \
  $G/branch_protections/$RULE -d "$BODY"
```

Re-read the rule and diff it against `$B` (`main-protection-before.json` in the same `_out/`; only those
two fields may change).

### Rule after the change (2026-10-06, non-secret fields only)

```json
{
  "protected_file_patterns": "deploy/helm/values/providers/ailab-s2s-registry.yaml;deploy/gitops/flux/clusters/ailab/**;deploy/helm/charts/gatekeeper/**;deploy/helm/templates/_helpers.tpl;infra/gatekeeper/src/app/gatekeeper/service_registry.py;infra/gatekeeper/src/app/gatekeeper/service_verifier.py;infra/gatekeeper/src/app/gatekeeper/service_token.py;infra/gatekeeper/src/app/gatekeeper/tokenreview_verifier.py;infra/gatekeeper/src/app/gatekeeper/config.py;infra/gatekeeper/src/app/core/lifecycle.py;infra/gatekeeper/src/app/core/config/**;infra/gatekeeper/src/app/main.py;infra/gatekeeper/src/app/__main__.py;infra/gatekeeper/src/app/cli/**;infra/gatekeeper/Dockerfile;infra/gatekeeper/pyproject.toml;infra/gatekeeper/uv.lock;.github/workflows/s2s-authority-guard.yml;scripts/ci/check-s2s-authority.py;scripts/ci/test_check_s2s_authority.py;scripts/ci/check-ailab-pins.py;scripts/ci/list-ailab-pins.py;deploy/secrets/ailab/**;docs/decisions/ADR-034-s2s-projected-token-identities.md;scripts/ci/install-uv.sh;scripts/ci/with-retry.sh",
  "status_check_contexts": [
    "CI / ci-gate*",
    "E2E Preflight / preflight*",
    "E2E Tests / smoke*",
    "Contract Tests / contract-gate*",
    "S2S Authority Guard / guard*"
  ],
  "enable_push": true,
  "push_whitelist_usernames": [
    "chifor",
    "gitea_admin"
  ],
  "required_approvals": 1,
  "block_admin_merge_override": false,
  "enable_status_check": true
}
```

### Gate tests (Phase 0 step 5): results

Gitea 1.26 checks merge preconditions in this order: status checks, approvals, rejected reviews,
official review requests, outdated branch, protected files. A merge attempt alone therefore cannot
isolate the protected-files check, so protected-file detection is read from the DB instead.

1. **Pattern detection.** PRs #2095, #2096 and #2097, authored by `dev-worker-bot` with label
   `no-automerge`, together touched one file per original pattern plus the unprotected control
   `infra/gatekeeper/src/app/gatekeeper/routes.py`. All 23 original patterns were flagged in
   `pull_request.changed_protected_files`, and the control never was. Gitea records at most 10 files
   per PR, hence 3 PRs.
2. **Bot merge routes.** Merge, squash, rebase and `force_merge` by the bot all returned 405. Status
   checks are Gitea's first precondition, so this proves the gate holds but not which check fired
   (hence item 1).
3. **Direct push.** A direct push to `main` by `dev-worker-bot` was rejected ("branch main is
   protected from changing file ...").
4. **Same permission class.** The reviewer bots that actually merge (`reviewer-codex`,
   `reviewer-claude`) have the same permission class as `dev-worker-bot` (write, non-admin; verified
   with the collaborator-permission API).
5. **End-to-end refusal on a green, bot-approved PR (#2098, 2026-10-06).** CI green with the guard success, 2 approvals (reviewer-codex, reviewer-claude); dev-worker-bot merge via merge, squash, rebase and force_merge -> HTTP 405 "Changed protected files". The PR was closed unmerged.
   **Added patterns (#2099):** the PR flagged the 3 added patterns (ADR-034, `install-uv.sh`,
   `with-retry.sh`) and not the control. All 26 patterns are now proven.
6. Controls still to record: the **owner path** (an admin merge of the Phase 2a/3 PRs) and the
   **unrelated-PR control** (the next non-protected merge goes through the existing automation).
7. A red guard (a forbidden setting, e.g. `gatekeeper.serviceAuth.composite.enabled: true` in
   `ailab.yaml`) blocks the merge, and the guard check is reported on that PR.

If the patterns do not hold, apply the plan's Phase 0 step 7 fallback (`required_approvals >= 1` on
the effective `main` rule, keeping existing protections) and repeat. Do NOT proceed untested.

### Recording the results

Write a dated record in the S2S ADR (the plan's Phase 0 step 6): the Gitea version
(`curl -s https://git.chifor.me/api/v1/version`), the rule JSON after the change, a table of
pattern x route -> blocked/allowed, and the red-guard, unrelated-PR and owner-path outcomes. Author
or confirm it through a non-shared identity (next section).

## D2 and D3: owner acceptance records

The owner decided D2 and D3 directly in the session on 2026-10-06, outside the shared login (the
ADR records them; see "How to record" below). The decisions:

- **D2: ACCEPTED (2026-10-06), together with the stale-token revocation below.** Residual bypasses that stay bot-approvable after Phase 0: unprotected gatekeeper modules
  (`routes.py`, `helpers.py`, the rest of `infra/gatekeeper/**`), `ailab.yaml` digest pins and `ci.yml`,
  `build.yml` (including `/release-build`), `scripts/ci/reviewed-release-build.py`,
  `protect-ailab-images.yml`, provenance of `ailab`-family tags, and the ailab Flux cluster-admin
  path into `strive-ailab`.

  **Platform-side Flux residual:** platform Flux Kustomizations without `serviceAccountName` and
  cluster-admin controllers apply whatever a bot-approvable path puts in the tree, and
  `deploy/components/**` is such a route (not a protected pattern).

  **Further residuals found at execution (2026-10-06), migrated the same day.** Automation that
  held `chifor` (site-admin) tokens was an owner-gate bypass path; none of it needed site-admin:
  - `ci-rerun-watchdog`, `cloud-power` and the reviewer webhook check now use `org-ops-bot`. It is a
    NON-admin member of the org Owners team (team 1), holding three tokens:
    - `org-ops-watchdog-20261006` with read:organization,read:repository. The watchdog is still in
      DRY_RUN; its rerun POST will need a separate non-owner write token.
    - `org-ops-cloud-power-20261006` with write:organization.
    - `org-ops-hookcheck-20261006` with read:organization.
  - `forge-actions-release` is replaced by `release-bot`. It sits in team `release` (code and
    releases read, packages write, `cchifor/forge` only), with token `forge-release-20261006`. It is
    the forge repo secret `FORGE_RELEASE_TOKEN` plus variable `FORGE_RELEASE_USERNAME`.
  - Revoked: `agentforge-ui` (scope `all`, held in AgentForge's `gitea_credentials` table and used
    only by its project wizard, so reconnect a scoped token in the UI if that is needed), and the
    dead session tokens `cloud-power-drain-pr` and `cloud-ci-session-20260923`. The replaced
    `ci-rerun-watchdog`, `cloud-power-drain`, `reviewbot-hook-check` and `forge-actions-release`
    are revoked once their replacements are proven in use.
  - 20 stale `chifor` tokens unused for 14+ days were revoked earlier (ids 9 25 30 31 76 77 84 85 87
    100 160 161 162 163 176 178 179 180 189 202, including `cp-all` with scope `all`).

  **Still open (owner credentials and accepted residuals):**
  - `cc-admin-20260913` is the owner's workstation credential (`~/.git-credentials` and Windows
    Credential Manager, `chifor@git.chifor.me`). It is interactive, not automation.
  - The org account `cchifor` (a converted user, uid 2) still owns 21 tokens. One of them is this
    workstation's default `git.chifor.me` credential, and it likely acts with owner rights on every
    org repo. Migrating those tokens is the owner's call.
  - `org-ops-bot` is an org owner. write:organization (cloud-power's runner pause, for which Gitea
    has no narrower scope) can also manage team membership. That token lives only in the
    cloud-power api pod, whose ingress is oauth2-proxy-only.
  - Two read-only tokens on the site-admin `gitea_admin` are held by automation:
    `af-ci-scaler-2941` (read:admin, `agentforge-ci/agentforge-ci-scaler-token`) and
    `flux-ailab-read` (read:repository, `flux-system/flux-gitea-auth`).
  - The operator's temporary `phase0-setup-20261006` is deleted after the Phase 3 owner merge.

- **D3: RESTRICT PRE-FLIP (decided 2026-10-06).** Gatekeeper supports a per-audience `grant_types`
  list. `svc-harness` gets `svc-mcp: [client_credentials, token_exchange]` and `[token_exchange]`
  for `svc-integration`, `svc-airlock`, `svc-workflow`, `svc-knowledge`, `svc-profile`,
  `svc-notification` and `svc-digest`. A harness audit found `client_credentials` used only for the
  `svc-mcp` capability publish; delegation issue and redeem require `token_exchange`. The plain-HTTP
  replay window on the harness-to-gatekeeper hop remains a follow-up.
  **Identity boundary:** any pod in `strive-ailab` that runs as ServiceAccount `harness` with a
  projected token for audience `strive-gatekeeper` holds the svc-harness identity.

**D2/D3 acceptance must be recorded BEFORE Phase 3.** Done 2026-10-06: both were decided directly by the owner (D2 accepted; D3 restrict pre-flip) and are recorded in platform `docs/decisions/ADR-034-s2s-projected-token-identities.md` (owner-protected).

**How to record, through a non-shared channel.** The acceptance must not be authored from the shared
`chifor` login that workers could have used. Either (a) commit or approve the ADR text as a distinct
non-shared owner identity (own device, own credentials, present on no worker), or (b) give a direct
confirmation outside the shared login (for example a signed commit, or a message to the reviewers
over a separate channel) and reference it in the ADR. Anything authored through `dev-worker-bot` or a
worker's credentials does not count.

## Phase 4: activation checks

Run after the flip (#2092) lands. Each check targets each gatekeeper pod IP separately (2 replicas),
not just the Service.

**Pre-flip / roll acceptance, per replica:** the effective `extras_sha` equals the rendered hash
(`extras_sha` is the SHA-256 of the exact ConfigMap data bytes:
`kubectl -n strive-ailab get cm gatekeeper-registry-extras -o jsonpath='{.data.registry\.yaml}' | sha256sum`;
it is NOT the pod annotation `checksum/registry-extras`, which hashes the whole rendered template);
`gatekeeper_service_registry_extras_rejected == 0` and every
`gatekeeper_service_registry_extras_refused_total{reason}` series is 0; the
`base_sha` values agree; a preshared mint succeeds (Service plus both pods' `service_token_minted`
logs); the e2e lane passes; `report-ailab-pin-drift` shows 0 torn.

**Post-flip probes, per replica IP (mandatory):**

1. A k8s mint with a complete request; assert `sub` and `azp` in the minted JWT.
2. Refusals: `svc-deepagent` with the harness Bearer gives 401 (no fallback); a second k8s entry
   claimed with the harness token gives 401 (precheck, generic message); a wrong-audience token gives
   401; no token gives 401; a `client_credentials` request from the harness token for a non-`svc-mcp`
   audience (e.g. `svc-workflow`) gives 403 `unauthorized_client`.
3. #2092's own checks: Ready, migrate completed, 401 rather than 302, the `@api` journeys, 0 torn.
4. **On any failure, darken the harness** (scale to 0 or `enabled: false`; deleting the pod does not
   revoke, because `Recreate` brings up a fresh valid identity).

**Reading TokenReview results** (probe output):

- A refusal carrying `status.error` is a **503**, uncached (the signed plan's rule; the brief 401
  deviation was withdrawn).
- `authenticated=true` together with `status.error`, or a malformed body, is also a **503**.
- The positive cache holds for at most 60 s, and `exp` is rechecked after the review.
- Concurrent same-token misses share one review (single-flight).

Every later roll of an active registry repeats the pre-flip and post-flip checks.

### Drills (plan, "Verification and drills")

- **Cold start:** extras absent, gatekeeper boots on the base, svc-harness gets 401.
- **Deletion and restore during a roll:** a pod that already loaded extras keeps them; restoring them
  needs a roll.
- **Rollback (not image-only):** darken the harness, revert the image AND the composite and extras
  config together, then verify a base preshared mint. Keep the `strive-pg-harness-dsn` Secret until no
  consumer uses it.
- **Token rotation:** for both the harness token and gatekeeper's own reviewer token, observe a change
  in token fingerprint (a hash, never the token) and drive an uncached mint past both caches.
- **Revocation:** owner-run, with a surviving probe holding the old pod's bearer. An old bearer is
  rejected after `deletionTimestamp` plus the API leeway plus at most 60 s of cache, never later than
  its `exp`; issued JWTs stop within 300 s of the last mint plus consumer skew; removing the entry
  gives 401 on each replica after the roll. Test both replica IPs through rejection.

Never run these from a worker that holds owner credentials (D1), and never print a token.
