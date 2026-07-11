# Implementation review — agentforge-v2 — round 3

<!-- codex-impl-review-status: pending -->

## Summary

- R2-1 is resolved: empty/default-placeholder secrets are rejected in dev mode, and production requires at least 32 characters.
- R2-2 is resolved: DNS over UDP/TCP 53, the control-plane API on TCP 8080, and HTTPS on TCP 443 are permitted. Destination-specific tightening remains correctly deferred.
- R2-4 through R2-7 are resolved without a new blocker: ingest has no await in the check/apply/mark window, audit failure cannot replay the event, bootstrap uses its distinct credential with 501 when absent, commit paths are batch-preflighted, and both Codex gate paths use `AF_JOBS_ROOT`.
- R2-3 is only partially resolved: the stable identity is rendered and checked for explicit pools, but the replica limit remains configurable above one and an empty pool bypasses the required-worker check.
- No other new blocker or important regression was found in the reviewed changes.

## Findings

### P1 single-replica invariant is only a default
**Location:** src/agentforge_platform/settings.py:98
**Severity:** important
<!-- codex: `max_worker_replicas` is an unconstrained environment setting, so setting it above 1 makes `create_workspace()` render multiple replicas sharing the same stable worker/claim identity. Make the P1 limit non-configurable or validate it with an upper bound of 1, defensively reject `PoolSpec.max_replicas > 1`, and test an attempted environment override. -->

### Empty pool bypasses the required-worker policy
**Location:** src/agentforge_platform/api/workspaces.py:183
**Severity:** important
<!-- codex: Omitting `pool` leaves `required_worker=None`, so a valid config that does not declare the provisioned `{workspace}-{pool}` worker is accepted under the empty pool while the deployed worker subsequently fetches its named pool and receives no usable config. Require a non-empty, existing pool for P1 config writes and always bind that pool's rendered worker identity before persistence. -->

## Verdict

The P1 slice is not yet fully ALIGNED; enforce the single-replica invariant and require an existing pool on every P1 config write before proceeding.