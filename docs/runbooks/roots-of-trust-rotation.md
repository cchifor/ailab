# Runbook — roots of trust: age key, Kubernetes ServiceAccount signing key, Keycloak realm keys

**When to use:** a scheduled rotation of one of the three roots of trust below, or a suspected exposure of one of them.
Each section states the holders, the order, the overlap, the user impact, the verification and the rollback. Written
for the auth-hardening plan (`plans/2026-10-07-auth-hardening-and-ops-plan.md`, item A5; decisions D7 and D8).

> Nothing here is executed by reading it, and none of it is a routine task: rotate **one root at a time**, in a quiet
> window, with the owner (`chifor`) in person. The Strive-side credentials (the gatekeeper signing key, the test-bypass
> token, preshared S2S secrets, the Fernet keys) are in the **platform** repository: `docs/runbooks/gatekeeper-key-rotation-ailab.md`
> and `docs/runbooks/ailab-credential-rotation.md` (added by the platform PR of the same plan).

Context for every command: `kubectl --context admin@ai` (the default context is a different cluster), Windows
workstation, Git Bash, the main checkout `C:\Users\chifo\work\home\ailab` for `kubernetes/infra/_out/` (gitignored,
absent from a git worktree; resolve it with `"$(cd "$(git rev-parse --git-common-dir)/.." && pwd -P)"`).

## Rules for every procedure here

- **No secret value on a screen, in a log, a commit message or a PR.** Keys are read from files with restricted
  permissions or piped; print only names, counts or the first 12 hex of a sha256.
- **Never `git revert` an `.enc.yaml` or `.sops.yaml` data file** once another commit has touched it since (a rotation, a
  re-encryption, a recipient change): the old ciphertext would undo those changes or no longer decrypt. Edit the *current*
  file with `sops` (`set`, `unset`, `updatekeys`, `rotate`), changing only the intended key or recipient, and recompute any
  checksum from the new ciphertext (`gatekeeper.serviceRegistry.checksum` in platform, below).
- **One platform-wide change per day.** A5.2 touches both repositories' Secrets and runs alone.
- Artefacts that could hold a secret go to `kubernetes/infra/_out/` of the main checkout, restricted to the user
  (`icacls <path> /inheritance:r /grant:r "%USERNAME%:F"`), and are deleted after use unless a step keeps them. Before the
  first such step, confirm `_out/` is not inside a synced or backed-up folder.
- A rotation does **not** revoke what the old key opened in the past: ciphertexts in git history (and in backups) stay
  readable by whoever holds the old key. After a suspected compromise, every value behind the root must be rotated too.

---

# 1. The age key (SOPS root) — decision D8 (a): dual-recipient rotation

## What it is, who holds it

One age recipient, `age1g0exjqgq9l52m8g7hqkqelc3kcam4wwlkngzjhn5wv6dzxfjdceqdyfl4t` (since the 2026-10-08 rotation; see "Rotation record" below; the
retired one was `age1nfa6hhdz...`), is the recipient of **every** SOPS
file of this repository (73 encrypted files on 2026-10-07; the "74" of the plan counts `.sops.yaml` itself, which is
configuration, not ciphertext) and of the 23 platform `deploy/secrets/ailab/*.enc.yaml`. The cluster decrypts several other repositories with the same Flux key
(cloudlab's `.sops.yaml` is recorded as naming the same recipient in `kubernetes/apps/clusters/ai/cloudlab.yaml`; step 0 checks the rest).
The private key is one file and lives in several places:

| Holder (as read on 2026-10-07) | Notes | Step |
| --- | --- | --- |
| `kubernetes/infra/_out/age.agekey` of the main ailab checkout | The fixed path read by `justfile` recipes, `scripts/fleet-converge-daily.sh` (as `/mnt/c/Users/chifo/work/home/ailab/kubernetes/infra/_out/age.agekey`, from WSL, on a schedule), the runbooks, `CLAUDE.md` and `README.md` | 1, 2, 6 |
| `~/work/keys/age.agekey` | A second workstation copy. `docs/runbooks/openbao-estate-credentials.md` §2 says delete it only once an offline copy is confirmed | 0, 6 |
| An offline copy (removable media / password manager) and a Vaultwarden copy | `kubernetes/infra/openbao-unseal.sops.yaml` and `talos-secrets-bundle.sops.yaml` say the decrypting key must be kept offline (the unseal file also names a Vaultwarden copy): both files are DR-only ciphertexts that are useless without it | 0, 5 gate, 6 |
| Secret `flux-system/sops-age` (one key, `age.agekey`; hand-applied) | Read by the kustomize-controller for every Kustomization with `decryption.secretRef.name: sops-age` | 2, 4, 6 |
| Gitea Actions secret `SOPS_AGE_KEY` (org scope per `docs/runbooks/ci-runners.md`; platform `contract.yml` reads it through `scripts/load-secrets.sh`) | **Write-only**: the owner confirms in the UI which key it holds. Platform's `.sops.yaml` also has rules for a different recipient (`age1wuvg...`: `secrets/`, dev, staging, prod), so it may not be this one | 0, 5 |
| Other repositories decrypted with `sops-age` | Kustomizations with that secret, by source: `cloudlab` (`cloudlab-monitoring`), `trueswarm-admin` (`trueswarm`, `trueswarm-admin-foundation`), `muse-stream`, `agentforge-tenants`, `platform`, and this repository. `kubernetes/apps/clusters/ai/cloudlab.yaml` records that cloudlab's `.sops.yaml` names the same recipient | 0, 3, 4, 5 |
| Workstation backups, `~/.config/sops/age/keys.txt`, `%APPDATA%\sops\age\keys.txt`, any `SOPS_AGE_KEY*` environment variable, the dev-worker and reviewer VMs | Not inventoried; step 0 finds them | 0 |

Different keys, **not** rotated here: `kubernetes/infra/_out/talos-backup-age.key` (public `age13ruz38k...`, etcd snapshots;
see `docs/runbooks/openbao-estate-credentials.md`) and platform's `age1wuvg...` recipient.

## Tooling facts (Windows Git Bash checked 2026-10-07 with sops 3.9.4; WSL re-checked 2026-10-08 with sops 3.13.1)

- WSL (`Ubuntu`) has no internet. Since 2026-10-08 it has sops 3.13.1 at `~/.local/bin/sops` (in a login shell:
  `wsl.exe -e bash -l`; capture `SOPS=$(command -v sops)` before any `env -i`). The 2026-10-08 rotation used the WSL sops
  for **every** repository: the rules of platform, muse-stream and trueswarm-admin use `/`, which the Windows sops does
  not match (next bullet). Generate keys and run git on Windows (`age-keygen` is Windows-only).
- This repository's `.sops.yaml` rules use `[/\\]` separators, so they match on Windows and Linux. **Platform's anchored
  rule `^deploy/secrets/ailab/.*\.enc\.yaml$` does not match on Windows**: `sops updatekeys` and `sops encrypt` answer
  `no matching creation rules found` because sops sees `deploy\secrets\ailab\...`. For the platform half, either run a Linux
  sops, or change the rule in the same PR to `deploy[/\\]secrets[/\\]ailab[/\\].*\.enc\.yaml$` (no `^`).
- `sops updatekeys -y` keeps the data key; `sops rotate -i --rm-age <old>` removes the recipient **and** generates a new data
  key. Both preserve the file's own `encrypted_regex`. A file with an old data key stays decryptable by anyone who recovered
  that data key from git history, which is why step 5 rotates.
- An age identity file may hold several `AGE-SECRET-KEY-` lines: sops tries all of them.

## Step 0 — inventory the holders (names only, read-only) and rehearse

1. Record every holder in the table above plus whatever this finds, then decide the end state of each. **Step 5 waits until
   every holder is updated or shown to use another recipient.**

   ```bash
   # Kustomizations that decrypt with sops-age, and the repository each one reads (names only):
   kubectl --context admin@ai get kustomization -A -o json | jq -r '.items[] | select(.spec.decryption.secretRef.name=="sops-age") | [.metadata.name, .spec.sourceRef.name, .spec.path] | @tsv'
   # Secrets in the cluster that hold an age identity (by key name):
   kubectl --context admin@ai get secret -A -o json | jq -r '.items[] | select((.data // {}) | keys | any(test("agekey|age[.]key|keys[.]txt|age-key"))) | .metadata.namespace + "/" + .metadata.name'
   # Repositories: every file naming the recipient, and every encrypted file (this repo: expect 73):
   # The current recipient, from the key file. Refuse an empty or multi-line answer: `git grep -l ""` would list every file.
   # Run on Windows (age-keygen is Windows-only). The cross-check fails when this runbook's "What it is" is stale.
   R=$(age-keygen -y <main checkout>/kubernetes/infra/_out/age.agekey | tr -d '\r')
   [ -n "$R" ] && [ "$(printf '%s\n' "$R" | wc -l)" = 1 ] && grep -qF "$R" docs/runbooks/roots-of-trust-rotation.md \
     && git grep -l -F "$R" || echo "STOP: no single recipient read, or it is not the one this runbook names"
   git grep -l -E '^sops:' -- '*.sops.yaml' ':!*.example' | grep -v '^\.sops\.yaml$' | wc -l
   # Workstation and VMs (paths only):
   ls -la ~/.config/sops/age/ ~/work/keys/ 2>/dev/null; env | cut -d= -f1 | grep -i '^SOPS_AGE'
   ```

   On 2026-10-07 the first command lists Kustomizations on the sources `flux-system` (this repo), `platform`, `cloudlab`,
   `muse-stream`, `agentforge-tenants` and `trueswarm-admin`, and the second lists only `flux-system/sops-age`. For every
   other repository in the first list, open its `.sops.yaml` and its encrypted files: the ones that name the old recipient
   need steps 3 and 5 too, or step 4's check fails for their Kustomizations (and removing the old identity there would break
   them).
