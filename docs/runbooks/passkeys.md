# Runbook: passkeys (Windows Hello) for estate SSO

How to log into the `*.chifor.me` apps with a **face/PIN scan instead of a password**, and the one-time
ceremony that registers the credential. Config: `kubernetes/apps/apps/auth/authelia-config.yaml`
(`webauthn:` + `identity_validation.elevated_session`). Design: **ADR 0012**. The Cloudflare-gated
hosts: `docs/runbooks/cloudflare-access-apps.md`.

**Context for every command below:** `kubectl --context admin@ai` (the default context flip-flops —
always pass it explicitly), namespace `auth`.

## What this is

A passkey is a WebAuthn credential. On Windows the platform authenticator **is Windows Hello**, so
"register a passkey" and "log in with my face" are the same sentence. Authelia 4.39 can accept one
*in place of* the username+password (`webauthn.enable_passkey_login`), and because the default policy
is `one_factor`, a passkey alone is a complete login.

Because a passkey is the *whole* login here, `selection_criteria.user_verification` is pinned to
`required`: the authenticator has to prove a PIN or biometric, not merely that it is present. Windows
Hello always verifies, so nothing changes for it; a roaming security key must have a PIN set. Left at
the `preferred` default, a PIN-less key could have logged in on possession alone.

The Relying Party ID is the **portal hostname, `sso.chifor.me`** — Authelia takes it from the origin of
the request, not from the cookie domain. A single registered credential still covers every app in the
estate, but through the session cookie rather than the RP ID: every WebAuthn ceremony happens on the
portal, and what the apps see afterwards is the `chifor.me` cookie. Credentials are stored in infra-pg
(`webauthn_credentials`), not on a pod, so both Authelia replicas see them and a reschedule doesn't
lose them.

| Gate | Hosts | Login after this change |
|---|---|---|
| **Authelia** | `git` `chat` `grafana` `home` `registry` `agentforge` | **Passkey** (Windows Hello), password still accepted |
| **Cloudflare Access** | `dw1`–`dw6` `dsh` `k8s` `hubble` `proxmox` `qnap` `prometheus` `alertmanager` `openbao` `vault/admin` | Still an **emailed one-time PIN** until Authelia is wired as the Access OIDC IdP |

## One-time: register Windows Hello

Registering a credential requires an **elevated session**, and Authelia proves elevation with a
One-Time Code sent through the `notifier`. There is no SMTP relay in this estate, so the notifier is
`filesystem` and the code must be read out of the pod by hand. That is the whole awkward part.

1. Log into <https://sso.chifor.me> with your **password** as usual.
2. Go to <https://sso.chifor.me/settings/security> → add a **passkey / WebAuthn credential**.
3. Authelia says it sent a code. Read it — **check both replicas**, the code lands on whichever one
   served the request:

   ```bash
   for p in $(kubectl --context admin@ai -n auth get pods \
                -l app.kubernetes.io/name=authelia -o name); do
     echo "== $p"
     kubectl --context admin@ai -n auth exec "$p" -- cat /data/notification.txt 2>/dev/null | tail -20
   done
   ```

4. Paste the code. You now have ~10 minutes of elevated session (`elevation_lifespan`); the code
   itself is good for 15 (`code_lifespan`, raised from the 5-minute default precisely because of the
   exec dance above).
5. Name the credential after the machine (`win-desktop`, `laptop`) — you will be reading these names
   later when revoking one.
6. Windows Hello prompts for face/PIN. Done.

Hello is **device-bound**: a credential registered on the desktop does not exist on the laptop. Repeat
per machine, or use the cross-device (QR) flow to register a phone and scan from any of them.

## Daily use

At the Authelia login page, choose **sign in with a passkey** → Hello prompts → you are in. The
password form is still there as a fallback and nothing about it changed.

## Revoking a lost device

Delete the credential in <https://sso.chifor.me/settings/security>. If you cannot log in to get there,
delete it in the database (this is the break-glass path):

```bash
# the primary moves — never hardcode infra-pg-N
PG=$(kubectl --context admin@ai -n databases get pod \
       -l cnpg.io/cluster=infra-pg,role=primary -o jsonpath='{.items[0].metadata.name}')
kubectl --context admin@ai -n databases exec "$PG" -c postgres -- \
  psql -U postgres -d authelia -c \
  "select id, description, created_at, last_used_at from webauthn_credentials;"
# then, with the id you mean to kill:
#   delete from webauthn_credentials where id = <id>;
```

A revoked passkey does not end an existing session — the cookie lives out its own clock, so worst case
is **12h** (see Session lifetimes below). Deleting the credential is therefore not a containment step on
its own. To cut sessions immediately, bounce the session store: `kubectl --context admin@ai -n auth
rollout restart deploy/auth-valkey` (accepted-ephemeral; costs everyone one re-login). Do both whenever
a device is actually lost.

## Session lifetimes

`session.cookies[0]` in `authelia-config.yaml`:

| Setting | Value | Meaning |
|---|---|---|
| `inactivity` | 8 hours | idle time before the session dies |
| `expiration` | 12 hours | hard cap regardless of activity |
| `remember_me` | `-1` | **disabled** — there is no "remember me" box on the login form |

Why remember-me is off rather than at its old `1 month`: Authelia does **not** treat `remember_me` as a
longer version of the same clock. A remembered session is exempt from the inactivity check altogether
(Authelia skips it and stops stamping last-activity) and takes `remember_me` as its cookie lifetime in
place of `expiration`. Ticking the box would therefore have produced a month-long session with no idle
timeout at all, which would have made the 8h/12h pair above a fiction and left credential revocation
unable to cut existing access. With the box gone, **12h is the true worst case for any estate session**,
and the cost — one login a day — is a Hello face scan rather than a typed password.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| No "passkey" option on the login page | Config not reconciled. `kubectl --context admin@ai -n auth get cm authelia-config -o yaml \| grep enable_passkey_login`, then `rollout restart deploy/authelia`. |
| `notification.txt` missing or stale | You read the wrong replica — loop over both (above). The file is a per-pod `emptyDir` and is lost on restart. |
| "elevation has expired" | More than 10 minutes passed since the code. Start over at step 2. |
| Hello prompt never appears | The browser must reach `sso.chifor.me` over **HTTPS** with the real hostname — WebAuthn is origin-bound and will not fire on an IP or a port-forward. |
| Roaming key refused at registration or login | `user_verification: required` — the key needs a PIN. Set one in the vendor tool (or Windows *Settings → Passkeys*) and retry. Windows Hello is never affected. |
| Locked out after 3 tries | `regulation`: 3 retries in 2 minutes = a 5-minute ban. Wait it out; the ban is in `banned_user`. |
