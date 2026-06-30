# Implementation review — flux-ui-headlamp-plugin — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- **Quality:** The design is faithfully implemented with clear separation of concerns (initContainer + emptyDir for plugin delivery; separate, isolated safe-ops ClusterRole; proper RBAC binding). Inline documentation and ADR 0015 record the decisions and accepted risks honestly.
- **Critical blocker:** The `docker buildx imagetools create` recipe in justfile lacks the `--push` flag, preventing the image from actually mirroring to `registry.chifor.me`. This will cause pod pull failures at runtime.
- **Tool substitution issue:** The plan specified `skopeo copy`; the implementation substitutes `docker buildx imagetools`, which is incomplete without `--push` and may not be available in all admin environments.
- **Verification gaps:** Chart 0.43.0 values structure for top-level `initContainers`/`volumes`/`volumeMounts` should be re-confirmed against the actual chart. Plugin landing path and degradation of missing CRDs (Image-Automation/Flagger) require runtime validation.
- **Approved details:** RBAC is correctly scoped, emptyDir wiring is sound, chown/chmod defensible, ADR 0015 properly recorded.

## Findings

### docker buildx imagetools missing --push flag
**Location:** justfile lines 95–102
**Severity:** blocker
<!-- codex: The `docker buildx imagetools create --tag '{{dst}}' '{{src}}'` command does not include `--push`, so it will create a local manifest reference but NOT push the image to registry.chifor.me. The kubelet will fail to pull the init image at pod start. Either add `--push` flag: `docker buildx imagetools create --tag '{{dst}}' --push '{{src}}'`, or revert to the plan's `skopeo copy` which is idiomatic for registry mirroring. The `docker buildx` approach requires push credentials and explicit promotion. -->

### Tool substitution from skopeo to docker buildx needs validation
**Location:** justfile + headlamp.yaml comments
**Severity:** important
<!-- codex: Plan specified `skopeo copy` as the mirroring tool; implementation switches to `docker buildx imagetools create`. While both can preserve multi-arch digests, (1) `docker buildx imagetools` is not always available in minimal CI/admin environments where registry tasks run, and (2) the current invocation won't actually push. If deliberately substituting buildx as a lab-wide standard, document the rationale and confirm buildx is always available when running `just mirror-image`. Otherwise, use skopeo per the plan or fall back to docker pull/tag/push as a portable alternative. -->

### Chart 0.43.0 values structure not re-confirmed
**Location:** kubernetes/apps/apps/headlamp/headlamp.yaml spec.values
**Severity:** important
<!-- codex: The plan's design doc (§4.1) notes "Chart-values check: chart 0.43.0 values.yaml exposes top-level initContainers, volumes, volumeMounts and config.pluginsDir — verified against the chart. (Codex: please re-confirm key names.)" The implementation adds these keys but final confirmation that Headlamp chart 0.43.0 template renders them correctly at the Deployment level is missing. Verify `kustomize build kubernetes/apps/apps/headlamp` produces the expected initContainer, volume mounts, and volumes in the rendered Deployment, then `kubectl apply --server-side --dry-run=server` to catch any PSA/unknown field rejections. -->

### Plugin path and Headlamp load verification required
**Location:** kubernetes/apps/apps/headlamp/headlamp.yaml initContainers command
**Severity:** important
<!-- codex: The implementation structures the copy correctly (`mkdir -p /build/plugins && cp -r /plugins/* /build/plugins/` lands at `/build/plugins/flux/`), but runtime proof is needed: (1) After Flux reconcile, exec into the pod and confirm `/build/plugins/flux/main.js` exists with correct ownership (`stat` shows 100:101), (2) the pod's init container ImageID shows the pinned @sha256 digest, (3) Headlamp's UI loads the Flux section without 404/403 errors, and (4) Image-Automation and Flagger menus (absent CRDs) degrade cleanly without persistent console noise. -->

### chmod -R a+rX included (plan noted as optional)
**Location:** kubernetes/apps/apps/headlamp/headlamp.yaml initContainers command
**Severity:** nit
<!-- codex: Plan mentions `chmod -R a+rX` as "cheap insurance if the upstream image ships restrictive modes" (optional fallback). Implementation includes it as standard. This is defensible defensive programming (ensures readability even if upstream has umask 077), not a problem — actually improves robustness. No change needed. -->

### Relative paths in justfile depend on CWD
**Location:** justfile mirror-image recipe
**Severity:** nit
<!-- codex: Recipe uses relative paths: `kubernetes/infra/_out/age.agekey` and `ansible/secrets/registry.sops.yaml`. The comment says "Run from the main checkout" but doesn't enforce it. Consider adding explicit cwd enforcement (`cd {{justfile_directory()}}`) or absolute path construction to prevent silent failures if run from a subdirectory. Non-blocking if the lab's CI/runbook discipline is strong. -->

