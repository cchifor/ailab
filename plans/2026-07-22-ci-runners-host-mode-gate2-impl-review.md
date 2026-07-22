# Implementation review — ci-runners-host-mode-gate2 — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- The explicit implementation range `f813409..538d45c` largely follows the planned architecture: read-only SSH, the required host checks, default-on API validation, fail-closed host evaluation, and a remote `.runner` parser that emits only `label` and `address`.
- Several blockers and important issues remain: the API transport cannot guarantee that `GITEA_TOKEN` stays out of redirects/errors, redirect status codes (301/302) deviate from plan policy, API pagination is incomplete, and duplicate probe/runner names lack fail-closed handling.
- The default five-runner path is sound, but positional IP overrides can reduce the API gate to a subset—or even a vacuous `0/0` success when an unknown IP is passed.
- The written tests cover most pure evaluation cases, but several mandatory transport, malformed-input, exit-code, and secret-redaction test cases are absent.
- D2 (decision record) is substantially complete, and D3 (IP corrections in runner-specific docs) is done, but several current operational/ADR documents still assign agent-nodes to `.14–.16` or describe `.47–.49` as free.

## Findings

### API transport cannot guarantee token containment

**Location:** `scripts/check-ci-runners.py:228–244`, `scripts/check-ci-runners.py:299–302`  
**Severity:** blocker

`urllib.request.urlopen` follows redirects by default, potentially forwarding the explicit `Authorization` header. Exceptions outside the three handled classes (HTTPError, URLError, JSONDecodeError) are printed verbatim by `main()`, which can leak token-bearing header values in error messages. Reject redirects via an explicit redirect handler, validate the token format, convert every exception inside `query_gitea_runners()` to a fixed sanitized error message, and add test cases with a unique token sentinel to ensure no error path leaks it.

### Redirect status codes deviate from the finalized policy

**Location:** `scripts/check-ci-runners.py:52–53`, `scripts/tests/test_check_ci_runners.py:99–101`  
**Severity:** important

The implementation and test deliberately accept `301` and `302` in `GITEA_REACHABLE`, but the finalized plan permits only `{200, 401, 403}`—a redirect therefore incorrectly passes the Gitea egress check and violates fail-closed semantics. Remove `301` and `302` from `GITEA_REACHABLE`, update the test assertion to match, and verify no other HTTP codes slip in.

### Identical duplicate probe keys are accepted

**Location:** `scripts/check-ci-runners.py:103–117`  
**Severity:** important

The parser marks a duplicate only when its value differs (`fields[k] != v`), contrary to the requirement that any repeated required key fail closed. A key=value line appearing twice with the same value incorrectly passes. Change the condition to mark every repeated key as `DUP` regardless of value, and add explicit unit tests for both identical- and differing-duplicate cases.

### Host IP overrides can make the API check vacuous

**Location:** `scripts/check-ci-runners.py:258–267`, `scripts/check-ci-runners.py:305–311`  
**Severity:** important

Passing a known IP on the command line correctly limits the host check to that runner; but passing an unknown IP produces an empty `expected_names` set and reports `0/0 expected runners online` as OK. The API expectation should either remain fixed at `ci-runner-1..5` regardless of positional args, or the script should reject unknown IPs and require explicit runner-name mappings. Define the policy and enforce it; consider whether positional IP-only overrides should be supported for the API check at all.

### API pagination and duplicate names are not fail-closed

**Location:** `scripts/check-ci-runners.py:178–196`, `scripts/check-ci-runners.py:228–244`  
**Severity:** important

The API fetch retrieves only one page despite the documented `total_count` field in the schema, and duplicate runner names silently overwrite earlier entries in the `by_name` dict. Enough stale registrations can hide expected runners, and duplicate online/offline records can pass depending on iteration order. Implement pagination (fetch until the runners array is empty or `len(runners) < page_size`), reject duplicate names as a schema failure, and enforce strict type checking on the `name` and `status` fields.

### Mandatory test cases are missing

**Location:** `scripts/tests/test_check_ci_runners.py:1–176`  
**Severity:** important

The written tests cover core pure-logic evaluations (parse, evaluate_host, evaluate_api) but omit: (1) invalid `.runner` JSON on the remote (the remote python3 parser failing), (2) actual SSH nonzero command exit codes and stderr output, (3) missing `GITEA_TOKEN` env var, (4) Gitea API 401/403 responses and malformed JSON, (5) unreachable host causing an exception in main, (6) SSH host-key configuration (AutoAddPolicy, no system known_hosts load), (7) meaningful token/key/`.runner` redaction in error paths. The test `test_failures_never_leak_raw_secrets` never supplies a secret sentinel or asserts its absence from failure strings. Add mocked transport-layer tests (paramiko/urllib mocks) and direct main() integration tests with a unique token string to ensure secrets are never printed.

### Current IP documentation remains contradictory

**Location:** `docs/network-plan.md:17,34`; `docs/decisions/0019-agentforge-v2-control-plane.md:155,216`; `docs/runbooks/agent-nodes.md:4`; `docs/runbooks/agentforge-activation.md:41`; `docs/runbooks/agentforge.md:132`  
**Severity:** important

D3 corrected the runner-specific files (CLAUDE.md, README.md, infra/runners/variables.tf, ADR 0013, ADR 0019 addendum, ci-runners.md §8), but several current operational documents and an ADR still call `.47–.49` free and place agent-nodes at `.14–.16`. Search and assign agent-nodes to `.47/.48/.49` throughout docs, mark `.47–.49` as occupied, and leave only clearly annotated historical references (e.g., "originally .47–.49") unchanged.

### Original ADR phasing text was rewritten

**Location:** `docs/decisions/0019-agentforge-v2-control-plane.md:237`  
**Severity:** nit

The addendum is present and correct, but the original P2 phasing text (describing "k8s-native CI runners") was also edited to include a reference to the override, contrary to the explicit "addendum, not rewrite" requirement. Restore the original phasing line as-is and keep the override reference exclusively in the dated Update section below it.

### Unused constants remain

**Location:** `scripts/check-ci-runners.py:34, 49`  
**Severity:** nit

`REPO` (line 34) and `REGISTRY_URL` (line 49) are unused; the registry URL is duplicated inline in the remote probe (line 80). Remove the dead definitions or centralize probe construction so the documented and probed endpoint cannot drift. This is low-risk but reduces code debt.

## Diff stat

Verbatim output from `git diff f813409..HEAD --stat`:

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
