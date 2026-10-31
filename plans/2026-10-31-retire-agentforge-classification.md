# AgentForge retirement: file classification (gitea/main 9ae3bf84, 2026-10-31)

Read-only classification of every reference outside `plans/`, used by `plans/2026-10-31-retire-agentforge-plan.md`.

Abbreviations: `K/` = `kubernetes/apps/`, `I/` = `kubernetes/apps/infrastructure/`, `C/` = `kubernetes/apps/clusters/ai/`.

## A. Delete (AgentForge-only)

**Directories:**
- `K/apps/agentforge/`
- `K/agentforge-tenants-bootstrap/`
- `I/agentforge-{broker,ci-runners,codex-refresh,pkgcache,sandbox,tenant-platform-dev,workers}/`
- `I/autoscaling/keda/`

**Flux files:**
- `I/sources/keda.yaml`
- `C/agentforge-{broker,ci-runners,codex-refresh,pkgcache,sandbox,tenant-platform-dev,tenants,tenants-source,tenants-git-auth,workers}.yaml`
- `C/keda.yaml`
- Not `C/agentforge-runtimeclasses.yaml`.

**Monitoring:**
- `I/monitoring/agentforge.yaml` (dormant)
- `agentforge-rules.yaml`, `agentforge-coverage-rules.yaml`
- `agentforge-{conductor,platform,provisioner}-rules.yaml` and their `.test.yaml` files

**OpenBao, phase 2:**
- `I/security/openbao/provisioner-deploy.yaml`, `provisioner-podmonitor.yaml`, `capability-kids-configmap.yaml`
- the provisioner block in `unseal-rbac.yaml` (159-188)

**Databases:** `K/databases/infra-pg-afp-admin.sops.yaml` and `infra-pg-afp-app.sops.yaml`.

**Workflows:** `.gitea/workflows/tenant-guard-cel.yaml`, and `broker-inventory.yaml` once its unit-test step has moved.

**Scripts:**
- `af-cp-smoke.sh`, `af-db.sh`, `af-revoke-legacy-kid.sh`, `af_revoke_legacy_kid.py`
- `check-seat-guard-cel.py`, `check-tenant-guard-cel.py`
- `gen-broker-inventory.py`, `verify-sandbox-boundary.sh`
- optionally `verify-image-digest.sh`

**Script tests:**
- `_brokerseat_support.py`
- `test_af_revoke_legacy_kid.py`
- `test_broker_refresh_intervals.py`, `test_broker_servicemonitor.py`
- `test_brokerseat_{admission,crd,rbac}.py`
- `test_cp_env.py`, `test_gen_broker_inventory.py`, `test_verify_sandbox_boundary.py`
- `test_provisioner_podmonitor.py` (phase 2)

**dev_worker role:** `tasks/agentforge.yml`, `files/agentforge-update.sh` and `templates/agentforge{.env,.service,-update.service,-update.timer,-memory-cap.conf}.j2`.

**Runbooks:**
- `agentforge.md`, `agentforge-platform-activation.md`, `agentforge-v3-cutover.md`, `sandbox-boundary-canary.md`
- `agentforge-activation.md`, after moving its OpenBao disaster notes (197-202) into `openbao-recovery.md`.

## B. Edit (shared files)

**Kustomizations:**
- `K/apps/kustomization.yaml:12`
- `I/monitoring/kustomization.yaml` 22-29 and 58-62
- `I/sources/kustomization.yaml:14`
- `I/security/openbao/kustomization.yaml:20-22`

**OpenBao:** `devworker-provision-job.yaml:127-142` (codex-hosts-publisher).

**infra-pg:**
- `K/databases/infra-pg.yaml` 159-187 (postInitSQL) and 282-310 (managed.roles)
- `K/databases/kustomization.yaml:23-26`

**Gitea admin token `af-ci-scaler-2941`:** `gitea-credential-inventory.sql:50`, `infra-pg-gitea-credential-queries.yaml:45`, and `gitea-credential-rules.yaml:86` with its `.test.yaml:108`.

**Auth, status and edge:**
- `K/apps/auth/configuration.yml` 188-193 and 310-336
- `K/apps/gatus/configmap.yaml:498-503`
- `K/apps/homepage/configmap.yaml` 17 and 87-94
- `K/apps/edge/cloudflared.yaml:143-148`
- the `kubernetes/edge/hostnames.yaml:194-201` entry, then `just edge-render` (Caddyfile, `dns-records.json`), then `gateway.py` (`gateway-state.yaml:15`)
- `kubernetes/infra/cloudflare/moved.tf:86-87`
- `test_edge_render.py` `DROPPED`

