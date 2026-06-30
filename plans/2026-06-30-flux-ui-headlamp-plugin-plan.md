# Flux UI — Headlamp Flux plugin (monitoring + safe-ops)

## Codex Review

- Chart wiring is correct for Headlamp chart/app 0.43.0: `initContainers`, `volumes`, `volumeMounts`, and `config.pluginsDir` are top-level values, and the plugin README documents the same init-container pattern.
- The `emptyDir` + root copy works under PSA baseline; `chown` is enough if it covers the mounted plugin directory, but it is not restricted-PSA compatible and should be server-dry-run tested in the target namespace.
- RBAC is the riskiest part: Kubernetes cannot field-scope `patch`/`update`, so this grants full mutation of the selected Flux CR specs, not just reconcile/suspend/resume.
- HelmRelease force-reconcile is annotation-driven in Flux v2.8 (`requestedAt` + `forceAt`), while the plugin source's "force" action appears to toggle Kustomization `.spec.force`; test that exact UI action before granting/claiming HelmRelease force.
- Verification needs one more layer: render the Helm chart/Deployment, server-side dry-run against the cluster, `kubectl auth can-i` checks, image-pull credentials for Zot, and negative tests for Flux CR spec edits.

> Full rationale + 2026 eval: `docs/superpowers/specs/2026-06-30-flux-ui-headlamp-plugin-design.md`.
> This plan is the actionable, codex-reviewable distillation.

## Context

