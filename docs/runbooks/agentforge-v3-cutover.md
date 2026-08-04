# Runbook — AgentForge v3 coordinated cutover (schema v3, `AF_BOT_TOKENS`, ops-bot)

**One coordinated cutover window, not a gradual gate.** Worker main (`cchifor/agentforge`, ≥ `9da00b3`)
carries the full v3 program: `SUPPORTED_SCHEMA=3` **and** `MIN_SUPPORTED_SCHEMA=3` (v2 configs are
rejected with a migration pointer), the `AF_BOT_TOKENS` JSON map replacing the four `AF_BOT_TOKEN_*`
env vars **outright**, the `ops` principal (`ops-bot`), and planner workflow directives. The control
plane program (`cchifor/agentforge-platform` PR1/PR2 + webapp PRs) serves **v3 only** once pinned.
Program plan: `cchifor/agentforge` `plans/2026-08-03-user-workflows-program-plan.md` (§Rollout, PR6).

## Why the order matters — the 2026-08-04 dispatcher crashloop

On 2026-08-04 a v3-floor worker pin was merged **before** any of this runbook existed. The result was
a ~5.5 h fleet outage that is the exact failure this document sequences away:

- the new dispatcher image fetched the **v2** `agentforge.json` from the config repo → rejected by the
  `MIN_SUPPORTED_SCHEMA=3` floor → no usable config → **CrashLoopBackOff**;
- even with a v3 config it would have crashed next on the missing **`AF_BOT_TOKENS`**: the ESO-delivered
  Secret still carried only the retired `AF_BOT_TOKEN_PLANNER` key, and `_build_config_source` exits
  hard on an empty map (deliberately — a worker that started anyway would fail-closed on its first
  claim, the ~400-restart empty-token incident the hard exit exists to prevent).

The revert (`7c79358`) restored the pre-program fleet and stamped **PIN FREEZE** markers on
`kubernetes/apps/infrastructure/agentforge-workers/{worker,dispatcher}-deployment.yaml`. **The v3
cutover PR is the only sanctioned path those markers come off.**

## Credential/env delivery inventory (what carries the bot PATs today)

Every consumer, and where its forge credentials come from. "extract" = ESO `dataFrom.extract` — every
KV key of the OpenBao doc becomes a Secret key, so **the KV key name IS the env var name** and adding
`AF_BOT_TOKENS` to the doc needs **no manifest change** on those surfaces.

| Consumer | Manifest (ns) | Delivery | OpenBao path | Legacy v2 keys | v3 change |
|---|---|---|---|---|---|
| tenant-zero **playground** orchestrator (`af-orch-playground-planner`, `AF_CONFIG_SOURCE=gitea`) | `agentforge-workers/worker-deployment.yaml` + `worker-externalsecret.yaml` (`af-tenant-tenant-zero-playground`) | `envFrom` Secret `af-creds-playground-planner` ← ESO **extract** | `af/data/tenants/tenant-zero/playground/orchestrator` | `AF_BOT_TOKEN_{PLANNER,TESTER,IMPL,REVIEWER}` (+ HMAC, litellm, `AF_CAPABILITY_SIGNING_KEY`/`KID`) | vault doc gains one `AF_BOT_TOKENS` key (bootstrap mint, below); legacy keys become harmless-but-dead env vars (v3 never reads them) |
| tenant-zero **platform-dev** orchestrator (`af-orch-platform-dev-delivery`, CP-mode) | `agentforge-tenant-platform-dev/externalsecret.yaml` (+ CP-rendered Deployment in `cchifor/agentforge-tenants`) | `envFrom` Secret `af-creds-platform-dev-delivery` ← ESO **extract** | `af/data/tenants/tenant-zero/platform-dev/orchestrator` | same four + `AF_CONTROL_PLANE_TOKEN` | same as playground; **plus** its seeds fragment must be refreshed (see the seeds trap below) |
| **dispatcher** (`agentforge-dispatcher`, scale oracle) | `agentforge-workers/dispatcher-deployment.yaml` + `dispatcher-externalsecret.yaml` (`agentforge`) | `envFrom` Secret `agentforge-dispatcher-forge` ← ESO **explicit `data` mapping** | `af/data/operator/dispatcher/forge` | `AF_BOT_TOKEN_PLANNER` (READ-ONLY issues:read PAT) | **manifest change** (this PR): ES maps `AF_BOT_TOKENS` ← property `AF_BOT_TOKENS`; vault doc gains that key at cutover |
| **reaper** (`agentforge-reaper`) | `agentforge-sandbox/reaper-deployment.yaml` (`agentforge`) | — | — | **none** — `reaper()` builds no forge client and no config source | env unchanged; only its `p1-worker` digest moves with the fleet |
| **control plane** (`agentforge-platform`) | `apps/agentforge/deployment.yaml` (`agentforge`) | `secretKeyRef`s from SOPS Secrets (`agentforge-runtime`, `agentforge-db`, `agentforge-oauth`) + `envFrom agentforge-infra-bot` | — (SOPS, not OpenBao) | none of the `AF_BOT_TOKEN_*` family (CP bots are `AFP_*`) | image + `AFP_WORKER_IMAGE`/`AFP_SANDBOX_IMAGE` pins only |
| dev-worker **v1 hosts** (ADR 0018, DORMANT) | `ansible/roles/dev_worker/templates/agentforge.env.j2` | systemd EnvironmentFile from ansible SOPS secrets | — | all four `AF_BOT_TOKEN_*` | **no change** — `dev_worker_enable_agentforge: false` (defaults) and the group_vars enable line is commented out. If v1 is ever re-enabled on a v3 release, the template + `dev-worker.sops.yaml` keys must be migrated to `AF_BOT_TOKENS` first |

