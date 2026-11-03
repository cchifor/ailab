# Dev-worker read-only access to the CI-artifact store

## Codex Review

- Separating reader access from the CI root credential is sound, and the measured IAM behavior establishes feasibility.
- Security blockers: a failed proof leaves the identity enabled, and the proposed probes do not establish all claimed permission boundaries.
- Two SOPS homes and two independent Jobs need an explicit rotation, emergency revocation, rollback and recovery procedure.
- The shared credential’s audience, HTTP transport and actual artifact retention/exposure need clearer risk acceptance.
- Rollout must gate on Job completion, respect protected-secret merge controls, and justify permanent fleet access against the existing automated presigned-URL attempt.

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
  <!-- codex: The measured restart does not establish recovery after pod replacement or PVC loss/restore: only /data is persistent, while /tmp and the container filesystem are not (platform/deploy/components/ci-objectstore/deployment.yaml:116). Add replacement against the same mounted data, an empty-volume bootstrap and an older-volume restore to the recovery tests; a previously Complete Job will not notice lost or reverted IAM state. -->
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
<!-- codex: Make the audience explicit: the four current dev-worker policies read the entire common document, while reviewer-1/2 do not (ailab/kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml:81 and :112); any process able to use a worker's sink token can obtain this key, not just the requesting agent. Verify live grants, including broader policies, and account for OpenBao administrators and holders of either SOPS decryption path; cred is a delivery helper, not an additional authorization boundary. -->

### 1. platform PR: `feat/ci-objectstore-reader-identity`

<!-- codex: The secret directory is owner-protected under platform/scripts/ci/check-s2s-authority.py:184, and platform/.agentforge/repo-map.md:16 reserves deploy/ changes for owner PRs. State the owner merge path explicitly; scripts/ci/owner_ack.py:44 covers different paths, so its approve-pin ceremony is not the relevant gate for this proposed diff. -->

- `deploy/secrets/ailab/ci-objectstore-reader-creds.enc.yaml`: Secret `ci-objectstore-reader-creds`
  holding `access-key: dev-workers-ci-reader` and `secret-key` (GEN, 64 hex chars). It is
  SOPS-encrypted to the ailab recipient (the existing `deploy/secrets/ailab` rule), with a matching
  `_example/` template, a `kustomization.yaml` entry and a `SECRETS.md` row. The row records the
  second home (ailab `devworker-seeds.sops.yaml` → `af/dev-workers/common`) and gives the rotation
  procedure.
  <!-- codex: The creation rule selects the recipient but does not set values-only encryption (platform/.sops.yaml:77); explicitly use encrypted_regex ^(data|stringData)$ as in deploy/secrets/ailab/ci-objectstore-creds.enc.yaml:28. Keep manifest identity fields readable and include the new template in _example/kustomization.yaml, which separately enumerates examples. -->
  <!-- codex: Designate one authoritative credential source and specify the two-repository rotation order: applying a new secret to this fixed username immediately invalidates the old one, while each reconciler may still hold the previous value. Require both deployed Secrets to converge, explicitly rerun both Jobs, verify the new key works and the old key fails, and document the accepted interruption and forward-recovery procedure if either half fails. -->
