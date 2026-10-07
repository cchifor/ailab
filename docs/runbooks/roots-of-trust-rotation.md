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

One age recipient, `age1nfa6hhdz9egnje3nwa2k0gpk5nr29nyvu74eprk20m7ql4fhw4esrlmt5g`, is the recipient of **every** SOPS
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

## Tooling facts (checked 2026-10-07, sops 3.9.4, Windows Git Bash)

- WSL (`Ubuntu`) has **no `sops`** and no internet. Use the Windows `sops` (`/c/Users/chifo/bin/sops`) with a controlled
  environment (below), or copy a Linux `sops` binary into WSL through `/mnt/c` if you want the "WSL sops" the plan names.
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
   git grep -l 'age1nfa6hhdz9egnje3nwa2k0gpk5nr29nyvu74eprk20m7ql4fhw4esrlmt5g'
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
D=<yyyymmdd of step 1>; OLD=age1nfa6hhdz9egnje3nwa2k0gpk5nr29nyvu74eprk20m7ql4fhw4esrlmt5g
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

## Residual

- Git history holds ciphertexts the old key opens, and so do backups of it. **After a suspected compromise of the age key,
  rotate every value behind it**, not only the key: that means every secret family in `git grep -l -E '^sops:'` (database passwords,
  OAuth client secrets, deploy keys, the OpenBao seeds and unseal escrow, the Talos secrets bundle's PKI, `gitea-admin`), each with its
  own procedure.
- `openbao-unseal.sops.yaml` and `talos-secrets-bundle.sops.yaml` are DR-only ciphertexts: their recoverability now depends on the
  new key's offline copy.

---

# 2. The Kubernetes ServiceAccount signing key — decision D7 (a): research and rehearse, never on `admin@ai`

**Status: no tested procedure exists.** Talos `talosctl rotate-ca --kubernetes` rotates the Kubernetes API **CA** only; there is no
Talos procedure for the ServiceAccount token signing key (Talos 1.11 "CA rotation" docs). This section is a **research and
rehearsal** procedure, time-boxed to one day including building the cluster. **A recorded outcome of "not feasible without
downtime" is acceptable.** It never delays the other items of the plan.

## Why it matters and what depends on it

- After #2125 (live 2026-10-07) every service-to-service mint depends on **TokenReview of projected ServiceAccount tokens**, and
  gatekeeper's own automounted token calls TokenReview (finding F-26). Removing the old verifier before every token it signed has
  been replaced breaks all S2S.
- TokenReview consumers on `admin@ai` on 2026-10-07 (ClusterRoleBindings to `system:auth-delegator` or to a TokenReview ClusterRole):
  `keda/keda-metrics-server`, `kube-system/metrics-server`, `openbao/openbao-server` (Kubernetes auth), the OpenBao `k8stoken-sync`
  role and gatekeeper (`strive-ailab-gatekeeper-tokenreview`). Re-read the list before the rehearsal; it is the verification list.

  ```bash
  kubectl --context admin@ai get clusterrolebinding -o json | jq -r '.items[] | select(.roleRef.name=="system:auth-delegator") | .metadata.name + " -> " + ([.subjects[]? | .kind + ":" + (.namespace // "") + "/" + .name] | join(","))'
  kubectl --context admin@ai get clusterrole -o json | jq -r '.items[] | select(any(.rules[]?; (.resources // []) | index("tokenreviews"))) | .metadata.name'
  ```
- **Legacy `kubernetes.io/service-account-token` Secrets never refresh** and stop working when their signer stops verifying. On
  2026-10-07 there are four, all in `testpool` (`tep-dw1-token` ... `tep-dw4-token`, consumed by `scripts/tep-render-kubeconfigs.py` and
  the dev-worker `~/.tep/kubeconfig`). Re-take the inventory: `kubectl --context admin@ai get secrets -A --field-selector type=kubernetes.io/service-account-token -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name`.
- Projected tokens (the S2S identity, the Kubernetes API client libraries) refresh within about an hour, so they follow a signer
  switch if the new key is already a verifier.
- The cluster is `v1.31.4`, issuer `https://192.168.0.40:6443`, one RS256 key at `/openid/v1/jwks`. The key material lives in the OpenTofu
  state (`talos_machine_secrets` in `kubernetes/infra/`, local state) and in the DR copy `kubernetes/infra/talos-secrets-bundle.sops.yaml`; a
  rotation that is not written back to both makes a total-loss recovery regenerate the **old** key.

## The procedure to prove (on a disposable Talos 1.11.2 cluster only)

Use `_out/talosctl-1112.exe` (v1.11.2); the system `talosctl` is v1.6.2 and unsafe. Roll **one control plane at a time**, with `talosctl ... etcd status` 3/3 in sync between
control planes. Never touch `admin@ai` for this.

1. **Build** a 3-CP Talos 1.11.2 cluster (the `kubernetes/infra/` module with a different state and names, or `talosctl cluster create` if it runs here) with a workload that
   authenticates by TokenReview (a copy of the gatekeeper pattern), a projected-token consumer and one legacy token Secret.
2. **Make the verifier set contain BOTH public keys, independently of the signer, on every control plane.** By default the kube-apiserver verifies with
   the public half of the key in `cluster.serviceAccount.key`, the same key that signs, so changing that key in step 3 would silently swap the old verifier for the
   new one and invalidate every old token at once. Break that coupling first. Generate the new RSA key. Two ways to try, **in this order** (both UNVERIFIED, the rehearsal's job):
   - **(a) A PEM bundle in `cluster.serviceAccount.key`.** kube-apiserver's `--service-account-key-file` accepts a PEM file with several keys (all become verifiers),
     while `--service-account-signing-key-file` uses the **first** private key. Appending the new private key as a second block makes it a verifier without changing the
     signer, and reordering the blocks later switches the signer. Prove that Talos accepts a two-block value and writes it unchanged to the file both flags read, and that the
     apiserver then verifies with both.
   - **(b) Explicit flags and files:** mount the old and the new public key files (`cluster.apiServer.extraVolumes`) and list both as `--service-account-key-file`
     (`cluster.apiServer.extraArgs`). Talos may **reject** an `extraArgs` entry that duplicates a flag it sets itself (and this flag is one it sets), and a repeated flag may need a
     comma-joined value or, on newer Talos, a list: settle both on the rehearsal cluster before relying on this path.

   Apply to one CP at a time. Check on **each** control plane (address each CP's API server directly, not the VIP): a TokenReview of an old token (a projected token and a legacy Secret
   token taken before the change) is `authenticated`; the apiserver's arguments or key files show both keys; `/openid/v1/jwks` lists both keys (if it does).
3. **Switch the signer.** Change `cluster.serviceAccount.key` to the new key (one CP at a time). The verifier set from step 2 is unchanged by this, so old and new tokens
   both verify throughout the roll. Check after each CP: etcd 3/3; a freshly created TokenRequest has the new `kid`; a TokenReview of an **old-signed** token is still `authenticated` on **every**
   control plane, the one just switched included, and so is a TokenReview of a new-signed token on each of them; the consumer's projected token refreshes and keeps working.
4. **Keep the old verifier until every token it signed has been replaced:** projected tokens within about an hour; legacy Secrets never (re-issue them: delete and
   recreate the Secret, re-run the consumers' renderers). Only then remove **the OLD key specifically** (its block in the bundle, or its file and volume in the flag list; leave the new one;
   do not just drop "the second entry"), one CP at a time, and check that an old-signed TokenReview is now `authenticated: false` and a new-signed one still `true` on each.
5. **Record the result in this runbook and in the plan's A5.7 note**: the exact `talosctl` and machine-config patches that worked, the measured downtime per CP and for the
   workload, what had to be re-issued, and where the new key was written (tfstate and the DR bundle). Or the recorded finding that it cannot be done without
   downtime and why.

**Rollback (rehearsal):** destroy the disposable cluster. **Rollback (a future real run):** keep the old public key in the explicit verifier list until the very end; the
reverse of step 3 is to point `cluster.serviceAccount.key` back at the old key, one CP at a time (the verifier list holds both, so nothing it signed is lost); the reverse of step 4 is to re-add the old
public key file to the list.

**Real-cluster gate (out of scope for the rehearsal):** a go decision for `admin@ai` needs the rehearsal's measured downtime, the verified TokenReview-consumer
list above, an inventory of legacy token Secrets with their owners, and the quiet window rule (no other platform-wide change that day).

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
