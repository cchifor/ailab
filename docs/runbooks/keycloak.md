# Keycloak (`id.chifor.me`)

The ailab identity provider, decided in [ADR 0038](../decisions/0038-ailab-keycloak-for-relay.md).
There is one realm per application. Today the only one is `relay`, with issuer
`https://id.chifor.me/realms/relay`, used by Relay (`relay.chifor.me`).

The pending `llm-router` realm has a separate [two-phase activation runbook](llm-router-keycloak.md).
Provision its required Secrets before merging the prerequisite rollout; verify the new issuer and
administrator before enabling OIDC on the router. The router does not become a client in `relay`.

| Piece | Where |
|---|---|
| Deployment, Service, NetworkPolicy, realm JSON | `kubernetes/apps/apps/keycloak/` (Flux, `apps` layer) |
| Secrets (bootstrap admin, DB password, `relay` client secret) | `keycloak-secrets.sops.yaml` (SOPS is the system of record, not OpenBao) |
| Database | `keycloak` DB on CNPG `infra-pg`: role in `databases/infra-pg.yaml`, password `databases/infra-pg-keycloak.sops.yaml`, DB from `databases/keycloak-db-bootstrap.sql` |
| Public route | `apps/edge/cloudflared.yaml` (allowlist: `/realms/relay` + `/resources`; `..`/`;` and everything else 404) + `id` in `kubernetes/infra/cloudflare/variables.tf` |
| Monitoring | Gatus "Keycloak (id.chifor.me) OIDC discovery" |
| Backups | the nightly `infra-pg-dump` enumerates every database, `keycloak` included |

Everything below runs from the workstation with `kubectl --context admin@ai` (the default context is
a different cluster). **Never print a secret value.** Compare values by sha256 prefix.

## kcadm

Run kcadm inside the pod, against the pod's own HTTP port. The bootstrap credentials come from the
pod env, so they never cross the workstation. There are two wrappers:

```bash
# kc   - arguments only. NEVER put a secret in its arguments: kubectl exec sends argv to the API
#        server as URL query parameters (audit log), and your shell keeps it in history.
# kcin - the same, plus the request body on STDIN (`-f -`). Every secret goes this way, from a
#        private file you delete afterwards.
kc()   { kubectl --context admin@ai -n keycloak exec    deploy/keycloak -c keycloak -- sh -c "$KCADM" kc "$@"; }
kcin() { kubectl --context admin@ai -n keycloak exec -i deploy/keycloak -c keycloak -- sh -c "$KCADM" kc "$@"; }
KCADM='K=/opt/keycloak/bin/kcadm.sh; C=/tmp/kcadm-$$.cfg; trap "rm -f $C" EXIT
  $K config credentials --config $C --server http://localhost:8080 --realm master \
    --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null &&
  $K "$@" --config $C'
kc get realms/relay --fields realm,registrationAllowed,verifyEmail,bruteForceProtected,passwordPolicy
RELAY_CLIENT=$(kc get clients -r relay -q clientId=relay --fields id --format csv --noquotes | tr -d '\r')
```

Read a secret only through a hash, never to the terminal:

```bash
kc get clients/$RELAY_CLIENT/client-secret -r relay \
  | python3 -c 'import json,sys,hashlib; print(hashlib.sha256(json.load(sys.stdin)["value"].encode()).hexdigest()[:12])'
```

`kcin update clients/<id> -f -` and `kcin update users/<id>/reset-password -f -` were checked against
26.7.5 on 2026-10-13 (disposable local container).

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

1. Write `{"type":"password","value":"<new>","temporary":false}` to a private file. Then run
   `kcin update users/<kc-admin id>/reset-password -r master -f - < that-file` and delete the
   file. The id comes from `kc get users -r master -q username=kc-admin -q exact=true --fields id`.
2. Update `KC_BOOTSTRAP_ADMIN_PASSWORD` in the SOPS file in the same PR. That value is what the
   `kc` wrappers log in with, so the pod must restart onto the new Secret before they work again.

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
   - The edge allowlist works. Use `curl --path-as-is -o /dev/null -w '%{http_code}'` so curl does not
     normalize the paths itself.
     - Expect `404` for `/`, `/admin/`, `//admin/`, `/admin/realms/relay/users`, `/realms/master/`,
       `/realms//master/.well-known/openid-configuration`,
       `/realms/relay/../master/.well-known/openid-configuration`, `/realms/relay/%2e%2e/master/x`,
       `/realms/relay/x;y` and `/js/keycloak.js`.
     - Expect `200` for `/realms/relay/account` and for one `/resources/…` asset that the login page
       references.
     - Expect the registration form to render at `/realms/relay/login-actions/registration`, reached
       from the login page.
   - The client matches the JSON: `kc get clients/$RELAY_CLIENT -r relay` (the full object;
     `--fields attributes` prints `{}` because kcadm's field filter drops nested maps). Check
     `attributes."pkce.code.challenge.method"` = `S256`.
   - The client secret was substituted: its hash (see "kcadm") equals the sha256 prefix of
     `RELAY_OIDC_CLIENT_SECRET` from `sops -d`. Keycloak 26.7.5 resolves the bare
     `${RELAY_OIDC_CLIENT_SECRET}` placeholder at import. This was checked on 2026-10-13 in a
     disposable local container with this exact realm file, and the PKCE attribute and default scopes
     (`profile`, `email`, `basic`) came through too.
     - **If it stays literal anyway** (the hash equals the sha256 of the string
       `${RELAY_OIDC_CLIENT_SECRET}`), the import will not run again. Set the real value through the
       API: write `{"secret":"<value from sops -d>"}` to a private file, run
       `kcin update clients/$RELAY_CLIENT -r relay -f - < that-file`, delete the file, and re-check
       the hash.

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

1. Generate the new value into a private file as `{"secret":"<new>"}`. Keep the old value until
   step 5. It is still in git history (SOPS) for rollback.
2. Set it live: `kcin update clients/$RELAY_CLIENT -r relay -f - < that-file`, then delete the
   file. The value never goes into argv.
3. Verify that the client-secret hash (see "kcadm") equals the new value's. From this moment Relay
   sign-ins fail until step 4 lands, so do 2–4 together.
4. One PR updates both `OIDC_CLIENT_SECRET` in `apps/relay/relay-secrets.sops.yaml` and
   `RELAY_OIDC_CLIENT_SECRET` here. Relay restarts on its Secret change; confirm that it does, and
   restart it otherwise.
5. Rollback: `kcin update` back to the old value, the same way, then revert the PR.

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

1. Write `{"type":"password","value":"<temporary>","temporary":true}` to a private file.
2. Run `kcin update users/<id>/reset-password -r relay -f - < that-file`, then delete the file.
3. Give the value to the person out of band. Keycloak adds `UPDATE_PASSWORD`, so they must change it
   at the next sign-in.

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
