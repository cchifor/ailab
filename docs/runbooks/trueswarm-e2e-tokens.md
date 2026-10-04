# Trueswarm e2e tokens for dev-worker agents

How the dev-worker agents authenticate their Playwright runs against the live `trueswarm.chifor.me`
and `trueswarm-admin.chifor.me`. The decision and its reasoning are in
[ADR 0035](../decisions/0035-trueswarm-e2e-tokens-for-dev-worker-agents.md). Nothing here needs the
operator in steady state.

## Moving parts

| Piece | Where |
|---|---|
| Token sync (CronJob 03:41 UTC + bootstrap Job), ns `openbao` | `kubernetes/apps/trueswarm-e2e-tokens/` (Flux Kustomization `trueswarm-e2e-tokens`) |
| Vault login | k8s-auth role `trueswarm-e2e-sync`, policy `k8stoken-sync` (`devworker-provision-job.yaml`) |
| Hash files the apps read | Secret `trueswarm-e2e-tokens` (key `tokens.json`) in ns `trueswarm` and `trueswarm-admin` |
| Tokens the workers read | `af/dev-workers/dev-worker-<N>`: `trueswarm_e2e_token`, `trueswarm_admin_e2e_token` (+ `_valid_until`) |
| Cloudflare service token (admin only) | `af/dev-workers/common`: `trueswarm_admin_access_client_id`, `trueswarm_admin_access_client_secret` |
| App endpoints | `POST /auth/e2e` in cchifor/trueswarm and cchifor/trueswarm-admin |
| Live suite | cchifor/trueswarm-admin `web/playwright.live.config.ts` |

## On a dev worker

```sh
cred get "$(hostname -s)" trueswarm_e2e_token >/dev/null && echo "trueswarm token: present"
cred get "$(hostname -s)" trueswarm_admin_e2e_token >/dev/null && echo "admin token: present"
cred get common trueswarm_admin_access_client_id >/dev/null && echo "Cloudflare service token: present"

# From a trueswarm-admin checkout. The suite mints its own storage state from the tokens.
cd web && ADMIN_E2E_BASE_URL=https://trueswarm-admin.chifor.me \
  PLATFORM_E2E_BASE_URL=https://trueswarm.chifor.me \
  npx playwright test -c playwright.live.config.ts
```

By hand, never printing a token (`cred exec` puts it in the child's environment only):

```sh
cred exec "$(hostname -s)" trueswarm_e2e_token T -- sh -c \
  'curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Authorization: Bearer $T" https://trueswarm.chifor.me/auth/e2e'
```

Expected: `200` and a `trueswarm_session` cookie. The admin call also needs the
`CF-Access-Client-Id` and `CF-Access-Client-Secret` headers.

What an e2e principal can do:
- **trueswarm:** an ordinary signed-in user (`e2e dev-worker-<N>`).
- **trueswarm-admin:** operator, minus every operation that needs a fresh MFA. `infra.*`,
  `releases.*`, `credentials.revoke`, `connectors.*`, `jobs.*` and `configuration.*` return
  403 "Fresh MFA required". That is by design: ask the operator to perform them.

Name anything a test creates `e2e-dw<N>-<run>` and clean it up in teardown.

## Health

```sh
kubectl --context admin@ai -n openbao logs job/trueswarm-e2e-token-sync-bootstrap   # or the latest CronJob run
kubectl --context admin@ai -n openbao create job --from=cronjob/trueswarm-e2e-token-sync e2e-sync-manual
```

A healthy run ends with `validated 8/8 slot tokens (<n> rotated)`, which is 4 slots × 2 apps. It prints
slot and app names and dates, never a token. A re-run with nothing due is a no-op.

| Symptom | Cause / fix |
|---|---|
| `/auth/e2e` → 404 | The app has no `*_E2E_TOKENS_FILE`, or the Secret has not been written yet. Run the sync once. |
| 401 right after a rotation | The kubelet has not refreshed the mounted Secret yet (the sync waits 120 s). Retry. |
| Admin → 403 "service token" / "client id" | `ADMIN_ACCESS_CLIENT_IDS` is empty, or the Cloudflare headers are missing. See *Cloudflare service token* below. |
| Admin → 409 | No active human administrator exists, so the e2e login refuses to pre-empt bootstrap. |
| Sync: `changed under this run` | Two runs overlapped. The losing run published nothing. Re-run. |
| Sync: `OpenBao k8s-auth login failed` | The `trueswarm-e2e-sync` role is missing (devworker-provision has not run) or the vault is sealed. |

## Rotate and revoke

- **Rotate every slot now** (old hashes stay valid for one hour):

  ```sh
  kubectl --context admin@ai -n openbao create job --from=cronjob/trueswarm-e2e-token-sync e2e-rotate \
    --dry-run=client -o json \
    | python3 -c 'import json,sys; j=json.load(sys.stdin); j["spec"]["template"]["spec"]["containers"][0]["env"].append({"name":"FORCE_ROTATE","value":"1"}); print(json.dumps(j))' \
    | kubectl --context admin@ai apply -f -
  ```
- **One worker, immediately (admin):** an administrator deactivates `e2e:dev-worker-<N>` in the admin
  UI. The next e2e login does not reactivate it.
- **Retire a slot:** drop it from both `LIVE_SLOTS` copies in `token-sync.yaml`. This is part of
  ADR 0028's retire checklist, and `scripts/check-slot-enumerations.py` enforces it.
- **Every worker, admin, now:** revoke the Cloudflare service token in Zero Trust → Access → Service
  Auth, then re-run `--apply-e2e-access` to mint a new one.

## Cloudflare service token (one-time; re-run to rotate)

From the workstation that owns the Cloudflare state, with the ailab checkout on an up-to-date `main`:

```sh
TRUESWARM_ADMIN_CHECKOUT=/path/to/trueswarm-admin bash scripts/trueswarm-admin-access.sh --apply-e2e-access
```

The plan guard refuses anything except:
- creating the token and its `non_identity` policy;
- an in-place update of the existing admin Access application, keeping the private deployment's
  audience and policy precedences `[1, 2]`.

The IdP, the human MFA policy and the DNS record must be unchanged. The helper then rewrites two files
for you to ship in one ailab PR:
- `devworker-seeds.sops.yaml` (`common.json` gains the client ID and secret);
- `token-sync.yaml` (`ADMIN_ACCESS_CLIENT_IDS`).

The secret is never printed.