Durability layer (NOT a delivery path): `kubernetes/apps/infrastructure/security/openbao/`
`operator-seeds.sops.yaml` → `seeds.json`, applied by `_apply_operator_seeds` on **every** provision
Job run, **merge-writes with seed keys winning over the live vault**. Current relevant fragments:
`operator/dispatcher/forge` (`AF_BOT_TOKEN_PLANNER`) and `tenants/tenant-zero/platform-dev/orchestrator`
(four `AF_BOT_TOKEN_*` + `AF_CONTROL_PLANE_TOKEN`).

> **The seeds trap (why this PR does NOT pre-stage an `AF_BOT_TOKENS` placeholder in seeds.json):**
> seed keys are authoritative over the vault. A placeholder `AF_BOT_TOKENS` committed now would be
> merge-written **over** the real minted map on the next provision run and fail-close the fleet — the
> same end state as the 2026-08-04 incident, delivered by our own durability machinery. The seeds
> refresh therefore happens **at cutover, with the real values**, via `agentforge-bootstrap
> --seeds-out` + SOPS re-encrypt (step 4). Conversely, NOT refreshing seeds is also an outage, just a
> deferred one: today's platform-dev fragment holds only legacy keys, so a post-cutover OpenBao
> wipe + re-provision would restore names v3 never reads and no `AF_BOT_TOKENS` → worker fail-closed.
> The refresh is mandatory, not hygiene.

The ops-bot "provisioning surface" in this repo is exactly the above: there are **no per-bot ESO
entries** to add (delivery is the single `AF_BOT_TOKENS` key), no per-bot k8s Secrets, and the Gitea
user + PAT + collaborator grant are minted by `agentforge-bootstrap` (worker repo,
`docs/runbooks/orchestrator-credentials.md` — BOTS tuple includes `ops-bot`; scope table: ops-bot gets
`write:issue, read:repository, read:user, read:package`, **never** `write:repository`).

## The cutover PR (this PR) — what it stages

- `REPLACE_ME_V3_CUTOVER` placeholder digests (pin-script-compatible: `[A-Za-z0-9_]+`) on every
  fleet image ref that moves at cutover:
  `p1-worker` ×4 (worker, dispatcher, reaper, CP `AFP_WORKER_IMAGE`), `agentforge-platform` ×2
  (deployment + db-migrate), `sandbox` ×2 (worker `AF_SANDBOX_IMAGE`, CP `AFP_SANDBOX_IMAGE`).
  The real digests land on this branch via the normal pin flow (`just pin-workloads
  p1-worker=sha256:… sandbox=sha256:… agentforge-platform=sha256:…`) once the v3 release CI has
  built them — the script rewrites placeholder and real digests alike, and also aligns the
  sandbox egress-canary refs.
- dispatcher ExternalSecret: `AF_BOT_TOKENS` ← `operator/dispatcher/forge` property `AF_BOT_TOKENS`
  (replaces the `AF_BOT_TOKEN_PLANNER` mapping).
- PIN FREEZE markers kept in place, each annotated "removed by this PR at cutover merge".
- this runbook.

**Merging this PR with placeholders still in it is a fleet outage** (unpullable image refs). The
checklist below is ordered so that cannot happen.

## Pre-cutover (any time before the window)

