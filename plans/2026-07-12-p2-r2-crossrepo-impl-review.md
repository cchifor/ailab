# Implementation review — agentforge v2 P2 R-2 cross-repo coordination

<!-- codex-impl-review-status: pending -->

Cross-repo tranche: agentforge `feat/r2-capability-delivery` + agentforge-platform
`feat/r2-crossrepo` (the ailab VAP/PV changes ride `feat/p2-unlock`). Round 1 raised 5
findings; round 2 CLOSED 2 (pool signing, per-engine routes/clamp-safe quota) and left 3.
This file records the round-2 survivors and the round-3 responses (opus fixes / pushback).

## Round-2 survivors + round-3 responses

### [CLOSED r2] Platform silently mints every deployed pool as `default`
Fixed round 2 (`AF_SANDBOX_POOL` rendered + signed + cross-repo asserted).

### [CLOSED r2] Capability route and quota defaults — per-engine route + clamp-safe quota
Fixed round 2 (per-engine `_ENGINE_CAPABILITY_ROUTES`; broker `verify_against_policy` clamps).

### [FIXED r3] Capability never attached to CLI requests + `is_broker_url` empty authority
**Location:** agentforge `deploy/sandbox.Dockerfile`, `src/agentforge/adapters/exec/broker_launch.py`, `.../sandbox.py`; platform `renderer.py:66`
Round-2 finding: the delivery minted+dropped the JWT but no wrapper made the stock claude/
codex CLIs read it and attach `Authorization: Bearer`; also `http://` (empty authority)
passed `is_broker_url`.
Round-3 fix (agentforge `b76fd3c..e5d3dd8`):
- `deploy/af-broker-key`: a request-time key helper baked read-only into the sandbox image;
  emits the capability read FRESH from the reserved file each call (never argv / a credential
  env var); fails closed if the file is absent/empty.
- `broker_launch.py`: per-engine consumer wiring as DATA (plain-value env only) — claude
  `ANTHROPIC_BASE_URL` + `CLAUDE_CONFIG_DIR` + `apiKeyHelper=af-broker-key`; codex `CODEX_HOME`
  + `config.toml base_url`. Takes only `(engine, url)` — no capability param — so a staged
  config structurally cannot bake the bearer.
- `sandbox.py`: `_agent_env_allowlist` merges `agent_broker_env`; the run stages the config
  under the reserved `.af/` (`write_agent_config`, O_NOFOLLOW+O_EXCL+0600, import-skipped),
  located via a plain-value path env (the VAP forbids `valueFrom`/`envFrom`).
- Tests: helper reads-fresh + fails-closed; wiring never carries a bearer in env/argv; run
  stages after mint / before Job; and an END-TO-END recording-broker test proves the helper's
  output is accepted as a bearer by the REAL broker app for `/v1/messages` and `/v1/responses`.
Round-3 fix (platform `ccda672`): `is_broker_url` now uses `urlsplit` + requires a non-empty
hostname, so `http://`, `https://`, `http:///v1` are all rejected (unit-tested).
<!-- opus-pushback: The REAL claude/codex-CLI recording-broker test cannot run in the unit
tier — it needs the built sandbox image + the real CLIs + a live broker on a Kata node (no
egress/CLIs/image here). That is preflight-gated by construction (your own "preflight-confirmed
per-engine wrapper" phrasing + plan §preflight -> real-Kata CLI discovery). It is landed as a
skipped @pytest.mark.integration scaffold gated on AF_SANDBOX_IMAGE, run by the preflight
harness before the v1.1 flip. The unit tier proves the mechanism end-to-end against the real
broker via the actual helper; the residual is the real-CLI acceptance, which is the preflight
gate, not a unit-tier gap. -->

### [FIXED r3] Org canonicalization still accepts a trailing hyphen
**Location:** platform `renderer.py:108` (`_CANONICAL_SLUG`)
Round-2 finding: the leading-anchor-only regex accepted `acme-`, a valid slug prefix but an
invalid k8s label value that fails at Job-create instead of at ingestion.
Round-3 fix (platform `ccda672`): `_CANONICAL_SLUG = [a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?` — a
full DNS label, both ends alphanumeric, 1-63. Rejected now at BOTH `PoolSpec` construction and
OIDC `parse_groups` (shared `is_canonical_slug`); trailing/leading/double-hyphen tests added at
both call sites.

### [FIXED r3] Capability can escape through result-schema error messages
**Location:** agentforge `sandbox.py:1254`, `import_validator.py:716` (`parse_result`)
Round-2 finding: `.af-result.json` is import-skipped so `contains_bytes` never scanned it, and
`parse_result` interpolated Pydantic's ValidationError (hostile field name/value) into
`ImportRejected` -> orchestrator escalation text.
Round-3 fix (agentforge `b7c2e3e`):
- `sandbox.py`: scan the BOUNDED raw result bytes for the exact capability BEFORE parsing;
  reject fail-closed with a constant `CapabilityLeakError` (no capability in the message);
  `apply_back` never runs. Transformed copies remain the documented residual (TTL/session-close
  /source-IP).
- `parse_result`: raise CONSTANT, hostile-free messages and break the exception chain
  (`from None`) so neither the message nor a downstream `format_exc`/`__cause__` render can
  carry a smuggled field name/value outward. Regression asserts the exact token is absent from
  the raised error (str + repr) and that `__cause__` is None.

## Diff ranges for round 3
- agentforge `feat/r2-capability-delivery`: `b76fd3c..e5d3dd8` (result-leak + CLI-consumer wiring)
- agentforge-platform `feat/r2-crossrepo`: `050b2ff..ccda672` (is_broker_url + org DNS-label regex)

## Gates
- agentforge: full `uv run pytest` green; `ruff check src tests` clean; `mypy` clean on changed modules.
- platform: `uv run pytest` renderer/auth/cross-repo suites green; `ruff` clean.
