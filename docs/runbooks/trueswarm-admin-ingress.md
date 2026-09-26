# Trueswarm administration ingress

Use `https://trueswarm-admin.chifor.me`. This single-level name uses the existing
`*.chifor.me` edge certificate. No Advanced Certificate Manager purchase or SSL API
permission is required.

DNS was published on **2026-09-26 at 18:54 UTC**, after the operator explicitly
lifted the publication hold and authorized a temporary Cloudflare CLI login. Normal
DNS resolution, valid HTTPS and the dedicated Access/Authelia redirect passed in a
fresh browser, including rejection of forged identity headers. Human login and
action-specific MFA still require authenticated acceptance checks.

The operator created the dedicated **locally managed** tunnel with:

```sh
cloudflared tunnel login
cloudflared tunnel create trueswarm-admin
```

Tunnel UUID: `d2452442-efae-4056-ac82-a5c348033971`. Its credential is encrypted in
the private `cchifor/trueswarm-admin` repository at
`deploy/foundation/admin-tunnel-credentials.sops.yaml`. Flux projects it only into
`trueswarm-admin/admin-tunnel-credentials`. The shared public tunnel is unchanged.
Do not import this tunnel into OpenTofu, or put an estate credential in Actions.

## Operator application

Run the existing Cloudflare module on the workstation that owns its state and
DNS/Access token. Do not initialize a replacement state on a worker or runner.
The change manages only one DNS record, one Access application, one dedicated OIDC
identity provider, and one named-email policy. Both enable flags default to false.

The helper automates the secret read and audience handoff. With this PR checked out
on the workstation and the private admin repository available locally, run:

```sh
TRUESWARM_ADMIN_CHECKOUT=/path/to/trueswarm-admin \
  bash scripts/trueswarm-admin-access.sh --apply-access
```

It uses the already-loaded `CLOUDFLARE_API_TOKEN` and the existing state. It refuses
CI, missing state, deletions and unrelated resource changes. It decrypts only the
dedicated OIDC client secret, applies only the Access gate, then commits the generated
**non-secret** audience to the private deployment. It does not publish DNS. No secret
or audience needs to be copied into chat. The following is the equivalent manual
sequence for operators who prefer separate plan/apply steps.

For a new installation, first provision the gate without DNS. The live gate is
already applied; use the adoption procedure below for the published installation.
From the private admin checkout, use the
operator's SOPS identity to read **only the dedicated client secret** into the
current shell; never echo it or use shell tracing:

```sh
export TF_VAR_trueswarm_admin_access_client_secret="$(sops decrypt \
  --extract '["stringData"]["OIDC_CLIENT_SECRET"]' \
  deploy/foundation/admin-access-identity.sops.yaml)"
export TF_VAR_enable_trueswarm_admin=true
export TF_VAR_publish_trueswarm_admin=false
# In the existing AILab kubernetes/infra/cloudflare directory, using existing state:
tofu plan -out=trueswarm-admin.plan
tofu apply trueswarm-admin.plan
tofu output -raw trueswarm_admin_access_audience
```

