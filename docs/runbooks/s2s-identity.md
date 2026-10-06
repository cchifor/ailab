# S2S identities from projected ServiceAccount tokens: owner runbook

Owner-only steps for activating secretless service identities (the harness authenticating to
gatekeeper with its pod's projected token). The design, phases and rationale are in
[`plans/2026-10-06-s2s-identity-openbao-plan.md`](../../plans/2026-10-06-s2s-identity-openbao-plan.md);
this page is the checklist and does not repeat them. The DSN side (Phase 1) is in
[`infra-pg.md`](infra-pg.md), section "strive-pg (the PLATFORM's cluster ...)".

Nothing here is done by automation or by a dev worker. Do not activate (Phase 4) until D1, Phase 0
and D2/D3 below are complete and recorded.

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
  workers. It was repointed to `dev-worker-bot`. Follow-up: manage it with ansible.
- Revoked the `chifor` tokens with ids 2, 4, 207 and 276 (by deleting the DB rows). The old tokens
  were proven to return 401.
- Workers 5 and 6 (`.12`/`.13`) are not in the converge inventory and presented changed SSH host
  keys. They were not touched. Follow-up: confirm what those VMs are now.
- Known effects, both by design: the owner-ack `approve-pin` flow on platform can no longer be
  posted as the owner by a worker, and "merge as chifor from a worker" no longer works. The owner
  acknowledges and merges in person, from a non-shared login.

### Rotation / re-do procedure

1. As Gitea site admin, create a NON-admin user (`dev-worker-bot`) and put it in the org team
   `automation` (write on the repos the workers need; never admin, never owner).
2. Mint that user's PAT(s) with the scopes the workers use (`gitea admin user generate-access-token
   --username dev-worker-bot ... --raw`, run in the Gitea pod). Keep the value out of the terminal
   history and out of git.
3. Replace the seed. Either edit the SOPS seed (owner-only; age key in
   `kubernetes/infra/_out/age.agekey`; substitute only the token values, see the snippet in
   `openbao-dev-workers.md`), or patch the live path:
   `bao kv patch -mount=af dev-workers/common gitea_pat=-` (value on stdin; repeat for each of the
   three fields `gitea_pat`, `gitea_package_pat` and `gitea_repo_pat`, one value each). Do the SOPS edit too, or
   the next seed run restores the old value. Also re-encrypt `ansible/secrets/dev-worker.sops.yaml`.
4. Ensure `dev_worker_gitea_user: dev-worker-bot` (`ansible/roles/dev_worker/defaults/main.yml`, or
   group/host vars) and converge: reviewers first, then workers (`just dev-workers`, see
   [`dev-workers.md`](dev-workers.md)). Run it twice; the second run should report near-zero `changed`.
5. Verify on a worker, per the smoke test in `openbao-dev-workers.md` section (g):
   `cred get common gitea_pat | wc -c` (length only), `ls -l ~/.git-credentials` (0600), and
   `git ls-remote https://git.chifor.me/cchifor/ailab.git HEAD`. Then confirm the identity:
   `cred exec common gitea_pat T -- sh -c 'curl -s -H "Authorization: token $T" https://git.chifor.me/api/v1/user'`
   must show `dev-worker-bot` with `is_admin: false` (do not print the token).
6. **Revoke the `chifor` token(s) that the workers held** (Gitea UI as `chifor`: Settings,
   Applications, or delete the DB rows) and any other owner token copied to workers. Confirm the old
   token now gets 401.

## Phase 0: the owner gate on platform `main`

Prerequisite: the platform PR adding the authority guard (`.github/workflows/s2s-authority-guard.yml`
plus `scripts/ci/check-s2s-authority.py`) is merged. The calls below need a **repo-admin** token for
`cchifor/platform` (the owner's, held outside the workers). Read the current rule first; Gitea
applies only the FIRST matching rule, so patch the effective one. Keep the saved copy in the
gitignored `_out/` directory, not the working directory:

```sh
G=https://git.chifor.me/api/v1/repos/cchifor/platform
curl -s -H "Authorization: token $OWNER_TOKEN" $G/branch_protections            # find the effective rule for main
curl -s -H "Authorization: token $OWNER_TOKEN" $G/branch_protections/main > kubernetes/infra/_out/main-protection-before.json
```

Patch `protected_file_patterns` and append `S2S Authority Guard / guard*` to the EXISTING
`status_check_contexts` (keep every current entry). Gitea matches with gobwas/glob, `.` and `/` as
separators, and brace support is unverified, so every brace is EXPANDED into separate patterns. The
API field is one `;`-separated string; the list, one pattern per line, is the heredoc below (written to the gitignored `_out/`):

```sh
cat > kubernetes/infra/_out/patterns.txt <<'EOF'
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
EOF
```

Two patterns go beyond the plan: `deploy/gitops/flux/clusters/ailab/**` (a single listed Flux file
could otherwise be sidestepped by another file in that directory carrying arbitrary kinds) and
`deploy/secrets/ailab/**` (a plaintext Secret there could shadow the SOPS `gatekeeper-secrets`).
Expect about 19 owner reviews a month.

```sh
PY=python3   # python3 on Linux hosts; use PY=python in Git Bash
B=kubernetes/infra/_out/main-protection-before.json
PATTERNS=$(paste -sd';' kubernetes/infra/_out/patterns.txt)
BODY=$($PY - "$PATTERNS" "$B" <<'PYEOF'
import json, sys
before = json.load(open(sys.argv[2], encoding='utf-8'))
ctx = list(before.get('status_check_contexts') or [])
if 'S2S Authority Guard / guard*' not in ctx:
    ctx.append('S2S Authority Guard / guard*')
print(json.dumps({'protected_file_patterns': sys.argv[1],
                  'enable_status_check': True,
                  'status_check_contexts': ctx}))
PYEOF
)
curl -s -X PATCH -H "Authorization: token $OWNER_TOKEN" -H "Content-Type: application/json" \
  $G/branch_protections/main -d "$BODY"
```

Re-read the rule and diff it against `kubernetes/infra/_out/main-protection-before.json` (only those
two fields may change).

### Gate tests (Phase 0 step 5)

Gitea 1.26 checks merge preconditions in this order: status checks, approvals, rejected reviews,
official review requests, outdated branch, protected files. A merge attempt alone therefore cannot
isolate the protected-files check, so the test is:

1. Open a throwaway PR (authored by `dev-worker-bot`, label `no-automerge`) touching one file per
   pattern plus one unprotected control file (`infra/gatekeeper/src/app/gatekeeper/routes.py`).
2. Read `pull_request.changed_protected_files` from the Gitea DB. It must list every touched
   protected file and NOT the control.
3. Show that a direct push to `main` by `dev-worker-bot` is refused (push whitelist: `chifor`,
   `gitea_admin`) and that `force_merge` by the non-admin bot is refused.
4. Rely on the real Phase 2a/3 PRs for the end-to-end "Changed protected files" refusal and the
   owner merge. `block_admin_merge_override` stays false, which is the owner path.
5. A red guard (introduce a forbidden setting, e.g. `gatekeeper.serviceAuth.composite.enabled: true`
   in `ailab.yaml`) blocks the merge, and the guard check is reported on that PR.
6. An unrelated PR (e.g. a README edit) still merges normally through the existing automation.

If the patterns do not hold, apply the plan's Phase 0 step 7 fallback (`required_approvals >= 1` on
the effective `main` rule, keeping existing protections) and repeat. Do NOT proceed untested.

### Recording the results

Write a dated record in the S2S ADR (the plan's Phase 0 step 6): the Gitea version
(`curl -s https://git.chifor.me/api/v1/version`), the rule JSON after the change, a table of
pattern x route -> blocked/allowed, and the red-guard, unrelated-PR and owner-path outcomes. Author
or confirm it through a non-shared identity (next section).

## D2 and D3: owner acceptance records

The owner must explicitly accept, in the ADR:

- **D2, residual bypasses** that stay bot-approvable after Phase 0: unprotected gatekeeper modules
  (`routes.py`, `helpers.py`, the rest of `infra/gatekeeper/**`), `ailab.yaml` digest pins and `ci.yml`,
  `build.yml` (including `/release-build`), `scripts/ci/reviewed-release-build.py`,
  `protect-ailab-images.yml`, provenance of `ailab`-family tags, and the ailab Flux cluster-admin
  path into `strive-ailab`. Otherwise B does not activate.

  **Further residuals found at execution (2026-10-06).** Other automation still holds `chifor`
  (site-admin) tokens. Each is an owner-gate bypass path until migrated to a bot identity:
  - `ci-rerun-watchdog` (Secret `ci-rerun-watchdog/ci-rerun-watchdog-gitea`; write:repository and
    read:admin);
  - `cloud-power` (Secret `cloud-power/cloud-power-gitea`; write:organization);
  - `agentforge-ui` (scope `all`; location outside the cluster/OpenBao, unmapped);
  - `reviewbot-hook-check` (read:organization) and `forge-actions-release` (write:package);
  - about 20 stale per-session `chifor` tokens (e.g. `pr487-*`, `claude-session-*`, `cp-all` with
    scope `all`).

  Recommended: migrate these to bot identities and revoke the stale tokens.
- **D3, svc-harness authority**: the cross-tenant `client_credentials` authority (F1) and the
  plain-HTTP replay window on the harness-to-gatekeeper hop remain. Either accept deferring a
  per-entry `allowed_grant_types` restriction and transport encryption, or make grant-type
  restriction a pre-flip item.

**How to record, through a non-shared channel.** The acceptance must not be authored from the shared
`chifor` login that workers could have used. Either (a) commit or approve the ADR text as a distinct
non-shared owner identity (own device, own credentials, present on no worker), or (b) give a direct
confirmation outside the shared login (for example a signed commit, or a message to the reviewers
over a separate channel) and reference it in the ADR. Anything authored through `dev-worker-bot` or a
worker's credentials does not count.

## Phase 4: activation checks

Run after the flip (#2092) lands. Each check targets each gatekeeper pod IP separately (2 replicas),
not just the Service.

**Pre-flip roll acceptance, per replica:** the effective `extras_sha` equals the rendered hash; the
`base_sha` values agree; a preshared mint succeeds (Service plus both pods' `service_token_minted`
logs); the e2e lane passes; `report-ailab-pin-drift` shows 0 torn.

**Post-flip probes, per replica IP (mandatory):**

1. A k8s mint with a complete request; assert `sub` and `azp` in the minted JWT.
2. Refusals: `svc-deepagent` with the harness Bearer gives 401 (no fallback); a second k8s entry
   claimed with the harness token gives 401 (precheck, generic message); a wrong-audience token gives
   401; no token gives 401.
3. #2092's own checks: Ready, migrate completed, 401 rather than 302, the `@api` journeys, 0 torn.
4. **On any failure, darken the harness** (scale to 0 or `enabled: false`; deleting the pod does not
   revoke, because `Recreate` brings up a fresh valid identity).

**Reading TokenReview results** (probe output):

- A refused token with `status.error` is a generic **401**, not cached. This deviates from the
  plan's 503 rule and follows Kubernetes' own webhook client.
- `authenticated=true` together with `status.error`, or a malformed body, is a **503**.
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
