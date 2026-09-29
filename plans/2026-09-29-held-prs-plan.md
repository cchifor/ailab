# Resolve the three held ailab PRs: #917 (LiteLLM), #900/#901 (router admin bridge), #835 (env-node-2)

## Context

The 2026-09-29 PR triage merged or closed everything the reviewer bot could handle. Three PRs remain,
and none of them will ever merge on its own. Facts established on 2026-09-29:

**#917: LiteLLM `main-stable` digest bump `d295634e…` → `bd089afd…` (Renovate, `no-automerge`).**
- The deployed digest is exactly `v1.101.0` and the proposed one is exactly `v1.103.0`: tag →
  digest resolved on ghcr. `main-stable` just tracks the latest release. Versioned tags exist:
  `v1.101.1`, `v1.101.2`, `v1.102.0`, `v1.102.1`, `v1.103.0`.
- `litellm-route-contract` fails on substance under 1.103.0, in
  `test_litellm_chatgpt_chat_handler_contract.py` case `e-nousage`.
  - The upstream SSE stream reports no usage, and the case expects an error.
  - Instead the caller gets a `ModelResponse` with `usage` 11/9/20, which look like back-filled token estimates.
  - `len(sent) == 1` passed, so this is not a retry.
  - A Pydantic warning (`Expected ResponseAPIUsage … completion_tokens: 9`) suggests the 1.103.0
    Responses bridge now synthesises a usage block on its own terminal chunk.
  - That would erase the only signal `chatgpt_chat.py:274` uses:
    `chunk.usage` on the bridge's raw `completion_stream`.
  - This is a hypothesis, not yet proven.
- The contract exists because the consumer records usage as billed truth (ADR 0027). A silent
  estimate is exactly the failure it guards.
- The first red run (09-27) was a runner containerd glitch. The re-run on 09-29 at 16:59Z is the real failure.