The initial allowlist is `chifor@gmail.com`. The two dedicated Authelia clients use
the `trueswarm_admin_mfa` authorization policy, which allows only `user:chifor` and
requires two-factor authentication. Access sessions expire after one hour. Its policy
requires both the dedicated IdP and the RFC 8176 `mfa` authentication-method claim.
Authelia includes this claim after multi-factor login (including verified passkeys).
See [Authelia AMR values](https://www.authelia.com/reference/guides/authentication-method-references/)
and [Cloudflare OIDC MFA requirements](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/mfa-requirements/).
Access selects only its dedicated OIDC IdP;
one-time PIN and shared service-token bypasses are not enabled for this application.

## Publication and browser qualification

Access-only provisioning intentionally creates no DNS record. Until publication,
`https://trueswarm-admin.chifor.me/` returns a DNS error (`NXDOMAIN`) in a normal
browser even when the application, tunnel and Access gate are healthy.

There are two release checks:

1. **Before publication:** internal health, current-pod network denial, origin JWT
   rejection, mTLS/signed-request enforcement, admission/RBAC and local restore
   checks must pass. Confirm Flux has rolled out the committed Access audience.
   The private repo's `node scripts/qualify-access-edge.mjs` verifies the existing
   edge certificate and dedicated Access/OIDC/PKCE route with a browser-only DNS
   override. It cannot establish a successful administrator login or action MFA.
2. **After controlled publication:** use the routable hostname to verify named
   administrator login, native OIDC MFA, denial of other identities, logout/session
   expiry/revocation, and operation-specific MFA. Do not declare the admin console
   production-ready until these authenticated browser checks pass.

The operator's publication hold remains authoritative. Successful automated checks
are evidence for that decision, not automatic permission to publish. Once the
operator lifts the hold for the authenticated browser rehearsal, run from the AILab
checkout on the workstation that owns the existing Cloudflare state and token:

```sh
TRUESWARM_ADMIN_CHECKOUT=/path/to/trueswarm-admin \
  bash scripts/trueswarm-admin-access.sh --publish-dns
```

This mode loads the dedicated client secret from SOPS exactly as Access mode does.
Its saved-plan guard requires all three Access resources to exist unchanged and
checks that the Access audience matches private GitOps and its rollout marker. It
requires that DNS record to be a proxied CNAME to the tunnel in private GitOps and
permits only changes to `cloudflare_dns_record.trueswarm_admin[0]`; Access changes,
deletions and unrelated changes are rejected. It does not change the private repo.
It cannot inspect live Flux from the workstation, so verify that rollout separately.

The resulting record is a **proxied CNAME** from `trueswarm-admin.chifor.me` to
`d2452442-efae-4056-ac82-a5c348033971.cfargotunnel.com`, behind the existing Access
application. No temporary bypass, alternate hostname, SSL resource or Tunnel API
permission is introduced. Estate credentials and state remain on the workstation.

### Adopt the CLI-published record

The operator-authorized publication used:

```sh
cloudflared tunnel route dns d2452442-efae-4056-ac82-a5c348033971 trueswarm-admin.chifor.me
```

Cloudflare confirmed the proxied CNAME with TTL `1` (automatic). Its non-secret
record ID is `0286010c0496bb660172f7b89713142d` in zone
`c967ce7dbbf43b1d7599eb4d213efa57`. The temporary CLI login certificate was removed
after verification. This temporary authorization was used for CLI publication and
target-record verification. Workstation state was not copied or replaced, and no
credential was put in Actions.

`trueswarm-admin-import.tf` adopts this existing record when **both** enable and
publication flags are true; it does nothing when either flag is false. On the
workstation that retains the Access state, pull this change and use the same
`--publish-dns` helper above. The saved plan should import the existing DNS record,
possibly adding its descriptive comment, while leaving all Access resources
unchanged. It must not create a second record or replace/delete the current record.
The import remains harmless after adoption. Keep both flags true in the workstation
configuration for future applies. Do not rerun `--apply-access` on the adopted live
installation: that mode sets publication false and its guard refuses DNS deletion.

The CLI publication is live independently of this state adoption. To repeat the
read-only public edge checks from the private admin checkout, use
`node scripts/qualify-access-edge.mjs --published`; this mode uses normal browser
DNS resolution, validates TLS, and checks the exact Access audience and dedicated
OIDC client/PKCE route. It does not complete human MFA or privileged operations.

Recovery is an independent gate: leave `recovery_qualified=false` and
`backup_hour_utc=null` until off-site coverage and recovery/promotion qualification
are explicitly approved. Local restore success or DNS publication does not enable
recovery operations.

Store the plans/state as sensitive operator files; Access client secrets can be
present in them. Confirm anonymous requests encounter the Access gate, the approved
administrator can complete native OIDC MFA, and other identities cannot enter.
Publishing DNS depends on the Access application resource. The explicit publication
flag is an operator gate, not a claim that application qualification has completed.

Keep `enable_trueswarm_admin=true` and the selected publication flag in the existing
workstation variable configuration after provisioning. Future module operations also
need the dedicated client secret loaded from SOPS; the defaults deliberately do not
adopt or preserve enabled resources. Do not apply a later plan that removes this gate.

Validation: OpenTofu 1.12.2 `fmt` and `validate` with Cloudflare provider 5.26.0
and backend disabled. `python3 -m unittest scripts.tests.test_trueswarm_admin_access`
exercises the actual helper using isolated Git repositories and fake cloud commands:
CI/missing prerequisites, unrelated changes, DNS publication and deletion are rejected;
valid Access changes commit and push the audience, reruns do not create empty commits,
and temporary sensitive plans are removed. Publication tests accept a DNS-only plan
with the existing matching gate, and reject Access changes, absent gate resources,
missing/mismatched audiences, stale rollout markers, deletions and unrelated DNS
changes. These fixture checks are not a real apply.
