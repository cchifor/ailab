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
   `git ls-remote https://git.chifor.me/cchifor/ailab.git HEAD`. Then confirm the identity of ALL
   THREE tokens (each was minted with `read:user`):
   `for f in gitea_pat gitea_repo_pat gitea_package_pat; do printf '%s ' $f; cred exec common $f T -- sh -c 'curl -s -H "Authorization: token $T" https://git.chifor.me/api/v1/user' | python3 -c 'import sys,json; u=json.load(sys.stdin); print(u["login"], "is_admin=%s" % u["is_admin"])'; done`
   Every line must show `dev-worker-bot is_admin=False` (the token itself is never printed).
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

Use it to restore or extend the rule. If its first lines print `not an ailab checkout` or `STOP`,
nothing after them may run: `cd` into an ailab checkout and start again. It needs a **repo-admin** token for `cchifor/platform` (the
owner's, held outside the workers). Gitea applies only the FIRST matching rule, so patch the
effective one. The PATCH MERGES with the live rule: it unions the live `protected_file_patterns` and
`status_check_contexts` with the documented ones and never replaces them.

```sh
G=https://git.chifor.me/api/v1/repos/cchifor/platform
# Snapshots and the pattern list live in the MAIN checkout's gitignored kubernetes/infra/_out/ (a git
# worktree has no _out/), never in the current directory. Run this from inside any AILAB checkout:
# from a platform checkout the same path would not be gitignored, so refuse anything else.
case "$(git remote get-url origin 2>/dev/null)" in
  *cchifor/ailab*) OUT="$(cd "$(git rev-parse --git-common-dir)/.." && pwd -P)/kubernetes/infra/_out" ;;
  *) echo "not an ailab checkout: cd into one first" >&2; OUT= ;;
esac
[ -n "$OUT" ] && git -C "${OUT%/kubernetes/infra/_out}" check-ignore -q kubernetes/infra/_out/x \
  && mkdir -p "$OUT" || { echo "STOP: _out/ is not a gitignored ailab path; do not run the rest" >&2; OUT=; }
# Every later use of OUT goes through ${OUT:?}: with OUT cleared above, each command aborts instead of
# writing to /, and the PATCH is skipped because BODY stays empty.
B="${OUT:?refused: not a gitignored ailab checkout}/main-protection-before.json"
curl -s -H "Authorization: token $OWNER_TOKEN" $G/branch_protections            # find the effective rule for main
RULE=<rule_name of the effective rule from the output above>   # often `main`, but use what the output shows
curl -s -H "Authorization: token $OWNER_TOKEN" $G/branch_protections/$RULE > "${B:?refused}"
cat > "${OUT:?refused: not a gitignored ailab checkout}/patterns.txt" <<'EOF'
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
BODY=$($PY - "${OUT:?refused}/patterns.txt" "${B:?refused}" <<'PYEOF'
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
[ -n "$BODY" ] && curl -s -X PATCH -H "Authorization: token $OWNER_TOKEN" -H "Content-Type: application/json" \
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

Run these after the flip (#2092) lands. The owner runs them from a machine that is not a dev worker, with
the `admin@ai` context. Every gatekeeper check runs against each replica on its own: the script
execs into container `gatekeeper` of each pod and drives `http://127.0.0.1:5000` with the pod's
own python. The gatekeeper NetworkPolicy admits only Traefik and the `allowedClients`, so a
request to a pod IP from anywhere else would test the NetworkPolicy, not gatekeeper.

**Pre-flip / roll acceptance, per replica:**

- The effective `extras_sha` equals the rendered hash. `extras_sha` is the SHA-256 of the exact
  ConfigMap data bytes:
  `kubectl -n strive-ailab get cm gatekeeper-registry-extras -o jsonpath='{.data.registry\.yaml}' | sha256sum`.
  It is NOT the pod annotation `checksum/registry-extras`, which hashes the whole rendered template.
- `gatekeeper_service_registry_extras_rejected == 0`, and every
  `gatekeeper_service_registry_extras_refused_total{reason}` series is 0.
- The `base_sha` values agree.
- A preshared mint succeeds (Service plus both pods' `service_token_minted` logs).
- The e2e lane passes.
- `report-ailab-pin-drift` shows 0 torn.

Before the flip, check the first three by hand. The probes script below cannot run yet: it mints
the harness's token, and a dark harness has no ServiceAccount.

```sh
K="kubectl --context admin@ai -n strive-ailab"
$K get cm gatekeeper-registry-extras -o jsonpath='{.data.registry\.yaml}' | sha256sum
for p in $($K get pod -l app.kubernetes.io/name=gatekeeper -o name); do
  echo "$p"
  $K exec "$p" -c gatekeeper -- python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:5000/metrics',timeout=5).read().decode())" \
    | grep -E '^gatekeeper_service_registry_(info|extras_rejected|extras_refused_total)'
done
```

After the flip, and on every later roll, the probes script checks the same three items per replica
from the pod's own `/metrics`: its `registry` check and its "Registry agreement" section. The last
three items stay manual.

### The probes: `scripts/s2s/phase4-probes.sh` (mandatory after the flip)

```sh
scripts/s2s/phase4-probes.sh --dry-run   # the plan; no cluster or network call
scripts/s2s/phase4-probes.sh             # exit 0 required; anything else: darken (below)
```

**Tokens.** The script makes three TokenRequests (`kubectl create token`, 10 minutes, no stored
object):

- SA `harness` for audience `strive-gatekeeper`;
- SA `harness` for `not-strive-gatekeeper`;
- SA `default` for `strive-gatekeeper`.

They stay in shell variables and reach the pod on stdin only. The script never prints a token, and
it decodes a minted JWT only for the claims it asserts. Gatekeeper logs each probe mint as
`service_token_minted grant=client_credentials client_id=svc-harness target=svc-mcp tenant=phase4-probe`.

**Per replica.** The program is `scripts/s2s/phase4_probe.py`; its expectations cite gatekeeper at
platform `ac123f047` line by line. Every request has the harness's own `client_credentials` shape
(`s2s.ts:297-301,346-348`): the Bearer, and no `client_secret`.

| Check | Request | Expected |
|---|---|---|
| `registry` | `GET /metrics` | The loaded `extras_sha` equals the mounted file; `extras_rejected` is 0; every `extras_refused_total` is 0. |
| `d3-policy` | the mounted extras | `client_credentials` is open for `svc-mcp` only; `k8s_subject` is `system:serviceaccount:strive-ailab:harness`. |
| `k8s-mint[svc-mcp]` | harness token as `svc-harness`, audience `svc-mcp`, tenant `phase4-probe` | 200, with: `sub` = `azp` = `svc-harness`; `platform_target_service` = `svc-mcp`; the tenant claim; no `act`; `exp - iat` ≤ 300; the scopes the registry grants. The replica must also count a positive-cache miss and an `authenticated` TokenReview, which proves a fresh review on THAT replica. |
| `d3-refused[<aud>]` | the same request, for each token_exchange-only audience | 403 `unauthorized_client`, `client 'svc-harness' not allowed grant 'client_credentials' for audience '<aud>'`. |
| `refuse-preshared[svc-deepagent]` | harness Bearer, no secret | 401 (no fallback). |
| `refuse-unregistered[svc-phase4-unregistered]` | harness Bearer, an unregistered client_id | 401. |
| `refuse-second-k8s[<id>]` | harness Bearer, another k8s entry (only when the extras have one) | 401 (the `sub` precheck). |
| `refuse-other-sa` | the `default` SA's token as `svc-harness` | 401 (the `sub` precheck). |
| `refuse-wrong-audience` | the harness SA's token for the wrong audience | 401 (the `aud` precheck). |
| `refuse-no-token` | no Authorization header | 401. |
| `refusals-identical` | | Every refusal has the same body: `{"error":"invalid_client","error_description":"invalid client credentials"}`. |

**Across replicas.** Each loaded `extras_sha` equals the sha256 of ConfigMap
`gatekeeper-registry-extras` `data.registry.yaml`, and `base_sha` is the same on every replica.

**#2092's checks that the script runs:**

- HelmRelease `strive` is Ready.
- Exactly one harness pod, Running and Ready, as SA `harness`.
- Its init container `migrate` is Completed (exit 0).
- `HARNESS_CLIENT_TOKEN_FILE` is set, and no `HARNESS_CLIENT_SECRET` is.
- SA and IngressRoute `harness` are present.
- An unauthenticated `GET https://strive.place/api/harness/admin/v1/chat` answers 401, not a login
  302.

**The second k8s entry.** The plan's refusal "a second k8s entry claimed with the harness token"
needs a second `auth_method: k8s` entry, and today `svc-harness` is the only one. Two checks stand
in for it:

- `refuse-unregistered`: the harness token cannot claim an identity outside the registry.
- `refuse-other-sa`: another ServiceAccount's valid `strive-gatekeeper` token cannot claim
  `svc-harness`. This is the same `sub` precheck (`tokenreview_verifier.py:177-178`), from the
  other side.

When the extras gain a second k8s entry, `refuse-second-k8s` runs as well, automatically.

**Still manual (#2092).** The script prints a reminder for both:

- The `@api` assistant journeys (`tests/e2e/journeys/assistant/api-*.spec.ts`) pass live against
  `https://strive.place` with the persona, one worker.
- `report-ailab-pin-drift` shows 0 torn. From a platform checkout at main:
  `uv run --quiet python3 scripts/ci/report-ailab-pin-drift.py --count-app-templates --fail-on-torn --fail-on-incoherent`.

**On any failure, darken the harness.** The script prints `DARKEN THE HARNESS` and these commands.
First the freeze, the Kustomization first, then the HelmRelease, then the scale:

```sh
kubectl --context admin@ai -n flux-system patch kustomization platform-app --type=merge -p '{"spec":{"suspend":true}}'
kubectl --context admin@ai -n strive-ailab patch helmrelease strive --type=merge -p '{"spec":{"suspend":true}}'
kubectl --context admin@ai -n strive-ailab scale deployment/harness --replicas=0
```

Make it durable with a platform PR setting `harness.enabled: false` in
`deploy/helm/values/providers/ailab.yaml` (revert #2092). **Only after that PR has merged**, resume.
This is a separate block, so pasting the freeze never un-freezes.

Resume the Kustomization first, and wait for it to reconcile. Its re-apply of the HelmRelease from
git clears the hand-set suspend, so Helm upgrades the new spec. Resume the HelmRelease only if it is
still suspended after that. Resuming the HelmRelease first can upgrade a stale spec and, mid-rollback,
bring the harness back before the reverted `helmrelease.yaml` is applied.

```sh
kubectl --context admin@ai -n flux-system patch kustomization platform-app --type=merge -p '{"spec":{"suspend":false}}'
G=$(kubectl --context admin@ai -n flux-system get kustomization platform-app -o jsonpath='{.metadata.generation}')
kubectl --context admin@ai -n flux-system wait kustomization/platform-app --for=jsonpath='{.status.observedGeneration}'="$G" --timeout=10m
[ "$(kubectl --context admin@ai -n strive-ailab get helmrelease strive -o jsonpath='{.spec.suspend}')" = true ] && kubectl --context admin@ai -n strive-ailab patch helmrelease strive --type=merge -p '{"spec":{"suspend":false}}'
```

- **Suspend both, the Kustomization first.** The Flux Kustomization `flux-system/platform-app`
  (path `deploy/gitops/flux/clusters/ailab/app`) re-applies the HelmRelease from git. It clears a
  hand-set HelmRelease suspend within one reconcile: observed 2026-10-06, suspended at 17:21Z and
  cleared at 17:22:39Z. The HelmRelease suspend is still needed: the HelmRelease uses
  `reconcileStrategy: Revision`, so every platform commit upgrades the release, and Helm's
  three-way merge puts `replicas: 1` back.
- **What freezing costs.** It freezes every strive-ailab deploy, and everything else that
  `platform-app` applies, until both are resumed.
- **Deleting the harness pod does not revoke.** The Deployment is `Recreate` and brings up a fresh,
  valid identity.
- **Scaling to 0 does.** The projected token is bound to the pod; drill 4 measures how fast.

**Every later roll of an active registry** repeats the pre-flip acceptance and
`scripts/s2s/phase4-probes.sh --gatekeeper-only`.

**Reading TokenReview results** (probe output):

- A refusal carrying `status.error` is a **503**, uncached (the signed plan's rule; the brief 401
  deviation was withdrawn).
- `authenticated=true` together with `status.error`, or a malformed body, is also a **503**.
- The positive cache holds for at most 60 s, and `exp` is rechecked after the review.
- Concurrent same-token misses share one review (single-flight).

### Drills (plan, "Verification and drills")

The owner runs these, with `K="kubectl --context admin@ai -n strive-ailab"`.

- Run drills 1, 3 and 4 after the probes pass, in any order. Drill 2 deactivates the harness.
- Drills 1, 2 and 4 take the harness out of service (the freeze block of the darken commands). Drill 1
  does it first: otherwise the harness would get 401s from the replica that refuses it.
- **Restore the harness afterwards** (only once the drill is over):
  1. Resume the Kustomization first, and wait for it to reconcile.
  2. Resume the HelmRelease only if it is still suspended.
  3. Scale back.
  4. Run the full probes.

```sh
kubectl --context admin@ai -n flux-system patch kustomization platform-app --type=merge -p '{"spec":{"suspend":false}}'
G=$(kubectl --context admin@ai -n flux-system get kustomization platform-app -o jsonpath='{.metadata.generation}')
kubectl --context admin@ai -n flux-system wait kustomization/platform-app --for=jsonpath='{.status.observedGeneration}'="$G" --timeout=10m
[ "$(kubectl --context admin@ai -n strive-ailab get helmrelease strive -o jsonpath='{.spec.suspend}')" = true ] && kubectl --context admin@ai -n strive-ailab patch helmrelease strive --type=merge -p '{"spec":{"suspend":false}}'
$K scale deployment/harness --replicas=1
$K rollout status deployment/harness
scripts/s2s/phase4-probes.sh
```

**1. Cold start, and deletion and restore during a roll.** Expected:

- A gatekeeper that boots without extras serves only the base, and `svc-harness` gets 401.
- A pod that already loaded the extras keeps them.
- Restoring them needs a roll.

```sh
K="kubectl --context admin@ai -n strive-ailab"
# Freeze (the Kustomization first: it re-applies the HelmRelease and clears a hand-set suspend).
# This also keeps Helm off the ConfigMap mid-drill.
kubectl --context admin@ai -n flux-system patch kustomization platform-app --type=merge -p '{"spec":{"suspend":true}}'
$K patch helmrelease strive --type=merge -p '{"spec":{"suspend":true}}'
$K scale deployment/harness --replicas=0
scripts/s2s/phase4-probes.sh --gatekeeper-only                            # baseline: PASS on both replicas
A=$($K get pod -l app.kubernetes.io/name=gatekeeper -o jsonpath='{.items[0].metadata.name}')
B=$($K get pod -l app.kubernetes.io/name=gatekeeper -o jsonpath='{.items[1].metadata.name}')
# Delete the extras (non-secret: grants only), but ONLY once a copy to restore from is saved. The copy
# goes to the MAIN checkout's gitignored kubernetes/infra/_out/ (a worktree has none), so run this
# from an ailab checkout. On "STOP", nothing was deleted: do not go on.
OUT="$(cd "$(git rev-parse --git-common-dir)/.." && pwd -P)/kubernetes/infra/_out"
git -C "${OUT%/kubernetes/infra/_out}" check-ignore -q kubernetes/infra/_out/x && mkdir -p "$OUT" || OUT=
SAVE="${OUT:-/nonexistent-ailab-out}/gatekeeper-registry-extras-drill.yaml"
$K get configmap gatekeeper-registry-extras -o yaml > "$SAVE" && grep -q '^kind: ConfigMap' "$SAVE" \
  && $K delete configmap gatekeeper-registry-extras || echo "STOP: no verified copy at $SAVE; nothing deleted" >&2
scripts/s2s/phase4-probes.sh --gatekeeper-only --no-registry-check        # PASS: both pods kept the extras they loaded
# Cold start: restart A without the ConfigMap (the mount is optional: true).
$K delete pod "$A"
$K rollout status deployment/gatekeeper --timeout=300s
C=$($K get pod -l app.kubernetes.io/name=gatekeeper -o name | sed 's|^pod/||' | grep -vx -e "$A" -e "$B")
scripts/s2s/phase4-probes.sh --replica "$C" --expect-refused              # PASS: extras_sha empty, svc-harness 401
scripts/s2s/phase4-probes.sh --replica "$B" --gatekeeper-only --no-registry-check   # PASS: B still serves svc-harness
# Restore the ConfigMap. There is no reload: C keeps refusing until it is rolled.
sed -e '/^  resourceVersion:/d' -e '/^  uid:/d' -e '/^  creationTimestamp:/d' "$SAVE" | $K create -f -
scripts/s2s/phase4-probes.sh --replica "$C" --expect-refused              # PASS: still refused
$K rollout restart deployment/gatekeeper
$K rollout status deployment/gatekeeper --timeout=300s
scripts/s2s/phase4-probes.sh --gatekeeper-only                            # PASS on both: extras_sha = the ConfigMap again
```

Then restore the harness (above).

**2. Rollback (not image-only).** This is a real deactivation: it undoes Phases 3 and 4, so run it
only when the owner chooses to. One platform PR, merged by the owner (it touches the protected Flux
file):

- `deploy/helm/values/providers/ailab.yaml`:
  - `harness.enabled: false`;
  - the gatekeeper `image.digest` back to the pre-Phase-3 pin that the Phase 3 comment records:
    `sha256:45abbd52fd3ea9985372056490078cec54b0a2964ee88ba8718fe402086aee79` (`sha-19363455156f`).
- `deploy/gitops/flux/clusters/ailab/app/helmrelease.yaml`: drop the
  `deploy/helm/values/providers/ailab-s2s-registry.yaml` `valuesFiles` entry. The composite backend
  and the extras go with it. The old image has no composite backend, so the image and the config
  revert together.

Freeze, then merge the rollback PR:

```sh
kubectl --context admin@ai -n flux-system patch kustomization platform-app --type=merge -p '{"spec":{"suspend":true}}'
$K patch helmrelease strive --type=merge -p '{"spec":{"suspend":true}}'
$K scale deployment/harness --replicas=0
```

**Only after the rollback PR has merged**, resume the Kustomization first. It applies the reverted
`helmrelease.yaml` and, with it, clears the HelmRelease suspend, so Helm upgrades the rolled-back spec
in one step. Resuming the HelmRelease first would upgrade the stale pre-rollback spec and bring the
harness back. Then verify:

```sh
kubectl --context admin@ai -n flux-system patch kustomization platform-app --type=merge -p '{"spec":{"suspend":false}}'
G=$(kubectl --context admin@ai -n flux-system get kustomization platform-app -o jsonpath='{.metadata.generation}')
kubectl --context admin@ai -n flux-system wait kustomization/platform-app --for=jsonpath='{.status.observedGeneration}'="$G" --timeout=10m
[ "$(kubectl --context admin@ai -n strive-ailab get helmrelease strive -o jsonpath='{.spec.suspend}')" = true ] && kubectl --context admin@ai -n strive-ailab patch helmrelease strive --type=merge -p '{"spec":{"suspend":false}}'
$K rollout status deployment/gatekeeper --timeout=600s
scripts/s2s/phase4-probes.sh --expect-refused
kubectl --context admin@ai get clusterrole,clusterrolebinding strive-ailab-gatekeeper-tokenreview
for p in $($K get pod -l app.kubernetes.io/name=gatekeeper -o name); do
  echo "$p $($K logs "$p" -c gatekeeper --since=30m | grep -c service_token_minted)"
done
$K get externalsecret strive-pg-harness-dsn
```

Expected:

- `--expect-refused` PASSes. SA `harness` is gone, so its cases are skipped; the other-SA and
  no-token requests get 401 on the preshared backend.
- The ClusterRole and ClusterRoleBinding are NotFound: the TokenReview RBAC went with composite.
- The `service_token_minted` count is above 0 on each pod: base preshared mints work. Run the e2e
  lane if traffic is quiet.
- The DSN ExternalSecret stays SecretSynced. Do not delete it here; it stays until no consumer uses
  it.

Re-activation is Phases 3 and 4 again: revert the rollback PR, run the pre-flip acceptance, then the
flip and the probes.

**3. Token rotation.** Both tokens rotate in place. The kubelet replaces a projected token after
about 80% of its lifetime, so about 48 minutes for a one-hour token. For each token, observe a
fingerprint change (12 hex of its sha256, never the token), then drive an uncached mint past both
caches. A new token digest misses the positive cache (keyed by `sha256(token)`) and the negative
cache (keyed by `(sha256(token), client_id)`) by construction.

The harness token:

```sh
hfp() { $K exec deploy/harness -c harness -- node -e 'const c=require("crypto"),f=require("fs");const t=f.readFileSync("/var/run/secrets/tokens/gatekeeper/token","utf8").trim();const p=JSON.parse(Buffer.from(t.split(".")[1],"base64url"));console.log(c.createHash("sha256").update(t).digest("hex").slice(0,12),"iat="+p.iat,"exp="+p.exp)'; }
gkm() { for p in $($K get pod -l app.kubernetes.io/name=gatekeeper -o name); do echo "$p"; $K exec "$p" -c gatekeeper -- python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:5000/metrics',timeout=5).read().decode())" | grep -E '^gatekeeper_tokenreview_(total|cache_total)\{'; done; }
hfp; gkm   # note the fingerprint and the counters
# repeat hfp every 5 minutes until the fingerprint changes (about 48 min after its iat), then:
gkm        # counters before
# make the harness call gatekeeper: run one @api journey (api-greet) against https://strive.place
gkm        # after
```

The harness token passes when:

- the fingerprint changed;
- the journey passed;
- after the change, a replica counted a positive-cache miss and an `authenticated` review.

Gatekeeper's own reviewer token. The probes print it per replica as `OWNTOKEN fp=… iat=… exp=…`:

```sh
scripts/s2s/phase4-probes.sh --gatekeeper-only | grep -E '^== Replica|OWNTOKEN|k8s-mint'   # note each fp
# re-run until a replica's fp changes (about 48 min after its iat), then once more in full:
scripts/s2s/phase4-probes.sh --gatekeeper-only
```

It passes when that replica's `OWNTOKEN fp` changed and its `k8s-mint` PASSes:

- `k8s-mint` counts a fresh TokenReview on that replica. Each run mints a new token, so it misses
  both caches, and gatekeeper re-reads its own token for every uncached review (GC1).
- **Caveat:** the API server extends the lifetime of automount tokens (on by default), so the
  previous own token stays valid after the rotation. This drill shows that rotation is tolerated;
  it cannot show that the old token was refused.

**4. Revocation, with a surviving probe.**

```sh
# terminal 1: holds a token bound to the CURRENT harness pod (a TokenRequest with
# --bound-object-kind Pod, which dies with the pod as its projected token does), checks that
# both replicas accept it, then watches for the pod to go
scripts/s2s/phase4-probes.sh --revocation-drill
# terminal 2, when terminal 1 prints "Revoke now" (the Kustomization first, then the HelmRelease):
kubectl --context admin@ai -n flux-system patch kustomization platform-app --type=merge -p '{"spec":{"suspend":true}}'
$K patch helmrelease strive --type=merge -p '{"spec":{"suspend":true}}'
$K scale deployment/harness --replicas=0
```

Revoke promptly: the pod must be gone at least 30 s before the held token expires
(`PHASE4_REMOVAL_MARGIN_SECONDS`).

Terminal 1 then polls each replica every 5 s with the held bearer until the token's own `exp`. The
script decodes `exp` from the token without printing it.

- **The held token's `--duration`** is `PHASE4_HELD_TOKEN_SECONDS` (default 600 s: the drill runs
  about 10 minutes after the mint). The API server refuses less than 600 s.
- **The cap.** The duration must be at or below `PHASE4_MAX_WATCH_SECONDS` (default 900 s), the
  longest the drill watches. Otherwise the script refuses to start (exit 2).
- **INCOMPLETE.** If the API server still issues a token that outlives the cap, the drill stops
  before any probe and before the "Revoke now" prompt. It prints `INCOMPLETE: token lifetime not
  fully observed (exp in Ns > cap Ms)` and exits **3**. That is never a pass: a partial watch says
  nothing about the rest of the lifetime. Re-run with `PHASE4_MAX_WATCH_SECONDS` raised to cover the
  issued lifetime; the message gives a value.

It passes when every replica:

- refuses the bearer within 90 s of the pod's removal (`PHASE4_REVOCATION_BOUND`: the 60 s
  positive cache plus polling);
- never accepts it again before the token's expiry: a 200 after the replica's first refusal fails
  the drill at once;
- refuses it in each of the last two rounds before the expiry. A round counts only if every
  replica refused in it; any other round resets the count.

An attempt with no HTTP answer (a transport or exec failure, `MINT 0`) is neither a refusal nor an
acceptance. Three in a row on one replica fail the drill as "could not observe"
(`PHASE4_MAX_NO_ANSWER`).

The refusal is a **503** `temporarily_unavailable`, not a 401: TokenReview reports the missing pod
in `status.error`, and gatekeeper never reads that as a verdict (GC4). Then restore the harness.

The other two revocation bounds need no drill of their own:

- **Minted JWTs.** Every one carries `exp - iat` ≤ 300, as `k8s-mint` asserts. With weld-auth's
  30 s skew, a JWT minted just before the revocation is dead within 330 s.
- **"Removing the entry gives 401 on each replica after the roll."** This is drill 1's
  `--expect-refused` on the rolled replica.

Never run these from a worker that holds owner credentials (D1), and never print a token.
