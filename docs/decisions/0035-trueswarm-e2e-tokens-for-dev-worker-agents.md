# ADR 0035 — Dev-worker agents run live Trueswarm e2e with per-worker bearer tokens, not human logins

**Status:** ACCEPTED (2026-10-04), owner-directed. Asked for "the best solution" for a dev-worker running
Playwright e2e against the live Trueswarm and Trueswarm Admin. When offered Authelia machine identities
with TOTP, the owner asked for "a better approach that doesn't require my manual intervention", said a
bearer token was fine, and chose **operator** for the admin role, "for all dev workers, not only dw4".
Approved and directed to implement on 2026-10-04.
**Relates to:** ADR 0012 (Authelia SSO), ADR 0020/0021 (the per-worker OpenBao documents, `cred`, and
the sync-owned KV class this extends), ADR 0028 (slot enumerations, per-slot attribution and
revocation; its amendment on app-repo merge gates), `docs/runbooks/trueswarm-admin-ingress.md` (the
admin Access gate). Operator how-to: `docs/runbooks/trueswarm-e2e-tokens.md`.

## Context

- **The ask.** The Trueswarm agent on dev-worker-4 ran its Playwright suites locally, then stopped
  short of the live deployments. It wrote "the operator's existing decision still withholds browser
  credentials from this worker", and handed a read-only suite back to the operator. That decision is
  real and written down in cchifor/trueswarm-admin: "no canary account, TOTP seed or storageState is
  issued to this agent". Its live config (`web/playwright.live.config.ts`) therefore requires an
  operator-supplied `ADMIN_E2E_STORAGE_STATE`.
- **The two apps authenticate differently.**
  - `trueswarm.chifor.me` has no Cloudflare gate. It runs its own Authelia OIDC login (`one_factor`,
    implicit consent). A human session is a `credentials` row, accepted as the `trueswarm_session`
    cookie **or** as `Authorization: Bearer`.
  - `trueswarm-admin.chifor.me` is behind Cloudflare Access: dedicated IdP, named email, RFC 8176
    `mfa` claim, 1 h session. The origin verifies `Cf-Access-Jwt-Assertion` itself, then runs its own
    OIDC login with `trueswarm_admin_mfa` (`two_factor`). Admin sessions are bound to the Access
    subject and need an `administrators` row (viewer/moderator/operator/administrator).
  - Sensitive admin operations need `operations.reauthenticated_at`. Only the OIDC callback sets it,
    after a fresh MFA ID token.
- **Rejected first design: Authelia users `e2e-dw<N>` with password + TOTP.** It worked on paper but
  needed the operator for three things: a workstation script, a UI action per worker to add the admin
  rows, and an allow-list apply. It also needed a fence around six `one_factor` OIDC clients that would
  otherwise admit any new Authelia user. And it turned the admin's fresh-MFA step-up into something an
  agent completes by computing a TOTP code.

## Decision

### 1. One bearer token per live slot per app, minted in-cluster

`trueswarm-e2e-token-sync` (CronJob plus bootstrap Job, ns `openbao`,
`kubernetes/apps/trueswarm-e2e-tokens/`, its own Flux Kustomization) does the following for every
`LIVE_SLOTS` worker and each app.

**Mint.** Each token has the form `<prefix>.dev-worker-<N>.<32 random bytes, base64url>`. The prefix is
`tse2e` for trueswarm and `tsadmine2e` for admin, so one app's token is useless at the other.

**Publish the hashes.** It writes only the SHA-256 of each token to Secret `trueswarm-e2e-tokens`
(key `tokens.json`) in the app's namespace:

```json
{"version": 1, "principals": [{"name": "dev-worker-4",
  "tokens": [{"sha256": "…", "not_after": 1760000000}],
  "role": "operator", "access_client_ids": ["….access"]}]}
```

`role` and `access_client_ids` appear in the admin file only. The app mounts this Secret, optionally,
and re-reads it on every login.

**Publish the tokens.** After the Secrets, and after a kubelet-refresh pause when anything rotated, it
writes the tokens to the worker's own document `af/dev-workers/dev-worker-<N>`, fields
`{trueswarm,trueswarm_admin}_e2e_{token,valid_until}`. Agents read them with
`cred get "$(hostname -s)" trueswarm_e2e_token`. No ansible or bao-agent template change is needed.

**Rotation.** Tokens rotate when needed, not on every run. A token is valid for 14 days and is re-minted
once less than 7 days remain. The run also re-mints when OpenBao and the app disagree about the current
token, when anything is missing, or when `FORCE_ROTATE=1`. A rotated slot's old hash stays accepted for
one hour, so an in-flight run survives.

**Failure handling.** Both Secret writes are compare-and-swap on `resourceVersion`, so a concurrent run
exits before publishing anything. A Kubernetes Lease serialises whole runs (CronJob, bootstrap Job, hand-made Jobs) from the first read through OpenBao publication, and expires after 20 minutes, longer than the Job deadline, so a killed run frees it. Under it, a publication fence re-reads the Secrets and publishes only tokens that are still each app's current one. A run that dies between the two writes self-heals on the next run.
Nothing ever logs a token or a hash. The vault login is k8s-auth role `trueswarm-e2e-sync`, sharing the
`k8stoken-sync` policy for the same reason `platform-pg-sync` does.

The RBAC in each app namespace is `create` on secrets plus `get`/`update` on that one name. There is no
list or watch, so the sync can never read another Secret there.

### 2. Each app exchanges a token for an ordinary session: `POST /auth/e2e`

