# Implementation review — ci-runners-host-mode-gate2 — round 1

<!-- codex-impl-review-status: finalized -->
<!-- Phase B: 1 round, converged. Codex raised 9 findings (1 blocker, 6 important, 2 nits); ALL accepted
and fixed — API token containment (no-redirect opener + token validation + fully-sanitized errors),
gitea status policy {200,401,403}, fail-closed duplicate probe keys + duplicate/non-str API names,
full-pool API gate, API pagination, +18 mocked transport/main/host-key tests (49 total green), the
repo-wide agent-node .14-.16→.47-.49 doc reconciliation, the ADR phasing revert, and dead-constant
removal. No pushbacks. -->


## Findings

### API transport cannot guarantee token containment

**Location:** `scripts/check-ci-runners.py` (`query_gitea_runners`, `main`)
**Severity:** blocker
**Resolution (accepted):** `query_gitea_runners` now uses a `build_opener` with an `HTTPRedirectHandler`
that refuses ALL redirects (the `Authorization` header can never be re-sent to another host), validates
the token shape (`_validate_token`), and wraps every path — including a catch-all `except Exception` — so
only fixed, token-free `RuntimeError` messages escape. Tests assert a `TOKEN_SENTINEL` never appears in any
error or in `main`'s stdout.

### Redirect status codes deviate from the finalized policy

**Location:** `scripts/check-ci-runners.py` (`GITEA_REACHABLE`)
**Severity:** important
**Resolution (accepted):** `GITEA_REACHABLE` is now exactly `{200, 401, 403}`; 301/302 removed. The test
asserts 3xx/4xx-other/5xx/000 all FAIL.

### Identical duplicate probe keys are accepted

**Location:** `scripts/check-ci-runners.py` (`parse_probe_output`)
**Severity:** important
**Resolution (accepted):** any repeated key now collapses to `DUP` regardless of value; tests cover both
identical- and differing-value duplicates.

### Host IP overrides can make the API check vacuous

**Location:** `scripts/check-ci-runners.py` (`main`)
**Severity:** important
**Resolution (accepted):** `expected_names` is now fixed to the full pool (`ci-runner-1..5`) regardless of
positional args; positional IPs only narrow the SSH probe. A test proves a one-host run still FAILs when a
pool member is missing/offline in the API.

### API pagination and duplicate names are not fail-closed

**Location:** `scripts/check-ci-runners.py` (`query_gitea_runners`, `evaluate_api`)
**Severity:** important
**Resolution (accepted):** `query_gitea_runners` paginates (`?page=&limit=`, stops on a short page,
capped); `evaluate_api` rejects duplicate runner names and non-string `name`/`status` as schema failures.
Tests cover pagination and both schema-failure cases.

### Mandatory test cases are missing

**Location:** `scripts/tests/test_check_ci_runners.py`
**Severity:** important
**Resolution (accepted):** added mocked-transport tests (urllib 403/malformed/redirect-body/pagination/
unexpected-exception, all sentinel-redacted), `main()` integration (missing-token exit 1, unreachable-host
exit 1, full pool PASS with the token never printed, subpool-still-gates-full-pool), invalid-`.runner`
host eval, token validation, and an SSH host-key-policy test (AutoAddPolicy, no `load_system_host_keys`/
`save_host_keys`). 49 tests total, green.

### Current IP documentation remains contradictory

**Location:** `docs/network-plan.md`, `docs/decisions/0019-*`, `docs/runbooks/{agent-nodes,agentforge-activation,agentforge}.md`
**Severity:** important
**Resolution (accepted):** agent-nodes are LIVE at `.47/.48/.49` (verified via guest agent; `.14–.16` would
now collide with the renumbered CI runners). Fixed `network-plan.md` (agent-nodes row + free-space line +
removed the ".47–.49 free" claim) and the three operational runbooks to current-state, and annotated the
ADR 0019 decision body with the renumber (history preserved). A repo-wide sweep confirms the only remaining
`.14–.16` hits are annotated history or the correct `ci-runner-3` at `.16`.

### Original ADR phasing text was rewritten

**Location:** `docs/decisions/0019-agentforge-v2-control-plane.md` (P2 phasing line)
**Severity:** nit
**Resolution (accepted):** reverted the inline edit to the original phasing line; the override reference now
lives solely in the dated Update (2026-07-22) section.

### Unused constants remain

**Location:** `scripts/check-ci-runners.py`
**Severity:** nit
**Resolution (accepted):** removed `REPO` (+ the now-unused `pathlib` import) and `REGISTRY_URL`; added a
note that the probe hardcodes the URLs inline (can't `.format()` around the `{{.Server.Version}}` braces).

## Diff stat

```text
 CLAUDE.md                                          |   4 +-
 README.md                                          |   2 +-
 docs/decisions/0013-ci-self-hosted-runners.md      |   6 +-
 docs/decisions/0019-agentforge-v2-control-plane.md |  19 +-
 docs/runbooks/ci-runners.md                        |  51 ++++
 justfile                                           |   7 +
 kubernetes/infra/runners/variables.tf              |   8 +-
 scripts/check-ci-runners.py                        | 323 +++++++++++++++++++++
 scripts/tests/test_check_ci_runners.py             | 180 ++++++++++++
 9 files changed, 590 insertions(+), 10 deletions(-)
```
