# Relay Router connection for the first researcher worker (hub connection inactive)

This is the fixed, reviewed record of the Relay → Router connection that the first repository-free
researcher worker (`relay-researcher`, `gpt-6-luna` on the dedicated route `relay-researcher-luna`) will use, the Router's trust mapping for it, the
OpenBao escrow writer, and the order in which the operator brings them up. Owner approval for creating
the connection, binding and reader identity: [relay#277 comment 120817](https://git.chifor.me/cchifor/relay/issues/277#issuecomment-120817)
(caps in 120818 as corrected, amendments in 120903), 2026-11-14.

**State.** The signer keys and trust document are written, and the Router trust and budget-reader
components are included (`kubernetes/apps/apps/llm-router/kustomization.yaml`; remaining-live-steps
checklist steps 1-2 DONE). The hub release and budget client are deployed (H2-H4a). The Relay
connection is the **last** step below; its inclusion PR is hub upgrade step H6
([relay-researcher-hub-upgrade.md](relay-researcher-hub-upgrade.md)), which also took it off
`test_relay_v060_activation_gate.py`'s forbidden list. `relay-router-files` needs step 9's escrow token
before H6 merges.

| Source | Flux Kustomization | Merge effect |
| --- | --- | --- |
| `apps/infrastructure/security/openbao/relay-router-provision-job.yaml` | `openbao` | Policies `relay-router-escrow`, `af-app-relay-router`, `af-app-router-relay-keys`; k8s-auth roles of the two `af-app-*` names; token role `relay-router-escrow`. No KV document, no token |
| `apps/relay-router-connection/credentials/` | `relay-router-credentials` (no wait) | SecretStore + ExternalSecret in `relay` and `llm-router`, not Ready until their documents exist |
| `components/relay-router-connection` | `apps` (through `kubernetes/apps/apps/relay`, since hub step H6) | Step 10, the Relay connection |
| `components/llm-router-relay-keys-trust` | `apps` (included) | Step 6 complete; Router loads the public trust |

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

**TLS trust.** The Router origin is public. On the post-qualification production path, the worker
side reaches it with the pinned complete Mozilla CA bundle (`https://curl.se/ca/cacert.pem`, SHA-256
`427c064216761a1e72a8b30f0d954d08cb6e4435a82091fb865afd20c8497285`, vendored by the researcher
image; [relay#277 comment 120903](https://git.chifor.me/cchifor/relay/issues/277#issuecomment-120903)).
This owner-recorded choice still applies. During qualification only, G3's measured `hostAliases`
resolve the Router origin to the in-cluster terminator at `10.96.0.41`, and the pinned runtime and
template use the ailab root CA in `/etc/relay/hub-ca.pem`
(`docs/relay-workers/researcher-runtime-ca.crt`, `researcher-i4/binding.json`; installation Q1-Q5).

Relay's own process keeps Node's bundled roots for the public Router origin:
`NODE_EXTRA_CA_CERTS` only **adds** `ailab-ca` for the private OpenBao origin. Pinning Relay itself to
that exact Mozilla bundle would change trust for all of Relay's outbound TLS and is a separate decision.

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

**The reader digest is pinned.** Step 5 (ailab #1807) included the component with this value in
`kubernetes/components/llm-router-worker-budget-reader/deployment.yaml`.
`test_router_component_is_not_live_with_a_placeholder_digest` requires a 64-hex digest when the
component is included. There is no remaining placeholder to replace.

## Router trust mapping

`kubernetes/apps/relay-router-connection/credentials/llm-router.yaml` renders `trust.json`
(llm-router `packages/plugins/relay-keys/contracts.ts`, `Trust`). Git fixes everything except the two
public keys:

- issuer `relay.chifor.me`; key `relay-admin-2026-11` with roles `[admin]`, key
  `relay-reconciler-2026-11` with roles `[reconciler]`;
- tenant `00000000-0000-4000-8000-000000000001`, Router namespace `default` (every live route and key);
- `routes: ["relay-researcher-luna", "canary-pi-codex", "canary-pi-qwen", "canary-pi-claude"]`, `access: ["api", "subscription", "local"]`, `maxLifetimeSeconds: 86400`, `maxKeys: 8`. The three `canary-pi-*` routes use one one-route binding each ([relay-pi-canary-holder.md](relay-pi-canary-holder.md)). The Router serves a target only when the account's access is in both the key's and the route's access (llm-router `usableTarget`), and each of the four routes has one access kind and one deployment. Ending the canary is ordered, because the Router reads trust once at startup and narrowing it under live keys strands them: (1) drain canary launches and revoke all issued canary bindings in Relay; (2) wait until each credential reports `revoked` with cleanup done; (3) revert the trust change and apply it with a deliberate Router restart; (4) delete the three routes through the Router config API. `maxKeys` remains 8;
- `workerBudgets: {maxTokens: 8 × (I + 8192), maxAuthoritySeconds: 120, maxRequests: 8}` (comment
  120818 D), set to `maxTokens: 7441536`: I = 922,000, the gpt-6-luna input bound the owner accepted
  (comment 122095), so 8 × (922,000 + 8,192). It is per allocation; the Router schema allows up to
  1e9. The Router counts requests against `maxRequests` only when an authorization carries a
  `requestLimit`: candidate runs do; the ordinary canary does from Relay v0.10.3 (owner decision U10,
  comment 124122). The activation test still refuses including the component with the old
  placeholder string.

`maxLifetimeSeconds` 86400 and `maxKeys` 8 are conservative ceilings for one researcher worker and the
three canary bindings, each with one rotation overlap. This fills the ceiling: inspect existing key occupancy
before issuance or rotation; do not enlarge it implicitly. Review these bounds against the researcher pool's lifetime before step 6. The pool
inference policy's grant (`routes`, `access`, `lifetimeSeconds`) must be a subset of this ceiling.

For this canary, first verify the running Router image is
`registry.chifor.me/llm-router/router@sha256:c35fc2225b787a041799cd2c638d129c349d2718d6a090040aca1f84c9181516`
and its `/app/SOURCE_COMMIT` is `2ad086a9002311ff4eaa61ff26dacc56d121cbeb`, with
`relay-keys` active in `GET /admin/v1/plugins`. These are the reviewed image/source
pins in [the rollout record](relay-router-messages-20261125.md). If identity differs
or cannot be confirmed, stop new issuance and requalify the count semantics for the
running release; conservatively treat pending-cleanup keys as occupied meanwhile.
Do not infer code identity from an active plugin state alone.

At capacity the Router rejects managed creation with HTTP 409 `KEY_LIMIT` and
`Relay managed key limit reached.` The [deployed count predicate](https://git.chifor.me/cchifor/llm-router/src/commit/2ad086a9002311ff4eaa61ff26dacc56d121cbeb/packages/plugins/relay-keys/service.ts#L192)
counts this issuer/tenant's non-revoked, unexpired keys that are acknowledged or
still inside their delivery window. A durably revoked Router key is excluded even
if Relay escrow cleanup remains pending; a revocation that has not reached the
Router can still consume a slot. Do not infer occupancy from the number of profiles.
Use the existing authenticated Relay management `list`/`inspect` flow to check
key IDs, delivery status, expiry and revocation metadata without exposing tokens.
Serialize new issuance and rotations, and confirm the preceding delivery and old
key revocation before starting another. On `KEY_LIMIT`, stop new issuance, reconcile
existing operations through their normal lifecycle, then recheck occupancy; do not
mint substitute keys. Keep the cleanup requirement before removing a route from
trust even though confirmed revocation already frees its key slot.
If an owner approves a higher ceiling instead, change it in Git and verify the
mounted trust after a Router restart; editing the Secret alone does not change the
running plugin's limit. Freeing a slot by normal revocation needs no restart.

### Route precondition (stop until the route exists)

The owner chose a dedicated route ([relay#277 comment 121330](https://git.chifor.me/cchifor/relay/issues/277#issuecomment-121330)):
the shared `gpt-6-luna` route stays untouched. The selection is
`{kind: "model", model: "relay-researcher-luna"}` with no route, and the profile pins
`adaptedResponses: {catalogModel: "gpt-6-luna", maxOutputTokens: 8192, reasoningEffort: "none"}`.
The effort is `none` by owner decision D
([relay#277 comment 121597](https://git.chifor.me/cchifor/relay/issues/277#issuecomment-121597)):
OpenAI allows function calling on Chat Completions for `gpt-6-luna` only with
`reasoning_effort: none`. Relay's contract admits no other value. Trial results are "pipeline
qualified, reasoning off", not evidence of research quality.
Relay's design note `docs/plans/2026-11-08-infrastructure-workers/researcher-budgeted-codex-route.md`
(relay#487) holds the reasoning. Why the alias works:

- Relay sends `requestedModel = selection.model` and the grant's `routes` to the Router's
  `worker-budget-preflight` (Relay `server/workers/pool-readiness.ts`,
  `server/workers/runtime-session/profiles.ts`), and the Router refuses unless `routes` includes
  `requestedModel` and every route exists in the namespace (llm-router
  `packages/plugins/relay-keys/service.ts`). The alias resolves by route name.
- At inference a Relay key is always route-scoped. A request for model `relay-researcher-luna` is
  route mode on that route, and the budget compares each policy with the attempt's deployment (its
  real model `gpt-6-luna`), never with the requested name.
- Model mode (`selection.route`) is not available to a budgeted worker: the preflight checks route
  aliases only ("Model selectors/default routes are not currently issued by this bridge").

Before step 6 the operator creates the route. `GET /admin/v1/config` returns the whole Router
configuration; its `policies` array holds the routes (each entry is one route, a `RoutePolicy`).
Append this entry to `policies`, then `PUT /admin/v1/config` with the edited document and
`If-Match: <revision>`:

```json
{
  "alias": "relay-researcher-luna",
  "namespace": "default",
  "deployments": [
    { "id": "relay-researcher-luna-openai-2", "accountId": "openai-2", "model": "gpt-6-luna", "enabled": true, "weight": 1 }
  ],
  "strategy": "priority",
  "affinity": "none",
  "access": ["api"],
  "maxAttempts": 1,
  "fallbackAliases": [],
  "native": []
}
```

- `native: []`: Codex's protocol is never passed through on this route. A Router release with
  llm-router #189 then lists it with `adaptedResponses`, which Relay's holder requires. The same
  release needs llm-router #190: without it the Router drops the Relay MCP tools (Codex sends them as
  one `mcp__relay` namespace) and invents `cached_tokens: 0` in usage.
- The same edit sets two settings on the shared account `openai-2`:
  - `baseUrl: "https://api.openai.com/v1"`, explicitly. The budget authority compares
    `account.baseUrl` with the policy's required `baseUrl`, so an account relying on the provider
    default never qualifies. It changes no request: it is already the `openai` provider's default
    (llm-router `provider-openai` and `adapters/providers/url.ts`), so `codex-api-1` and
    `pool-codex-api` keep reaching the same endpoint. Readback: those routes still list their
    `openai-2` target.
  - `reasoningEffort: "none"`, the Router's enforcement layer for the effort; Relay's launcher and
    proxy are the other. The Router sends it on every request in place of any client value. This
    one does change shared traffic: every route on `openai-2`, `codex-api-1` and `pool-codex-api`
    included, loses reasoning, which the owner accepted (decision D). The `QualifiedTokenPolicy`
    does not bind the effort, so the setting is recorded with the policy digest, and any change to it
    requires requalification. Readback: the journal's `request.parameters_adjusted` shows
    `reasoning_effort:passed` or `reasoning_effort:overridden` on every attempt, and reported
    reasoning tokens are 0.
- `affinity` stays `none`: `strict` requires an `agent_id`, which Codex does not send.

The qualification evidence and every `QualifiedTokenPolicy` name the account and deployment ID of
this route's target: `accountId: openai-2`, `deploymentId: relay-researcher-luna-openai-2`,
`model: gpt-6-luna`. The authority accepts a policy only when its deployment ID occurs exactly once
across all routes, so that ID must be new. A policy that names a `codex-api-1` deployment ID does
not qualify this route. Readback: `GET /v1/routes` with the worker's key lists
`relay-researcher-luna` with `available: true`, `capabilities.tools: true`,
`adaptedResponses: {protocol: "codex-responses"}` and targets `[{provider: openai, model: gpt-6-luna}]`.

### The Pi canary's two routes (stop until both read back exactly)

The trust ceiling's `subscription` and `local` access is safe only because each canary route admits
exactly one access kind through exactly one target ("Router trust mapping" above). Both routes are
created through the same `GET`/`PUT /admin/v1/config` edit (the body keeps `revision`, and
`If-Match` carries it), with new deployment IDs. They were created on 2026-11-24 at 20:18Z, revision
2509 -> 2510, with every existing route and account unchanged. Their shape is the one the
inference session's live wire canary passed with (revision 2506):

```json
{"alias": "canary-pi-codex", "namespace": "default",
 "deployments": [{"id": "canary-pi-codex:fixed", "accountId": "codex-2", "model": "gpt-6-astra", "enabled": true, "weight": 1}],
 "strategy": "smart", "affinity": "none", "access": ["subscription"], "maxAttempts": 1, "fallbackAliases": [],
 "timeoutMs": 90000, "idleTimeoutMs": 90000, "models": ["gpt-6-astra"], "native": ["codex-responses"], "placement": "conversation"}
{"alias": "canary-pi-qwen", "namespace": "default",
 "deployments": [{"id": "canary-pi-qwen:fixed", "accountId": "lan-192.168.0.28-18020", "model": "qwen3.8-27b", "enabled": true, "weight": 1}],
 "strategy": "usage", "affinity": "none", "access": ["local"], "maxAttempts": 1, "fallbackAliases": [],
 "timeoutMs": 90000, "idleTimeoutMs": 90000, "compat": "lenient", "models": ["qwen3.8-27b"], "placement": "conversation"}
```

Readback before the trust restart (and again before any binding is created), from
`GET /admin/v1/config` inside the Router pod. For each of the two routes:
- `access` is exactly `["subscription"]` or `["local"]` respectively;
- exactly one deployment, enabled, on `codex-2` / `gpt-6-astra` or `lan-192.168.0.28-18020` / `qwen3.8-27b`;
- `fallbackAliases` is `[]` and `maxAttempts` is 1.

The two accounts' own `access` must still read `subscription` and `local`. If any of these differs, stop: do
not restart the Router with the widened trust, and do not create a binding.
Before applying the Claude addition below, also require the `claude` account's
access to be `subscription` and `canary-pi-claude.access` to equal `["subscription"]`.

### Claude canary addition (cold acceptance required)

The final canary route uses the existing `claude` subscription account and `sonnet`,
with its own one-route binding from [the exact inputs](inference-canary-v0113/README.md).
This addition is prepared for operator review; it is not evidence of a live Claude
route, trust restart, credential or profile activation.

After the current post-roll gate finishes, the operator creates this route using
the latest configuration and its `If-Match` revision, preserving all existing routes
and accounts. Refuse to overwrite an existing alias whose definition differs.

```json
{"alias": "canary-pi-claude", "namespace": "default",
 "deployments": [{"id": "canary-pi-claude:fixed", "accountId": "claude", "model": "sonnet", "enabled": true, "weight": 1}],
 "strategy": "smart", "affinity": "none", "access": ["subscription"], "maxAttempts": 1, "fallbackAliases": [],
 "timeoutMs": 90000, "idleTimeoutMs": 90000, "maxDurationMs": 120000, "models": ["sonnet"], "placement": "conversation"}
```

Read back the exact target, route and account `subscription` access, no fallback,
and one attempt before the trust change. Merging the trust PR updates the ESO
template and eventually the Secret; the mounted subPath and plugin retain their
old trust only until the next restart. An eviction, node drain, OOM or unrelated
image rollout can load the widened trust just as a scheduled restart can. After
any restart, compare the mounted trust SHA with the Secret and read back the grants
before relying on either old or new trust. Require the existing grants plus
`canary-pi-claude`, with all other ceilings unchanged. The restart must be scheduled
outside other provider gates and followed by the normal post-roll gate.

On the pinned Router release, a trust alias absent from the route catalog does
not fail startup: `readTrust` validates the trust schema, mapping uniqueness and
Ed25519 public keys, not configured routes. Managed key creation calls
[`checkRoutes`](https://git.chifor.me/cchifor/llm-router/src/commit/2ad086a9002311ff4eaa61ff26dacc56d121cbeb/packages/plugins/api-keys/keys.ts#L54)
and refuses a missing alias with HTTP 400 `INVALID_REQUEST` (`No such route`).
Thus an early restart loads an unused grant ceiling, not a usable missing route.
Still follow the route-before-trust order above, verify after any restart, and
recheck these semantics when changing the pinned Router release.

Before issuing the Claude credential, require Claude Code 2.1.283 active with no
pending change, `sdk-resume`, `warmSessions:false`, and the `inference-sessions`
plugin in state `active` from `GET /admin/v1/plugins`. This checks the plugin,
not the existence of a running conversation; begin the isolated canary with zero
active canary turns and no prior retained canary process.
Enable Messages through the normal plugin configuration flow, verify managed
capabilities `nativeAnthropic:true`/`routeSelectors:false` and the exact route's
`anthropicMessages:true` catalog flag, then complete the operator's cold Messages
acceptance. Only then may the owner create the late Claude binding and approve
its profile launch. Warm retention, live Pi acceptance and active warm-to-cold
rollback remain subsequent gates. No credential issuance, service start or plugin
enablement is performed by this trust manifest.

## OpenBao documents the operator fills

All in the KV-v2 mount `af`. The provision Job creates none of them.

| Path | Field(s) | Content | Written by | Read by |
| --- | --- | --- | --- | --- |
| `relay-router/relay/admin-signer` | `private_key` | Ed25519 private key, PKCS#8 PEM (kid `relay-admin-2026-11`) | operator, step 3 | `af-app-relay-router` (SA `relay/relay-router-eso`) |
| `relay-router/relay/reconciler-signer` | `private_key` | Ed25519 private key, PKCS#8 PEM (kid `relay-reconciler-2026-11`) | operator, step 3 | `af-app-relay-router` |
| `relay-router/router/trust` | `admin_public_key`, `reconciler_public_key` | the two SPKI public keys, PEM | operator, step 3 | `af-app-router-relay-keys` (SA `llm-router/router-relay-keys-eso`) |
| `relay-router/escrow/<tenant>/<host-or-assistant>/<credential-id>` | `token`, `router_key_id`, `operation_id` | escrowed Router credential | **Relay only** (`relay-router-escrow`), never by hand | Relay (`relay-router-escrow`) |

The escrow token Relay writes with is not a document: ESO mints it (section "Escrow token" below).

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

### Escrow token (minted by ESO, rotated every 24 h)

Since 2026-11-22 nobody mints the escrow token by hand. The VaultDynamicSecret
`relay/relay-router-escrow-token` (`credentials/relay.yaml`) POSTs `auth/token/create/relay-router-escrow`
with `ttl=168h` on every refresh of the ExternalSecret `relay-router-files`, every 24 h. It logs in as the
SecretStore does (SA `relay-router-eso`, k8s-auth role `af-app-relay-router`, whose policy may `update`
that one path). The token role caps what comes back: orphan, not renewable, policy `relay-router-escrow`
only, at most 168 h. The template writes the response's `client_token` into `escrow-token`; the kubelet
swaps the directory-mounted file, and Relay rereads it on every escrow call. Relay never renews a token.

- **Lifetimes.** A token lives 168 h and is replaced after 24 h. Replaced tokens are not revoked: each
  expires on its own, so in steady state about seven are live, all with the same escrow-only policy.
  ESO runs the generator BEFORE it reads the signer documents, so a refresh that fails later (a signer
  document unreadable) still mints, and every retry mints again: during such an outage undelivered
  tokens pile up until they expire. They carry the escrow policy only; revoke them with revoke-prefix
  below once the cause is fixed, then force a sync.
- **Health.** `RelayRouterFilesNotReady` (`monitoring/ha-rules.yaml`) fires after 30 min of Ready=False,
  or when ESO has not reported this ExternalSecret for 25 h (ESO does not re-export the condition after
  a restart until the next refresh, so a shorter window would page on every restart). Relay then still
  has about five days on the last token.
  `kubectl --context admin@ai -n relay describe externalsecret relay-router-files` names the error.
- **Rotate now** (also mints one): `kubectl --context admin@ai -n relay annotate externalsecret relay-router-files force-sync="$(date +%s)" --overwrite`.
- **The token Relay uses**, by accessor, without printing it (admin token): the delivered token goes to
  OpenBao on stdin, never in argv.

  ```sh
  kubectl --context admin@ai -n relay get secret relay-router-files -o jsonpath='{.data.escrow-token}' | base64 -d |
    bao write -format=json auth/token/lookup token=- | jq -r '.data | [.accessor, .creation_time, .expire_time] | @tsv'
  ```

  The newest minted token is not necessarily this one (a failed refresh can mint without delivering).
  **Every live escrow token**, oldest first:

  ```sh
  bao list -format=json auth/token/accessors | jq -r '.[]' | while read -r a; do
    bao token lookup -format=json -accessor "$a" |
      jq -r 'select(.data.display_name == "token-relay-router-escrow") | [.data.creation_time, .data.accessor, .data.expire_time] | @tsv'
  done | sort -n
  ```
- **Revoke them all** (a leak, or abandoning the connection), with the admin token:
  `bao write -f sys/leases/revoke-prefix/auth/token/create/relay-router-escrow`. Escrow, delivery and
  cleanup then fail closed (`remote_unavailable`) until the next mint: force a sync right after, unless
  you are abandoning the connection (Rollback).
- **Fallback, if the generator cannot mint** and the last token would expire before the cause is fixed:
  mint one from the role by hand and write it into the Secret directly. ESO replaces it at its next
  successful refresh, which is what you want.

  Each step runs only if the previous one succeeded, so a failed mint never replaces the still-valid
  token with an empty one, and the token appears in no argv and no shell trace (`jq` builds the patch):

  ```sh
  umask 077; d="$(mktemp -d)"
  bao token create -role=relay-router-escrow -display-name=relay-router-escrow -format=json > "$d/t.json" &&
    jq -e '.auth.client_token | select(type == "string" and length > 0)
           | [{op: "replace", path: "/data/escrow-token", value: @base64}]' "$d/t.json" > "$d/patch.json" &&
    kubectl --context admin@ai -n relay patch secret relay-router-files --type=json --patch-file "$d/patch.json" &&
    jq -r .auth.accessor "$d/t.json"   # record the accessor (not secret) in the operator evidence
  shred -u "$d"/*; rmdir "$d"
  ```

- **The one-time cutover from the hand-minted token** (2026-11, this change): after the provision Job
  has run with the mint grant and `relay-router-files` is Ready with a `refreshTime` after the merge,
  confirm the Secret's `escrow-token` changed (compare `sha256sum` of the old and new values; print
  neither), wait two minutes for the kubelet, then revoke the hand-minted token with
  `bao token revoke -accessor <accessor from the step 9 evidence>` and delete its retired document:
  `bao kv metadata delete -mount=af relay-router/relay/escrow-token`.

## Order, with the operator's readback for each step

Every step is its own reviewed change or operator action. A failed readback stops the sequence.
Budget-runbook step numbers refer to `docs/runbooks/relay-worker-budget-authority.md`.
Steps 1-6 and the release deployment in step 8 are complete; retain their readbacks without repeating
key generation or inclusion. H2-H4a ran before the remaining budget proofs. The resumed order is the
[remaining-live-steps checklist](relay-researcher-remaining-live-steps.md): its steps 3-10, then H6
(step 10 here). File B follows H6, before R0/H7, per the decisions in
[ailab #1820](https://git.chifor.me/cchifor/ailab/issues/1820).

1. **Record fixed (this PR merged).** Readback: the `openbao-relay-router-provision` Job log ends with
   `no KV document and no token written`; `bao policy read relay-router-escrow` is exactly the HCL
   above; `bao read auth/token/roles/relay-router-escrow` shows `allowed_policies [relay-router-escrow]`,
   `orphan true`, `renewable false`, `token_no_default_policy true`, `token_explicit_max_ttl 720h`
   (168h since the 2026-11-22 automatic rotation);
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
   carries `workerBudgets.maxTokens` 7441536 (ailab #1778). `relay-router-files` stays not Ready (no
   token yet).
4. **Readers row** (budget step 7): `relay_budget_router` → this digest.
   Readback: `SELECT login, deployment_digest FROM worker_budget.readers;`
5. **Router budget-reader inclusion** (budget step 8): one PR includes
   `components/llm-router-worker-budget-reader` and replaces its placeholder with this digest.
   Recreate rollout; the `worker-budget-authority` plugin stays disabled. Readback: the router's
   `ROUTER_WORKER_BUDGET_DEPLOYMENT_DIGEST` equals the digest, `GET /admin/v1/plugins` shows the plugin
   disabled, the mounted files' SHA-256 equal the Secret's.
6. **Router trust inclusion; File B later.** The owner decided on 2026-11-17 (remaining-live-steps
   open point 6) to include the trust without the `qualifications` rows (File B, keyed on this
   digest). File B follows H6, before H7 (`relay-researcher-hub-upgrade.md`, enablement order).
   Before this step the route precondition is resolved, I is accepted (comment 120818 A/B), and
   the `clients` row exists for `relay_budget_allocator`. It is written with File A at budget step 7
   (`relay-worker-budget-authority.md`, step 7; the step-7 plan, ailab #1753 comment 124071), not here.
   The row carries issuer `relay.chifor.me`, connection
   `babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8`, this digest, tenant `00000000-0000-4000-8000-000000000001`,
   `max_tokens` 8 × (I + 8192), `max_requests` 8, `max_authority_ms` 120000. The ledger's
   `max_requests` applies only to scoped allocations (candidate runs); the canary's unscoped
   allocation is capped at 8 requests by the Router from the `requestLimit` Relay v0.10.3 sends
   (owner decision U10, relay#277 comment 124122). The trust `maxTokens` is already
   8 × (922,000 + 8,192) = 7441536 (ailab #1778), so the step-6 PR only includes
   `components/llm-router-relay-keys-trust` and amends `test_relay_v060_activation_gate.py` for that
   component only. Readback:
   `SELECT login, issuer, connection_id, deployment_digest, tenant_ids, max_tokens, max_requests, max_authority_ms FROM worker_budget.clients;`;
   after the rollout an unsigned `POST https://router.chifor.me/register/relay-keys` with
   `Content-Type: application/json` and body
   `{"version":1,"operationId":"operator-probe","action":"capabilities"}` is refused with
   403 `RELAY_DELEGATION_DENIED` (an empty body fails schema validation first with 400;
   llm-router `5a5181b7:packages/plugins/relay-keys/index.ts`, `contracts.ts`),
   `GET /admin/v1/plugins` shows `relay-keys` active, the log has no `Invalid RELAY_KEYS_TRUST_FILE`
   error, and the mounted `trust.json` SHA-256 equals the Secret's.
7. **Budget authority ready** (budget steps 8-9): `worker_budget.authority.ready=true` after independent
   review, then the router plugin enabled through the admin API. Readback as in that runbook
   (`GET /admin/v1/plugins` active, `Worker budget authority available`).
8. **Researcher release deployed.** The reviewed Relay release that reads these settings (worker budget
   client, installations), through its own PRs and pre-migration backup. Readback per that release's
   runbook ([relay-researcher-hub-upgrade.md](relay-researcher-hub-upgrade.md), H2-H4a). The pinned
   profile registry is **not** a prerequisite of the connection: its entries need the genuine release
   approval that follows qualification, so it comes after step 10 (hub upgrade H4b). Until then the
   `relay-researcher` profile (`connectionId babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8`,
   `protocol codex-responses`, `selection {kind: model, model: relay-researcher-luna}` with
   `adaptedResponses`) reaches Relay through the signed candidate runs and then the canary permit
   (hub upgrade H7). The pool inference policy grant stays within the ceiling above.
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
    connection row is created by Relay, never by hand: from Relay v0.10.9 (relay #569, hub-upgrade
    H8) by the owner's first pool approval whose inference policy names this connection, else by the
    first budgeted worker issuance. Relay inserts it from the mounted record (its `deploymentDigest`),
    never updates or re-enables a row, and refuses the approval on a disabled or drifted row. The
    `relay-researcher` binding and its key are still created only by budgeted issuance. Afterwards
    `SELECT id, origin, deployment_digest FROM router_connections;` (tenant-scoped) shows this digest.

## Rollback

- Before step 10, reverting an inclusion PR is enough; a Router trust without the Relay keys in use
  issues nothing.
- To abandon the connection, deleting the OpenBao documents is not enough: ESO keeps the last
  rendered Secrets (`deletionPolicy: Retain`), so `relay/relay-router-files` would keep the private
  keys. In order: revert both inclusions; remove `clusters/ai/relay-router-credentials.yaml` in a
  reviewed PR, so Flux prunes the two ExternalSecrets and the generator (nothing mints any more) and
  Kubernetes garbage-collects the Secrets they own (`creationPolicy: Owner`). The same PR removes
  `RelayRouterFilesNotReady` from `monitoring/ha-rules.yaml` (and its fixtures): without the
  ExternalSecret its series is gone, and the rule would fire about 25 h later and stay firing. Revoke every minted
  escrow token (`bao write -f sys/leases/revoke-prefix/auth/token/create/relay-router-escrow`); confirm with
  `kubectl --context admin@ai -n relay get secret relay-router-files` and
  `-n llm-router get secret llm-router-relay-trust` (both NotFound; delete them explicitly if not);
  then delete the documents (`bao kv metadata delete -mount=af relay-router/relay/admin-signer`, and
  `reconciler-signer`, `relay-router/router/trust`).
- After any issuance, revoke the credentials through Relay first and confirm the Router rejects them:
  removing the components does not revoke issued keys, and Relay needs the connection files to finish
  revocation and escrow cleanup. Keep them until no pending job remains.
- Removing `relay-router-provision-job.yaml` from git does not remove its policies or roles, and does
  not revoke a token; address those objects in OpenBao explicitly.
- A new key pair needs new kids in `connections.json` and the trust, and a rollout of both; it does
  not change the digest.