The lab is GitOps on **Flux v2.8.8** (source/kustomize/helm/**notification** controllers; **no** image-automation).
Observability/UI is already rich (Grafana/Loki/Gatus/Homepage/Hubble) plus **Headlamp** (k8s explorer, PR #20)
running **read-only** behind Cloudflare Access at `k8s.chifor.me` via `unsafeUseServiceAccountToken: true` (single
shared SA bound to a read-only ClusterRole). The remaining gap — a **web UI for Flux** — was explicitly deferred in
PR #20 as "the Headlamp Flux plugin". A 2026 agentic eval (cited in the design doc) confirmed the Headlamp Flux
plugin as the best fit vs Flux Operator Web UI (graduation path), Weave GitOps (EOL), and Capacitor (legacy).
<!-- codex: The plugin registers Image Automation and Flagger/Canary navigation in addition to core Flux views; with those CRDs absent, verify the UI degrades cleanly without persistent 404/403 noise. -->

User decisions (locked): **monitoring + safe-ops**; **Headlamp Flux plugin**; **full** ops scope (incl. HelmRelease
force-reconcile, no create/delete); **mirror** the plugin image to Zot (`registry.chifor.me`); **accept & defer** the
single-factor CF Access gate as a recorded risk.
<!-- codex: "HelmRelease force-reconcile" may be an incorrect capability assumption for plugin v0.6.0; Flux v2.8 force is `reconcile.fluxcd.io/requestedAt` + `reconcile.fluxcd.io/forceAt`, not a spec field. -->

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
<!-- codex: Chart 0.43.0 does support these top-level values; `pluginsManager` also exists but is a sidecar/npm installer path, not a better fit for digest-pinned mirrored plugin image delivery. -->
<!-- codex: `chown -R 100:101` is sufficient only if it covers the mounted plugin directory itself, not just copied children; verify with `stat` in the running pod. -->
<!-- codex: Root init + `CHOWN` is baseline-compatible, but not restricted-compatible; a future namespace PSA change would reject `runAsUser: 0`. -->
<!-- codex: The v0.6.0 compatibility claim should be verified from the image/Artifact Hub metadata and by loading Headlamp 0.43.0, not inferred only from the chart version. -->

2. **Safe-ops RBAC** (new `rbac-flux.yaml`): a **separate** ClusterRole `headlamp-flux-safeops` (so
   `headlamp-readonly` stays honestly read-only and the privileged delta is isolated), bound to the **same** SA
   `headlamp/headlamp`. Verbs **`patch`,`update`** only (reconcile = patch the `reconcile.fluxcd.io/requestedAt`
   annotation; suspend/resume = patch `spec.suspend`; HelmRelease force = patch — all the same verb). Scoped to the
   installed toolkit groups only: `kustomize.toolkit.fluxcd.io/kustomizations`, `helm.toolkit.fluxcd.io/helmreleases`,
   `source.toolkit.fluxcd.io/{gitrepositories,helmrepositories,ocirepositories,buckets,helmcharts}`,
   `notification.toolkit.fluxcd.io/{alerts,providers,receivers}`. **No** `create`/`delete`; **no** image-automation
   GVKs (not installed); **no** non-Flux write verbs. `get/list/watch` already comes from the existing wildcard read
   rule. Add the file to `kustomization.yaml` `resources`.
<!-- codex: RBAC is resource-scoped, not field-scoped; `patch`/`update` lets the UI mutate any spec/metadata field on those Flux CRs, including source URLs, refs, intervals, serviceAccountName, provider endpoints, and receiver config. -->
<!-- codex: `update` is probably broader than the plugin actions need; keep it only if a confirmed UI path uses full-object update, otherwise prefer `patch` alone. -->
<!-- codex: HelmRelease force in Flux v2.8 requires both `reconcile.fluxcd.io/requestedAt` and `reconcile.fluxcd.io/forceAt` annotations with the same token; verify the plugin emits that, because the observed plugin action code toggles `.spec.force` for Kustomizations instead. -->
<!-- codex: Notification write scope is likely over-broad for safe-ops unless the plugin has concrete suspend/resume/reconcile actions there; patching Providers/Receivers can redirect or alter notification behavior. -->

3. **Image mirror** (`justfile` target + runbook note): `skopeo copy` ghcr → `registry.chifor.me` with `ci` creds
   from `registry.sops.yaml`, then `skopeo inspect` to read the digest to pin in `headlamp.yaml`. Repeatable on bump.
<!-- codex: Mirroring with CI credentials is separate from kubelet pulling the init image; if Zot requires auth, add or verify an `imagePullSecret` in the Headlamp namespace. -->
<!-- codex: Pin the digest after comparing upstream and mirrored manifests, not just inspecting the mirror, so the pinned digest is known to correspond to upstream `0.6.0`. -->

4. **Homepage tile** (edit `homepage/configmap.yaml`): soften the existing "Headlamp (K8s)" tile description from
   `Cluster explorer (read-only) · CF Access` → `Cluster explorer + Flux safe-ops · CF Access`. No new tile (Flux has
   no standalone URL), no href/siteMonitor change.

**Unchanged:** exposure (`k8s.chifor.me` + CF Access already gates the whole Service), NetworkPolicy (ingress-only;
egress already permits the Zot pull + apiserver), no Gatus check (Homepage siteMonitor suffices; Gatus would force a
NetworkPolicy ingress exception).
<!-- codex: NetworkPolicy does not govern kubelet image pulls, so "egress permits the Zot pull" is the wrong control for the plugin init image; image pull reachability/auth must be checked at node/runtime level. -->

**Accepted risk (record as ADR ~0013):** single shared SA + single-factor email-OTP CF Access (24h) means any
Access'd browser can reconcile/suspend/force Flux. Bounded by the Flux-CR-only scope (no core/workload/secret writes,
no create/delete). Per-user OIDC + the Flux Operator UI are the documented graduation paths.
<!-- codex: This is acceptable only as an explicitly time-boxed risk: Kubernetes audit will attribute all writes to the shared SA, and CF Access logs are not a substitute for per-user Kubernetes identity; add MFA/session-tightening as near-term compensating controls. -->
<!-- codex: "Flux-CR-only" is not a complete safety boundary because Flux CR mutations can indirectly change what Flux reconciles or how notifications leave the cluster. -->

## Critical files

- `kubernetes/apps/apps/headlamp/headlamp.yaml` — HelmRelease values: add pluginsDir + initContainer + volumes.
- `kubernetes/apps/apps/headlamp/rbac-flux.yaml` — **new** scoped Flux-write ClusterRole + binding.
- `kubernetes/apps/apps/headlamp/kustomization.yaml` — add `rbac-flux.yaml` to `resources`.
- `kubernetes/apps/apps/homepage/configmap.yaml` — soften the Headlamp tile description.
- `justfile` (+ a short runbook note) — `mirror-image` helper for the plugin image.

## Verification

1. `kustomize build kubernetes/apps/apps/headlamp` renders; dry-run/`kubeconform` clean.
<!-- codex: `kustomize build` only validates the HelmRelease object, not the rendered Headlamp Deployment; add Helm/Flux render of chart 0.43.0 and confirm the Deployment contains the expected initContainer, args, volumes, and mounts. -->
<!-- codex: Add `kubectl apply --server-side --dry-run=server` against the real cluster/namespace to catch PSA, unknown fields such as `hostUsers`, and API-version compatibility. -->
2. Flux reconcile → Headlamp pod `Ready`, initContainer completed, `/build/plugins/flux/main.js` present in the pod.
<!-- codex: Also verify `stat -c '%u:%g %a' /build/plugins /build/plugins/flux` and the init image `ImageID` digest so permissions and pinning are proven. -->
3. UI shows a **Flux** section listing Kustomizations/HelmReleases/Sources with live status/conditions/events.
<!-- codex: Include checks for absent optional CRDs: Image Automation and Flagger/Canary menu entries should not break the UI when those controllers are not installed. -->
4. Reconcile a low-risk Kustomization from the UI → `requestedAt` annotation updates, re-sync observed
   (cross-check `flux get kustomization`); no 403.
<!-- codex: Add a separate HelmRelease force test if "full ops" still includes it: verify both `requestedAt` and `forceAt` annotations and `.status.lastHandledForceAt`. -->
5. Suspend then resume a low-risk HelmRelease via the UI → `spec.suspend` toggles true→false; GitOps resumes.
<!-- codex: Because these manual patches drift from Git, verify Flux/Git later reverts or tolerates them as intended and document the operator expectation. -->
6. Negative: editing a **non-Flux** object (e.g. a Deployment) from the UI still **403s** — read-only preserved.
<!-- codex: Add `kubectl auth can-i --as=system:serviceaccount:headlamp:headlamp` checks for allowed Flux verbs and denied core/workload verbs, plus a negative UI/API test that full Flux CR spec editing is not unintentionally exposed. -->
7. Homepage tile renders with the updated description.
<!-- codex: Add `flux check` and `kubectl version` to document Kubernetes support for Flux v2.8.x; the v2.8 docs list supported Kubernetes versions and this should be explicit in the rollout evidence. -->

## Open questions for the reviewer

- Chart 0.43.0 value keys: confirm top-level `initContainers`/`volumes`/`volumeMounts` + `config.pluginsDir` are the
  correct schema (vs a newer first-class `plugins:`/`pluginsManager:` block) before writing the manifest.
<!-- codex: Confirmed correct for chart 0.43.0; keep using the documented init-container pattern for a mirrored digest-pinned plugin image. -->
- emptyDir uid/readability: is `chown -R 100:101` sufficient, or is an explicit `fsGroup`/`0755` also needed so the
  main container (uid 100) can read the copied plugin?
<!-- codex: `fsGroup` is not required if the init container chowns the mounted directory and files; explicit `chmod a+rX` is a useful fallback if the plugin image has unexpectedly restrictive modes. -->
- Is scoping `patch` to *all* listed source/notification GVKs over-broad for "safe-ops", or appropriate given the
  "full" decision?
<!-- codex: It is over-broad for notification resources and still broad for all Flux resources because patch/update can mutate arbitrary fields; scope to resources with confirmed UI actions and consider admission policy if "safe-ops fields only" is required. -->

<!-- codex-review-status: complete -->
