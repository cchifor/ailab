# c-p0-05-manifest-lint — codex implementation review

## Round 1 — model gpt-5.6-sol — branch ci/c-p0-05-manifest-lint — base origin/main — 2026-09-02T11:49Z

<!-- codex-impl-review-status: complete -->
## Verdict
changes_requested

## Findings
**Location** scripts/manifest-paths.py:163 — **Severity** high — **Issue** Discovery silently excludes the `agentforge-tenants` and `platform` Flux Kustomizations based on `sourceRef`, returning 23 of the 25 current `spec.path` values. This narrows the binding requirement to validate every path and makes the test explicitly bless omissions. — **Fix** Validate every Flux Kustomization path, provisioning/checking out the referenced repositories where necessary, or obtain an explicit spec amendment permitting external-source exclusions.

**Location** scripts/tests/test_manifest_paths.py:179 — **Severity** medium — **Issue** The required broken-fixture test invokes only `manifest-paths.py`’s in-process `main()`. It never runs `scripts/manifest-lint.sh`, so it does not prove the lint command exits non-zero or that failure propagates through its shell control flow. — **Fix** Add a subprocess test that runs `bash scripts/manifest-lint.sh` against the broken fixture with Docker safely stubbed; assert a non-zero exit before any build/validation succeeds.

## Checked
- **Workflow definition — pass.** One `manifests` job, push and pull-request triggers, per-ref concurrency, configured runner fallback, 15-minute timeout, checkout, manifest lint, and inline-hash verification; no `needs`, `if`, or `continue-on-error`.
- **Manifest lint implementation — pass.** Uses `set -euo pipefail`, digest-pinned kustomize v5.4.3 and kubeconform v0.6.7, one fail-closed build per discovered path, strict/summary schema validation, and a documented `Secret` skip.
- **Path discovery — fail.** YAML-free fallback and kustomization-file assertion exist, but discovery intentionally omits two current Flux paths instead of covering every `spec.path`.
- **Required tests — fail.** Exact local-set and helper failure behavior are tested, but the expected set is 23 rather than every listed Flux path and the broken fixture does not execute the lint script.
- **Just recipe — pass.** `manifest-lint` directly invokes `bash scripts/manifest-lint.sh` after `af-verify-hashes`.
- **Tests-first — pass.** `b1bc722` adds failing behavioral tests before `e4dfea5` and `06b60f0`; the later regex regression tests also precede their fix.
- **Credential/token safety — pass.** The PR introduces no bearer/auth/token handling or credential values; builds use read-only mounts and no manifest content is printed by the new code.
- **Wire compatibility and frozen test IDs — pass.** No API/DTO, mock-server, broker-stub, or UI test-ID changes are present.
- **House rules, scope, and diff hygiene — pass.** No repository `AGENTS.md` or `TESTING.md` was present; the merge-base diff is confined to manifest-validation workflow, scripts, tests, recipe, and output ignore entry. `git diff --check` passed.
- **Verification plausibility — pass with environment limitation.** Six real-tree discovery tests, `bash -n`, and inline-hash verification passed. The complete unittest run could not be reproduced in this read-only review sandbox because Python had no writable temporary directory; Docker execution likewise could not create `out/`. The commands and pinned invocation are otherwise plausible from the diff.