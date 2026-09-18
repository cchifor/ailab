# Implementation review — dsh-k8s-full-access — round 1

<!-- codex-impl-review-status: finalized -->

## Summary

- Implementation adheres to the finalized plan exactly: RBAC switched to one ClusterRoleBinding to cluster-admin, admission guard deleted, kubectl installed via init container with proper checksum verification and degraded-boot fallback.
- Shell script is well-designed: POSIX-compliant, properly guarded without `set -e`, handles download retries with bounded timeouts, employs atomic rename for safe publish, and leaves no partial files on failure.
- Documentation is complete and consistent across five files (README, two ADRs, two runbooks, agents.seed.md) with all cross-references in place; comment corrections to network policies accurately reflect the new cluster-admin identity.
- No security, race, or edge-case issues detected; the degraded boot trade-off is explicit and acceptable.

## Findings

Codex (gpt-6-astra, `review` profile) raised no blocker, important or nit that asks for a change; every
section it returned confirms a plan item as implemented (RBAC shape, atomic publish and the
remove-before-download invariant, the 400 s retry bound, `sh -n` coverage via
`test_dsh_embedded_shell.py`, POSIX compliance, the `KUBERNETES_*`-survives-the-scrub claim, ADR 0025,
the degraded-boot fallback, the network-policy comment corrections, the `Init:0/4` count, the README row).
No `<!-- codex: ... -->` markers to address.

## Diff stat

```
 README.md                                          |   4 +-
 .../0021-agent-credential-plane-and-helm-testns.md |   2 +
 docs/decisions/0025-dsh-cluster-admin.md           |  55 +++++++++
 docs/runbooks/dsh-k8s-admin.md                     | 130 ++++++++++++++++++---
 docs/runbooks/dsh.md                               |  42 +++++--
 kubernetes/apps/apps/dsh/agents.seed.md            |  23 ++++
 kubernetes/apps/apps/dsh/deployment.yaml           |  92 ++++++++++++++-
 kubernetes/apps/apps/dsh/k8s-admin-admission.yaml  |  63 ----------
 kubernetes/apps/apps/dsh/k8s-admin.yaml            |  43 +++----
 kubernetes/apps/apps/dsh/kustomization.yaml        |   3 +-
 kubernetes/apps/apps/dsh/networkpolicy.yaml        |  25 ++--
 .../apps/apps/dsh/operator-ssh-networkpolicy.yaml  |   5 +
 kubernetes/apps/apps/dsh/searxng.yaml              |   8 +-
 13 files changed, 362 insertions(+), 133 deletions(-)
```

## Adversarial panel (4 lenses × refute) — round 1, after codex

Codex returned no change requests; an independent four-lens panel (shell script, k8s/GitOps, docs
truth, plan drift; every finding attacked by a skeptic against the worktree and the live cluster)
confirmed 12 and refuted 1. All 12 fixed in the follow-up commit:

- `degrade()` did not remove the destination, so the arch-mismatch and chmod-failure branches left
  a stale file on PATH behind an exit 0 while the comment claimed otherwise — fixed, re-tested live.
- The cache-hit log echoed `KUBECTL_VERSION`, which that branch never verifies — it now names what
  was verified and prints the real `version --client`.
- 400 s worst case misread `--max-time` (it bounds the whole transfer): 370 s stalled, ~40 s unreachable.
- Two "SearXNG is dsh's ONLY route to the web" comments (kustomization.yaml, networkpolicy.yaml)
  contradicted the history note this branch adds; README Decisions row said ADRs end at 0023;
  dsh.md said web_fetch is off and the image is node:22; ADR 0025 said "three PRs" then listed five;
  the runbook overstated that no agentforge guard can match this identity.
- Refuted: "pre-merge step 4 not evidenced" — the in-container run happened before the commit and
  is in the PR body.