**#900: "Deploy scoped Trueswarm Admin bridge" (author `chifor`, conflicting).**
- #900 builds release `router-0.1.0-20260927-admin-bridge` from `dsh/llm-router@a2a3b20e`
  (router PR #42) through two temporary init containers. It adds:
  - a new read-only deploy key: Gitea key id 6, `release-stage-readonly-20260927`, live on `dsh/llm-router`
  - a SOPS secret holding that key
  - a temporary SSH egress NetworkPolicy
  - a SQLite backup step
  - Recreate-time staging
- `a2a3b20e` is an **ancestor** of router `main@05bfecf` (0 commits only on a2a3b20e, 59 after it).
  `05bfecf` is `router-0.1.0-20260929-subs`, the release production runs since ailab #958. Its tree
  contains `packages/plugins/admin-bridge/`.
- So the bridge code is **already deployed**. #900's remaining value is only the enablement:
  - the verifier public-key ConfigMap `admin-bridge-verifier` (`public.pem`, Ed25519)
  - env `ADMIN_BRIDGE_VERIFY_KEY=/admin-bridge/public.pem`
  - the volume + mount
  - a cosmetic move of `ROUTER_PLUGIN_CONFIG` into a ConfigMap
- A plain rebase of #900 would move production back to 09-27 source.
- #900's last open review finding (npm/DNS egress unproven) was answered by #901's canary:
  `router-network-preflight-20260927` → `"result":"passed"`.

**#901's canary is still in main**, pending cleanup, as its own runbook says:
- `kubernetes/apps/apps/llm-router/network-preflight.yaml`
- its `kustomization.yaml` entry (line 15)
- the extra `llm-router-preflight` value in the router NetworkPolicy selector (`router.yaml:243`)

**#835: env-node-2, a second warm env-pool member on ai-node3 (T4, gate G3b, `no-automerge`, conflicting).**
- The pool is **paused**: `SandboxWarmPool env-std-pool` has `replicas: 0` since ailab#880
  (2026-09-26, the shared golden-v1 iSCSI target 11 died).
- golden-v2 was never built, and no tracking issue exists for the restore.
- Related open issues: #865 (env pods get Zot 401 on pulls) and #879 (registry tag lookups hang).
- Usage history: zero leases 09-12…09-23, until the platform routing fix.
- #835's premise, spreading two warm members across two nodes, cannot be exercised while the pool
  is at 0 with no golden image. It also costs RAM on an ai-node that is processing data.

## Approach

### A. #917: stop tracking a moving tag, find the break, fix the handler, then upgrade

1. **Re-pin to the version tag, same digest.** Change the image references in `litellm.yaml` and
   `litellm-local.yaml` (Deployments) and `litellm-vkeys.yaml` to
   `ghcr.io/berriai/litellm:v1.101.0@sha256:d295634e…`.
   - `litellm-vkeys.yaml` holds an operator-run seeding Job. It is absent from the kustomization,
     so Flux never runs it, and the edit only keeps the pin consistent. Say so in the PR.
   - The digest is identical, but the pod-template image string changes, so both LiteLLM
     Deployments roll once. Merge it outside busy hours and confirm the rollout.
   - Renovate then proposes version updates (not necessarily one release at a time) that the
     contract judges, instead of an opaque moving-tag digest.
2. **Renovate rule.** Split the combined moving-tag rule:
   - TEI keeps its `matchUpdateTypes: ["digest"]` + `no-automerge` rule.
   - `ghcr.io/berriai/litellm` gets its own rule with NO `matchUpdateTypes`, so every update type
     is `automerge: false` + `addLabels: ["no-automerge"]`. The gateway fronts every LLM call, and
     the contract proves only what it covers.
   - No `groupName`, so nothing new gets grouped.
3. **Close #917** with a pointer to this plan.
4. **Bisect offline on a dev-worker.** Run the contract exactly as the CI job does: the manifest's
   image, networking disabled, the test file mounted.
   - Versions: `v1.101.0` (baseline, must pass), `v1.101.1`, `v1.101.2`, `v1.102.0`, `v1.102.1`, `v1.103.0`.
   - Record per version: every case result, the runtime package versions, and the
     `adapter_source_sha256` the test prints.
   - Diff the adapter files between the last passing and the first failing version to find the change.
5. **Tighten `e-nousage` first.** It currently accepts ANY error. Require the handler's specific
   missing-usage rejection (the `reported no usage` message / 502 mapping). This strengthens the
   contract and never loosens it. Ship it with, or before, the fix.
6. **Fix the handler so the "did upstream report usage" signal survives the bridge change.** The
   direction is decided after step 4: take the signal from something the bridge cannot synthesise,
   e.g. the upstream `response.completed` usage observed at the transport/SSE seam the handler
   already controls, or a provenance marker the new bridge sets on a synthesised block. Constraints:
   - Any provenance state is per request, and correct under concurrency, cancellation and stream closure.
   - Omitted, `null`, empty and zero usage are each covered by a case. Zero reported by upstream is
     real usage, not missing.
   - ADR 0027's distinction stands: non-streaming returns the upstream's exact usage; streaming's
     documented behaviour is unchanged.
   - It must keep passing on 1.101.0, because the contract always runs against the manifest's image.
   - The upgrade ships as ONE PR: handler change + version bump + the `checksum/chatgpt-chat`
     annotation (inline-hash site 5, `scripts/check-inline-hashes.py`).
   - If the fix proves invasive, the fallback is to stay on 1.101.x and take the newest 1.101 patch
     that passes.

### B. #900 / #901: enable the bridge on the live release; clean up the canary

1. **#901 canary cleanup PR, first.** This is zero-downtime: no Deployment template change.
   - The canary result is already recorded and verified: `"result":"passed"`, Job Complete 1/1.
   - Delete `network-preflight.yaml` and its kustomization entry.
   - Drop `llm-router-preflight` from the NetworkPolicy selector.
   - Record the result in `docs/runbooks/llm-router.md` in place of the "pending" text.
   - Flux prunes the completed Job.
2. **Close #900 as superseded**, commenting with the ancestry proof. Then **revoke deploy key 6**
   (`release-stage-readonly-20260927`) on `dsh/llm-router`: it existed only for #900's
   init-container fetch. Its SOPS copy lives only on the unmerged branch.
3. **Bridge enablement PR on the current release.**
   - Add `admin-bridge.yaml` (the public-key ConfigMap, content from #900) AND list it in
     `kustomization.yaml` resources. Without the ConfigMap, the pod cannot mount it and the
     Recreate singleton goes down.
   - Add env `ADMIN_BRIDGE_VERIFY_KEY` and the `admin-bridge` volume + read-only mount to the router container.
   - Update the runbook section, including the rollback: remove the env var + mount, which
     disables the bridge without reverting the release.
   - No subPath change, no init containers, no deploy key, no temporary egress, no SOPS secret.
     Leave `ROUTER_PLUGIN_CONFIG` inline (no refactor).
   - It rolls the Recreate singleton once, like any config change.
   - It turns on a new externally reachable, signed-assertion endpoint, so it needs the owner's
     explicit go. The bot review alone is not enough.
   - Before merging:
     - Check that the Admin side (trueswarm-admin) holds the private key that matches this public
       key: compare fingerprints.
     - Check that it targets `router.chifor.me`.
     - Check that the running `router-0.1.0-20260929-subs` artifact actually contains and
       registers the admin-bridge plugin (in `/app` on the live pod), not just source ancestry.

### C. #835: park it behind an explicit pool-restore gate

1. **Do not rebase or apply now.**
2. **Open a tracking issue "env-pool restore (golden-v2 + resume)"** listing the prerequisites:
   - NAS-side target 11 / clone-LUN cleanup verified, and a stale session reset on VM 4401.
   - A golden-v2 procedure change. `golden-refresh.sh` deletes the populate pod whose source PVC
     is a generic ephemeral volume, which contradicts "a source volume that is kept alive". The
     source-volume ownership and retention must change first. Then publish and verify golden-v2,
     and update the template pointer before restoring a replica.
   - #865 fixed. Record #865 and #879 reproduction results at the time.
   - The pool back to 1 replica.
   - One measured release/refill cycle.
   - A demand check on current metrics (`agent_sandbox_claim_creation_total` since the 09-23
     routing fix). The historical zero-lease window is weak evidence either way.
3. **Comment on #835** linking that issue as its gate, and keep the `no-automerge` label (the
   reviewbot honours that label; a `WIP:` title is not a rule it checks). Only after the gate:
   - check ai-node3 `MemAvailable` headroom under load for the new VM (ai-nodes carry live data)
   - rebase #835
   - run `just env-pool-plan` / `env-pool-apply` (staged; reboot via talosctl)
   - verify one warm member per node
   - remove `no-automerge`

## Critical files

- `kubernetes/apps/apps/ai/litellm.yaml`, `litellm-local.yaml`, `litellm-vkeys.yaml`: image pin (A1); the upgrade PR later (A6)
- `renovate.json`: the split LiteLLM rule (A2)
- `scripts/tests/integration/test_litellm_chatgpt_chat_handler_contract.py`: the tightened `e-nousage` (A5), plus new usage cases for the fix (A6)
- `kubernetes/apps/apps/ai/chatgpt_chat.py`: the usage-provenance fix (A6), plus the `checksum/chatgpt-chat` annotation in `litellm.yaml`
- `kubernetes/apps/apps/llm-router/{kustomization.yaml,router.yaml,network-preflight.yaml}`, `docs/runbooks/llm-router.md`: (B1, B3)
- `kubernetes/apps/apps/llm-router/admin-bridge.yaml` (new, B3)
- #835 branch `feat/env-node-2`: untouched until its gate (C)

## Verification

- **A1:** `kustomize build` + kubeconform are green, and the litellm-route-contract CI passes on the
  version-tag pin (same digest, same result as today). After merge, both LiteLLM Deployments roll to
  Ready and a smoke completion through `api.chifor.me` answers.
- **A2:** `renovate-config-validator --strict` passes. The next Renovate run's LiteLLM PRs are version
  updates carrying `no-automerge`, and TEI digest PRs still carry it.
- **A4:** a version → per-case result table, with the adapter-file diff that explains the first failure.
- **A5/A6:** the contract is green on 1.101.0 AND on the target version. `e-nousage` asserts the
  specific missing-usage error, the new omitted/null/empty/zero cases pass, and every other case is unchanged.
- **B1:** after merge, `kubectl -n llm-router get job,networkpolicy` shows no preflight objects, and the
  router Deployment's generation is unchanged (no restart).
- **B2:** the `dsh/llm-router` deploy-keys list has no key 6.
- **B3:** after the roll:
  - the plugin is registered
  - `/register/admin-bridge` rejects an unsigned request and a tampered assertion, and accepts an
    Admin-signed one. Expired and replayed assertions are covered by the deployed commit's
    `tests/admin-bridge.test.ts`.
  - `node scripts/validate-live.mjs` passes 6/6
  - existing API keys still work
  - the rollback path is written down
- **C:** the tracking issue exists and is linked from #835. No infra change.

<!-- codex-review-status: complete -->