The endpoint is **off** unless the env var (`TRUESWARM_E2E_TOKENS_FILE` / `ADMIN_E2E_TOKENS_FILE`)
points at a file that exists. Every token failure returns the same 401. After login the agent drives
the normal UI with a normal session cookie, so what it tests is what humans use.

**trueswarm.** It upserts a `human` principal `e2e|dev-worker-<N>` (OIDC subjects are
`<https issuer>|<sub>`, so the two cannot collide) and issues a 1 h `session` credential. A moderator's
suspension of that principal sticks.

**trueswarm-admin.**
- **Access.** Machines pass Cloudflare Access with **one shared service token**: a `non_identity`
  policy at precedence 2, after the human MFA policy. Service-token Access JWTs carry an empty `sub` and
  a `common_name`. Only the Access path, never the OIDC ID-token path, may turn that into the identity
  `service:<client id>`. The human `login()` refuses it, so a service identity can never start the
  fresh-MFA flow.
- **Login checks.** `/auth/e2e` checks four things:
  - the identity is a service token;
  - the client ID is in the principal's `access_client_ids`, which must be non-empty (fail closed);
  - the role is at most operator; **administrator is refused in code, whatever the file says**;
  - an active human administrator already exists, so an e2e login can never pre-empt bootstrap.
- **The row.** `administrators` gets row `e2e:dev-worker-<N>`. The upsert only updates rows that are
  still active, so **a human deactivating it is a kill switch** the next e2e login cannot undo.
  `session()` denies an `e2e:` subject whose row was promoted to administrator.
- **Sessions.** Sessions last 1 h and are audited as `session.login` with `{"e2e": true, "mfa": false}`.

**Sensitive operations stay human-only, enforced, not by discipline.** `infra.*`, `releases.*`,
`credentials.revoke`, `connectors.*`, `jobs.*` and `configuration.*` need a fresh MFA that a machine
session cannot produce. Operator still covers simulations, plugin enable/disable, model removal and
every read.

### 3. The Cloudflare service token is shared and seeded through git

`kubernetes/infra/cloudflare/trueswarm-admin.tf` adds the token (`duration = "forever"`) and its policy
behind `enable_trueswarm_admin_e2e` (default false). The only sanctioned apply is the new helper mode
`scripts/trueswarm-admin-access.sh --apply-e2e-access`. Its plan guard allows only:
- creating the token and the policy;
- an in-place update of the existing application, with the audience equal to the private deployment's
  and precedences exactly `[1, 2]`.

The IdP, the human policy and the published DNS record must all be unchanged. The helper then puts
the client ID and secret into `devworker-seeds.sops.yaml` → `af/dev-workers/common`, and the client ID
into `ADMIN_ACCESS_CLIENT_IDS` in token-sync.yaml. The secret never touches argv or stdout. Both files
ship in an ordinary ailab PR.

**Shared, not per slot:** adding or retiring a worker never needs a Cloudflare change, and per-worker
identity, attribution and revocation already live in the app tokens. Revoking the service token is the
"all workers, now" switch.

### 4. The live suite mints its own state

trueswarm-admin's `web/playwright.live.config.ts` gains a `globalSetup`. When no operator state file is
given, it reads the tokens (from env, else from `cred`), calls both `/auth/e2e` endpoints, and writes
private storage-state files that are deleted afterwards. The admin context sends the `CF-Access-*`
headers to the admin origin only.

## Consequences

- **What workers can now do.** Every live worker can act as an **operator** in production Trueswarm
  Admin, minus sensitive operations, and as an ordinary user in Trueswarm. This is the ADR 0021 §5 /
  0028 §5 posture. The shared forge PAT these workers already hold could do more through the admin
  repo, which has no independent merge gate. What this ADR adds is narrower and attributable: a per-slot
  audit subject, per-slot revocation, and an ailab-reviewed role.
- **The human login chain is not exercised by agents.** Cloudflare's IdP redirect, Authelia passkey
  and app OIDC stay covered by the credential-free `scripts/qualify-access-edge.mjs` checks, and by
  humans.
- **Revocation, from fastest to broadest.**
  - Deactivate the e2e row in the admin UI (immediate, admin only).
  - Drop the slot from `LIVE_SLOTS` (effective once kubelet refreshes the Secret, about 1–2 min).
  - Run `FORCE_ROTATE=1` (every slot).
  - Revoke the service token (admin, every slot).
- **A sync failure is quiet but bounded.** Tokens outlive a week of missed runs. A failed run turns
  `KubeJobFailed` red; no new alert.
- **One more slot enumeration.** `token-sync.yaml`'s `LIVE_SLOTS` (two copies) joins
  `scripts/check-slot-enumerations.py`.
- **The admin repo's "no browser credential for agents" statements are superseded.** cchifor/trueswarm-admin
  docs now point here.

## Rejected

- **Authelia machine users with TOTP:** see Context. Three manual steps, a fence around six OIDC
  clients, and a TOTP-solving agent where a fresh-MFA gate used to mean a human.
- **Operator-minted storageState pushed to the workers:** it is the operator's own estate-wide
  identity, Access expires it within an hour, and it needs the operator for every run.
- **An in-cluster bypass of Access for the admin:** the origin rejects requests without an Access JWT
  by design. Weakening that for tests would weaken it for everyone.
- **Per-slot Cloudflare service tokens:** a Cloudflare apply for every slot change, buying nothing the
  app tokens do not already give.
- **Passkey-as-two-factors (`webauthn.experimental_enable_passkey_uv_two_factors`)** to make agent or
  human logins one step: Authelia documents it as unsupported and likely to fail startup in a future
  release.
