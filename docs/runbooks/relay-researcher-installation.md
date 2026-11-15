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

`scripts/tests/test_relay_researcher_installation.py` pins the shapes, the derived quotas, both
egress stages and their digests (exactly one included, the definition pinning that one), the RBAC
and the trust pins, and refuses including either Flux Kustomization while a pin is a placeholder.
`scripts/tests/test-relay-researcher-render.sh` builds both trees, the installation once per egress
stage, under manifest-lint's pinned kustomize and kubeconform.

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
| `agentImage` | `pins.yaml` | `publication.image` of the agent's `deploy/worker-images/publish.py` receipt |
| `sourceCommit` | `pins.yaml` | the reviewer-merged Relay commit everything was built from |
| `installationPublicKey` | the operator's copy of the definition, then `agent/config.json` (generated) | step I3 |
| `installationPolicy.allowedImageDigests`, `allowedHarnessAdapters`, `binding` | the operator's copy of the definition | the runtime image's publish receipt and the release's sealed researcher template (built from the baseline) and `PodRuntimeBinding` |
| `policy.*` release fields in `agent/config.json` | generated by Relay's tool | never typed by hand |
| `identity.fenceObjectUid` | regenerated `agent/config.json` (step I10) | the live fence ConfigMap after enrollment |

`scripts/relay-researcher-bundle.py import` writes the three pins and the generated `config.json`;
nothing else is edited by hand except the step I10 removal of the enrollment mount.

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

**I4. Definition and Relay's bundle.** Copy the template, remove `_comment`, fill the
OPERATOR-FILLS leaves (table above) and nothing else. From the pinned Relay checkout
(`git rev-parse HEAD` = `sourceCommit`, clean, locked dependencies, Node 22.21+), following Relay's
`docs/infrastructure-installation-preparation.md` (operator-owned input, not group/other writable,
new output directory):

```sh
node_modules/.bin/tsx scripts/prepare-pod-installation.ts /operator/researcher-definition.json \
  "$(sha256sum /operator/researcher-definition.json | cut -d' ' -f1)" /operator/researcher-bundle
python3 scripts/relay-researcher-bundle.py check /operator/researcher-bundle --chart /path/to/relay
python3 scripts/relay-researcher-bundle.py import /operator/researcher-bundle --chart /path/to/relay
```

`check` refuses any difference between the bundle and this review: definition, namespaces,
containment, gate, egress digest, agent values and config, the generated agent policy against the
definition and its binding (placements on `relay-worker-1` with the relay-gvisor overhead, one runtime
digest, the binding's templates), and (with `--chart`, which `import` requires) the chart render
against the committed agent objects. `report.json` must say `enrollment-only-worker-creation-disabled`
with every readback `not-observed`. Keep the bundle, its `bundle-manifest.json` and the definition's
SHA-256 in the operator evidence.

**I5. Installation stage PR.** One PR: the imported `pins.yaml` and `agent/config.json`, and
`git mv kubernetes/apps/relay-researcher/flux/relay-researcher-installation.yaml kubernetes/apps/clusters/ai/`.
Re-run step I2's `dig` first. The tests refuse the move with any placeholder pin. Merging applies
the namespaces and their containment in one Flux apply.

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

**I9. Enrollment readback.** `GET /api/infrastructure/installations/<id>` is `ready` with a pinned
fence UID and the hub epoch acknowledged; `kubectl -n relay-infra-control get configmap
relay-installation-fence -o jsonpath='{.metadata.uid}'` equals it; the Lease
`relay-infrastructure-agent` is held; agent logs carry IDs and bounded codes only;
`kubectl -n relay-researcher-1 get pods` is still empty. Record the fence UID.

**I10. Drop first-enrollment access.** Delete the enrollment Secret. Regenerate the bundle with
`identity.fenceObjectUid` set and `references.enrollmentTokenSecret` removed (template too), run
`check --chart` and `import`, and in the same PR remove the `enrollment-token` volume and mount from
`agent/deployment.yaml` and lower the control `secrets` quota from 5 to 4 (Relay's budget for one
credential object). The tests require all four to agree.

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
([relay-worker-candidate-journal.md](relay-worker-candidate-journal.md)) renders its Service at the
pinned `10.96.0.42`.

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
  `f6d3187c2fa3843ef54ed5d6026093c00a8fe487ab54263b7ce6db140d51282f`.

The tests refuse one without the other. Re-run step I4 with the operator's definition updated the
same way: `check --chart` must pass (the binding's G3 fields are unchanged since I4), and `import` changes
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

**Not covered by these steps.** The Relay half is the release's (Q1). The candidate journal is still
the optional bundle, and the agent's own journal egress and candidate runtime values are separate
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
   reviewed change.
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
