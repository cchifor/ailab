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