1. **CP PRs merged + released.** Worker main is already v3; the CP program PRs (S1, PR1, PR2 at
   minimum — `afp-backfill-definitions` and the v3-only serve path live there) must be merged and the
   release CI must have produced the `agentforge-platform`, `p1-worker` and `sandbox` images to pin.
   Check the alembic delta (it WILL be non-empty — PR1 adds four tables):
   `git diff --name-only <old>..<new> -- alembic/versions/` in `agentforge-platform`.
2. **Board/issue inventory (drain).** On the board (agentforge.chifor.me) and the forge, list open
   issues in allowlisted repos that are mid-lifecycle (states 2–4 / claimed). Either let them drain
   to terminal states or note them — they will be re-planned under v3 (the fenced route-of-record
   does not survive a schema break). Pause new intake:
   ```bash
   kubectl --context admin@ai -n af-tenant-tenant-zero-playground annotate scaledobject \
     af-orch-playground-planner autoscaling.keda.sh/paused-replicas="0" --overwrite
   ```
   (Remove the annotation post-verify.)
3. **CP DB backup** (CNPG `infra-pg`, ns `databases`, db `agentforge_platform`). Resolve the primary
   fresh — never assume `infra-pg-1` — and dump via exec-redirect (NEVER `kubectl cp`; MSYS mangling
   has produced a 0-byte "backup" before):
   ```bash
   PRIMARY=$(kubectl --context admin@ai -n databases get cluster infra-pg \
     -o jsonpath='{.status.currentPrimary}')
   kubectl --context admin@ai -n databases exec "$PRIMARY" -c postgres -- \
     pg_dump -U postgres --clean --if-exists -d agentforge_platform \
     > agentforge_platform.pre-v3.$(date +%Y%m%d-%H%M%S).sql
   ```
   Verify: file is non-trivially sized AND ends with `PostgreSQL database dump complete`
   (`tail -1` — check content, not just exit code). Keep it until the soak (and the later
   `role_overlays` drop migration) completes.

## The window — ordered steps

### 4. Mint credentials (before any manifest merges)

Run `agentforge-bootstrap` (worker repo ≥ 9da00b3, full contract in its
`docs/runbooks/orchestrator-credentials.md` — exit codes, rotation semantics, CAS behavior) once per
tenant workspace. It creates/updates the five bot users **including `ops-bot`**, mints the five PATs
with per-principal scopes, validates every value, and writes them add-only as ONE `AF_BOT_TOKENS`
key into `af/data/tenants/<org>/<ws>/orchestrator`, preserving `AF_CAPABILITY_SIGNING_KEY`/`KID`
byte-for-byte:

```bash
# playground, then platform-dev (append --cp-bearer-file only if rotating the CP bearer)
agentforge-bootstrap \
  --url https://git.chifor.me --admin forge-admin --password-file <0600-file> \
  --org cchifor --repo agentforge-playground \
  --write-openbao \
  --tenant-org tenant-zero --tenant-workspace playground \
  --openbao-addr https://openbao.ailab.chifor.me \
  --openbao-token-file <openbao-operator-provisioner-token, from Secret \
    openbao-operator-provisioner-token in ns openbao> \
  --seeds-out /path/to/seeds-fragment.json     # MANDATORY choice on every run — see Rotation
```

- **Every run rotates** (old PATs are revoked on the forge). Re-run with the same args until exit 0.
- The seeds preflight **refuses to carry the retired `AF_BOT_TOKEN_*` names forward** — that is the
  designed forcing function for the platform-dev fragment refresh.
- **Rollback bridge (do not skip):** the mint revoked the PATs the legacy keys point at. So that a
  pin revert to v2 images stays a working rollback, patch the NEW PAT values under the LEGACY key
  names too (harmless-but-dead to v3; the bootstrap tool itself refuses retired names, so do it with
  the operator-provisioner token — stage the payload as a file in the pod, MSYS stdin dies silently):
  `bao kv patch -mount=af tenants/tenant-zero/<ws>/orchestrator AF_BOT_TOKEN_PLANNER=<planner-bot PAT>
  AF_BOT_TOKEN_TESTER=<tester-bot> AF_BOT_TOKEN_IMPL=<impl-bot> AF_BOT_TOKEN_REVIEWER=<reviewer-bot>`.
  Delete these keys post-soak.
