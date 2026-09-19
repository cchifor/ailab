# Implementation review — second-codex-subscription — round 1

<!-- codex-impl-review-status: finalized -->

## Findings

### Freshness fields defeat the unchanged-state comparison
**Location:** `ansible/roles/dsh_codex_publisher/files/publish.py:118`
**Severity:** important
### Failed rotations advertise the unpublished token's expiry
**Location:** `ansible/roles/dsh_codex_publisher/files/publish.py:174`
**Severity:** important
### Device-flow regression test never reads production ESO template
**Location:** `scripts/tests/integration/test_litellm_chatgpt_route_contract.py:110`
**Severity:** important
### Root metrics writer follows a predictable temporary pathname
**Location:** `ansible/roles/dsh_codex_publisher/files/publish.py:97`
**Severity:** important
### ADR incorrectly promises expiry alerts before activation
**Location:** `docs/decisions/0026-second-chatgpt-subscription-through-litellm.md:198`
**Severity:** nit
### Remaining verification gaps

**Coverage not exercised:** CAS version conflicts, external vault version changes with unchanged credentials, soft-deleted/destroyed documents, state-file I/O failures, vault HTTP error on first projection followed by successful second, request parameters `prompt_cache_key` and `parallel_tool_calls` (documented loss, not exercised in tests).

**Positive review findings:**

| Area | Result |
|---|---|
| Publisher isolation | Document keys, CAS PATCH, per-projection login, exception handling match design; optional skip and failure paths work as specified. State/expiry defects above. |
| ESO template | V2 syntax correct; both `default "unconfigured"` expressions, `toJson` for JSON safety, numeric `4102444800` sentinel present. No rendering defect found given supplied engine facts. |
| LiteLLM manifest | Distinct route `gpt-6-astra-realjaynesage`, mode responses, both env vars, directory mount without subPath, optional:false, checksum check passes. |
| DSH provider config | Fields and reconcile invocation match; quoted `"off"` YAML syntax correct; gateway credential passed through. |
| Staging logic | `pr_reviewer_llm_seats_effective` expressions provision seats a,b,c,d; active list remains a,b,c; seat d projection stays optional. |
| Alert rules | Required/optional joins, thresholds (24h, 30m, 15m gates), heartbeat (15 min silent = down), ESO readiness expressions match plan. Fixtures cover positive/negative cases. |
| Route contract | Placeholder, expired JWT under sentinel, missing/empty/malformed credentials, 401/429 responses, three credential swaps through one Router all exercised. Production template coupling missing. |
| Dashboard and docs | Generated SEAT_NAMES entry verified; ADR 0026 covers device-flow hazard, single-process limit, parameter loss; recovery steps in openbao-recovery.md section PUBLISHER-OWNED; runbook procedures (operator ceremony, AppRole setup, login checks) documented. No unrelated scope expansion or dead code. |

**Security observations:**

- Textfile `0644` is appropriate: contains document names, email, status, timestamps—not tokens. Exposes metadata to local readers; `0600` would prevent node_exporter scrape.
- Secret mount uses default mode `0644`; processes with mount access can read the access token. This is not public Kubernetes Secret exposure; `readOnly` prevents container writes.
- `optional: false` blocks startup when Secret is absent, guarding against device-flow entry. No explicit `items` entry means it does not independently require auth.json key existence.
- No new Kubernetes API permissions granted to LiteLLM pod; `litellm-eso` is SecretStore identity, not pod ServiceAccount. Pod receives mounted token only, not publisher AppRole or refresh token.
- Publisher logs omit exception bodies and credential values; new tests use synthetic tokens. No usable secret found in committed code.

## Diff stat

```
.gitea/workflows/litellm-route-contract.yaml       |  16 +
 README.md                                          |   2 +-
 ansible/host_vars/reviewer-2.yml                   |  25 +-
 .../files/dsh-codex-publisher.service              |   6 +-
 ansible/roles/dsh_codex_publisher/files/policy.hcl |   4 -
 ansible/roles/dsh_codex_publisher/files/publish.py | 236 +++++++++---
 ansible/roles/dsh_codex_publisher/tasks/main.yml   |   7 +-
 ansible/roles/pr_reviewer/defaults/main.yml        |  18 +-
 ...-second-chatgpt-subscription-through-litellm.md | 238 ++++++++++++
 docs/runbooks/dev-workers.md                       |  94 +++--
 docs/runbooks/dsh.md                               | 176 +++++++--
 docs/runbooks/openbao-estate-credentials.md        |   5 +-
 docs/runbooks/openbao-recovery.md                  |  11 +-
 kubernetes/apps/apps/ai/kustomization.yaml         |   1 +
 kubernetes/apps/apps/ai/litellm-chatgpt-eso.yaml   | 109 ++++++
 kubernetes/apps/apps/ai/litellm.yaml               |  89 ++++-
 kubernetes/apps/apps/dsh/deployment.yaml           |   8 +
 kubernetes/apps/apps/dsh/openbao-eso.yaml          |   6 +-
 kubernetes/apps/apps/dsh/settings.seed.yaml        |  49 +++
 .../infrastructure/monitoring/ha-rules.test.yaml   |  46 +++
 .../apps/infrastructure/monitoring/ha-rules.yaml   |  31 ++
 .../monitoring/reporting-dashboard.yaml            |   2 +-
 .../monitoring/reviewbot-rules.test.yaml           | 198 ++++++++++
 .../infrastructure/monitoring/reviewbot-rules.yaml | 126 +++++++
 scripts/gen-reporting-dashboard.py                 |  12 +-
 .../test_litellm_chatgpt_route_contract.py         | 401 +++++++++++++++++++++
 scripts/tests/test_dsh_codex_publisher.py          | 273 ++++++++++++--
 27 files changed, 2011 insertions(+), 178 deletions(-)
```
