# Implementation review — flux-ui-headlamp-plugin — round 1

<!-- codex-impl-review-status: pending -->

## Findings

### docker buildx imagetools missing --push flag
**Location:** justfile lines 95–102
**Severity:** blocker
<!-- codex: The `docker buildx imagetools create --tag '{{dst}}' '{{src}}'` command does not include `--push`, so it will create a local manifest reference but NOT push the image to registry.chifor.me. The kubelet will fail to pull the init image at pod start. Either add `--push` flag: `docker buildx imagetools create --tag '{{dst}}' --push '{{src}}'`, or revert to the plan's `skopeo copy` which is idiomatic for registry mirroring. The `docker buildx` approach requires push credentials and explicit promotion. -->
<!-- opus-pushback: Verified false positive. `docker buildx imagetools create --help` on this box lists ONLY `--dry-run` ("Show final image instead of pushing") — i.e. it PUSHES to the --tag registry BY DEFAULT; `--push` is NOT a valid flag for `imagetools create` (it belongs to `buildx build`) and adding it errors "unknown flag: --push". The recipe already pushes correctly. Added a clarifying comment to the recipe documenting the push-by-default behaviour. -->

### Tool substitution from skopeo to docker buildx needs validation
**Location:** justfile + headlamp.yaml comments
**Severity:** important
<!-- codex: Plan specified `skopeo copy` as the mirroring tool; implementation switches to `docker buildx imagetools create`. While both can preserve multi-arch digests, (1) `docker buildx imagetools` is not always available in minimal CI/admin environments where registry tasks run, and (2) the current invocation won't actually push. If deliberately substituting buildx as a lab-wide standard, document the rationale and confirm buildx is always available when running `just mirror-image`. Otherwise, use skopeo per the plan or fall back to docker pull/tag/push as a portable alternative. -->
<!-- opus-pushback: (2) is the same verified false positive as above — imagetools create pushes by default. (1) buildx ships bundled with the Docker CLI the lab already uses everywhere (registry push is proven via docker on the dev-workers; docker 29.5.1 here includes buildx), so it IS available where `just mirror-image` runs (operator/main checkout). The substitution is deliberate (skopeo absent) and documented in the recipe + ADR; buildx imagetools preserves the multi-arch index, which `docker pull/tag/push` would NOT. Keeping buildx. -->