2. Offline copies: the **new** key needs the same offline and Vaultwarden homes the old one has, before step 5 (the DR
   files below are ciphertexts to it). Plan the copy now.
3. **Scratch rehearsal (10 minutes) of steps 3-5 on a throwaway file.** Everything below is the exact sequence of the real
   steps, on dummy keys and a dummy secret, outside the repository:

   ```bash
   R=$(mktemp -d) && cd "$R" && mkdir -p deploy/secrets/ailab empty-home
   age-keygen -o old.agekey 2>/dev/null; age-keygen -o new.agekey 2>/dev/null; age-keygen -o unrelated.agekey 2>/dev/null
   OLD=$(age-keygen -y old.agekey); NEW=$(age-keygen -y new.agekey)
   printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: t\nstringData:\n  k: v\n' > plain.yaml
   export SOPS_AGE_KEY_FILE=$PWD/old.agekey
   sops encrypt --encrypted-regex '^(data|stringData)$' --age "$OLD" --input-type yaml --output-type yaml plain.yaml > deploy/secrets/ailab/t.enc.yaml; rm plain.yaml
   # A .sops.yaml with BOTH recipients (step 3); note the separator class and no ^ anchor:
   printf 'creation_rules:\n  - path_regex: deploy[/\\\\]secrets[/\\\\]ailab[/\\\\].*\\.enc\\.yaml$\n    age: "%s,%s"\n' "$OLD" "$NEW" > .sops.yaml
   sops updatekeys -y deploy/secrets/ailab/t.enc.yaml
   SOPS=$(command -v sops)
   E() { env -i PATH=/usr/bin:/bin HOME="$R/empty-home" XDG_CONFIG_HOME="$R/empty-home" SOPS_AGE_KEY_FILE="$R/$1" "$SOPS" decrypt "$2" > /dev/null 2>&1; echo "$1 exit=$?"; }
   E unrelated.agekey deploy/secrets/ailab/t.enc.yaml   # negative control: must be non-zero (128)
   E new.agekey       deploy/secrets/ailab/t.enc.yaml   # must be 0
   sops rotate -i --rm-age "$OLD" deploy/secrets/ailab/t.enc.yaml
   E old.agekey       deploy/secrets/ailab/t.enc.yaml   # must now be non-zero
   E new.agekey       deploy/secrets/ailab/t.enc.yaml   # must be 0
   grep -c encrypted_regex deploy/secrets/ailab/t.enc.yaml   # 1: the file kept its own regex
   cd - >/dev/null; rm -rf "$R"
   ```

   (The `printf` line doubles every backslash for printf. Check that `.sops.yaml` reads
   `deploy[/\\]secrets[/\\]ailab[/\\].*\.enc\.yaml$`.) If any line does not behave as commented, stop: the rehearsal is the
   gate for the real run.

## Step 1 — generate the new key

The rotation spans days and several PRs, so no single shell will last. Every step uses these variables; **paste this block at the
top of each step in a fresh shell** (set `D` to the date of step 1; it re-derives everything from the files and prints STOP if the new key
file is missing):

```bash
cd <main checkout>/kubernetes/infra/_out
D=<yyyymmdd of step 1>; OLD=<the recipient being retired: the current one, from step 0>
WORK=$PWD/age-rotation-$D; NEWFILE=$PWD/age-$D.agekey; umask 077
NEW=$(age-keygen -y "$NEWFILE" 2>/dev/null); [ -n "$NEW" ] || echo "STOP: $NEWFILE is missing or unreadable; do not continue (the loops would run with an empty recipient)"
```

Then, once, for step 1 itself (on this very first run the block above prints STOP until the key exists; run it again after `age-keygen`):

```bash
mkdir -p "$WORK"
age-keygen -o age-$D.agekey 2>&1 | sed 's/^Public key: /new recipient: /'           # prints the public key only (re-run the block above afterwards)
MSYS_NO_PATHCONV=1 icacls age-$D.agekey /inheritance:r /grant:r "$USERNAME:F"       # (or run icacls from PowerShell)
[ -e age-old.agekey ] || cp age.agekey age-old.agekey     # a plain backup of the old key for rollback (never overwritten: a re-run would copy the merged file); deleted in step 6
```

**Keep every fixed-path reader working through the whole rotation.** After step 5 the files open only with the new key, but
`justfile` recipes and the scheduled `scripts/fleet-converge-daily.sh` read `_out/age.agekey` and would fail until step 6. So
make `age.agekey` hold **both** identities now (sops tries each line), and reduce it to the new key in step 6:

```bash
{ cat age-old.agekey; echo; cat "$NEWFILE"; } > age.agekey.tmp && mv age.agekey.tmp age.agekey
```

Check: `SOPS_AGE_KEY_FILE=$PWD/age.agekey sops decrypt <any one file> >/dev/null` still exits 0. Rollback: `cp age-old.agekey age.agekey`.

## Step 2 — add the new key as a second identity in `flux-system/sops-age`

The kustomize-controller reads every `*.agekey` key of the Secret. Build the Secret from files, never from the command line:

```bash
kubectl --context admin@ai -n flux-system create secret generic sops-age \
  --from-file=age.agekey=age-old.agekey --from-file=age-$D.agekey="$NEWFILE" --dry-run=client -o yaml \
  | kubectl --context admin@ai replace -f -
kubectl --context admin@ai -n flux-system get secret sops-age -o json | jq -r '.data | keys[]'     # exactly: age-<D>.agekey and age.agekey
```

`replace` writes the Secret wholesale, so a key that is not in the new object is gone (`apply` on a Secret that was applied by hand
only removes keys when a last-applied annotation exists). Check: the key **names** above, and every SOPS Kustomization stays Ready
(the loop of step 4, without the wait for a fresh reconcile). Rollback: replace with only the old key.

## Step 3 — add the new recipient next to the old one and re-key every file

1. In **this** repository: all seven rules of `.sops.yaml` name the recipient. Add the new one beside it, then re-key:

   ```bash
   sed -i "s/^\( *age: \)$OLD\$/\1$OLD,$NEW/" .sops.yaml && grep -c "$NEW" .sops.yaml     # 7
   export SOPS_AGE_KEY_FILE=<main checkout>/kubernetes/infra/_out/age.agekey   # holds both identities since step 1
   # paste the variable block of step 1 first (it sets OLD, NEW, NEWFILE, WORK, D), then cd to this repository's root
   git grep -l -E '^sops:' -- '*.sops.yaml' ':!*.example' | grep -v '^\.sops\.yaml$' > "$WORK/ailab-files.txt"; wc -l < "$WORK/ailab-files.txt"   # 73
   while read -r f; do sops updatekeys -y "$f" > /dev/null 2>&1 || echo "FAILED $f"; done < "$WORK/ailab-files.txt"
   ```

2. In **platform**: the ailab rule of its `.sops.yaml` (`age: "<old>"` becomes `age: "<old>,<new>"`; make the path separator class tolerant if you use
   the Windows sops, see Tooling facts), then `sops updatekeys -y` on the 23 `deploy/secrets/ailab/*.enc.yaml`. The change to
   `gatekeeper-secrets.enc.yaml` changes its ciphertext, so `gatekeeper.serviceRegistry.checksum` in
   `deploy/helm/values/providers/ailab.yaml` must be bumped in the same PR (sha256 of the staged blob: `git show :deploy/secrets/ailab/gatekeeper-secrets.enc.yaml | sha256sum`),
   which rolls gatekeeper. The PR touches `deploy/secrets/ailab/**` (owner merge) and `ailab.yaml` (`approve-pin`).
