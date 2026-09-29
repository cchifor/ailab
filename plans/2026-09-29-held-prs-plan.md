# Resolve the three held ailab PRs: #917 (LiteLLM), #900/#901 (router admin bridge), #835 (env-node-2)

## Codex Review

- The approach is sound overall; the core technical claims about the usage-signal regression and bridge enablement strategy are justified.
- Two concrete omissions: (1) B3 must add `admin-bridge.yaml` to the llm-router Kustomization resources list so the ConfigMap is created, and (2) C must clarify golden-v2 source-volume ownership/retention lifecycle, not just state the gate.
- The LiteLLM regression hypothesis remains unproven but plausible; the test really does assert a missing-usage error. A4 must test both 1.101.0 and 1.101.1 as baselines to identify the first affected release.
- A2's Renovate rule change (digest-only to all updateTypes for litellm) is safe; split the rule to keep text-embeddings digest-only. No conflicting rules exist; this does not create unwanted grouping.
- B1 cleanup is straightforward; verify the canary result before deleting objects. B3 requires both kustomization.yaml wiring and runtime verification that the deployed artifact contains and registers the admin-bridge plugin.

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
  - This is a hypothesis, not yet proven. <!-- codex: e-nousage really does assert missing-usage error; reported 11/9/20 consistent with backfilled estimates; preserve "hypothesis, not proven" wording. -->
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

1. **Re-pin to the version tag, same digest.** Change the three references in `litellm.yaml`,
   `litellm-local.yaml` and `litellm-vkeys.yaml` to `ghcr.io/berriai/litellm:v1.101.0@sha256:d295634e…`.
   - The digest is identical, but the pod-template image string changes, so the LiteLLM
     Deployments roll once. Merge it outside busy hours and confirm the rollout. <!-- codex: litellm.yaml and litellm-local.yaml contain ConfigMap+Deployment+Service; litellm-vkeys.yaml contains ConfigMap+Job. Image references are in Deployment/Job pod templates. Changing main-stable@digest to v1.101.0@same-digest triggers pod replacement despite identical content. vkeys Job is operator-run and absent from Kustomize, so it does NOT rerun through Flux; handle its lifecycle explicitly if needed. -->
   - Renovate then proposes discrete releases (`v1.101.2`, `v1.102.1`, `v1.103.0`) that the
     contract judges one by one, instead of one opaque moving-tag digest.
2. **Renovate rule.** Make every `ghcr.io/berriai/litellm` update type (not only `digest`)
   `automerge: false` + `addLabels: ["no-automerge"]`. The gateway fronts every LLM call, and the
   contract proves only what it covers. Keep the TEI digest rule as it is. <!-- codex: Split the current combined rule: retain TEI's digest-only restriction, give litellm its own rule with matchUpdateTypes omitted. No conflicting later rules exist. Matching package rules combine but no dependency grouping occurs without groupName. Renovate can propose newer versions directly, not necessarily sequentially. -->
3. **Close #917** with a pointer to this plan.
4. **Bisect offline on a dev-worker.** Run the contract exactly as the CI job does: the manifest's
   image, networking disabled, the test file mounted. Do this for `v1.101.0`, `v1.101.1`, `v1.101.2`, `v1.102.0`,
   `v1.102.1` and `v1.103.0`. <!-- codex: Include 1.101.0 and 1.101.1 as baselines to identify first affected release. Record runtime package versions from adapter files. e-nousage currently accepts any error; require specific missing-usage rejection. -->
   - Record the first version where `e-nousage` fails.
   - The test prints `adapter_source_sha256` for the bridge files (`llms/chatgpt/responses/transformation.py`,
     `completion_extras/litellm_responses_transformation/transformation.py`,
     `litellm_core_utils/streaming_handler.py`, …). Diff those files between the last passing and
     the first failing version to find the change.
5. **Fix the handler so the "did upstream report usage" signal survives the bridge change.** Do not
   loosen the contract. Direction, decided after step 4: take the signal from something the bridge
   cannot synthesise, such as the upstream `response.completed` event's usage observed at the
   transport/SSE seam the handler already controls, or a provenance marker the new bridge sets on a
   synthesised block. <!-- codex: Observing raw SSE introduces another production integration point. Any provenance state must be isolated per request and survive concurrency, cancellation, stream closure. Cover omitted/null/empty/zero usage; require missing-usage error, not any exception. Preserve ADR 0027 distinction: exact upstream for non-streaming, estimates documented for streaming. -->
   - It must keep passing on 1.101.0, because the contract always runs against the manifest's image.
   - The upgrade then ships as ONE PR: handler change + version bump + the `checksum/chatgpt-chat`
     annotation (inline-hash site 5, `scripts/check-inline-hashes.py`).
   - If the fix proves invasive, the fallback is to stay on 1.101.x and take `v1.101.2` if it passes.

### B. #900 / #901: enable the bridge on the live release; clean up the canary

1. **#901 canary cleanup PR, first.** This is zero-downtime: no Deployment template change.
   - Delete `network-preflight.yaml` and its kustomization entry.
   - Drop `llm-router-preflight` from the NetworkPolicy selector.
   - Record the canary result in `docs/runbooks/llm-router.md` in place of the "pending" text.
   - Flux prunes the completed Job.
