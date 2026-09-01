# Runbook: dev-worker credentials from OpenBao (AppRole + `bao agent` + `cred`)

How the six dev-worker VMs get their secrets from OpenBao instead of from a hand-distributed SOPS
value, and the operator ceremonies that go with it: **activation**, **rotation**, **failure modes**,
**adding a secret / adding a worker**. Design + rejected alternatives: **ADR 0020**
(`docs/decisions/0020-dev-worker-openbao-credentials.md`). The VMs themselves:
`docs/runbooks/dev-workers.md`. The vault itself: `docs/runbooks/openbao-recovery.md`.

**Context for every command below:** `kubectl --context admin@ai` (the default context flip-flops —
always pass it explicitly), namespace `openbao`, repo root as CWD.

## What this is

Each worker gets its **own identity** in OpenBao (an AppRole with a read-only policy over its own KV
subtree) and a root-owned `bao agent` that keeps a **periodic** token renewed. The agent renders file-shaped
credentials (today: `~/.git-credentials`) and drops its token in a group-readable sink; the
`cred` helper reads that sink so an interactive Claude/Codex agent can fetch a secret **without ever
holding a vault token and without printing the value**. `cred exec … -- <cmd>` injects the value
straight into a child process environment — that is the form the managed CLAUDE.md block tells the
agents to prefer.

Everything above the secret-id is declarative (Flux + Ansible). The **only** hand ceremony is minting
each worker's secret-id, and that is deliberate: a login credential exists in exactly two places —
`ansible/secrets/dev-worker.sops.yaml` and `/etc/openbao-agent/` on the worker — never in a k8s
Secret.

### Component map

| Component | Lives in | Does |
|---|---|---|
| Service `openbao-lan` | `kubernetes/apps/infrastructure/security/openbao/openbao-lan.yaml` | NodePort **30820** on the node IPs (.41/.42/.43) → OpenBao :8200. No endpoints while sealed (selector needs `openbao-active`). |
| SAN `openbao.lan.chifor.me` | `.../openbao/tls.yaml` | The name the workers dial. cert-manager re-issues on spec change; **the server only re-reads on pod restart**. |
| Job `openbao-devworker-provision` (+ ConfigMap `openbao-devworker-provision-script`) | `.../openbao/devworker-provision-job.yaml` | Daily: enable `approle`, upsert 6 policies + 6 roles (periodic 72h tokens, no CIDR binding — see below), seed-patch the KV from one `<name>.json` per path. |
| Secret `openbao-devworker-seeds` | `.../openbao/devworker-seeds.sops.yaml` | Seed values for `af/dev-workers/*`. **Seed wins over live KV** on every run — see the precedence warning below. |
| Secret `openbao-breakglass-token` | live cluster only — **not in git** | The Job's auth. Never-expiring root, left by the 2026-08-30 re-bootstrap. **Data key is `root_token`.** |
| `bao agent` + unit | `ansible/roles/dev_worker/templates/openbao-agent.hcl.j2`, `openbao-agent.service.j2` | AppRole auto-auth → periodic token → sink + template rendering. |
| sink `/run/openbao-agent/token` | tmpfs (systemd `RuntimeDirectory=`) | Group-readable (`openbao-agent`) token for `cred`. Gone on reboot; re-created on login. |
| `/usr/local/bin/cred` | `ansible/roles/dev_worker/files/cred` | `cred list` · `cred get <name> <field>` · `cred exec <name> <field> <VAR> -- <cmd>`. |
| managed CLAUDE.md block | per user, `~/.claude/CLAUDE.md` | Tells the agents to use `cred` and to never print values. |