3. In every **other repository** found in step 0 that names the old recipient: the same two edits, in that repository's own PR.
4. **Check each file decrypts with the new key alone, in an emptied environment, with the negative control first.** One sops
   binary; `env -i` drops `SOPS_AGE_KEY`, `SOPS_AGE_KEY_CMD`, any SSH-key variable and `%APPDATA%`, so no other identity source
   is reachable:

   ```bash
   SOPS=$(command -v sops); EMPTY=$(mktemp -d)
   age-keygen -o "$WORK/unrelated.agekey" 2>/dev/null
   chk() { env -i PATH=/usr/bin:/bin HOME="$EMPTY" XDG_CONFIG_HOME="$EMPTY" SOPS_AGE_KEY_FILE="$1" "$SOPS" decrypt "$2" > /dev/null 2>&1; }
   # Negative control FIRST: the unrelated key must FAIL on every file, which proves no other identity source is reachable.
   bad=0; while read -r f; do chk "$WORK/unrelated.agekey" "$f" && { echo "NEGATIVE CONTROL OPENED $f"; bad=1; }; done < "$WORK/ailab-files.txt"; echo "control leaks: $bad"
   # Then the new key alone:
   fail=0; while read -r f; do chk "$NEWFILE" "$f" || { echo "NEW KEY FAILS $f"; fail=1; }; done < "$WORK/ailab-files.txt"; echo "new-key failures: $fail"
   ```

   Both must print 0. A non-zero first line means the check proves nothing: find the leaking identity source before going on.
   Run the same loop over platform's 23 files and every other repository's files.

Rollback of step 3: `sops rotate -i --rm-age <new>` on each file (or restore the previous recipient lists and `updatekeys` again); never `git revert`
the re-keying PR once any other commit has touched those files.

## Step 4 — prove Flux with the new key alone