- **Dispatcher doc** (bootstrap does not manage it): mint a fresh READ-ONLY `read:issue` PAT (owning
  bot user, e.g. `planner-bot` — the map key MUST be the username that owns the PAT), then:
  `bao kv patch -mount=af operator/dispatcher/forge AF_BOT_TOKENS='{"planner-bot":"<ro-PAT>"}'`
  (leave `AF_BOT_TOKEN_PLANNER` in place as the rollback bridge; delete post-soak).
- **Seeds re-encrypt:** merge the `--seeds-out` fragments plus the dispatcher `AF_BOT_TOKENS` into
  `operator-seeds.sops.yaml` and commit ON THIS PR BRANCH. Decrypt→edit→re-encrypt with
  `SOPS_AGE_KEY_FILE` pointing at `kubernetes/infra/_out/age.agekey` (in a worktree resolve the main
  checkout: `"$(cd "$(git rev-parse --git-common-dir)/.." && pwd -P)/kubernetes/infra/_out/age.agekey"`).
  **The tracked file must never hold plaintext** — verify ciphertext with `git diff` before staging,
  and `sops -d` round-trip after. Remove the legacy `AF_BOT_TOKEN_*` keys from the seeds fragments
  (the vault keeps its bridge copies; seeds must describe the v3 end state).
- Force-sync the ESO objects rather than waiting the 1 h refresh:
  ```bash
  kubectl --context admin@ai -n agentforge annotate externalsecret agentforge-dispatcher-forge \
    force-sync=$(date +%s) --overwrite
  kubectl --context admin@ai -n af-tenant-tenant-zero-playground annotate externalsecret \
    af-creds-playground-planner force-sync=$(date +%s) --overwrite
  kubectl --context admin@ai -n af-tenant-tenant-zero-platform-dev annotate externalsecret \
    af-creds-platform-dev-delivery force-sync=$(date +%s) --overwrite
  ```
  Verify each target Secret now carries an `AF_BOT_TOKENS` key (key NAMES only — never print values):
  `kubectl … get secret <name> -o jsonpath='{.data}' | python -c "import sys,json;print(sorted(json.load(sys.stdin)))"`.

### 5. Config repo to v3 (gitea-source consumers)

Bump `cchifor/agentforge-config` `agentforge.json` to schema v3 (shape per worker
`domain/config.py` @ the release commit): `schema_version: 3`, `stage_type` on **every** role
(the polymorphic implementer is dissolved — `ops`/`answerer` ship as roles), `bots` map covering all
five principals (`{"planner":"planner-bot","tester":"tester-bot","implementer":"impl-bot",
"reviewer":"reviewer-bot","ops":"ops-bot"}`), `workflows` as desired.

Ordering note: doing this BEFORE the image pins is safe by design — running v2 workers reject a v3
config as newer-than-supported and keep operating on `config-lastgood.json`; running v3 images
against a v2 config is the crashloop. Config first, images second, never the reverse.

### 6. Land the real pins on this branch, drop the freeze, merge

1. `just pin-workloads p1-worker=sha256:<…> sandbox=sha256:<…> agentforge-platform=sha256:<…>`
   (rewrites every placeholder in one reviewable diff; digests from the v3 release CI, tag→digest
   verified — `just pin-verify` / `crane digest`, fail closed if the tag moved).
2. **Remove the PIN FREEZE marker blocks** from `worker-deployment.yaml` and
   `dispatcher-deployment.yaml` — explicit checklist item; this PR is the only sanctioned removal
   path. Also drop this PR's "removed by this PR at cutover merge" annotation lines with them.
3. Re-validate: `kubectl kustomize` over `agentforge-workers/`, `agentforge-sandbox/`,
   `apps/agentforge/`; `sops -d` round-trip on the updated seeds file.
