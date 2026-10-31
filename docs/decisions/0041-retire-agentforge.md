# ADR 0041 — Retire AgentForge (the control plane, brokers, sandbox, workers and KEDA)

**Status:** ACCEPTED (2026-10-31), owner-directed: *"Remove the AgentForge and all the dependent
deployments"*, then *"Address all the issues yourself. I approve!"* Execution plan, gates and the
Codex review trail: `plans/2026-10-31-retire-agentforge-plan.md`, with the delete/edit/keep
classification in `plans/2026-10-31-retire-agentforge-classification.md`.
**Supersedes:** ADR 0018 (AgentForge v1, autonomous dev agents) and ADR 0019 (AgentForge v2 control
plane). Their text is kept as the historical record; they no longer describe anything that runs.
**Follows:** the playground retirement the day before (`plans/2026-10-30-retire-playground-plan.md`,
#1505).

## Context

AgentForge was the estate's label-driven multi-agent development platform: a control plane
(`agentforge-platform`), five credential-injecting LLM brokers, a provisioner, a reaper, an ephemeral
Kata sandbox, a conductor and CI runners scaled by KEDA. On 2026-10-31 only one CP-rendered tenant was
left, `tenant-zero/platform-dev`, and it had created no sandbox Job since 2026-08-17. Its pool ran on
images Zot had already garbage-collected (#1519). The conductor was parked, codex-refresh suspended
since 10-11 (its login was revoked 09-29), the CI-runner ScaledJob was never listed, and KEDA held zero
ScaledObjects or ScaledJobs. No dev-worker had an AgentForge unit installed. The agents' work had moved
to the dev-workers and the two reviewer bots.

## Decision

Remove everything that exists only for AgentForge, in verified, separately reviewed steps:
- **Workloads and Flux units:** the CP (`apps/agentforge/`, the tenants bootstrap and git auth), the
  brokers, codex-refresh, the provisioner, the sandbox (namespace, VAPs, reaper, canaries, NFS PVs), the
  workers (the conductor), pkgcache (`npm-cache`), the CI runners, and then KEDA with its CRDs.
- **Data:** both infra-pg databases (`agentforge_platform`, `agentforge_broker`) and their five roles.
  They were dumped and restore-verified first. The disposable NFS scratch tree and npm-cache's LUN
  were erased.
- **Identity:**
  - every token of the twelve AgentForge Gitea bots, plus `gitea_admin`'s `af-ci-scaler-2941`;
  - the tenants deploy key and the four webhooks that pointed at the CP and dispatcher;
  - the Authelia `agentforge` OIDC client and the `af:tenant-zero:owner` group;
  - the edge hostname;
  - the AgentForge OpenBao KV leaves, and every path in `operator-seeds` (now `{}`).
- **Repos:** `agentforge`, `agentforge-platform`, `agentforge-tenants`, `agentforge-config`,
  `agentforge-playground` and `primes-lab` are archived in Gitea (read-only; an owner can unarchive them), after the
  reviewbot allowlists dropped them (#1533).
- **Tooling:** the AgentForge-only CI checks, generators, recipes, alerts, dashboards and runbooks.

**Kept, because something outside AgentForge needs it.** These are listed explicitly; never select
objects by the `app.kubernetes.io/part-of: agentforge` label, which shared objects also carry. The
object-level checklist (OpenBao TLS, the `openbao-keys` / `openbao-operator-provisioner-token` Secrets,
`openbao-state`, `operator-seeds`, `unseal-rbac` blocks 1–3, the canary, and the non-AgentForge
provisioning Jobs) is section C of `plans/2026-10-31-retire-agentforge-classification.md`.
- **OpenBao's bootstrap still runs AgentForge code.** `openbao-init`, `openbao-unsealer` and
  `openbao-provision` use `agentforge/orchestrator@sha256:ed8c94fa…`, also mirrored as
  `registry.chifor.me/infra/openbao-bootstrap:orchestrator-f76c9ce`. The image, its Zot retention rule
  and the Renovate disable rule stay. The `af` KV mount stays too: estate, dev-worker, litellm and
  dsh paths live there.
- **`agentforge-runtimeclasses`** (`kata`, `gvisor`): Workbricks uses them.
- **The three Talos agent nodes** (.47–.49): llm-router, llm-router-edge and relay run there.
- **litellm-local:** Strive and the dev-worker diag key use it.
- **dsh's conductor** (`dsh-team-conductor`), which is not AgentForge's.
- **Generic tooling with an `af-` name:** `af-verify-litellm`, `af-gen-litellm`, `af-verify-hashes`,
  `pin-image-digests.py` and `check-inline-hashes.py`'s LiteLLM sites.

**Retired-credential guard.** The retained bootstrap re-mints the provisioner's scoped token on the
root path of a first bootstrap (after a vault wipe). `security/openbao/retired-token-revoker.yaml`
revokes and deletes it every 15 minutes, and `OpenBaoRetiredProvisionerTokenPresent` is the
backstop (`docs/runbooks/openbao-recovery.md` § "After a re-bootstrap"). Both go once OpenBao no
longer runs the AgentForge image (#1536).

## Consequences

- **Freed:** the sandbox's NFS tree, a 10 GiB LUN, two databases, KEDA's controllers and their
  external-metrics APIService, and more than a dozen Flux units, each of which was a dependency for
  upgrades and alerts.
- **The names stay misleading:** `agentforge-runtimeclasses`, the `agent-nodes` module and the `af`
  mount keep their names. Renaming any of them is churn on live objects; the RuntimeClasses rename is
  #1538.
- **Dormant leftovers, deliberately deferred.** They are inert because they bind identities in deleted
  namespaces or are rendered by no host:
  - the operator k8s-auth roles that `openbao-provision` re-applies from image code, which go with
    moving OpenBao off the AgentForge image (#1536). The same reconcile also re-stamps `cas_required` on
    four broker `oauth` paths, which leaves empty metadata stubs (no versions, no data) after every
    provision run;
  - `CODEX_AUTH_GRANT` and the reviewer AppRoles in `devworker-provision-job.yaml`, plus the
    `codex-hosts-publisher` role bound to the deleted `agentforge-broker/af-codex-refresher` (#1537);
  - the empty NFS export root on the QNAP (#1539).
- **Rollback** has three kinds of part:
  - **Reversible by revert:** the manifests, one PR at a time (PR-0 to PR-4). A recreated Retain PV needs
    its `claimRef` UID cleared before it rebinds.
  - **Restorable from the kept dump:** the two databases, from
    `postgres-dumps:/dumps/retired/agentforge-2026-10-31/`, with `SHA256SUMS` and per-table row counts
    alongside. The procedure is `docs/runbooks/infra-pg.md` § "Retired: the AgentForge databases": recreate
    the roles, create the databases with their owners, then `pg_restore` and compare the counts.
  - **Reissued, never restored:** the revoked Gitea tokens and deploy key, and the destroyed KV documents.
    Their values are gone by design, so they are minted afresh; the KV layout and seed rules are in
    `docs/runbooks/openbao-recovery.md`. An owner can unarchive the repos.