2. **Close #900 as superseded**, commenting with the ancestry proof. Then **revoke deploy key 6**
   on `dsh/llm-router`: it existed only for #900's init-container fetch. Its SOPS copy lives only
   on the unmerged branch. <!-- codex: After closing #900, verify key 6 is absent from the deploy-keys list. Success criterion is "key 6 does not exist," not just "repository has deploy keys." -->
3. **Bridge enablement PR on the current release.** <!-- codex: B3 MUST add admin-bridge.yaml to kustomization.yaml resources list. Otherwise the required ConfigMap is never created and the Recreate rollout leaves the singleton unavailable. Verify the deployed router-0.1.0-20260929-subs artifact contains and registers the admin-bridge plugin at runtime, not just source ancestry. -->
   - Add `admin-bridge.yaml` (the public-key ConfigMap, content taken from #900).
   - Add env `ADMIN_BRIDGE_VERIFY_KEY` and the `admin-bridge` volume + read-only mount to the router container.
   - Update the runbook section.
   - No subPath change, no init containers, no deploy key, no temporary egress, no SOPS secret.
   - Leave `ROUTER_PLUGIN_CONFIG` inline (no refactor).
   - It rolls the Recreate singleton once, the same as a config change.
   - It turns on a new externally reachable, signed-assertion endpoint, so it needs the owner's
     explicit go. The bot review alone is not enough. <!-- codex: Test tampered/expired/replayed/scoped assertions; verify verifier fingerprint against Admin's signing key; test rollback that disables bridge without reverting release. -->
   - Before merging, confirm the Admin side (trueswarm-admin) holds the matching private key and
     targets `router.chifor.me`.

### C. #835: park it behind an explicit pool-restore gate

1. **Do not rebase or apply now.**
2. **Open a tracking issue "env-pool restore (golden-v2 + resume)"** listing the prerequisites:
   - NAS-side target 11 / clone-LUN cleanup verified, and a stale session reset on VM 4401
   - golden-v2 from a source volume that is kept alive <!-- codex: golden-refresh.sh:75 deletes the populate pod whose source PVC is generic ephemeral. Reusing that procedure conflicts with the requirement to keep the golden source volume alive. Add explicit prerequisite to change source-volume ownership/retention, publish and verify golden-v2, update template pointer before restoring replica. -->
   - #865 fixed
   - the pool back to 1 replica
   - one measured release/refill cycle
   - a demand check: `agent_sandbox_claim_creation_total` since the 09-23 routing fix (historical zero leases are weak evidence; use current metrics)
3. **Comment on #835** linking that issue as its gate. Keep the label `no-automerge`; the reviewbot checks that label and draft status, not an explicit `WIP:` title rule. <!-- codex: Include current #865/#879 reproduction results and fresh memory headroom under load. -->
   Only after the gate:
   - check ai-node3 `MemAvailable` headroom for the new VM (ai-nodes carry live data)
   - rebase #835
   - run `just env-pool-plan` / `env-pool-apply` (staged; reboot via talosctl)
   - verify one warm member per node
   - remove `no-automerge`

## Critical files

- `kubernetes/apps/apps/ai/litellm.yaml`, `litellm-local.yaml`, `litellm-vkeys.yaml`: image pin (A1); the upgrade PR later (A5)
- `renovate.json`: the LiteLLM rule (A2)
- `kubernetes/apps/apps/ai/chatgpt_chat.py`: the usage-provenance fix (A5); plus the `checksum/chatgpt-chat` annotation in `litellm.yaml`
- `scripts/tests/integration/test_litellm_chatgpt_chat_handler_contract.py`: unchanged assertions. New cases only if the fix adds a new seam.
- `kubernetes/apps/apps/llm-router/{kustomization.yaml,router.yaml,network-preflight.yaml}`, `docs/runbooks/llm-router.md`: (B1, B3)
- `kubernetes/apps/apps/llm-router/admin-bridge.yaml` (new, B3)
- #835 branch `feat/env-node-2`: untouched until its gate (C)

## Verification

- **A1:** `kustomize build` + kubeconform are green, and the litellm-route-contract CI passes on the
  version-tag pin: same digest, so same result as today. After merge, the LiteLLM Deployments roll
  to Ready and a smoke completion through `api.chifor.me` answers.
- **A2:** `renovate-config-validator --strict` passes. The next Renovate run opens `v1.101.x` / `v1.10x`
  version PRs carrying `no-automerge`.
- **A4:** a table of version → contract result per case, with the adapter-file diff that explains the first failure.
- **A5:** the contract is green on 1.101.0 (current) and on the target version, with `e-nousage` still
  asserting an error and every other case unchanged.
- **B1:** after merge, `kubectl -n llm-router get job,networkpolicy` shows no preflight objects.
  Router pods are not restarted (the Deployment generation is unchanged).
- **B2:** the Gitea deploy-keys list for `dsh/llm-router` shows no entry 6.
- **B3:** after the roll:
  - `/register/admin-bridge` rejects an unsigned request and accepts an Admin-signed assertion
  - `node scripts/validate-live.mjs` passes 6/6
  - existing API keys still work
- **C:** the tracking issue exists and is linked from #835. No infra change.

<!-- codex-review-status: complete -->
