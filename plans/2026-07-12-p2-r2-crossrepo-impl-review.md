# Implementation review — agentforge v2 P2 R-2 cross-repo coordination — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- Minting takes audience/model/budget from the typed ExecSpec, tenant/workspace/pool from SandboxConfig, requires an Ed25519 signing key, and bounds TTL by both the Job deadline and MAX_TTL_S; no hostile argv/repository value is parsed into a signed claim.
- Reserved `.af` delivery uses O_NOFOLLOW+O_EXCL+0600, pre-Job cleanup, and top-level import skipping. The Job/pod manifests carry the org label and exactly `{workspace, home}` for both profiles, matching the separate ailab VAP and the default `af-sbx-ws-<org>-<workspace>` name.
- Platform's org-qualified workspace/staging identities and `tenants/<org>/<workspace>/orchestrator` key are correct; `broker` is nested beneath the sole operator top-level prefix `operator`, so the reserved-set contents `{tenants, operator}` cover the current OpenBao layout.
- The tranche is not merge-ready because deployed jobs do not receive audience-specific broker routing or the real pool claim, capability policy defaults break non-Claude/low-ceiling configurations, org identifiers are not canonicalized, and a hostile job can copy the capability into imported output or captured logs.
- `git diff --check` is clean in both repositories. The managed read-only review environment rejected pytest/ruff/mypy execution, so the branches' claimed green gates were not independently rerun here; LocalExecutor and broker application sources are untouched by these diffs.

## Findings

### Broker routing is absent from platform output and cannot follow the signed audience

**Location:** agentforge/src/agentforge/adapters/exec/sandbox.py:825  
**Severity:** blocker  
<!-- codex: Every agent container receives the one global `cfg.broker_base_url`, while the signed `aud` varies as `<provider>/<account>`. The broker is explicitly one deployment with one expected audience, and the shared executor can run Anthropic plus OpenAI cross-review jobs, so one of those tokens will be sent to the wrong broker and rejected. In addition, platform renders no `AF_SANDBOX_BROKER_BASE_URL`, leaving the real Settings default empty and causing `_agent_env_allowlist` to fail before Job creation unless an undocumented secret field supplies it. Add a trusted provider/account-to-URL map (or an equivalent audience-keyed endpoint setting), select the URL with the same typed values used to form `aud`, render/provision it explicitly, fail before lease/staging when that exact mapping is absent, and test at least Anthropic and OpenAI accounts on one worker. -->

### Platform silently mints every deployed pool as `default`

**Location:** agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:463  
**Severity:** blocker  
<!-- codex: Agentforge added `AF_SANDBOX_POOL` and signs `SandboxConfig.pool`, but the platform env list emits only the org/workspace and never emits the trusted `PoolSpec.pool`. Thus planner, implementer, and reviewer deployments all mint `pool="default"`: a correctly narrow kid policy rejects them, while a policy that permits `default` loses the intended pool/role binding. Render `AF_SANDBOX_POOL=spec.pool`, include it in the real cross-repo Settings test, and assert the verified JWT carries the provisioned pool. -->

### Capability route and quota defaults are global, non-configurable, and not actually clamp-safe

**Location:** agentforge/src/agentforge/adapters/exec/sandbox.py:569  
**Severity:** important  
<!-- codex: The production composition never supplies these SandboxConfig fields, so every engine/account gets `/v1/messages`, POST, rate 60, concurrency 4. That route is Claude-specific and cannot authorize a Codex request. Also, the broker calls `verify_against_policy` before its later `min(...)` calls, so a requested default above an operator ceiling is rejected rather than clamped. Source route/method/rate/concurrency from a trusted per-engine or per-audience configuration, ensure requested values are within the operator record (or make the broker's documented clamp real), and add Codex plus lower-ceiling tests. -->

### The capability can be copied into imported output or ExecResult stdout

**Location:** agentforge/src/agentforge/adapters/exec/sandbox.py:1114  
**Severity:** important  
<!-- codex: Skipping the original `.af` path does not establish the stated no-result-leak property. The hostile process can read `/workspace/.af/broker-cap.jwt`, print it into pod logs, or copy it to an ordinary workspace file; `_safe_logs` is returned verbatim as `ExecResult.stdout`, and `import_tree` accepts the copied ordinary file. Retain the minted bytes long enough to reject/redact exact occurrences in captured logs and the bounded validated import before apply-back, add tests covering both `cat` to stdout and copying to another file, and explicitly document the residual for transformed exfiltration that session close/source-IP binding must contain. -->

### Reserved-prefix enforcement accepts non-slug org identifiers

**Location:** agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:165  
**Severity:** blocker  
<!-- codex: `PoolSpec.__post_init__` performs only an exact, case-sensitive set lookup. It accepts empty strings, whitespace, uppercase/case variants, Unicode, `/`, and dot segments; OIDC `parse_groups` also accepts any non-empty org text and can persist it. Those values violate the DNS-label/Vault-path assumptions on which PVC identity and reserved-prefix separation rely, and fail only later or produce a differently segmented key. Enforce the same canonical lowercase ASCII DNS-slug fullmatch used for workspaces at PoolSpec construction and org ingestion, reject reserved names after canonical validation, and test empty/whitespace/case/Unicode/slash/dot inputs as well as `tenants` and `operator`. -->

## Verdict

Not ready to merge agentforge + platform. The cryptographic mint and reserved-file mechanics are sound against a hostile repository, and the two-volume/org-qualified shape agrees with the separate ailab VAP/PV work, but the deployed coordination does not reliably route a capability to its audience, signs the wrong pool, has unusable provider/quota defaults, permits non-canonical org identities at the reserved-prefix boundary, and can return the bearer through imported output or captured stdout. These need fixes and green agentforge/platform regression gates before merge; ailab VAP/PV changes still land separately on `feat/p2-unlock`.