> **Precedence: this subtree is seed-WINS, and that is no longer the estate-wide default.** The
> `openbao-devworker-provision` Job is a shell `bao kv patch` loop: a key present in
> `devworker-seeds.sops.yaml` **overwrites live KV on every daily run**, and a key absent from it
> survives. The AgentForge **operator** seed (`operator-seeds.sops.yaml` → `_apply_operator_seeds`)
> is the opposite — **create-if-absent**, live vault wins, divergence reported as drift. Same word
> "seed", different precedence; the canonical side-by-side is
> `docs/runbooks/openbao-recovery.md` § "The seed-ownership contract". **Consequence here is
> unchanged:** rotating a seeded value in the vault alone is silently reverted within a day (see
> *Rotation*). Any prose in this repo that calls the dev-worker contract "the same as
> `operator-seeds.sops.yaml`" predates the operator-side change — including the header comment
> inside `devworker-seeds.sops.yaml` itself, which cannot be corrected without re-encrypting the
> file.

**KV layout** (mount `af`, KV v2 — the same mount ADR 0019 uses):

- `af/dev-workers/common` — shared across all six. First field: `gitea_pat`.
- `af/dev-workers/<inventory_hostname>` — per-worker. Empty today; this is where per-worker Gitea bot
  PATs land (ADR 0020 follow-up).

**Worker → IP → role** (the provision script's list must mirror this and
`kubernetes/infra/dev-workers/variables.tf`):

| Worker | IP | vmid | role + policy |
|---|---|---|---|
| dev-worker-1 | 192.168.0.8  | 4201 | `dev-worker-1` |
| dev-worker-2 | 192.168.0.9  | 4202 | `dev-worker-2` |
| dev-worker-3 | 192.168.0.10 | 4203 | `dev-worker-3` |
| dev-worker-4 | 192.168.0.11 | 4204 | `dev-worker-4` |
| dev-worker-5 | 192.168.0.12 | 4205 | `dev-worker-5` |
| dev-worker-6 | 192.168.0.13 | 4206 | `dev-worker-6` |

> **The IP column is documentation, not a control.** The roles carry **no** `token_bound_cidrs` /
> `secret_id_bound_cidrs`: Cilium's default SNAT LB mode plus `externalTrafficPolicy: Cluster` means
> OpenBao sees the ingress node, not .8–.13, so a CIDR binding would reject every login (full
> reasoning in `openbao-lan.yaml` and ADR 0020 §Decision 1). **Consequence: a secret-id works from
> anywhere on the LAN that can reach :30820, not only from its worker.** Treat each one as a live
> credential — destroy the old accessor when you re-mint, and when a worker is rebuilt.

## Activation ceremony

Run in order. Steps (a)–(d) are cluster-side and safe to do without touching a worker; the workers
change nothing until (f) flips `dev_worker_enable_openbao`.

**Operator prerequisites:** `kubectl` with the `admin@ai` context (`docs/runbooks/00-access-prereqs.md`),
`sops` + the age key at `kubernetes/infra/_out/age.agekey` (gitignored — it exists only in the main
checkout, not in a worktree), `python3` with `PyYAML`, `openssl`, and the **`bao` CLI** on the
workstation for step (e).

### (a) Merge → Flux applies

Merge the PR. The `openbao` Kustomization (`clusters/ai/openbao.yaml`, `wait: false`) picks up
`openbao-lan.yaml`, `devworker-seeds.sops.yaml`, and `devworker-provision-job.yaml`.

```bash
kubectl --context admin@ai -n flux-system annotate kustomization openbao \
  reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite
kubectl --context admin@ai -n openbao get svc openbao-lan \
  -o jsonpath='{.spec.ports[0].nodePort}{"\n"}'      # expect 30820
```

### (b) Re-issue the TLS cert for the new SAN, then restart the server

Adding `openbao.lan.chifor.me` to `dnsNames` is a spec change, so **cert-manager re-issues on its
own — do NOT delete the `openbao-tls` Secret**. But the OpenBao listener only re-reads its cert file
on process start, so the pod must be restarted before the new SAN is actually served.

