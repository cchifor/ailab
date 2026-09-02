# c-p0-06-worker-probes — codex implementation review

## Round 1 — model gpt-5.6-sol — branch fix/c-p0-06-worker-probes — base origin/main — 2026-09-02T12:28Z

<!-- codex-impl-review-status: complete -->
## Verdict
approve

## Findings
None.

## Checked

- **PASS — Worker identity:** Both Deployments set `AF_WORKER_INSTANCE` through `valueFrom.fieldRef.fieldPath: metadata.name`, with no literal value.
- **PASS — Worker probes:** Startup and liveness use `/healthz`; readiness uses `/readyz`; all constants exactly match the specification.
- **PASS — Dispatcher probes:** Liveness and readiness use the required paths, periods, timeouts, and thresholds.
- **PASS — Probe ports:** Every probe references the existing named `webhook` container port (`8700` worker, `8710` dispatcher).
- **PASS — Manifest tests:** `scripts/tests/test_worker_probes.py` loads the real YAML and checks both downward-API wiring and every required probe property; all 9 tests pass.
- **PASS — Tests-first:** Commit `a02a8f5` adds the behavioral tests before production commit `fd70e3d`; the parent manifests contain none of the expected identity or probe fields, so the tests are demonstrably red there.
- **PASS — Dispatcher engine comment:** The requested `infra/settings.py` wording is explicitly an engine-side docs follow-up and is correctly excluded from this repository.
- **PASS — Credential safety:** The diff introduces no credential processing, logging, response bodies, metrics, or client forwarding; no bearer, `auth.json`, `sk-ant-`, or JWT-like `eyJ` material appears.
- **PASS — Wire compatibility:** No API or DTO changes are present; no frozen data-testids, mock server, or broker-stub behavior is touched.
- **PASS — House rules/scope:** The merge-base diff contains only the two manifests, their real-manifest tests, and a directly related CI comment; no unrelated refactor or base-movement deletion is present.
- **PASS — Diff size:** The change is narrowly scoped and proportionate to the manifest documentation and required tests.
- **PASS — Verification plausibility:** Focused tests pass 9/9 and `gen-broker-inventory.py --check` passes. The full discovery run was attempted but unrelated pre-existing tests require a writable temporary directory unavailable in the review sandbox; the new tests themselves all passed. The claimed pinned kustomize/kubeconform commands are consistent with the manifest-only diff.