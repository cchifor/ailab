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
`kubernetes/apps/infrastructure/agentforge-workers/{worker,dispatcher}-deployment.yaml`; a third
marker landed on the CP manifest (`apps/agentforge/deployment.yaml`) via #227/#228 — its text
explains why the freeze outlived its original reason and MUST be read, not "cleaned up". **The v3
cutover PR is the only sanctioned path any of the three markers come off.**

## Credential/env delivery inventory (what carries the bot PATs today)

Every consumer, and where its forge credentials come from. "extract" = ESO `dataFrom.extract` — every
KV key of the OpenBao doc becomes a Secret key, so **the KV key name IS the env var name** and adding
`AF_BOT_TOKENS` to the doc needs **no manifest change** on those surfaces.

| Consumer | Manifest (ns) | Delivery | OpenBao path | Legacy v2 keys | v3 change |
|---|---|---|---|---|---|
| tenant-zero **playground** orchestrator (`af-orch-playground-planner`, `AF_CONFIG_SOURCE=gitea`) | `agentforge-workers/worker-deployment.yaml` + `worker-externalsecret.yaml` (`af-tenant-tenant-zero-playground`) | `envFrom` Secret `af-creds-playground-planner` ← ESO **extract** | `af/data/tenants/tenant-zero/playground/orchestrator` | `AF_BOT_TOKEN_{PLANNER,TESTER,IMPL,REVIEWER}` (+ HMAC, litellm, `AF_CAPABILITY_SIGNING_KEY`/`KID`) | vault doc gains one `AF_BOT_TOKENS` key (bootstrap mint, below — **doc still legacy-only today**); legacy keys become harmless-but-dead env vars (v3 never reads them). **LIVE workload** (`af-orch-playground-planner` 2/2) — needs a restart after the mint to project the new key (§4) |
| tenant-zero **platform-dev** orchestrator (`af-orch-platform-dev-delivery`, CP-mode) | `agentforge-tenant-platform-dev/externalsecret.yaml` (+ CP-rendered Deployment in `cchifor/agentforge-tenants`) | `envFrom` Secret `af-creds-platform-dev-delivery` ← ESO **extract** | `af/data/tenants/tenant-zero/platform-dev/orchestrator` | same four + `AF_CONTROL_PLANE_TOKEN` | same as playground (**doc still legacy-only today**; **LIVE workload**, `af-orch-platform-dev-delivery` 1/1 — restart after the mint, §4); **plus** its seeds fragment must be refreshed (see the seeds trap below) |
| **dispatcher** (`agentforge-dispatcher`, scale oracle) | `agentforge-workers/dispatcher-deployment.yaml` + `dispatcher-externalsecret.yaml` (`agentforge`) | `envFrom` Secret `agentforge-dispatcher-forge` ← ESO **explicit `data` mapping** | `af/data/operator/dispatcher/forge` | `AF_BOT_TOKEN_PLANNER` (READ-ONLY issues:read PAT) | **DONE on main (#226)**: the ES projects **BOTH** `AF_BOT_TOKENS` **and** the legacy `AF_BOT_TOKEN_PLANNER` (deliberate — either image starts off the one Secret); the vault doc already carries the map (§4). The legacy **mapping** drops **post-soak**, not in this PR |
| **reaper** (`agentforge-reaper`) | `agentforge-sandbox/reaper-deployment.yaml` (`agentforge`) | — | — | **none** — `reaper()` builds no forge client and no config source | env unchanged; only its `p1-worker` digest moves with the fleet |
| **control plane** (`agentforge-platform`) | `apps/agentforge/deployment.yaml` (`agentforge`) | `secretKeyRef`s from SOPS Secrets (`agentforge-runtime`, `agentforge-db`, `agentforge-oauth`) + `envFrom agentforge-infra-bot` | — (SOPS, not OpenBao) | none of the `AF_BOT_TOKEN_*` family (CP bots are `AFP_*`) | image + `AFP_WORKER_IMAGE`/`AFP_SANDBOX_IMAGE` pins only |
| dev-worker **v1 hosts** (ADR 0018, DORMANT) | `ansible/roles/dev_worker/templates/agentforge.env.j2` | systemd EnvironmentFile from ansible SOPS secrets | — | all four `AF_BOT_TOKEN_*` | **no change** — `dev_worker_enable_agentforge: false` (defaults) and the group_vars enable line is commented out. If v1 is ever re-enabled on a v3 release, the template + `dev-worker.sops.yaml` keys must be migrated to `AF_BOT_TOKENS` first |

Durability layer (NOT a delivery path): `kubernetes/apps/infrastructure/security/openbao/`
`operator-seeds.sops.yaml` → `seeds.json`, applied by `_apply_operator_seeds` on **every** provision
Job run, **merge-writes with seed keys winning over the live vault**. Current relevant fragments:
`operator/dispatcher/forge` (**since #226**: `AF_BOT_TOKENS` — the canonical five-principal map —
**plus** the legacy `AF_BOT_TOKEN_PLANNER` bridge) and `tenants/tenant-zero/platform-dev/orchestrator`
(**still legacy-only**: four `AF_BOT_TOKEN_*` + `AF_CONTROL_PLANE_TOKEN`).

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
- dispatcher ExternalSecret: **no change on this branch** — #226 (main) already projects
  `AF_BOT_TOKENS` ← `operator/dispatcher/forge` property `AF_BOT_TOKENS` **beside** the legacy
  `AF_BOT_TOKEN_PLANNER` mapping, and **both mappings stay through the cutover**: the legacy key is
  the rollback bridge (a reverted pin lands the v2 image on this same Secret, and v2 reads only the
  legacy var). Dropping the legacy mapping is a **post-soak** follow-up (see Post-soak cleanup).
- PIN FREEZE markers kept in place — the worker/dispatcher markers from the revert **and** main's CP
  marker (`apps/agentforge/deployment.yaml`, #227/#228 text preserved verbatim) — each annotated
  "removed by this PR at cutover merge".
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
3. **CP DB backup** (CNPG `infra-pg`, ns `databases`, db `agentforge_platform`). **This dump is the
   only pre-cutover copy of the DB in a restorable form** — `infra-pg` has **no** CNPG
   `ScheduledBackup` and an empty `.spec.backup` (no WAL archiving, no PITR); Velero's nightly CSI
   snapshot data movement covers the instance PVC crash-consistently, which is a rebuild path, not a
   point-in-time DB restore (standing gap — flagged as an out-of-scope follow-up below). So the dump
   step is deliberately paranoid: dump **in-pod** (custom format), verify **in-pod**, copy off the
   pod, then copy **off-cluster to the estate's durable backup target** — the versitygw S3 store on
   the QNAP (ADR 0010 copy #2, bucket `velero`), from which the 04:00 `backup-offsite` rclone leg
   mirrors it to the encrypted Drive remote (copy #3) automatically on the next run.

   Resolve the primary fresh — never assume `infra-pg-1`. Exec-redirect only, in the OUT direction
   (NEVER `kubectl cp`, and never pipe INTO an exec — MSYS mangling/stdin-reads have produced 0-byte
   "backups" before):
   ```bash
   STAMP=$(date +%Y%m%d-%H%M%S); DUMP="agentforge_platform.pre-v3.${STAMP}.dump"
   PRIMARY=$(kubectl --context admin@ai -n databases get cluster infra-pg \
     -o jsonpath='{.status.currentPrimary}')
   # 1) dump INSIDE the pod, custom format (-Fc: compressed + pg_restore-verifiable). The instance
   #    PVC has gigabytes free vs a ~10 MB DB (the df line proves it before writing); the in-pod
   #    copy is KEPT until post-soak — it is the no-stdin rollback path.
   kubectl --context admin@ai -n databases exec "$PRIMARY" -c postgres -- sh -c \
     "df -h /var/lib/postgresql/data | tail -1 \
      && pg_dump -U postgres -Fc -d agentforge_platform -f /var/lib/postgresql/data/$DUMP \
      && pg_restore --list /var/lib/postgresql/data/$DUMP >/dev/null \
      && echo TOC-OK && ls -l /var/lib/postgresql/data/$DUMP"
   # 2) copy OFF the pod (exec-redirect OUT — the safe direction on MSYS):
   kubectl --context admin@ai -n databases exec "$PRIMARY" -c postgres -- \
     cat "/var/lib/postgresql/data/$DUMP" > "$DUMP"
   ls -l "$DUMP"   # byte size MUST equal the in-pod ls -l size from step 1
   # 3) copy OFF-CLUSTER to versitygw (bucket-owner key from velero/secret.sops.yaml; -k matches the
   #    BSL's insecureSkipTLSVerify — self-signed cert, and MSYS curl is Schannel so --cacert PEM
   #    fails exit 60). curl >= 7.75 signs S3 requests natively:
   eval "$(SOPS_AGE_KEY_FILE="$(cd "$(git rev-parse --git-common-dir)/.." && pwd -P)/kubernetes/infra/_out/age.agekey" \
     sops -d --extract '["stringData"]["cloud"]' \
       kubernetes/apps/infrastructure/storage/velero/secret.sops.yaml \
     | awk -F' *= *' '/aws_access_key_id/{print "AK="$2} /aws_secret_access_key/{print "SK="$2}')"
   curl -k --fail --aws-sigv4 "aws:amz:us-east-1:s3" --user "$AK:$SK" \
     -T "$DUMP" "https://192.168.1.225:7070/velero/manual-dumps/$DUMP"
   # 4) verify the durable copy landed at full size:
   curl -k --fail -sI --aws-sigv4 "aws:amz:us-east-1:s3" --user "$AK:$SK" \
     "https://192.168.1.225:7070/velero/manual-dumps/$DUMP" | grep -i content-length
   ```
   **Pre-flight gate — do NOT enter the window until all three agree:** `pg_restore --list`
   succeeded (`TOC-OK` — the TOC listing is the integrity check; a truncated/empty custom-format
   dump fails it), the workstation file size equals the in-pod size, and the versitygw
   `Content-Length` equals both. Three copies then exist (in-pod on the instance PVC, workstation,
   QNAP S3 → Drive overnight). Keep all of them until the soak (and the later `role_overlays` drop
   migration) completes.

   > **Standing gap (out of scope here, do not lose it):** `infra-pg` has no scheduled logical
   > backup at all — this runbook's manual dump is a one-off. Follow-up: give `infra-pg` a real
   > mechanism (CNPG `barmanObjectStore` + `ScheduledBackup` to versitygw, or a pg_dump CronJob into
   > the same bucket).

## The window — ordered steps

### 4. Mint credentials (before any manifest merges)

**Scope check first — TWO tenant orchestrators are LIVE, and both are covered by this step.** The
`af-orch-*` names are easy to miss when scanning for "worker": `af-orch-playground-planner` (2/2, ns
`af-tenant-tenant-zero-playground`) and `af-orch-platform-dev-delivery` (1/1, ns
`af-tenant-tenant-zero-platform-dev`). Their OpenBao docs
(`tenants/tenant-zero/{playground,platform-dev}/orchestrator`) **still carry ONLY the legacy
`AF_BOT_TOKEN_*` vars today** — the tenant mint below writes the five-principal `AF_BOT_TOKENS` map
(incl. `ops-bot`) into **both** workspaces. And because `envFrom` is read at pod start, the running
pods do NOT pick the new key up on their own: each orchestrator **needs a restart** after its mint +
ESO sync (the restart sub-step below). Sequencing caveat (same as the operator-doc caveat below):
**every mint ROTATES**, so run each workspace's mint **immediately before** its restart — a long gap
leaves the live v2 pods holding revoked PATs.

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
- **Dispatcher doc — DONE (2026-08-04), verify rather than redo:** `af/operator/dispatcher/forge`
  is at KV **version 29** carrying the **canonical `AF_BOT_TOKENS` map over all five principals
  (incl. `ops-bot`)** plus `AF_BOT_TOKEN_PLANNER` as the rollback bridge, and
  `operator-seeds.sops.yaml` on main (#226) carries the **same** map, so a provision-Job run
  preserves it. Nothing to mint here at cutover — just confirm the doc still holds both keys.
  **Caveat that makes "DONE" conditional:** `mint_bot_tokens` **ROTATES on every run** — any further
  `agentforge-bootstrap` invocation (including the tenant mints above) supersedes these values: the
  five PATs in the v29 map are revoked and re-minted, so after the tenant mints, re-patch
  `operator/dispatcher/forge` with the fresh map (and re-encrypt the seeds fragment to match) or the
  dispatcher doc + seeds hold dead tokens. The same rotation is why a tenant mint invalidates what
  the LIVE tenant workers hold in env — sequence each tenant mint immediately before its restart.
- **Seeds re-encrypt:** merge the tenant `--seeds-out` fragments into `operator-seeds.sops.yaml`
  and commit ON THIS PR BRANCH (the dispatcher fragment already carries `AF_BOT_TOKENS` since #226 —
  it only needs a refresh here if the tenant mints rotated the map, per the caveat above).
  Decrypt→edit→re-encrypt with
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
- **Restart the LIVE tenant orchestrators** so the running pods project the refreshed Secret
  (`envFrom` is read at pod start — without this the v2 pods keep the pre-mint, now-REVOKED PATs in
  env). Immediately after each workspace's mint + force-sync:
  ```bash
  kubectl --context admin@ai -n af-tenant-tenant-zero-playground rollout restart \
    deploy/af-orch-playground-planner    # no-op while KEDA holds paused-replicas=0 — still run it
  kubectl --context admin@ai -n af-tenant-tenant-zero-platform-dev rollout restart \
    deploy/af-orch-platform-dev-delivery
  ```
  The dispatcher is the same story one level up: a tenant mint rotates the PATs behind the operator
  doc's map/bridge values, so after re-patching `operator/dispatcher/forge` (caveat above) either
  restart `deploy/agentforge-dispatcher` or accept a degraded scale oracle until the pin rollout
  replaces the pod minutes later in the same window.

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
2. **Remove the PIN FREEZE marker blocks** from `worker-deployment.yaml`,
   `dispatcher-deployment.yaml` **and** the CP `apps/agentforge/deployment.yaml` (the #227/#228
   block) — explicit checklist item; this PR is the only sanctioned removal path. Also drop this
   PR's "removed by this PR at cutover merge" annotation lines with them.
3. Re-validate: `kubectl kustomize` over `agentforge-workers/`, `agentforge-sandbox/`,
   `apps/agentforge/`; `sops -d` round-trip on the updated seeds file.
4. **Migrate the CP DB ahead of the rollout** (the alembic head moves — PR1's four tables). From
   THIS branch checkout: `AF_KUBE_CONTEXT=admin@ai bash scripts/af-db.sh migrate` (db-migrate.yaml
   is excluded from kustomization, so it applies from the checkout, not Flux). The OLD CP stays
   Ready under an ahead-of-expected head (`ahead_or_unknown`) — verified behavior; the NEW CP would
   wedge NotReady against an un-migrated DB, which is why migrate precedes merge.
5. Merge (squash) on Gitea → Flux applies: new CP + worker/dispatcher/reaper images and the seeds
   Secret (the dispatcher ES dual mapping is already live from #226 — nothing to apply there).

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
   readyz fail on skew. The pre-cutover dump was deliberately LEFT IN-POD (custom format) so the
   restore needs **no stdin into an exec** (the MSYS silent-death trap). Scale the CP down, restore,
   scale up:
   ```bash
   kubectl --context admin@ai -n agentforge scale deploy/agentforge-platform --replicas=0
   PRIMARY=$(kubectl --context admin@ai -n databases get cluster infra-pg \
     -o jsonpath='{.status.currentPrimary}')
   # The dump lives on the volume of the pod that WAS primary at pre-cutover step 3. If the primary
   # has moved since, exec into the instance that holds the file instead (ls both); if neither has
   # it, pull manual-dumps/<dump> back from versitygw and restore from a Linux/WSL shell (stdin
   # into kubectl exec is only unsafe from MSYS).
   kubectl --context admin@ai -n databases exec "$PRIMARY" -c postgres -- \
     pg_restore -U postgres --clean --if-exists -d agentforge_platform \
     /var/lib/postgresql/data/agentforge_platform.pre-v3.<stamp>.dump
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

- **drop the legacy `AF_BOT_TOKEN_PLANNER` mapping from `dispatcher-externalsecret.yaml`** (#226
  projects BOTH keys on purpose, and the dual projection must OUTLIVE the cutover merge — the legacy
  mapping is what makes a pin **revert** safe: a reverted pin lands the old v2 image on this same
  Secret, and v2 reads only the legacy key. Only once the soak has retired "revert to v2" as an
  option does the mapping come off). Drop the matching legacy key from the dispatcher seeds fragment
  in `operator-seeds.sops.yaml` in the same PR;
- delete the legacy `AF_BOT_TOKEN_*` bridge keys from the three vault docs;
- CP PR2's separate `role_overlays` drop migration may now be applied;
- delete the in-pod pre-v3 dump copy
  (`kubectl … exec <instance> -c postgres -- rm /var/lib/postgresql/data/agentforge_platform.pre-v3.<stamp>.dump`)
  — the workstation + versitygw/Drive copies remain the archive;
- **file the standing-gap follow-up:** `infra-pg` still has NO scheduled logical backup (empty
  `.spec.backup`, no `ScheduledBackup`) — CNPG `barmanObjectStore` + `ScheduledBackup` to versitygw,
  or a pg_dump CronJob into the same bucket (out of scope for the cutover, must not stay unfiled);
- retire the dormant v1 ansible surface (`agentforge.env.j2` four-var block) or migrate it to
  `AF_BOT_TOKENS` if dev-worker v1 is ever to run a v3 release.
