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
| tenant-zero **playground** orchestrator (`af-orch-playground-planner`, `AF_CONFIG_SOURCE=gitea`) | `agentforge-workers/worker-deployment.yaml` + `worker-externalsecret.yaml` (`af-tenant-tenant-zero-playground`) | `envFrom` Secret `af-creds-playground-planner` ← ESO **extract** | `af/data/tenants/tenant-zero/playground/orchestrator` | `AF_BOT_TOKEN_{PLANNER,TESTER,IMPL,REVIEWER}` (+ HMAC, litellm, `AF_CAPABILITY_SIGNING_KEY`/`KID`) | **DONE 2026-08-05 (out-of-band)**: the vault doc carries `AF_BOT_TOKENS` (KV **v2**) beside the intact legacy keys — §4 **verifies**, nothing to mint; legacy keys stay valid (no rotation ran) and become harmless-but-dead env vars to v3. **LIVE workload** (`af-orch-playground-planner` 2/2) — **no restart needed**: the ESO-synced Secret already carries the key, and the v3 pods project it when the pin rollout replaces them |
| tenant-zero **platform-dev** orchestrator (`af-orch-platform-dev-delivery`, CP-mode) | `agentforge-tenant-platform-dev/externalsecret.yaml` (+ CP-rendered Deployment in `cchifor/agentforge-tenants`) | `envFrom` Secret `af-creds-platform-dev-delivery` ← ESO **extract** | `af/data/tenants/tenant-zero/platform-dev/orchestrator` | same four + `AF_CONTROL_PLANE_TOKEN` | same as playground — **DONE 2026-08-05 (out-of-band)**: doc carries `AF_BOT_TOKENS` (KV **v30**), §4 verifies (**LIVE workload**, `af-orch-platform-dev-delivery` 1/1 — **no restart needed**); its seeds fragment is already refreshed on main (**#230**) |
| **dispatcher** (`agentforge-dispatcher`, scale oracle) | `agentforge-workers/dispatcher-deployment.yaml` + `dispatcher-externalsecret.yaml` (`agentforge`) | `envFrom` Secret `agentforge-dispatcher-forge` ← ESO **explicit `data` mapping** | `af/data/operator/dispatcher/forge` | `AF_BOT_TOKEN_PLANNER` (READ-ONLY issues:read PAT) | **DONE on main (#226)**: the ES projects **BOTH** `AF_BOT_TOKENS` **and** the legacy `AF_BOT_TOKEN_PLANNER` (deliberate — either image starts off the one Secret); the vault doc already carries the map (§4). The legacy **mapping** drops **post-soak**, not in this PR |
| **reaper** (`agentforge-reaper`) | `agentforge-sandbox/reaper-deployment.yaml` (`agentforge`) | — | — | **none** — `reaper()` builds no forge client and no config source | env unchanged; only its `p1-worker` digest moves with the fleet |
| **control plane** (`agentforge-platform`) | `apps/agentforge/deployment.yaml` (`agentforge`) | `secretKeyRef`s from SOPS Secrets (`agentforge-runtime`, `agentforge-db`, `agentforge-oauth`) + `envFrom agentforge-infra-bot` | — (SOPS, not OpenBao) | none of the `AF_BOT_TOKEN_*` family (CP bots are `AFP_*`) | image + `AFP_WORKER_IMAGE`/`AFP_SANDBOX_IMAGE` pins only |
| dev-worker **v1 hosts** (ADR 0018, DORMANT) | `ansible/roles/dev_worker/templates/agentforge.env.j2` | systemd EnvironmentFile from ansible SOPS secrets | — | all four `AF_BOT_TOKEN_*` | **no change** — `dev_worker_enable_agentforge: false` (defaults) and the group_vars enable line is commented out. If v1 is ever re-enabled on a v3 release, the template + `dev-worker.sops.yaml` keys must be migrated to `AF_BOT_TOKENS` first |

Durability layer (NOT a delivery path): `kubernetes/apps/infrastructure/security/openbao/`
`operator-seeds.sops.yaml` → `seeds.json`, applied by `_apply_operator_seeds` on **every** provision
Job run, **merge-writes with seed keys winning over the live vault**. Current relevant fragments:
`operator/dispatcher/forge` (**since #226**: `AF_BOT_TOKENS` — the canonical five-principal map —
**plus** the legacy `AF_BOT_TOKEN_PLANNER` bridge) and **both** tenant orchestrator paths
(**since #230**: the platform-dev fragment gained `AF_BOT_TOKENS`, a playground fragment was
added) — so a wipe + re-provision restores the map v3 actually reads.

> **The seeds trap (why this PR never pre-staged an `AF_BOT_TOKENS` placeholder in seeds.json):**
> seed keys are authoritative over the vault — **KEY-level: for a key present in both, the seed
> value WINS; DOCUMENT-level is what "add-only" names** (unmentioned keys survive). Both halves are
> pinned in the #229 comment on `security/openbao/provision-job.yaml`. A placeholder
> `AF_BOT_TOKENS` committed ahead of the real values would have been merge-written **over** the
> minted map on the next provision run and fail-closed the fleet — the same end state as the
> 2026-08-04 incident, delivered by our own durability machinery. The refresh therefore happened
> **with the real values, out of band**: #226 (dispatcher) and #230 (both tenant paths) landed on
> main, so the vault state now survives a wipe. The standing rule: the provision Job re-applies
> seeds on every run (daily), so **any future rotation has a ~24 h deadline** to re-encrypt
> `operator-seeds.sops.yaml` in the same change (`--seeds-out`; requires `--write-openbao`) — see
> the re-mint subsection of §4. A vault-only rotation is silently reverted to REVOKED values.

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
   (Remove the annotation post-verify.) The pause is the **drain point**: from here the CP takes
   no new intake, which is what makes the window's first act — the DB dump, step 3 — a backup of
   the exact state a rollback would want back.

## The window — ordered steps

### 3. CP DB backup — first act of the window (after the drain, before ANY merge)

CNPG `infra-pg`, ns `databases`, db `agentforge_platform`. **Taken INSIDE the window, immediately
after the step-2 drain/KEDA pause and before anything merges**, so the dump is *current*, not
merely intact. Precisely what step 2 establishes: **playground intake is paused** (its
ScaledObject annotation) — but `af-orch-platform-dev-delivery` is CP-rendered, fixed-replica,
and stays a live 1/1 workload through the window, so it can still drive CP writes after the
dump. The delta is therefore *bounded by platform-dev's in-flight work*, not zero (small on a
~10 MB DB, and Rollback states what a restore discards). To make the delta genuinely zero,
take the OPTIONAL stronger quiesce before dumping: `flux suspend kustomization
agentforge-tenant-platform-dev` then `kubectl -n agentforge scale deploy/af-orch-platform-dev-delivery
--replicas=0` (suspend first — the Kustomization heals replicas on its 10m prune interval,
same reasoning as the Rollback wrap). Scale back up and `flux resume` at the step-6 restart —
which this pod needs ANYWAY to pick up `AF_BOT_TOKENS` from its Secret (envFrom is read at pod
start), so the quiesce doubles as the required credential-pickup restart rather than adding one. (An earlier draft took this dump "any time before the
window" — that proved the dump restorable but left everything written between dump and window
unprotected.) **This dump is the only pre-v3 copy of the DB in a restorable form** — `infra-pg`
has **no** CNPG `ScheduledBackup` and an empty `.spec.backup` (no WAL archiving, no PITR);
Velero's nightly CSI snapshot data movement covers the instance PVC crash-consistently, which is
a rebuild path, not a point-in-time DB restore (standing gap — flagged as an out-of-scope
follow-up below). So the dump step is deliberately paranoid: dump **in-pod** (custom format),
verify **in-pod**, copy off the pod, then copy **off-cluster to the estate's durable backup
target** — the versitygw S3 store on the QNAP (ADR 0010 copy #2, bucket `velero`), from which the
04:00 `backup-offsite` rclone leg mirrors it to the encrypted Drive remote (copy #3)
automatically on the next run.

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
**Gate — do NOT proceed to step 4 until all three agree:** `pg_restore --list`
succeeded (`TOC-OK` — the TOC listing is the integrity check; a truncated/empty custom-format
dump fails it), the workstation file size equals the in-pod size, and the versitygw
`Content-Length` equals both. Three copies then exist (in-pod on the instance PVC, workstation,
QNAP S3 → Drive overnight). Keep all of them until the soak (and the later `role_overlays` drop
migration) completes.

> **Standing gap (out of scope here, do not lose it):** `infra-pg` has no scheduled logical
> backup at all — this runbook's manual dump is a one-off. Follow-up: give `infra-pg` a real
> mechanism (CNPG `barmanObjectStore` + `ScheduledBackup` to versitygw, or a pg_dump CronJob into
> the same bucket).

### 4. Verify credentials (the mint is DONE — out of band, 2026-08-05; nothing to mint here)

**All three OpenBao docs already carry the canonical five-principal `AF_BOT_TOKENS` map (incl.
`ops-bot`), delivered out of band on 2026-08-05 and verified end to end** — both tenant
ExternalSecrets `Ready=True`, every pre-existing key preserved (`AF_CAPABILITY_SIGNING_KEY`/`KID`,
all four legacy `AF_BOT_TOKEN_*`, platform-dev's `AF_CONTROL_PLANE_TOKEN`). Critically, **no mint
was run**: bot PATs are per-**principal**, not per-workspace, so the already-valid map was copied
verbatim to the tenant paths — **zero rotation**. The legacy keys the two LIVE v2 orchestrators
(`af-orch-playground-planner` 2/2, `af-orch-platform-dev-delivery` 1/1) read were never revoked
and still work, so the old mint→force-sync→restart choreography is GONE from this window: no
restarts needed (the v3 pods project `AF_BOT_TOKENS` from the already-synced Secrets when the pin
rollout replaces them). This step is **verify-only**:

| OpenBao path (mount `af`) | expected state |
|---|---|
| `operator/dispatcher/forge` | `AF_BOT_TOKENS` (KV **v29**) **plus** `AF_BOT_TOKEN_PLANNER` patched to the *current* planner-bot PAT (the rollback bridge) |
| `tenants/tenant-zero/playground/orchestrator` | `AF_BOT_TOKENS` (KV **v2**), legacy `AF_BOT_TOKEN_*` intact |
| `tenants/tenant-zero/platform-dev/orchestrator` | `AF_BOT_TOKENS` (KV **v30**), legacy keys + `AF_CONTROL_PLANE_TOKEN` intact |

(The KV versions are floors, not exact-match gates — any later legitimate patch bumps them; the
gate is the KEY SET and the map's coverage. Seeds durability for all three paths: #226 + #230.)

**Verify the vault docs** — read each path in-pod (loopback; `openbao.ailab.chifor.me` does NOT
resolve and OpenBao deliberately has no ingress, so a workstation `--openbao-addr`/`BAO_ADDR` is
never the answer), token on stdin, printing key NAMES only, never values. The token comes from
Secret **`openbao-operator-provisioner-token`** (ns `openbao`, key `token`):

```bash
umask 077; TOK=<out-of-repo scratch file>
kubectl --context admin@ai -n openbao get secret openbao-operator-provisioner-token \
  -o jsonpath='{.data.token}' | base64 -d > "$TOK"
for P in operator/dispatcher/forge \
         tenants/tenant-zero/playground/orchestrator \
         tenants/tenant-zero/platform-dev/orchestrator; do
  kubectl --context admin@ai -n openbao exec -i openbao-0 -- sh -c \
    "read T; BAO_TOKEN=\$T BAO_ADDR=https://127.0.0.1:8200 BAO_SKIP_VERIFY=true \
     bao kv get -format=json -mount=af $P" < "$TOK" \
  | python -c "import sys,json; d=json.load(sys.stdin)['data']['data']; \
m=json.loads(d['AF_BOT_TOKENS']); print(sorted(d), sorted(m)); \
assert set(m) == {'planner-bot','tester-bot','impl-bot','reviewer-bot','ops-bot'}, sorted(m)"
done
```

Gate: on **all three** paths `AF_BOT_TOKENS` parses as JSON and covers all five principals'
bot users **including `ops-bot`** (the map is a bot-username→PAT object — worker
`infra/settings.py::bot_tokens`); `operator/dispatcher/forge` additionally still lists the
`AF_BOT_TOKEN_PLANNER` bridge key. (MSYS caveat: stdin into `kubectl exec` has died silently
under MSYS before — the `< file` redirect above is the form proven working on 2026-08-05; if
`read` comes back empty, run from WSL/Linux or stage the token via `exec -i -- sh -c 'cat >/tmp/t'`.)

**Verify the delivered Secrets** — both tenant `af-creds-*` Secrets carry `AF_BOT_TOKENS`, the
dispatcher Secret carries BOTH keys (key names only):

```bash
kubectl --context admin@ai -n af-tenant-tenant-zero-playground get secret \
  af-creds-playground-planner -o jsonpath='{.data}' \
  | python -c "import sys,json;ks=sorted(json.load(sys.stdin));print(ks);assert 'AF_BOT_TOKENS' in ks"
kubectl --context admin@ai -n af-tenant-tenant-zero-platform-dev get secret \
  af-creds-platform-dev-delivery -o jsonpath='{.data}' \
  | python -c "import sys,json;ks=sorted(json.load(sys.stdin));print(ks);assert 'AF_BOT_TOKENS' in ks"
kubectl --context admin@ai -n agentforge get secret agentforge-dispatcher-forge \
  -o jsonpath='{.data}' \
  | python -c "import sys,json;ks=sorted(json.load(sys.stdin));print(ks);\
assert {'AF_BOT_TOKENS','AF_BOT_TOKEN_PLANNER'} <= set(ks)"
```

Do not proceed to step 5 until all six checks are green.

#### Re-mint procedure (if ever needed — NOT part of the window)

If a credential is ever compromised/expired and a real re-mint is unavoidable, do NOT reach for a
workstation `--openbao-addr` (no ingress exists). The proven pattern is exec-in-pod with loopback
and the token **and** payload on stdin as one token-then-payload file — never argv (argv is
visible estate-wide via `ps`/audit logs; this is the same reasoning `--password-file` and
`--openbao-token-file` already encode):

```bash
kubectl --context admin@ai -n openbao exec -i openbao-0 -- sh -c \
  'read T; BAO_TOKEN=$T BAO_ADDR=https://127.0.0.1:8200 BAO_SKIP_VERIFY=true \
   bao kv patch -mount=af <path> @/dev/stdin' < <file: line 1 = token, then the JSON payload>
```

Two pins:

- the token is **`openbao-operator-provisioner-token`** — NOT `openbao-provisioner-token`, which
  **403s on operator paths by design**;
- token + payload arrive on **stdin, never argv**.

And the **seeds contract (mandatory, same change)**: any re-mint MUST re-encrypt
`operator-seeds.sops.yaml` with the new values in the same change — `agentforge-bootstrap
--seeds-out` emits the fragment (it requires `--write-openbao`) — because **seed keys WIN over
the live vault on every provision run** (KEY-level precedence; DOCUMENT-level is what "add-only"
names — the #229 comment on `security/openbao/provision-job.yaml` has both halves). The provision
Job re-applies seeds daily, so a vault-only rotation is silently reverted to REVOKED values
within ~24 h — the durability machinery delivering the outage itself. Remember also that
`agentforge-bootstrap` ROTATES on every run and writes ONLY `AF_BOT_TOKENS` (never the legacy
bridge keys): after any mint, copy the fresh map to all three paths, re-patch the legacy
`AF_BOT_TOKEN_*` bridge keys with the matching new per-bot PATs (keeps the v2 pin-revert path
alive), re-encrypt seeds, and restart the live consumers immediately (`envFrom` is read at pod
start — a gap leaves live pods holding revoked PATs).

### 5. Config repo to v3 (gitea-source consumers)

Bump `cchifor/agentforge-config` `agentforge.json` to schema v3 (shape per worker
`domain/config.py` @ the release commit): `schema_version: 3`, `stage_type` on **every** role
(the polymorphic implementer is dissolved — `ops`/`answerer` ship as roles), `bots` map covering all
five principals (`{"planner":"planner-bot","tester":"tester-bot","implementer":"impl-bot",
"reviewer":"reviewer-bot","ops":"ops-bot"}`), `workflows` as desired.

Ordering note: doing this BEFORE the image pins is safe by design — running v2 workers reject a v3
config as newer-than-supported and keep operating on `config-lastgood.json`; running v3 images
against a v2 config is the crashloop. Config first, images second, never the reverse.

### 5b. Relabel the estate — MANDATORY before the worker pins (worker #99 merged early)

Worker PR #99 (retire the legacy `state: N-*` vocabulary, dual-read deleted) was merged to
`cchifor/agentforge` main on 2026-08-05 — ahead of its own gate. Undeployed it is harmless (the
pin freeze holds), but the image this window ships reads ONLY the `stage: N-*` vocabulary: any
issue still wearing a legacy label becomes invisible to discovery and unprotected from ops triage
the moment the new workers start.

So, between the config bump (step 5) and the pins (step 6), for EVERY workflow-managed repo
(the config's `repos` allowlist — today `cchifor/agentforge-playground` plus any tenant repos):

```
# in the worker repo checkout @ the release commit, per target repo:
uv run python scripts/migrate_state_vocabulary.py --repo <owner>/<repo> --apply
# MUST end exit 0 with its re-read verification clean; --dry-run exit 0 alone proves nothing
# (the script's dry-run exits 0 without relabeling — a known footgun, see PR #99).
```

Gate: zero open OR closed issues wearing a `state: N-*` label in any managed repo
(the script's re-read verification is the check). Do not proceed to step 6 until this holds.

**Rehearsed 2026-08-05 (live census + dry-runs): the estate is ALREADY CONVERGED — zero legacy
labels worn in any of the four repos that still define them (playground, ailab, platform,
primes-lab; the other five org repos define neither vocabulary). 5b is expected to be a ~30 s
`--apply`-for-the-record per repo; only `--apply`'s `verified: no legacy lifecycle label remains`
line + exit 0 is the gate (dry-run exit 0 proves nothing — it exits 0 even with hundreds of
pending relabels).** The census is point-in-time, though — any hand-applied `state:` label between
rehearsal and window puts the drift back, which is why the definition deletion below is part of
5b proper, not optional cleanup.

**Auth — `--apply` is a WRITE and requires `write:issue` on every managed repo** (via
`AF_GITEA_URL` + `AF_GITEA_TOKEN`, the script's own env). Do not define the credential by the
zero-write census: if the estate has drifted by window time, the gate fails AND an under-scoped
token fails with it, mid-run. The `~/.git-credentials` chifor PAT carries the scope today; Gitea
PAT scopes are immutable, so the fallback is a fresh scoped token minted in-pod
(`write:issue` ⊇ read:issue; store it out-of-repo, never in argv/echo):
```bash
kubectl --context admin@ai -n gitea exec deploy/gitea -- \
  gitea admin user generate-access-token --raw -u chifor -t relabel-<stamp> --scopes write:issue
```

**Then convert the census into an invariant — delete the five `state: N-*` label DEFINITIONS** in
the four repos that still define them (`agentforge-playground`, `ailab`, `platform`,
`primes-lab`), immediately AFTER each repo's `--apply` verification: a label nobody can select is
a label nobody can re-attach. (After, never instead — deleting a definition silently STRIPS it
from any wearer without migrating it to `stage:`, so on a drifted estate deletion-first loses
lifecycle state that `--apply` would have translated.) Label ids are per-repo int64s — list, then
delete:
```bash
# per R in agentforge-playground ailab platform primes-lab:
curl -sf -u "chifor:$PAT" "https://git.chifor.me/api/v1/repos/cchifor/$R/labels?limit=50" \
  | python -c "import sys,json;[print(l['id'],l['name']) for l in json.load(sys.stdin) \
if l['name'].startswith('state: ')]"
curl -sf -X DELETE -u "chifor:$PAT" "https://git.chifor.me/api/v1/repos/cchifor/$R/labels/<id>"
# re-list: zero 'state: ' definitions may remain in any of the four repos
```

Remaining trap: a STALE `oauth2@git.chifor.me` entry in GCM causes a forge-wide ~5-7 min auth
lockout if any tooling retries it — purge it before the window.

### 6. Land the real pins on this branch, drop the freeze, merge

1. `just pin-workloads p1-worker=sha256:<…> sandbox=sha256:<…> agentforge-platform=sha256:<…>`
   (rewrites every placeholder in one reviewable diff; digests from the v3 release CI, tag→digest
   verified — `just pin-verify` / `crane digest`, fail closed if the tag moved).
2. **Remove the PIN FREEZE marker blocks** from `worker-deployment.yaml`,
   `dispatcher-deployment.yaml` **and** the CP `apps/agentforge/deployment.yaml` (the #227/#228
   block) — explicit checklist item; this PR is the only sanctioned removal path. Also drop this
   PR's "removed by this PR at cutover merge" annotation lines with them.
3. Re-validate: `kubectl kustomize` over `agentforge-workers/`, `agentforge-sandbox/`,
   `apps/agentforge/`. (No seeds change ships on this branch — the #226/#230 refresh already
   landed on main; only a §4 re-mint would ever put a seeds commit here, and then `sops -d`
   round-trip it.)
4. **Migrate the CP DB ahead of the rollout** (the alembic head moves — PR1's four tables). From
   THIS branch checkout: `AF_KUBE_CONTEXT=admin@ai bash scripts/af-db.sh migrate` (db-migrate.yaml
   is excluded from kustomization, so it applies from the checkout, not Flux). The OLD CP stays
   Ready under an ahead-of-expected head (`ahead_or_unknown`) — verified behavior; the NEW CP would
   wedge NotReady against an un-migrated DB, which is why migrate precedes merge.
5. Merge (squash) on Gitea → Flux applies: new CP + worker/dispatcher/reaper images (the
   dispatcher ES dual mapping and the seeds refresh are already live from #226/#230 — nothing to
   apply there).

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
   readyz fail on skew. The window dump was deliberately LEFT IN-POD (custom format) so the
   restore needs **no stdin into an exec** (the MSYS silent-death trap). Be plain about what a
   restore discards: **everything written after the drain point is gone** — and that loss is
   bounded, because the dump is taken as the window's first act AFTER the drain (step 3) and the
   CP is quiesced from the drain onward, so what vanishes is quiesced-window churn, not user work.

   **Suspend Flux first, resume only after verification:** the `apps` Kustomization (ns
   `flux-system`, path `./kubernetes/apps/apps`, `prune: true`) reconciles on a **10m interval** —
   left running, it re-applies the manifest's `replicas: 1` mid-restore and the CP writes into a
   half-restored database:
   ```bash
   flux --context admin@ai suspend kustomization apps    # ns flux-system; 10m interval + prune
   kubectl --context admin@ai -n agentforge scale deploy/agentforge-platform --replicas=0
   PRIMARY=$(kubectl --context admin@ai -n databases get cluster infra-pg \
     -o jsonpath='{.status.currentPrimary}')
   # The dump lives on the volume of the pod that WAS primary at window step 3. If the primary
   # has moved since, exec into the instance that holds the file instead (ls both); if neither has
   # it, pull manual-dumps/<dump> back from versitygw and restore from a Linux/WSL shell (stdin
   # into kubectl exec is only unsafe from MSYS).
   #
   # Pre-drop what the pre-PR1 dump cannot: --clean only drops objects that are IN the dump, and
   # PR1's four definitions tables (alembic 0016) are not — they would survive the restore, and
   # their composite FKs into workspaces would make --clean's DROP of workspaces fail (which
   # --exit-on-error below rightly turns into an abort). 0016 creates NO sequences (UUID pks
   # throughout — its own docstring), so the four tables are the complete pre-drop list; CASCADE
   # takes their FKs/indexes/policies, and the uq_workspace_id_org constraint 0016 added to
   # workspaces disappears when --clean recreates workspaces from the dump. role_overlays needs
   # nothing here — its drop migration ships post-soak, so it is still in the dump.
   kubectl --context admin@ai -n databases exec "$PRIMARY" -c postgres -- \
     psql -U postgres -d agentforge_platform -v ON_ERROR_STOP=1 -c \
     'DROP TABLE IF EXISTS agent_definition_versions, workflow_definition_versions, agent_definitions, workflow_definitions CASCADE'
   # --exit-on-error: pg_restore otherwise exits 0 over per-object failures — a rollback that
   # reports success over a partial restore is the one outcome this section exists to prevent.
   kubectl --context admin@ai -n databases exec "$PRIMARY" -c postgres -- \
     pg_restore -U postgres --clean --if-exists --exit-on-error -d agentforge_platform \
     /var/lib/postgresql/data/agentforge_platform.pre-v3.<stamp>.dump
   kubectl --context admin@ai -n agentforge scale deploy/agentforge-platform --replicas=1
   kubectl --context admin@ai -n agentforge rollout status deploy/agentforge-platform
   # verify the OLD image goes Ready (readyz green) against the restored DB FIRST, then:
   flux --context admin@ai resume kustomization apps
   ```
4. Credentials: nothing to do — **no mint ran** (§4), so nothing was ever revoked: the legacy
   `AF_BOT_TOKEN_*` keys still hold WORKING PATs (and `operator/dispatcher/forge` carries the
   `AF_BOT_TOKEN_PLANNER` bridge patched to the current planner-bot PAT), while `AF_BOT_TOKENS`
   is an unknown env var to v2 (ignored). Only if a §4 re-mint ever runs without its bridge
   re-patch is the v2 fleet fail-closed on revoked PATs — then re-mint per-bot PATs and patch the
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
