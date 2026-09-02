# c-p0-07-broker-servicemonitor — codex implementation review

## Round 1 — model gpt-5.6-sol — branch feat/c-p0-07-broker-servicemonitor — base origin/main — 2026-09-02T13:18Z

<!-- codex-impl-review-status: complete -->
## Verdict
changes_requested

## Findings
**Location** kubernetes/apps/infrastructure/monitoring/agentforge-rules.yaml:473 — **Severity** medium — **Issue** `ForgeBrokerSeatReplicaMissing` is an additional, non-DECIDED alert added in the final production commit without a preceding failing behavioral test. It also knowingly misses the most serious case where both replicas disappear. This violates the tests-first and no-unrelated-scope checks. — **Fix** Remove this alert from C-P0-07 and propose it separately with tests covering one- and zero-discovered-replica cases, or add a preceding failing test and implement complete seat enumeration.

## Checked
- **PASS — ServiceMonitor resource:** `agentforge-broker` is in `monitoring`, labeled `release: kube-prometheus-stack`, and selects namespace `agentforge-broker`.
- **PASS — Service selector:** Both required `component: broker` and `part-of: agentforge` labels are present and tested against every pinned broker Service.
- **PASS — Endpoint configuration:** Uses named port `metrics`, a 15-second interval, constant `job=agentforge-broker`, and derives `seat` from the Service name label.
- **PASS — Kustomization:** `servicemonitor.yaml` is listed immediately after `configmap.yaml` with the required explanatory comment.
- **PASS — Required alert:** `ForgeBrokerTargetDown` uses `up{job="agentforge-broker"} == 0`, `for: 5m`, critical severity, and seat-aware description.
- **PASS — Inventory-glob safety:** The filename is outside `broker-*.yaml`; tests verify both glob exclusion and `load_seats()` behavior.
- **PASS — Metrics-port coverage:** Tests verify every routable pinned Service exposes every configured ServiceMonitor endpoint port.
- **PASS — Inventory verification:** `python3 scripts/gen-broker-inventory.py --check` completed successfully.
- **PASS — Tests-first for the DECIDED feature:** Commit `0b8ee63` adds failing structural tests before production commit `13a4156`.
- **FAIL — Tests-first for all added production behavior:** The additional replica-missing alert was introduced in `f544c92` without a preceding behavioral test.
- **PASS — Credential safety:** The diff handles no credentials or request bodies and introduces no path by which bearer tokens, `auth.json`, `sk-ant-`, or JWT-like values reach output.
- **PASS — Wire compatibility and testids:** No API/DTO, mock-server, broker-stub, or UI testid changes are present.
- **FAIL — House-rule scope discipline:** The unrequested second alert adds substantial independently deployable behavior beyond C-P0-07.
- **PASS — Diff hygiene:** The three-dot diff contains only the four intended areas and `git diff --check` reports no formatting errors.
- **INCONCLUSIVE — Full unittest tier:** The new ServiceMonitor tests pass, but the repository-wide command cannot complete in this read-only environment because existing tests require a writable temporary directory.
## Round 2 — model gpt-5.6-sol — branch feat/c-p0-07-broker-servicemonitor — base origin/main — 2026-09-02T13:28Z

<!-- codex-impl-review-status: complete -->
## Verdict
changes_requested

## Findings
**Location** kubernetes/apps/infrastructure/monitoring/agentforge-rules.yaml:473 — **Severity** medium — **Issue** `ForgeBrokerSeatReplicaMissing` is an additional, non-DECIDED production alert introduced in commit `f544c92`; its behavioral tests were added only afterward in `2bb471f`, violating the mandatory tests-first rule. It also knowingly cannot detect a seat when both replicas disappear. — **Fix** Remove the alert from C-P0-07 and pursue it as a separately specified change, or rewrite the history so a failing behavioral test precedes a complete implementation that covers both partial and total discovery loss.

## Checked
- **PASS — ServiceMonitor identity:** `agentforge-broker` is created in `monitoring` with `release: kube-prometheus-stack`.
- **PASS — Namespace selection:** `namespaceSelector.matchNames` contains `agentforge-broker`.
- **PASS — Service selection:** Required `component: broker` and `part-of: agentforge` labels match every pinned routable Service.
- **PASS — Endpoint:** Uses `metrics`, a 15-second interval, constant `job=agentforge-broker`, and derives `seat` from `app.kubernetes.io/name`.
- **PASS — Kustomization:** The resource appears immediately after `configmap.yaml` with the required comment.
- **PASS — Required alert:** `ForgeBrokerTargetDown` has the exact expression, five-minute hold, critical severity, and seat-aware description.
- **PASS — Inventory-glob safety:** `servicemonitor.yaml` does not match `broker-*.yaml`; `load_seats()` still finds exactly four seats.
- **PASS — Metrics-port coverage:** Tests verify every routable pinned Service exposes the configured `metrics` port.
- **PASS — Tests-first for DECIDED behavior:** Commit `0b8ee63` precedes production commit `13a4156` and exercises selector, port, glob, and inventory behavior.
- **FAIL — Tests-first for all production behavior:** Commit `f544c92` added `ForgeBrokerSeatReplicaMissing` before its behavioral tests in `2bb471f`.
- **PASS — Credential safety:** The diff handles no credentials, request bodies, or client responses and introduces no route for bearer tokens, `auth.json`, `sk-ant-`, or JWT-like values.
- **PASS — Wire compatibility:** No API/DTO, mock-server, broker-stub, or frozen data-testid changes occur.
- **FAIL — House-rule scope discipline:** The second alert is unrelated to the binding C-P0-07 scope and substantially expands the monitoring behavior.
- **PASS — Diff/base hygiene:** The three-dot diff contains the intended implementation areas plus the conventionally allowed review artifact; `git diff --check` passes.
- **PASS — Scoped tests:** All 16 ServiceMonitor tests and `gen-broker-inventory.py --check` pass.
- **PASS — Claimed validation plausibility:** Commit claims align with the manifest/test changes. The full local suite reached 83 tests but 19 errored solely because the read-only environment provides no writable temporary directory.