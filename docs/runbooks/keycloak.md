# Keycloak (`id.chifor.me`)

The ailab identity provider, decided in [ADR 0038](../decisions/0038-ailab-keycloak-for-relay.md).
There is one realm per application. Today the only one is `relay`, with issuer
`https://id.chifor.me/realms/relay`, used by Relay (`relay.chifor.me`).

| Piece | Where |
|---|---|
| Deployment, Service, NetworkPolicy, realm JSON | `kubernetes/apps/apps/keycloak/` (Flux, `apps` layer) |
| Secrets (bootstrap admin, DB password, `relay` client secret) | `keycloak-secrets.sops.yaml` (SOPS is the system of record, not OpenBao) |
| Database | `keycloak` DB on CNPG `infra-pg`: role in `databases/infra-pg.yaml`, password `databases/infra-pg-keycloak.sops.yaml`, DB from `databases/keycloak-db-bootstrap.sql` |
| Public route | `apps/edge/cloudflared.yaml` (`/admin` and `/realms/master` answer 404) + `id` in `kubernetes/infra/cloudflare/variables.tf` |
| Monitoring | Gatus "Keycloak (id.chifor.me) OIDC discovery" |
| Backups | the nightly `infra-pg-dump` enumerates every database, `keycloak` included |

Everything below runs from the workstation with `kubectl --context admin@ai` (the default context is
a different cluster). **Never print a secret value.** Compare values by sha256 prefix.

## kcadm

Run kcadm inside the pod, against the pod's own HTTP port. The bootstrap credentials come from the
pod env, so they never cross the workstation:

```bash
kc() { kubectl --context admin@ai -n keycloak exec deploy/keycloak -c keycloak -- sh -c \
  'K=/opt/keycloak/bin/kcadm.sh; C=/tmp/kcadm-$$.cfg; trap "rm -f $C" EXIT
   $K config credentials --config $C --server http://localhost:8080 --realm master \
     --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null &&
   $K "$@" --config $C' kc "$@"; }
kc get realms/relay --fields realm,registrationAllowed,verifyEmail,bruteForceProtected,passwordPolicy
```

## Admin console

```bash
kubectl --context admin@ai -n keycloak port-forward deploy/keycloak 8080:8080
# browser: http://localhost:8080/admin/   (master realm, the bootstrap admin)
```

`KC_HOSTNAME_ADMIN=http://localhost:8080` makes the console build its URLs for the port-forward.
The public host never serves it.

**The bootstrap admin** is the break-glass master admin. Its values are `KC_BOOTSTRAP_ADMIN_*` in
`keycloak-secrets.sops.yaml`. Keycloak creates the account **only at the first start, and only while
the master realm has no admin**. Editing those values later changes nothing live. To rotate:

1. Run `kc set-password -r master --username kc-admin --new-password …` with a value read from a
   private file, never typed into argv on a shared host.
2. Update the SOPS file in the same PR.

The console shows a "temporary admin" banner for this account. That is accepted (ADR 0038 decision
6).

## First deployment (once)

1. **Merge the PR.** Flux creates `infra-pg-keycloak` (the CNPG role reconciles within seconds) and
   the `keycloak` namespace. The pod then waits, failing its DB connection, until step 2.
2. **Create the database** on the current primary:

   ```bash
   P=$(kubectl --context admin@ai -n databases get cluster infra-pg -o jsonpath='{.status.currentPrimary}')
   kubectl --context admin@ai -n databases exec -i "$P" -c postgres -- \
     psql -U postgres -v ON_ERROR_STOP=1 -f - < kubernetes/apps/databases/keycloak-db-bootstrap.sql
   ```

3. **Publish DNS.** In `kubernetes/infra/cloudflare`:
   - First check that `id.chifor.me` does not resolve yet. The state must be the existing operator
     state, never an empty one.
   - Use a **targeted** apply:

     ```bash
     tofu plan  '-target=cloudflare_dns_record.tunnel["id"]'   # must be: 1 to add, 0 to change, 0 to destroy
     tofu apply '-target=cloudflare_dns_record.tunnel["id"]'
     ```

   On 2026-10-13 an untargeted plan against the operator state also carried unrelated drift: a
   pending `relay` record import, two **destroys** of the live `trueswarm-admin-e2e` Access service
   token and policy, a `trueswarm_admin` precondition error, and a cosmetic
   `include_shadow_metadata` update on every record. A full apply would have deleted that token.
   Reconcile that drift separately, with its owner.
