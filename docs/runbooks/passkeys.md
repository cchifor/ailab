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

The Relying Party ID is the **session cookie domain, `chifor.me`** — so a single registered credential
covers every app in the estate. Credentials are stored in infra-pg (`webauthn_credentials`), not on a
pod, so both Authelia replicas see them and a reschedule doesn't lose them.

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

A revoked passkey does not end an existing session — the cookie lives until `expiration` (12h). To
cut sessions estate-wide, bounce the session store: `kubectl --context admin@ai -n auth rollout
restart deploy/auth-valkey` (accepted-ephemeral; costs everyone one re-login).

## Session lifetimes

`session.cookies[0]` in `authelia-config.yaml`:

| Setting | Value | Meaning |
|---|---|---|
| `inactivity` | 8 hours | idle time before the session dies |
| `expiration` | 12 hours | hard cap regardless of activity |
| `remember_me` | 1 month | only if you tick the box — and it overrides **`expiration` only** |

The trap worth knowing: `remember_me` does **not** suspend `inactivity`. They are independent timers,
which is why the old `inactivity: 5 minutes` logged you out of a remembered session after five idle
minutes.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| No "passkey" option on the login page | Config not reconciled. `kubectl --context admin@ai -n auth get cm authelia-config -o yaml \| grep enable_passkey_login`, then `rollout restart deploy/authelia`. |
| `notification.txt` missing or stale | You read the wrong replica — loop over both (above). The file is a per-pod `emptyDir` and is lost on restart. |
| "elevation has expired" | More than 10 minutes passed since the code. Start over at step 2. |
| Hello prompt never appears | The browser must reach `sso.chifor.me` over **HTTPS** with the real hostname — WebAuthn is origin-bound and will not fire on an IP or a port-forward. |
| Locked out after 3 tries | `regulation`: 3 retries in 2 minutes = a 5-minute ban. Wait it out; the ban is in `banned_user`. |