- `deploy/components/ci-objectstore/reader-user-job.yaml`: Job `ci-objectstore-reader-user`, the
  same lifecycle as `ci-objectstore-bucket-init` (`ttlSecondsAfterFinished: 86400`, so Flux
  recreates it about daily; that re-run is the convergence loop and is what applies a rotation). It
  carries `kustomize.toolkit.fluxcd.io/force: enabled`, `backoffLimit: 6` and
  `activeDeadlineSeconds: 600`, and the same restricted securityContext as its siblings.
  <!-- codex: force only replaces a Job when an immutable field changes; updating a referenced Secret or reconciling Flux does not rerun an unchanged Complete or Failed Job (platform/deploy/components/ci-objectstore/bucket-init-job.yaml:23). A terminal failure can therefore leave convergence stalled for roughly another day plus the 10-minute Flux interval; document an immediate rerun and failure alert instead of relying on retryInterval (deploy/gitops/flux/clusters/ailab/kustomizations.yaml:153). -->
  <!-- codex: Set automountServiceAccountToken: false explicitly: this root-credential-bearing Job needs S3 access, not Kubernetes API credentials. The OpenBao provisioner already follows that rule (ailab/kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml:425), whereas copying the objectstore siblings alone would omit it. -->
  - initContainer `fetch-mc` (rclone image): `wget` the pinned `mc` into an emptyDir, then
    `sha256sum -c` against the same version and digest as `upload-s3-artifact`. Any mismatch fails
    the Job.
    <!-- codex: The checksum protects integrity, not availability: every fresh emptyDir requires GitHub and its release-asset redirects, unlike the uploader's verified persistent cache (platform/.github/actions/upload-s3-artifact/action.yml:189 and :207). Bound download retries within the Job deadline and specify an existing-mirror fallback or explicitly accept that an upstream outage prevents bootstrap, drift repair and urgent rotation. -->
    <!-- codex: This binary pin is Linux amd64, while the image reference alone does not constrain scheduling; select kubernetes.io/arch: amd64 or provide an independently pinned binary per supported architecture. The uploader explicitly checks this assumption before downloading (platform/.github/actions/upload-s3-artifact/action.yml:185). -->
  - container `reconcile` (rclone image, runs `/work/mc`). Root creds arrive as env from
    `ci-objectstore-creds`, and the root alias is built as `MC_HOST_root` inside the script.
    <!-- codex: Root-key rotation needs an ordered recovery procedure: RustFS reads the root pair from process environment, so changing its Secret does not update the running server, but a fresh reconcile pod receives the new pair and may fail authentication (platform/deploy/components/ci-objectstore/deployment.yaml:66; deploy/secrets/ailab/SECRETS.md:78). Coordinate server restart, CI credential updates and a fresh reader Job, and test that persisted IAM remains usable after the root change. -->
    The steps:
    1. `admin policy create root gha-artifacts-ro`. The policy allows `s3:GetBucketLocation` and
       `s3:ListBucket` on `arn:aws:s3:::gha-artifacts`, and `s3:GetObject` on
       `arn:aws:s3:::gha-artifacts/*`. Nothing else.
    2. `admin user add root`, with the access and secret keys on stdin (an upsert, which is also
       the rotation path).
    3. `admin policy attach root gha-artifacts-ro --user <ak>`, then detach every other policy that
       `admin user info --json` lists, and `admin user enable`.
       <!-- codex: Direct policy attachments are not necessarily the full authorization set: account for group-derived permissions and bucket-policy grants before claiming this identity has exactly these rights. Query user-info with root and reject missing/malformed results rather than treating failed enumeration as an empty policy list. -->
       <!-- codex: Unconditionally enabling the identity defeats an operator's emergency disable on the next run. Define an incident procedure that stops or changes desired reconciliation before disabling the user, so automated upsert/enable cannot restore revoked access. -->
    4. **Prove before exiting 0**, as the reader:
       - `ls` of the bucket succeeds;
       - a write probe (`pipe` to `gha-artifacts/.reader-probe-<ts>`) is DENIED. If it succeeds,
         remove the probe with root and exit 1;
         <!-- codex: Blocker: deleting the probe and exiting 1 leaves the overprivileged reader enabled; this is failure detection, not fail-closed authorization. Disable the identity on a detected privilege violation, surface any containment failure, and test interrupted/partially completed reconciliation before distributing the key. -->
       - `admin user list` is DENIED;
       <!-- codex: A nonzero CLI exit is not proof of AccessDenied: timeout, connection failure, unsupported operation and invalid signatures must fail verification rather than count as successful negative probes. Inspect the structured error code and retain sanitized diagnostics. -->
       - `user info` reports exactly `policyName == gha-artifacts-ro`.
       <!-- codex: These proofs can miss a policy granting delete or reads outside gha-artifacts, and ls does not prove GetObject. Extend exact-script tests with a successful read plus denied deletion and cross-bucket reads against known disposable fixtures, including deliberately broadened policies for each case. -->
  - Never print a secret. `set -eu` is on and `set -x` is never used.
    <!-- codex: set -eu does not reliably propagate an earlier command's failure through a pipeline; explicitly check the mc exit status before parsing user-info or interpreting probe output. Test command failure and malformed JSON as failures of the reconciler, not merely the happy-path policy shape. -->