4. **Migrate the CP DB ahead of the rollout** (the alembic head moves — PR1's four tables). From
   THIS branch checkout: `AF_KUBE_CONTEXT=admin@ai bash scripts/af-db.sh migrate` (db-migrate.yaml
   is excluded from kustomization, so it applies from the checkout, not Flux). The OLD CP stays
   Ready under an ahead-of-expected head (`ahead_or_unknown`) — verified behavior; the NEW CP would
   wedge NotReady against an un-migrated DB, which is why migrate precedes merge.
5. Merge (squash) on Gitea → Flux applies: new CP + worker/dispatcher/reaper images,
   dispatcher ES mapping, seeds Secret.

### 7. Post-apply

1. **Backfill definitions** (one-shot, idempotent — UPSERT on `(workspace_id, name)`, content-hash
   skip, refuses to clobber post-cutover human edits) in the CP pod:
   ```bash
   kubectl --context admin@ai -n agentforge exec deploy/agentforge-platform -- \
     afp-backfill-definitions
   ```
   (Console-script name per CP PR1 — if it ships as a subcommand instead, it is
   `agentforge-platform backfill-definitions`; confirm against the merged PR before the window.)
2. **Re-render the CP-rendered pool** so platform-dev picks up the new `AFP_WORKER_IMAGE`: trigger
   the pool rollout from the CP (the rollout endpoint re-renders + commits to
   `cchifor/agentforge-tenants`); Flux applies the tenant layer.
3. Un-pause KEDA (remove the `paused-replicas` annotation from step 2 of pre-cutover).
4. **Verify the fleet chain green:**
   - CP: `just af-cp-smoke` (rollout, digest, readyz/healthz); Fleet page shows config pulls
     recording v3 (`note_config_pull` diagnostics), no version-floor 409s from CURRENT workers —
     a 409 here means a stale image somewhere (diagnose against `AFP_MIN_WORKER_VERSION`).
   - dispatcher: pod Ready, no restarts, `forge_pending` series present in Prometheus
     (distroless — port-forward `svc/kube-prometheus-stack-prometheus` and curl the HTTP API).
   - worker: scale-from-zero on a probe issue works; logs show a successful **v3** config pull.
5. **e2e probes** (plan §Verification):
   - create a `docs-fastlane`-style workflow from the library (or enable it), then file a playground
     issue whose body starts with `workflow: wf-docs-fastlane` → planner honors the directive, the
     plan gate reviews it, the issue wears `route: wf-docs-fastlane`, the dashboard chip shows it;
   - file an issue directing a DISABLED workflow → explanatory comment + `CAPABILITY_GAP`
     escalation, **no agent spend**, never a silent fallback;
   - same-slug effect edit while an issue sits at stage 3 → escalation (replan), not a silent
     authority change.

### Stale `config-lastgood.json` recovery

The state dir is an emptyDir, so a FRESH pod has no last-good at all; a pod that lived across the
cutover can hold a **v2** last-good, which the v3 floor rejects — the worker runs degraded (readyz
fails, no new claims) until its first successful v3 pull. If the CP/config repo is briefly
unavailable at exactly that moment, do not debug the file in place: **delete the pod**
(`kubectl … delete pod <worker-pod>`) — the emptyDir dies with it and the replacement starts clean
against the (by then reachable) v3 source. The same applies to the dispatcher.

## Rollback

Order matters here too (the mirror image of the window):

1. **Revert this PR** on main (pin revert → previous images). Re-stamp PIN FREEZE markers in the
   revert if the retry is not immediate.
2. **Revert the config repo commit** (`agentforge.json` back to v2) — v2 images reject a v3 config
   (newer-than-supported) and would otherwise run indefinitely on lastgood.
3. **Restore the CP DB** — required, not optional: the migrated head makes the OLD CP image's
   readyz fail on skew. Scale the CP down, restore, scale up:
   ```bash
   kubectl --context admin@ai -n agentforge scale deploy/agentforge-platform --replicas=0
   PRIMARY=$(kubectl --context admin@ai -n databases get cluster infra-pg \
     -o jsonpath='{.status.currentPrimary}')
   kubectl --context admin@ai -n databases exec -i "$PRIMARY" -c postgres -- \
     psql -U postgres -d agentforge_platform < agentforge_platform.pre-v3.<stamp>.sql
   kubectl --context admin@ai -n agentforge scale deploy/agentforge-platform --replicas=1
   ```
4. Credentials: nothing to do — the rollback bridge (step 4 of the window) left WORKING PATs under
   the legacy key names, and `AF_BOT_TOKENS` is an unknown env var to v2 (ignored). If the bridge
   was skipped, the v2 fleet is fail-closed on revoked PATs: re-mint per-bot PATs and patch the
   legacy keys by hand.
5. `role_overlays` is untouched by any of this: its **drop migration ships separately** (CP PR2
   follow-up) and applies only after the cutover has soaked — which is exactly what keeps the
   backfill additive and this rollback a restore + revert rather than a reconstruction.

## Post-soak cleanup (separate PR, after the soak window)

- delete the legacy `AF_BOT_TOKEN_*` bridge keys from the three vault docs;
- CP PR2's separate `role_overlays` drop migration may now be applied;
- retire the dormant v1 ansible surface (`agentforge.env.j2` four-var block) or migrate it to
  `AF_BOT_TOKENS` if dev-worker v1 is ever to run a v3 release.