**Monitoring:** `I/monitoring/platform-rules.yaml:89` (drop `keda` from the regex) and its test at line 37.

**CI scripts and tests:**
- `scripts/check-inline-hashes.py` 98-170 and 276-277 (docstring items 1 and 4); `manifests.yaml:65` step name
- `scripts/tests/test_manifest_paths.py` 56-64, 76, 117, 277, 280
- `scripts/manifest-paths.py:128` (`EXPECTED_EXTERNAL_SOURCES`) and the fixtures at 461-500

**justfile:** delete af-verify-tenant-guard, af-verify-brokers, af-gen-brokers, af-record-seed-coverage, af-db-init, af-db-migrate, af-cp-smoke, and optionally pin-verify. Keep af-verify-hashes, af-verify-litellm, af-gen-litellm, pin-bootstrap and openbao-status.

**dev_worker role:**
- `tasks/main.yml:216-218`, `handlers/main.yml:31-38`, `tasks/firewall.yml:20-34`
- `defaults/main.yml` 285-298 and 348-356
- `group_vars/dev_workers.yml:12`
- the `secrets/dev-worker.sops.yaml.example` keys at 33-44

**After the repos are archived:**
- `pr_reviewer/defaults/main.yml` 30, 32, 91, 292, with `test_reviewbot.py` 2676-2677 and 2812
- `ci-rerun-watchdog` `deployment.yaml:56` and `app.py:58-59`
- `scripts/ci-queue-stats.py:28`

**Docs:**
- `openbao-recovery.md` 98-101
- `infra-pg.md:178`, `owner-credentials.md:194`, `s2s-identity.md:657`
- `network-plan.md` 176-178
- `CLAUDE.md` 30 and 64
- `README.md:161`

## C. Keep
- **OpenBao bootstrap:** the init, unsealer and provision Jobs and Deployment; `operator-seeds`; `unseal-rbac` blocks 1-3; the canary.
- **`agentforge-runtimeclasses`:** used by Workbricks.
- **litellm-local.**
- **The agent nodes,** their tolerations and their schematics.
- **dsh:** its conductor and `dsh_validator_pin`.
- **The Renovate `agentforge/**` rule and the zot retention policy.**
- **Generic tooling:** `pin-image-digests.py`, `check-ci-runners.py`.
- **`part-of: agentforge` labels on shared objects.**
- **The Gitea webhook host allowlist.**
- **openbao_agent `codex-auth.ctmpl`** (dormant).
- **Historical ADR prose.**

## D. Ordering and risk
1. Never let OpenBao's orchestrator image become unavailable.
2. The provisioner must outlive what it holds finalizers on: the tenant namespaces and BrokerSeats.
3. Delete the tenants Kustomization before tenants-git-auth (it needs the impersonated SA for GC).
4. Remove KEDA last: its CRDs uninstall with it, and a dangling external-metrics APIService hangs namespace deletion.
5. RuntimeClasses: never prune and recreate them in one step.
6. Flux deletes children concurrently, so separate PRs at every ordering point. The GitHub mirror must equal Gitea.
7. CI:
   - move the `scripts/tests` step first;
   - delete each test with what it tests;
   - `check-inline-hashes`, `test_manifest_paths` and `test_edge_render` change in step;
   - the platform-rules fixture changes with its regex;
   - each rule file leaves with its promtool test.
8. infra-pg: removing a managed role does not drop it, and Flux never drops a database. Order: dump, DROP, `ensure: absent`.
9. Gitea admin token: delete the token before the allowlist edit, or `GiteaAdminTokenNotAllowlisted` fires.
10. Retain storage: the NFS af-sbx-* PVs and the npm-cache iSCSI PV keep their data when deleted. The plan resolves this: a cleanup Job erases the NFS data, and the npm-cache PV is flipped to `Delete`, before PR-2d removes them.
11. Live OpenBao state: seeds come first (create-if-absent on every provision run); the operator roles are re-applied from image code.
12. Remove the repos from `pr_reviewer_repos` before archiving them.

## Outside dependents
- **OpenBao:** a hard dependency on the AgentForge image.
- **Workbricks:** the RuntimeClasses.
- **Dev workers and reviewer-2:** the codex-auth projection, which is dormant.
- Nothing else: llm-router, relay, dsh, platform, trueswarm, monitoring.