- `deploy/components/ci-objectstore/kustomization.yaml`: add the resource, plus a header note.

### 2. ailab PR: `feat/dev-workers-ci-artifact-reader`

- `kubernetes/apps/infrastructure/security/openbao/devworker-seeds.sops.yaml`, `common.json`: add
  `ci_s3_endpoint` (`http://192.168.0.41:30900`, the same node litellm_diag_base uses; `.42` and
  `.43` also work), `ci_s3_bucket` (`gha-artifacts`), `ci_s3_access_key` (`dev-workers-ci-reader`)
  and `ci_s3_secret_key`. The edit goes through a sops `EDITOR` shim that reads the secret from the
  platform file via an in-process `sops -d`. The value never reaches argv, the terminal or my
  context. Verify by comparing hashes, never values.
  <!-- codex: The fixed .41 endpoint adds a dependency on that node even when the store remains reachable through .42/.43; include a concrete fallback in the usage recipe. This does not require introducing another load balancer. -->
  <!-- codex: A SOPS EDITOR workflow still materializes a plaintext editing file, containing the other common credentials as well. Specify an owner-only temporary location and cleanup, and ensure exception/debug output never includes decrypted documents. -->
- `docs/runbooks/openbao-dev-workers.md`: the `common` field list, plus a paragraph covering scope
  (read-only, one bucket, 7-day bundles), the two homes, rotation, the residual risk, and a usage
  recipe:
  `cred exec common ci_s3_secret_key CI_S3_SK -- sh -c 'MC_HOST_ci="http://$AK:$CI_S3_SK@host:30900" mc cp ci/gha-artifacts/<run>-<attempt>/<name>.tar.gz .'`
  with `mc` fetched at the CI action's pinned version and sha. A `curl --aws-sigv4` alternative is
  also listed.
  <!-- codex: Make this recipe executable: AK is never populated/exported, host is a placeholder, and the stored endpoint/bucket fields are unused. cred exec injects only the requested field (ailab/ansible/roles/dev_worker/files/cred:98), so show how the child obtains all required non-secret settings and uses the verified binary. -->
  <!-- codex: The curl alternative must keep the secret out of argv: expanding CI_S3_SK into --user would expose it despite using cred exec. Supply credentials through stdin configuration or an appropriately protected temporary config, and avoid verbose/debug output containing signed URLs or credentials. -->

### 3. Roll out and verify

Merge platform first, so the identity exists, then ailab. Then force the provision Job (runbook
*Rotation* step 4) and verify from dev-worker-4. Then tell the requesting agent where the key is.
<!-- codex: Blocker: merge order does not establish readiness because platform-ci-objectstore has wait: false (platform/deploy/gitops/flux/clusters/ailab/kustomizations.yaml:159), and bucket-init and reader reconciliation can start concurrently. Gate the ailab rollout on the intended platform-secrets revision, store/bucket readiness and successful completion of the current reader Job; likewise apply the intended ailab seed Secret before recreating its provision Job. -->

### Rejected alternatives

- **Share the root pair**: rejected for the reasons in Context.
- **Presigned URL per request**: the narrowest option (one object, read-only, at most 7 d). But it is
  operator-in-the-loop for every request and leaves the fleet gap open. It stays the fallback for a
  one-off outside this scope.
  <!-- codex: The uploader already attempts mc share download --expire 168h and writes the result to GITHUB_STEP_SUMMARY (platform/.github/actions/upload-s3-artifact/action.yml:268). Establish whether that output exists and is accessible for this run before calling presigning necessarily operator-mediated; permanent fleet access may still be justified, but this rejection currently overstates the alternative's cost. -->
- **Operator fetch, sanitize and copy**: the same problem, plus operator labour on every request.

### Residual risk (accepted, documented)

