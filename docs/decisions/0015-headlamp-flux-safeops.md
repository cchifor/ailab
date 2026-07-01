# ADR 0015 — Headlamp Flux plugin + scoped "safe-ops" RBAC

**Status:** ACCEPTED (2026-06-30). Adds the Headlamp **Flux** plugin (v0.6.0) to the existing in-cluster
Headlamp and grants its ServiceAccount a **scoped, Flux-CR-only `patch`** capability so reconcile / suspend
/ resume / force can be driven from the UI — moving Headlamp from strictly read-only to **monitoring +
safe-ops** for Flux. Codex plan-reviewed (`plans/2026-06-30-flux-ui-headlamp-plugin-plan.md`); full 2026 eval
in `docs/superpowers/specs/2026-06-30-flux-ui-headlamp-plugin-design.md`.
**Relates to:** ADR 0006 (Talos+Flux), ADR 0012 (Authelia SSO), ADR 0007 (exposure/MFA roadmap), ADR 0014
(Zot registry — the plugin image is mirrored there).

## Context
The lab GitOps's on Flux v2.8.8 but had no web UI for Flux (sync state, events, reconcile/suspend). PR #20
deployed **Headlamp** read-only behind Cloudflare Access (`k8s.chifor.me`) and explicitly deferred "the
Headlamp Flux plugin". A 2026 agentic eval confirmed the Headlamp Flux plugin as the best fit for this lab
(reuses the deployed UI + exposure + tile) vs the first-party **Flux Operator Web UI** (graduation path),
**Weave GitOps** (EOL after Weaveworks' 2024 shutdown), and **Capacitor** (legacy, no release since Jan 2025).

Headlamp runs with `config.unsafeUseServiceAccountToken: true` — every Cloudflare-Access'd browser acts as the
single `headlamp` ServiceAccount, which was bound only to the read-only `headlamp-readonly` ClusterRole. The
Flux plugin's actions (reconcile = patch `reconcile.fluxcd.io/requestedAt`; suspend/resume = patch
`spec.suspend`; force = patch the force annotation / `spec.force`) are all Kubernetes **PATCH** calls, which
403 under a read-only role.

## Decision
1. **Deliver the plugin GitOps-natively.** An `initContainer` copies the pinned, **Zot-mirrored** plugin image
   (`registry.chifor.me/headlamp-k8s/headlamp-plugin-flux@sha256:055377…`, mirrored from
   `ghcr.io/headlamp-k8s/headlamp-plugin-flux:v0.6.0`, multi-arch index digest) into a shared `emptyDir`
   mounted at `config.pluginsDir: /build/plugins` in both containers. Stateless (re-seeds on restart), pinned,
   and hermetic at pod-pull time (the kubelet pulls from Zot on the LAN, anon). Allowed under the namespace's
   **baseline** PSA (a root copy + `chown 100:101`); a future `restricted` PSA would require a baked image.
2. **Grant a separate, narrowly-scoped write role.** A new ClusterRole `headlamp-flux-safeops` (kept apart from
   `headlamp-readonly` so that role stays honestly read-only) bound to the same SA, granting **`patch` only**
   on the Flux toolkit resources the plugin acts on: `kustomizations`, `helmreleases`, and the `source`
   group's git/helm/oci/bucket/helmchart repositories. **Not** granted: `create`/`delete`; `notification` CRs
   (patching alerts/providers/receivers redirects notification delivery); image-automation GVKs (not
   installed); any non-Flux resource. `get/list/watch` stays covered by `headlamp-readonly`'s wildcard.
3. **Keep the existing edge gate.** No exposure change: `k8s.chifor.me` + Cloudflare Access already gates the
   whole Service (UI + the new write actions). The Homepage tile description is updated from "read-only" to
   "Flux safe-ops".

## Alternatives rejected
- **Flux Operator Web UI now** — strongest dedicated Flux UI with `flux-web-user`/`flux-web-admin` +
  `SelfSubjectAccessReview` per-user gating, but requires adopting the Flux Operator and exposing a second
  SSO-fronted app. Recorded as the graduation path. ⏸
- **Weave GitOps / Capacitor** — EOL / legacy; unpatched-CVE or staleness risk once exposed. ❌
- **`update` verb / notification write / image-automation GVKs** — broader than the plugin needs; dropped. ❌
- **Per-user OIDC for Headlamp now** — correct end-state for per-user audit/least-privilege, but a larger
  change (apiserver OIDC + group→role wiring); deferred as a graduation path. ⏸
- **ValidatingAdmissionPolicy to field-scope the patch** — would constrain writes to the suspend/reconcile
  fields only; deferred per the "full ops" decision (below). ⏸

## Consequences
- **Single-SA, all-or-nothing writes (accepted, time-boxed).** Because there is no per-user identity, the
  Flux `patch` grant is available to **anyone who clears Cloudflare Access** — currently single-factor email
  OTP, 24h session. Kubernetes audit attributes every write to the shared `headlamp` SA, not a human; CF
  Access logs are not a substitute for per-user K8s identity. **Near-term compensating controls** (tracked,
  not blockers): lower the `k8s.chifor.me` Access `session_duration` and accelerate the ADR 0007 MFA-IdP work.
- **`patch` is resource-scoped, not field-scoped — not a complete safety boundary.** "Flux-CR-only" bounds the
  grant (no core/workload/secret writes, no create/delete), but `patch` still permits mutating any field on
  those CRs: a source `spec.url`/`ref` (repoint what Flux pulls) or a Kustomization/HelmRelease
  `spec.serviceAccountName` (change Flux's apply-time impersonation identity) — an escalation-adjacent surface.
  This is the accepted content of the "full ops" blast radius; field-scoping needs a VAP (deferred).
- **UI suspend drifts from Git.** A UI suspend/resume patches the live object and is invisible in Git; treat it
  as break-glass and commit long-lived suspends to Git (Flux will not auto-revert `spec.suspend`).
- **Plugin image lifecycle.** Bumps require re-mirroring to Zot (`just mirror-image …`) and re-pinning the
  digest; the Zot catch-all retention (`deleteUntagged:false`, keep all tags) protects the mirrored tag.