### SOPS_AGE_KEY_FILE path should exist and be gitignored
**Location:** justfile mirror-image recipe
**Severity:** nit
<!-- codex: The recipe references `SOPS_AGE_KEY_FILE=kubernetes/infra/_out/age.agekey` as a relative path. Confirm (1) this file exists and is properly gitignored, (2) the path matches your actual age key location in the repo, and (3) the SOPS_AGE_KEY_FILE variable is set correctly before the `sops` command runs. If the file is missing or misconfigured, the mirror recipe will fail silently at the SOPS step. -->

### docker login in just recipe lacks explicit error handling
**Location:** justfile mirror-image recipe
**Severity:** nit
<!-- codex: The recipe pipes SOPS output to `docker login` but doesn't explicitly check its exit status before proceeding to `docker buildx imagetools create`. With `set -euo pipefail`, a failed login will error the script, which is good — but consider adding a post-login sanity check (e.g., `docker info`) to catch credential validity issues early. Non-blocking if CI/testing is thorough. -->

### ADR 0015 correctly records the decision and accepted risks
**Location:** docs/decisions/0015-headlamp-flux-safeops.md
**Severity:** approved
<!-- codex: ADR 0015 properly documents the context, decision rationale, alternatives rejected, and consequences (single-SA + single-factor Access, `patch` resource-scoping limitation, UI suspend drift). The relationship to ADR 0006, 0012, 0007, 0014 is noted. Risk of per-user OIDC as a future graduation path is recorded. This is a model ADR for the decision scope. No changes needed. -->

### RBAC correctly scoped to Flux CRs with patch-only verb
**Location:** kubernetes/apps/apps/headlamp/rbac-flux.yaml
**Severity:** approved
<!-- codex: The ClusterRole `headlamp-flux-safeops` correctly grants `patch` on `kustomizations`, `helmreleases`, and `source` toolkit groups' five resources (gitrepos, helmrepos, ocirepositories, buckets, helmcharts). Correctly excluded: `create`/`delete`, `notification` CRs, image-automation GVKs. The ClusterRoleBinding correctly binds to the same `headlamp` SA in the `headlamp` namespace. The field-scope limitation (resource-scoped, not field-scoped) is documented. Separation from `headlamp-readonly` is clean. No changes needed. -->

### emptyDir wiring and plugin path structure are correct
**Location:** kubernetes/apps/apps/headlamp/headlamp.yaml
**Severity:** approved
<!-- codex: Both initContainer and main container mount the `flux-plugin` emptyDir at `/build/plugins`. The initContainer's copy command `cp -r /plugins/* /build/plugins/` correctly lands the plugin at `/build/plugins/flux/` (subdirectory — required by Headlamp's plugin loader). The `config.pluginsDir: /build/plugins` matches the mount point. Stateless ephemeral volume (re-seeds on restart) is appropriate for 1 replica and keeps the deployment GitOps-clean. No changes needed. -->

### chown and chmod correct for baseline PSA
**Location:** kubernetes/apps/apps/headlamp/headlamp.yaml initContainers command
**Severity:** approved
<!-- codex: `chown -R 100:101 /build/plugins` (Headlamp's uid:gid) and `chmod -R a+rX` are both allowed under the namespace's baseline PSA. The `-R` on the mountPath correctly chowns/chmods the mounted directory itself plus its contents. Comment notes that this is baseline-compatible but not `restricted`-compatible (future PSA tightening would need a baked image). No changes needed. -->

### Homepage tile description correctly updated
**Location:** kubernetes/apps/apps/homepage/configmap.yaml
**Severity:** approved
<!-- codex: Description changed from "Cluster explorer (read-only) · CF Access" to "Cluster explorer + Flux safe-ops · CF Access". Accurately reflects the new posture without overstating scope. No href or siteMonitor changed (Flux is a plugin view, not a standalone URL). No changes needed. -->

### kustomization.yaml correctly adds rbac-flux.yaml
**Location:** kubernetes/apps/apps/headlamp/kustomization.yaml
**Severity:** approved
<!-- codex: The `rbac-flux.yaml` file is correctly added to the `resources` list with an inline comment explaining its purpose. The file ordering (rbac.yaml before rbac-flux.yaml) ensures the base read-only RBAC is rendered first, then the safe-ops grant unions additively. No changes needed. -->

### Plugin version tag and digest are correct
**Location:** kubernetes/apps/apps/headlamp/headlamp.yaml + justfile
**Severity:** approved
<!-- codex: The upstream tag `v0.6.0` (with v-prefix) is noted in the justfile comment and ADR. The manifest is digest-pinned `@sha256:055377b9011dcc73235e8969c488ecd92af5cb70aa5d5df0f66c1cea667fdccb` (multi-arch OCI index), which is correct for the pinning-by-digest strategy. The Zot mirror tag is omitted in favor of the digest, avoiding tag-drift. No changes needed. -->

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