Bundles are raw `docker compose logs` of CI stacks. Any worker can read any bundle from the last 7
days, including whatever CI services log (CI-only test credentials). The same audience already reads
the job logs with `gitea_pat`. A leak is revoked by rotating one file pair, and CI uploads are
untouched.
<!-- codex: The bucket's contents are not constrained to compose logs or CI-only credentials: the generic uploader recursively archives caller-selected paths, including hidden files (platform/.github/actions/upload-s3-artifact/action.yml:119). Inventory actual upload callers and payload sensitivity before asserting equivalence with job-log access or treating fleet-wide exposure as accepted. -->
<!-- codex: Seven days is a deletion threshold, not a hard access lifetime: a successful daily sweep normally permits objects approaching eight days old, and failed sweeps permit longer retention (platform/deploy/components/ci-objectstore/expiry-cronjob.yaml:34 and :63). The static reader key also has no expiry and reads future uploads until revoked. -->
<!-- codex: HTTP exposes artifact contents and provides no authenticated server response; SigV4 request signing does not supply TLS confidentiality or response integrity. Explicitly accept that LAN trust assumption for potentially sensitive diagnostic evidence, or provide a protected transport (platform/deploy/components/ci-objectstore/service.yaml:14). -->
<!-- codex: A shared S3 key provides neither per-worker attribution nor individual revocation, and a compromised worker retaining common access can immediately fetch its replacement. Incident response must revoke the compromised worker's OpenBao access as well as rotate the S3 credential; retiring a worker's AppRole alone does not invalidate an already copied S3 key. -->
<!-- codex: Add teardown and rollback: removing the Job/Secret does not remove the RustFS user, and removing fields from the seed does not delete them from live KV (ailab/kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml:355). Stop recreation, revoke the IAM identity, remove its projected fields explicitly, and recover forward rather than restoring a compromised key from either repository's history. -->

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
   <!-- codex: Script-only execution does not verify the actual pod contract: exercise the init container and reconciler with the specified UID, read-only filesystem, shared emptyDir permissions and mc config location. Include cold bootstrap with delayed bucket readiness and a failed download; verify the resulting failure/retry behavior rather than only successful IAM commands. -->
2. `kubectl kustomize deploy/components/ci-objectstore` builds, and platform CI is green.
   <!-- codex: Component changes trigger ailab-pins (platform/.github/workflows/ci.yml:746), but its collector matches first-party registry/strive images, not rclone or the downloaded mc binary (scripts/ci/list-ailab-pins.py:147); green CI therefore does not validate those pins. Run the required guard:all workflow, render the secret and example Kustomizations too, and explicitly check both container pins and the binary version/digest without widening unrelated guard allowlists. -->
3. SOPS: every new value is `ENC[`, and `sops filestatus` reports encrypted. The SHA-256 of the
   secret in both homes is equal (print the hashes, never the values).
   <!-- codex: Equal repository hashes do not establish that RustFS and OpenBao use that value, and filestatus alone does not validate a decryptable, structurally correct document. Verify a silent SOPS round-trip, preserve all unrelated common fields, and compare the intended credential with the deployed projection before authenticating with it. -->
4. Live: Job `Complete`, with the log showing the four proofs. `admin user info` shows only
   `gha-artifacts-ro`.
   <!-- codex: Add authorization checks using representative live worker tokens and a reviewer token: workers should read the new fields, reviewers should be denied common, and workers should remain unable to update it. This validates the actual OpenBao audience rather than inferring it solely from the provisioner's source. -->
5. After the provision Job runs, on dev-worker-4: `cred get common ci_s3_access_key` returns the
   reader, `mc ls` lists `ci/gha-artifacts/119597-1/`, `mc cp` fetches the requested bundle, a write
   is denied, and `~/.git-credentials` and the other `common` fields still render.
   <!-- codex: The requested historical bundle may expire during the two-PR rollout; its absence must not be confused with an authorization failure. Use a known disposable object for repeatable acceptance checks and separately report whether the original diagnostic bundle remains available. -->

<!-- codex-review-status: complete -->
