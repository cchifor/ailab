# Runbook — AgentForge v2 P1 control-plane (agentforge-platform) activation

Activates the `agentforge-platform` control plane (ADR 0019) at `https://agentforge.chifor.me`:
OIDC login → create a Workspace → the CP commits a CR to `cchifor/agentforge-tenants` → Flux
materializes the tenant. This is distinct from `agentforge-activation.md` (the broader P2-unlock
stack: OpenBao/ESO/KEDA/Kata). Plan: `plans/2026-07-22-agentforge-p1-activate-plan.md` (codex-reviewed).

The GitOps scaffolding (DB roles+DSNs, OIDC client, RBAC/SA/Service/NetworkPolicy/admission,
cloudflared route) is already merged. This runbook covers what remains, split across **two PRs** so
activation is a transactional switch:

- **PR-A (prerequisites, safe to merge anytime):** pins the CP image digest in `deployment.yaml` +
  `db-migrate.yaml` (+ the optional CNPG init-container digest), switches the Deployment readiness
  probe to `/readyz`, and seeds `af:tenant-zero:owner` onto the owner in `authelia-secret.sops.yaml`.
  `deployment.yaml` is **still excluded** from the kustomization, so **merging PR-A deploys nothing.**
- **PR-B (go-live switch):** the single line `- deployment.yaml` in
  `apps/agentforge/kustomization.yaml`. **Do NOT merge PR-B until** the bot tokens are minted + the
  DB is created + migrated (steps 2–4). Merging PR-B is what brings the CP up.

`/readyz` runs `SELECT 1` on the admin DSN, so the Deployment (under the wait:true `apps`
Kustomization) only reports Ready once the DB is reachable + migrated — the real go-live gate.
`/healthz` is unconditional and is used for liveness only.

