# Trueswarm administration ingress

Use `https://trueswarm-admin.chifor.me`. This single-level name uses the existing
`*.chifor.me` edge certificate. No Advanced Certificate Manager purchase or SSL API
permission is required.

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

First provision the gate without DNS. From the private admin checkout, use the
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
requires two-factor authentication. Access selects only its dedicated OIDC IdP;
one-time PIN and shared service-token bypasses are not enabled for this application.

Supply the non-secret Access audience to the private deployment. Its issuer is
`https://chifor.cloudflareaccess.com`. Complete internal health, signed command,
network denial and recovery qualification checks before publishing:

```sh
export TF_VAR_publish_trueswarm_admin=true
tofu plan -out=trueswarm-admin-publish.plan
tofu apply trueswarm-admin-publish.plan
unset TF_VAR_trueswarm_admin_access_client_secret
```

Store the plans/state as sensitive operator files; Access client secrets can be
present in them. Confirm anonymous requests encounter the Access gate, the approved
administrator can complete native OIDC MFA, and other identities cannot enter.
Publishing DNS depends on the Access application resource. The explicit publication
flag is an operator gate, not a claim that application qualification has completed.

Validation performed in the worker: OpenTofu 1.12.2 `fmt` and `validate` with
Cloudflare provider 5.26.0 and backend disabled. No plan or apply was run there.
