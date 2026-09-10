# Implementation review — retire-review-bot-fixture — round 1

<!-- codex-impl-review-status: pending -->

## Findings

No blocker findings.

### Escape repo labels before exporting metrics

**Location:** ansible/roles/pr_reviewer/files/reviewbot.py:1407  
**Severity:** important  
<!-- codex: A configured repo such as o/quote"x produces repo="o/quote"x", while backslashes and newlines also violate label escaping; this can cause node_exporter to reject the entire reviewbot textfile, although the input is administrator-controlled configuration rather than a demonstrated remote exploit. Escape backslash, double quote, and line feed according to the [Prometheus text format](https://prometheus.io/docs/instrumenting/exposition_formats/#text-format-details), apply the same handling to persona, and test the output with a real exposition parser because _exported() merely splits lines and accepts malformed labels. -->
<!-- opus: ADDRESSED in cfa59bd0 -->

### Atomicity tests never fail inside the publication transaction

**Location:** scripts/tests/test_reviewbot.py:1717  
**Severity:** important  
<!-- codex: These tests inject failures before commit_sweep() executes, and enqueue_exc fires on the first repo rather than after an earlier repo recovers; all 14 new tests still passed with database I/O redirected to memory and isolation_level=None, which permits partial publication. Add faults at a later gauge INSERT, the timestamp INSERT, and COMMIT, asserting error propagation and preservation of the complete previous snapshot, plus a second-repo enqueue failure after the first repo has recovered. -->
<!-- opus: ADDRESSED in cfa59bd0 -->

### Alert fixtures omit the claimed handoff and recovery transitions

**Location:** kubernetes/apps/infrastructure/monitoring/reviewbot-rules.test.yaml:475  
**Severity:** important  
<!-- codex: The fixture claiming to assert the handoff checks both alerts only at 60m, while the alleged “single blip” at :470 uses an uninterrupted 1x60 series; there is no recovery transition or explicit stale-alert silence assertion during sustained repo failure with an advancing timestamp. Assert both alerts before the handoff, inside the gap, and after the stale hold (for example 20m, 35m, and 46m), and add actual short-blip, firing-to-recovered, missing-to-present startup, and node-down suppression cases so regressions in gates and pending-state resets cannot pass unnoticed. -->
<!-- opus: ADDRESSED in cfa59bd0 -->

### Reconcile errors omit the promised PR and operation context

**Location:** ansible/roles/pr_reviewer/files/reviewbot.py:1646  
**Severity:** nit  
<!-- codex: The finalized plan requires the PR number and operation where known, but this handler emits only the repo and exception, making listing failures indistinguishable from marker-read failures and leaving malformed-PR errors without the identity of the PR blocking that repo’s sweep. Track the current operation and PR number, reset them at each repo boundary, and assert that a marker-read failure logs both. -->
<!-- opus: ADDRESSED in cfa59bd0 -->

### Admission tests do not protect the actual fixture removal

**Location:** scripts/tests/test_reviewbot.py:1781  
**Severity:** nit  
<!-- codex: These tests supply a synthetic repos=["o/kept"] configuration and exercise the pre-existing enqueue gate, so restoring cchifor/review-bot-fixture in the Ansible defaults would leave both tests green. Keep those behavioral checks and add a regression assertion against the actual defaults that the fixture is absent and the four intended repositories remain. -->
<!-- opus: ADDRESSED in cfa59bd0 -->

## Diff stat

```text
 ansible/roles/pr_reviewer/defaults/main.yml        |  12 +-
 ansible/roles/pr_reviewer/files/reviewbot.py       |  92 +++++++--
 .../monitoring/reviewbot-rules.test.yaml           | 185 +++++++++++++++++
 .../infrastructure/monitoring/reviewbot-rules.yaml |  68 +++++++
 scripts/tests/test_reviewbot.py                    | 221 +++++++++++++++++++++
 5 files changed, 560 insertions(+), 18 deletions(-)
```