### Chart 0.43.0 values structure not re-confirmed
**Location:** kubernetes/apps/apps/headlamp/headlamp.yaml spec.values
**Severity:** important
<!-- codex: The plan's design doc (§4.1) notes "Chart-values check ... (Codex: please re-confirm key names.)" The implementation adds these keys but final confirmation that Headlamp chart 0.43.0 template renders them correctly at the Deployment level is missing. Verify `kustomize build` produces the expected initContainer/volumeMounts/volumes in the rendered Deployment, then `kubectl apply --server-side --dry-run=server`. -->
<!-- opus-resolved: Statically confirmed against the chart's templates/deployment.yaml: it wires `.Values.initContainers` (L108-110), `.Values.config.pluginsDir`→`-plugins-dir={{ . }}` arg (L279-280), `.Values.volumeMounts` into the main container (L441), and `.Values.volumes` into the pod (L556). CRUCIALLY the chart's OWN `plugins-dir` volume/mount at config.pluginsDir is gated on `.Values.pluginsManager.enabled` (OFF here), NOT on `.Values.volumeMounts` — so there is NO duplicate-mountPath collision at /build/plugins. (kustomize build renders only the HelmRelease CR, hence the static template trace; the live server-side dry-run stays a rollout-time step in the plan's Verification §1.) -->

### Plugin path and Headlamp load verification required
**Location:** kubernetes/apps/apps/headlamp/headlamp.yaml initContainers command
**Severity:** important
<!-- codex: The copy is structured correctly, but runtime proof is needed: (1) /build/plugins/flux/main.js exists with ownership 100:101, (2) init container ImageID shows the pinned @sha256, (3) the UI loads the Flux section with no 404/403, (4) absent Image-Automation/Flagger CRD menus degrade cleanly. -->
<!-- opus-resolved: Agreed — inherently rollout-time checks, already enumerated verbatim in the plan's Verification steps 2–3 (stat ownership, init ImageID digest, Flux section loads, absent-CRD degradation). No code change; tracked for post-merge rollout. -->

### chmod -R a+rX included (plan noted as optional)
**Location:** kubernetes/apps/apps/headlamp/headlamp.yaml initContainers command
**Severity:** nit
<!-- codex: Implementation includes chmod -R a+rX as standard (plan called it an optional fallback). Defensible defensive programming — improves robustness. No change needed. -->
<!-- opus-resolved: Agreed, keeping it (cheap insurance against a restrictive upstream umask). -->

### Relative paths in justfile depend on CWD
**Location:** justfile mirror-image recipe
**Severity:** nit
<!-- codex: Recipe uses relative paths; consider `cd {{justfile_directory()}}` or absolute paths to prevent silent failures if run from a subdirectory. -->
<!-- opus-resolved: ACCEPTED — added `cd '{{justfile_directory()}}'` as the first line of the recipe body so the relative SOPS/age-key paths resolve from the repo root regardless of invocation CWD. -->

### SOPS_AGE_KEY_FILE path should exist and be gitignored
**Location:** justfile mirror-image recipe
**Severity:** nit
<!-- codex: Confirm the age key path exists, is gitignored, and is set before sops runs; else the recipe could fail silently. -->
<!-- opus-resolved: Confirmed: `kubernetes/infra/_out/age.agekey` is the lab's standard gitignored age-key path (also used by the runners/dev-workers/registry recipes), absent from this worktree by design (lives in the main checkout). `set -euo pipefail` makes a missing key error in `sops` and FAIL the pipe loudly, not silently. No change. -->

### docker login in just recipe lacks explicit error handling
**Location:** justfile mirror-image recipe
**Severity:** nit
<!-- codex: Consider a post-login sanity check (e.g. docker info) to catch credential issues early. Non-blocking. -->
<!-- opus-resolved: `set -euo pipefail` already aborts on a failed login (and on a failed SOPS decrypt via pipefail), so a credential failure cannot fall through to the create step. A `docker info` probe adds noise without changing failure semantics. No change. -->

### ADR 0015 correctly records the decision and accepted risks
**Severity:** approved
<!-- codex: ADR 0015 properly documents context, decision, alternatives, consequences (single-SA + single-factor Access, patch resource-scoping, UI suspend drift) and the OIDC graduation path. Model ADR. No changes needed. -->

### RBAC correctly scoped to Flux CRs with patch-only verb
**Severity:** approved
<!-- codex: headlamp-flux-safeops grants patch on kustomizations/helmreleases/source's five resources; correctly excludes create/delete, notification, image-automation; binds to the same headlamp SA; field-scope limitation documented; clean separation from headlamp-readonly. No changes needed. -->

### emptyDir wiring and plugin path structure are correct
**Severity:** approved
<!-- codex: Both containers mount the flux-plugin emptyDir at /build/plugins; cp lands the plugin at /build/plugins/flux/ (required subdir); config.pluginsDir matches; stateless re-seed on restart suits 1 replica. No changes needed. -->

### chown and chmod correct for baseline PSA
**Severity:** approved
<!-- codex: chown -R 100:101 + chmod -R a+rX on the mountPath are allowed under baseline PSA; comment notes restricted would need a baked image. No changes needed. -->

### Homepage tile description correctly updated
**Severity:** approved
<!-- codex: "read-only" → "Flux safe-ops" accurately reflects the new posture without overstating; no href/siteMonitor change. No changes needed. -->

### kustomization.yaml correctly adds rbac-flux.yaml
**Severity:** approved
<!-- codex: rbac-flux.yaml added to resources with an explanatory comment; ordering (rbac before rbac-flux) is fine since RBAC unions additively. No changes needed. -->

### Plugin version tag and digest are correct
**Severity:** approved
<!-- codex: tag v0.6.0 (v-prefix) noted; manifest digest-pinned to the multi-arch OCI index @sha256:055377…; Zot tag omitted in favour of the digest (no tag-drift). No changes needed. -->

## Round-1 outcome (opus)

- **0 source changes required by the blocker/important findings.** The one "blocker" (#1) and the push half of
  #2 are **verified false positives** (`imagetools create` pushes by default; `--push` is not a valid flag).
  #3 and #4 are **verified-correct / rollout-time** (static chart-template trace done; live checks already in the
  plan's Verification).
- **1 nit accepted as a source fix:** #6 — `cd '{{justfile_directory()}}'` added to the mirror recipe.
- Remaining `opus-pushback` markers on #1 and #2 → one round-2 codex pass to respond (per the skill's process).

## Diff stat
```
 docs/decisions/0015-headlamp-flux-safeops.md       | 66 ++++++++++++++++++++++
 .../2026-06-30-flux-ui-headlamp-plugin-design.md   | 33 ++++++-----
 justfile                                           | 14 +++++
 kubernetes/apps/apps/headlamp/headlamp.yaml        | 35 ++++++++++++
 kubernetes/apps/apps/headlamp/kustomization.yaml   |  1 +
 kubernetes/apps/apps/headlamp/rbac-flux.yaml       | 43 ++++++++++++++
 kubernetes/apps/apps/homepage/configmap.yaml       |  2 +-
 plans/2026-06-30-flux-ui-headlamp-plugin-plan.md   | 19 ++++---
 8 files changed, 190 insertions(+), 23 deletions(-)
```
