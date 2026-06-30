# Flux UI — Headlamp Flux plugin (monitoring + safe-ops)

> Full rationale + 2026 eval: `docs/superpowers/specs/2026-06-30-flux-ui-headlamp-plugin-design.md`.
> This plan is the actionable, codex-reviewed distillation. (Codex plan-review round 1 applied — see git trail.)

## Context

The lab is GitOps on **Flux v2.8.8** (source/kustomize/helm/**notification** controllers; **no** image-automation).
Observability/UI is already rich (Grafana/Loki/Gatus/Homepage/Hubble) plus **Headlamp** (k8s explorer, PR #20)
running **read-only** behind Cloudflare Access at `k8s.chifor.me` via `unsafeUseServiceAccountToken: true` (single
shared SA bound to a read-only ClusterRole). The remaining gap — a **web UI for Flux** — was explicitly deferred in
PR #20 as "the Headlamp Flux plugin". A 2026 agentic eval (cited in the design doc) confirmed the Headlamp Flux
plugin as the best fit vs Flux Operator Web UI (graduation path), Weave GitOps (EOL), and Capacitor (legacy).

User decisions (locked): **monitoring + safe-ops**; **Headlamp Flux plugin**; **full** ops scope (reconcile / suspend
/ resume / force — `patch` on Flux CRs, no create/delete); **mirror** the plugin image to Zot (`registry.chifor.me`);
**accept & defer** the single-factor CF Access gate as a recorded risk.

## Approach

Four small, independently-reviewable units:

1. **Plugin delivery** (edit `headlamp.yaml` `spec.values`): add `config.pluginsDir: /build/plugins`, an
   `initContainer` that copies the **mirrored, digest-pinned** plugin image
   (`registry.chifor.me/headlamp-k8s/headlamp-plugin-flux@sha256:<DIGEST>`, mirrored from
   `ghcr.io/headlamp-k8s/headlamp-plugin-flux:0.6.0`) into a shared `emptyDir`, mounted at `/build/plugins` in
   **both** the init and main containers. The init copies as root then `chown -R 100:101 /build/plugins` — chowning
   the **mounted dir itself**, not only the copied children (`-R` on the mountPath covers both); allowed under the
   namespace's **baseline** PSA. (A `chmod -R a+rX` fallback is cheap insurance if the upstream image ships
   restrictive modes.) emptyDir (not PVC) keeps it stateless/declarative — the plugin re-seeds from the pinned image
   on every restart, landing at `/build/plugins/flux/{main.js,package.json}` (a subdirectory — required). Leave
   `unsafeUseServiceAccountToken: true` and `clusterRoleBinding.create: false` **unchanged**.
   - **Compat:** plugin v0.6.0 declares `version-compat ">=0.22"` and `in-cluster` distro in its `artifacthub-pkg.yml`
     (verified from the metadata, not inferred from the chart) → compatible with chart/app 0.43.0; still confirm at
     runtime that the Flux section loads in the deployed 0.43.0.
   - **PSA note:** the root copy/chown init is baseline-compatible but **not** `restricted`-compatible; if the ns is
     ever tightened to `restricted`, switch to a baked custom plugin image (design doc §8) — no `runAsUser: 0`.
   - **Plugin scope:** the plugin also registers Image-Automation and Flagger/Canary nav; those CRDs are absent here,
     so verification must confirm the UI degrades cleanly (no persistent 404/403 noise).

2. **Safe-ops RBAC** (new `rbac-flux.yaml`): a **separate** ClusterRole `headlamp-flux-safeops` (so
   `headlamp-readonly` stays honestly read-only and the privileged delta is isolated), bound to the **same** SA
   `headlamp/headlamp`. Verb **`patch` only** (the plugin issues merge-PATCH for every action — reconcile patches the
   `reconcile.fluxcd.io/requestedAt` annotation; suspend/resume patch `spec.suspend`; force patches the
   reconcile/force annotations or `spec.force` depending on resource — all the same `patch` verb; `update` is dropped
   as unneeded). Scoped to the resources the plugin actually acts on:
   `kustomize.toolkit.fluxcd.io/kustomizations`, `helm.toolkit.fluxcd.io/helmreleases`,
   `source.toolkit.fluxcd.io/{gitrepositories,helmrepositories,ocirepositories,buckets,helmcharts}`.
   **Dropped from the write grant:** `notification.toolkit.fluxcd.io` (Alerts/Providers/Receivers) — patching those
   redirects/alters notification delivery and is not a meaningful safe-op; they stay **read-only** via the existing
   wildcard rule. **No** `create`/`delete`; **no** image-automation GVKs (not installed); **no** non-Flux write verbs.
   `get/list/watch` already comes from the existing wildcard read rule. Add the file to `kustomization.yaml`
   `resources`.
   - **Known limitation (informed acceptance, part of the "full" decision):** Kubernetes RBAC is resource-scoped, not
     field-scoped, so `patch` on these CRs permits mutating **any** field — including a Kustomization/HelmRelease
     `spec.serviceAccountName` (Flux's apply-time impersonation identity) and a source `spec.url`/`spec.ref`. Behind
     the single shared SA that means an escalation-adjacent surface, not merely "reconcile/suspend". This is the
     concrete content of the accepted blast radius (see Accepted risk). The only way to constrain `patch` to the
     suspend/annotation fields is a ValidatingAdmissionPolicy — out of scope per the "full" choice, recorded as a
     graduation option.

3. **Image mirror** (`justfile` target + runbook note): `skopeo copy` ghcr → `registry.chifor.me` with `ci` creds
   from `registry.sops.yaml`; then `skopeo inspect` **both** the upstream and the mirrored ref and confirm the
   digests match before pinning `headlamp.yaml` to `@sha256:<DIGEST>` (so the pinned digest is provably the upstream
   `0.6.0`). Repeatable on bump.
   - **Pull path:** the **kubelet** (node), not the pod, pulls the init image — so this is a node→Zot reachability
     concern, governed by neither the pod NetworkPolicy nor the `ci` push creds. Zot serves **anonymous LAN pull**
     (already proven for the cluster's other images via `global.imageRegistry=registry.chifor.me`), so **no
     `imagePullSecret` is needed**; verification still confirms the kubelet can pull (initContainer reaches Ready).

4. **Homepage tile** (edit `homepage/configmap.yaml`): soften the existing "Headlamp (K8s)" tile description from
   `Cluster explorer (read-only) · CF Access` → `Cluster explorer + Flux safe-ops · CF Access`. No new tile (Flux has
   no standalone URL), no href/siteMonitor change.

**Unchanged:** exposure (`k8s.chifor.me` + CF Access already gates the whole Service); NetworkPolicy (ingress-only;
the **running pod's** egress already reaches the apiserver for the Flux API calls — image pulls are node-level, §3);
no Gatus check (Homepage siteMonitor suffices; Gatus would force a NetworkPolicy ingress exception).

**Accepted risk (record as ADR ~0013, explicitly time-boxed):** single shared SA + single-factor email-OTP CF Access
(24h) means any Access'd browser can `patch` the in-scope Flux CRs — i.e. reconcile/suspend/force **and** (per §2's
limitation) mutate source URLs/refs and `serviceAccountName`. Kubernetes audit attributes every such write to the
shared `headlamp` SA, not a human; CF Access logs are not a substitute for per-user K8s identity. Bounded by the
Flux-CR-only, no-create/delete scope, but **not** a complete safety boundary. **Near-term compensating controls**
(tracked, not blockers): lower the `k8s.chifor.me` Access `session_duration` and accelerate the ADR 0007 MFA-IdP work.
**Graduation paths:** per-user OIDC for Headlamp (group→Flux-write role, per-user audit) and/or the Flux Operator Web
UI (`flux-web-user`/`flux-web-admin` + `SelfSubjectAccessReview`-gated actions).

## Critical files

- `kubernetes/apps/apps/headlamp/headlamp.yaml` — HelmRelease values: add pluginsDir + initContainer + volumes.
- `kubernetes/apps/apps/headlamp/rbac-flux.yaml` — **new** scoped Flux-write ClusterRole (`patch`, no notification) + binding.
- `kubernetes/apps/apps/headlamp/kustomization.yaml` — add `rbac-flux.yaml` to `resources`.
- `kubernetes/apps/apps/homepage/configmap.yaml` — soften the Headlamp tile description.
- `justfile` (+ a short runbook note) — `mirror-image` helper for the plugin image.

## Verification

1. **Render the real Deployment, not just the HelmRelease object.** `kustomize build kubernetes/apps/apps/headlamp`
   (validates the HR + RBAC) **plus** a Helm/Flux render of chart 0.43.0 with these values, confirming the rendered
   Deployment contains the expected initContainer, command/args, volumes, and mounts. Then
   `kubectl apply --server-side --dry-run=server` against the real namespace to catch PSA rejection, unknown fields,
   and API-version mismatches.
2. **Pod + plugin landed, permissions + pinning proven.** After Flux reconcile: pod `Ready`, initContainer completed,
   `/build/plugins/flux/main.js` present; `stat -c '%u:%g %a' /build/plugins /build/plugins/flux` shows `100:101` and
   readable modes; the pod's init container `ImageID` shows the pinned `@sha256` digest.
3. **UI loads + degrades cleanly.** The **Flux** section lists Kustomizations/HelmReleases/Sources with live
   status/conditions/events; Image-Automation and Flagger/Canary menus do **not** break the UI given those CRDs are absent.
4. **Reconcile.** Trigger reconcile on a low-risk Kustomization from the UI → `reconcile.fluxcd.io/requestedAt` updates,
   re-sync observed (cross-check `flux get kustomization`); no 403. If a force action is exercised, verify the exact
   annotations/field it sets (`requestedAt`/`forceAt` and/or `spec.force`) and `.status.lastHandled*`.
5. **Suspend/resume + drift expectation.** Suspend then resume a low-risk HelmRelease via the UI → `spec.suspend`
   toggles true→false; GitOps resumes. Document that a UI suspend drifts from Git (Flux will not auto-revert
   `spec.suspend`); operators treat UI suspend/resume as break-glass and commit long-lived suspends to Git.
6. **RBAC boundary (positive + negative).** `kubectl auth can-i patch kustomizations.kustomize.toolkit.fluxcd.io
   --as=system:serviceaccount:headlamp:headlamp` → **yes**; `... patch deployments ...` and `... delete
   kustomizations ...` → **no**. Confirm a non-Flux write from the UI (e.g. edit a Deployment) still **403s**.
7. **Document support matrix + tile.** Record `flux check` and `kubectl version` (Flux v2.8.x supported-K8s matrix) in
   the rollout evidence; Homepage tile renders with the updated description.

## Out of scope / follow-ups

- ValidatingAdmissionPolicy to constrain Flux `patch` to suspend/annotation fields (would realize a true
  "safe-ops-fields-only" boundary; deferred per the "full" decision).
- Per-user OIDC for Headlamp; Flux Operator Web UI adoption (graduation paths).
- PSA `restricted` for the namespace (needs a baked custom plugin image).
- Image-automation toolkit RBAC (controllers not installed); Gatus check (NetworkPolicy exception).

<!-- codex-review-status: complete -->
