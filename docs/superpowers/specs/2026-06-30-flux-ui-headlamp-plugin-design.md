# Flux management/monitoring web UI — design

- **Date:** 2026-06-30
- **Status:** Draft (pending codex cross-review + user approval)
- **Decision:** Add the **Headlamp Flux plugin** to the existing Headlamp deployment, granting a scoped Flux-CR write capability for "monitoring + safe ops".

## 1. Context

The lab is 100%-IaC GitOps on **Flux v2.8.8** (source/kustomize/helm/**notification** controllers — no
image-automation), tracking `main` of `cchifor/ailab`. The observability/UI stack is already deep:
Grafana (metrics), Loki (logs), Gatus (uptime), Homepage (portal), Hubble UI (network), and **Headlamp**
(k8s object explorer, PR #20) — deployed read-only behind Cloudflare Access at `k8s.chifor.me`.

The one remaining gap is a **web UI dedicated to Flux** (Kustomizations/HelmReleases/Sources sync state +
reconcile/suspend/resume). PR #20 explicitly deferred the **Headlamp Flux plugin** as a follow-up. This
design closes that gap.

## 2. Eval summary (2026 landscape)

An agentic eval (3 parallel research agents, cited) validated the choice:

| Option | 2026 status | Verdict for this lab |
|---|---|---|
| **Headlamp Flux plugin** v0.6.0 (CNCF/kubernetes-sigs) | Active | **Chosen** — reuses the deployed Headlamp, CF Access, Homepage tile, RBAC. Zero new infra. |
| **Flux Operator Web UI** (ControlPlane, GA w/ Flux 2.8, Feb 2026) | Most-active, first-party | **Graduation path** — strongest read/admin RBAC split (`flux-web-user`/`flux-web-admin` + `SelfSubjectAccessReview`), but means adopting the Flux Operator + a 2nd SSO-fronted app. |
| Weave GitOps OSS | Stalled/EOL (Weaveworks defunct; last stable Dec 2024) | Avoid — unpatched-CVE risk once exposed. |
| Capacitor (in-cluster) | Legacy (no release since Jan 2025) | Avoid for long-lived. |
| k9s flux plugin / `flux` CLI | Active | Keep as break-glass baseline (not a web UI). |

Why Headlamp wins **for this lab**: the plugin is a drop-in view inside an app already running behind the
lab's SSO/Access edge, so "view all Flux state + reconcile/suspend/resume" becomes a **plugin-install +
RBAC grant**, not a new service to deploy, expose, gate, health-check, and tile. The Flux Operator UI is
recorded as the documented graduation path if a dedicated Flux dashboard or per-user `SelfSubjectAccessReview`
gating is later wanted.

Plugin compatibility is confirmed: Flux plugin **0.6.0** declares `version-compat ">=0.22"` and
`distro-compat` including `in-cluster`; the deployed chart is `headlamp` **0.43.0** (appVersion 0.43.0) — no gap.

## 3. Decisions (locked)

| # | Decision | Choice | Rationale |
|---|---|---|---|
| Capability | Posture | **Monitoring + safe ops** | View all Flux state + reconcile/suspend/resume from the UI. |
| Architecture | Where | **Headlamp Flux plugin** | Reuse the existing deploy/exposure/tile/RBAC. |
| Ops scope | RBAC width | **Full** (reconcile/suspend/resume/force) | `patch` on the acted-on Flux toolkit CRs; **no** create/delete (that stays Git's job). RBAC is resource-scoped, not field-scoped — see §6. |
| Image source | Plugin image | **Mirror to Zot** (`registry.chifor.me`) | Consistent with the lab's private-registry + image-pinning posture; no external fetch at pod start. |
| Access gate | Hardening | **Accept & defer** | Keep the 24h single-factor CF Access gate; record as an accepted risk; revisit with the ADR 0007 MFA roadmap. |

## 4. Design

The change is four units, each independently reviewable:

### 4.1 Plugin delivery (GitOps-pure, into `headlamp.yaml`)

Headlamp loads plugins from its `pluginsDir` at startup; the image layer is read-only and ships no plugins.
Deliver the plugin via an `initContainer` that copies the **mirrored, pinned** plugin image into a shared
`emptyDir` mounted at `config.pluginsDir` in both containers. Add to `spec.values`:

```yaml
config:
  unsafeUseServiceAccountToken: true   # unchanged
  pluginsDir: /build/plugins           # NEW — Headlamp reads plugins from here
initContainers:
  - name: headlamp-plugin-flux
    # Mirrored to Zot from ghcr.io/headlamp-k8s/headlamp-plugin-flux:0.6.0; pin by digest after mirroring.
    image: registry.chifor.me/headlamp-k8s/headlamp-plugin-flux@sha256:<DIGEST>
    imagePullPolicy: IfNotPresent
    command: ['/bin/sh', '-c', 'mkdir -p /build/plugins && cp -r /plugins/* /build/plugins/ && chown -R 100:101 /build/plugins']
    securityContext:
      runAsUser: 0          # copy + chown to Headlamp's uid 100/gid 101 (allowed under baseline PSA)
      runAsNonRoot: false
    volumeMounts:
      - { name: headlamp-plugins, mountPath: /build/plugins }
volumeMounts:                # main container
  - { name: headlamp-plugins, mountPath: /build/plugins }
volumes:
  - { name: headlamp-plugins, emptyDir: {} }
```

- **Why emptyDir (not PVC):** declarative/stateless; the plugin re-seeds from the pinned image on every pod
  start, so it always matches Git. A PVC would add an RWO/RWX storage dependency for no benefit at 1 replica.
- **Result:** plugin lands at `/build/plugins/flux/{main.js,package.json}` (a *subdirectory* — required).
- **PSA:** namespace is `baseline`; the root copy/chown initContainer is permitted as-is. (If the ns is ever
  flipped to `restricted`, switch to a baked custom image — see §8.)
- **Chart-values check:** chart 0.43.0 `values.yaml` exposes top-level `initContainers`, `volumes`,
  `volumeMounts` and `config.pluginsDir` — verified against the chart. (Codex: please re-confirm key names.)

### 4.2 Safe-ops RBAC (new `rbac-flux.yaml`)

Keep `headlamp-readonly` honestly read-only. Add a **separate** ClusterRole bound to the *same* SA, so the
privileged delta is isolated and auditable. `get/list/watch` is already covered by the wildcard read rule —
this file adds **only** write verbs, scoped to the installed Flux toolkit groups:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: headlamp-flux-safeops
rules:
  - apiGroups: ["kustomize.toolkit.fluxcd.io"]
    resources: ["kustomizations"]
    verbs: ["patch"]                        # plugin issues merge-PATCH for every action; `update` unneeded
  - apiGroups: ["helm.toolkit.fluxcd.io"]
    resources: ["helmreleases"]            # patch covers reconcile, suspend/resume, and force
    verbs: ["patch"]
  - apiGroups: ["source.toolkit.fluxcd.io"]
    resources: ["gitrepositories", "helmrepositories", "ocirepositories", "buckets", "helmcharts"]
    verbs: ["patch"]
  # notification.toolkit.fluxcd.io (alerts/providers/receivers) intentionally NOT granted write:
  # patching them redirects/alters notification delivery and is not a meaningful safe-op (read stays via wildcard).
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: headlamp-flux-safeops
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: headlamp-flux-safeops
subjects:
  - kind: ServiceAccount
    name: headlamp
    namespace: headlamp
```

- **Mechanics:** reconcile = `patch` of the `reconcile.fluxcd.io/requestedAt` annotation; suspend/resume =
  `patch` of `spec.suspend`; force = `patch` of the reconcile/force annotations or `spec.force` (the plugin's force
  action toggles Kustomization `spec.force`) — all the **same `patch` verb**. Verify the exact action in test.
- **Deliberately excluded:** `update` (the plugin uses merge-PATCH only); `create`/`delete` (no destructive Flux-CR
  ops from the UI); `notification` write (patching alerts/providers/receivers redirects notification delivery —
  read-only via the wildcard rule); image-automation GVKs (controllers not installed); all non-Flux write verbs.
- **Field-scope caveat (§6):** RBAC is resource-scoped, not field-scoped — `patch` permits mutating *any* field on
  these CRs (e.g. a source `spec.url`/`ref`, a Kustomization/HelmRelease `spec.serviceAccountName`), not just the
  suspend/reconcile fields. Accepted as the content of the "full" blast radius; a VAP would be needed to field-scope.
- **Do NOT** set `clusterRoleBinding.create: true` in the chart — it re-adds the cluster-admin binding which
  unions additively and defeats the whole design.

### 4.3 Image mirror to Zot (one-time + on version bump)

Mirror the upstream image into the private registry, then pin the manifest by digest:

```sh
# ci creds from registry.sops.yaml; registry.chifor.me = LAN, anon pull / ci push
skopeo copy --dest-creds ci:$REGISTRY_CI_PASSWORD \
  docker://ghcr.io/headlamp-k8s/headlamp-plugin-flux:0.6.0 \
  docker://registry.chifor.me/headlamp-k8s/headlamp-plugin-flux:0.6.0
# confirm the mirrored digest matches upstream BEFORE pinning (provably the upstream 0.6.0):
skopeo inspect docker://ghcr.io/headlamp-k8s/headlamp-plugin-flux:0.6.0          | jq -r .Digest
skopeo inspect docker://registry.chifor.me/headlamp-k8s/headlamp-plugin-flux:0.6.0 | jq -r .Digest
```

- Add a `just` target (e.g. `just mirror-image <src> <dst:tag>`) wrapping this, and a short runbook note, so
  future bumps are repeatable. Renovate already tracks Flux/chart versions; the plugin mirror is a manual
  sync step on bump (documented).
- **Pull path:** the **kubelet** (node) pulls the init image, *not* the pod — so this is governed by neither the pod
  NetworkPolicy nor the `ci` push creds. Zot serves **anonymous LAN pull** (already proven for the cluster's other
  images), so **no `imagePullSecret` is needed**; verification confirms the kubelet pull (initContainer reaches Ready).

### 4.4 Homepage tile (soften wording in `homepage/configmap.yaml`)

No new tile (Flux has no standalone URL — it's a view inside Headlamp). Update the existing **"Headlamp (K8s)"**
tile description from `Cluster explorer (read-only) · CF Access` → e.g. `Cluster explorer + Flux safe-ops · CF Access`
so the dashboard stays honest. No `siteMonitor`/href change.

### 4.5 Unchanged

- **Exposure:** `k8s.chifor.me` + CF Access already gates all paths/methods of the same Service — the plugin
  and its writes are already behind the gate. No new hostname/route/Access app.
- **NetworkPolicy:** ingress-only; the **running pod's** egress already reaches the apiserver for the Flux API
  calls. (Image pulls are node-level, not pod-level — §4.3 — so the policy is irrelevant to the plugin pull.) No
  change. (A Gatus check would force an ingress exception — see §6, skipped.)
- **Gatus:** none added; Homepage's `siteMonitor` already provides the health dot.

## 5. Files changed

| File | Change |
|---|---|
| `kubernetes/apps/apps/headlamp/headlamp.yaml` | Add `config.pluginsDir`, `initContainers`, `volumes`, `volumeMounts` (§4.1). |
| `kubernetes/apps/apps/headlamp/rbac-flux.yaml` | **NEW** — `headlamp-flux-safeops` ClusterRole + binding (§4.2). |
| `kubernetes/apps/apps/headlamp/kustomization.yaml` | Add `rbac-flux.yaml` to `resources`. |
| `kubernetes/apps/apps/homepage/configmap.yaml` | Soften the Headlamp tile description (§4.4). |
| `justfile` + a short runbook note | `mirror-image` helper + plugin-mirror procedure (§4.3). |

## 6. Accepted risks & security notes

- **Single shared SA, no per-user RBAC (time-boxed acceptance).** `unsafeUseServiceAccountToken: true` means every
  CF-Access'd browser acts as the one `headlamp` SA. Granting Flux `patch` makes reconcile/suspend/force reachable by
  **anyone who clears CF Access** (currently single-factor email OTP, 24h session). Kubernetes audit attributes every
  write to the shared SA, not a human; CF Access logs are not a substitute for per-user K8s identity. **Accepted &
  deferred** per decision, recorded as an explicitly time-boxed risk. **Near-term compensating controls** (tracked):
  lower the `k8s.chifor.me` Access `session_duration` and accelerate the ADR 0007 MFA-IdP work.
- **`patch` is resource-scoped, not field-scoped — and not a complete safety boundary.** "Flux-CR-only" bounds the
  grant (no core/workload/secret writes, no create/delete), but `patch` still permits mutating *any* field on those
  CRs: a source `spec.url`/`ref` (repoint what Flux pulls) or a Kustomization/HelmRelease `spec.serviceAccountName`
  (change Flux's apply-time impersonation identity) — an escalation-adjacent surface, and Flux mutations can change
  what gets reconciled. This is the concrete content of the accepted "full" blast radius. A reconcile/suspend-only
  posture would require a ValidatingAdmissionPolicy field-scoping the patch — out of scope given the "Full" decision.
- **UI suspend is invisible in Git (drift).** Treat UI suspend/resume as break-glass; for long-lived suspend,
  commit `spec.suspend` to Git. (Operational note, not a code change.)
- **Audit attribution** is to the SA, not the human — inherent to single-SA mode; the per-user OIDC graduation
  path (Authelia → apiserver OIDC → group→role) fixes this if needed later.
- An **ADR** (next number, ~0013) should record the read-only→safe-ops posture change and the accepted risk.

## 7. Verification plan

1. `flux build` / `kustomize build kubernetes/apps/apps/headlamp` renders cleanly; `kubeconform`/dry-run OK.
2. After Flux reconcile: Headlamp pod `Ready`, initContainer completed, `/build/plugins/flux/main.js` present.
3. UI at `k8s.chifor.me` shows the **Flux** section listing Kustomizations/HelmReleases/Sources with status.
4. Trigger **reconcile** on a low-risk Kustomization → `reconcile.fluxcd.io/requestedAt` annotation updates,
   object re-syncs (cross-check with `flux get kustomization`). Confirm no 403.
5. **suspend** then **resume** a low-risk HelmRelease via the UI → `spec.suspend` toggles; resume restores.
6. Negative check: a **non-Flux** write from the UI (e.g. edit a Deployment) still **403s** — read-only intact.
7. Homepage tile renders with the updated description.

## 8. Out of scope / follow-ups (YAGNI)

- Flux Operator Web UI adoption (graduation path, documented only).
- Per-user OIDC for Headlamp (graduation path; needed only if per-user RBAC/audit becomes a requirement).
- PSA `restricted` for the namespace (would require a baked custom plugin image instead of the initContainer).
- Gatus health check for Headlamp (Homepage siteMonitor suffices; would need a NetworkPolicy exception).
- A ValidatingAdmissionPolicy to constrain Flux patches to the reconcile annotation only.
- Image-automation toolkit RBAC (those controllers are not installed).
