# Validation — last week's merges to `main` (codex cross-reviewed)

<!-- codex-impl-review-status: pending -->

**Date:** 2026-06-30 · **Scope:** the 42 PRs merged to `main` Jun 16–18 (#1–#43, no #42).
**Method:** an agentic workflow ran one Opus review agent per PR cluster (structured findings + per-PR verdict), then the **codex** subagent independently cross-reviewed each Tier‑A cluster and every high‑severity finding (CONFIRM / REFUTE / PARTIAL + missed‑issue hunt). 23 agents, ~1.36M tokens.

> The README refresh (`c287a56`) is on `docs/readme-refresh`, unmerged → out of scope. The Jun 13–15 foundational work was pushed direct‑to‑`main` (not PRs) → out of scope.

## Headline

| | |
|---|---|
| Per‑PR verdicts (42) | **best ×16 · acceptable ×19 · improvable ×6 · problematic ×1** |
| Findings | **1 blocker · 23 important · 40 nits** |
| Codex agreement | **0 refutes.** Codex CONFIRMED every high‑severity Opus finding and added several missed issues (below). A few PARTIALs refined severity, none overturned a finding. |

**Verdict: the week's work is sound.** ~83% of PRs are `best`/`acceptable`; the issues cluster into a small, fixable set. The single `problematic` PR (#20 Headlamp) is the only true blocker. Notably, the review independently re‑derived the *correct* fix‑chain inside the window — #29's two bugs were genuinely shipped broken and genuinely fixed by #30/#31, and #32's bug by #33 — so those are validations, not new action items.

Disposition legend: ✅ **applied** on `chore/merge-validation-jun` · 📋 **recommended** (needs operator validation / data migration) · ⚖️ **escalated** (product decision) · ✓ **already fixed in‑window**.

---

## Tier A — deep + full codex cross-review

### Backup / DR (#28–#35) — Velero + talos-backup + rclone→Drive
Verdicts: #28 best · #29 improvable · #30 best · #31 best · #32 improvable · #33 best · #34 acceptable · #35 best. **Architecture is strong** (age‑encrypted etcd snapshots, scoped `talos.dev` SA, PSA‑hardened ns, digest‑pinned image, honest ADR with an acceptance gate). Codex CONFIRMED all findings.

- ✓ **#29** talos leg shipped non‑functional — inverted `USE_PATH_STYLE` + wrong age env (`AGE_RECIPIENT_PUBLIC_KEY` vs `AGE_X25519_PUBLIC_KEY`); verified against beta.3 source. **Fixed by #31.**
- ✓ **#29** Velero missing `features: EnableCSI` → backups carry no PV data. **Fixed by #30.**
- ✓ **#32** prune `RCLONE_CONFIG_GT_NO_CHECK_CERTIFICATE` ignored by the s3 backend → prune errors every run, retention never enforced. **Fixed by #33** (`RCLONE_CA_CERT`).
- ✅ **#34** off‑site `rclone sync` has **no `--max-delete` guard** — a local versitygw wipe/empty‑listing would mirror a mass‑delete to the only off‑site copy. → add `--max-delete`.
- 📋 **#29** Velero **API‑object backups store cluster Secrets in plaintext** in the local versitygw bucket (only CSI PV data + the off‑site copy are encrypted; the talos etcd snapshot IS age‑encrypted — an asymmetry). → enable QNAP/versitygw at‑rest encryption (LUKS) for the velero bucket; record in ADR 0010.
- 📋 **#29** Velero BSL uses `insecureSkipTLSVerify: true` although the versitygw CA is already a ConfigMap → add `caCert` + drop the skip.
- 📋 **#32** prune is purely age‑based with **no count floor** — if talos‑backup stalls >30 d, prune can delete the last good snapshot. → add a keep‑N guard.
- 📋 **#28** the whole 3‑2‑1 hinges on **offline escrow of one SOPS age key** (private half lives only in‑cluster + tfstate). #35 rotated it but the SPOF stands. → verify+document offline escrow of (1) SOPS age key, (2) talos‑backup age key, (3) rclone crypt password/salt; run a real restore drill (codex also flagged old‑key destruction is undocumented).

### Vaultwarden (#38) — `acceptable`
- ✅ **`IP_HEADER=X-Forwarded-For` is client‑spoofable behind Cloudflare**, defeating the very `LOGIN_RATELIMIT` brute‑force control the PR built (CF appends the real IP last; Vaultwarden reads the first, attacker‑controlled entry). → `IP_HEADER=CF-Connecting-IP` (single, edge‑set, un‑spoofable).
- 📋 **Container runs as root** (uid 0 + `NET_BIND_SERVICE`) on the most security‑sensitive workload; the cited gitea precedent runs **rootless**. → run rootless (`ROCKET_PORT=8080`, drop `NET_BIND_SERVICE`, `runAsNonRoot`/`runAsUser: 1000` + `fsGroup`, Service `targetPort` 8080, raise ns PSA to `restricted`). Validate the vault starts before merging.

### WAN-expose admin UIs (#24) — `improvable` — ⚖️ ESCALATED
Codex CONFIRMED all and added a missed issue. This is a **product decision**, surfaced to the operator:
- ⚖️ The PR publishes **Proxmox / QNAP / Prometheus / Alertmanager** to the WAN, but `docs/runbooks/internet-exposure.md`, `cloudflare-access-apps.md`, and ADR 0007 still call these **Tailscale‑private / "no public surface"** — the live config and the threat model now contradict each other, with no ADR recording the reversal.
- ⚖️ Prometheus/Alertmanager have **no native auth**, so a single‑factor email‑OTP CF Access policy (no IdP/MFA) is the *sole* gate; an Alertmanager compromise can silence every alert (blinding the lab's own detection). 24 h session vs 8 h for dev shells.
- ⚖️ (codex‑missed) Origins use **`noTLSVerify: true`** for Proxmox/QNAP — cloudflared doesn't verify the LAN origin before forwarding admin creds (MITM surface).

### cert-manager + Trivy Operator (#36, #37) — both `acceptable`
Codex: all CONFIRMED, none missed.
- ✅ **#37** moving the Trivy DB cache to `qnap-iscsi` pulls a **rebuildable cache into the Velero off‑site chain** (qnap‑iscsi has the Velero snapshot class; schedules back up `*`). → add `trivy-system` to `excludedNamespaces` in both schedules.
- 📋 nits: `cert-manager` ns PSA is `baseline` (could be `restricted`); `cert-manager-config` `dependsOn` the whole `infrastructure` tree; DNS‑01 issuers lack `--dns01-recursive-nameservers`; qnap‑iscsi `Retain` orphans the cache LUN on PVC delete.

### Renovate (#39, #43, #40, #41)
Verdicts: #39 improvable · #43 best · #40 acceptable · #41 acceptable. Codex CONFIRMED all (incl. both important issues), none missed beyond Opus's list.
- ✅ **#39 manager overlap** — the customManager (`/kubernetes/.+`) fully overlaps the kubernetes manager (`/kubernetes/apps/.+`) on the *only* tag@digest image (talos‑backup, which is under `apps`), risking competing branches on the DR‑critical component. Its rationale ("flux/helm can't see it") is wrong. → remove the customManager.
- ✅ **#39 Flux scope** — the kubernetes manager matches `flux-system/gotk-components.yaml` (a `flux bootstrap` artifact) → unsafe per‑controller bumps / bootstrap drift. → `ignorePaths` it.
- ✅ nits: drop `:dependencyDashboard` (redundant with `config:recommended`) + `helpers:pinGitHubActionDigests` (no‑op, no `.github/workflows`); add `emptyDir.sizeLimit` + an ephemeral‑storage limit on the CronJob.
- 📋 **#41** `local-path-provisioner` v0.0.30→v0.0.36 auto‑applies via Flux to the Prometheus‑backing class; codex's changelog review found no breaking changes but a smoke‑test (bind a PVC, confirm 0777, restart Prometheus) is prudent.
- nits (no action): `renovate/renovate:43` floats; #40 zot edits git only (out‑of‑cluster, manual redeploy); labels added late in #43 (already fixed).

---

## Tier B — focused review (codex on high‑severity findings)

### k8s dashboards (#20–#23) — **#20 `problematic` (the one blocker)**
- ✅ **BLOCKER #20** — the Headlamp chart defaults `clusterRoleBinding.create: true` → `cluster-admin`, and the HelmRelease never overrides it, so the SA is **cluster‑admin** (RBAC union) despite the repo's own read‑only binding and the "inspector, not mutator" narrative. → `clusterRoleBinding: { create: false }` so only the repo's `headlamp-readonly` binding applies. Codex CONFIRMED.
- ✓ **#21** `unsafeUseServiceAccountToken` is correct *behind an auth proxy* — it only becomes the prereq once #20 is fixed (then the SA is genuinely read‑only). No separate change.
- 📋 (codex‑missed) Headlamp has **no NetworkPolicy** — any in‑cluster pod can hit `headlamp.headlamp.svc:80` directly, bypassing CF Access (cluster‑admin until #20, all‑secrets read after). → add a NetworkPolicy admitting only the tunnel.
- nits: stale Grafana comment vs intentional `Editor` SSO role (#22); Loki dashboard panel substring mismatch (#23).

### Gitea + ntfy (#25–#27) — #25 improvable, #26/#27 acceptable
- ✅ **#26** ntfy postStart seed hook **always exits 0** — a failed seed leaves a Ready pod with deny‑all auth, so every Alertmanager webhook 401s silently. → `exit 1` after the loop exhausts.
- ✅ **#27** internet‑exposed Gitea has **no `REQUIRE_SIGNIN_VIEW`** → anonymous UI/API/repo enumeration. → `REQUIRE_SIGNIN_VIEW: true` + `DEFAULT_PRIVATE: true` (OIDC UI + PAT/SSH clone still work).
- 📋 **#25** ntfy SQLite on **`nfs-csi`** contradicts the repo's own SQLite‑on‑NFS rule (corruption risk) and has no Velero snapshot class → no backup. → move to `qnap-iscsi` (requires PVC recreation; admin auto‑reseeds).
- 📋 **#25** Alertmanager→ntfy posts **raw JSON** (no title/priority; grouped payloads can exceed ntfy's ~4 KB limit → 400 dropped). → add a formatting bridge. (codex) Alertmanager uses the admin cred vs a scoped publish token.
- 📋 (codex) Authelia `one_factor` auto‑creates Gitea accounts; SSH clone URLs advertised but only HTTP is tunneled (use `DISABLE_SSH`).

### Registry / Zot (#14–#19) — #14/#18 acceptable, #15/#17/#19 best
- ✅ **#14** the **live LXC zot binary** (`registry_zot_version`) is a raw string not renovate‑tracked and has **drifted** (v2.1.2 vs the compose image's v2.1.17 from #40) → no CVE patching on the live path. → bump to v2.1.17 + add a renovate annotation.
- ✅ (codex‑missed) OIDC client secret is rendered by a **non‑`no_log`** ansible task → leaks under `--diff`. → `no_log: true`.
- 📋 (codex‑missed) CI/SSO admins get `delete` on `**` (scope down); LXC template over HTTP + zot binary unverified (HTTPS + checksum); config.json templated without `validate`; Terraform data volume lacks `prevent_destroy`.
- 📋 **#16** registry image store has no backup/DR (outside Velero/talos‑backup) → document the rebuild‑from‑CI path / add a vzdump.

### node3 storage (#10–#13) — #10/#13 best, #11 improvable, #12 acceptable
Net: **safe as‑merged** (jumbo was reverted to MTU 1500 by #13). Codex PARTIAL on the secondary diagnostics.
- 📋 **#11** "staged" jumbo MTU wasn't actually gated (template renders `mtu` unconditionally → live on next reconcile). Process fix: don't commit risky values behind a "do not apply" comment; add a real DF‑bit pre‑flight gate. Plus stale `irqbalance`/comment cleanups and optional tofu `nconnect=4`.

### AI model swap (#3) — `acceptable`
- 📋 **#3** rollout renames systemd instances to new units on the **same ports without stopping the old ones** → port‑bind collision / stale Endpoints until manual intervention; runbook has no teardown step. → keep the same instance names *or* add an explicit `systemctl disable --now` teardown step; fix the inaccurate `provision.sh` comment.
- 📋 (codex‑missed) LiteLLM ConfigMap change won't restart the pod (no checksum annotation) → new model list staged but not served. Old model names removed without aliases (pinned clients break).

### Platform SSH deploy key (#8, #16) — acceptable/best — clean
No high‑severity findings; codex pass skipped. Deploy key is SOPS‑encrypted, read‑only, in its own Kustomization. Nits only.

## Tier C — already codex-reviewed (light spot-check)
**CI runners (#1,#2,#4)** and **dev-workers (#5,#6,#7,#9)** confirmed sound; prior `*-impl-review.md` artifacts cover them. Nits only (GitHub App auth, ephemeral wrapper, SOPS ordering all good).

---

## Applied fixes (this branch)

Committed as separate conventional commits on `chore/merge-validation-jun` — see the section below once committed. Each maps to a ✅ finding above.

## Recommended (operator decision / validation required)

The 📋 items above — chiefly: Vaultwarden rootless; ntfy → qnap-iscsi (PVC recreation); Velero at‑rest encryption + caCert; **backup key‑escrow verification + a real restore drill**; Headlamp NetworkPolicy; registry RBAC/supply‑chain hardening + backup; AI cutover teardown + LiteLLM reload. None are auto‑applied because they touch live data/services or need a smoke‑test.

## Escalation

The **WAN‑exposure model (#24)** is a product decision (keep admin UIs public+Access‑gated and update the docs/ADR, or revert to Tailscale‑only), plus the single‑factor and `noTLSVerify` hardening. Surfaced to the operator separately.
