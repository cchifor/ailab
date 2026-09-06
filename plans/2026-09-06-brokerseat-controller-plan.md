# BrokerSeat controller — GitOps plan (ailab)

> Program: AgentForge subscriptions — git-less `BrokerSeat` controller (Phase 2) and compliant usage (Phase 3).
> Operator decisions (2026-09-05): adds must be seamless (no PR, no manual step); Claude usage from the broker's
> rate-limit header tee only (no login-token refresher); Flux-managed seats stay in git.
> Design: two independent designs (minimal-change / security-first) judged and synthesized on 2026-09-06; the
> winner is the minimal design with the security-first grafts named below. Phase 1 (PR-gated add fixed + Retry)
> is merged and live; Phase 3a (usage copy, header meters) is merged (agentforge-platform #199); Phase 3c/3d
> (`last_relayed_at`, credential-level probe backoff) is agentforge #316 (+ platform follow-up).

Base: `cchifor/ailab` main `9c48b62` (2026-09-06). This repo carries A1–A4 (CRD, RBAC, admission, pins, env).

## Summary

1. One namespaced CRD brokerseats.agentforge.io/v1alpha1 in ns agentforge-broker; spec = {provider, account, clusterIP} ONLY (the CP names, the controller decides), immutable, name CEL-locked to broker-<provider>-<account>; status subresource written only by the provisioner; every rendered child is owner-referenced (controller+blockOwnerDeletion) and labelled agentforge.io/broker-seat.
2. The EXISTING provisioner (SA openbao/agentforge-provisioner, orchestrator image) gains `reconcile_seats` at the top of run_once — a level-triggered LIST per 30 s pass, per-CR isolated — behind AF_PROVISIONER_BROKERSEAT_NAMESPACE (unset = byte-identical behaviour, no new Deployment).
3. cas_required: the CP stamps it in add_account through the af-cp-sub-rotator role it already holds (metadata create/update, bootstrap.py:314-332); the controller verifies the stamp through its existing metadata read and never renders an unstamped or absent credential (#287 closed structurally, no policy change, no Job re-run).
4. Seeds: kids envelope {"registry.json": "{\"kids\": {}}"} and the ledger DSN copied from the operator-reviewed template seat (AF_PROVISIONER_SEAT_TEMPLATE_AUDS) are written create-if-absent (cas=0) under the controller token's existing af/data/operator/broker/* grant; kv_gc gains controller_auds in `in_use`; the readyz-subset refusal is never fed controller seats.
5. Rendering vendors the CP's byte-parity templates into the engine: 8 objects per seat (Deployment, PDB, headless + pinned ClusterIP Service, 3 ExternalSecrets, broker-ns CNP), image from AF_PROVISIONER_SEAT_IMAGE which `just pin-bootstrap` rewrites in lockstep (pin-image-digests.py REF_RE is context-free).
6. Inventory: in-process SeatView feeds the seat inventory (all CR seats), the kid declaration (entitlements cloned from the template seat for every workspace already entitled to it — no ConfigMap edit, explicit ConfigMap entries still win), the activation barrier (Ready seats ONLY, evaluated per poll — #127 closed) and the KV GC; workers derive any unmapped aud's URL from AF_SANDBOX_BROKER_URL_TEMPLATE and the existing per-Job endpoint resolution still gates it — no RBAC, no re-render per seat, tenant pools included.
7. RBAC: one Role/RoleBinding pair in agentforge-broker for the provisioner (CR get/list/patch + status patch + finalizers update; children get/create/update — NO delete, NO watch, NO secrets/configmaps/pods) and one for the CP (CR create/get/list/delete only), plus two fail-closed VAPs pinning what each SA may write.
8. CP: adapters/kube/seats.py (sync kubernetes client in a thread, jobs.py/fleet.py precedent), AFP_SEAT_MODE=gitops|controller (default gitops), ONE new state `provisioning`, SeatEvidence branch in advance(), per-account remove mode, cancel = Foreground-delete the CR, address released on CR 404 (the apiserver is the witness), drift banner reads CR set ∪ env vs DB, rollback = flip the env var.
9. Phase 3: the heartbeat CronJob is NOT viable (sandbox-guard.yaml:52-56 forbids init containers; anywhere else = a new secret holder + a CNP rule on every seat) — replaced by last_relayed_at on /internal/usage; the hourly 429s are stopped by a capped scope_missing/unauthorized probe backoff reset on credential-generation change (no election, no replica identity).
10. Eleven PRs across three repos, each feature-flagged and independently revertible, rolled out by pin bumps; the operator only merges (plus a one-time tenant-pool rollout to pick up the worker URL template and one live wizard add as the recorded proof).

## Decisions this repo implements

### D1

**Choice.** CRD `brokerseats.agentforge.io`, group `agentforge.io`, version `v1alpha1` (served+storage), kind `BrokerSeat`, plural `brokerseats`, shortName `bseat`, scope Namespaced; objects live ONLY in ns `agentforge-broker` (same ns as every child, so ownerReferences are legal). File `kubernetes/apps/infrastructure/agentforge-broker/brokerseat-crd.yaml` (name deliberately outside the `broker-*.yaml` glob of scripts/gen-broker-inventory.py:278). spec = {provider: enum[anthropic, openai]; account: `^[a-z0-9]+(?:-[a-z0-9]+)*$`, minLength 1, maxLength 35, reserved slugs refused by CEL; clusterIP: IPv4 regex confined to 10.96.0.0/12 (the Service CIDR; the CP pool 10.96.0.192/26 is inside it)} — ALL THREE ALLOCATED/NAMED BY THE CP (clusterIP from `_allocate_cluster_ip`, api/subscriptions.py:833, the existing uniqueness authority `uq_subaccount_cluster_ip`), NOTHING ELSE. Root CEL: `self.metadata.name == 'broker-' + self.spec.provider + '-' + self.spec.account` and `size(self.metadata.name) <= 52` (= the CP's `_STEM_MAX`, broker_renderer.py:118, so `<name>-headless`/`-ledger` fit 63). Spec transition rule `self == oldSelf` (immutable: a re-pinned Service cannot be updated in place; change = delete + re-create). status subresource: `aud`, `phase` enum {Pending, Seeding, Rendering, Ready, Degraded, Terminating}, `observedGeneration`, `reason` (≤64), `message` (≤1024), `readyReplicas`, `renderDigest`, `image`, `serviceURL`, `headlessURL`, `lastReconciledAt`, `conditions[]` (map-listed by `type` ∈ {CredentialPresent, CasRequired, Seeded, Rendered, Available, Ready, Entitled, Collision}). Printer columns Aud/Phase/ClusterIP/Ready/Reason(priority 1)/Age. Finalizer `agentforge.io/brokerseat` (added by the controller on first sight). Every rendered child carries `ownerReferences: [{apiVersion: agentforge.io/v1alpha1, kind: BrokerSeat, name, uid, controller: true, blockOwnerDeletion: true}]`, label `agentforge.io/broker-seat: <name>`, annotation `agentforge.io/render-digest: sha256(<canonical JSON of the un-stamped objects>)` and NO Flux label. No `kidBarrier` field (barrier membership is Ready-derived, D4) and no `entitleLike` field (the template seat is operator env, D3/D4).

**Why.** Decision 1: the CP supplies exactly the triple `render_broker_manifest` takes today (`__STEM__`/`__AUD__`/`__CLUSTER_IP_BLOCK__`) so no new CP authority is created; decision 3: a CR can never take a hand-named git stem (`KNOWN_STEMS`, broker_renderer.py:110-114) because the name CEL forces the mechanical rule, and un-Flux-labelled owner-referenced children are invisible to `prune: true` (kubernetes/apps/clusters/ai/agentforge-broker.yaml:21, docs/runbooks/agentforge.md:353). Keeping allocation in the CP avoids a second ClusterIP authority. Rejected: kidBarrier/entitleLike in spec (Candidate 1 — policy levers in the CP's hands), optional clusterIP (two allocators), cluster scope (breaks ownerRef GC), a `spec.image` (the digest is estate-pinned, D5).

**Residual risk.** A hand-made CR with a clusterIP inside the CIDR but outside AFP_BROKER_CLUSTERIP_POOL is admitted; the apiserver refuses the Service on conflict and the controller reports Degraded/ClusterIPRejected — late but never silent. The CP is the only intended writer (VAP, D7).

### D7

**Choice.** File `kubernetes/apps/infrastructure/agentforge-broker/brokerseat-rbac.yaml` (reaper-rbac.yaml cross-ns shape: SA in one ns, Role+RoleBinding in the target ns). Role `agentforge-provisioner-brokerseats` (ns agentforge-broker) bound to SA `agentforge-provisioner` (ns openbao): [agentforge.io brokerseats: get, list, patch] (patch = finalizer add/remove); [agentforge.io brokerseats/status: patch]; [agentforge.io brokerseats/finalizers: update] (blockOwnerDeletion under OwnerReferencesPermissionEnforcement); [apps deployments: get, create, update]; ["" services: get, create, update]; [policy poddisruptionbudgets: get, create, update]; [external-secrets.io externalsecrets: get, create, update]; [cilium.io ciliumnetworkpolicies: get, create, update]. NO delete (GC via ownerReferences + Foreground), NO watch, NO list on children (GET by derived name), NO secrets/configmaps/pods. The existing ClusterRole `agentforge-provisioner-namespaces` is untouched. Role `af-cp-brokerseats` (ns agentforge-broker) bound to SA `agentforge-platform` (ns agentforge): [agentforge.io brokerseats: create, get, list, delete] — NO update/patch, NO status, nothing on the child kinds (agentforge-codex-refresh/rbac.yaml shape and comment style). File `brokerseat-admission.yaml`: VAP `agentforge-cp-brokerseat-guard` (matchConditions username == system:serviceaccount:agentforge:agentforge-platform; brokerseats CREATE): `!has(object.metadata.finalizers) && !has(object.metadata.ownerReferences) && !has(object.metadata.labels) && !has(object.metadata.annotations) && object.metadata.name == 'broker-' + object.spec.provider + '-' + object.spec.account`. VAP `agentforge-provisioner-seat-objects-guard` (username == system:serviceaccount:openbao:agentforge-provisioner; CREATE/UPDATE of deployments, services, poddisruptionbudgets, externalsecrets, ciliumnetworkpolicies in ns agentforge-broker): `object.metadata.name.startsWith('broker-') && 'agentforge.io/broker-seat' in object.metadata.labels && !('kustomize.toolkit.fluxcd.io/name' in object.metadata.labels) && (oldObject == null || !('kustomize.toolkit.fluxcd.io/name' in (has(oldObject.metadata.labels) ? oldObject.metadata.labels : {})))`, plus for externalsecrets `object.spec.secretStoreRef.name == 'agentforge-broker-store'` and every remoteRef key `startsWith('operator/broker/')`, and for deployments `object.spec.template.spec.serviceAccountName == 'agentforge-broker'` and the container image matches `^registry\.chifor\.me/agentforge/orchestrator@sha256:[0-9a-f]{64}$`. Both `failurePolicy: Fail`, bound with `validationActions: [Deny]`. CEL logic proven by `scripts/check-seat-guard-cel.py` = the existing cel-python harness (`scripts/check-tenant-guard-cel.py`) with its evaluator generalised to a `--policy` argument and a fixture table for both policies, run from the existing `tenant-guard-cel.yaml` workflow. No change to the provisioner CNP (egress already admits kube-apiserver + component=broker pods, provisioner-deploy.yaml CNP).

**Why.** Exactly the verbs the reconcile pseudocode issues, per namespace, SA-scoped like every estate precedent; the provisioner still cannot read any Secret anywhere. `create` cannot be resourceName-scoped (k8s ignores it, agentforge-codex-refresh/rbac.yaml:9) — hence the VAPs, which are this estate's admission style (VAP-only, cp-flux-guard.yaml matchConditions on username). The objects-guard makes 'never touch a Flux-labelled object' an admission fact, closing decision 3 structurally; the CP guard makes it impossible for the CP to fake ownership or readiness. Candidate 2's claim that check-tenant-guard-cel.py already covers new VAPs is false (it is hard-wired to tenant-guard.yaml) — the harness is generalised instead.

**Residual risk.** `update` on Deployments in agentforge-broker lets a compromised provisioner rewrite a git seat's Deployment for ≤10 min (Flux SSA re-asserts) — the VAP refuses any object carrying a Flux label, so this is closed at admission unless VAP admission is disabled. Pinned by scripts/tests/test_brokerseat_rbac.py asserting the exact verb sets.

### D12

**Choice.** Order: E1 → E2 → E3 (engine, all inert without env) ‖ A1 (ailab CRD+RBAC+VAPs, inert) → A2 (ailab: orchestrator + p1-worker pin bump + AF_PROVISIONER_BROKERSEAT_NAMESPACE + AF_PROVISIONER_SEAT_IMAGE + AF_PROVISIONER_SEAT_TEMPLATE_AUDS + worker AF_SANDBOX_BROKER_URL_TEMPLATE; the controller runs with zero CRs = no-op) → P1 (CP: require_cas + controller mode behind AFP_SEAT_MODE=gitops default) → P2 (CP compose seat-stub + integration spec) → A3 (ailab: CP pin bump + AFP_SEAT_MODE=controller — the one behaviour flip) → E4 (broker last_relayed_at + backoff, independent) → A4 (orchestrator pin bump). Feature flags: engine `AF_PROVISIONER_BROKERSEAT_NAMESPACE`, worker `AF_SANDBOX_BROKER_URL_TEMPLATE`, CP `AFP_SEAT_MODE`. Every PR is a single revert: A3 revert = PR-gated adds again with controller seats still removable; A2 revert = controller off (existing CRs keep their objects via ownerRefs until deleted); A1 revert after CRs exist is refused by the runbook (delete CRs first — CRD deletion cascades). No PR touches capability-kids-configmap.yaml, the SOPS seeds, gen-broker-inventory.py or its derived spans, so the broker-inventory gate and check-inline-hashes.py never go red.

**Why.** Each step is observable before the next: A2 proves the loop is healthy on zero CRs (af_provisioner_ops_total{action=brokerseat-*}, no brokerseat-* alerts); P1/P2 prove the CP path against the stub and the real OpenBao (require_cas negative probe); A3 is the one flip and a one-line revert. The CRD lands before any controller code can see it (A1 ≺ A2) and the CP switch lands last (A3) so no CR is ever created without a controller to own it.

**Residual risk.** Between A2 and A3 the wizard still opens PRs (gitops mode) — intended. The approval-gated bot PRs are the only place a human click remains on the critical path.


## CRD (A1)

```yaml
# kubernetes/apps/infrastructure/agentforge-broker/brokerseat-crd.yaml
# BrokerSeat — a git-less broker seat materialised by the EXISTING agentforge provisioner
# (SA openbao/agentforge-provisioner, orchestrator image). One CR == one seat == the same 8 objects a
# broker-*.yaml carries (Deployment, PDB, 2 Services, 3 ExternalSecrets, CiliumNetworkPolicy), rendered
# byte-for-byte from the vendored seat templates and OWNED by the CR (ownerReferences, controller +
# blockOwnerDeletion), so `kubectl delete bseat` (the CP deletes with propagationPolicy=Foreground)
# cascades and Flux prune (label-tracked) never sees them. Flux-managed seats stay in broker-*.yaml:
# the metadata.name rule makes a CR unable to take a hand-named git stem, and the controller refuses any
# aud the readyz map already serves. THE CP MAY ONLY NAME provider/account/clusterIP (VAP
# agentforge-cp-brokerseat-guard); entitlement, ledger source, image and barrier membership are decided
# by the provisioner from operator env (AF_PROVISIONER_SEAT_TEMPLATE_AUDS, AF_PROVISIONER_SEAT_IMAGE).
# cas_required on the seat's oauth path is stamped by the CP (af-cp-sub-rotator) and VERIFIED here.
# NOTE the file name deliberately does not match broker-*.yaml (scripts/gen-broker-inventory.py glob).
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: brokerseats.agentforge.io
  labels:
    app.kubernetes.io/name: agentforge-broker
    app.kubernetes.io/component: brokerseat-crd
    app.kubernetes.io/managed-by: agentforge-operator
    app.kubernetes.io/part-of: agentforge
spec:
  group: agentforge.io
  scope: Namespaced
  names:
    kind: BrokerSeat
    listKind: BrokerSeatList
    plural: brokerseats
    singular: brokerseat
    shortNames: [bseat]
  versions:
    - name: v1alpha1
      served: true
      storage: true
      subresources:
        status: {}
      additionalPrinterColumns:
        - { name: Aud,       type: string,  jsonPath: .status.aud }
        - { name: Phase,     type: string,  jsonPath: .status.phase }
        - { name: ClusterIP, type: string,  jsonPath: .spec.clusterIP }
        - { name: Ready,     type: integer, jsonPath: .status.readyReplicas }
        - { name: Reason,    type: string,  jsonPath: .status.reason, priority: 1 }
        - { name: Age,       type: date,    jsonPath: .metadata.creationTimestamp }
      schema:
        openAPIV3Schema:
          type: object
          required: [spec]
          x-kubernetes-validations:
            # A controller seat is ALWAYS named broker-<provider>-<account>: it can never take the
            # hand-named stems of the git seats (broker-anthropic-max1, broker-openai-codex, ...).
            - rule: "self.metadata.name == 'broker-' + self.spec.provider + '-' + self.spec.account"
              message: "metadata.name must equal broker-<spec.provider>-<spec.account>"
            # = the CP renderer's _STEM_MAX: <name>-headless / <name>-ledger must fit 63 characters.
            - rule: "size(self.metadata.name) <= 52"
              message: "name too long: pick a shorter account slug"
          properties:
            spec:
              type: object
              required: [provider, account, clusterIP]
              x-kubernetes-validations:
                # Immutable: a pinned Service cannot be re-pinned in place. Change = delete + re-create.
                - rule: "self == oldSelf"
                  message: "spec is immutable; delete and re-create the BrokerSeat"
              properties:
                provider:
                  type: string
                  enum: [anthropic, openai]
                account:
                  type: string
                  minLength: 1
                  maxLength: 35
                  pattern: "^[a-z0-9]+(?:-[a-z0-9]+)*$"
                  x-kubernetes-validations:
                    - rule: "!(self in ['tenants', 'operator', 'shared', 'default', 'headless'])"
                      message: "reserved account slug"
                clusterIP:
                  # Allocated by the control plane from AFP_BROKER_CLUSTERIP_POOL (10.96.0.192/26); MUST be
                  # inside the cluster Service CIDR 10.96.0.0/12 or the apiserver refuses the Service.
                  type: string
                  pattern: "^10\\.(9[6-9]|10[0-9]|11[01])\\.(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\\.(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])$"
            status:
              type: object
              properties:
                aud: { type: string }
                phase:
                  type: string
                  enum: [Pending, Seeding, Rendering, Ready, Degraded, Terminating]
                observedGeneration: { type: integer, format: int64 }
                reason: { type: string, maxLength: 64 }
                message: { type: string, maxLength: 1024 }
                readyReplicas: { type: integer, minimum: 0 }
                renderDigest: { type: string, maxLength: 71 }
                image: { type: string, maxLength: 256 }
                serviceURL: { type: string }
                headlessURL: { type: string }
                lastReconciledAt: { type: string, format: date-time }
                conditions:
                  type: array
                  x-kubernetes-list-type: map
                  x-kubernetes-list-map-keys: [type]
                  items:
                    type: object
                    required: [type, status]
                    properties:
                      type:
                        type: string
                        enum: [CredentialPresent, CasRequired, Seeded, Rendered, Available, Ready, Entitled, Collision]
                      status:
                        type: string
                        enum: ["True", "False", "Unknown"]
                      reason: { type: string, maxLength: 64 }
                      message: { type: string, maxLength: 1024 }
                      observedGeneration: { type: integer, format: int64 }
                      lastTransitionTime: { type: string, format: date-time }
```

## PR ladder (this repo)

### A1 — agentforge-broker: BrokerSeat CRD + provisioner/CP seat RBAC + two VAP guards + runbook (inert: no controller, no CRs)

Depends on: nothing. Size ≈ 750 lines.

Files:
- `kubernetes/apps/infrastructure/agentforge-broker/brokerseat-crd.yaml`
- `kubernetes/apps/infrastructure/agentforge-broker/brokerseat-rbac.yaml`
- `kubernetes/apps/infrastructure/agentforge-broker/brokerseat-admission.yaml`
- `kubernetes/apps/infrastructure/agentforge-broker/kustomization.yaml`
- `scripts/check-seat-guard-cel.py (the tenant-guard cel-python evaluator generalised to --policy + fixture table)`
- `scripts/tests/test_brokerseat_crd.py`
- `scripts/tests/test_brokerseat_rbac.py`
- `scripts/tests/test_brokerseat_admission.py`
- `.gitea/workflows/tenant-guard-cel.yaml (also run check-seat-guard-cel.py)`
- `docs/runbooks/agentforge.md (controller seats: ownership labels, refusal reasons, Foreground teardown, revert order 'delete CRs before the CRD', DR = re-add from the wizard)`

Tests (each safety property has a test that goes red when its gate is deleted; mutation-check before the PR):
- test_brokerseat_crd.py::test_crd_has_name_and_immutability_cel_rules; ::test_crd_status_subresource_and_printer_columns; ::test_seat_files_do_not_match_the_broker_glob (gen-broker-inventory --check still 'OK — 4 seats')
- test_brokerseat_rbac.py::test_provisioner_role_verbs_exact_no_delete_no_watch_no_secrets; ::test_cp_role_is_cr_only_no_status_no_patch
- test_brokerseat_admission.py::test_vaps_match_only_their_sa_and_fail_closed; scripts/check-seat-guard-cel.py fixture table (CP: finalizers/ownerRefs/labels denied, derived name enforced; provisioner: Flux-labelled target denied, foreign store denied, non-broker- name denied)
- scripts/manifest-lint.sh (kustomize build; kubeconform skips the kind)

Rollout: Flux applies the CRD, Roles and VAPs; nothing reads them yet — zero behaviour change.

### A2 — provisioner: enable the BrokerSeat controller (orchestrator + p1-worker pin bump to E1–E3, AF_PROVISIONER_BROKERSEAT_NAMESPACE, AF_PROVISIONER_SEAT_IMAGE, AF_PROVISIONER_SEAT_TEMPLATE_AUDS, worker AF_SANDBOX_BROKER_URL_TEMPLATE)

Depends on: A1, E2, E3. Size ≈ 60 lines.

Files:
- `kubernetes/apps/infrastructure/security/openbao/provisioner-deploy.yaml (3 env lines + digest)`
- `kubernetes/apps/infrastructure/security/openbao/provision-job.yaml (digest, via pin script)`
- `kubernetes/apps/infrastructure/agentforge-broker/broker-*.yaml (digest, via pin script)`
- `kubernetes/apps/infrastructure/agentforge-workers/worker-deployment.yaml (AF_SANDBOX_BROKER_URL_TEMPLATE + p1-worker digest)`
- `scripts/tests/test_provisioner_seat_image_pin.py`

Tests (each safety property has a test that goes red when its gate is deleted; mutation-check before the PR):
- test_provisioner_seat_image_pin.py::test_seat_image_digest_equals_the_provisioner_container_digest; ::test_pin_image_digests_dry_run_rewrites_both_lines
- test_cp_env.py (existing invariants) + gen-broker-inventory.py --check (derived spans untouched) + check-inline-hashes.py (ConfigMap untouched)
- ailab-pin-guard (existing)

Rollout: `just pin-bootstrap orchestrator=sha256:… platform=<current>` + `just pin-workloads p1-worker=sha256:…` as a branch PR (or the bot's AGit PR once approvals are unblocked); Recreate rolls the provisioner; with zero CRs the new stage is a no-op — verify af_provisioner_seats{} == 0 and no brokerseat-* alerts. Revert = env unset / pin back; nothing to clean.

### A3 — agentforge-platform: AFP_SEAT_MODE=controller (CP pin bump to P1)

Depends on: A2, P1. Size ≈ 20 lines.

Files:
- `kubernetes/apps/apps/agentforge/deployment.yaml (1 env line + digest)`
- `scripts/tests/test_cp_env.py (AFP_SEAT_MODE ∈ {gitops, controller})`

Tests (each safety property has a test that goes red when its gate is deleted; mutation-check before the PR):
- test_cp_env.py::SeatModeEnv::test_value_is_gitops_or_controller
- ailab-pin-guard

Rollout: The behaviour flip. Live proof: one wizard add on ailab → `kubectl -n agentforge-broker get bseat` shows Phase Ready within ~5 min, tracker reaches active, no PR opened; delete from the wizard → objects gone, address released, git seats and kid activation untouched (watch af_provisioner_alerts_total). Rollback = revert this PR (existing CRs keep serving).

### A4 — broker/provisioner: orchestrator pin bump to E4 (Phase 3)

Depends on: E4, A2. Size ≈ 20 lines.

Files:
- `kubernetes/apps/infrastructure/agentforge-broker/broker-*.yaml (digest)`
- `kubernetes/apps/infrastructure/security/openbao/provisioner-deploy.yaml (digest, both lines)`
- `kubernetes/apps/infrastructure/security/openbao/provision-job.yaml (digest)`

Tests (each safety property has a test that goes red when its gate is deleted; mutation-check before the PR):
- gen-broker-inventory.py --check
- test_provisioner_seat_image_pin.py

Rollout: Pin PR; brokers roll (RollingUpdate, PDB minAvailable 1); watch af_broker_usage_probe_total{outcome=scope_missing} fall to ≤1/6h/replica.


## Operator actions (merge-only by design)

- Merge the PRs in ladder order: E1 → E2 → E3, A1, A2, P1, P2, A3, E4, A4. No OpenBao ceremony, no capability-kids ConfigMap edit, no SOPS seeds edit, no kubectl, no PR per seat.
- Land the three pin bumps (A2, A3, A4) as branch PRs (as done for #492/#493) until the forge-admin fix for approval-gated bot AGit runs lands; each is `just pin-bootstrap` / `just pin-workloads` output.
- ONE-TIME after P1 + A2: trigger a rollout of the existing CP-rendered tenant pools from the UI so their worker Deployments pick up AF_SANDBOX_BROKER_URL_TEMPLATE (or accept that each pool picks it up on its next change). Never needed again per seat.
- After A3: perform one wizard add on ailab and record the live proof in docs/runbooks/agentforge.md (`kubectl -n agentforge-broker get bseat`, tracker reaches active, no PR opened) — the only step the unit/compose tiers cannot substitute for.

## Open questions for the operator (defaults assumed until answered)

- Template seats per provider (the A2 env AF_PROVISIONER_SEAT_TEMPLATE_AUDS): new controller seats copy the ledger DSN from, and inherit the per-workspace entitlements of, one reviewed seat per provider — proposed {"anthropic": "anthropic/claude-max-1", "openai": "openai/codex-pro"}. Confirm, or name different seats (an explicit capability-kids ConfigMap entry for a controller seat always overrides the clone).
- Phase 3c later: is a SCHEDULED GENUINE tenant job (a Gitea Actions `schedule:` workflow in a tenant repo through the existing dispatcher webhook; one real `claude -p` turn from an ordinary sandbox pod; no new secret holder, no new Deployment) acceptable as 'ordinary use' under decision 2 to keep idle seats' usage fresh — or should idle seats simply show 'last genuine traffic <ago>' (the default this design ships)?

Assumed defaults: `AF_PROVISIONER_SEAT_TEMPLATE_AUDS={"anthropic":"anthropic/claude-max-1","openai":"openai/codex-pro"}`; no scheduled heartbeat job — idle seats show the last genuine traffic.

## Unverified assumptions (each is proven by the named test or by the first live add)

- kubernetes_asyncio's generated client accepts dict bodies for replace_namespaced_{deployment,service,pod_disruption_budget} and for CustomObjectsApi.replace_namespaced_custom_object / patch_namespaced_custom_object_status with the resourceVersion carried — assumed from the upstream client; the engine has no CustomObjectsApi precedent (repo-wide grep).
- Root-level x-kubernetes-validations referencing self.metadata.name and the `self == oldSelf` spec transition rule on k8s 1.31 (GA since 1.29) — from upstream knowledge; kubeconform skips the kind and no in-repo harness evaluates CRD CEL.
- Foreground cascading deletion removes blockOwnerDeletion dependents before the owner while a custom finalizer is also present on the owner — standard GC semantics, proven live only by A3's first remove.
- OpenBao 2.5.5 KV-v2 accepts a metadata POST carrying only {"cas_required": true} and leaves custom_metadata untouched — documented for hvac's update_metadata in bootstrap.py:1566-1576; the raw POST from the CP is first exercised by P2's compose probe against OpenBao 2.4.1.
- The LIVE vault's af-cp-sub-rotator policy contains the metadata create/update stanzas bootstrap.py:314-332 declares (roles were provisioned once under root; a vault bootstrapped by an older image could lack them — a refusal strands the add visibly).
- hvac's read_secret_metadata response `data` carries `cas_required` (KV-v2 metadata standard field) — not asserted by any test in the repo today.
- Whether Talos enables the OwnerReferencesPermissionEnforcement admission plugin (decides if brokerseats/finalizers update is actually needed; granted regardless).
- The exact CP renderer seam for one worker env line (renderer.py, 2646 lines, not opened) and the effort to generalise scripts/check-tenant-guard-cel.py's cel-python evaluator to a second policy file (it is hard-wired to tenant-guard.yaml today).
- The per-seat operator/broker/*/ledger docs carry an identical AF_BROKER_LEDGER_DSN (encrypted SOPS seeds; inferred from the manifest comments naming a single agentforge_broker role) — the template-aud env makes the choice explicit either way.
- That the pin bot's AGit flow stays approval-gated (tooling map) — the ladder assumes branch PRs for A2/A3/A4.


## Amendments after the codex plan review (round 1, 2026-09-06)

Numbered by the review's findings. Folded in unless marked *rejected*.

**F1 (BLOCKER, engine E2).** CRDs do not support strategic-merge patch. Finalizer add/remove use RFC 6902 JSON Patch with a `test` precondition on `metadata.finalizers` (the `KubernetesNamespaceClient.remove_finalizer` pattern), content type `application/json-patch+json`; status writes use `application/merge-patch+json` on the `/status` subresource. `test_provisioner_seat_kube.py` asserts the request content types, not only bodies.

**F2 (BLOCKER, platform P1).** Cancel is never immediately terminal. Cancel of an add: Foreground-delete the CR and mark `detail.cancelling=true`; the row stays `provisioning` until the seat is corroborated gone (F12), then `rejected` → address released. Cancel of a remove whose DELETE was accepted is refused (409 `teardown_in_progress`); a remove never maps back to `active`. Tests bless the delayed release, never an immediate one.

**F3 (HIGH, platform P1).** Management mode is durable per account: `subscription_accounts.seat_mode` (`gitops|controller`) + `seat_uid` (the CR uid) set at add; the seat client is wired whenever a kube config is available (in-cluster or `AFP_SEAT_KUBECONFIG`), independent of `AFP_SEAT_MODE`, which governs NEW adds only. Remove/repair route by the persisted mode, never by "is a CR present right now". `_Inventory.cr` distinguishes `None` (unreadable → banner, no refusal) from an empty set. DR for an out-of-band-deleted CR is **Repair** on the row (re-create the CR from the persisted spec; membership check exempts the row's own aud), not a duplicate add.

**F4 (HIGH, platform P1).** The add is a checkpointed, resumable sequence in BOTH modes: `detail.add_checkpoint ∈ {reserved, credential_written, cas_stamped, metadata_stamped, seat_created|pr_opened}`; Retry resumes at the first incomplete step and runs every remaining step in order (gitops Retry stamps `cas_required` too before `_retry_pr`). Ambiguous responses (timeout after a write) re-read before re-writing (metadata read for the stamp; CR GET for the create; a terminating CR is never adopted — wait for 404, then re-create).

**F5 (HIGH, engine E2).** Every desired child is inspected on every pass: GET by name → 404 ⇒ create; owned ⇒ compare the per-object `agentforge.io/render-digest` (sha256 of that object's canonical desired body) ⇒ noop or replace with the live resourceVersion; foreign ⇒ collision. A deleted Service/ExternalSecret/CNP is therefore re-created within one pass; an image bump replaces only the Deployment (its digest alone changes). `cr.status.renderDigest` becomes the digest over the eight per-object digests (informational).

**F6 (HIGH, engine E2).** Membership is derived from the CR LIST, not from reconcile success: every non-terminating CR enters the `SeatView` as a member (inventory, declaration clone, KV-GC `in_use`) before its reconcile runs; reconcile only sets `ready` and the URLs. Diagnostic `_status` writes in the per-CR error path are wrapped so a failing status write cannot escape the isolation boundary. Tests: a child failure followed by a status failure leaves the seat a member and the other seats and the tenant pass untouched.

**F7 (HIGH, engine E2).** On a LIST failure the last-good `SeatView` is kept for membership AND for `barrier_urls()` (the previous Ready set); a seat that is genuinely gone fails its poll and defers activation for its audience, which is the safe direction. The barrier never commits a kid whose audience includes a controller seat while that seat's state is unknown. Test through the real publisher/barrier path.

**F8 (HIGH, engine E2) — accepted as the git-seat semantics.** The barrier polls each seat's headless URL once per poll; readiness of the set is the pass-start snapshot, exactly as the static git list today; an unready replica answering the headless URL fails the GET and defers the commit to the next pass. Documented; no change.

**F9 (HIGH, engine/platform).** The CR carries two entitlement conditions: `Entitled` (the extended declaration names the aud for ≥1 workspace) and `Published` (the aud's kids registry doc holds ≥1 committed kid — read via the fail-closed `read_secret_versioned`). `active` keeps meaning "the broker serves the pasted credential"; the tracker shows `Published=False` as "entitlement pending: no workspace kid committed yet" on the seat line, and the live A3 proof executes an ordinary authorized sandbox request against the new audience from a tenant pool that starts scaled to zero.

**F10 (HIGH, ailab A1).** The objects guard binds structure, not just names: Deployment — exactly one container, image matches the pinned orchestrator regex, `serviceAccountName: agentforge-broker`, every Secret volume / `secretKeyRef` / `envFrom` names only `<stem>-oauth|-kids|-ledger` (stem derived from `object.metadata.name`), no hostPath/projected-token/other volume types beyond what the template renders, ownerReferences exactly one BrokerSeat controller ref named `<stem>`; ExternalSecret — `secretStoreRef` kind `SecretStore` + name `agentforge-broker-store`, `target.name == <stem>-<kind>`, every `remoteRef.key == operator/broker/<provider>/<account>/<kind>` for the stem's provider/account, no `dataFrom`; Services — selector equals the seat's own labels. Adversarial fixtures for each in the cel harness.

**F11 (HIGH, ailab A1).** CNP: `endpointSelector` must select exactly the seat's own pod identity (the template's instance label = `<stem>`), never empty and never a git seat; only the template's rule shape; no `specs`, no deny rules. Fixture: a `broker-x` CNP selecting a git seat is denied.

**F12 (HIGH, engine/platform/ailab).** Teardown witness = CR 404 **and** the pinned Service `<stem>` 404 (CP Role gains `services get` in agentforge-broker, read-only); the controller quiesces rendering for a CR whose deletionTimestamp is set (already) and additionally re-GETs the CR (uid precondition) before any create so a stale pass never creates children for a deleting CR. Test: deletion interleaved with child creation.

**F13 (HIGH, engine E1/E2).** Ledger seed: on CAS conflict the existing destination doc must carry a non-empty `AF_BROKER_LEDGER_DSN` (else Degraded/LedgerDocInvalid); each pass re-reads the template seat's ledger doc and, if its DSN differs from the destination's, CAS-writes the destination's `AF_BROKER_LEDGER_DSN` at the destination's current version (owned field only). Tests: malformed existing destination, source rotation propagates, delete/re-add with retained docs converges.

**F14 (HIGH, all repos) — documented limitation, out of scope.** The Codex refresher CronJob serves one pinned oauth path (`CODEX_REFRESHER_OAUTH_PATH`): an additional OpenAI seat added from the wizard is `refreshable: false` and renews by paste (the CP already models per-account refreshability and refuses `refresh-now` with 422). A seat-scoped refresher is a separate follow-up; the runbook and the wizard copy say so for openai adds.

**F15 (HIGH) — rejected.** The Anthropic usage probe stays enabled with the credential-level backoff (#316): calling the vendor's usage endpoint with the operator's own setup-token is not the prohibited use (that is intermediating consumer login tokens), the 403 costs nothing at ~4 calls/day/replica, and it turns `ok` automatically if Anthropic ever grants the scope. Claude meters are populated only from relayed-response headers, as decided.

**F16 (HIGH, ailab/platform ladder).** A3 splits: **A3a** = CP pin bump to P1 with `AFP_SEAT_MODE=gitops` (behaviour unchanged) + a one-time rollout of every existing tenant pool so their worker Deployments carry `AF_SANDBOX_BROKER_URL_TEMPLATE`; verify the zero-to-running path once; **A3b** = flip `AFP_SEAT_MODE=controller`. P2 (compose seat-stub lane) must be green before A3b.

**F17 (HIGH, all repos).** Rollback is configuration-only: `AFP_SEAT_MODE=gitops` stops new controller adds while the CP keeps reading/deleting/repairing existing controller seats; `AF_PROVISIONER_BROKERSEAT_NAMESPACE` stays set so existing seats keep reconciling and finalizers keep working. Image or CRD rollback requires tearing down every controller seat first (runbook order: remove from the wizard → CRs gone → then revert). Tested with an active and an in-flight controller seat.

**F18 (HIGH, engine/ailab).** A disposable `kind` (k8s 1.31) check runs in a leased pool env before A3b: `scripts/seat-kind-check.sh` applies the A1 manifests (CRD, RBAC, VAPs) to a kind cluster, then drives the real `KubernetesSeatApi` and the CP's `KubeBrokerSeats` against it: CRD CEL (bad name, immutable spec), effective RBAC (provisioner cannot delete children, CP cannot patch status), VAP denials (the adversarial fixtures), finalizer patch content types, Service replace with a pinned clusterIP, Foreground deletion ordering with a child created concurrently. Its verdict is attached to the A3b PR. The API-object fakes stay as unit tests.

**F19 (MEDIUM, engine E2).** `_status` compares the semantic status without `lastReconciledAt`; the timestamp is written only when something else changes, plus a heartbeat at most every 10 minutes. Tests advance the clock between otherwise identical passes and assert no write.

**F20 (MEDIUM, engine E4) — already implemented in #316 round 1:** credential-level waits are sliced per base interval and end on a generation change; the first repeat waits one base interval; jitter is applied before the cap.

**F21 (MEDIUM, platform).** The usage presentation follow-up exists: agentforge-platform #202 (`last_relayed_at` normalisation + presenter + mock/stub + docs), independent of ordering with #316 (null from an older broker). Added to the ladder as **P0**.

**F22 (MEDIUM, engine/platform).** A persistent ClusterIP rejection (`Degraded/ClusterIPRejected` for > 2 passes) is a Retry-able strand: Retry = Foreground-delete the CR, wait for CR + Service 404, release the address in the DB, allocate a fresh one, re-create the CR with the same name; credential provenance (kv version, generation) is kept on the operation row. Test: a persistent collision, not only a delayed release.

### Ladder after the amendments

P0 (#202, open) · E1 → E2 → E3 · A1 · A2 (controller enabled with zero CRs) · P1 (+P1b if split) · P2 · **A3a** (CP pin, `AFP_SEAT_MODE=gitops`, tenant-pool rollout) · kind check (F18, attached to A3b) · **A3b** (`AFP_SEAT_MODE=controller`) · E4 (#316) · A4 (broker pin).


## Amendments after the codex plan review (round 2, final — 2026-09-06)

Round 2 marked 17 findings resolved, 5 partial, F15 rejected-by-decision, and returned NOT READY on five items. These close them; the review cap (two rounds) is reached, and F15 (keep the six-hourly Anthropic usage probe) is recorded as an operator-visible decision, not a blocker.

**N1 (HIGH, engine E2) — first finalizer install.** A CP-created CR has no `metadata.finalizers` field. `add_finalizer` sends a JSON Patch whose `test` targets `metadata.resourceVersion` (the value from the GET) and `add`s `/metadata/finalizers` as a one-element array when the field is absent; when present it `test`s the current array and `add`s `/metadata/finalizers/-`. Both bodies are unit-tested and both run in the kind check (F18).

**F7 (HIGH, engine E2) — unknown states are explicit.** `SeatViewHolder` starts in `UNKNOWN` (no successful LIST since process start). While UNKNOWN: no declaration extension (no cloned entitlements are published), the seat inventory and KV-GC `in_use` are computed from the git map ∪ floor only AND every KV-GC decision that could retire an aud is suspended (the collector runs in report-only mode), and the barrier defers any kid whose audience set contains an aud outside git map ∪ floor (it cannot attribute it, so it cannot prove it). After the first successful LIST the holder is `KNOWN`; a later LIST failure keeps the last-good view (round-1 F7). A per-seat observation failure (LIST ok, the seat's own GET/reconcile fails) keeps the seat a member with `ready = last known readiness if any else False` → its audience defers. Alert `brokerseat-inventory-unknown` after 3 consecutive UNKNOWN passes.

**F10 (HIGH, ailab A1) — ExternalSecret sources.** The objects guard rejects `spec.dataFrom`, any `spec.target.template.templateFrom`, and any `spec.target.template` shape other than the one the seat template renders (if the template renders none: `!has(object.spec.target.template)`). Adversarial fixture: an ExternalSecret whose `templateFrom[].secret` names another seat's Secret is denied. The A1 implementer reads the real ExternalSecret stanzas in `broker_templates/anthropic.yaml` before writing the rule.

**F12 (HIGH, engine E2) — the finalizer is a fence.** Reconciliation is serialized per CR uid within the single provisioner loop (one pass at a time; already true). Immediately before EVERY child CREATE the controller re-GETs the owner CR by name and proceeds only if `uid == snapshot.uid` and `deletionTimestamp` is unset; the child's ownerReference carries that uid. An ambiguous create (timeout) is resolved by the next pass's GET (exists+owned → fine; absent → re-create). In the finalizer path the controller GETs the eight children by name and removes its finalizer only when all are 404 (GC deletes them under Foreground; our finalizer merely holds the CR until they are gone), so `CR 404` ⇒ children gone ⇒ pinned Service gone. Tests: deletion injected between the owner GET and a child CREATE (the create is skipped, the pass ends with phase Terminating); finalizer not removed while a child still exists; delayed completion.

**N3 (HIGH, platform P1) — UID-preconditioned deletes.** `KubeBrokerSeats.create` returns the CR uid; the CP persists `subscription_accounts.seat_uid` (and `detail.seat_uid` on the operation) before the next checkpoint. Every delete (cancel, remove, repair, ClusterIP reallocation) passes `DeleteOptions(preconditions=Preconditions(uid=<persisted>))`; a 409 Conflict means the CR was replaced out of band → the operation strands with `seat_uid_mismatch` and the seat line reads "BrokerSeat replaced out of band — Repair re-binds". Evidence (`SeatEvidence.uid`) is compared with the persisted uid; a mismatch is reported, never acted on. Tests: a delayed DELETE carrying the previous uid is a 409 and the replacement survives; repair persists the new uid before any further step.

**N2 (MEDIUM, engine E2) — ledger DSN adoption.** The broker reads the ledger DSN from a Secret-backed env var, so a propagated DSN (round-1 F13) needs a pod restart, exactly as a git seat does today. After propagating, the controller watches the `-ledger` ExternalSecret's `status.syncedResourceVersion` (get on externalsecrets is granted) and, once it moved, bumps the Deployment pod-template annotation `agentforge.io/ledger-generation: <sha8 of the DSN>` — a rolling restart under the PDB (minAvailable 1). The live proof for F13 ("recovery after revoking the old DSN") is part of the A3b runbook, not a unit test.

## Estate safeguards

- Gitea via API; explicit-path staging; no AI attribution; batched review rounds (one push per round, reply per finding);
  codex read-only cross-review of every final diff; Playwright only in a leased pool env; no live kubectl writes — every
  cluster change is an ailab PR the operator merges; ≤ 2 concurrent Claude agents.
- Every PR stays far under the reviewbot's 400 KB cap and is independently revertible; feature flags default off.