Pinned image: `registry.chifor.me/agentforge/agentforge-platform@sha256:e8cce2ecbf14695796d5cd3f86daf6306f404174e0935187d558366803259094`
(tag `276ccad` = agentforge-platform `origin/main` HEAD `276ccad857…`, PR #18). This is whatever is
CURRENTLY pinned in `deployment.yaml` — re-check there if this note has gone stale; step 0 below
self-defaults off the live manifest so it never needs to be re-verified against a copy-pasted digest.

All `kubectl` uses `--context admin@ai` (or `KUBECONFIG=kubernetes/infra/_out/kubeconfig`).

---

## PR checklist

- [ ] `just af-verify-hashes` passes — confirms the `checksum/capability-kids` pod-template
      annotation (provisioner-deploy.yaml) and the platform-dev NFS provisioner Job's
      content-addressed name suffix still match a fresh recompute of their source content (no
      reloader/controller keeps either honest; see `scripts/check-inline-hashes.py`).

---

## Ordered activation steps

### 0. Re-verify the pinned image tag still resolves to the approved digest (fail closed)

```sh
just pin-verify agentforge-platform 276ccad
# scripts/verify-image-digest.sh: HEADs the registry manifest and PASS/FAILs against the digest
# self-defaulted from the CURRENT deployment.yaml pin (no copy-pasted digest to go stale here — pass
# an explicit 3rd arg, e.g. `just pin-verify agentforge-platform 276ccad sha256:...`, to check against
# something other than what's live-pinned).
# If it moved, re-pin deployment.yaml + db-migrate.yaml to the new digest (re-verify provenance) first.
```

### 1. Merge PR-A (prerequisites)

Safe: no CP is deployed (deployment.yaml still excluded). Flux applies the image pins (inert until
PR-B), the `/readyz` probe change (inert), and the Authelia owner-group seed.

```sh
flux --context admin@ai reconcile source git flux-system
flux --context admin@ai reconcile kustomization apps
# roll Authelia so it reloads the file-based user DB with the new group:
kubectl --context admin@ai -n auth rollout restart deploy/authelia
kubectl --context admin@ai -n auth rollout status  deploy/authelia
```

### 2. Mint the two Gitea bot tokens + fill the SOPS secret (GATED)

> Gitea PATs are **user+scope**, not per-repo, so per-repo isolation = **dedicated restricted bot
> users**. `agentforge-runtime.sops.yaml` ships with the two token values as **placeholders**;
> replace them with freshly-minted tokens. This mutates Gitea and was intentionally NOT run headless
> (the auto-mode classifier blocks in-pod user creation). **PR-B must not merge until this is done.**

Create the users + grant the tenants-repo collaborator (run inside the gitea pod). Emits only
non-secret status; it mints a transient site-admin token to add the collaborator and revokes it:

```sh
kubectl --context admin@ai -n gitea exec -i deploy/gitea -- sh <<'SH'
set -u
API=http://localhost:3000/api/v1
mk(){ u="$1"; gitea admin user list 2>/dev/null | awk '{print $2}' | grep -qx "$u" && { echo "$u EXISTS"; return; }
  gitea admin user create --restricted --username "$u" --email "$u@bots.local" \
    --random-password --must-change-password=false >/dev/null 2>&1 && echo "$u CREATED" || echo "$u CREATE-FAIL"; }
mk agentforge-cp-bot
mk agentforge-bootstrap-bot
ADMTOK=$(gitea admin user generate-access-token --raw -u gitea_admin -t afp-collab-tmp --scopes all 2>/dev/null)
curl -s -o /dev/null -w 'collab-add HTTP %{http_code}\n' -X PUT -H "Authorization: token $ADMTOK" \
  -H 'Content-Type: application/json' -d '{"permission":"write"}' \
  "$API/repos/cchifor/agentforge-tenants/collaborators/agentforge-cp-bot"
curl -s -H "Authorization: token $ADMTOK" "$API/repos/cchifor/agentforge-tenants/collaborators" \
  | tr ',' '\n' | grep -E '"login"|"permission"'
curl -s -o /dev/null -w 'adm-token-revoke HTTP %{http_code}\n' -X DELETE \
  -H "Authorization: token $ADMTOK" "$API/users/gitea_admin/tokens/afp-collab-tmp"
SH
```

Now land the two token VALUES. Two routes: **A** drives the CP's own seeding CLI, **B** is the manual
sequence. Both put a live PAT into a file on the operator host, and this is the step that leans on that
file's mode — so read the caveat first:

> **Windows caveat — this estate is operated from Windows, where the 0600 is not what protects the
> file.** Route A's writer says so in its own source: "Windows honours neither the open mode nor
> `O_NOFOLLOW`, so there the `chmod` below is all the platform offers and a token file is only as
> private as its **directory**" (`_write_private`, `src/agentforge_platform/forge_bootstrap/cli.py`) —
> it runs as native Windows CPython, where `os.open`'s mode and `os.chmod` reach little more than the
> read-only bit. Measured on this host: `_write_private` writes the handoff and Python then reports the
> mode as `0o666`, and `hasattr(os, "O_NOFOLLOW")` is `False`. (Its *exclusivity* does survive —
> `O_CREAT|O_EXCL` works on Windows, so a pre-existing path is still refused; only the mode is
> cosmetic.) Route B's `umask 077` is a POSIX mechanism Git Bash only *emulates*: these paths are
> `noacl` mounts (`mount` → `C:/Users/<you>/AppData/Local/Temp on /tmp type ntfs
> (binary,noacl,posix=0,usertemp)`), so a `0600` that `ls -l` reports there is not backed by an NTFS
> ACL. Either way the **directory** is the whole control: keep the file inside your own profile. Git
> Bash `/tmp` already is (`cygpath -w /tmp` → `C:\Users\<you>\AppData\Local\Temp`), which is why route
> B below uses a `mktemp -d` under it instead of fixed, guessable `/tmp/.afp_*` names.

#### Route A — the scripted ceremony (`afp-forge-bootstrap`)

`agentforge-platform` ships this entrypoint (`[project.scripts]` in its `pyproject.toml`; run through
`uv run`, as its own `justfile` `up` recipe does). It is what removes the durable exposure outright:
`fill-sops` edits the encrypted Secret **in place** via `sops set`, so no decrypted copy of
`agentforge-runtime.sops.yaml` is written to disk at all — `sops_fill.py`: "sops re-encrypts in place,
so no plaintext copy of the secret ever exists on disk (a decrypt-edit-encrypt cycle would leave one)".

```sh
AILAB=$(pwd)                                      # run from the ailab checkout
CP=../agentforge-platform                         # your agentforge-platform checkout
H="$HOME/.cache/agentforge/forge-bootstrap-handoff.json"   # = fill-sops's own default
export SOPS_AGE_KEY_FILE="$AILAB/kubernetes/infra/_out/age.agekey"
cd "$CP"
uv run afp-forge-bootstrap prod-seed --kubectl-context admin@ai --emit-handoff "$H"
uv run afp-forge-bootstrap fill-sops --handoff "$H" \
  --file "$AILAB/kubernetes/apps/apps/agentforge/agentforge-runtime.sops.yaml" \
  --map AFP_TENANTS_BOT_TOKEN=agentforge-cp-bot,AFP_BOOTSTRAP_TOKEN=agentforge-bootstrap-bot
uv run afp-forge-bootstrap verify --kubectl --kubectl-context admin@ai
```

What each guarantees (re-read `forge_bootstrap/cli.py` before trusting this summary):

- `prod-seed` **refuses to mint at all** without `--emit-handoff` (or an explicit `--discard-tokens`),
  so no run can leave a live PAT nobody holds.
- the handoff is created `O_WRONLY|O_CREAT|O_EXCL` (plus `O_NOFOLLOW` on platforms that have it — not
  Windows) at `0600`-intent **before the first byte of content exists**: it never truncates or
  overwrites, and a pre-existing path (stale handoff, planted symlink, fifo) is a hard error —
  "refusing to write …: it already exists" — not a target. On Windows read that as exclusivity only;
  see the caveat above for the mode.
- if that write fails, every token minted in the run is revoked before the non-zero exit
  (`_rollback_tokens`): the forge's state, not the exit code, is what it keeps honest.
- `fill-sops` **deletes the handoff on success** — and only once every token in it has landed. An
  unmapped one means exit 1, the file kept, and the unconsumed user named
  (`tests/unit/test_forge_bootstrap_sops.py::test_fill_sops_deletes_the_handoff_file_on_success`,
  `::test_fill_sops_keeps_the_handoff_when_it_still_holds_an_unmapped_token`).
- `verify --kubectl` is strictly zero-write. It mints nothing, so it can strand nothing; with no admin
  password in `PROD_SPEC` it checks existence only and warns that `is_admin`/`restricted` were not read.
- the operator host's `sops 3.9.4` has **no** stdin flag for a value (`sops set --help` lists none), so
  `fill` passes each token as an **argv element** for the length of one `sops set` call, and warns
  about it. Run it on your own machine only — never a shared or CI host.

What route A does **not** cover — do not skip these:

- **the collaborator grant.** `PROD_SPEC` declares `repos=[]` ("collaborator permissions on the real
  ailab repo stay a reviewed operator act, not an unattended CLI write" — `spec.py`), so the write
  grant on `cchifor/agentforge-tenants` still comes from the pod block above. `prod-seed` *does* create
  the users, with the same `--restricted --random-password --must-change-password=false` flags, so the
  two run in either order — both are idempotent (`mk` then prints `EXISTS`).
- **it mints four tokens, not two.** `PROD_SPEC` covers `agentforge-cp-bot`, `agentforge-bootstrap-bot`,
  `agentforge-infra-bot` and `agentforge-ci-bot`, and no flag narrows it. `AFP_INFRA_BOT_TOKEN` lives in
  a *different* file (`agentforge-infra-bot.sops.yaml`) and `agentforge-ci-bot` has **no destination in
  ailab at all** (`git grep agentforge-ci-bot` → no hits). `--file` is one file and the unmapped-token
  check is per-run, so **no single `fill-sops` run can consume all four**: land the infra token with a
  second `fill-sops --file …/agentforge-infra-bot.sops.yaml --map
  AFP_INFRA_BOT_TOKEN=agentforge-infra-bot`, revoke the `afp-ci-*` token on the forge, then `rm "$H"`
  yourself — by design the last run exits 1 and keeps the file rather than destroying a credential's
  only copy.
- **it does not prune what an earlier run minted.** `PROD_SPEC` seeds no admin with a password, so the
  prune step logs "NOT pruning tokens from earlier runs … revoke it there" and a re-run adds four more
  live PATs.

So route A is the **day-0, all-four-bots** path. For a two-token top-up on a live estate — what this
step usually is — route B is the narrower blast radius.

#### Route B — manual, two tokens, trap-guarded

Mint the two scoped tokens into **files outside the repo, under an owner-only temp dir** (never echo
the value / never in argv; `--raw` prints only the token), then fill the SOPS Secret from them. **Run
the block as ONE script** (`bash afp-step2.sh`, kept outside the checkout) **from the ailab checkout
root** — its `sops`/`git` paths are relative — and **never paste it line by line**: the `trap`
protects only the shell it is set
in, and it is what deletes the token files and the decrypted Secret on *every* exit path (success, any
`set -e` failure, `Ctrl-C`, `SIGTERM`) instead of a final `rm` that only the happy path reaches.

```sh
set -eu
umask 077
D=$(mktemp -d)                             # 0700 dir, unguessable name (no pre-planted /tmp symlink)
trap 'rm -rf "$D" 2>/dev/null || :' EXIT   # fires on success, on set -e, and on both exits below
trap 'exit 130' INT                        # Ctrl-C  -> exit -> the EXIT trap
trap 'exit 143' TERM                       # SIGTERM -> exit -> the EXIT trap

kubectl --context admin@ai -n gitea exec deploy/gitea -- \
  gitea admin user generate-access-token --raw -u agentforge-cp-bot \
  -t cp-tenants --scopes write:repository > "$D/cp_tok"
kubectl --context admin@ai -n gitea exec deploy/gitea -- \
  gitea admin user generate-access-token --raw -u agentforge-bootstrap-bot \
  -t bootstrap-labels --scopes write:issue > "$D/boot_tok"

# Fill agentforge-runtime.sops.yaml WITHOUT putting a token on a command line:
# build a plaintext copy from the token files, then encrypt in place.
export SOPS_AGE_KEY_FILE=kubernetes/infra/_out/age.agekey
F=kubernetes/apps/apps/agentforge/agentforge-runtime.sops.yaml
sops --decrypt "$F" > "$D/rt.yaml"
D="$D" python - <<'PY'
import os, yaml
d=os.environ["D"]; p=os.path.join(d,"rt.yaml")
y=yaml.safe_load(open(p,"rb")); sd=y["stringData"]
sd["AFP_TENANTS_BOT_TOKEN"]=open(os.path.join(d,"cp_tok")).read().strip()
sd["AFP_BOOTSTRAP_TOKEN"]=open(os.path.join(d,"boot_tok")).read().strip()
open(p,"wb").write(yaml.safe_dump(y,sort_keys=False,allow_unicode=True).encode())
PY
cp "$D/rt.yaml" "$F"
sops --encrypt --in-place "$F"
git diff --stat "$F"     # confirm only this file; values are ENC[...]
```

Why it is shaped that way:

- `set -eu` is load-bearing: without it a failed `kubectl exec` leaves an EMPTY token file and the run
  marches on to encrypt that empty value into the Secret (`/readyz` would still pass — see below — so
  it surfaces only as a failed tenants commit). With it, the first failure exits, into the trap.
- the `INT`/`TERM` traps are not redundant with `EXIT`: not every shell runs an `EXIT` trap when a
  fatal signal it has no trap for arrives (bash 5.3 does — that is not portable), and `exit 130`/`143`
  keep the conventional signal status. Both paths fall through to the one `EXIT` trap, so cleanup lives
  in exactly one place.
- the trap cannot fail the step: `rm -rf … || :` always succeeds, and the `EXIT` trap does not touch the
  exit status (a `set -e` abort still exits 1, `Ctrl-C` exits 130).
- it bounds, it does not remove: route B still writes a **fully decrypted** copy of the Secret to
  `$D/rt.yaml` for the length of the run. Route A never creates one. Route B also round-trips the file
  through `yaml.safe_dump`, which carries no YAML comments and so drops the Secret's
  `# AgentForge CP runtime secrets …` header — restore it in the diff.

Commit this `agentforge-runtime.sops.yaml` change **onto the PR-B branch itself** (the same PR as the
`- deployment.yaml` line) so the tokens and the Deployment merge **atomically**. This is required:
the pod captures `AFP_TENANTS_BOT_TOKEN`/`AFP_BOOTSTRAP_TOKEN` as env at startup, so it must never
start with placeholders (with placeholders `/readyz` still passes — it only checks the DB — but
create-workspace→tenants-commit then fails on a bad token). A live `kubectl` edit of the live Secret
is NOT a substitute — Flux reverts it to the committed ciphertext on the next reconcile.

Negative checks (least privilege): both bots `restricted` + non-admin; `agentforge-cp-bot` is a
**write** collaborator on **only** `cchifor/agentforge-tenants` (no write to `cchifor/ailab`, no
repo/org create); `agentforge-bootstrap-bot` has **no** repo write (its per-workspace collaborator
grant is added when a workspace repo is connected — bootstrap is off the create→commit path).

### 3. Create the `agentforge_platform` database (roles already exist)

`postInitSQL` does not run on the already-bootstrapped infra-pg; `managed.roles` already created
`afp_admin`/`afp_app`. Only the DB is missing. `scripts/af-db.sh init` resolves the **current
primary** from cluster status (never assumes `infra-pg-1`), pipes the `agentforge-db-bootstrap`
ConfigMap's `bootstrap.sql` through the idempotent `\gexec` one-shot as the peer-auth superuser, then
verifies the DB exists + `afp_admin` BYPASSRLS + `afp_app` NOBYPASSRLS:

```sh
just af-db-init
```

### 4. Run the schema/RLS migration (before PR-B, so no post-go-live missing-table errors)

`scripts/af-db.sh migrate` deletes + re-applies `db-migrate.yaml`, waits for completion (dumping the
last 40 log lines and exiting non-zero on failure), then prints `alembic_version` and verifies
`pg_class.relforcerowsecurity` is set on every RLS table:

```sh
just af-db-migrate
```

### 5. Merge PR-B (go-live) and verify

```sh
flux --context admin@ai reconcile source git flux-system
flux --context admin@ai reconcile kustomization apps
just af-cp-smoke
```

`scripts/af-cp-smoke.sh` waits for the rollout, asserts the running pod's `imageID` digest matches the
pin in `deployment.yaml`, hits the in-pod `/readyz`, and hits the external `/healthz` over the
cloudflared tunnel — one PASS/FAIL summary, non-zero on any failure. (Uses `AF_KUBE_CONTEXT` to
override `kubectl --context`; empty = current context, i.e. `admin@ai` if that's already selected.)

End-to-end (browser): `https://agentforge.chifor.me` loads → OIDC login (chifor) → `GET /api/me`
shows `tenant-zero: owner` → create a uniquely-named disposable Workspace → a commit appears under
`tenants/` in `cchifor/agentforge-tenants` → the `agentforge-tenants` Flux Kustomization materializes
the tenant namespace. Then delete the test workspace + its tenants-repo commit to leave no drift.

---

## Rollback

- **CP:** revert PR-B (remove `- deployment.yaml`) → Flux prunes the Deployment; SA/Service/RBAC
  remain (harmless).
- **Tokens:** in the gitea pod, `gitea admin user delete --username agentforge-cp-bot` /
  `agentforge-bootstrap-bot` (revokes their PATs), or `DELETE /api/v1/users/<user>/tokens/<name>`.
  Restore the placeholders in the SOPS secret.
- **Authelia group:** revert the `authelia-secret.sops.yaml` change and roll Authelia.
- **DB:** leave `agentforge_platform` in place unless a reviewed schema teardown exists; the
  `afp_admin`/`afp_app` roles are shared-managed — do not drop.

## Notes / gotchas

- ailab pushes go to the `gitea` remote (`git.chifor.me/cchifor/ailab`); Flux reconciles from
  in-cluster Gitea. `origin` (GitHub) is a backup mirror.
- The CP fails **shut** on an empty OIDC `groups` claim — the `af:tenant-zero:owner` seed (step 1)
  is what lets the owner in. Org rows auto-provision on first login from the groups claim.
- Access-FREE by design (ADR 0019): the CP does its own Authelia OIDC; no Cloudflare Access app.

## Day-2 — LLM subscriptions operations (WS4, 2026-07-24)

The Settings → Subscriptions page drives these; the CP holds NO delete/read-data privilege, so the
following stay OPERATOR steps:

- **Rotate (paste a new token/auth.json)**: fully self-service in the UI (CAS write → wait for the
  ESO sync (≤1h) → broker `credential_generation` match). To force an immediate sync:
  `kubectl --context admin@ai -n agentforge-broker annotate externalsecret <name> force-sync=$(date +%s) --overwrite`.
- **Refresh now (codex only)**: UI button creates a Job from CronJob `af-codex-refresh` (VAP-pinned).
- **Add account**: UI CAS-writes the cred, then opens an ailab PR via `agentforge-infra-bot`
  (AGit). Review + approve (reviewer-bot) + merge per the protected-main flow; the operation
  tracker advances to `active` once Flux/ESO/broker all report.
- **Remove account**: UI blocks while any workspace config or deployed render references the
  account, then opens the manifest-removal PR. AFTER merge+prune, the KV soft-delete is manual:
  `bao kv delete -mount=af operator/broker/<provider>/<account>/oauth` (provisioner token cannot
  and should not do this — deliberate).
- **Claude expiry**: not derivable in-cluster (opaque ~1yr `claude setup-token`). Keep the
  operator expiry note in the UI current when you rotate.
- **Bot/token inventory**: `agentforge-infra-bot` (READ on ailab; token = SOPS
  `AFP_INFRA_BOT_TOKEN`, ns agentforge) · `agentforge-reviewer-bot` (write collaborator, approvals
  only) · `agentforge-cp-bot`/`agentforge-bootstrap-bot` (tenants commits / label bootstrap).
  Rotate any of them the same way step 2 mints them: the trap-guarded block (route B) or
  `afp-forge-bootstrap prod-seed`/`fill-sops` (route A) — do not improvise a `/tmp` token file with no
  `trap`, that is the exposure step 2 exists to bound.