Remove the old identity from `sops-age`, force-reconcile every SOPS Kustomization, and require all Ready. **This is only
valid once step 3 has re-keyed every repository those Kustomizations read** (step 0's list).

```bash
kubectl --context admin@ai -n flux-system create secret generic sops-age \
  --from-file=age-$D.agekey="$NEWFILE" --dry-run=client -o yaml | kubectl --context admin@ai replace -f -
kubectl --context admin@ai -n flux-system get secret sops-age -o json | jq -r '.data | keys[]'     # must print ONLY age-<D>.agekey: the proof the old identity is gone
T=$(date +%s)
for k in $(kubectl --context admin@ai -n flux-system get kustomization -o json | jq -r '.items[] | select(.spec.decryption.secretRef.name=="sops-age") | .metadata.name'); do
  kubectl --context admin@ai -n flux-system annotate --overwrite kustomization/$k reconcile.fluxcd.io/requestedAt="$T" > /dev/null
done
sleep 90
kubectl --context admin@ai -n flux-system get kustomization -o json | jq -r --arg t "$T" '.items[] | select(.spec.decryption.secretRef.name=="sops-age") | [.metadata.name, (.status.lastHandledReconcileAt // "-"), ((.status.lastHandledReconcileAt // "") == $t), (.status.conditions[]? | select(.type=="Ready") | .status)] | @tsv'
```

Every row must read `<name>  <T>  true  True` (handled this reconcile, Ready). A Kustomization on a repository you did not re-key shows
`False` with a decryption error: replace `sops-age` with both keys (the step 2 command) and finish step 3 for that repository.
The Flux sources of this estate are the GitHub push-mirrors for ailab (interval 1 m) and platform (10 m); a re-keyed commit
reaches the cluster after the mirror sync, so run step 4 after `lastAppliedRevision` of each Kustomization is the re-keying
commit (`kubectl ... get kustomization <name> -o jsonpath='{.status.lastAppliedRevision}'`).

## Gate before step 5

All of: every holder of step 0 updated or shown to use another recipient; the **new key has its offline and Vaultwarden
copies** (the DR files are ciphertexts to it from step 5 on); **the Gitea Actions secret `SOPS_AGE_KEY`, if it holds this key, already
carries the new key** (the owner sets it in the UI now: after step 3 every file accepts both recipients, so CI keeps working, but the
step 5 PRs re-key the files to the new recipient only, and their own CI would fail with the old key in the secret); step 4 green; no pending drill. Step 5 is the first step that
cannot simply be undone by editing `.sops.yaml`.

## Step 5 — remove the old recipient and rotate each file's data key

`updatekeys` keeps the data key, which an old-key holder can recover from git history; `rotate` replaces it:

```bash
while read -r f; do sops rotate -i --rm-age "$OLD" "$f" > /dev/null 2>&1 || echo "FAILED $f"; done < "$WORK/ailab-files.txt"
sed -i "s/^\( *age: \)$OLD,$NEW\$/\1$NEW/" .sops.yaml && grep -c "$OLD" .sops.yaml     # 0
```

Then, per repository: remove the old recipient from `.sops.yaml` (platform: `age: "<new>"`), bump the gatekeeper checksum again in
platform (the ciphertext changed again), PR, owner merge. Repeat the step 3 check loops with the **old** key as the "must fail" key
(`SOPS_AGE_KEY_FILE=age-old.agekey` must now exit non-zero on every file) and the new key as the "must pass" key.

**Drill 2 ordering.** There is none: the S2S rollback drill (C4, C8) never applies a historical ciphertext; it edits the *current* file
with `sops` and reads the pre-#2125 registry entries locally from git history, which the old key opens. After step 6 that key
is archived (below); restore it temporarily with `age -d -i <new key> _out/age-retired-<date>.agekey.age > <temp file>`.

## Step 6 — the fixed path, the archive, the cleanup

1. The new key becomes the fixed path, and the Flux Secret returns to the documented key name `age.agekey`:

   ```bash
   cd <main checkout>/kubernetes/infra/_out
   cp "$NEWFILE" age.agekey.tmp && mv age.agekey.tmp age.agekey              # CLAUDE.md, README.md and tooling stay true
   kubectl --context admin@ai -n flux-system create secret generic sops-age --from-file=age.agekey=age.agekey --dry-run=client -o yaml \
     | kubectl --context admin@ai replace -f -
   kubectl --context admin@ai -n flux-system get secret sops-age -o json | jq -r '.data | keys[]'     # must print only age.agekey
   ```

   Re-run the annotate-and-check part of step 4 (not its `create secret`): all Ready. The Gitea `SOPS_AGE_KEY` secret was already moved at the gate before step 5. Update `~/work/keys/age.agekey` (or delete it per
   `openbao-estate-credentials.md`) and confirm the offline and Vaultwarden copies hold the new key.
2. Archive the old key encrypted to the **new** recipient, verify it, then delete the plaintext:

   ```bash
   age -r "$NEW" -o age-retired-$D.agekey.age age-old.agekey
   [ "$(age -d -i age.agekey age-retired-$D.agekey.age | sha256sum | cut -c1-12)" = "$(sha256sum < age-old.agekey | cut -c1-12)" ] && echo "archive verified"
   cmp -s "$NEWFILE" age.agekey && rm -f "$NEWFILE"     # the new key must not exist twice in plaintext (age.agekey is the one copy)
   rm -f age-old.agekey && rm -rf "$WORK"               # on an SSD a delete is not an erase; the archive is the only recoverable copy
   ```

## Rollback summary

| After | Undo |
| --- | --- |
| step 1 | `cp age-old.agekey age.agekey`; delete the new key file |
| step 2 | replace `sops-age` with the old key only |
| step 3 | `sops rotate -i --rm-age <new>` per file, or remove the new recipient from `.sops.yaml` and `updatekeys` (no `git revert`) |
| step 4 | replace `sops-age` with both keys |
| step 5 | the old key still exists (`age-old.agekey`, then the archive): re-add it with `sops updatekeys -y` after listing both recipients again |
| step 6 | restore the old key from the archive with the new key; the old recipient must be re-added to every file first |

## Rotation record (2026-10-08, auth-hardening plan A5.2, decision D8)

Retired `age1nfa6hhdz...` (in service since 2026-06-14). The new recipient is `age1g0exjqgq9l52...`.

| Step | When (UTC) | What |
| --- | --- | --- |
| 0 | 2026-10-08 | Inventory of H1-H14 and the 127 files in 7 repositories (5 in scope); scratch rehearsal passed |
| 1 | 2026-10-08 | New key `_out/age-20261008.agekey` (ACL user-only); `age-old.agekey` backup; `age.agekey` held both identities |
| 2 | 17:01 | `sops-age` = both keys (`replace`, which dropped the `last-applied` copy of the key, H5); 27/27 SOPS Kustomizations Ready |
| 3 | by 19:38 | `updatekeys` to both recipients. Repositories and PRs: ailab #1152 (73 files), platform #2197 (27, with the checksum bump), cloudlab #38 (1), muse-stream #2 (1), trueswarm-admin #153 (25 files, plus `provision-foundation.py`) |
| 4 | 19:43 | `sops-age` = the new key only; all 27 handled a fresh reconcile, Ready |
| gate | by 21:31 | H6-H8 dev-worker copies deleted (no workflow used them); H9 org Actions secret `SOPS_AGE_KEY` re-set to the platform `age1wuvg...` identity only; drill 2 finished first |
| 5 | 21:49-22:05 | `sops rotate -i --rm-age <old>` with only the new key. Repositories and PRs: cloudlab #39, muse-stream #3, trueswarm-admin #155, ailab #1163 (plus the broker inventory's `seedsDocumentSha256`), platform #2201 (checksum `ef33ed12...`). In each repository the old key opens 0 files, the new key opens all of them, and an unrelated key opens 0 |
| 6 | 22:26-22:40 | `_out/age.agekey` = the new key. Its ACL keeps the owner, SYSTEM and Administrators, as before, and drops the inherited sandbox principals (`CodexSandboxUsers` had Modify); the step-1 file was owner-only. `sops-age` = the single key `age.agekey`, and all 27 reconciled fresh; the old key archived as `_out/age-retired-20261008.agekey.age` (encrypted to the new key, verified); the plaintext old key, the duplicate new-key file and the rotation scratch deleted; `~/work/keys/age.agekey` and `~/work/keys/backup/age.agekey` now hold the new key |

Left open after the rotation:

- **Owner-held copies.** The offline and Vaultwarden copies (H13) and the GitHub Actions secrets on the dormant mirrors
  (H14) must hold the new key. The workstation copies under `~/work/keys/` may be deleted once the offline copy is
  confirmed (`openbao-estate-credentials.md` §2).
- **Not re-keyed.** trueswarm (2 files) and trueswarm-test (2 files) were outside the R10 scope and are still encrypted
  to the retired key. They open only through the archive. Neither repository is read by Flux.
- **Backups.** Backups taken before step 4 (2026-10-08 19:43Z) still contain the retired key inside `sops-age`:
  - Velero: the last weekly that holds it expires on 2026-11-03 (its TTL is 720 h), and the dailies (168 h) expire
    by 2026-10-15.
  - etcd snapshots: `talos-backup`'s prune deletes them after 30 days, so the last ones go by about 2026-11-08.
  - The Google Drive mirrors (`gdrive-crypt:velero`, `gdrive-crypt:talos-etcd-backups`): the nightly `rclone sync`
    (04:00Z, `--drive-use-trash=false`, so no Drive trash copy) follows each deletion within a day. That holds
    while the sync job keeps succeeding.

## Residual

- Git history holds ciphertexts the old key opens, and so do backups of it. **After a suspected compromise of the age key,
  rotate every value behind it**, not only the key: that means every secret family in `git grep -l -E '^sops:'` (database passwords,
  OAuth client secrets, deploy keys, the OpenBao seeds and unseal escrow, the Talos secrets bundle's PKI, `gitea-admin`), each with its
  own procedure.
- `openbao-unseal.sops.yaml` and `talos-secrets-bundle.sops.yaml` are DR-only ciphertexts: their recoverability now depends on the
  new key's offline copy.

---

# 2. The Kubernetes ServiceAccount signing key — decision D7 (a): rehearsed, feasible without downtime

**Status: a tested procedure exists; it has not been run on `admin@ai`.** Rehearsed on 2026-10-07 on a disposable Talos
1.11.2 / Kubernetes 1.31.4 cluster (3 control planes and 1 worker, `talosctl cluster create` with the docker provisioner on
dev-worker-1, destroyed afterwards). Done in order, no probe of a client that re-reads its token got a 401 or a 5xx; the only failures were
refused connections to the one control plane whose kube-apiserver was restarting, while the other two answered every probe. It costs **one
rolling reboot of the control planes** (step 1) and an overlap of about two hours (step 3). Talos has no procedure of its own:
`talosctl rotate-ca --kubernetes` rotates the Kubernetes API CA only (Talos 1.11 "CA rotation" docs). A real run needs the go
decision at the end of this section.

## How Talos 1.11 handles the signer and the verifiers (read this first)

- Talos renders both kube-apiserver key files from **`cluster.serviceAccount.key`**: `--service-account-signing-key-file=/system/secrets/kubernetes/kube-apiserver/service-account.key`
  (the **signer**) and `--service-account-key-file=/system/secrets/kubernetes/kube-apiserver/service-account.pub` (the **verifier set**: the
  public half of the same key). kube-controller-manager signs legacy token Secrets with `/system/secrets/kubernetes/kube-controller-manager/service-account.key`,
  the same key. `admin@ai` runs exactly these flags (read on 2026-10-07). **So changing `cluster.serviceAccount.key` swaps the verifier
  together with the signer**: on that control plane every token signed by the old key fails at once (execution ruling R11). Rehearsed: switching
  one CP's key alone (no step 1) made it reject a healthy client's one-minute-old token with 401 while the other two accepted it.
- **`cluster.apiServer.extraArgs` cannot add a verifier.** Talos 1.11.2 denies both `service-account-key-file` and
  `service-account-signing-key-file` there (`MergeDenied` in `internal/app/machined/pkg/controllers/k8s/control_plane_static_pod.go`). The trap:
  `talosctl patch mc` **accepts** such a patch ("Applied configuration without a reboot"), then `k8s.ControlPlaneStaticPodController` fails in
  a loop with `extra arg "service-account-key-file" is not allowed` and stops updating the kube-apiserver static pod, for every later change
  too, until the entry is removed. Even if it were allowed, extraArgs is a map in 1.11 (one value per flag) and the apiserver does not split
  this flag on commas.
- **Two keys in `cluster.serviceAccount.key` do not add a verifier either.** Talos writes `service-account.key` verbatim (both PEM blocks; the
  apiserver signs with the first) but derives `service-account.pub` from the first key only: `/openid/v1/jwks` listed one `kid`.
- **What works: a PEM bundle mounted over Talos's `service-account.pub` inside the apiserver pod.** `--service-account-key-file` accepts a
  file with several public keys. A `cluster.apiServer.extraVolumes` entry bind-mounts a host file at
  `/system/secrets/kubernetes/kube-apiserver/service-account.pub`; the mount exists only in the pod, Talos keeps writing its own `.pub` on
  the host, the flag stays Talos's, and the signer keeps following `cluster.serviceAccount.key`. The host file comes from `machine.files`
  (`op: create`, under `/var`). Talos writes those files **at boot only** (`machine.files` is not in the list `CanApplyImmediate` allows), which
  is why step 1 reboots each control plane once. Changes under `cluster.*` (the volume, the key) apply without a reboot: Talos restarts the
  static pods.
- **Order: the file must exist before the volume.** A volume whose host file is missing makes the runtime create a *directory* at that path,
  and that control plane's apiserver crash-loops (`runc ... error mounting "/var/.../service-account.pub"`, rehearsed). The directory would then
  also stop `machine.files` writing the file at the next boot. So step 1 puts the file and the volume **in one patch that takes effect at a
  boot** (Talos writes the file before kubelet starts). Never add the volume with `--mode=no-reboot` to a node that has not booted with the file.
- **Apply the step 1 patch once per node.** A merge patch *appends* to `machine.files` and `cluster.apiServer.extraVolumes`: the same patch twice
  gives two entries (checked with `talosctl machineconfig patch`). Talos names the volume after its mount path, so two volumes on that path make
  kubelet reject the node's whole static pod set (`invalid pod: ... Duplicate value: "system-secrets-kubernetes-kube-apiserver-service-account-pub"`):
  that control plane loses kube-apiserver, kube-controller-manager and kube-scheduler (rehearsed by accident, fixed with a JSON patch removing
  `/cluster/apiServer/extraVolumes/1`). Check for an existing entry before step 1 and before a rollback that re-adds the volume.
- **A Talos-initiated reboot does not drain.** `apply-config --mode=reboot` and `talosctl reboot` run the reboot sequence (stop pods, reboot);
  only `talosctl shutdown` and upgrades cordon and drain (`v1alpha1_sequencer.go`). On `admin@ai`, step 1 therefore stages the patch
  (`--mode=staged`) and reboots through [node-maintenance.md](node-maintenance.md)'s planned procedure. `patch mc --mode=staged` patches the
  *running* config: a second staged patch replaces the first, and staging a no-op patch un-stages (rehearsed; the staged config shows as
  MachineConfig `persistent`, the running one as `v1alpha1`).
- Each control plane's `/openid/v1/jwks` lists exactly its verifier set, and `kid = base64url(sha256(DER SubjectPublicKeyInfo))`. `kid`s are
  public, so they are the check everywhere below.
- A legacy token Secret is also checked against the Secret's current value. Re-issuing it (remove `data.token`; the token controller fills it
  again within seconds) **revokes the old value at once on every control plane**, whatever the verifier set (`Token does not match server's copy`,
  rehearsed).
- In Git Bash, `kubectl get --raw /openid/...` and `talosctl read /system/...` need `MSYS_NO_PATHCONV=1`, otherwise the path becomes a
  Windows path and the API answers `NotFound`.

## What depends on it (`admin@ai`, read-only inventory of 2026-10-07)

- **The cluster:** Talos v1.11.2, Kubernetes v1.31.4, control planes .41, .42 and .43, issuer and `--api-audiences` `https://192.168.0.40:6443`, one
  RS256 key, `kid` `b1TEXOwB...` on all three control planes' `/openid/v1/jwks`.
- **TokenReview consumers.** After #2125 every S2S mint depends on TokenReview of projected ServiceAccount tokens, and gatekeeper's own
  automounted token calls TokenReview (F-26). Consumers (ClusterRoleBindings to `system:auth-delegator` or to a TokenReview ClusterRole):
  `keda/keda-metrics-server`, `kube-system/metrics-server`, `openbao/openbao-server` (Kubernetes auth), the OpenBao `k8stoken-sync` role and
  gatekeeper (`strive-gatekeeper-gatekeeper-tokenreview`). Re-read the list before a run:

  ```bash
  kubectl --context admin@ai get clusterrolebinding -o json | jq -r '.items[] | select(.roleRef.name=="system:auth-delegator") | .metadata.name + " -> " + ([.subjects[]? | .kind + ":" + (.namespace // "") + "/" + .name] | join(","))'
  kubectl --context admin@ai get clusterrole -o json | jq -r '.items[] | select(any(.rules[]?; (.resources // []) | index("tokenreviews"))) | .metadata.name'
  ```
- **Projected tokens:** 333 pod volumes with the default 3607 s (the apiserver extends these to one year, but kubelet still replaces them at
  80 % of 3607 s, about 48 minutes), 15 with audience `strive-gatekeeper` and 3600 s (`strive-ailab`), none longer. Re-take:

  ```bash
  kubectl --context admin@ai get pods -A -o json | jq -c '[.items[] | . as $p | (.spec.volumes // [])[] | .projected.sources[]? | .serviceAccountToken // empty | {exp: (.expirationSeconds // 3600), aud: (.audience // ""), ns: $p.metadata.namespace}] | group_by([.exp, .aud])[] | {exp: .[0].exp, aud: .[0].aud, pods: length}'
  ```
- **Clients that never re-read their token:** `serviceaccount_stale_tokens_total` counts uses of an extended token after its first 3607 s, i.e.
  a client still holding a token kubelet has already replaced. It rose by **0** on all three control planes over the last 7 days (one event on
  cp1 between 7 and 8 days ago): today every client re-reads its projected token.
- **TokenRequest tokens:** `openbao/openbao-k8stoken-sync` (CronJob, `17 3 * * *` UTC) mints **720 h** tokens for the dev workers' `tep`,
  `helmtest` and `platform` kubeconfigs and publishes them to OpenBao KV; the openbao-agent on each worker renders them to
  `~/.tep/kubeconfig`, `~/.helmtest/kubeconfig` and `~/.platform/kubeconfig`. Tokens minted before the switch keep the old signature for 30 days
  unless re-minted. Hand-minted `kubectl create token` tokens are not inventoried.
- **Legacy `kubernetes.io/service-account-token` Secrets (never refresh): 4**, all in `testpool`: `tep-dw1-token`, `tep-dw2-token`,
  `tep-dw3-token`, `tep-dw4-token` (ServiceAccounts `tep-dw1` ... `tep-dw4`), declared in git in
  `kubernetes/apps/infrastructure/testpool/tep-access.yaml` (Flux `testpool`). `serviceaccount_legacy_tokens_total` rose by 3 on cp1 in 7 days,
  so at least one is still used although the kubeconfigs moved to k8stoken-sync: find who in the audit log before re-issuing (step 3). Re-take:
  `kubectl --context admin@ai get secrets -A --field-selector type=kubernetes.io/service-account-token -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name`.
- **Where the key lives:** the OpenTofu state (`talos_machine_secrets.this` in `kubernetes/infra/`, `certs.k8s_serviceaccount.key`) and the DR copy
  `kubernetes/infra/talos-secrets-bundle.sops.yaml` (`stringData."secrets.yaml"`, `certs.k8sserviceaccount.key`). Worker machine configs
  (`agent-nodes`, `env-pool`) do not carry it (checked on the rehearsal's worker).

## Before a real run: make the change survive `tofu apply` (not rehearsed)

The control planes' machine config belongs to OpenTofu: `talos_machine_configuration_apply.cp` applies the config generated from
`talos_machine_secrets.this` plus `machine-config/controlplane.yaml.tftpl`. It does not see a `talosctl patch`, and **the next `just apply`
re-applies the old key as signer and sole verifier**: after step 4 that is an instant 401, on that control plane, for every token signed since the
switch. So, before step 1, either:

- **(preferred) carry every step in OpenTofu:** steps 1 and 4 as the `machine.files` and `extraVolumes` block in `controlplane.yaml.tftpl` (public
  keys only, plain text is fine); step 2 as a second config patch setting `cluster.serviceAccount.key` from a sensitive variable fed from a new
  SOPS file (`talos_machine_secrets.this` keeps the old key, so this override stays for good); apply one control plane at a time with
  `-target='talos_machine_configuration_apply.cp["cp1"]'` after a `tofu plan` that shows only that control plane's expected diff. `apply_mode`
  belongs to that one resource, so set it per step and re-plan each time: step 1 needs `"staged"` (or `"staged_if_needing_reboot"`; the
  provider's validator, v0.11.0 as locked in the ops checkout, accepts `auto`, `reboot`, `no_reboot`, `staged`, `staged_if_needing_reboot`)
  followed by the planned drain and reboot below,
  because the default `auto` would reboot the control plane **undrained** (a Talos-initiated reboot does not drain, above); steps 2 and 4 need
  `"no_reboot"`, because a mode left at `staged` would only stage the signer switch or the verifier drop (step 2's and step 4's wait loops
  would never end) and the change would land silently at the next unplanned reboot; or
- run the steps with `talosctl` as below and freeze every `kubernetes/infra` apply until the template and the variable match and `tofu plan` is clean.

Either way, write the new private key into the DR bundle in the same change (`certs.k8sserviceaccount.key` in the inner `secrets.yaml`, following
the rules at the top), or a total-loss recovery comes back with the old key.

## Procedure (as rehearsed; one control plane at a time)

Setup, workstation, Git Bash. `$W` holds the new private key and witness tokens: restrict it (rules at the top) before step 0.

```bash
OUT="$(cd "$(git rev-parse --git-common-dir)/.." && pwd -P)/kubernetes/infra/_out"
export TALOSCONFIG="$OUT/talosconfig" MSYS_NO_PATHCONV=1
T="$OUT/talosctl-1112.exe"; K="kubectl --context admin@ai"; W="$OUT/sa-rotation"
CPS="192.168.0.41 192.168.0.42 192.168.0.43"; ALL=192.168.0.41,192.168.0.42,192.168.0.43
declare -A POD=([192.168.0.41]=kube-apiserver-talos-cp1 [192.168.0.42]=kube-apiserver-talos-cp2 [192.168.0.43]=kube-apiserver-talos-cp3)
b64url() { base64 -w0 | tr '+/' '-_' | tr -d '='; }
kid()    { openssl pkey -pubin -in "$1" -outform DER | openssl dgst -sha256 -binary | b64url; }
hdr()    { local s; s=$(cut -d. -f1 | tr '_-' '/+'); while [ $(( ${#s} % 4 )) -ne 0 ]; do s="$s="; done; echo "$s" | base64 -d; }
jwks()   { $K --server "https://$1:6443" get --raw /openid/v1/jwks | jq -c '[.keys[].kid[0:8]]'; }
signs()  { $K --server "https://$1:6443" -n default create token default --duration=10m | hdr | jq -r '.kid[0:8]'; }
trv()    { jq -n --rawfile t "$2" '{apiVersion:"authentication.k8s.io/v1",kind:"TokenReview",spec:{token:($t|rtrimstr("\n"))}}' |
           $K --server "https://$1:6443" create -f - -o jsonpath='{.status.authenticated} {.status.error}{"\n"}'; }
state()  { for c in $CPS; do echo "$c jwks=$(jwks $c) signs=$(signs $c)"; done; "$T" -n $ALL etcd status; }
# start time of $1's kube-apiserver container, asked through ANOTHER control plane (the VIP may sit on $1)
started(){ local o; for o in $CPS; do [ "$o" != "$1" ] && break; done
           $K --server "https://$o:6443" -n kube-system get pod "${POD[$1]}" -o jsonpath='{.status.containerStatuses[0].state.running.startedAt}'; }
entries(){ "$T" -n "$1" get mc "$2" -o jsonpath='{.spec}' | grep -c 'sa-verify'; }   # $2 = v1alpha1 (running) or persistent (next boot)
# resign <token-file> <private-key>: the token's claims re-signed with another key (to test a verifier before anything signs with it)
resign() { local h p; h=$(printf '{"alg":"RS256","kid":"%s"}' "$(openssl pkey -in "$2" -pubout -outform DER | openssl dgst -sha256 -binary | b64url)" | b64url)
           p=$(cut -d. -f2 < "$1"); printf '%s.%s.%s\n' "$h" "$p" "$(printf '%s.%s' "$h" "$p" | openssl dgst -sha256 -sign "$2" -binary | b64url)"; }
```

Never paste a `talosctl ... --dry-run` diff of a control plane anywhere: its context lines print the current private key (`key:` lines).

### Step 0 — the new key, the patches, the witnesses (no cluster change)

```bash
umask 077; mkdir -p "$W"
"$T" -n 192.168.0.41 read /system/secrets/kubernetes/kube-apiserver/service-account.pub > "$W/k1.pub"   # public half of the current key
openssl genrsa -traditional -out "$W/k2.key" 4096          # same form Talos generated: PKCS#1 RSA 4096
openssl pkey -in "$W/k2.key" -pubout -out "$W/k2.pub"
echo "k1=$(kid "$W/k1.pub") k2=$(kid "$W/k2.pub")"; state   # k1 = the kid every CP lists and signs with
# step 1: bundle (old + new public key) written at boot, mounted over Talos's .pub
{ printf 'machine:\n  files:\n    - op: create\n      path: /var/sa-verify/service-account.pub\n      permissions: 0o444\n      content: |\n'
  cat "$W/k1.pub" "$W/k2.pub" | sed 's/^/        /'
  printf 'cluster:\n  apiServer:\n    extraVolumes:\n      - hostPath: /var/sa-verify/service-account.pub\n        mountPath: /system/secrets/kubernetes/kube-apiserver/service-account.pub\n        readonly: true\n'
} > "$W/step1-verify-k1-k2.yaml"
printf 'cluster:\n  serviceAccount:\n    key: %s\n' "$(base64 -w0 < "$W/k2.key")" > "$W/step2-sign-k2.yaml"   # secret
printf '[{"op":"remove","path":"/cluster/apiServer/extraVolumes"}]\n' > "$W/step4-drop-k1.json"
# old-key witness: a TokenRequest token now (nothing can mint an old-key token after step 2); 168 h covers an overlap of up to a week
$K -n default create token default --duration=168h > "$W/k1-witness.jwt"
# step 2 rollback patch, built now rather than under pressure. The OpenTofu output (like the DR bundle's
# certs.k8sserviceaccount.key) already holds the key as base64 of the PEM: paste it as-is, do NOT base64 it again.
k1b64=$(cd "$OUT/.." && ~/.tofubin/tofu.exe output -json machine_secrets | jq -r '.certs.k8s_serviceaccount.key')
[ "$(printf '%s' "$k1b64" | base64 -d | openssl pkey -pubout -outform DER | openssl dgst -sha256 -binary | b64url)" = "$(kid "$W/k1.pub")" ] &&
  printf 'cluster:\n  serviceAccount:\n    key: %s\n' "$k1b64" > "$W/rollback-sign-k1.yaml" && echo "rollback patch = k1" || echo "STOP: not the old key"
unset k1b64
```

`step4-drop-k1.json` removes the whole `extraVolumes` list: correct while this bundle is the only entry (true on 2026-10-07; the template has no
`apiServer` section). If another volume was added since, remove this entry by its index instead.

### Step 1 — make every control plane verify both keys (one reboot each, signer unchanged)

For each control plane in turn (`cp=192.168.0.41`, then `.42`, then `.43`):

```bash
"$T" -n $ALL etcd status                                   # 3 members, same raft index, no errors — before EVERY control plane
entries $cp v1alpha1; entries $cp persistent               # 0 and 0; anything else: stop, the patch would duplicate (above)
"$T" -n $cp patch mc --patch @"$W/step1-verify-k1-k2.yaml" --mode=staged
entries $cp persistent                                     # 2 (the file and the volume): staged for the next boot
# now reboot this control plane the way node-maintenance.md "Planned drain / host reboot" does (its quorum and CNPG
# checks, talosctl shutdown, which drains and powers off, then qm start <vmid> on the Proxmox host; no host reboot).
# Apply nothing else to it in between: another patch starts again from the running config and replaces what is staged.
until [ "$($K --server https://$cp:6443 get --raw /readyz 2>/dev/null)" = ok ]; do sleep 5; done
entries $cp v1alpha1                                       # 2: running
state                                                      # $cp: jwks [k1,k2] signs k1; the others unchanged
for c in $CPS; do echo "$c $(trv $c "$W/k1-witness.jwt")"; done   # true on all three
```

**Gate before step 2:** every control plane lists both `kid`s, and a token signed by the **new** key is accepted by all three:

```bash
$K -n default create token default --duration=1h > "$W/witness-1h.jwt"
resign "$W/witness-1h.jwt" "$W/k2.key" > "$W/k2-test.jwt"
for c in $CPS; do echo "$c $(trv $c "$W/k2-test.jwt")"; done   # true on all three; "invalid signature" = that CP lacks the bundle
```

Rehearsal: `--mode=reboot` on each node (the docker provisioner supports neither `talosctl shutdown` nor `talosctl reboot`), 39-47 s from apply
to a ready apiserver, no drain; the staged path separately (stage, power-cycle the node: the staged file and volume were live after the boot;
that test patch used a second path on a node that already had the entry, which is how the duplicate-volume failure above was found).
`/var/sa-verify/service-account.pub` present (`-r--r--r--`, root); the host's own `.pub` unchanged; before the gate, the forged new-key token
was rejected (`invalid signature`) exactly on the control planes not yet rebooted.

### Step 2 — switch the signer (no reboot)

For each control plane in turn. `/readyz` alone is not enough here: the old pod keeps serving for about 50 s after the apply, so wait for the
container to be replaced (`started` asks another control plane, because the VIP may sit on the one restarting):

```bash
before=$(started $cp)
"$T" -n $cp patch mc --patch @"$W/step2-sign-k2.yaml" --mode=no-reboot
until s=$(started $cp) && [ -n "$s" ] && [ "$s" != "$before" ]; do sleep 3; done
until [ "$($K --server https://$cp:6443 get --raw /readyz 2>/dev/null)" = ok ]; do sleep 3; done
state                                                      # $cp: jwks [k1,k2] signs k2
$K --server https://$cp:6443 -n default create token default --duration=1h > "$W/k2-from-$cp.jwt"
for c in $CPS; do echo "$c old=$(trv $c "$W/k1-witness.jwt") new=$(trv $c "$W/k2-from-$cp.jwt")"; done   # all true
```

A mixed state between control planes is safe: each one verifies both keys. kube-controller-manager restarts first, then kube-apiserver
(rehearsal: 50-65 s from apply to the new container). Note the time the last control plane switched: step 3 counts from it.

### Step 3 — the overlap: wait until nothing uses an old-key token

All of these, then step 4. Nothing breaks while both keys verify, so the overlap can be as long as needed (a day is fine).

1. **At least 60 minutes since the last switch.** Kubelet replaces 3600/3607 s projected tokens at about 48 minutes (rehearsal: a 600 s token was
   replaced after 476 s and came back signed by the new key).
2. **No client still holds a replaced token:** in Prometheus (`port-forward svc/kube-prometheus-stack-prometheus`),
   `sum by (instance) (increase(serviceaccount_stale_tokens_total{job="apiserver"}[1h]))` is 0 on all three control planes for the hour that
   starts 61 minutes after the last switch (every old-key token is past its first 3607 s by then, so any use of one counts), which puts step 4
   at two hours after the last switch at the earliest. If not, the audit log names the client: annotation `authentication.k8s.io/stale-token`
   (Talos's default audit policy is `Metadata`, which keeps annotations):

   ```bash
   for c in $CPS; do "$T" -n $c read /var/log/audit/kube/kube-apiserver.log; done | grep stale-token |
     jq -r '(.annotations["authentication.k8s.io/stale-token"] | split(",")[0]) + " pod=" + ((.user.extra["authentication.kubernetes.io/pod-name"] // []) | join(","))' |
     sort | uniq -c
   ```
   (Rehearsal: the client that read its token once showed up here, and in the metric on all three control planes, 67 s after its token's
   first 3607 s: `subject: system:serviceaccount:satest:prober pod=prober-frozen`.)
   Restart that pod (a new pod gets a new token) or fix the client, and wait again.
3. **TokenRequest tokens:** `$K -n openbao create job --from=cronjob/openbao-k8stoken-sync k8stoken-sync-sa-rotation`, wait for `Complete`, then
   on each dev worker check that the three kubeconfigs carry the new key (header only):
   `for f in ~/.tep/kubeconfig ~/.helmtest/kubeconfig ~/.platform/kubeconfig; do yq '.users[0].user.token' $f | cut -d. -f1 | tr '_-' '/+' | awk '{while (length($0) % 4) $0 = $0 "="; print}' | base64 -d; echo; done`.
   Tell the owner that tokens minted by hand before the switch stop working at step 4.
4. **Legacy Secrets,** only after step 2 is done on **all three** control planes (the controller re-fills a Secret with its own control plane's
   key; one not yet switched would re-mint an old-key token). First find who uses them: annotation `authentication.k8s.io/legacy-token` in the
   same audit logs. Then, per Secret, with its consumer ready to take the new value at once (the old value dies on the spot):

   ```bash
   $K -n testpool patch secret tep-dw1-token --type=json -p '[{"op":"remove","path":"/data/token"}]'
   $K -n testpool get secret tep-dw1-token -o jsonpath='{.data.token}' | base64 -d | hdr   # kid = new key
   ```
   If nothing uses them any more, retiring them from `tep-access.yaml` is better than re-issuing.

### Step 4 — drop the old verifier (no reboot)

For each control plane in turn, with Prometheus open on
`sum by (instance) (rate(authentication_attempts{job="apiserver",result="error"}[5m]))` (baseline on 2026-10-07: 5 errors in 24 hours on all three
together):

```bash
before=$(started $cp)
"$T" -n $cp patch mc --patch @"$W/step4-drop-k1.json" --mode=no-reboot
until s=$(started $cp) && [ -n "$s" ] && [ "$s" != "$before" ]; do sleep 3; done
until [ "$($K --server https://$cp:6443 get --raw /readyz 2>/dev/null)" = ok ]; do sleep 3; done
state                                                      # $cp: jwks [k2] signs k2
$K --server https://$cp:6443 -n default create token default --duration=10m > "$W/k2-now.jwt"   # fresh: step 2's 1 h tokens have expired
for c in $CPS; do echo "$c old=$(trv $c "$W/k1-witness.jwt") new=$(trv $c "$W/k2-now.jwt")"; done
#   $cp: old=false "[invalid bearer token, invalid signature]", new=true; the others still old=true until rolled
```

Watch 10 minutes before the next control plane. A rise in the error rate on the control plane just rolled, or
`$K -n kube-system logs ${POD[$cp]} | grep -c 'Unable to authenticate'` growing, means an old-key holder was missed: roll that control plane
back (below) and find it (audit log: 401 events carry `sourceIPs` and `userAgent`). The `machine.files` entry stays for now: it is what makes
this step's rollback a no-reboot change.

### Step 5 — clean up and record

- Drop the `machine.files` entry at the next planned control plane reboot or Talos upgrade (removing it only takes effect at a reboot; the file
  left on `/var` holds public keys only).
- Delete `$W` once OpenTofu (or its SOPS file) and the DR bundle hold the new key and no rollback of step 2 can be needed: it holds the new
  private key twice in plain form (`k2.key`, and base64 in `step2-sign-k2.yaml`), the old private key (base64 in `rollback-sign-k1.yaml`), and
  the witness tokens (`*.jwt`).
- Record the old and new `kid`s, the date and the PR numbers (see "After any of these").

## Rollback

| From | Do | Why it is safe |
|---|---|---|
| Step 1, staged but not yet rebooted | Un-stage: stage a no-op patch, `printf '[{"op":"test","path":"/version","value":"v1alpha1"}]' > "$W/noop.json"; "$T" -n $cp patch mc --patch @"$W/noop.json" --mode=staged`, then `entries $cp persistent` is 0. | The running config never had the entry (rehearsed). |
| Step 1, rebooted with it | Remove `/cluster/apiServer/extraVolumes` on it (no reboot). The file can stay. | Talos's own `.pub` is the old key again. |
| Step 2 (any control plane), **before step 4 has started** | On each switched control plane, `"$T" -n $cp patch mc --patch @"$W/rollback-sign-k1.yaml" --mode=no-reboot` (the patch from step 0, checked against `k1`), then wait and check as in step 2: it signs `k1` again. | Every control plane still verifies both keys, so tokens signed either way keep working (rehearsed). |
| Step 4 (any control plane) | Check `entries $cp v1alpha1` is 1 (the file only), then re-add only the volume (no reboot): `cluster: { apiServer: { extraVolumes: [ { hostPath: /var/sa-verify/service-account.pub, mountPath: /system/secrets/kubernetes/kube-apiserver/service-account.pub, readonly: true } ] } }`. | The file is still on disk because step 4 keeps `machine.files` (rehearsed: the old key verified again after 41 s). |

Do not roll the signer back while any control plane has dropped the old verifier: tokens it then signs fail there (rehearsed). Roll step 4 back on
every control plane first.

The rows use `talosctl`. On the OpenTofu path, either express the rollback as a tofu change (revert the template or variable change and
apply that control plane with the same per-step `apply_mode`), or freeze `kubernetes/infra` applies until the `talosctl` rollback is mirrored
in tofu: otherwise the next `just apply` undoes it.

## What breaks if the order is wrong (rehearsed)

- **Switching the signer without step 1** (Talos's default coupling): that control plane accepts only the new key at once. A healthy client
  whose token was a minute old got 401 there and 201 from the other two; through the VIP or the `kubernetes` Service that is one request in three,
  then all of them once every control plane is switched, until kubelet replaces every token (up to about 48 minutes) and every client re-reads it:
  an S2S outage of up to an hour on `admin@ai`.
- **Dropping the old verifier before the overlap ends:** after step 4 on one control plane, a client still holding an old-key token (a 3607 s token
  read once at start, the extended kind that stays valid for a year) got 401 from that control plane on every probe and 201 from the two others; an
  old-key 24 h TokenRequest token failed TokenReview there with `invalid signature`; the rejections showed in `authentication_attempts{result="error"}`
  and the apiserver log as `Unable to authenticate the request ... invalid signature`.
- **The volume before the file:** that control plane's apiserver crash-loops and a directory appears where the file should be (above).
- **The step 1 patch twice:** two volumes on one path, and that control plane runs no control plane static pods at all (above).
- **A verifier through `extraArgs`:** accepted by `talosctl`, then the static pod controller refuses it and stops updating the apiserver (above).

## Timings (rehearsal; production will be slower)

| Step | Rehearsal (docker nodes) | Expect on `admin@ai` |
|---|---|---|
| 1, per control plane | apply to ready 39-47 s | a full reboot with drain, several minutes; one control plane at a time with etcd 3/3 in between |
| 2, per control plane | apply to new apiserver 50-65 s | about a minute, then the checks |
| 3 | 600 s tokens replaced after 476 s | two hours at least (item 2's window), plus the k8stoken-sync run and the legacy Secrets |
| 4, per control plane | apply to new apiserver 43-61 s | about a minute, then 10 minutes of watching |

**Real-cluster gate:** a go decision for `admin@ai` needs the OpenTofu path above reviewed (a clean `tofu plan` per control plane), the
TokenReview-consumer list re-read, the legacy Secrets' users found, the k8stoken-sync re-run planned, a CP reboot window (step 1), and the quiet
window rule (no other platform-wide change that day).

## Rehearsal record (2026-10-07)

Cluster `sa-key-research`: `talosctl` v1.11.2 (checksum verified) `cluster create --controlplanes 3 --workers 1 --kubernetes-version 1.31.4` on
dev-worker-1, destroyed at the end. Old key `TYHsFxe7...`, new key `ZM7Or2rD...`, a third key `O0_bdAMK...` for the "without step 1" test.
Workload: a ServiceAccount, a legacy token Secret, and two host-network pods probing all three apiservers directly every 15 s with
SelfSubjectReview: `reload` (600 s projected token, re-read every loop) and `frozen` (3607 s token, read once). Witnesses: a 24 h TokenRequest
token, a 3607 s projected token and the legacy token, all taken before step 1, checked by TokenReview on each control plane after every change.

- Experiments that failed as predicted: a two-key `cluster.serviceAccount.key` (one `kid` in the JWKS); `extraArgs` `service-account-key-file`
  (static pod controller error loop).
- Step 1 on the three control planes, then step 2 on the three: every witness `true` on every control plane after every change; the `reload`
  probe changed to the new `kid` on its first refresh after the switch; neither probe saw a 401.
- Legacy Secret re-issued after step 2: new `kid`, the old value rejected everywhere at once.
- Step 4 on one control plane ("too early"): old-key witnesses `false` there only, the `frozen` probe 401 there only, new-key tokens `true`
  everywhere. Then the rollbacks of step 4 and of step 2 as in the table; the "without step 1" signer switch (a third key on one control plane)
  and the missing-file volume, each reverted; the staged path and, by accident, the duplicate volume.
- After the `frozen` probe's token passed 3607 s: `serviceaccount_stale_tokens_total` and the audit annotation named it (step 3, item 2).
- Final step 4 on the three control planes: every old-key token `invalid signature` everywhere, new-key tokens `true`, the `reload` probe 201
  everywhere, the `frozen` probe 401 everywhere.
- Totals over the 65 minutes: `reload` 703 answers 201, 74 refused connections (restarts), 3 answers 401, all three from the "without step 1"
  test; `frozen` 657 answers 201, 74 refused, 49 answers 401, all after the deliberate early step 4. etcd: 3 members, same raft index, no errors
  after every change. Cluster destroyed (`talosctl cluster destroy`: no container, network or volume left) and the Talos image removed from
  dev-worker-1.

---

# 3. The Keycloak realm keys (realm `strive`) — A5.3

Owner, Admin console. **The public edge blocks `/admin` and `/realms/master`** (the Keycloak IngressRoute with
`restrictPublicPaths`), so reach the console through the cluster:

```bash
kubectl --context admin@ai -n strive-ailab port-forward svc/keycloak 9180:9180
# then open http://localhost:9180/admin/master/console/ , realm `strive` -> Realm settings -> Keys -> Providers
```

The `master` admin password is in `strive-ailab/keycloak-secrets` key `admin-password`. Do not print it: copy it to the clipboard for the login form and clear the clipboard afterwards
(`kubectl --context admin@ai -n strive-ailab get secret keycloak-secrets -o jsonpath='{.data.admin-password}' | base64 -d | clip`).
UNVERIFIED: whether the console login survives a `localhost` port-forward given the realm's front-channel hostname; if the login redirects to `auth.strive.place/admin`
(blocked at the edge), use `kcadm` in the pod instead. If `kcadm` is used, `--config` points to a temporary file inside the pod that is deleted afterwards, and the password is piped
from the Secret (never `--password`). UNVERIFIED: whether `kcadm.sh config credentials` accepts a non-TTY stdin password; if it does not, use the console.

```bash
kc() { kubectl --context admin@ai -n strive-ailab exec -i deploy/keycloak -- /opt/keycloak/bin/kcadm.sh "$@" --config /tmp/kcadm-rotation.config; }
kubectl --context admin@ai -n strive-ailab get secret keycloak-secrets -o jsonpath='{.data.admin-password}' | base64 -d \
  | kubectl --context admin@ai -n strive-ailab exec -i deploy/keycloak -- /opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:9180 --realm master --user admin --config /tmp/kcadm-rotation.config
# ... the steps below ...
kubectl --context admin@ai -n strive-ailab exec deploy/keycloak -- rm -f /tmp/kcadm-rotation.config
```

## What is published, who verifies, what each key signs

- The realm publishes one `RS256` signing key (`sig`) and one `RSA-OAEP` key (`enc`); read them without secrets:
  `curl -s https://auth.strive.place/realms/strive/protocol/openid-connect/certs | jq '.keys[] | {kid, alg, use}'` (2026-10-07: one of each).
- **Gatekeeper** verifies Keycloak access and ID tokens against that JWK Set (`infra/gatekeeper/src/app/gatekeeper/jwks.py` in platform). It caches the set for **900 s**; an unknown `kid`
  forces one refetch; the **60 s cooldown is armed only when the kid is still absent after that refetch** (a fabricated kid arms it, a real new kid does not); a token that does not
  verify after the refetch terminates the session. Inventory any other verifier before step 1 (other realm clients, the MCP service's external issuer settings).
- The realm seed (`deploy/components/keycloak-realm-seed/realm-configmap.yaml`, platform, at `ff1630206`) declares **no key providers** and `keycloak-sync` reconciles only the user-profile
  component, so key providers created in the console are not reverted by the sync job.
- Key providers (Keycloak 26): `rsa-generated` (RS256 access/ID tokens), `rsa-enc-generated` (RSA-OAEP), `hmac-generated-hs512` (refresh tokens; listed as `hmac-generated` on older
  releases, use the name the Providers tab shows), `aes-generated`.
- **Which key signs a refresh token (read from the running image, 2026-10-07).** The Keycloak in this cluster is 26.0.0. In its jars, `RefreshToken.getCategory()` is
  `TokenCategory.INTERNAL`, and `DefaultTokenManager` (which maps a token category to a signature algorithm) holds the `HS512` constant for it, whereas access and ID tokens
  follow the realm's default signature algorithm (RS256 here). So refresh and offline tokens are signed HS512 with the realm's **HMAC** key, not with the RSA key: retiring the old HMAC
  provider too early invalidates every refresh token, and so every session that refreshes, even though the RSA key overlap is fine. The refresh check at each step below is the empirical
  confirmation on this build (a session logged in before the change must still refresh), so run it; if it contradicts this paragraph, trust the check and correct the paragraph.

## Before step 1 — read the lifetimes (no secrets) and fix the retention

Read the realm's `ssoSessionIdleTimeout`, `ssoSessionMaxLifespan`, `offlineSessionIdleTimeout`, `offlineSessionMaxLifespan` (and whether it is enabled), `rememberMe` with its two
remember-me lifespans, `accessTokenLifespan`, `accessCodeLifespan*` and the action-token lifespans (Realm settings, Sessions / Tokens tabs; or
`kc get realms/strive --fields ssoSessionIdleTimeout,ssoSessionMaxLifespan,offlineSessionIdleTimeout,offlineSessionMaxLifespan,offlineSessionMaxLifespanEnabled,rememberMe,ssoSessionIdleTimeoutRememberMe,ssoSessionMaxLifespanRememberMe,accessTokenLifespan,accessCodeLifespan,actionTokenGeneratedByUserLifespan,actionTokenGeneratedByAdminLifespan`),
and each client's offline session count (`kc get clients/<id>/offline-session-count -r strive`, for every client id from `kc get clients -r strive --fields id,clientId`).

**Retention for step 3 = the longest lifetime that has live artifacts.** The realm seed sets `ssoSessionMaxLifespan` to 36000 s (10 h) and `offlineSessionIdleTimeout`
to 30 days; with no offline sessions and no remember-me sessions the retention is **11 hours** (the 10-hour SSO maximum plus an hour); with offline sessions it is their idle
timeout (30 days by the seed), and a remember-me session lives as long as its own lifespan.

## Steps

1. **Add a passive RSA signing provider.** Add provider `rsa-generated` named `rsa-<date>`, key size 2048, algorithm RS256, `active = false`, `enabled = true`, priority higher than the current one's (the
   Providers tab shows it). Check: `.../protocol/openid-connect/certs` lists **both** RS256 kids. Keycloak documents a passive key as verify-only, so it should be listed, but this build has **not** been observed doing it (the
   realm has only ever had active keys): confirm it **before** waiting. If a passive key is not published, gatekeeper's cache refresh brings nothing in and step 2 produces unknown-kid refetches instead (still safe: a real new kid does not
   arm the cooldown, but the first tokens after step 2 pay a refetch). **Wait at least 16 minutes**:
   gatekeeper's 900 s cache refresh, not an unknown-kid refetch, brings the new kid in, so the cooldown cannot interfere (a real new kid does not arm it, but waiting removes the question).
2. **Make it active.** `active = true` on `rsa-<date>` (higher priority wins). New tokens carry the new `kid`; the old key keeps verifying everything it signed.
3. **Add the refresh-token and cookie keys.** Add new `hmac-generated-hs512` and `aes-generated` providers with a higher priority than the current ones; keep the old ones **enabled** for the retention above, so
   refresh tokens and state signed under the old keys still validate.
4. **Retire the old providers.** After the retention: set each old provider **passive**, then **disabled**, then **delete it a day later**. Deletion is the irreversible boundary: Keycloak never shows or exports a private key,
   so a deleted provider cannot be restored. Do `rsa-enc-generated` the same way (add a new one with a higher priority, keep the old enabled for the retention, then passive, disabled, deleted).

**Check at each step.** A test session logged in **before step 2** still refreshes after steps 2 and 3 (its refresh token is the old HMAC-signed one); a fresh login works; and gatekeeper's
`gatekeeper_auth_requests_total` refresh-failure and termination statuses are at their baseline (read the baseline over the previous 24 hours first, counts only):
`gatekeeper_auth_requests_total` by `status` in Prometheus (`monitoring`, port-forward `svc/kube-prometheus-stack-prometheus 9090:9090`, then the HTTP API).

**User impact.** None expected when the order is respected: sessions and refreshes keep validating against the old keys during the overlap. Steps 1-3 are one controlled operation under the calendar rule
(the plan counts A5.3's steps 1-3 as one change); step 4 is a separate day.

**Rollback.** Re-activate the old provider (set it active with the higher priority again, or lower the new one's priority): possible until the old provider is deleted. After step 2, a rollback means tokens
signed with the new key must still verify, so keep the new provider **enabled (passive)** for the same retention.

---

# After any of these

Record the date, the old and new identifiers (age recipient prefix, `kid`s) and the PR numbers in the plan's A5.7 note (the spec's rotation inventory), and open the due-dated issue for the next rotation
of that root (D17). Update `CLAUDE.md` or `README.md` only if a path or a name changed (step 6 of the age key is designed so that none does).
