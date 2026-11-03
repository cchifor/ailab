# Dev-worker read-only access to the CI-artifact store


## Context

A codex agent on dev-worker-4 needs one CI debug bundle:
`s3://gha-artifacts/119597-1/docker-logs-journey-3.tar.gz` (platform run 119597, job 680776, journey
shard 3/4, Sentinel HTTP 503). The job log confirms it was uploaded (`860K`, 16:03:05Z), next to
`journey-results-3.tar.gz` (`1.1M`). Platform CI uploads terminal debug bundles with
`.github/actions/upload-s3-artifact` into the **ci-objectstore** (platform #621): RustFS
`rustfs/rustfs:1.0.0-beta.8` in `strive-ailab`, NodePort `:30900`, bucket `gha-artifacts`, and a
daily expiry sweep. Gitea's own Actions artifact storage is a different thing (a local PVC since W1)
and does not hold these bundles.

The store has exactly **one** credential: its RustFS root pair (`ci-objectstore-creds`), which is
also the runners' upload credential (Gitea secrets `CI_S3_ACCESS_KEY` / `CI_S3_SECRET_KEY`). Nothing
`cred` serves can read it, and the operator asked for the access to go through OpenBao.

Sharing the root pair is the wrong key to share:

- it is write + delete + **IAM admin** over the store, and the same key CI writes with, so a worker
  could plant or replace a bundle that another agent later trusts as evidence;
- rotating it breaks CI uploads (fail-soft, but evidence is lost in the window), so a worker leak
  would be expensive to revoke;
- the estate precedent is consistent: workers get the read-only zot `pull` identity, never `ci`, and a
  LiteLLM virtual key, never a master key.

Verified facts (2026-11-03). All of these were measured in a local docker copy of the exact image,
using the exact `mc` the CI action pins (`RELEASE.2025-08-13T08-35-41Z`, sha256 `01f866e9…e891`),
unless marked live.

- **IAM works.** RustFS beta.8 implements MinIO-compatible IAM: `admin policy create`, `user add`,
  `policy attach/detach`, `user info --json`, `user disable/enable`.
  - A user holding only the policy below can list and get in `gha-artifacts`.
  - It is denied put, rm, `mb`, reads in another bucket, and `admin user list`.
  - A user with no policy is denied everything.
- **IAM state persists.** It survives a server restart, **pod replacement** (a new container on the
  same volume), and a **root-key rotation** (secret only, and the whole pair).
- **Rotation and re-runs.**
  - Re-running `user add` with a new secret rotates it: the old secret then fails with
    `SignatureDoesNotMatch`, and the policy stays attached.
  - Re-running create/add/attach is idempotent (rc 0).
- **`user add` on a DISABLED user re-enables it.** A reconcile loop must therefore check the user's
  status *before* the upsert, or it silently undoes an operator's emergency disable.
- **Secrets stay out of argv.** `user add` and `alias set` read keys from stdin, and `MC_HOST_<alias>`
  env aliases work.
- **Error shapes.** With `--json`, real denials report `"Code":"AccessDenied"` (rm, mb, admin) or
  `Insufficient permissions to access this path` (put, cross-bucket get). Unreachable endpoints
  (`connection refused`) and bad keys (`SignatureDoesNotMatch`) look different, so a probe can tell
  "denied" from "inconclusive".
- **The images.** The pinned `rclone/rclone:1.75.1@sha256:45401ad7…` image is Alpine with busybox
  `wget` (HTTPS works) and `sha256sum`. RustFS ships no admin CLI.
- **Network.** No CiliumClusterwideNetworkPolicy exists, and the `strive-ailab` egress policies
  select only app pods (by `app.kubernetes.io/name`), so a new Job pod can reach github.com. The
  ci-objectstore ingress policy admits every source on :9000.
- **Live, from dev-worker-4.** `http://192.168.0.41|42|43:30900/` answer 403 (the anonymous S3
  denial). The API VIP `.40` does not serve the NodePort. The worker has no S3 client.
- **Live OpenBao audience (source:** `devworker-provision-job.yaml:81-96` and `:112-116`). The
  dev-worker-1..4 policies grant `read` on `af/data/dev-workers/common`, and nothing can write it.
  reviewer-1/2 get only the codex projection. So anything that can use a worker's sink token
  (root, plus the worker user that `cred` runs as) can read the key. `cred` is a delivery helper,
  not an authorization boundary. Holders of the ailab age key and OpenBao admins can also read it.

## Approach

A dedicated **read-only RustFS identity scoped to `gha-artifacts`**, reconciled declaratively in the
platform repo next to the store, with its key projected into `af/dev-workers/common` through the
existing seed-wins path. It goes in `common` because the need is fleet-wide: any worker agent
debugging a platform CI failure hits the same gap.

### 1. platform PR: `feat/ci-objectstore-reader-identity`

`deploy/secrets/ailab/**` is owner-protected (`check-s2s-authority.py:184`), so this PR merges
through `scripts/gitea-owner-merge.sh platform <PR> <head> --execute`. That script gates on both
reviewer bots APPROVED at the exact head and every required context green, mints a one-shot owner
token, and only uses `force_merge` after Gitea's "Changed protected files" refusal. `owner_ack.py`'s
`approve-pin` is not involved: it covers different paths.

- **The Secret.** `deploy/secrets/ailab/ci-objectstore-reader-creds.enc.yaml`, Secret
  `ci-objectstore-reader-creds`:
  - `access-key: dev-workers-ci-reader`, and `secret-key` (GEN, 64 hex chars).
  - SOPS-encrypted to the ailab recipient with `--encrypted-regex '^(data|stringData)$'`, like its
    siblings.
  - A matching `_example/` template, listed in BOTH `kustomization.yaml` and
    `_example/kustomization.yaml`.
  - A `SECRETS.md` row naming this file as the **authoritative** value (it is what RustFS holds),
    the second home, and the rotation, revocation and teardown procedures below.
- **The Job.** `deploy/components/ci-objectstore/reader-user-job.yaml`, Job
  `ci-objectstore-reader-user`.
  - **Lifecycle (same as bucket-init).** `ttlSecondsAfterFinished: 86400`, so Flux re-creates it
    about daily. `force: enabled`, `backoffLimit: 6`, `activeDeadlineSeconds: 600`.
    `automountServiceAccountToken: false`, `nodeSelector: kubernetes.io/arch: amd64` (the binary pin
    is amd64), and the restricted securityContext its siblings use.
  - **Re-runs.** `force` only replaces a Job whose template changed. A changed Secret, or a Job that
    finished Complete or Failed, re-runs only after the TTL sweep (≤ 1 day + 10 min). An immediate
    re-run is `kubectl -n strive-ailab delete job ci-objectstore-reader-user`, after which Flux
    re-creates it. A failed run surfaces as the existing KubeJobFailed alert, as bucket-init's does.
  - **initContainer `fetch-mc`.** Up to 3 `wget` attempts (`-T 60`, backoff), then
    `sha256sum -c` against the action's pin. Any mismatch fails the Job. Accepted trade-off: a
    GitHub outage blocks bootstrap, drift repair and a rotation's apply until a later run. The
    already-provisioned reader keeps working meanwhile, and no mirror is introduced for this.
  - **container `reconcile`.** Root keys arrive as env and reach `mc` on stdin. Every `mc` call's
    output and rc are captured before parsing; nothing is piped from a command whose failure could
    be masked. The steps:
    0. **Pre-state.** Read `user info` strictly.
       - `NoSuchResource` means a first run.
       - `success` + `enabled` means an update.
       - `success` + `disabled` makes the Job **exit 1 without touching anything**: the Job never
         re-enables a user, because a disabled reader means an open incident (an operator's
         revocation, or a past failed proof).
       - Anything else (unreachable, bad root key, malformed JSON) fails the Job.
    1. `admin policy create root gha-artifacts-ro` (an upsert of the Job's literal). It allows
       `s3:GetBucketLocation` and `s3:ListBucket` on `arn:aws:s3:::gha-artifacts`, and
       `s3:GetObject` on `arn:aws:s3:::gha-artifacts/*`. Nothing else.
    2. `admin user add root`, with the keys on stdin. This is an upsert, and it is the rotation path.
    3. `admin policy attach`, then detach every other directly attached policy. A non-empty
       `memberOf` (group-derived rights) is treated as a privilege violation.
    4. **Prove it, as the reader, before exiting 0.**
       - Write a canary object as root. The reader must list the bucket and `cat` the canary
         byte-exact.
       - Expect DENIED, classified by the error shapes above: put, rm of the canary, `mb` of a new
         bucket, and `admin user list`. A probe that SUCCEEDS is a violation. A probe that fails
         any other way is inconclusive and fails the Job without disabling anything.
       - Finally, a strict `user info` must report exactly `policyName == gha-artifacts-ro`,
         `userStatus == enabled`, and no `memberOf`.
       - **On any violation: `admin user disable` the reader, then exit 1.** If the disable itself
         fails, log `CONTAINMENT FAILED` and exit 1. The canary and any probe object are removed
         with root on every exit path.
  - Never print a secret. No `set -x`.
- `deploy/components/ci-objectstore/kustomization.yaml`: add the resource, plus a header note that
  the root pair is never handed to a reader.

### 2. ailab PR: `feat/dev-workers-ci-artifact-reader`

- **The seed.** `kubernetes/apps/infrastructure/security/openbao/devworker-seeds.sops.yaml`,
  `common.json`, gains four fields:
  - `ci_s3_endpoint`: `http://192.168.0.41:30900`. `.42` and `.43` are the documented fallback;
    the VIP `.40` is not one.
  - `ci_s3_bucket`: `gha-artifacts`.
  - `ci_s3_access_key`: `dev-workers-ci-reader`.
  - `ci_s3_secret_key`.

  The edit goes through a `sops edit` EDITOR shim, run in WSL. sops holds the plaintext in its own
  private tmpfile and deletes it. The shim:
  - reads the secret from the platform file by an in-process `sops -d`;
  - splices only the `common.json` scalar;
  - asserts every other key and every other `common.json` field is unchanged;
  - prints field names and a SHA-256 prefix, never a value.
- **The runbook.** `docs/runbooks/openbao-dev-workers.md`: the `common` field list, plus a section
  covering:
  - scope and audience;
  - the two homes, and which one is authoritative;
  - rotation, emergency revocation, a compromised worker, and teardown;
  - the residual risk;
  - an executable recipe. The non-secret `ci_s3_endpoint` / `ci_s3_access_key` are fetched with
    `cred get` into the child's env, the secret arrives with `cred exec`, and `mc` takes it as
    `MC_HOST_ci` (env). The `curl --aws-sigv4` alternative passes the key through `curl -K -` on
    stdin (`printf` is a builtin), never `--user` in argv. `mc` is fetched at the action's version
    and sha into `$HOME/.local/bin` and called by full path.

### 3. Roll out and verify (gated, in this order)

1. **Platform PR** merged by the owner path above, after both reviewer bots are clean and
   `platform-guards` (CI's `mise run guard:all`) ran on it rather than being path-skipped.
2. **Gate on the live Job.** Flux `platform-secrets` and `platform-ci-objectstore` report the merge
   revision, and Secret `ci-objectstore-reader-creds` exists. Job `ci-objectstore-reader-user` is
   `Complete` and its log shows `reader-user: OK`. If it is Failed, read the log, fix forward, and do
   NOT merge the ailab PR.
3. **ailab PR** merged by the reviewer bots. Then gate on Flux applying the new seed: the `openbao`
   layer at the merge revision, and the `openbao-devworker-seeds` Secret's resourceVersion changed.
   Only then force the provision Job (runbook *Rotation* step 4) and wait for it to be `Complete`.
4. **Verify on dev-worker-4** (step 5 below), then tell the requesting agent where the key is and
   how to use it.

### Rotation, revocation and teardown (documented in SECRETS.md and the runbook)

- **Rotation.** Generate a new secret in the platform file first (authoritative). Merge it, delete
  the Job, and wait for `reader-user: OK`. From that moment the old key is dead. Then carry the same
  value to `common.json` (shim), merge, and force the provision Job. The accepted interruption is
  that workers hold a dead key between those two steps. CI is untouched. Recovery is always
  forward: never restore a key from either repo's history.
- **Emergency revocation.** As root, run `mc admin user disable dev-workers-ci-reader`. It takes
  effect immediately. The Job then fails loudly on every run and never re-enables the user. Next,
  rotate as above. Re-enable deliberately with `mc admin user enable`, which also clears the daily
  failure.
- **A compromised worker.** Revoke that worker's AppRole secret-id and tokens FIRST (the runbook's
  per-worker procedure). Otherwise it reads the replacement from `common` the moment it is seeded.
  Then rotate. The shared key gives no per-worker attribution or revocation (the same trade-off the
  runbook records for `proxmox_ssh_key`).
- **Root rotation.** IAM survives it (measured). The Job authenticates with whatever the Secret
  says, so between a root-Secret change and the store's pod roll it fails auth. That is expected and
  self-clears on the next run after the roll.
- **Teardown.** Remove the Job and the Secret from platform. As root, run
  `mc admin user remove dev-workers-ci-reader` and `mc admin policy remove gha-artifacts-ro`
  (removing manifests does not touch RustFS). Drop the four fields from `common.json`, and add
  `common ci_s3_endpoint ci_s3_bucket ci_s3_access_key ci_s3_secret_key` to the provision Job's
  `MOVED_FIELDS`, because seed-wins never deletes a key.

### Rejected alternatives

- **Share the root pair**: rejected for the reasons in Context.
- **Presigned URL per request**: the narrowest option (one object, read-only, at most 7 d). But it is
  operator-in-the-loop for every request and leaves the fleet gap open. It stays the fallback for a
  one-off outside this scope.
- **Operator fetch, sanitize and copy**: the same problem, plus operator labour on every request.

### Residual risk (accepted, documented)

- **Contents.** The bucket holds more than compose logs. The callers (`upload-s3-artifact` across 9
  workflows) upload:
  - `docker-logs-*` (CI stacks);
  - Playwright results and HTML reports, coverage, and airlock e2e results;
  - **`live-ailab-e2e-traces` / `-deep-traces`**: Playwright traces of the LIVE ailab platform.
    These can carry the e2e persona's session cookies and bearer tokens (short-lived) and the
    persona password it types. That password is already in `common` (`strive_test_password`), so
    this widens nothing for the password. It does expose live session tokens until they expire.
- **Audience.** All four dev-workers, and anything that can use their sink tokens.
- **Accepted on the same grounds as the existing `common` fields.** A worker that already holds the
  live persona login and the job logs gains no new principal.
- **Lifetime.** The sweep deletes objects older than 7 days once a day, so objects live up to ~8
  days, or longer if a sweep fails (KubeJobFailed). The reader key itself never expires and reads
  future uploads until rotated.
- **Transport.** It is plain HTTP on the mgmt LAN. SigV4 authenticates requests but gives no
  confidentiality or response integrity. This is the same LAN-trust model the runners already
  upload under, with the ROOT key, over the same NodePort (`service.yaml:14-19`). It is accepted and
  stated in the runbook, not fixed here.

## Round-2 resolutions (all accepted; the plan cap is 2 rounds)

- **Bucket policies.** The Job reads `mc anonymous get-json` and treats any statement as a
  violation (disable + fail). Measured: a principal prefix grant really does let the reader write
  under that prefix, and `mc anonymous set none|private` does NOT remove it on beta.8; only
  `set-json` with an empty Statement list does. That is now in SECRETS.md.
- **Contents readback.** After `policy create` and again at the end, the STORED policy must have
  exactly the literal's actions and resources, Allow only, and no Not*/Principal keys. A
  broadened literal (put, delete, cross-bucket, CreateBucket) is contained before any probe runs.
- **Incident blocker.** The procedure is now: suspend the Kustomization, delete any in-flight Job,
  disable, revoke the worker's AppRole if compromised, rotate, and wait for the live Secret.
  Recovery is by `user remove` and a fresh create, never `enable`, so the compromised secret is
  never revived. Tested: 12, 12b and 12c.
- **Rotation and restore gates.** Re-run only after the live Secret's hash matches the new value,
  then verify new-reads and old-refused on a worker. After a volume restore, use the same
  remove-and-recreate path. Both are in SECRETS.md.
- **Root rotation.** Explicit `rollout restart` (a Secret change does not roll the pod), then the
  Gitea Actions secrets, then a fresh reader Job.
- **Failure-path tests.** Added malformed user info (15), containment-disable failure (16), the
  probe-level put control with readback stubbed (07p), a cross-bucket read fixture (03c), and an
  old key refused after rotation (03a). An interruption between upsert and detach is retried
  immediately by `restartPolicy: OnFailure`, and leaves no state worse than before the run.
- **Reviewer audience.** Stated from the provisioner source and NOT live-verified. A live check
  needs the breakglass token for a policy this change does not touch.
- **Worker end-to-end.** On dev-worker-4, fetch the real bundle (present until at least 11-10)
  with the documented recipe, and compare its sha256 with the copy fetched with root
  (`3f4d7c7ede212bcb…`).

## Critical files

- platform `deploy/components/ci-objectstore/reader-user-job.yaml` (new): the IAM reconcile, proof
  and containment.
- platform `deploy/components/ci-objectstore/kustomization.yaml`: wires the resource.
- platform `deploy/secrets/ailab/ci-objectstore-reader-creds.enc.yaml` (new), plus its `_example/`,
  both `kustomization.yaml`s and `SECRETS.md`.
- ailab `kubernetes/apps/infrastructure/security/openbao/devworker-seeds.sops.yaml`: `common.json`.
- ailab `docs/runbooks/openbao-dev-workers.md`.

## Verification

1. **Local, in the pod contract.** Run the scripts extracted from the manifest (not retyped), in the
   pinned rclone image, as UID 10001, with a read-only root filesystem and an exec tmpfs `/work` (the
   emptyDir), against `rustfs/rustfs:1.0.0-beta.8`. Cases:
   - first run, re-run, rotation (old key refused), and pod replacement;
   - a planted `readwrite` policy (must be detached);
   - a group membership (must disable and fail);
   - a policy broadened to put, and separately to delete (must DISABLE the reader and exit 1);
   - an operator-disabled reader (must exit 1 and stay disabled);
   - a missing bucket (must exit 1, inconclusive, user not disabled);
   - an unreachable store or bad root key (must exit 1, inconclusive);
   - a bad mc digest (init must fail).

   After every case, no canary or probe objects may be left behind.
2. **Builds and CI.** `kubectl kustomize` builds `deploy/components/ci-objectstore`, and both secret
   kustomizations build. A server-side dry-run of the Job passes admission (PodSecurity). Platform
   CI is green with `platform-guards` actually run. Note: `ailab-pins` does not validate the
   rclone or mc pins (its collector matches first-party `registry/strive` images only), so step 1 is
   what validates them.
3. **SOPS.**
   - Every new value is `ENC[`, `sops filestatus` reports encrypted, and a silent round-trip
     decrypt parses.
   - The shim asserts that every unrelated `common.json` field is unchanged.
   - The SHA-256 prefix of the secret is equal in both homes.
   - Deployed equality is proven end to end by step 5: the key `cred` serves authenticates to
     RustFS.
4. **Live.** Job `Complete`, with the log showing the proofs. `bao token capabilities` with
   dev-worker-4's token on `af/data/dev-workers/common` returns `read` only.
5. **On dev-worker-4, after the provision Job.**
   - The runbook recipe works verbatim, and a write with the reader is denied.
   - Report separately whether the requested `119597-1/docker-logs-journey-3.tar.gz` (uploaded
     2026-11-03 16:03Z, so expiring no earlier than 2026-11-10) is still present.
   - The Job's own canary is the repeatable authorization check, so an expired bundle is never
     mistaken for a denial.
   - `~/.git-credentials` and the other `common` fields still render.

<!-- codex-review-status: finalized -->