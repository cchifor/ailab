# First Relay researcher installation on ailab (staged, inactive)

Status: **source only. Nothing in this runbook is applied, and merging it changes no cluster object.**
It is step 8 of the [researcher delivery plan](https://git.chifor.me/cchifor/relay/src/branch/docs/researcher-delivery-plan-20261114/docs/plans/2026-11-08-infrastructure-workers/delivery-plan-2026-11-14.md)
("Reviewed ailab install") and steps 3-4 of the gated order in
[relay-worker-capacity.md](relay-worker-capacity.md#next-stage-first-researcher-runtime). The hub
side, which must come first, is [relay-researcher-hub-upgrade.md](relay-researcher-hub-upgrade.md),
staged with its components in ailab #1767 (a separate PR; either can merge first).

Every step that writes is an operator action with the owner's direct confirmation for the exact
reviewed rollout (capacity runbook, "Before any privileged write"). A dev-worker prepares the PRs and
the evidence; its read-only `platform` credential cannot do any of this.

## What is in git, and what merging it does

| Source | Flux | Merge effect |
| --- | --- | --- |
| `kubernetes/apps/relay-researcher/installation/` | none; staged as `flux/relay-researcher-installation.yaml` | Nothing |
| `kubernetes/apps/relay-researcher/agent/` | none; staged as `flux/relay-researcher-agent.yaml` | Nothing |
| `kubernetes/apps/relay-researcher/pins/pins.yaml` | read by both trees at build time | Nothing; three placeholders |
| `kubernetes/apps/relay-researcher/installation/qualification-egress.yaml` | not included (the tree includes `execution-egress.yaml`) | Nothing; selected by step Q4 |
| `kubernetes/apps/relay-internal-tls/` (ailab #1768) | none; staged as `flux/relay-internal-tls.yaml` | Nothing |
| `docs/relay-workers/researcher-runtime-ca.crt` | not a manifest | The pinned ailab root CA for the release build (step Q1) |
| `docs/relay-workers/researcher-installation-definition.json` | not a manifest | The reviewed input template for Relay's tool |
| `scripts/relay-researcher-bundle.py` | not a manifest | Operator's check and import of Relay's generated bundle |
| `scripts/relay-researcher-binding.mts`, `docs/relay-workers/researcher-binding-inputs.json` | not a manifest | Step I4's generator of the binding and the definition, run with the pinned Relay checkout's code, and its pinned inputs |
| `docs/relay-workers/researcher-i4/` | not a manifest | The generator's output for the identity approved at step I3; the operator's I4 run must reproduce it byte for byte |

`scripts/tests/test_relay_researcher_installation.py` pins the shapes, the derived quotas, both
egress stages and their digests (exactly one included, the definition pinning that one), the RBAC
and the trust pins, and refuses including either Flux Kustomization while a pin is a placeholder.
`scripts/tests/test-relay-researcher-render.sh` builds both trees, the installation once per egress
stage, under manifest-lint's pinned kustomize and kubeconform.
`scripts/tests/test_relay_researcher_binding.py` pins the committed output to the template, the
RuntimeClass, the internal TLS and journal manifests, the release pins and the decisions.

### Names and placement

| Object | Value | Why |
| --- | --- | --- |
| Control namespace | `relay-infra-control` | The agent, its key Secret, its Lease and its fence ConfigMap. One installation per cluster; later slots join it. |
| Execution slot | `relay-researcher-1` | One slot, one role, one Pod at most (plan: "one worker per slot"). |
| Agent node | `talos-agent-node-3` (`ailab.io/agent-pool=true`, tolerates `dedicated=agent`) | Off the worker pool; least-loaded agent node on 2026-11-10. |
| Worker node | `relay-worker-1`, RuntimeClass `relay-gvisor` (overhead 80m / 24Mi) | The dedicated pool. |
| Cluster UID | `faf3a521-bd08-4d6d-ba5c-9dc7da903819` (`kube-system` UID) | The installation's cluster pin. Re-read it (step I2). |
| Tenant | `00000000-0000-4000-8000-000000000001` | The only tenant. |

Relay's preparation refuses a control placement that does not conflict with every worker placement
on some nodeSelector key. The researcher binding's placement must therefore carry
`kubernetes.io/hostname: relay-worker-1` next to `ailab.io/relay-worker-pool: "true"`. This is a
requirement on the release's binding; the template says so.

It must also carry `ailab.io/runtime-gvisor: "true"`. RuntimeClass admission merges `relay-gvisor`'s
`scheduling.nodeSelector` (`ailab.io/relay-worker-pool` and `ailab.io/runtime-gvisor`) into every Pod
of the class before validation, and Relay pins the placement's selector exactly (the admission
policies' CEL equality and the executor's readback); Relay's candidate policy documentation requires
every RuntimeClass selector key to be in the placement already. A placement without it would pass
enrollment and refuse every worker Pod at qualification, with a binding that cannot change between the
stages. `relay-researcher-bundle.py check` reads the class's selector and tolerations from
`relay-workers.tf` and refuses a placement that lacks a selector key or has other tolerations.

### What the two stages create

**Installation stage** (Relay `10-namespaces.json`, `20-containment.json`, `21-admission-deny.json`,
plus ailab's allow-list), in one Flux apply:

- both namespaces with restricted Pod Security (enforce, warn, audit, `v1.35`), Relay's
  installation-id label, and `prune: disabled` / `helm.sh/resource-policy: keep`;
- `relay-enrollment-quota`: control 1 Pod, 200m / 256Mi / 64Mi, 5 Secrets, 8 ConfigMaps, 2 Leases;
  slot **0 Pods**, 2080m / 4120Mi / 10240Mi (the ceiling plus the gVisor overhead), 0 Secrets, no
  PVC, Service or Lease;
- `relay-enrollment-limits` (the same maxima per container and Pod);
- `relay-default-deny` in both (Ingress and Egress, no rule);
- `relay-researcher-egress` in the slot (next section);
- the deny-all gate `relay-enrollment-<installationId>` and its binding.

**Agent stage**, mirroring the pinned chart `deploy/infrastructure-agent` rendered with enrollment-only
values: ServiceAccount (no automounted token), the control Role (get/update the fence ConfigMap and
the Lease by name, create ConfigMaps and Leases), the `kube-system` Namespace read, the slot Role
(Pods get/create/delete only), the config and trust ConfigMaps, the agent NetworkPolicy, ailab's
`kube-apiserver` Cilium rule, the Deployment (1 replica, Recreate, UID 65532, read-only root,
no capabilities, projected 3600 s token) and the `relay-researcher-installation` PrometheusRule.

Quota, limits and the RBAC are the enrollment state. Opening the slot is a later, separately reviewed
activation (see "Beyond enrollment").

### Egress: enrollment stage only

**This allow-list serves enrollment and nothing after it** (owner decision G3, relay#277 comment
122379). Enrollment needs only the agent's own egress (control namespace) and a slot that admits no
Pod. No qualification scenario runs against this policy.

The slot's enrollment-stage allowance: DNS to the CoreDNS pods (UDP/TCP 53), and TCP 443 to
`104.21.86.245/32` and `172.67.138.98/32`. `relay.chifor.me` and `router.chifor.me` are
Cloudflare-proxied and resolve to exactly those two addresses (read through 1.1.1.1 on 2026-11-14;
CoreDNS forwards Pods to the public resolvers, so Pods see the same answer). Everything else is
denied: the LAN `192.168.0.0/23`, every node address, the API server, the `cloud-power` WoL listener,
the NAS `10.55.0.254`, the metadata address and every other namespace. The policy's measured digest,
`aa11c2299af12acc886696480b5800ac0b568a0b6e2813609d4ed2f7eea38405`
(Relay `candidateMeasurementDigest(candidateNetworkPolicy(...))`), is the definition's
`executionEgressArtifacts[0].sha256`.

Why it cannot carry qualification:

1. **One rule reaches both the hub and the Router.** They share both addresses, so no NetworkPolicy
   can cut one without the other, and the hub-only partition evidence
   (`researcher-hub-partition-observation`) cannot be waived.
2. **The two addresses are Cloudflare anycast, shared with other Cloudflare customers**, so a worker
   could open TLS to another zone through them.
3. **DNS goes to the public resolvers through CoreDNS**, so queries themselves can carry data out.

A Cloudflare address change breaks the enrollment-stage slot **closed**: re-read both names before
each inclusion PR (step I5) and treat a change as a policy change (new digest, new definition,
re-review).

### Egress: the qualification stage

`installation/qualification-egress.yaml`, a separate file next to the enrollment stage's
`execution-egress.yaml`, which is never edited in place. Step Q4 selects it instead of that file: the
same object, `relay-researcher-egress`, with this body (the drivers refuse a third policy). Measured digest
`f6d3187c2fa3843ef54ed5d6026093c00a8fe487ab54263b7ce6db140d51282f`.

Workers keep the public names `https://relay.chifor.me` and `https://router.chifor.me`: the hub origin
is baked into the measured runtime and must equal Relay's `RELAY_URL` origin, and the Router origin
must equal the template's attested origin. The Pod's measured `hostAliases` (Relay
`PodRuntimeBinding.hostAliases`, rendered into the Pod, admission-checked, and part of every
measurement; `dnsPolicy` stays `ClusterFirst` and no other alias is accepted) map them to two
distinct in-cluster TLS terminators, `kubernetes/apps/relay-internal-tls`, and, for candidate runs
only, the candidate journal's name to its Service:

| Name in the Pod | Address (pinned Service) | Rule | Peer (namespace AND Pods) | Port |
| --- | --- | --- | --- | --- |
| `relay.chifor.me` | `10.96.0.40` `relay-internal-tls/relay-hub-tls` | 0, hub | `relay-internal-tls`, `app.kubernetes.io/name: relay-hub-tls` | TCP 443 |
| `router.chifor.me` | `10.96.0.41` `relay-internal-tls/relay-router-tls` | 1, Router | `relay-internal-tls`, `app.kubernetes.io/name: relay-router-tls` | TCP 443 |
| `relay-candidate-journal.relay-candidate-journal.svc` (candidate runs) | `10.96.0.42` `relay-candidate-journal/relay-candidate-journal` | 2, journal | `relay-candidate-journal`, `app: relay-candidate-journal` | TCP 8443 |

No DNS rule and no address block: nothing else resolves, and nothing else routes. Socket LB is off,
so each ClusterIP connection is translated per packet before policy and the rules name the backend
Pods on their container ports. The journal entry is an addition to the owner's two-name design: the
candidate supervisor dials the journal from the worker Pod to consume its one-use session permit, so
a DNS-less candidate slot needs the name too; ordinary workers never dial it, and Relay renders the
third alias only for candidate runs. The owner approved this extension on 2026-11-15, word for word
(relay#277: decision record comment 123028, approval comment 123063): "candidate-only alias
`relay-candidate-journal.relay-candidate-journal.svc -> 10.96.0.42`, TCP 8443, plus the documented
ClusterIP-pinning exception for 10.96.0.40-42". The exception and its removal triggers are in
`docs/network-plan.md`.

**Rule 2 applies to every Pod in the slot: accepted, not narrowed** (condition 1 of comment 123028).
The condition was to narrow the journal rule to candidate Pods by label, or to record why not. No
label can do it. Relay's executor gives every worker Pod, candidate and ordinary alike, exactly one
label, `relay.infrastructure/managed: "true"` (`packages/infrastructure-agent/executor.ts`,
`MANAGED_LABEL`), and the per-Pod admission policies require the label set to equal that rendered set
(`candidate-policy-preparation.ts`, `pod-policy-preparation.ts`, `ordinary-canary-pod-gate.ts`), so no
Pod in the slot can carry a candidate-only label. A candidate Pod is marked only by the annotations
`relay.infrastructure/candidate-run-sha256` and `relay.infrastructure/candidate-trust-sha256`, and a
NetworkPolicy cannot select on annotations. Even with such a label, a `podSelector` belongs to the
whole policy, not to one rule: narrowing rule 2 alone would take a second policy in the slot, which
the drivers refuse. So the policy keeps `podSelector: {}`, and its digest is unchanged. What bounds
the exposure:

1. An ordinary Pod cannot name the journal: it has no resolver, and Relay's ordinary contract admits
   exactly the two aliases (hub and Router), so it carries no journal alias.
2. Reaching the journal is not authority: the journal verifies every request against the signed
   run's agent or holder key, and the client verifies the journal's name and certificate against its
   pinned CA (`checkCandidateJournalPins`). The alias changes name resolution only.
3. The journal's own ingress admits only the managed worker Pods of `relay-researcher-1` and the
   infrastructure agent in `relay-infra-control`, on TCP 8443, and its renderer refuses a run that
   names any other namespace (`kubernetes/optional/relay-candidate-journal/render.py`,
   `CLIENT_NAMESPACES`). The agent stays because it is a journal client itself: it inspects and admits
   each candidate Pod with the run's agent key before creating it (Relay
   `CandidateAdmissionController`).
4. The rule is temporary: step Q7 removes it at ordinary activation.

What this fixes, against the enrollment stage's three limits:

1. **A hub-only cut is measurable.** Rule 0 admits only the hub terminator's Pods and rule 1 only the
   Router terminator's. Relay's `researcher-hub-partition-observation` removes `hubRuleIndex` 0 and
   keeps `routerRuleIndex` 1; it derives which rule admits which terminator from the live Services
   (ClusterIP equal to the Pod's aliases), their ready Pods and namespace labels, instead of trusting
   the declared indices, and counts the Router terminator's access-log lines from the worker Pod's
   address after the cut (Router traffic that kept flowing).
2. **No Cloudflare anycast:** the slot reaches three in-cluster Services and nothing else.
3. **No DNS:** the slot sends no query at all.

Trust: each terminator holds an `ailab-ca` certificate for exactly its public name (cert-manager,
ECDSA P-256, 90 days, reloaded without a restart) and overwrites `CF-Connecting-IP`,
`X-Forwarded-For` and `X-Real-IP` with the worker Pod's address, so Relay's and the Router's trust in
`CF-Connecting-IP` holds (their NetworkPolicy comments name the terminators). The runtime trusts only
the ailab root CA for these origins: the release's `hub-ca.pem` (supervisor and connector) and the
worker templates' `holder.router` and `holder.communication` `caFile`/`caSha256` (the holder's Router
catalog client, the native inference proxy and the agent-tools hub client) are
`docs/relay-workers/researcher-runtime-ca.crt`, sha256
`3d109e83f3e144daf1022cff5af22cbeda668db9bb1f4b25fc42f179254ad4c3` (CN `ailab-root-ca`, valid to
2036-06-15; byte-identical to `ansible/roles/openbao_agent/files/ailab-root-ca.crt`, CI compares).

### Template and ceilings

The researcher template is Relay's `ROLE_BASELINES.researcher`
(`packages/infrastructure-protocol/baselines.ts`): 1000m CPU, 2048 MiB memory, 4096 MiB scratch, 512
processes, lease 900 s, lifetime 8 h, idle stop 1800 s, no egress destination slugs. The
installation's ceilings (owner decision G4) sit above it: 2000m / 4096 MiB / 10240 MiB scratch / 512
processes, `maxLeaseSeconds` 900, `maxLifetimeSeconds` 28800, `allowedEgressDestinations: []`. A
lease ceiling below the template's 900 s makes `prepare-pod-installation.ts` refuse the template; the
test checks the baseline fits. The slot quota is the ceiling plus the gVisor overhead, so one worker
of the baseline size uses about half of it. The canary has its own shorter deadline.

## Operator-filled values

Every source and image pin is a single value, written once:

| Value | Where | From |
| --- | --- | --- |
| `installationId` | `kubernetes/apps/relay-researcher/pins/pins.yaml` | the approval receipt (step I3) |
| `agentImage` | `pins.yaml` | `publication.image` of the agent's `deploy/worker-images/publish.py` receipt: the receipt of the inputs' `release`, or of their `agentRelease` after an agent-only update ("Agent-only update" below) |
| `sourceCommit` | `pins.yaml` | the reviewer-merged Relay commit of the runtime release (`release.sourceCommit`): the worker runtime, the definition and binding and the chart were built from it, and the agent too unless an agent-only update pins a later one. An agent-only update never changes it |
| `installationPublicKey` | the operator's copy of the definition, then `agent/config.json` (generated) | step I3 |
| `installationPolicy.allowedImageDigests`, `allowedHarnessAdapters`, `binding` | the operator's copy of the definition | generated by `scripts/relay-researcher-binding.mts` (step I4) from the release assets, the runtime image's publish receipt and the researcher template it seals with Relay's code; never typed by hand |
| `policy.*` release fields in `agent/config.json` | generated by Relay's tool | never typed by hand |
| `identity.fenceObjectUid` | regenerated `agent/config.json` (step I10) | the live fence ConfigMap after enrollment |

`scripts/relay-researcher-bundle.py import` writes the three pins and the generated `config.json`;
nothing else is edited by hand except the step I10 removal of the enrollment mount.

### Binding inputs that are decisions (owner decided 2026-11-15)

`scripts/relay-researcher-binding.mts` derives the binding from the release assets (runtime
manifest, image receipts), the reviewed template, the manifests here (RuntimeClass, internal TLS
and journal Services) and Relay constants (identities 1000/2000, the researcher baseline, the
instructions digest, the harness pins). The values below are not derived; the owner decided them on
2026-11-15 (relayed by the operator). They are `decisions` in
`docs/relay-workers/researcher-binding-inputs.json`, and the committed output
(`docs/relay-workers/researcher-i4/`) was built with them. Each one is part of the binding digest (the
template pins also of the template digest), which qualification evidence binds: change one in a
reviewed PR with a regenerated output before I4, never after qualification without a new one. The
installation ID and public key are not decisions: they are the identity Relay approved at step I3,
recorded as `installation` in the same file. Any change to the template or to these inputs
regenerates the committed output in the same PR (delete the directory, rerun the I4 command):
`scripts/tests/test_relay_researcher_binding.py` refuses a stale one.

| Decision | Decided | Source | What it constrains |
| --- | --- | --- | --- |
| `supervisor` (milliCpu / memoryMiB / ephemeralMiB) | `250 / 256 / 64` | The owner's decision. The proposal was Relay's reference binding, `100 / 128 / 64` (`tests/fixtures/worker-runtime.ts` at the pinned commit); the owner raised CPU and memory for headroom. No live measurement exists: the supervisor container runs the Node supervisor and the Rust connector, so memory is the value to watch in the qualification and canary evidence | Carved out of every intent (the harness gets the rest); never added to the quota. Relay floors: memory at least 80 (16 MiB tmpfs + 64 MiB process), ephemeral at least 2 (lease + control volumes), harness at least 250m / 256 MiB, workspace at least 256 MiB |
| placement `tier` | `sandboxed-pod` | Relay `TIER_CAPABILITY_CEILING`: the pod tier of `user-space-kernel-sandbox`, which gVisor is; the researcher baseline admits both pod tiers | Every intent's `runtime.tier`, the pool's `tierBindings`, the agent policy's `podTiers` |
| placement `binding` | `relay-gvisor` | Relay `KubernetesTierBinding.binding`: "operator binding name (RuntimeClass or VM profile)", so the RuntimeClass name | Every intent's `runtime.binding`, the pool's `tierBindings`, `podTiers` |
| template `templateId` | `4da6f919-d660-472c-b503-4d8849fa60df` | A new random UUID: the hub has no template yet (step I1) | The pool's template, every intent, the H4 profile registry entry (by template digest) |
| template `revision` / `name` | `1` / `researcher` | First revision of a new ID; the name of the image's worker profile template (Relay `deploy/worker-images/researcher/inputs.json`) | Template digest; Relay refuses duplicate names |
| template `skills` / `toolsets` | `[]` / `[]` | Nothing consumes them at this release (only the preparation and baseline code read them), so a pin would be an unenforced claim | Template digest |
| template `harness.modelPolicy` | `relay-researcher` | A slug naming the pool's inference policy, after the execution identity. Model-agnostic as a name only: the released image's holder template already selects `relay-researcher-luna` (catalog model `gpt-6-luna`), so a switch to astra (delivery plan step 4) is a rebuilt image, a new runtime digest and a regenerated binding whatever this slug says | The pool's `inferencePolicies[].name`, the H4 profile registry's `modelPolicy`, template digest |
| `releaseApproval` | absent | Forced by Relay: the candidate runtime and candidate policy preparation refuse a binding that carries one (`candidate-runtime.ts`, `candidate-policy-preparation.ts` `ordinary-approval`, `pod-renderer.ts`), and ordinary activation compares the binding without it (`pod-policy-activation.ts`) | The binding stays identical through enrollment and qualification; ordinary activation ("Beyond enrollment", item 4) adds the approval, a new binding digest reviewed then |

Derived, not decided, but easy to miss: the placement's `nodeSelector` also carries
`ailab.io/runtime-gvisor: "true"` (section "Names and placement").

Fit, at the decided supervisor allocation (Relay carves the supervisor out of the intent; the
quota and the scheduler see the intent plus the RuntimeClass overhead; the 50m / 32 MiB / 1 MiB
`workspace-init` container runs before the others and adds nothing):

| | Researcher template (baseline) | G4 ceiling |
| --- | --- | --- |
| Intent CPU / memory / scratch | 1000m / 2048 MiB / 4096 MiB | 2000m / 4096 MiB / 10240 MiB |
| Harness = intent - supervisor (250m / 256 / 64) | 750m / 1792 MiB / 4032 MiB | 1750m / 3840 MiB / 10176 MiB |
| Workspace = scratch - 64 - 128 holder state - 32 log reserve | 3872 MiB (floor 256) | 10016 MiB |
| Quota view = intent + 80m / 24 MiB overhead | 1080m / 2072 MiB / 4096 MiB | 2080m / 4120 MiB / 10240 MiB |
| Slot quota 2080m / 4120 MiB / 10240 MiB | fits, about half | equal, by construction |

## Order, with a readback for each step

A failed readback stops the sequence. Use `kubectl --context admin@ai` throughout.

**I1. Preconditions.** Hub steps H1-H3 are done and read back: the reviewed release is live with its
migrations, the first recovery generation is initialized (receipt `kind: "initial"`), and `GET /api/infrastructure/installations`
answers 200 with `[]`. The agent and worker-runtime images are published from the pinned
`sourceCommit` and both receipts are verified (`kind: relay-worker-image`, digest-only
`publication.image`, raw manifest readback). The release contains `scripts/prepare-pod-installation.ts`
and the chart. Budget, Router and qualification are **not** needed for enrollment.

**I2. Read-only readbacks** (record each):

```sh
kubectl --context admin@ai get ns kube-system -o jsonpath='{.metadata.uid}'      # faf3a521-...-9dc7da903819
kubectl --context admin@ai version -o json | jq -r .serverVersion.minor          # 35
kubectl --context admin@ai get ns relay-infra-control relay-researcher-1         # both NotFound
kubectl --context admin@ai get validatingadmissionpolicies -o name | grep relay-enrollment-   # nothing
kubectl --context admin@ai get runtimeclass relay-gvisor -o jsonpath='{.overhead.podFixed}'   # cpu 80m, memory 24Mi
kubectl --context admin@ai get node relay-worker-1 --show-labels; kubectl --context admin@ai describe node relay-worker-1 | grep Taints
kubectl --context admin@ai get endpointslices -n default -l kubernetes.io/service-name=kubernetes -o jsonpath='{.items[*].endpoints[*].addresses}{" "}{.items[*].ports[*].port}'   # .41 .42 .43, 6443
kubectl --context admin@ai -n kube-system get pods -l k8s-app=kube-dns -o wide
kubectl --context admin@ai top node talos-agent-node-3                           # headroom for 200m / 256Mi
dig +short @1.1.1.1 relay.chifor.me A; dig +short @1.1.1.1 router.chifor.me A    # exactly 104.21.86.245 and 172.67.138.98
```

Any difference stops here: a different API endpoint, DNS label or Cloudflare address is a change to
the definition template, the manifests and the egress digest, reviewed as such.

**I3. Installation key and approval.** In a private directory of the operator session (never a
shared path, never echoed):

```sh
umask 077; d="$(mktemp -d)"
openssl genpkey -algorithm ed25519 -out "$d/installation.pem"
openssl pkey -in "$d/installation.pem" -pubout -outform DER | tail -c 32 | basenc --base64url | tr -d '=' > "$d/public"
bao kv put -mount=af relay-infrastructure/installation-key installation_pem=@"$d/installation.pem"
cat "$d/public"          # the raw base64url public key: public, record it
shred -u "$d/installation.pem"
```

The private key now lives only in OpenBao (`openbao-recovery.md`, "RELAY INFRASTRUCTURE
INSTALLATION KEY") until step I8 puts it in its Secret. A Relay administrator approves it with
`POST /api/infrastructure/installations` and body
`{operationId: <new UUID, kept for exact retries>, name: "ailab relay-worker pool", clusterUid:
"faf3a521-bd08-4d6d-ba5c-9dc7da903819", controlNamespace: "relay-infra-control",
installationPublicKey: <public>}`. Record the returned installation ID. The receipt also carries a
one-use enrollment token that expires within the hour: do not log or keep it. Step I8 reissues one.
Readback: `GET /api/infrastructure/installations/<id>` shows `approved`, this cluster UID, control
namespace and public key, and the hub's recovery generation.

**I4. Definition and Relay's bundle.** Never assemble the definition by hand: the generator builds
it, binding included, with the pinned Relay checkout's own code. Prepare that checkout
(`git rev-parse HEAD` = the release's `sourceCommit`, no local changes, `npm ci`, Node 22.21+) and
download the release assets that `docs/relay-workers/researcher-binding-inputs.json` pins (`release.assets`:
`SHA256SUMS`, `manifest.json` and the three `*-133788.json` worker files of Gitea release 2142) into a
new empty directory. If the inputs also pin an `agentRelease` (an agent-only update, "Agent-only
update" below), download the assets it pins into a second new empty directory and add
`--agent-assets <that directory>` to the command; the generator refuses the flag without an
`agentRelease` and an `agentRelease` without the flag. The checkout stays the runtime's
(`release.sourceCommit`) either way. Then, from this checkout, with the identity approved at step I3:

```sh
/path/to/relay/node_modules/.bin/tsx scripts/relay-researcher-binding.mts --relay /path/to/relay \
  --assets /operator/release-assets \
  --installation-id b380ff7f-0716-478d-8b01-3d39f30e750b \
  --installation-public-key 6D2Cma_Oa4KiaDtOnpmiiztuBhEKObNtEqFNJl-FBas \
  --out /operator/researcher-i4
diff -r /operator/researcher-i4 docs/relay-workers/researcher-i4    # nothing: byte-identical
sha256sum /operator/researcher-i4/binding.json                       # = summary.json files.binding.json
```

The identity must equal the reviewed record (`installation` in the inputs file): the generator
refuses any other, checks the key with Relay's own Ed25519 validation, and requires Relay's
`publicKeyFingerprint` (first 16 hex of sha256 over the base64url key text) to equal the approval's,
`25070c63dada58d3`.

The generator refuses another commit, a checkout with local changes, another checkout's `tsx`, any
asset whose sha256 is not pinned, receipts that do not name the runtime manifest, a runtime manifest
without the G3 fields, a template with OPERATOR-FILLS leaves other than the seven it fills, an
`agentRelease` that fails the checks under "Agent-only update", and any definition Relay's
`preparePodInstallation` refuses. It writes, as canonical JSON, `definition.json`
(the template, `_comment` removed, every OPERATOR-FILLS leaf filled and nothing else),
`binding.json`, `template-preparation-input.json` (Relay `scripts/prepare-worker-templates.ts` input
that seals the same researcher template) and `summary.json` (the pins, template, policy and binding
digests, and the `agentRelease` when the inputs pin one). Every binding value that is not derived is in the inputs file's `decisions`, reviewed with
the committed output ("Binding inputs that are decisions" above). The `diff` proves the operator's
definition and binding are exactly the reviewed ones. Then, from the Relay checkout, following its
`docs/infrastructure-installation-preparation.md` (operator-owned input, not group/other writable,
new output directory):

```sh
node_modules/.bin/tsx scripts/prepare-pod-installation.ts /operator/researcher-i4/definition.json \
  "$(sha256sum /operator/researcher-i4/definition.json | cut -d' ' -f1)" /operator/researcher-bundle
python3 scripts/relay-researcher-bundle.py check /operator/researcher-bundle --chart /path/to/relay
python3 scripts/relay-researcher-bundle.py import /operator/researcher-bundle --chart /path/to/relay
```

`check` refuses any difference between the bundle and this review: definition, namespaces,
containment, gate, egress digest, agent values and config, the generated agent policy against the
definition and its binding (placements on `relay-worker-1` with the relay-gvisor overhead, one runtime
digest, the binding's templates), and (with `--chart`, which `import` requires) the chart render
against the committed agent objects. `report.json` must say `enrollment-only-worker-creation-disabled`
with every readback `not-observed`, and its `identity.bindingDigest` equals the generator's
`summary.json`. Keep the bundle, its `bundle-manifest.json`, the generator's `summary.json` and the
definition's SHA-256 in the operator evidence.

**I5. Installation stage PR.** One PR: the imported `pins.yaml` and `agent/config.json`, and
`git mv kubernetes/apps/relay-researcher/flux/relay-researcher-installation.yaml kubernetes/apps/clusters/ai/`.
Re-run step I2's `dig` first. The tests refuse the move with any placeholder pin, and need no edit
otherwise: the bundle tests follow the committed pins and config, and `test_manifest_paths.py` adds
the tree to its reviewed path set once its Flux file is in `clusters/ai` (likewise I7 and Q3). Merging
applies the namespaces and their containment in one Flux apply.

Reproduce it before review, and again whenever the generator, its inputs or these files change:
`just relay-researcher-reproduce /path/to/relay /operator/release-assets` (the same checkout and
assets as I4) re-runs the generator, `prepare-pod-installation.ts`, `check --chart` and `import`
against the committed tree and fails on any byte difference from `docs/relay-workers/researcher-i4/`,
`pins.yaml` or `agent/config.json`. Once the inputs pin an `agentRelease`, pass its asset directory
as well: `just relay-researcher-reproduce /path/to/relay /operator/release-assets HEAD
/operator/agent-release-assets` (`--agent-assets` of `scripts/relay-researcher-reproduce.py`, required
then and refused otherwise). CI cannot run it, because the Relay repository and its release
assets are private and ailab's runners hold no credential for them.
`scripts/tests/test_relay_researcher_reproduction.py` checks in CI that the committed I4 output, the
imported pins and config, the provenance record and the pinned inputs agree with each other.

**I6. Containment readback.** Nothing may receive Pod rights before this passes:

```sh
kubectl --context admin@ai get ns relay-infra-control relay-researcher-1 -o custom-columns=NAME:.metadata.name,UID:.metadata.uid,LABELS:.metadata.labels
kubectl --context admin@ai get validatingadmissionpolicy relay-enrollment-<id> -o jsonpath='{.status}'   # typeChecking without warnings
kubectl --context admin@ai -n relay-researcher-1 get resourcequota,limitrange,networkpolicy
kubectl --context admin@ai -n relay-researcher-1 get pods                         # No resources found
kubectl --context admin@ai -n relay-researcher-1 create --dry-run=server -f - <<'POD'
apiVersion: v1
kind: Pod
metadata: {name: gate-probe}
spec:
  automountServiceAccountToken: false
  securityContext: {runAsNonRoot: true, runAsUser: 65532, seccompProfile: {type: RuntimeDefault}}
  containers:
  - name: probe
    image: registry.k8s.io/pause:3.10
    securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}
    resources: {limits: {cpu: 10m, memory: 16Mi, ephemeral-storage: 16Mi}}
POD
# must fail with: ValidatingAdmissionPolicy 'relay-enrollment-<id>' ... denied request: Relay
# enrollment-only installation: all worker Pod creation and updates are disabled.
```

Record both namespace UIDs (the later `ordinaryNamespacePins`). Exactly two NetworkPolicies in the
slot. The probe is restricted-compliant on purpose: Pod Security admission runs before the policy, so
a non-compliant probe would be refused by PSA and prove nothing about the gate, and the quota is
evaluated after the policy. Only the gate's own message is this proof.

**I7. Agent stage PR.** Re-check `talos-agent-node-3` headroom. One PR:
`git mv kubernetes/apps/relay-researcher/flux/relay-researcher-agent.yaml kubernetes/apps/clusters/ai/`.
After merge the Pod waits in ContainerCreating: its two Secrets do not exist yet, by design, so that
the one-use token of step I8 is minted only when a Pod is waiting to consume it.

**I8. The two Secrets, then enrollment.** Neither value touches disk or the transcript:

```sh
bao kv get -mount=af -field=installation_pem relay-infrastructure/installation-key \
  | kubectl --context admin@ai -n relay-infra-control create secret generic relay-infrastructure-agent-key --from-file=installation.pem=/dev/stdin
# With the administrator's browser session (cookie file mode 0600) and Origin https://relay.chifor.me:
curl -sS --fail -b "$COOKIES" -H 'Origin: https://relay.chifor.me' -H 'Content-Type: application/json' \
  -d '{"operationId":"<new UUID>","enrollmentTtlSeconds":900}' \
  https://relay.chifor.me/api/infrastructure/installations/<id>/enrollment-token \
  | jq -j .enrollmentToken \
  | kubectl --context admin@ai -n relay-infra-control create secret generic relay-infrastructure-agent-enrollment --from-file=token=/dev/stdin
```

The kubelet retries the mount and the agent enrolls. If the token expires first, reissue (allowed
only while `approved`); never create a second installation.

Without a browser session, use the just-in-time operator identity for the second Secret
(docs/runbooks/relay.md, "Operator automation identity"): `python scripts/relay-operator.py call
enrollment-token <id> --ttl 900` writes only the raw token to stdout. It holds Relay admin only for that
one call. The reissue answers HTTP 200; a replayed operationId returns no token. Done this way on
2026-11-16 (reissue revision 3; the agent enrolled 14:34:02Z, epoch 1).

**I9. Enrollment readback.** `GET /api/infrastructure/installations/<id>` is `ready` with a pinned
fence UID and the hub epoch acknowledged; `kubectl -n relay-infra-control get configmap
relay-installation-fence -o jsonpath='{.metadata.uid}'` equals it; the Lease
`relay-infrastructure-agent` is held; agent logs carry IDs and bounded codes only;
`kubectl -n relay-researcher-1 get pods` is still empty. Record the fence UID.

**I10. Drop first-enrollment access.** One PR, then two live steps in this order.

1. **The PR.** Regenerate the bundle with `identity.fenceObjectUid` set and
   `references.enrollmentTokenSecret` removed: edit the template too, because the generator takes
   `--fence-object-uid <uid>` and reads the updated template. Run `check --chart` and `import`.
   Regenerate `docs/relay-workers/researcher-i4/` with the I4 command plus `--fence-object-uid <uid>`:
   its `binding.json` comes out byte-identical, and its `definition.json` gains the fence UID and loses
   the enrollment Secret reference. In the same PR, remove the `enrollment-token` volume and mount
   from `agent/deployment.yaml`, and lower the control `secrets` quota from 5 to 4 (Relay's budget for
   one credential object). The tests require all four to agree. Run
   `just relay-researcher-reproduce` on the PR head.
2. **Merge first, then read back.** The merge rolls the agent: a new `config.json` makes a new
   ConfigMap name. The new Pod no longer mounts the enrollment Secret. With the fence present, it
   takes over without reading the token: the v0.10.3 agent reads the token only when the fence
   ConfigMap is missing (Relay `c0fe50f0` `packages/infrastructure-agent/daemon.ts:345-360`). It also refuses to start
   if the hub's fence UID differs from the pin (`fence_identity_mismatch`). Do not delete the Secret
   first: the running Pod's `enrollment-token` volume is not optional, so a replacement Pod started
   before the merge would wait in ContainerCreating. Readbacks:
   - Both Flux Kustomizations are Ready at the merge revision. Exactly one agent Pod is Running from
     the new ReplicaSet, and its spec has no `enrollment-token` volume.
   - Its log has `"event":"ready"` with an `epoch` and an `incarnationId`. The epoch is one higher
     than at step I9: a new process takes over and retires the previous incarnation
     (Relay `server/workers/installations/auth.ts:320-325`). That is expected, not a re-enrollment.
   - Step I9's readbacks hold: the installation is `ready`, `currentIncarnationId` and `epoch` match
     that log line, and `fenceObjectUid`, `recoveryCurrent` and `enrollmentUsedAt` are unchanged. The
     fence ConfigMap UID is unchanged. The Lease is held by the new Pod and renewing.
   - `relay-enrollment-quota` in `relay-infra-control` shows `secrets` hard 4. The slot still has
     quota `pods` 0, its denial gate, and no Pods.
3. **Then delete the consumed enrollment Secret.** Its token was used at enrollment, and the hub
   reissues only while `approved`:
   `kubectl -n relay-infra-control delete secret relay-infrastructure-agent-enrollment`.
   Readbacks:
   - that Secret is NotFound, and `relay-infrastructure-agent-key` remains;
   - the quota's used `secrets` drops by one;
   - the agent Pod, its epoch, the fence and the Lease are unchanged: no rollout and no new epoch.

### Agent-only update (owner decision 2026-11-17)

**When.** A later Relay release changes only the infrastructure agent and publishes only its image;
the worker runtime, the binding, the templates, the slot and the hub stay. The first one is v0.10.5,
the fix for the v0.10.3 agent losing leadership (relay#537): the runtime stays v0.10.3's
(`c0fe50f0`) and the hub v0.10.4. Relay's preparation allows it: `preparePodInstallation` requires
`definition.sourceCommit` to equal the runtime manifest's (`installation-preparation.ts`,
`runtime-source`) and names the agent only by digest (`definition.agentImage`). Not for a release that
changes the worker runtime, the template or the binding: that is a new binding digest, which the
qualification evidence binds. The chart, the agent config and the bundle are still rendered by the
RUNTIME's checkout, so first confirm that the agent release needs nothing new from them: review
`git diff <release.sourceCommit> <agentRelease.sourceCommit> -- deploy/infrastructure-agent
packages/infrastructure-agent/config.ts packages/infrastructure-agent/installation-preparation.ts
scripts/prepare-pod-installation.ts` in the Relay repository, and that the new agent speaks the live
hub's protocol. Anything there the new agent depends on makes it a full release update instead.

**Inputs.** One reviewed addition to `docs/relay-workers/researcher-binding-inputs.json`; `release`
does not change:

```json
"agentRelease": {
  "tag": "v0.10.5",
  "giteaReleaseId": <the Gitea release ID>,
  "sourceCommit": "<the 40-hex commit the agent image was built from>",
  "assets": {
    "SHA256SUMS": "<sha256>",
    "manifest.json": "<sha256>",
    "worker-image-infrastructure-agent-<run>.json": "<sha256>"
  },
  "agentReceipt": "worker-image-infrastructure-agent-<run>.json",
  "agentImage": "registry.chifor.me/relay/infrastructure-agent@sha256:<the receipt's publication.manifestSha256>"
}
```

The generator then refuses: an `agentRelease` with any other field (a runtime field included); a tag
that is not `vMAJOR.MINOR.PATCH` newer than `release.tag`; assets that do not pin `SHA256SUMS`,
`manifest.json` and the receipt, or whose bytes do not have the pinned sha256s (or disagree with
`SHA256SUMS` where it lists them); a `manifest.json` whose `sourceCommit` or `version` is not
`agentRelease`'s; a receipt that is not an `infrastructure-agent` receipt of `agentRelease.sourceCommit`
(a runtime receipt included) with a digest-only `publication.image`; and an `agentImage` that is not
that image or not in `release.agentImage`'s repository. It takes nothing else from `agentRelease`: the
checkout (HEAD = `release.sourceCommit`), the runtime manifest, receipt and image, the template and
`definition.sourceCommit` all stay `release`'s. Without `agentRelease` it behaves exactly as before.

**Commands** (one PR; the I4 Relay checkout and the I4 release assets, unchanged):

Use `python3 scripts/relay-researcher-agent-pin.py --agent-assets /operator/agent-release-assets
--tag vX.Y.Z --gitea-release-id N --relay /path/to/relay --assets /operator/release-assets` to automate
the local work in steps 1–4 after downloading the three agent assets and reviewing the compatibility
preconditions above. Put Node 22.21+ on PATH; the tool uses that Relay checkout's `node_modules/.bin/tsx`
and the bundle check requires helm and PyYAML. It preserves the inputs file's formatting, validates
the generated binding and import in a temporary copy, and installs changes only after all checks
pass. It prints the provenance values and exact existing sentences/rows for the manual step 5;
registry readback remains an operator check. The manual commands below remain the reference.
The tool refuses a tag older than the current `agentRelease.tag`; re-running the current tag is
allowed. For an intentional rollback, pass `--allow-downgrade` and review its warning; the agent
must still be newer than `release.tag`. Step 2 lists which of the three assets were cross-checked
against `SHA256SUMS`, or `none` when it lists only other release artifacts. Installation stages
and fsyncs replacements and backups beside their destinations before any atomic replacement.
On failure it attempts every required restoration; if a restoration fails, the error names the
affected file and its retained backup for manual recovery.

```sh
# 1. The agent release's assets, into a NEW empty directory (private release: your own token).
#    Record each sha256 and the receipt's publication.image; read the image manifest back from the
#    registry and hash it, as at I1.
sha256sum /operator/agent-release-assets/*
# 2. Add agentRelease to researcher-binding-inputs.json (and say so in its _comment), then regenerate:
/path/to/relay/node_modules/.bin/tsx scripts/relay-researcher-binding.mts --relay /path/to/relay \
  --assets /operator/release-assets --agent-assets /operator/agent-release-assets \
  --installation-id b380ff7f-0716-478d-8b01-3d39f30e750b \
  --installation-public-key 6D2Cma_Oa4KiaDtOnpmiiztuBhEKObNtEqFNJl-FBas \
  --fence-object-uid "$(jq -r .identity.fenceObjectUid docs/relay-workers/researcher-i4/definition.json)" \
  --out /operator/researcher-i4-agent
# 3. What must not change: nothing but the agent image.
cmp /operator/researcher-i4-agent/binding.json docs/relay-workers/researcher-i4/binding.json
cmp /operator/researcher-i4-agent/template-preparation-input.json docs/relay-workers/researcher-i4/template-preparation-input.json
diff <(jq -S 'del(.agentImage)' /operator/researcher-i4-agent/definition.json) \
     <(jq -S 'del(.agentImage)' docs/relay-workers/researcher-i4/definition.json)
jq -r .bindingDigest /operator/researcher-i4-agent/summary.json docs/relay-workers/researcher-i4/summary.json   # equal
rm -r docs/relay-workers/researcher-i4 && cp -r /operator/researcher-i4-agent docs/relay-workers/researcher-i4
# 4. Relay's bundle for the new definition, then the reviewed check and the import (as at I4):
(cd /path/to/relay && node_modules/.bin/tsx scripts/prepare-pod-installation.ts /operator/researcher-i4-agent/definition.json \
  "$(sha256sum /operator/researcher-i4-agent/definition.json | cut -d' ' -f1)" /operator/researcher-bundle-agent)
python3 scripts/relay-researcher-bundle.py check /operator/researcher-bundle-agent --chart /path/to/relay
python3 scripts/relay-researcher-bundle.py import /operator/researcher-bundle-agent --chart /path/to/relay
git diff --stat kubernetes/apps/relay-researcher   # pins.yaml only, its agentImage line only
# 5. Update docs/relay-workers/researcher-i5-provenance.md: the agentImage row (the new digest, from
#    the agentRelease receipt) and the agent release ("Gitea release <id> (<tag>, source <commit>)")
#    with its worker asset sha256s. The committed-state tests require both. Commit, then:
python3 -m unittest discover -s scripts/tests -p 'test_relay_*.py'
just relay-researcher-reproduce /path/to/relay /operator/release-assets HEAD /operator/agent-release-assets
```

`check` accepts a bundle whose agent image differs from the committed pin only when it is the image
the inputs pin (`agentRelease.agentImage`); `installationId` and `sourceCommit` must still equal the
pins exactly. The chart it renders is still the runtime's (`--chart` at `release.sourceCommit`).

**What stays byte-identical**, and the tests and the reproduction hold it: `binding.json` (its sha256
is the frozen `BINDING_SHA256` of `scripts/relay-candidate-run.py`), `template-preparation-input.json`,
the binding, policy, runtime and template digests in `summary.json`, `agent/config.json`, and
`pins.yaml`'s `installationId` and `sourceCommit`. What changes: `definition.json` (`agentImage` only),
`summary.json` (`agentImage`, `agentRelease`, the definition's sha256), `pins.yaml` (`agentImage`
only) and the provenance record. The agent Pod's `chifor.me/source-commit` annotation keeps recording
`pins.yaml` `sourceCommit`, the installation's (runtime) commit: the image digest names the agent
build, and its source commit is `agentRelease.sourceCommit` in the inputs and the summary.
`scripts/tests/test_relay_researcher_agent_release.py` runs this whole procedure against a synthetic
later release when given the Relay checkout and the release assets
(`RELAY_RESEARCHER_RELAY`, `RELAY_RESEARCHER_ASSETS`).

**After the merge.** Flux rolls the agent Deployment to the new image (the config ConfigMap keeps its
name: its content is unchanged). Readbacks as at I10 step 2: one agent Pod Running with the new image
digest (`kubectl -n relay-infra-control get pod -l app.kubernetes.io/name=relay-infrastructure-agent
-o jsonpath='{.items[*].status.containerStatuses[*].imageID}'`), `"event":"ready"` with an epoch one
higher, the installation `ready` with the fence, recovery and enrollment fields unchanged, the Lease
held and renewing, and the slot unchanged (quota `pods` 0, the gate, no Pods).

**Rollback.** One PR, the same steps without the agent release: remove `agentRelease` and
regenerate without `--agent-assets` (the output is the previous one byte for byte), then prepare,
`check` (it accepts the bundle again: the inputs' agent image is `release`'s) and `import` (it writes
`release.agentImage` back). Restore the provenance record too: the agentImage row back to
`release.agentImage`'s digest and the agent release's paragraph removed (`git show
<the commit before the update>:docs/relay-workers/researcher-i5-provenance.md`). The committed-state
tests refuse a record that still names the agent release. Then run them and the two-argument
reproduction.

### The agent's leadership log

The agent (Deployment `relay-infrastructure-agent` in `relay-infra-control`) logs one JSON line per
leadership event, with enums and counts only. This is what the operator's Lease watch (E4) reads. Names
are as of Relay v0.10.6, from `packages/infrastructure-agent/leadership.ts` and `daemon.ts` at
`1599d06e`. The durations below come from `agent/config.json` `leadership`: renew deadline 10 s and
lease duration 15 s at 15/10/2, 20 s and 30 s once T1 merges ("Lease timing (T1, 30/20/2)" below).

| Event | Meaning |
| --- | --- |
| `starting`, `leader_acquired`, `ready` | A new incarnation took the Lease and the installation is `ready` at the next epoch. |
| `lease_renewal_failed` | A renewal round proved nothing. Leadership continues until the renew deadline (`renewDeadlineSeconds`) after the last proven renewal was sent. Fields: `reason`, `failureClass`, `lastDuration`, `failedAttempts`, `sinceRenewMs`. |
| `lease_write_adopted` | v0.10.6 and later. A later read showed that an earlier renewal whose outcome was unknown did land: it carries that write's random marker (annotation `relay.chifor.me/lease-write`) on a newer Lease version. The deadline moves to that write's send time plus the renew deadline. `failedAttempts` counts the rounds bridged. |
| `lease_renewal_recovered` | A renewal was proven again; `failedAttempts` is the number of rounds it bridged. |
| `admission_halted` with `code: leadership_lost` | Leadership ended. Fields: `reason`; the last failed round's `lastFailure`, `failureClass` and `lastDuration`; `failedAttempts`, `sinceRenewMs`; and `inFlight`/`inFlightDuration` when a Lease request was still outstanding. The process exits; the restarted process takes over at epoch + 1 once it has watched the old Lease record unchanged for longer than that record's `leaseDurationSeconds`. At 15/10/2 that was 22-31 s from this event to the next `ready` (2026-11-18); expect about 37-46 s under T1. The restart fires `RelayInfrastructureAgentRestarted`, and running workers end at their next lease renewal ("Lease timing (T1, 30/20/2)"). |

- **Round reasons** (`lastFailure`, and `reason` of `lease_renewal_failed`):
  - `lease-renew-unknown`: the PUT got no answer and may have landed;
  - `lease-read-failed`;
  - `lease-renew-conflict`: 409, and the re-read still showed this Lease;
  - `lease-renew-refused`;
  - `lease-read-late` and `lease-renew-late`: answered only at or after the deadline.
- **Failure classes:** `timeout`, `late`, `transport`, `invalid-response`, `local`, `error`, `401`, `403`, `404`, `409`, `429`, `4xx`, `5xx`, `other`.
- **Loss reasons** (all terminal for this incarnation):
  - `renew-deadline-passed`: no proven renewal within the renew deadline;
  - `lease-taken`: another holder or Lease UID;
  - `lease-deleted`;
  - `lease-invalid`: a successful read returned an invalid Lease or the wrong namespace (definitive loss);
  - `lease-write-unverified`: a successful write response failed Lease validation or showed the wrong namespace, holder or, on renewal, Lease UID (definitive loss);
  - `lease-holder-reused`: a candidate found its own never-reused holder ID in the Lease, possibly from an acquisition whose outcome was unknown (definitive loss);
  - `lease-response-too-late`: an acquisition response was processed at or after its round's acceptance deadline (deadline loss);
  - `lease-state-changed`: a write response arrived for a state this process had already left (definitive local state loss).

Reading an episode:

- **A slow API server** shows `lease_renewal_failed` (`lease-renew-unknown`, `timeout`). Within the renew-deadline window it is followed by `lease_write_adopted` and/or `lease_renewal_recovered`. That is tolerated, not an incident.
- **`renew-deadline-passed`** means no renewal was proven within the renew deadline; timely refusals or repeated conflicts can also cause this. Inspect `lastFailure`, `failureClass`, `lastDuration` and any `inFlight`/`inFlightDuration` first. Investigate etcd (WAL fsync, leader changes, apiserver-to-etcd latency) when those indicate stalled Lease requests. On 2026-11-18 at 03:53Z that was an etcd leader stall: reads were held 8-10 s on every apiserver. Riding out a stall that long needs longer Lease timings: T1 below, an ailab overlay (not a Relay preparation change).
- **`lease-taken`, `lease-deleted` or `lease-holder-reused`** are definitive. Find out who changed or removed the Lease.

The agent's Lease requests are GET and PUT on `leases/relay-infrastructure-agent` and POST on the
Lease collection (no list or watch). `agent/rbac.yaml` grants exactly `get` and `update` on that
resourceName and `create` on leases. For a `403` in `failureClass`, check `agent/rbac.yaml` and the
RoleBinding first; it does not prove RBAC changed. Also check admission denials, namespace state and
the requested Lease/namespace.

### Lease timing (T1, 30/20/2)

**Status.** Approved on 2026-11-18 on three conditions, answered below: an alert for brief leadership
losses ("Monitoring"), and audits of the hub's timeouts and the qualification campaign's timings.
Nothing changes until its PR merges. Until then the agent runs 15/10/2
(`leaseDurationSeconds`/`renewDeadlineSeconds`/`retryPeriodSeconds`), the values Relay's
preparation writes into every bundle: it takes no timing input (Relay `c0fe50f0`
`packages/infrastructure-agent/installation-preparation.ts:192-196`). T1 sets 30/20/2 with a reviewed
ailab overlay that the bundle tooling applies to the agent's config and values on `check`, `import`
and the reproduction. From the merge on, this installation's timing is the overlay's, and
`agent/config.json` `leadership` records it. The agent's schema already accepts 30/20/2 (lease 10-120,
renew 5-110, retry 1-30, renew < lease, 2 × retry ≤ renew: `config.ts:127-138`), so Relay changes nothing.

**Why.** On 2026-11-18 the v0.10.6 agent lost leadership 12 times, each on an etcd stall from the
control-plane nodes' worn NVMe disks (`kubernetes/apps/infrastructure/monitoring/control-plane-storage-rules.yaml`).
v0.10.7, live since 18:38Z, sends a second identical Lease request on a fresh connection when the
first is still pending after 1 s (Relay `61c02f76` `packages/infrastructure-agent/leadership.ts:105`).
That routes around an API server stuck on a stalled etcd follower, and it absorbed its first follower
stall cleanly. It cannot route around a stalled etcd leader, which every API server waits on: 2 of
the 12 losses were leader stalls, and the one at 03:53Z held reads 8-10 s on every API server. A 10 s
renew deadline with a 3 s per-request bound cannot ride that out; 20 s with 6 s can. T1 is not a
liveness guarantee: a longer stall, or repeated stalls with conflicts, still exhausts 20 s.

**What changes.**

- `agent/config.json` `leadership` becomes
  `{"leaseDurationSeconds":30,"renewDeadlineSeconds":20,"retryPeriodSeconds":2}`; nothing else in it.
- Hence the config ConfigMap's hashed name and the Deployment's reference to it: one Recreate rollout
  of the agent. One replica, Recreate and the 30 s termination grace stay.
- The Lease the agent writes carries `spec.leaseDurationSeconds: 30` (it publishes its configured
  duration: `leadership.ts:459`).
- The provenance record names the effective timing and the overlay.

**What does not.** No Relay release: the agent image (v0.10.7, `e72eb7d5…5e98`), the runtime
(`c0fe50f0`) and the hub (v0.10.4) stay. Timing is in neither the local policy digest (it covers
`config.policy.installation` only: Relay `c0fe50f0` `packages/infrastructure-agent/config.ts:255-256`)
nor `PodRuntimeBinding` (`packages/worker-runtime/binding.ts:163`), so `binding.json`,
`definition.json`, `summary.json`, the binding and policy digests and `pins.yaml` stay byte-identical,
as do RBAC, the NetworkPolicy, the installation identity, the fence and the slot. Qualification
receipts bind the binding, runtime, image and cluster, so none is invalidated, but none exercised
30/20/2: a run's record names the effective timing. A later agent-only update keeps the overlay:
Relay's bundle still carries 15/10/2, the import applies the overlay, and `agent/config.json` stays
byte-identical across the update, as that procedure requires.

**The trade-off: slower failover.** Fewer losses, each one longer (Relay `61c02f76`
`packages/infrastructure-agent/leadership.ts`):

| | 15/10/2 | 30/20/2 |
| --- | --- | --- |
| Admission closes this long after the start of the last proven renewal round, exited or not (`:811`) | 10 s | 20 s |
| Lease duration minus renew deadline: the successor's margin | 5 s | 10 s |
| Per-request bound, `floor(renewDeadline / 3)` clamped to 2-10 s (`:110-112`) | 3 s | 6 s |
| Hedge after; retry after a fast failure; after a timeout | 1 s; 1 s; at once | unchanged |
| Acquisition write, proof included | min(10 s, rest of the window) | unchanged: still ≤ 10 s |
| A restarted process's wait on an unchanged foreign Lease, from its first read, to the next 2 s tick (`:742-757`) | > 15 s (about 16 s) | > 30 s (about 32 s) |
| `admission_halted` to the next `ready` | 22-31 s observed | about 37-46 s |

The last row takes the 2026-11-18 handovers as measured from `admission_halted` to the next `ready`;
under T1 only the wait grows, by 15 s. Counted from the last proven renewal instead, the longer
deadline adds too (+25 s in all): about 47-56 s. These are projections, not bounds: kubelet restart
backoff after repeated exits, image pulls and slow hub calls add to both. Under T1 keep, for every
episode, `admission_halted` with its `sinceRenewMs` and the time of the next `ready`.

The first takeover after the merge, and the first after a rollback, are not steady-state
measurements: the restarted process waits the duration the old Lease record carries (`:755`), 15 s
on the merge and 30 s on a rollback.

**Hub timeouts across a handover** (Relay `e77bd09a`). None is broken by 37-46 s:

- **Offer claim, 30 s** from the claim (`server/environments/lifecycle.ts:540`), sent as the offer's
  `deadline` (`server/workers/operations/service.ts:339-366`). The agent executes only before it
  (Relay `61c02f76` `packages/infrastructure-agent/executor.ts:189-193`, `:1428`). An expired claim
  is offered again, to the new incarnation, as `reconcile`, never as a second `execute`
  (`lifecycle.ts:518`, `:537`); the old incarnation's late receipt is kept as evidence and settles
  nothing (`service.ts:538-544`, `:586-588`). At 22-31 s a claim made just before the loss could
  still be live when the successor first polled; at 37-46 s it has always expired, so the successor
  reconciles at once. Do not lengthen the claim to hide a handover.
- **Deferred offers, 30 s**: a refused candidate is retried after 30 s, and an expired claim becomes
  `unknown` first (`service.ts:183-202`). A poll backoff, not a takeover timer.
- **Polls** return at once when idle (`service.ts:113-128`).
- **Installation state** has no heartbeat: it stays `ready` through a takeover (`auth.ts:344-363`),
  `relay_control_installations_oldest_pending_seconds` counts only `approved` and `awaiting-fence`
  (`server/workers/metrics.ts:78`), and request freshness is a ±120 s signature skew
  (`packages/infrastructure-protocol/installation-auth.ts:45`).
- The 30 s holder and workspace observation windows (`server/environments/readiness.ts:97`, `:100`;
  `renewal.ts:91`) are reported by the worker, not the agent, and use the environment's authority
  epoch, not the installation's.
- **Running workers end at a takeover, at any timing.** A worker's runtime lease renewal is decided
  under the incarnation and epoch stored on its previous lease (`server/workers/runtime-lease/service.ts:36-41`,
  `:135-148`; `server/workers/bootstrap/README.md:111-114`), and `lockInstallationAuthority` refuses
  any but the current ones with 409 `installation_authority_lost` (`auth.ts:515-535`). The supervisor
  does not retry a 409 and stops (`protocol-violation`: Relay `c0fe50f0`
  `packages/worker-runtime/supervisor/supervisor.ts:91-92`, `:579-583`). So every takeover ends every
  running worker at its next renewal attempt. T1 does not change that; it makes takeovers rarer.

**Monitoring.** `RelayInfrastructureAgentUnavailable` (`agent/relay-researcher-rules.yaml`) waits 10
minutes, far longer than any 2026-11-18 loss. `RelayInfrastructureAgentRestarted` (warning) sees one: every
admission halt ends the process (exit 3; any other exit is a crash), the kubelet restarts the
container in place, and the rule fires on any increase of the agent container's
`kube_pod_container_status_restarts_total` within 15 minutes (`for: 1m`), then clears. A rollout or
a scale to zero makes a new Pod or none, so neither fires it. The agent exports no metrics and
nothing alerts on its log, so the restart counter is the signal (as for `GiteaForgeRestarting`). Its
fixture (`relay-researcher-rules.test.yaml`) covers a rollout, a restart and the clearing. A
narrower rule would need a leadership metric from the agent or alerting on its `admission_halted`
log line; neither exists. kube-state-metrics' Lease series (`kube_lease_renew_time`) could show a
stalled renewal, but no rule here reads them yet, so their presence in this Prometheus is
unverified. The other rules are unaffected by 37-46 s handovers: the worker rules hold 120-600 s for
5 minutes (`kubernetes/components/relay-worker-monitoring/relay-worker-rules.yaml`), and the
control-plane, candidate and recovery rules observe holders, journals and backups, not the agent.

**The qualification campaign** assumes no agent handover time: its cut and lease margins
(`relay-researcher-qualification-campaign.md` §6) are the worker lease `L`, and R2's 55 s disruption
and R3/R4's cuts change the worker slot's egress, not the agent's. Two things do depend on the agent:
its restart at §3.8 (U12 records the timing it was measured at) and a takeover during a run, which
ends the run's worker as above (§5.6).

**Why it stays safe.** The Lease timing decides when a successor may act. Stale authority is stopped
by two mechanisms that T1 does not touch:

- Locally, admission closes at the deadline whether or not the process has exited, and every offer
  rechecks it before acting (Relay `61c02f76` `packages/infrastructure-agent/daemon.ts:289-294`,
  `executor.ts:381`, `:1436`). A successor watches an unchanged record for longer than a full lease
  duration, so the old leader's window has closed at least the margin (10 s, was 5 s) before the
  successor can acquire, without comparing clocks across nodes.
- At the hub, a takeover advances the epoch: under the installation's row lock it retires the
  previous incarnation and sets epoch + 1 in one transaction (Relay `e77bd09a`
  `server/workers/installations/auth.ts:335-363`). Every later request from the old incarnation then
  fails the retired/current-incarnation/epoch check, however fresh its signature (`:476-480`), and a
  transaction relying on an earlier authentication rechecks it (`:515`). Polls and receipts take
  that path (`server/workers/operations/service.ts:120`, `:429`), and a receipt's identity must match
  (`:450`). The new incarnation CAS-persists its epoch in the fence before it is `ready` (Relay
  `61c02f76` `packages/infrastructure-agent/daemon.ts:619-620`), and the fence refuses a lower epoch
  (`fence.ts:182-203`). The hub keeps no timer tied to the Lease ("Hub timeouts" above).

Neither, at any timing, retracts a Kubernetes write already in flight or stops a hub transaction
serialized before the takeover; the fence is a point-in-time check (`fence.ts:176-180`). Uncertain
operations stay reserved and are reconciled.

**Rollout** (the operator merges the held PR):

1. Merge between qualification runs, with no worker running: the rollout is a takeover, and a
   takeover ends running workers ("Hub timeouts" above). While `relay-researcher-agent` is suspended
   (campaign runbook §3.5 to §3.10) Flux applies nothing, and a per-run branch cut before the merge
   still renders 15/10/2. Note the current epoch and the Lease
   (`kubectl -n relay-infra-control get lease relay-infrastructure-agent -o jsonpath='{.spec.holderIdentity} {.spec.leaseDurationSeconds}{"\n"}'`: `15`).
2. Merge. Flux applies the new config ConfigMap and the Deployment recreates the Pod. The new process
   waits out the old record's 15 s, so this gap is about one 15/10/2 handover.
3. Readbacks:
   - exactly one agent Pod Running from the new ReplicaSet, zero restarts, `imageID` still
     `…@sha256:e72eb7d5…5e98`;
   - its config:
     `kubectl -n relay-infra-control get cm "$(kubectl -n relay-infra-control get deploy relay-infrastructure-agent -o jsonpath='{.spec.template.spec.volumes[?(@.name=="config")].configMap.name}')" -o jsonpath='{.data.config\.json}' | jq -c .leadership`
     prints `{"leaseDurationSeconds":30,"renewDeadlineSeconds":20,"retryPeriodSeconds":2}`;
   - the Lease command of step 1 shows the new holder and `30`, and `.spec.renewTime` advances
     between two reads a few seconds apart (one renewal per 2 s);
   - its log has `"event":"ready"` with an epoch one higher, and the installation is `ready` at that
     epoch and incarnation with the fence, recovery and enrollment fields unchanged (as at I10
     step 2);
   - renewals flowing: since `ready`, no `admission_halted` and no `lease_renewal_failed` without a
     following `lease_renewal_recovered` or `lease_write_adopted`;
   - the slot as before the merge.

**Rollback.** Revert the PR, under step 1's conditions. `agent/config.json` returns to its previous
bytes (Relay's 15/10/2) and the agent rolls once more (Recreate). That restarted process waits the 30 s the old record carries,
so this one takeover is as slow as a T1 one; later ones are back to 22-31 s. Readbacks as above, with
`15` and `{"leaseDurationSeconds":15,"renewDeadlineSeconds":10,"retryPeriodSeconds":2}`. Run the
committed-state tests and the reproduction on the revert, as for any change to these files (I5).

## Qualification stage (owner decision G3)

The enrollment stage ends here. These steps move the slot to the internal TLS endpoints before any
qualification scenario runs; nothing in them creates a worker.

**Q1. Preconditions.** Steps I1-I10 are done. The pinned release carries the Relay part of G3
(`PodRuntimeBinding.hostAliases` rendered and measured, the Router and communication CA pins, the
candidate journal alias, and the hub-partition driver with separate hub and Router rules) and was
built with:

- `hubCa` = `docs/relay-workers/researcher-runtime-ca.crt` (sha256 `3d109e83…54ad4c3`), so
  `supervisorConfig.hubOrigin` is `https://relay.chifor.me`, `hubCaPath` `/etc/relay/hub-ca.pem` and
  `hubCaSha256` that sha256;
- every worker template's `holder.router` (origin `https://router.chifor.me`) and
  `holder.communication` (origin `https://relay.chifor.me`) with `caFile: /etc/relay/hub-ca.pem` and
  `caSha256` the same sha256;
- the binding's `hostAliases` `{hub: 10.96.0.40, router: 10.96.0.41, candidateJournal: 10.96.0.42}`;
- a runtime base image whose `/etc/nsswitch.conf` reads `/etc/hosts` before DNS (`hosts: files dns`).

The binding is the same in both stages (qualification evidence binds the exact binding), so step
I4's `check` already verified the first three: it refuses a binding without them, or with different
values, in either stage, so an enrollment that could never reach this stage stops at I4. The candidate journal
([relay-worker-candidate-journal.md](relay-worker-candidate-journal.md)) creates its Service at the
pinned `10.96.0.42` through Flux, the same object its renderer emits.

**Q2. Read-only checks.** Who holds the three reserved addresses (`docs/network-plan.md`): every
printed line must be that address's own Service (the journal's, once it exists), and `10.96.0.40` and
`.41` must print nothing; then the issuer is ready and the pinned PEM is the root:

```sh
kubectl --context admin@ai get svc -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {.spec.clusterIP}{"\n"}{end}' \
  | grep -E ' 10\.96\.0\.4[0-2]$'
kubectl --context admin@ai get clusterissuer ailab-ca -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'   # True
openssl x509 -in docs/relay-workers/researcher-runtime-ca.crt -noout -subject -fingerprint -sha256
# subject=CN = ailab-root-ca
# sha256 Fingerprint=FD:AC:C8:10:43:93:BA:DD:5A:B6:49:FE:03:BB:3C:E1:30:F9:BF:D8:4A:A0:E6:55:57:9A:11:2D:8D:98:C2:21
```

A taken address stops here: changing one is a new binding, a new release input and a re-review.
`clusterIP` is immutable, so a reserved Service that already exists on another address keeps it until
it is deleted and created again (network plan; the journal runbook has the procedure).

**Re-check at apply time, failing closed** (condition 5 of relay#277 comment 123028). An address can be
taken between this check and the merge that creates its Service. Run the `grep` above again just
before merging Q3 (for `.40` and `.41`) and just before the journal's Service is created (for `.42`):
the addresses being created must still print nothing. Then, once each of those Services has been
applied (Flux for Q3), read it back, and stop on anything other than the pinned address:

```sh
pinned() {  # pinned <namespace>/<service> <address>: fails unless that Service exists on exactly that address
  got=$(kubectl --context admin@ai -n "${1%/*}" get svc "${1#*/}" -o jsonpath='{.spec.clusterIP}') \
    && [ "$got" = "$2" ] || { echo "STOP: $1 is on '${got:-missing}', pinned $2" >&2; return 1; }
}
pinned relay-internal-tls/relay-hub-tls 10.96.0.40 && pinned relay-internal-tls/relay-router-tls 10.96.0.41   # after Q3
pinned relay-candidate-journal/relay-candidate-journal 10.96.0.42   # after the journal's Service, before any candidate run
```

A `STOP`, or a Flux apply that failed with `provided IP is already allocated`, fails closed: Q4 is not
merged (the slot keeps the enrollment policy and its 0-Pod quota) and no candidate run starts, because
a worker's alias would point at the wrong Service or at nothing. Never fix it by changing an alias (that
is a new binding and a re-review): free the address, then delete the Service and let Flux create it
again from the pinned manifest, and run `pinned` again.

**Q3. Terminators PR.** One PR:
`git mv kubernetes/apps/relay-internal-tls/flux/relay-internal-tls.yaml kubernetes/apps/clusters/ai/`.
Merging creates namespace `relay-internal-tls`, the two Certificates, the two Caddy terminators
(2 replicas each) behind `10.96.0.40` and `10.96.0.41`, their NetworkPolicies (ingress only from
managed worker Pods of `relay-researcher-1` on 443) and one additive ingress policy each in `relay`
and `llm-router`. No worker can reach them yet: the slot is still on the enrollment stage with a
0-Pod quota. Readback:

```sh
kubectl --context admin@ai -n relay-internal-tls get certificate,deploy,svc,pdb,networkpolicy
#   both Certificates READY True; both Deployments 2/2; Services 10.96.0.40 and 10.96.0.41 on 443
kubectl --context admin@ai -n relay get networkpolicy relay-internal-tls-ingress
kubectl --context admin@ai -n llm-router get networkpolicy llm-router-edge-internal-tls-ingress
# The served certificate chains to the pinned root and names exactly the public name
# (a port-forward reaches the Pod directly, past the NetworkPolicy):
kubectl --context admin@ai -n relay-internal-tls port-forward svc/relay-hub-tls 18443:443 &
openssl s_client -connect 127.0.0.1:18443 -servername relay.chifor.me -verify_hostname relay.chifor.me \
  -CAfile docs/relay-workers/researcher-runtime-ca.crt -verify_return_error </dev/null 2>/dev/null \
  | grep -E '^(subject|issuer)=|Verify return code'                     # issuer CN = ailab-root-ca, code 0 (ok)
kill %1
# the same for svc/relay-router-tls with router.chifor.me
```

**Q4. Egress stage PR.** One PR, after Q2's apply-time readback passed for `.40` and `.41`, outside
any ceremony window (if `relay-researcher-installation` is suspended for a drill or the activation
ceremony, finish it, confirm the restore and resume first; never switch stages inside a window) and
while the slot holds no Pod (`kubectl -n relay-researcher-1 get pods` empty; the quota is still 0):

- `installation/kustomization.yaml`: `- execution-egress.yaml` becomes `- qualification-egress.yaml`;
- the template's `executionEgressArtifacts[0].sha256` becomes
  `f6d3187c2fa3843ef54ed5d6026093c00a8fe487ab54263b7ce6db140d51282f`;
- `docs/relay-workers/researcher-i4/` regenerated with the command of its last regeneration (step
  I10's, with `--fence-object-uid`): its `definition.json` pins the egress digest; its `binding.json`
  must come out byte-identical.

The tests refuse one without the others. Re-run step I4 (the generator reads the updated template,
so the new `definition.json` carries the new digest and `binding.json` is byte-identical):
`check --chart` must pass (the binding's G3 fields are unchanged since I4), and `import` changes
nothing (the egress artifact is only in the definition and the report). Keep the new bundle and the
definition's SHA-256 in the evidence. Merging is one Flux apply of one object, same name, new body:
the slot is never without its allow-list.

**Q5. Readback.**

```sh
kubectl --context admin@ai -n relay-researcher-1 get networkpolicy        # relay-default-deny, relay-researcher-egress only
kubectl --context admin@ai -n relay-researcher-1 get networkpolicy relay-researcher-egress -o json \
  | python3 -c 'import json, sys, importlib.util as u
s = u.spec_from_file_location("b", "scripts/relay-researcher-bundle.py"); m = u.module_from_spec(s); s.loader.exec_module(m)
print(m.measurement_digest(json.load(sys.stdin)))'                        # f6d3187c...282f
```

Record the policy's UID: the drivers pin it.

**Q6. The hub-partition drill's configuration** (Relay
`scripts/qualification/researcher-hub-partition-observation.ts` in the pinned release):

```json
"egressPolicy": {"name": "relay-researcher-egress", "uid": "<Q5 UID>", "hubRuleIndex": 0, "routerRuleIndex": 1},
"endpoints": {"hub": {"namespace": "relay-internal-tls", "service": "relay-hub-tls"},
              "router": {"namespace": "relay-internal-tls", "service": "relay-router-tls"}}
```

The operator's identity also needs `get` on Services, Pods and Namespaces and `pods/log` in
`relay-internal-tls` (the Router terminator's access log, container `caddy`, is the evidence of
Router traffic during the cut). Suspend `relay-researcher-installation` for the drill window, as for
every in-place ceremony below.

**Q7. Exit at ordinary activation: the two-rule policy** (condition 1 of relay#277 comment 123028).
Rule 2 and the slot-wide `podSelector: {}` it rides on (accepted above) exist for candidate runs
only. The activation PR that raises the slot to its one-Pod quota for ordinary workers ("Beyond
enrollment", item 4) also swaps the egress stage, in that same PR, never in a separate one:

- add `installation/ordinary-egress.yaml`: the same object, `relay-researcher-egress`, with rules 0
  and 1 of `qualification-egress.yaml` unchanged (hub rule 0, Router rule 1) and no rule 2;
- `installation/kustomization.yaml`: `- qualification-egress.yaml` becomes `- ordinary-egress.yaml`,
  and the new file joins `EGRESS_STAGES` in `scripts/relay-researcher-bundle.py` and the tests'
  `EGRESS`, with its digest pinned in the tests;
- the egress digest pin moves with it: the template's `executionEgressArtifacts[0].sha256` becomes
  the new file's measured digest (the operator's definition the same way, re-checked with step I4's
  `check --chart`).

The tests refuse a pin that is not the included stage's, so the swap and the pin cannot land apart.
Not earlier: the hub-partition drill and every candidate scenario need rule 2. Not later: no ordinary
worker may run under an activated policy that still admits the journal. Readback as in Q5, against
the new digest. A later candidate run needs the qualification stage back (Q4 again, with this
runbook's checks).

**Rollback.** Revert Q4 (only while the slot holds no Pod) to return to the enrollment stage; revert
Q3 only after that, or once the slot is empty for good: a worker whose terminator is gone fails
closed (connection refused), it never falls back to Cloudflare. The namespace
`relay-internal-tls` is retained (`prune: disabled`) and deleted by hand once empty.

**Not covered by these steps.** The Relay half is the release's (Q1). The candidate journal is its own
Flux Kustomization with its own held PRs ([relay-worker-candidate-journal.md](relay-worker-candidate-journal.md),
"Activation under Flux"), and the agent's own journal egress and candidate runtime values are separate
changes ("Beyond enrollment", item 3). Rule 2 (the journal) stays in the policy through
qualification and the ordinary canary, and leaves it at ordinary activation (Q7).
The control namespace is unchanged: the agent still reaches the hub through Cloudflare
(`controlNetwork.hubCidrs`).

## Flux and the in-place ceremonies

Flux re-applies these trees every 10 minutes and on every commit. The qualification drivers replace
the gate in place for their bounded probe window and cut an egress rule under resourceVersion CAS,
and the activation compiler's outputs replace the gate and raise the quota in place. For every such
window: `flux suspend kustomization relay-researcher-installation` (and `relay-researcher-agent` when
the agent's values change), run the window, confirm the drivers restored the pinned objects, commit
any final objects to git, then resume. Never resume over an unreverted live change: Flux would
silently undo a drill or a ceremony.

## Beyond enrollment (not in this PR)

Enrollment is not worker readiness. Before the first worker Pod:

1. The qualification stage (steps Q1-Q6): internal TLS terminators, `hostAliases`, `ailab-ca`
   trust and the three-rule, DNS-less policy replacing `relay-researcher-egress`.
2. Pool and template approval in Relay (exact pool revision, policy digest = the local one).
3. The nineteen qualification scenarios on this slot with the merged drivers (delivery plan step 9):
   they need the signed candidate run (`candidateRuntime` values: candidate ConfigMaps,
   `bootstrapSecrets: true`, journal egress) and the independent candidate journal, each its own
   reviewed change. The per-run ceremony is
   [relay-researcher-qualification-campaign.md](relay-researcher-qualification-campaign.md). Each
   run's values (`candidateRuntime.trustSha256`, `trustConfigMap`, `pinConfigMap` and the `candidate`
   volume's ConfigMap) are generated by `scripts/relay-candidate-run.py`, and so is the one-time
   structure, which it writes with the first run and its `reset` removes
   ([relay-candidate-run-generator.md](relay-candidate-run-generator.md#one-time-prerequisites)).
   The delayed-start run (R7) also needs the control planes' kube-apiserver audit policy that logs
   Pod creates in this slot with their bodies (owner decision O7):
   [relay-researcher-r7-audit-policy.md](relay-researcher-r7-audit-policy.md).
4. The finite ordinary canary (step 10) and then `scripts/activate-ordinary-pod-policy.ts`'s outputs:
   staged guards, Secret quota, the in-place gate replacement and the **one-Pod quota**, plus agent
   values with `ordinaryNamespacePins` (the UIDs of step I6) and `bootstrapSecrets: true`, committed
   here, each through its own reviewed PR with the I6-style readbacks. The PR with the one-Pod quota
   also swaps to the two-rule egress policy and moves its digest pin (step Q7).

## Rollback and retirement

- Before step I5 merges: nothing exists.
- After I5, before I7: removing the Flux file stops reconciliation only (`prune: false`). The objects
  stay; delete them explicitly after review (gate and binding last), and the namespaces only when
  empty. Never delete the gate while anything could create Pods in the slot.
- After I7: revert the agent Flux file (prune removes the agent and its RBAC; the fence stays, it is
  in no inventory). If the installation never reached `ready`, revoke it in Relay.
- After enrollment: stop admissions, remove workers through Relay and wait for the operation journal
  and inventory to settle, then revoke the installation (`POST .../revoke`) and remove the agent
  stage. The namespaces, the fence ConfigMap, the key Secret and its OpenBao document are separate
  final cleanup steps after that evidence is recorded. Namespace deletion never replaces
  reconciliation. A lost or replaced installation key is a new installation, never a silent swap.

## What still blocks a web-UI-created researcher

See [relay-researcher-hub-upgrade.md](relay-researcher-hub-upgrade.md#what-still-blocks-a-web-ui-created-researcher)
(ailab #1767). For this installation in short: the pinned release with the `recovery:initialize`
command and the Relay part of G3, the qualification stage (steps Q1-Q6), the binding's
`relay-worker-1` placement, the Router model qualification and budget authority, then the nineteen
scenarios, the canary and the activation outputs.
