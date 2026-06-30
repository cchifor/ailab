# Validation — the newer merges to `main` (#44–#70), codex cross-reviewed

<!-- codex-impl-review-status: finalized -->

**Date:** 2026-06-30 · **Scope:** the 22 commits #44–#70 that were on `origin/main` but **missing from the local clone** during the first validation (which therefore covered #1–#43 only). Same agentic + codex method. 10 agents, ~0.76M tokens.

## Headline

| | |
|---|---|
| Per-PR verdicts (22) | mostly `best`/`acceptable`; `improvable ×4` (#44, #57, #60); **`problematic ×1` (#58)** |
| Findings | **0 blocker · 9 important · 15 nits** |
| Codex agreement | **0 refutes.** Confirmed all; independently surfaced the **`api.chifor.me` no-Access** gap, the **`#48` un-mirrored busybox**, and seconded the `#58`/`#60`/`#62` infra-skew findings. |

**Verdict: solid, but with three real infra-hygiene gaps** — two version-skews in the GitOps/CSI engine (#58, #60) and the unbudgeted/un-gated paid-cloud path (#44). The good news: **the LiteLLM ConfigMap-reload bug the part-1 review predicted was real in #44 and was correctly fixed by #66** (checksum annotation) — the validation re-derived that fix-chain independently.

Disposition: ✅ applied · 📋 recommended · 🔺 **priority follow-up (needs WSL2 `flux`/`tofu` tooling or touches live infra)** · ⚖️ decision · ✓ already fixed in-window.

## Tier A

### LiteLLM cloud models (#44 improvable, #66 acceptable)
- ✓ **#44 ConfigMap edit had no pod-roll** → the new cloud models wouldn't actually load until an unrelated restart (exactly the part-1 prediction). **Fixed by #66** (`checksum/config` annotation; digest verified correct).
- ✅ **#44 no spend cap** → applied a LiteLLM `max_budget: 50 / budget_duration: 30d` guard so a `LITELLM_MASTER_KEY` leak on the soon-public `api.chifor.me` can't run up unbounded OpenAI/Anthropic spend.
- ⚖️ **(codex) `api.chifor.me` has no Cloudflare Access** — the proxy is internet-exposed with only the master key as the gate; the access-apps doc says it *should* use a **Service Token** policy. Decision (changes the auth contract for `api.chifor.me` clients): add a service-token Access app, or segregate cloud models behind a separate budgeted virtual key. *Not auto-applied.*
- 📋 nits: replace the hand-maintained `checksum/config` with a Kustomize `configMapGenerator` (idiomatic, auto-rolls); **smoke-test the `gpt-5.4*` ids against OpenAI's catalog** (codex web-search found no `gpt-5.4` — may 404 at request time).
- ✅ secret handling is correct (keys are SOPS `ENC[...]`, referenced via `os.environ/`).

### Registry pull-through cache (#56 acceptable, #57 improvable, #70 acceptable)
- ✅ **#56 runbook would write the Docker Hub token in PLAINTEXT** — the existing `registry.sops.yaml` embeds a stale `encrypted_regex` (no token key), and SOPS uses the *embedded* regex on edit, so the doc's "already encrypted" assurance was false. Fixed the runbook to re-encrypt-from-decrypted + verify. (Latent only — token is unused today.)
- 📋 **#70 count-based retention is decoupled from the deployed pin** — raising keep_recent 25→100 only widens the window; it doesn't protect the live tag, so the 2026-06-28 ImagePullBackOff incident class can recur. Fix needs platform CI to push a **stable `ailab` tag** + a `keepTags ["^ailab$"]` pattern. *Cross-repo; recommended.*
- 📋 (codex) registry data volume has no `prevent_destroy`/backup (carried from part-1); mirror cache can fill the 192 GiB store (no retention on non-strive tags); LXC template over HTTP + unverified zot binary download.

## Tier B

### Dependency bumps (#46–#65) — **the skews**
- 🔺 **#58 `problematic` — Flux controllers hand-bumped, CRDs/RBAC left at v2.8.8.** Only the 4 controller image tags moved (source/kustomize/notification `v1.8.5→v1.9.0`, helm `v1.5.5→v1.6.0`) while the CRDs, RBAC, and `version: v2.8.8` labels in `gotk-components.yaml` stayed — an **unsupported skew** in the GitOps engine itself (`flux check` will complain; next `flux bootstrap` reverts it). **Fix on WSL2:** regenerate `gotk-components.yaml` from a real flux2 release via the `flux` CLI (forward to v2.9.x), *or* revert the 4 tags to v2.8.8's bundled set. Not auto-applied (downgrading/regenerating the live Flux engine needs the CLI). The `ignorePaths` added in part-1 (49b0060) stops Renovate from doing this again.
- 🔺 **#60 `improvable` — snapshot-controller `v8.0.1→v8.6.0`, CRDs/RBAC vendored at v8.2.0.** Already skewed before #60; #60 widened it. The controller provides the VolumeSnapshot API Velero/Trident depend on (ADR 0010), so missing RBAC verbs = silent DR degradation. **Fix on WSL2:** re-vendor the full external-snapshotter **v8.6.0** bundle (CRDs + RBAC + setup) as a unit + update the kustomization comment. Not a clean revert (image was already ahead of CRDs).
- ✅ **#48 busybox on Docker Hub** (kube-prometheus-stack init) → switched to `mirror.gcr.io/library/busybox` (homepage already does), off the anonymous 429 path.
- ✅ **#62 terraform locks not committed** → un-gitignored `.terraform.lock.hcl`. **Operator step (WSL2):** `tofu init` per module + commit the locks (talos floor jumped 0.7→0.11, so also `tofu plan` the talos module to confirm schema). 📋 #61 ai-lxc "validated on 0.109" comment is stale.
- The other 12 bumps are low-risk patch/minor (ntfy, zot, velero-plugin, busybox-mirror, blackbox-exporter, oauth2-proxy, flux2 distro #49) — confirmed no major jumps.

### AI window tuning (#69 acceptable)
- ✅ **#69 qwen3-30b `# host CTX=8192` comment was misleading** (daily driver is CTX=32768 PARALLEL=4 → 8192/slot) → annotated.
- 📋 **#69 qwen3.5-122b has no runbook launch command** to "keep in sync" with → the `max_input_tokens` is an unverifiable assumption. Add an explicit launch snippet to `ai-host-setup.md` (needs the real CTX/PARALLEL).

### CI runner memory (#68 `best`), README (#45 acceptable) — clean
No high-severity findings; codex pass skipped. #68 (balloon floor 12 GiB + 8 GiB swap) is sound; #45 is an accurate docs refresh.

## Applied fixes (branch `chore/merge-validation-part2`)

| Commit | Fix | Finding |
|---|---|---|
| `0d8ccf3` | Zot LXC binary v2.1.17→**v2.1.18** (match the compose image) | drift from #63 |
| `e46794e` | LiteLLM `max_budget` spend cap + qwen3-30b CTX comment | #44, #69 |
| `2d21982` | busybox init image → `mirror.gcr.io` | #48 |
| `6eaa6d8` | registry-cache runbook: correct the Docker Hub-token SOPS step | #56 |
| `6377352` | un-ignore `.terraform.lock.hcl` (commit locks) | #62 |

## Priority operator follow-ups (need WSL2 tooling / a decision)

1. 🔺 **#58 Flux skew** — `flux` CLI to realign controllers↔CRDs (forward to v2.9.x or revert to v2.8.8). Highest priority — it's the GitOps engine.
2. 🔺 **#60 snapshot-controller skew** — re-vendor external-snapshotter v8.6.0 as a unit (CSI/DR dependency).
3. 🔺 **#62 locks** — `tofu init` per module + commit `.terraform.lock.hcl`; `tofu plan` the talos module.
4. ⚖️ **`api.chifor.me` Access** + LiteLLM virtual-key segregation for the paid path.
5. 📋 `gpt-5.4*` model-id smoke test; #70 stable-`ailab`-tag retention (platform CI); qwen3.5-122b runbook snippet; LiteLLM `configMapGenerator`; registry volume backup/`prevent_destroy`.

## Verification
All edited manifests parse; `renovate.json` untouched this round. Run `just lint` + a Flux dry-run on WSL2 before merge.

**Codex final pass (fix diff):** all 5 fixes confirmed correct, no regressions. Specifically verified: `litellm_settings.max_budget`/`budget_duration: 30d` are honored by the `main-stable` image and only cap spend; `mirror.gcr.io/library/busybox:1.38` matches homepage's path and the init command is unchanged; the SOPS re-encrypt sequence correctly applies the `.sops.yaml` creation_rule (token ends ciphertext); v2.1.18 is a real release and the Ansible/compose pins now match. Codex's one nit — the `.gitignore` commit's verb "commit" reads stronger than the change (it only un-ignores; committing the locks is the documented operator step) — is cosmetic and already mitigated by the "(un-ignore)" in the message; left as-is. Report classification of #58/#60 as operator follow-ups confirmed sound.