4. **Verify.** Record each result, with no values:
   - The pod is 1/1 Ready, and the log contains the realm `relay` import.
   - Each management probe, `/health/started`, `/health/ready` and `/health/live`, returns 200 over
     `kubectl port-forward deploy/keycloak 9000:9000`.
   - `curl -s https://id.chifor.me/realms/relay/.well-known/openid-configuration`: `issuer` is
     exactly `https://id.chifor.me/realms/relay`, and `code_challenge_methods_supported` contains
     `S256`.
   - `curl -o /dev/null -w '%{http_code}' https://id.chifor.me/admin/` returns `404`, and so does
     `https://id.chifor.me/realms/master/`. `https://id.chifor.me/realms/relay/account` returns 200.
   - The client matches the JSON:
     `kc get clients -r relay -q clientId=relay --fields clientId,publicClient,standardFlowEnabled,implicitFlowEnabled,directAccessGrantsEnabled,serviceAccountsEnabled,redirectUris,webOrigins,attributes`.
   - The client secret was substituted. Compare the sha256 prefix of
     `kc get clients/<id>/client-secret -r relay` (field `value`) with that of
     `RELAY_OIDC_CLIENT_SECRET` from `sops -d`. A literal `${RELAY_OIDC_CLIENT_SECRET}` means the
     placeholder was not resolved.

## Realm changes (the import never re-applies)

`--import-realm` imports `realm-relay.json` **only while the realm does not exist**. Editing the file
renames its ConfigMap and restarts the pod, and the live realm stays exactly as it was. So every
change follows these steps:

1. Make the change live with `kc update …`, and read it back.
2. In the same PR, write the setting back into `realm-relay.json`. The JSON holds realm and client
   **settings only**: never users, password hashes, or a real secret. The client secret stays the
   `${RELAY_OIDC_CLIENT_SECRET}` placeholder.

**Drift check.** Diff what the JSON declares against what is live:

```bash
kc get realms/relay > /tmp/live-realm.json      # then compare the keys realm-relay.json sets
kc get clients -r relay -q clientId=relay > /tmp/live-client.json
```

Each top-level key of `realm-relay.json`, and each field of its `relay` client except `secret`,
must equal the live value.

## Client-secret rotation

The order matters, and the import does NOT apply a changed Secret.

1. Generate the new value into a private file. Keep the old value until step 5. It is still in git
   history (SOPS) for rollback.
2. Set it live: `kc update clients/<id> -r relay -s "secret=<new>"`, from the pod side, passing the
   value via a file, never via argv on the workstation.
3. Verify: the sha256 prefix of `kc get clients/<id>/client-secret -r relay` equals the new value's.
   From this moment Relay sign-ins fail until step 4 lands, so do 2–4 together.
4. One PR updates both `OIDC_CLIENT_SECRET` in `apps/relay/relay-secrets.sops.yaml` and
   `RELAY_OIDC_CLIENT_SECRET` here. Relay restarts on its Secret change; confirm that it does, and
   restart it otherwise.
5. Rollback: `kc update` back to the old value, then revert the PR.

## Members (Relay)

Signing up at `id.chifor.me` grants nothing in Relay. To admit a person:

1. They sign up at `https://id.chifor.me/realms/relay/account` → Register, or through Relay's
   sign-in button → Register.
2. Confirm the person **out of band**. Then read and record the exact user:
   `kc get users -r relay -q username=<email> -q exact=true --fields id,username,email,emailVerified,createdTimestamp`.
   Exactly one user must match. Its `id` is the OIDC `sub`.
3. Mark the email verified **on that id**: `kc update users/<id> -r relay -s emailVerified=true`.
   Relay runs with `OIDC_REQUIRE_EMAIL_VERIFIED=1`.
4. Bind the exact subject in Relay. Never derive it from an email:

   ```bash
   kubectl --context admin@ai -n relay exec deploy/relay -c relay -- node dist/scripts/member.js \
     --subject 'https://id.chifor.me/realms/relay|<id>' --name '<display name>' --role <viewer|operator|admin>
   ```

To remove someone, delete or disable the Keycloak user AND remove the Relay membership. A Relay
session lasts up to 8 h and is not revoked by Keycloak.

**Password reset.** There is no SMTP, so this is operator-only:
`kc set-password -r relay --userid <id> --new-password <value> --temporary`, with the value given out
of band. The user must change it at the next sign-in.

## Abuse watch

Sign-up is open (ADR 0038 decision 5). Review periodically, and whenever Relay reports unexpected
sign-in denials:

```bash
kc get users -r relay --fields id,username,createdTimestamp,emailVerified -q briefRepresentation=true   # recent sign-ups
kc get attack-detection/brute-force/users/<id> -r relay                                                 # lockout state
```

A burst of sign-ups is the trigger to revisit decision 5 (captcha or a Cloudflare rate limit on
`/realms/relay/login-actions/registration`). Delete junk accounts with `kc delete users/<id> -r relay`.

## Upgrades, backup and restore

- **Upgrade:** bump the pinned digest in `keycloak.yaml`, following Keycloak's upgrade notes for
  every minor version crossed. The pod migrates its own schema at start. Take a fresh `infra-pg-dump`
  run first.
- **Restore:** restore the `keycloak` database from the `infra-pg-dump` generation with the pod
  scaled to 0, then scale back up. The realm, users and client secret all live in the database.
  The JSON import only matters on an empty database.
