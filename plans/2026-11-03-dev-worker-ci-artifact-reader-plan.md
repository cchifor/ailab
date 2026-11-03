# Dev-worker read-only access to the CI-artifact store

## Context

A codex agent on dev-worker-4 needs one CI debug bundle:
`s3://gha-artifacts/119597-1/docker-logs-journey-3.tar.gz` (platform run 119597, job 680776, journey
shard 3/4, Sentinel HTTP 503). Platform CI uploads terminal debug bundles with
`.github/actions/upload-s3-artifact` into the **ci-objectstore** (platform #621): RustFS
`rustfs/rustfs:1.0.0-beta.8` in `strive-ailab`, NodePort `:30900`, bucket `gha-artifacts`, 7-day
expiry CronJob. Gitea's own Actions artifact storage is a different thing (local PVC since W1) and
does not hold these bundles.

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

Verified facts (2026-11-03):
- RustFS beta.8 implements MinIO-compatible IAM. In a local docker copy of the exact image, with the
  exact `mc` the CI action pins (`RELEASE.2025-08-13T08-35-41Z`, sha256 `01f866e9…e891`):
  `admin policy create`, `admin user add`, `admin policy attach/detach` and `admin user info --json`
  all work. A user holding only the policy below can `ls` and `get` in `gha-artifacts`, and is
  denied put, rm, reads in other buckets, and `admin user list`. The user and its policy survive a
  restart. Re-running `user add` with a new secret rotates it (old secret then fails signature
  checks, and the policy stays attached). Re-running create/add/attach is idempotent (rc 0).
  `user add` reads the access and secret keys from stdin, and `MC_HOST_<alias>` env aliases work, so
  no secret needs to appear in argv. A user with no policy is denied everything.
- The pinned `rclone/rclone:1.75.1@sha256:45401ad7…` image the component already uses is Alpine
  with busybox `wget` (HTTPS works) and `sha256sum`. RustFS ships no admin CLI.
- No CiliumClusterwideNetworkPolicy exists, and the `strive-ailab` egress policies select only app
  pods by `app.kubernetes.io/name`, so a new Job pod can reach github.com. The ci-objectstore
  ingress policy admits every source on :9000.
- From dev-worker-4, `http://192.168.0.41|42|43:30900/` answer 403 (the anonymous S3 denial). The
  API VIP `.40` does not serve the NodePort. The worker has no S3 client (`mc`, `aws` and `rclone`
  are all absent). Its curl supports `--aws-sigv4`.

## Approach

A dedicated **read-only RustFS identity scoped to `gha-artifacts`**, reconciled declaratively in the
platform repo next to the store, with its key projected into `af/dev-workers/common` through the
existing seed-wins path. It goes in `common` because the need is fleet-wide: any worker agent
debugging a platform CI failure hits the same gap.

### 1. platform PR: `feat/ci-objectstore-reader-identity`

- `deploy/secrets/ailab/ci-objectstore-reader-creds.enc.yaml`: Secret `ci-objectstore-reader-creds`
  holding `access-key: dev-workers-ci-reader` and `secret-key` (GEN, 64 hex chars). It is
  SOPS-encrypted to the ailab recipient (the existing `deploy/secrets/ailab` rule), with a matching
  `_example/` template, a `kustomization.yaml` entry and a `SECRETS.md` row. The row records the
  second home (ailab `devworker-seeds.sops.yaml` → `af/dev-workers/common`) and gives the rotation
  procedure.
- `deploy/components/ci-objectstore/reader-user-job.yaml`: Job `ci-objectstore-reader-user`, the
  same lifecycle as `ci-objectstore-bucket-init` (`ttlSecondsAfterFinished: 86400`, so Flux
  recreates it about daily; that re-run is the convergence loop and is what applies a rotation). It
  carries `kustomize.toolkit.fluxcd.io/force: enabled`, `backoffLimit: 6` and
  `activeDeadlineSeconds: 600`, and the same restricted securityContext as its siblings.
  - initContainer `fetch-mc` (rclone image): `wget` the pinned `mc` into an emptyDir, then
    `sha256sum -c` against the same version and digest as `upload-s3-artifact`. Any mismatch fails
    the Job.
  - container `reconcile` (rclone image, runs `/work/mc`). Root creds arrive as env from
    `ci-objectstore-creds`, and the root alias is built as `MC_HOST_root` inside the script.
    The steps:
    1. `admin policy create root gha-artifacts-ro`. The policy allows `s3:GetBucketLocation` and
       `s3:ListBucket` on `arn:aws:s3:::gha-artifacts`, and `s3:GetObject` on
       `arn:aws:s3:::gha-artifacts/*`. Nothing else.
    2. `admin user add root`, with the access and secret keys on stdin (an upsert, which is also
       the rotation path).
    3. `admin policy attach root gha-artifacts-ro --user <ak>`, then detach every other policy that
       `admin user info --json` lists, and `admin user enable`.
    4. **Prove before exiting 0**, as the reader:
       - `ls` of the bucket succeeds;
       - a write probe (`pipe` to `gha-artifacts/.reader-probe-<ts>`) is DENIED. If it succeeds,
         remove the probe with root and exit 1;
       - `admin user list` is DENIED;
       - `user info` reports exactly `policyName == gha-artifacts-ro`.
  - Never print a secret. `set -eu` is on and `set -x` is never used.
- `deploy/components/ci-objectstore/kustomization.yaml`: add the resource, plus a header note.

### 2. ailab PR: `feat/dev-workers-ci-artifact-reader`

- `kubernetes/apps/infrastructure/security/openbao/devworker-seeds.sops.yaml`, `common.json`: add
  `ci_s3_endpoint` (`http://192.168.0.41:30900`, the same node litellm_diag_base uses; `.42` and
  `.43` also work), `ci_s3_bucket` (`gha-artifacts`), `ci_s3_access_key` (`dev-workers-ci-reader`)
  and `ci_s3_secret_key`. The edit goes through a sops `EDITOR` shim that reads the secret from the
  platform file via an in-process `sops -d`. The value never reaches argv, the terminal or my
  context. Verify by comparing hashes, never values.
- `docs/runbooks/openbao-dev-workers.md`: the `common` field list, plus a paragraph covering scope
  (read-only, one bucket, 7-day bundles), the two homes, rotation, the residual risk, and a usage
  recipe:
  `cred exec common ci_s3_secret_key CI_S3_SK -- sh -c 'MC_HOST_ci="http://$AK:$CI_S3_SK@host:30900" mc cp ci/gha-artifacts/<run>-<attempt>/<name>.tar.gz .'`
  with `mc` fetched at the CI action's pinned version and sha. A `curl --aws-sigv4` alternative is
  also listed.

### 3. Roll out and verify

Merge platform first, so the identity exists, then ailab. Then force the provision Job (runbook
*Rotation* step 4) and verify from dev-worker-4. Then tell the requesting agent where the key is.

### Rejected alternatives

- **Share the root pair**: rejected for the reasons in Context.
- **Presigned URL per request**: the narrowest option (one object, read-only, at most 7 d). But it is
  operator-in-the-loop for every request and leaves the fleet gap open. It stays the fallback for a
  one-off outside this scope.
- **Operator fetch, sanitize and copy**: the same problem, plus operator labour on every request.

### Residual risk (accepted, documented)

Bundles are raw `docker compose logs` of CI stacks. Any worker can read any bundle from the last 7
days, including whatever CI services log (CI-only test credentials). The same audience already reads
the job logs with `gitea_pat`. A leak is revoked by rotating one file pair, and CI uploads are
untouched.

## Critical files

- platform `deploy/components/ci-objectstore/reader-user-job.yaml` (new): the IAM reconcile and
  proof.
- platform `deploy/components/ci-objectstore/kustomization.yaml`: wires the resource.
- platform `deploy/secrets/ailab/ci-objectstore-reader-creds.enc.yaml` (new), plus its `_example/`,
  `kustomization.yaml` and `SECRETS.md`.
- ailab `kubernetes/apps/infrastructure/security/openbao/devworker-seeds.sops.yaml`: `common.json`.
- ailab `docs/runbooks/openbao-dev-workers.md`.

## Verification

1. Local: run the Job's exact script, with the same images, against `rustfs/rustfs:1.0.0-beta.8` in
   docker. Cover first run, re-run, rotation, and a planted extra `readwrite` policy (it must be
   detached). Also confirm the proof step fails closed when the policy is too broad.
2. `kubectl kustomize deploy/components/ci-objectstore` builds, and platform CI is green.
3. SOPS: every new value is `ENC[`, and `sops filestatus` reports encrypted. The SHA-256 of the
   secret in both homes is equal (print the hashes, never the values).
4. Live: Job `Complete`, with the log showing the four proofs. `admin user info` shows only
   `gha-artifacts-ro`.
5. After the provision Job runs, on dev-worker-4: `cred get common ci_s3_access_key` returns the
   reader, `mc ls` lists `ci/gha-artifacts/119597-1/`, `mc cp` fetches the requested bundle, a write
   is denied, and `~/.git-credentials` and the other `common` fields still render.

<!-- codex-review-status: pending -->
