# AgentForge CP UX overhaul — status snapshot (2026-07-24)

Dated record; historical — supersede with a newer dated file. Detail lives in ADR 0019 (Update
2026-07-24), `docs/runbooks/{agentforge-platform-activation,openbao-recovery}.md`, and the
agentforge-platform repo docs.

## Shipped (all merged + deployed to agentforge.chifor.me)
- **Stage 0 companions** — agentforge #52 (OpenBao roles `af-cp-sub-status`/`af-cp-sub-rotator`,
  `cas_required`, broker `/readyz` `credential_generation`) + ailab #103 (refresher status
  custom-metadata, Refresh-now RBAC + VAP, infra-bot secret, broker repin). Live probe matrix
  passed positive+negative on the final vault.
- **agentforge-platform #10 (WS1)** — Tailwind v4/shadcn shell (Strive-pinned `0e5ff883`,
  radix-vue@1.9.17), sidebar nav, Overview (role cards, 1→5 lifecycle, entitlement-aware setup
  checklist), logout (Authelia redirect, 401-latch), same-origin CSRF middleware.
- **#11 (WS2, alembic 0003–0004)** — server-side template registry (subscription templates
  tenant-zero-only), multi-repo `workspace_repositories` (canonical repo identity, global
  one-active-owner), per-user Gitea PAT (Fernet key-ring, 5-op allowlisted adapter, user RLS),
  per-repo bot collaborator-grant enforcement, 5-step onboarding stepper.
- **#12 (WS3, alembic 0005)** — AG-UI chat wizard (pinned client 0.0.51, contract-tested SSE),
  deterministic wizard over application services, opaque single-use mutation `option_id`s,
  local-litellm free text with zero mutation ability.
- **#13 (WS4, alembic 0006)** — Settings → LLM subscriptions: generation-consistent status from
  OpenBao metadata (codex expiry live from JWT `exp`; claude last-rotated + operator note),
  CAS rotate, VAP-pinned refresh-now, PR-gated add/remove via `agentforge-infra-bot` (AGit) with
  the C8 async-operations state machine. Broker CNP fix #108 admits CP read-only `/readyz` probes.
- **Governance now live on ailab main**: branch protection (push whitelist + 1 approval,
  dismiss-stale), `agentforge-reviewer-bot` approvals, AGit PR-only infra bot.
- **OpenBao**: two wipe+re-bootstrap ceremonies (roles landing; then seal rotation after a brief
  escrow plaintext exposure — branch deleted, objects GC-purged, seal rotated, escrow re-encrypted
  Secret-shaped). Runbook: `docs/runbooks/openbao-recovery.md`.

## Verification at close
pytest 547 · vitest 127 · mock e2e 18 · real-integration e2e 23 (real OpenBao seeded with the
verbatim production policy split; pinned Gitea 1.26 with a genuinely read-only AGit bot) · live
server-side sweep green (all new endpoints auth-gated; CP reads all three broker generations).

## Open items
- Authenticated browser E2E walk (test-login is prod-disabled by design) — owner task.
- Tenant-zero playground lifecycle proof (issue → 5-completed) — pre-existing item, unchanged.
- Gitea OAuth (replacing per-user PATs) + standard AG-UI interrupts migration — documented follow-ups.
