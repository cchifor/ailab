# Relay Router connection for the first researcher worker (inactive)

This is the fixed, reviewed record of the Relay → Router connection that the first repository-free
researcher worker (`relay-researcher`, `gpt-6-luna`) will use, the Router's trust mapping for it, the
OpenBao escrow writer, and the order in which the operator brings them up. Owner approval for creating
the connection, binding and reader identity: [relay#277 comment 120817](https://git.chifor.me/cchifor/relay/issues/277#issuecomment-120817)
(caps in 120818 as corrected, amendments in 120903), 2026-11-14.

**Nothing here is active.** Merging installs only inert objects: three OpenBao policies, two
k8s-auth roles and one token role (no document, no token), and two ExternalSecrets that stay
not-Ready until the operator writes their documents. Both consuming components are un-included, the
live Relay overlay still refuses a router connection (`test_relay_v060_activation_gate.py`), and the
Relay connection is the **last** step below.

| Source | Flux Kustomization | Merge effect |
| --- | --- | --- |
| `apps/infrastructure/security/openbao/relay-router-provision-job.yaml` | `openbao` | Policies `relay-router-escrow`, `af-app-relay-router`, `af-app-router-relay-keys`; k8s-auth roles of the two `af-app-*` names; token role `relay-router-escrow`. No KV document, no token |
| `apps/relay-router-connection/credentials/` | `relay-router-credentials` (no wait) | SecretStore + ExternalSecret in `relay` and `llm-router`, not Ready until their documents exist |
| `components/relay-router-connection` | none (not included) | Nothing until the last step |
| `components/llm-router-relay-keys-trust` | none (not included) | Nothing until step 6 |

`scripts/tests/test_relay_router_connection.py` pins the record, the digest, the trust mapping,
the mounts, the policies and the inclusion gates; `scripts/tests/test-relay-router-provision.sh`
runs the provision script against a real `bao server -dev`;
`scripts/tests/test-relay-router-connection-render.sh` renders both components onto the live
Deployments.

## The connection record

`kubernetes/components/relay-router-connection/connections.json`, read by Relay through
`RELAY_ROUTER_CONNECTIONS_FILE` (Relay `server/router-credentials/transport.ts`, `RouterDeployment`):

| Field | Value | Why |
| --- | --- | --- |
| `id` | `babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8` | Generated once with `uuidgen -r` on 2026-11-14. Never regenerate it. |
| `name` | `ailab llm-router` | Display only. |
| `tenants` | `["00000000-0000-4000-8000-000000000001"]` | The workspace tenant. |
| `origin` | `https://router.chifor.me` | The public Router origin, through Cloudflare and `llm-router-edge`, which passes `/register/relay-keys`. Relay's egress baseline already allows public 443. `llm-router.chifor.me` does not resolve. |
| `issuer` | `relay.chifor.me` | The `iss` of every management assertion; the Router trust and the ledger `clients.issuer` use the same string. |
| `kid` / `reconcilerKid` | `relay-admin-2026-11` / `relay-reconciler-2026-11` | Two separate Ed25519 signers; the reconciler cannot create, list or inspect. |
| `signingKeyFile`, `reconcilerKeyFile` | `/run/relay-router/admin-key.pem`, `/run/relay-router/reconciler-key.pem` | Read-only subPath files from Secret `relay/relay-router-files`. |
| `baoOrigin` | `https://openbao.openbao.svc.cluster.local:8200` | The in-cluster OpenBao Service. Its certificate (`openbao-tls`) names it and is issued by `ailab-ca`, so the component sets `NODE_EXTRA_CA_CERTS` to that CA and adds one egress policy to the OpenBao server pods on 8200. Not `openbao.chifor.me` (behind Cloudflare Access) and not the LAN NodePort (Relay's egress excludes RFC1918). |
| `baoTokenFile` | `/run/relay-router-escrow/token` | A directory mount, so a rotated token reaches the running Relay. |
| `baoMount` / `baoPrefix` | `af` / `relay-router/escrow` | The estate KV-v2 mount (there is no `secret` mount). Escrow paths are `af/data/relay-router/escrow/<tenant>/<host-or-assistant>/<credential-id>`. |

**Identity.** Relay hashes `{id, origin, issuer, baoOrigin, baoMount, baoPrefix}` into the deployment
digest and persists it with the first credential it issues on this connection. A later change to any
of those six fields is refused ("Router deployment identity changed; reconcile its configuration") and
invalidates the ledger rows below, so it is a **new connection**, not an edit. `name`, the kids and the
file paths can change without that.

**Public-origin trust.** The Router origin is public. The worker side reaches it with the pinned
complete Mozilla CA bundle (`https://curl.se/ca/cacert.pem`, SHA-256
`427c064216761a1e72a8b30f0d954d08cb6e4435a82091fb865afd20c8497285`, vendored by the researcher
image; relay#277 comment 120903). Relay's own process keeps Node's bundled roots for it:
`NODE_EXTRA_CA_CERTS` only **adds** `ailab-ca` for the private OpenBao origin. Pinning Relay itself to
that exact bundle would change trust for all of Relay's outbound TLS and is a separate decision.

## Deployment digest

```text
547353ac649b454646cad921598d8e231c5e9db41c8e99b028351969e1a261ad
```

Computed with Relay's own functions on the committed record, not by hand. Source: Relay `origin/main`
`ccc4020e7` (`server/router-credentials/service.ts` blob `55244bf8`, `transport.ts` blob `d4fd588f`),
run from a Relay checkout at `e75d977e5` whose two blobs are identical:

```sh
cat > digest.mts <<'EOF'
import { loadRouterDeployments } from "/path/to/relay/server/router-credentials/transport.ts";
import { deploymentDigest } from "/path/to/relay/server/router-credentials/service.ts";
for (const d of await loadRouterDeployments(process.argv[2])) console.log(d.id, deploymentDigest(d));
process.exit(0);
EOF
/path/to/relay/node_modules/.bin/tsx digest.mts kubernetes/components/relay-router-connection/connections.json
```

`loadRouterDeployments` validates the file with Relay's schema first, so the digest is of the parsed
record. An independent check (Relay's `canonicalJson` is sorted keys with `JSON.stringify` spacing):

```sh
python3 -c 'import json,hashlib,sys; d=json.load(open(sys.argv[1]))[0]; print(hashlib.sha256(json.dumps({k:d[k] for k in ("id","origin","issuer","baoOrigin","baoMount","baoPrefix")},sort_keys=True,separators=(",",":")).encode()).hexdigest())' kubernetes/components/relay-router-connection/connections.json
```

Recompute it yourself from the merged record before using it anywhere. It goes, unchanged, into:

- `worker_budget.readers.deployment_digest` for `relay_budget_router`;
- `worker_budget.clients.deployment_digest` for `relay_budget_allocator`;
- `worker_budget.qualifications.deployment_digest` of every qualification row;
- `ROUTER_WORKER_BUDGET_DEPLOYMENT_DIGEST` in `kubernetes/components/llm-router-worker-budget-reader`.

**Why this PR does not write it into that component.** Its placeholder stays.
`test_router_component_is_not_live_with_a_placeholder_digest` requires a 64-hex digest when the
component is included and refuses one while it is not ("a real digest belongs in the same PR that
includes the component"). The operator's inclusion PR (step 5) replaces the placeholder with this
value.

## Router trust mapping

`kubernetes/apps/relay-router-connection/credentials/llm-router.yaml` renders `trust.json`
(llm-router `packages/plugins/relay-keys/contracts.ts`, `Trust`). Git fixes everything except the two
public keys:

- issuer `relay.chifor.me`; key `relay-admin-2026-11` with roles `[admin]`, key
  `relay-reconciler-2026-11` with roles `[reconciler]`;
- tenant `00000000-0000-4000-8000-000000000001`, Router namespace `default` (every live route and key);
- `routes: ["gpt-6-luna"]`, `access: ["api"]`, `maxLifetimeSeconds: 86400`, `maxKeys: 4`;
- `workerBudgets: {maxTokens: 8 × (I + 8192), maxAuthoritySeconds: 120, maxRequests: 8}` (comment
  120818 D). `maxTokens` is the placeholder string `operator-sets-8x(I+8192)` until qualification
  proves I, the provider-enforced input bound. A string fails the Router's schema, so the trust file
  is refused outright, and the activation test refuses including the component with it.

`maxLifetimeSeconds` 86400 and `maxKeys` 4 are conservative ceilings for one worker at a time with
a rotation overlap: review them against the researcher pool's lifetime before step 6. The pool
inference policy's grant (`routes`, `access`, `lifetimeSeconds`) must be a subset of this ceiling.

### Route precondition (stop until resolved)

The decided selection is `{kind: "model", model: "gpt-6-luna"}` with no route. With the current
sources that means the Router route **named** `gpt-6-luna` in namespace `default` serves the worker,
not `codex-api-1`:

- Relay sends `requestedModel = selection.model` and the grant's `routes` to the Router's
  `worker-budget-preflight` (Relay `server/workers/pool-readiness.ts`,
  `server/workers/runtime-session/profiles.ts`), and the Router refuses unless `routes` includes
  `requestedModel` and every route exists in the namespace (llm-router
  `packages/plugins/relay-keys/service.ts`). A grant of `["codex-api-1"]` is refused there.
- At inference a Relay key is always route-scoped. A request for model `gpt-6-luna` without
  `X-Router-Route` is route mode on the route of that name, and must be one of the key's routes
  (`packages/modules/inference/index.ts` `authorize`; `packages/plugins/api-openai/index.ts`
  `selectRoute`, `codexNativeRoute`: a route of that name wins over any mapping).
- Model mode (`selection.route: codex-api-1`) is not available to a budgeted worker: the preflight
  checks route aliases only ("Model selectors/default routes are not currently issued by this
  bridge").

In the 2026-10-10 notes of `docs/runbooks/llm-router.md` the `gpt-6-luna` route is a shared route
of the Codex **subscription** accounts, which the worker budget refuses. Before step 6, the operator reads
the live route (`GET /admin/v1/config` or the console) and records its targets, access list and
deployment IDs. Then, as an owner decision, one of:

1. the route `gpt-6-luna` allows `api` access and its **API** targets are exactly the qualified
   `openai-2` / `gpt-6-luna` deployment(s); the key's `access: ["api"]` keeps the worker off its
   subscription targets at preflight and at inference. Any other API target on that route would be
   reachable by the worker too, so it must be qualified or removed; or
2. a dedicated route alias is chosen for the worker, and the profile selection, the pool grant routes
   and this trust's `routes` change together (none of them is in the deployment digest).

Either way, the qualification evidence and every `QualifiedTokenPolicy` must name the **account and
deployment ID of the target in the route the worker resolves to**: the Router matches
`accountId`, `deploymentId` and `model` against that route's targets, so a policy that names a
`codex-api-1` deployment ID does not qualify a target of route `gpt-6-luna`.

## OpenBao documents the operator fills

All in the KV-v2 mount `af`. The provision Job creates none of them.

| Path | Field(s) | Content | Written by | Read by |
| --- | --- | --- | --- | --- |
| `relay-router/relay/admin-signer` | `private_key` | Ed25519 private key, PKCS#8 PEM (kid `relay-admin-2026-11`) | operator, step 3 | `af-app-relay-router` (SA `relay/relay-router-eso`) |
| `relay-router/relay/reconciler-signer` | `private_key` | Ed25519 private key, PKCS#8 PEM (kid `relay-reconciler-2026-11`) | operator, step 3 | `af-app-relay-router` |
| `relay-router/router/trust` | `admin_public_key`, `reconciler_public_key` | the two SPKI public keys, PEM | operator, step 3 | `af-app-router-relay-keys` (SA `llm-router/router-relay-keys-eso`) |
| `relay-router/relay/escrow-token` | `token` | a token minted from token role `relay-router-escrow` | operator, step 9 | `af-app-relay-router` |
| `relay-router/escrow/<tenant>/<host-or-assistant>/<credential-id>` | `token`, `router_key_id`, `operation_id` | escrowed Router credential | **Relay only** (`relay-router-escrow`), never by hand | Relay (`relay-router-escrow`) |

The escrow writer policy is, exactly:

```hcl
path "af/data/relay-router/escrow/00000000-0000-4000-8000-000000000001/+/+" { capabilities = ["create", "read", "update"] }
path "af/destroy/relay-router/escrow/00000000-0000-4000-8000-000000000001/+/+" { capabilities = ["update"] }
```

`+` is one path segment: host (connector UUID or `assistant`) and credential ID. No list, metadata,
delete, other tenant, other depth or other path. Relay writes each generation with `cas=0` and never
overwrites; it destroys a confirmed-revoked version.

Signer keys, in a private directory of the operator's session (never a shared path, never echoed):

```sh
umask 077; d="$(mktemp -d)"
openssl genpkey -algorithm ed25519 -out "$d/admin.pem"
openssl genpkey -algorithm ed25519 -out "$d/reconciler.pem"
openssl pkey -in "$d/admin.pem" -pubout -out "$d/admin.pub"
openssl pkey -in "$d/reconciler.pem" -pubout -out "$d/reconciler.pub"
bao kv put -mount=af relay-router/relay/admin-signer private_key=@"$d/admin.pem"
bao kv put -mount=af relay-router/relay/reconciler-signer private_key=@"$d/reconciler.pem"
bao kv put -mount=af relay-router/router/trust admin_public_key=@"$d/admin.pub" reconciler_public_key=@"$d/reconciler.pub"
shred -u "$d"/*; rmdir "$d"
```

Escrow token, minted only at step 9 (it starts its 720 h lifetime at once):

```sh
umask 077; d="$(mktemp -d)"
bao token create -role=relay-router-escrow -display-name=relay-router-escrow -format=json > "$d/t.json"
jq -r .auth.client_token "$d/t.json" | bao kv put -mount=af relay-router/relay/escrow-token token=-
jq -r .auth.accessor "$d/t.json"     # record the accessor (not secret) in the operator evidence
shred -u "$d/t.json"; rmdir "$d"
```

**Rotation.** Relay never renews the token. Before 75 % of its 720 h (day 22), mint a new one the same
way (a new KV version), force the ExternalSecret to sync
(`kubectl --context admin@ai -n relay annotate externalsecret relay-router-files force-sync="$(date +%s)" --overwrite`),
confirm the Secret changed and wait two minutes for the kubelet to swap the file, then
`bao token revoke -accessor <old accessor>`. An expired token makes escrow, delivery and cleanup fail
closed (`remote_unavailable`) until a valid token is in place.

## Order, with the operator's readback for each step

Every step is its own reviewed change or operator action. A failed readback stops the sequence.
Budget-runbook step numbers refer to `docs/runbooks/relay-worker-budget-authority.md`.

1. **Record fixed (this PR merged).** Readback: the `openbao-relay-router-provision` Job log ends with
   `no KV document and no token written`; `bao policy read relay-router-escrow` is exactly the HCL
   above; `bao read auth/token/roles/relay-router-escrow` shows `allowed_policies [relay-router-escrow]`,
   `orphan true`, `renewable false`, `token_no_default_policy true`, `token_explicit_max_ttl 720h`;
   `kubectl --context admin@ai get externalsecret -n relay relay-router-files` and
   `-n llm-router llm-router-relay-trust` exist and are not Ready (missing document); no Pod changed.
2. **Digest.** Recompute it from the merged `connections.json` with both commands above and compare
   with the value recorded here. Record it in the operator evidence.
3. **Signer keys and trust document** (section above). Readback, printing no private material:

   ```sh
   for r in admin reconciler; do
     bao kv get -mount=af -field=private_key relay-router/relay/$r-signer | openssl pkey -pubout -outform DER | sha256sum
     bao kv get -mount=af -field=${r}_public_key relay-router/router/trust | openssl pkey -pubin -outform DER | sha256sum
   done
   ```

   Each role's two fingerprints are equal and the admin and reconciler fingerprints differ.
   `llm-router-relay-trust` becomes Ready; its `trust.json` (public keys only) parses as JSON and
   still carries the `maxTokens` placeholder. `relay-router-files` stays not Ready (no token yet).
4. **Readers row** (budget step 7): `relay_budget_router` → this digest.
   Readback: `SELECT login, deployment_digest FROM worker_budget.readers;`
5. **Router budget-reader inclusion** (budget step 8): one PR includes
   `components/llm-router-worker-budget-reader` and replaces its placeholder with this digest.
   Recreate rollout; the `worker-budget-authority` plugin stays disabled. Readback: the router's
   `ROUTER_WORKER_BUDGET_DEPLOYMENT_DIGEST` equals the digest, `GET /admin/v1/plugins` shows the plugin
   disabled, the mounted files' SHA-256 equal the Secret's.
6. **Qualification, then Router trust inclusion.** After the route precondition is resolved and
   qualification proves I (comment 120818 A/B): the `qualifications` rows (keyed on this digest) and
   the `clients` row for `relay_budget_allocator` (issuer `relay.chifor.me`, connection
   `babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8`, this digest, tenant `00000000-0000-4000-8000-000000000001`,
   `max_tokens` 8 × (I + 8192), `max_requests` 8, `max_authority_ms` 120000). Then one PR sets
   `maxTokens` to the same 8 × (I + 8192), includes `components/llm-router-relay-keys-trust` and
   amends `test_relay_v060_activation_gate.py` for that component only. Readback:
   `SELECT login, issuer, connection_id, deployment_digest, tenant_ids, max_tokens, max_requests, max_authority_ms FROM worker_budget.clients;`;
   after the rollout an unsigned `POST https://router.chifor.me/register/relay-keys` is refused with
   403 `RELAY_DELEGATION_DENIED` (before inclusion the plugin does not serve that path),
   `GET /admin/v1/plugins` shows `relay-keys` active, the log has no `Invalid RELAY_KEYS_TRUST_FILE`
   error, and the mounted `trust.json` SHA-256 equals the Secret's.
7. **Budget authority ready** (budget steps 8-9): `worker_budget.authority.ready=true` after independent
   review, then the router plugin enabled through the admin API. Readback as in that runbook
   (`GET /admin/v1/plugins` active, `Worker budget authority available`).
8. **Researcher release deployed.** The reviewed Relay release that reads these settings (worker budget
   client, installations, pinned profile registry, `relay-researcher` profile with
   `connectionId babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8`, `protocol codex-responses`,
   `selection {kind: model, model: gpt-6-luna}`, pool inference policy grant within the ceiling above),
   through its own PRs and pre-migration backup. Readback per that release's runbook.
9. **Escrow token** (section above). Readback, with the admin token:
   `bao token lookup -accessor <accessor>` shows `policies [relay-router-escrow]`, `orphan true`,
   `renewable false` and its `expire_time`;
   `bao write sys/capabilities-accessor accessor=<accessor> paths=af/data/relay-router/escrow/00000000-0000-4000-8000-000000000001/x/y`
   is `create, read, update`, and the same for `af/data/relay-budget/witness` is `deny`.
   `relay-router-files` becomes Ready with exactly `admin-key.pem`, `reconciler-key.pem`,
   `escrow-token`.
10. **Relay connection, last.** One PR includes `components/relay-router-connection` in
    `kubernetes/apps/apps/relay`, and deliberately amends `test_relay_v060_activation_gate.py` and
    `InclusionGate.test_relay_component_is_not_included`. Only with ready=true (step 7) and the
    researcher release (step 8) live. Recreate rollout with the pre-migration backup. Readback: the
    Relay Pod has `RELAY_ROUTER_CONNECTIONS_FILE` and `NODE_EXTRA_CA_CERTS`; the mounted
    `connections.json` equals the committed file; NetworkPolicy `relay-router-openbao-egress` exists;
    the admin `GET /api/router-credentials/connections` lists `babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8`
    with origin `https://router.chifor.me` (when the release enables the control plane). The
    connection row and the `relay-researcher` binding are created by the first budgeted worker
    issuance, never by hand; afterwards
    `SELECT id, origin, deployment_digest FROM router_connections;` (tenant-scoped) shows this digest.

## Rollback

- Before step 10, reverting an inclusion PR is enough; a Router trust without the Relay keys in use
  issues nothing.
- To abandon the connection, deleting the OpenBao documents is not enough: ESO keeps the last
  rendered Secrets (`deletionPolicy: Retain`), so `relay/relay-router-files` would keep the private
  keys. In order: revert both inclusions; revoke every minted escrow token by accessor
  (`bao token revoke -accessor <accessor>`); remove `clusters/ai/relay-router-credentials.yaml` in a
  reviewed PR, so Flux prunes the two ExternalSecrets and Kubernetes garbage-collects the Secrets
  they own (`creationPolicy: Owner`); confirm with
  `kubectl --context admin@ai -n relay get secret relay-router-files` and
  `-n llm-router get secret llm-router-relay-trust` (both NotFound; delete them explicitly if not);
  then delete the documents (`bao kv metadata delete -mount=af relay-router/relay/admin-signer`, and
  `reconciler-signer`, `escrow-token`, `relay-router/router/trust`).
- After any issuance, revoke the credentials through Relay first and confirm the Router rejects them:
  removing the components does not revoke issued keys, and Relay needs the connection files to finish
  revocation and escrow cleanup. Keep them until no pending job remains.
- Removing `relay-router-provision-job.yaml` from git does not remove its policies or roles, and does
  not revoke a token; address those objects in OpenBao explicitly.
- A new key pair needs new kids in `connections.json` and the trust, and a rollout of both; it does
  not change the digest.
