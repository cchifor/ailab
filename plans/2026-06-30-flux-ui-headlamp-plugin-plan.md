# Flux UI — Headlamp Flux plugin (monitoring + safe-ops)

> Full rationale + 2026 eval: `docs/superpowers/specs/2026-06-30-flux-ui-headlamp-plugin-design.md`.
> This plan is the actionable, codex-reviewable distillation.

## Context

The lab is GitOps on **Flux v2.8.8** (source/kustomize/helm/**notification** controllers; **no** image-automation).
Observability/UI is already rich (Grafana/Loki/Gatus/Homepage/Hubble) plus **Headlamp** (k8s explorer, PR #20)
running **read-only** behind Cloudflare Access at `k8s.chifor.me` via `unsafeUseServiceAccountToken: true` (single
shared SA bound to a read-only ClusterRole). The remaining gap — a **web UI for Flux** — was explicitly deferred in
PR #20 as "the Headlamp Flux plugin". A 2026 agentic eval (cited in the design doc) confirmed the Headlamp Flux
plugin as the best fit vs Flux Operator Web UI (graduation path), Weave GitOps (EOL), and Capacitor (legacy).

User decisions (locked): **monitoring + safe-ops**; **Headlamp Flux plugin**; **full** ops scope (incl. HelmRelease
force-reconcile, no create/delete); **mirror** the plugin image to Zot (`registry.chifor.me`); **accept & defer** the
single-factor CF Access gate as a recorded risk.

## Approach

Four small, independently-reviewable units:

1. **Plugin delivery** (edit `headlamp.yaml` `spec.values`): add `config.pluginsDir: /build/plugins`, an
   `initContainer` that copies the **mirrored, digest-pinned** plugin image
   (`registry.chifor.me/headlamp-k8s/headlamp-plugin-flux@sha256:<DIGEST>`, mirrored from
   `ghcr.io/headlamp-k8s/headlamp-plugin-flux:0.6.0`) into a shared `emptyDir`, mounted at `/build/plugins` in
   **both** the init and main containers. The init copies as root then `chown -R 100:101` (Headlamp's uid/gid);
   allowed under the namespace's **baseline** PSA. emptyDir (not PVC) keeps it stateless/declarative — the plugin
   re-seeds from the pinned image on every restart. Plugin v0.6.0 (`version-compat ">=0.22"`, `in-cluster` distro)
   is compatible with chart 0.43.0 (appVersion 0.43.0). Leave `unsafeUseServiceAccountToken: true` and
   `clusterRoleBinding.create: false` **unchanged**.

2. **Safe-ops RBAC** (new `rbac-flux.yaml`): a **separate** ClusterRole `headlamp-flux-safeops` (so
   `headlamp-readonly` stays honestly read-only and the privileged delta is isolated), bound to the **same** SA
   `headlamp/headlamp`. Verbs **`patch`,`update`** only (reconcile = patch the `reconcile.fluxcd.io/requestedAt`
   annotation; suspend/resume = patch `spec.suspend`; HelmRelease force = patch — all the same verb). Scoped to the
   installed toolkit groups only: `kustomize.toolkit.fluxcd.io/kustomizations`, `helm.toolkit.fluxcd.io/helmreleases`,
   `source.toolkit.fluxcd.io/{gitrepositories,helmrepositories,ocirepositories,buckets,helmcharts}`,
   `notification.toolkit.fluxcd.io/{alerts,providers,receivers}`. **No** `create`/`delete`; **no** image-automation
   GVKs (not installed); **no** non-Flux write verbs. `get/list/watch` already comes from the existing wildcard read
   rule. Add the file to `kustomization.yaml` `resources`.

3. **Image mirror** (`justfile` target + runbook note): `skopeo copy` ghcr → `registry.chifor.me` with `ci` creds
   from `registry.sops.yaml`, then `skopeo inspect` to read the digest to pin in `headlamp.yaml`. Repeatable on bump.

4. **Homepage tile** (edit `homepage/configmap.yaml`): soften the existing "Headlamp (K8s)" tile description from
   `Cluster explorer (read-only) · CF Access` → `Cluster explorer + Flux safe-ops · CF Access`. No new tile (Flux has
   no standalone URL), no href/siteMonitor change.

**Unchanged:** exposure (`k8s.chifor.me` + CF Access already gates the whole Service), NetworkPolicy (ingress-only;
egress already permits the Zot pull + apiserver), no Gatus check (Homepage siteMonitor suffices; Gatus would force a
NetworkPolicy ingress exception).

**Accepted risk (record as ADR ~0013):** single shared SA + single-factor email-OTP CF Access (24h) means any
Access'd browser can reconcile/suspend/force Flux. Bounded by the Flux-CR-only scope (no core/workload/secret writes,
no create/delete). Per-user OIDC + the Flux Operator UI are the documented graduation paths.

## Critical files

- `kubernetes/apps/apps/headlamp/headlamp.yaml` — HelmRelease values: add pluginsDir + initContainer + volumes.
- `kubernetes/apps/apps/headlamp/rbac-flux.yaml` — **new** scoped Flux-write ClusterRole + binding.
- `kubernetes/apps/apps/headlamp/kustomization.yaml` — add `rbac-flux.yaml` to `resources`.
- `kubernetes/apps/apps/homepage/configmap.yaml` — soften the Headlamp tile description.
- `justfile` (+ a short runbook note) — `mirror-image` helper for the plugin image.

## Verification

1. `kustomize build kubernetes/apps/apps/headlamp` renders; dry-run/`kubeconform` clean.
2. Flux reconcile → Headlamp pod `Ready`, initContainer completed, `/build/plugins/flux/main.js` present in the pod.
3. UI shows a **Flux** section listing Kustomizations/HelmReleases/Sources with live status/conditions/events.
4. Reconcile a low-risk Kustomization from the UI → `requestedAt` annotation updates, re-sync observed
   (cross-check `flux get kustomization`); no 403.
5. Suspend then resume a low-risk HelmRelease via the UI → `spec.suspend` toggles true→false; GitOps resumes.
6. Negative: editing a **non-Flux** object (e.g. a Deployment) from the UI still **403s** — read-only preserved.
7. Homepage tile renders with the updated description.

## Open questions for the reviewer

- Chart 0.43.0 value keys: confirm top-level `initContainers`/`volumes`/`volumeMounts` + `config.pluginsDir` are the
  correct schema (vs a newer first-class `plugins:`/`pluginsManager:` block) before writing the manifest.
- emptyDir uid/readability: is `chown -R 100:101` sufficient, or is an explicit `fsGroup`/`0755` also needed so the
  main container (uid 100) can read the copied plugin?
- Is scoping `patch` to *all* listed source/notification GVKs over-broad for "safe-ops", or appropriate given the
  "full" decision?

<!-- codex-review-status: pending -->
