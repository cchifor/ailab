# First Relay researcher installation on ailab (staged, inactive)

Status: **source only. Nothing in this runbook is applied, and merging it changes no cluster object.**
It is step 8 of the [researcher delivery plan](https://git.chifor.me/cchifor/relay/src/branch/docs/researcher-delivery-plan-20261114/docs/plans/2026-11-08-infrastructure-workers/delivery-plan-2026-11-14.md)
("Reviewed ailab install") and steps 3-4 of the gated order in
[relay-worker-capacity.md](relay-worker-capacity.md#next-stage-first-researcher-runtime). The hub
side, which must come first, is [relay-researcher-hub-upgrade.md](relay-researcher-hub-upgrade.md).

Every step that writes is an operator action with the owner's direct confirmation for the exact
reviewed rollout (capacity runbook, "Before any privileged write"). A dev-worker prepares the PRs and
the evidence; its read-only `platform` credential cannot do any of this.

## What is in git, and what merging it does

| Source | Flux | Merge effect |
| --- | --- | --- |
| `kubernetes/apps/relay-researcher/installation/` | none; staged as `flux/relay-researcher-installation.yaml` | Nothing |
| `kubernetes/apps/relay-researcher/agent/` | none; staged as `flux/relay-researcher-agent.yaml` | Nothing |
| `kubernetes/apps/relay-researcher/pins/pins.yaml` | read by both trees at build time | Nothing; three placeholders |
| `docs/relay-workers/researcher-installation-definition.json` | not a manifest | The reviewed input template for Relay's tool |
| `scripts/relay-researcher-bundle.py` | not a manifest | Operator's check and import of Relay's generated bundle |

`scripts/tests/test_relay_researcher_installation.py` pins the shapes, the derived quotas, the
egress digest, the RBAC and the trust pin, and refuses including either Flux Kustomization while a
pin is a placeholder. `scripts/tests/test-relay-researcher-render.sh` builds both trees under
manifest-lint's pinned kustomize and kubeconform.

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

### Egress: what this allows and what it cannot separate

The slot's only allowance: DNS to the CoreDNS pods (UDP/TCP 53), and TCP 443 to `104.21.86.245/32`
and `172.67.138.98/32`. `relay.chifor.me` and `router.chifor.me` are Cloudflare-proxied and resolve
to exactly those two addresses (read through 1.1.1.1 on 2026-11-14; CoreDNS forwards Pods to the
public resolvers, so Pods see the same answer). Everything else is denied: the LAN `192.168.0.0/23`,
every node address, the API server, the `cloud-power` WoL listener, the NAS `10.55.0.254`, the metadata
address and every other namespace. The policy's measured digest,
`aa11c2299af12acc886696480b5800ac0b568a0b6e2813609d4ed2f7eea38405`
(Relay `candidateMeasurementDigest(candidateNetworkPolicy(...))`), is the definition's
`executionEgressArtifacts[0].sha256` and what the qualification drivers pin.

Three limits, stated rather than hidden:

1. **One rule reaches both the hub and the Router.** They share both addresses, so a NetworkPolicy
   cannot cut one without the other. The hub-partition drill
   (`researcher-hub-partition-observation`) removes one egress rule by index: for this slot the
   operator's reviewed declaration must say that rule 1 is hub **and** Router, so the drill measures a
   partition from both. A hub-only partition is not measurable on this path.
2. **The two addresses are Cloudflare anycast, shared with other Cloudflare customers.** A worker can
   open TLS to them with another SNI. Relay's and the Router's own authentication bound what it can
   do on ours; nothing at layer 3 bounds which Cloudflare-fronted zone it reaches through them. An
   FQDN or SNI rule would need a CiliumNetworkPolicy, which the drivers do not measure (they refuse any
   policy beyond the two pinned NetworkPolicies and reason only about those).
3. **DNS goes to the public resolvers through CoreDNS**, so queries themselves can carry data out.

A Cloudflare address change breaks the worker **closed**: re-read both names before each inclusion PR
(step I5) and treat a change as a policy change (new digest, new definition, re-review).

## Operator-filled values

Every source and image pin is a single value, written once:

| Value | Where | From |
| --- | --- | --- |
| `installationId` | `kubernetes/apps/relay-researcher/pins/pins.yaml` | the approval receipt (step I3) |
| `agentImage` | `pins.yaml` | `publication.image` of the agent's `deploy/worker-images/publish.py` receipt |
| `sourceCommit` | `pins.yaml` | the reviewer-merged Relay commit everything was built from |
| `installationPublicKey` | the operator's copy of the definition, then `agent/config.json` (generated) | step I3 |
| `installationPolicy.allowedImageDigests`, `allowedHarnessAdapters`, `allowedEgressDestinations`, `binding` | the operator's copy of the definition | the runtime image's publish receipt and the release's sealed researcher template and `PodRuntimeBinding` |
| `policy.*` release fields in `agent/config.json` | generated by Relay's tool | never typed by hand |
| `identity.fenceObjectUid` | regenerated `agent/config.json` (step I10) | the live fence ConfigMap after enrollment |

`scripts/relay-researcher-bundle.py import` writes the three pins and the generated `config.json`;
nothing else is edited by hand except the step I10 removal of the enrollment mount.

## Order, with a readback for each step

A failed readback stops the sequence. Use `kubectl --context admin@ai` throughout.

**I1. Preconditions.** Hub steps H1-H3 are done and read back: the reviewed release is live with its
migrations, the first recovery generation is fenced, and `GET /api/infrastructure/installations`
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

1. Pool and template approval in Relay (exact pool revision, policy digest = the local one).
2. The nineteen qualification scenarios on this slot with the merged drivers (delivery plan step 9):
   they need the signed candidate run (`candidateRuntime` values: candidate ConfigMaps,
   `bootstrapSecrets: true`, journal egress) and the independent candidate journal, each its own
   reviewed change.
3. The finite ordinary canary (step 10) and then `scripts/activate-ordinary-pod-policy.ts`'s outputs:
   staged guards, Secret quota, the in-place gate replacement and the **one-Pod quota**, plus agent
   values with `ordinaryNamespacePins` (the UIDs of step I6) and `bootstrapSecrets: true`, committed
   here, each through its own reviewed PR with the I6-style readbacks.

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

See [relay-researcher-hub-upgrade.md](relay-researcher-hub-upgrade.md#what-still-blocks-a-web-ui-created-researcher).