```bash
# 1. wait for the re-issued cert
kubectl --context admin@ai -n openbao get certificate openbao-tls \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}{" "}{.status.notBefore}{"\n"}'

# 2. restart the server (the unsealer Deployment re-unseals it automatically — watch it come Ready).
#    NOT `rollout status`: the chart's StatefulSet uses the OnDelete update strategy, which that
#    command refuses. Ready implies unsealed — the readiness probe fails on a sealed vault.
kubectl --context admin@ai -n openbao delete pod openbao-0
kubectl --context admin@ai -n openbao wait --for=condition=Ready pod/openbao-0 --timeout=5m

# 3. prove the SAN is actually served on the NodePort
openssl s_client -connect 192.168.0.41:30820 -servername openbao.lan.chifor.me </dev/null 2>/dev/null \
  | openssl x509 -noout -ext subjectAltName
#    must list DNS:openbao.lan.chifor.me alongside the existing svc names
```

If step 3 shows the old SAN set, the pod restarted before cert-manager finished — repeat 1→2→3.

### (c) Fill the seed with the live Gitea PAT

**The initial PR (#431) already shipped this step done** — `devworker-seeds.sops.yaml` landed
SOPS-encrypted with the live PAT, so on first activation just confirm
`sops filestatus` says `{"encrypted": true}` and move on to (d). The rest of this section is the
ceremony for the placeholder/re-seed case (e.g. after the seed file is ever reset or a new path is
added): the value to put in is the PAT the workers already use (`dev_worker_gitea_token` in
`ansible/secrets/dev-worker.sops.yaml`), copied across **programmatically** — never through the
terminal, never through a shell argument.

**Timing trap:** the provision Job is a run-once-per-day converger. If it already `succeeded` before
your seed change merged, the new value sits unapplied until the daily reap/re-apply — force it
instead (same as *Rotation* step 4):

```bash
kubectl --context admin@ai -n openbao delete job openbao-devworker-provision
flux --context admin@ai reconcile kustomization openbao -n flux-system
```

```bash
export SOPS_AGE_KEY_FILE="$(pwd)/kubernetes/infra/_out/age.agekey"   # main checkout; _out/ is gitignored
SEEDS=kubernetes/apps/infrastructure/security/openbao/devworker-seeds.sops.yaml
sops filestatus "$SEEDS"        # {"encrypted": true} once the orchestrator has encrypted it
```

```bash
python3 - "$SEEDS" <<'PY'
import json, pathlib, re, subprocess, sys, yaml

def plaintext(path):
    """sops -d if the file is encrypted, else read it as-is (pre-first-encryption state)."""
    status = json.loads(subprocess.run(["sops", "filestatus", path],
                                       capture_output=True, text=True, check=True).stdout)
    if not status.get("encrypted"):
        return pathlib.Path(path).read_text()
    return subprocess.run(["sops", "-d", path],
                          capture_output=True, text=True, check=True).stdout

pat = yaml.safe_load(plaintext("ansible/secrets/dev-worker.sops.yaml"))["dev_worker_gitea_token"]
assert pat and not pat.startswith("CHANGE-ME"), "dev_worker_gitea_token is unset/placeholder"

seeds = sys.argv[1]
src = plaintext(seeds)
new, n = re.subn(r'\{"gitea_pat": "[^"]*"\}', json.dumps({"gitea_pat": pat}), src)
assert n == 1, f"expected exactly 1 gitea_pat value in {seeds}, found {n}"
pathlib.Path(seeds).write_text(new)
print(f"substituted 1 gitea_pat value, length {len(pat)}")   # NAME + length only, never the value
PY

sops -e -i "$SEEDS"
sops filestatus "$SEEDS"                       # must now be {"encrypted": true}
grep -E '^\s+common\.json: ENC\[' "$SEEDS"     # the value line must be ciphertext
git diff --stat -- "$SEEDS"                    # only this file
```

Then commit + merge. **This subtree's seed is authoritative** (`bao kv patch`, seed-wins — not the
operator seed's create-if-absent floor; see the precedence note under *What this is*): whatever is in
this file overwrites live KV on the next daily run, so rotating the PAT in the vault alone is
silently reverted (see *Rotation*).

### (d) Verify the provision Job

```bash
kubectl --context admin@ai -n openbao get job openbao-devworker-provision \
  -o jsonpath='{.status.succeeded}{"\n"}'                       # expect 1
kubectl --context admin@ai -n openbao logs job/openbao-devworker-provision | tail -5
#    last line: devworker provision complete
```

A red Job with `CreateContainerConfigError` almost always means the `openbao-breakglass-token`
Secret is missing or its data key is not `root_token` — the reference is `optional: false` on
purpose, so this fails loudly instead of skipping.

### (e) Mint each worker's secret-id (the one hand ceremony)

Run from the operator workstation (Git Bash or WSL both work). Two shells.

> The minted secret-ids are **not** machine-bound (see the note under the worker table). Keep them in
> 0600 files, move them into SOPS in the same session, and delete the temp files afterwards — the
> steps below do exactly that.

**Shell 1 — the tunnel** (leave it running):

```bash
kubectl --context admin@ai -n openbao port-forward svc/openbao 8200:8200
```

**Shell 2 — set up TLS + the mint directory.** The port-forward makes the server answer on
`127.0.0.1`, which is not on the cert, so pin the SNI name to a real SAN (`BAO_TLS_SERVER_NAME`)
instead of reaching for `BAO_SKIP_VERIFY` — the CA is public and available:

```bash
umask 077
MINT="$HOME/.openbao-mint"; mkdir -p "$MINT"

# ca.crt is the PUBLIC ailab-root-ca certificate — safe to write to disk.
kubectl --context admin@ai -n openbao get secret openbao-tls -o jsonpath='{.data.ca\.crt}' \
  | base64 -d > "$MINT/ailab-root-ca.crt"

export BAO_ADDR="https://127.0.0.1:8200"
export BAO_CACERT="$MINT/ailab-root-ca.crt"
export BAO_TLS_SERVER_NAME="openbao.openbao.svc"   # a SAN on openbao-tls; 127.0.0.1 is not
```

The breakglass token is passed **inline on each command** (env prefix, never `export`ed, never
echoed, never in argv). Confirm it works first:

```bash
BAO_TOKEN="$(kubectl --context admin@ai -n openbao get secret openbao-breakglass-token \
  -o jsonpath='{.data.root_token}' | base64 -d)" \
  bao token lookup -format=json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["data"]; print("policies:", d["policies"], "ttl:", d["ttl"])'
#    expect policies including "root" and ttl 0 (never expires)
```

Mint role-id + secret-id for all six, straight into 0600 files:

```bash
for n in 1 2 3 4 5 6; do
  h="dev-worker-$n"
  BAO_TOKEN="$(kubectl --context admin@ai -n openbao get secret openbao-breakglass-token \
    -o jsonpath='{.data.root_token}' | base64 -d)" \
    bao read -field=role_id "auth/approle/role/$h/role-id" > "$MINT/$h.role-id"
  BAO_TOKEN="$(kubectl --context admin@ai -n openbao get secret openbao-breakglass-token \
    -o jsonpath='{.data.root_token}' | base64 -d)" \
    bao write -f -field=secret_id "auth/approle/role/$h/secret-id" > "$MINT/$h.secret-id"
done
wc -c "$MINT"/*.role-id "$MINT"/*.secret-id     # LENGTHS only — never `cat` these files
```

Fold them into the SOPS secrets file (again: no value through the terminal):

```bash
export SOPS_AGE_KEY_FILE="$(pwd)/kubernetes/infra/_out/age.agekey"
python3 - <<'PY'
import os, pathlib, subprocess, yaml
# APPEND rather than round-trip the parsed document: a yaml.safe_dump rewrite would silently drop
# every comment in the file (it documents which toggle each secret belongs to).
p = pathlib.Path("ansible/secrets/dev-worker.sops.yaml")
src = subprocess.run(["sops", "-d", str(p)],
                     capture_output=True, text=True, check=True).stdout
assert "dev_worker_openbao_credentials" not in src, "key already present — edit in place instead"
mint = pathlib.Path(os.environ["HOME"]) / ".openbao-mint"
block = {"dev_worker_openbao_credentials": {
    f"dev-worker-{n}": {
        "role_id":   (mint / f"dev-worker-{n}.role-id").read_text().strip(),
        "secret_id": (mint / f"dev-worker-{n}.secret-id").read_text().strip(),
    } for n in range(1, 7)}}
p.write_text(src.rstrip("\n") + "\n\n"
             + "# OpenBao AppRole logins per worker (ADR 0020). Minted by the ceremony in\n"
             + "# docs/runbooks/openbao-dev-workers.md; they exist ONLY here and on the worker.\n"
             + yaml.safe_dump(block, default_flow_style=False, sort_keys=False))
print("appended", len(block["dev_worker_openbao_credentials"]), "worker entries")   # count only
PY

sops -e -i ansible/secrets/dev-worker.sops.yaml
sops filestatus ansible/secrets/dev-worker.sops.yaml     # {"encrypted":true}
sops -d ansible/secrets/dev-worker.sops.yaml \
  | python3 -c 'import sys,yaml; c=yaml.safe_load(sys.stdin)["dev_worker_openbao_credentials"]; print(sorted(c), sorted(next(iter(c.values()))))'
#    expect the 6 hostnames and the 2 field names — values never printed
rm -f "$MINT"/*.role-id "$MINT"/*.secret-id
```

The `.sops.yaml` `dev-worker` creation rule encrypts `dev_worker_openbao_.*`, and a matching branch
key encrypts the whole subtree below it — so every `role_id`/`secret_id` is ciphertext. **Check the
diff before committing:** `git diff -- ansible/secrets/dev-worker.sops.yaml` must show only added
`ENC[...]` lines, with no plaintext value anywhere.

Re-minting a **single** worker later hits the `assert` above (the key now exists). Edit that one
entry in place instead — `EDITOR=... sops edit ansible/secrets/dev-worker.sops.yaml` — pasting the
new value from its 0600 mint file.

### (f) Enable the role path

In `ansible/group_vars/dev_workers.yml`, flip:

```yaml
dev_worker_enable_openbao: true
```

Then run the playbook. `just dev-workers` is the Windows-side entry point (it sets
`SOPS_AGE_KEY_FILE` for you). **From WSL, invoke `ansible-playbook` directly and set `ANSIBLE_CONFIG`
explicitly** — `/mnt/c` is world-writable, so an implicit `ansible.cfg` is silently ignored, which
drops the inventory and "deploys" to zero hosts (the same trap called out in the ci-runners and herdr
notes in `docs/runbooks/dev-workers.md`):

```bash
just dev-workers          # all six, full role
# targeted (one host, just this feature):
cd ansible && ANSIBLE_CONFIG="$(pwd)/ansible.cfg" SOPS_AGE_KEY_FILE=../kubernetes/infra/_out/age.agekey \
  ansible-playbook dev-workers.yml -l dev-worker-1 -t openbao
```

Roll it out **one host first** (`-l dev-worker-1`), verify with (g), then the rest — the failure mode
if a secret-id is wrong is a worker whose `~/.git-credentials` stops being maintained.

Run it twice — the second run should report near-zero `changed` (the role's idempotency contract).

### (g) Verify on a worker

```bash
ssh c4@192.168.0.8

systemctl status openbao-agent                 # active (running), no restart loop
sudo journalctl -u openbao-agent -n 30         # "authentication successful" / "renewed auth token"
ls -l /run/openbao-agent/token                 # root:openbao-agent, group-readable
id c4 | tr ' ' '\n' | grep openbao-agent       # c4 must be in the group (log out/in after first run)

cred list                                      # names visible to this worker
cred get common gitea_pat | wc -c              # LENGTH only — never print the value
ls -l ~/.git-credentials                       # 0600, owned by the user, rendered by the agent
git ls-remote https://git.chifor.me/cchifor/ailab.git HEAD >/dev/null && echo "forge auth ok"
```

`cred get … | wc -c` is the standard smoke test: it proves the whole chain (sink token → LAN NodePort
→ TLS → AppRole policy → KV read) without a value reaching the terminal.

## Rotation

**A seeded KV value (e.g. the Gitea PAT).** Two writes, one change — or the daily Job reverts you:

1. Mint the new PAT in Gitea.
2. Update `ansible/secrets/dev-worker.sops.yaml` (`dev_worker_gitea_token`) **and**
   `devworker-seeds.sops.yaml` (step (c) above re-runs unchanged and does both consistently).
3. Merge. The next `openbao-devworker-provision` run patches KV; each worker's `bao agent` re-renders
   `~/.git-credentials` when the template's source value changes.
4. To make it immediate rather than within a day: delete the Job and let Flux recreate it
   (`kubectl --context admin@ai -n openbao delete job openbao-devworker-provision`, then annotate the
   `openbao` Kustomization to reconcile).

**A worker's secret-id.** Re-run the mint for that one host, then re-run the playbook for it. To
invalidate the old one (do this whenever a worker is rebuilt or a secret-id may have leaked):

```bash
# list the accessors on the role (accessors are safe to print — they are not credentials)
BAO_TOKEN="$(kubectl --context admin@ai -n openbao get secret openbao-breakglass-token \
  -o jsonpath='{.data.root_token}' | base64 -d)" \
  bao list auth/approle/role/dev-worker-1/secret-id

# destroy the stale one by accessor
BAO_TOKEN="$(kubectl --context admin@ai -n openbao get secret openbao-breakglass-token \
  -o jsonpath='{.data.root_token}' | base64 -d)" \
  bao write auth/approle/role/dev-worker-1/secret-id-accessor/destroy \
    secret_id_accessor=<accessor>
```

Order matters: mint + deploy the new one **first**, then destroy the old — the agent only re-logs in
on restart or token loss, so destroying first leaves that worker fine until its next reboot and then
broken.

**A role or policy** (capabilities, `token_period`, adding a bound-CIDR once source IPs survive the
hop — ADR 0020 follow-up). Edit the provision script in
`devworker-provision-job.yaml`, merge; the Job converges within a day, or force it as in Rotation
step 4. When it takes effect differs by what changed: a **policy body** edit (the path/capability
lines) applies **immediately** — tokens carry the policy by *name* and it is evaluated on every
request, so no agent restart is needed. A **role** change (`token_period`, the `token_policies`
*list*) is stamped into tokens at issuance — a running agent keeps renewing its existing token, so
restart `openbao-agent` on the workers to force a fresh login that picks it up.

## Failure modes

| Symptom | Cause | Action |
|---|---|---|
| `cred: /run/openbao-agent/token not readable` | The user is not in the `openbao-agent` group, or the agent is not running. | `id <user>`; log out/in after the first playbook run (group membership is per-session). Else `systemctl status openbao-agent`. |
| Agent logs `invalid role or secret ID` (400) | The secret-id was destroyed/rotated, or the vault was wiped (all secret-ids die with it), or `/etc/openbao-agent/secret-id` never got the real value. | Re-mint (e) for that host and re-run the playbook. Check the role still exists: `bao read auth/approle/role/dev-worker-N`. |
| Agent logs TLS/x509 errors | The server has not restarted since the SAN was added, or `ailab-root-ca.crt` is missing on the host. | Re-run activation (b); check `/usr/local/share/ca-certificates/ailab-root-ca.crt` + `update-ca-certificates`. |
| Connection refused / timeout to `openbao.lan.chifor.me:30820` | `/etc/hosts` block missing, or all three nodes down, or the Service was removed. | `getent hosts openbao.lan.chifor.me`; `kubectl --context admin@ai -n openbao get svc openbao-lan`. |
| `openbao-lan` has **no endpoints** | The vault is **sealed** — the `openbao-active` label is absent, by design (a sealed vault is unreachable rather than answering 503s). | Unseal: check the unsealer Deployment (`docs/runbooks/openbao-recovery.md`). |
| Agent dies and stays dead | `exit_on_retry_failure = true` on template rendering — a missing KV path is fatal by design. | `journalctl -u openbao-agent`; usually `af/dev-workers/common` is missing its field → check the provision Job ran. |
| Token silently expires | Should be impossible: the role issues **periodic** tokens, whose TTL resets on renewal (ADR 0020 — the antidote to the 2026-08-25 768h lockout). If it happens anyway, the role lost `token_period`. | `bao read auth/approle/role/<host>` and compare with the provision script. |
| Provision Job red, `CreateContainerConfigError` | `openbao-breakglass-token` missing, or its data key is not `root_token`. | `kubectl --context admin@ai -n openbao get secret openbao-breakglass-token -o jsonpath='{.data}'` piped through a key-name print — **never** `-o yaml`. |

**After an OpenBao wipe + re-bootstrap** (`docs/runbooks/openbao-recovery.md`): the daily Job
re-creates the `approle` mount, all six policies/roles, and re-seeds `af/dev-workers/*` from
`devworker-seeds.sops.yaml` — **but every secret-id is invalidated**, because AppRole state lives in
the storage that was wiped. Re-run the mint ceremony (e) for all six workers and re-run the playbook.
Until then the agents log `invalid role or secret ID` and `~/.git-credentials` goes stale (it is not
deleted, so git keeps working on the old PAT until that is rotated too).

## Adding a secret

1. Decide the scope: shared → `af/dev-workers/common`; single worker → `af/dev-workers/<hostname>`.
   No policy change is needed for either — the per-worker policy already covers both subtrees.
   How `cred` reaches each shape (it probes `dev-workers/<hostname>/<name>` first, then
   `dev-workers/<name>`): a shared field is `cred get common <field>`; a field seeded at
   `af/dev-workers/<hostname>` is `cred get <hostname> <field>` (the host-first probe misses and the
   second probe is exactly that path); a deeper ad-hoc write to `af/dev-workers/<hostname>/<name>`
   is `cred get <name> <field>` and shadows any shared secret of the same name on that host.
   Note `cred list` shows sibling workers' path **names** (the policy's metadata `list` is
   subtree-wide); their values stay denied — don't put anything secret in a path name.
2. Write it. Ad-hoc (does **not** survive a wipe):
   `bao kv patch -mount=af dev-workers/common <field>=@/path/to/file` with the breakglass token, using
   the port-forward setup from (e). Prefer `@file` over an inline `field=value` so the value never
   enters argv or shell history.
3. To make it durable, add the field to `devworker-seeds.sops.yaml` in the same change (step (c)'s
   script pattern) — otherwise a wipe loses it and the seed contract will not restore it. That file
   holds **one `stringData` key per KV path**: `common.json` → `af/dev-workers/common`, and a
   per-worker path is added by dropping in e.g. `dev-worker-3.json`. The provision script loops over
   `*.json`, so no script change is needed for a new path.
4. File-shaped consumers (something that needs a rendered file rather than a `cred` call) need a new
   `template` stanza in `ansible/roles/dev_worker/templates/openbao-agent.hcl.j2` plus a `.ctmpl`;
   everything else is reachable via `cred get` / `cred exec` with no role change.

## Adding a worker

1. tofu + ansible for the VM itself (`docs/runbooks/dev-workers.md`).
2. Add the hostname to the `for host in dev-worker-1 …` list in the provision script inside
   `devworker-provision-job.yaml` (one word), and the row to the table in this runbook. Merge; the
   Job creates the policy + role.
3. Mint its secret-id (ceremony (e), one host) and add it to `dev_worker_openbao_credentials`.
4. Run the playbook for that host.

The provision script's host list, `inventory/hosts.yml`, and the table above are three copies of the
same fact — change them together.
