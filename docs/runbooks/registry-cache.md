# Runbook — registry.chifor.me pull-through cache (CI base images)

`registry.chifor.me` runs as an **on-demand pull-through cache** for the upstream base images that
`cchifor/platform` CI builds on (docker.io/quay.io/mcr). See ADR 0014. This runbook covers enabling
upstream auth, refreshing a stale cached tag, and verifying behaviour.

## Provision / change

```bash
tofu -chdir=kubernetes/infra/registry-lxc apply   # only if the LXC doesn't exist yet
just registry                                      # converge the registry_zot role (renders sync config)
just ping-registry                                 # connectivity check
```

The `sync` config is rendered into `/etc/zot/config.json`; a change to it restarts zot (sync is not on the
reload allowlist, see "Reload or restart" below).

### Applying a config change (pin the live binary)

**Since 2026-11-21 (the zot move to ai-node3) nothing here needs `-e`.** `ansible/group_vars/registry.yml` holds
the live state: the strive retention hold (100000 / 87600h) and `registry_zot_retention_dry_run: true`. The
v2.1.18 binary pin is gone (#1927): CT 5005 runs v2.1.23, the role default. The role also refuses any binary
change unless `-e registry_zot_allow_binary_change=true` (a binary change is a full-parse restart), and `get_url`
verifies the release sha256 from `registry_zot_sha256`. `-e registry_zot_read_only=true` is the registry-side writer
fence (a reload): every accessControl action becomes read-only until a play without it runs. On cutover night that
fence-off play ran against the new CT while the v2.1.18 pin was still in group_vars, as
`ansible-playbook registry.yml -e registry_zot_version=v2.1.23 -e registry_zot_read_only=false`: the installed bytes
matched the v2.1.23 entry, so the guard passed with no download and no restart. #1927 then dropped the pin, so a
plain `ansible-playbook registry.yml` matches the live binary. The older `-e` recipes below remain valid but are no
longer needed for these values.

There is no config-only apply: `registry.yml` always runs the WHOLE role. Besides `config.json`,
it converges the htpasswd lines, the certs, the sync upstream credentials and the systemd unit, and
a change to any of those restarts zot (a config.json change may reload instead, see "Reload or restart"). Its `get_url` task also converges `/usr/local/bin/zot` to
`registry_zot_version` (the renovate pin in defaults), and the live binary can lag it (2026-11-17:
live `v2.1.18`, pin `v2.1.23`). For a config or auth change, pin the version that is running:

```bash
python scripts/node-ssh.py 192.168.0.4 "pct exec 5005 -- /usr/local/bin/zot -v"   # ai-node3; e.g. commit v2.1.23-0-g...
# WSL: ansible ignores the world-writable /mnt/c ansible.cfg, so name it explicitly
export ANSIBLE_CONFIG=/mnt/c/Users/chifo/work/home/ailab/ansible/ansible.cfg
cd /mnt/c/Users/chifo/work/home/ailab/ansible
SOPS_AGE_KEY_FILE=../kubernetes/infra/_out/age.agekey ansible-playbook registry.yml -e registry_zot_version=v2.1.18
```

With the live version pinned, the binary task is EXPECTED to be a no-op: `get_url` sends a
conditional request (`If-Modified-Since` = the binary's mtime) and gets `304`, or downloads the same
bytes, and the file's mode/owner already match (`0755 root:root`), so `changed=False` and no
restart from it. It still runs, and it would replace the binary (and restart zot) if the bytes or
those attributes differed, so confirm `zot -v` first. The role has no task-level switch to skip it;
the pin is the gate.

### Reload or restart

**A zot restart is an outage, and an expensive one.** zot re-parses the whole store into its BoltDB metaDB
(`/var/lib/registry/store/meta.db`) before it binds :443. On 2026-11-17 that took **19 min** (161 repos, ~45.6k
manifests) and wrote **~265 GB** to ai-node1's worn NVMe, stalling etcd on cp1. Pulls and pushes fail meanwhile.
Poll `GET https://registry.chifor.me/v2/` for 200 (`systemctl is-active` reports active early).

So `config.json` is not templated straight into place. Each play renders its OWN candidate,
`config.json.candidate.<utc>.<random>`, so overlapping plays never share one; an `always:` step removes it. The play
runs `zot verify` on the candidate and hands it to `/usr/local/lib/zot-config-apply.py` (role `files/`), which picks
one of:

- **none**: the candidate equals the live config (content, owner, group and mode).
- **attributes**: same content, but a different owner/group/mode. The helper converges them with chown/chmod, which
  emit no WRITE, so zot does not reload.
- **reload**: only keys on the helper's `RELOADABLE` allowlist changed. Those are `storage.retention` (while
  retention is already active on both sides), `storage.gcDelay`, `storage.gcInterval` and the
  `http.accessControl` rules (`repositories`, `adminPolicy`, `groups`, `metrics`). zot v2.1.x watches the config
  FILE and reloads it on an fsnotify WRITE (`pkg/cli/server/config_reloader.go`), copying only some fields
  (`UpdateReloadableConfig`). So the helper writes the SAME inode, then proves the reload. First it waits until zot
  has logged no reload line for 3 s, then it takes a journal cursor. After its write, the LAST reload attempt must
  show `config file changed, trying to reload config` followed by `loaded new configuration settings`, with no
  `failed to reload config` after it. The PID must be unchanged and `/v2/` must answer. A FAILED line is not final
  (zot can read the file between the write and the truncate), so the helper waits for the last attempt and retries
  the write once. Otherwise it **fails the play** and writes the previous bytes back. That restores the file on DISK;
  whether zot's in-memory config also matches is then unconfirmed, which is what the marker below is for.
- **restart**: any other difference, or a first deploy. The helper replaces the file atomically and the role
  notifies `restart zot`.
- **blocked**: a reload-safe change that cannot be reloaded. Either zot no longer watches the file, or the live
  config.json is not valid JSON. zot ignores rename/remove events, so a config.json replaced since zot started (an
  atomic write, an editor save) is no longer watched. The helper checks zot's inotify watches in `/proc/<pid>/fdinfo`
  against the file's exact (inode, device) identity. Rather than cause a surprise 19 min outage, the play FAILS.
  Restart zot in a quiet window, or rerun once with `-e registry_zot_config_allow_restart=true`. Applies are
  serialized by an flock on `config.json.lock`.

**`config.json.unconfirmed`** (root-only, no secrets) marks an apply that is not proven yet. The helper writes it
before every reload or restart write, with a per-transaction token. A proven reload removes it. On a restart, the
`restart zot` handlers remove it after zot restarted, via `zot-config-apply.py --clear-marker <token>` under the
helper's lock, and only for this play's token. Restarts for other reasons (htpasswd, binary) clear nothing. While it exists, an unchanged candidate is NOT reported as `none`: the next run re-proves the
file on disk (an identical in-place rewrite that zot must reload), or restarts if the watch is gone. That covers a
failed reload, a killed helper, and a play that aborted before its handlers ran.

Run the play with `--check` first. The helper then only classifies (`--dry-run`) and the `Report how the zot
config was applied` task shows `{"mode": ..., "paths": [...]}`. Key paths only, never values: config.json holds
secrets. Check mode is not entirely side-effect free: it installs the helper and writes, then removes, this play's
throwaway candidate. The live `config.json` is never touched. Do not run two registry plays at once: the helper
serializes its own steps, but zot restarts (handlers) run outside its lock.

### Dropping orphaned referrers (`deleteReferrers`)

`registry_zot_strive_delete_referrers_repos` lists the repos whose retention also deletes **orphaned referrers**:
buildx attestation manifests whose subject image retention already deleted (55-77% of every strive repo's
manifests on 2026-11-18; they bloat zot's metaDB, which every restart rebuilds). zot v2.1.18's GC deletes a
referrer when its `subject` is not a TOP-LEVEL `index.json` entry, without recursing into indexes, and without
consulting `keepTags`. So, for every repo before it goes on the list:

1. Run `scripts/zot-referrer-orphan-check.py <repo>...` on the registry LXC (usage in its header). It must report
   `"safe": true` (`nested_not_top == 0`, `tagged_orphans == 0`); it exits 1 otherwise.
2. Note one kept tag's index digest and its platform manifests, and their `GET /v2/<repo>/referrers/<digest>`.
3. Apply (a reload, no restart). After the next GC cycle (`gcInterval` 1h), the orphan count should drop. Step 2's
   referrers must be unchanged. Watch, per 1-min sample:
   - the registry host's writes, `rate(node_disk_written_bytes_total{device="nvme0n1"}[1m])` on that host (ai-node3
     since the 2026-11-21 move; it was ai-node1 when this recipe was written). `nvme0n1` holds ai-node3's `pve` volume
     group and the `local-lvm` thin pool with 5005's mp0 (`pvs`: `/dev/nvme0n1p3`; `vm-5005-disk-1` in pool `data`;
     checked 2026-11-21). Re-check with `pvs` if the registry moves again;
   - the WAL fsync p99 of the control plane on that host (cp3 on ai-node3; cp1 before the move).
   **Abort when** the host's writes stay above 150 MiB/s for 3 consecutive samples, or that CP's fsync p99 is above
   1 s for 2 consecutive samples.
   **Abort** = remove the repo from the list and apply. Then VERIFY that the reload took: the play's `Report how the
   zot config was applied` shows `mode: reload`, and the live config.json no longer lists the repo. zot only stops
   between GC passes, so a pass already running finishes its repo. If the disabling apply fails or is `blocked` (the
   helper then restores the previous bytes, i.e. the canary stays ENABLED), **escalate**: `systemctl stop zot` on
   the LXC. That is an outage, but it ends GC at once. Then apply with `-e registry_zot_config_allow_restart=true`
   in a quiet window. Deletions already done are orphans and cannot be restored.
4. Widen to `["strive/**"]` only after a clean canary, and only when EVERY strive repo passes step 1. On
   2026-11-20 63 of 64 passed; `strive/keycloak-sync` fails closed because its index is empty. So the list names
   all 51 safe repos (strive/ci-build/* excepted: its own policy claims them first) instead of `strive/**`. A new
   strive repo joins the list only after its own step 1. To pace a large widening (~4 MB of node1 metaDB writes
   per deletion), apply a subset first with
   `-e '{"registry_zot_strive_delete_referrers_repos": ["strive/web", ...]}'` (keep the repos already live in it),
   watch one GC hour per step 3, then apply the full list. **Every apply, the full list included, needs a fresh
   step 1 on the repos it adds, run right before it.** A pre-check is a snapshot: CI keeps pushing, and a tagged or
   nested referrer that appears after it would be deleted irrecoverably once the policy is live.
   Once GC has emptied and removed the `strive/ci/*` repos (each held one orphaned referrer and nothing else; check
   `/v2/_catalog`), drop them from the list and from the tests' `_EXISTING_STRIVE_CI_REPOS`, so the list only names
   repos that exist and were pre-checked.

**Everything else the role converges still restarts zot**: htpasswd lines, certs, sync credentials, the unit and
the binary. Batch those changes into one apply, in a quiet window.

### Changing tag retention (dry run first)

A keepTags change (the strive/** families, their knobs) deletes tags on the next GC pass, and a deleted digest cannot
be re-pulled, only rebuilt. So every such change goes through a gate, a dry-run cycle, and then the real apply. Each
step is a reload (`storage.retention` is on the apply helper's allowlist). Always pin the live binary
(`-e registry_zot_version=v2.1.18`, see "Applying a config change").

1. **Gate: `scripts/zot-retention-pin-scan.py`.** It simulates zot v2.1.18 retention for the policy this branch's
   role renders (`--set KEY=YAML` tries another knob value) against the live registry, read-only. It exits 1 if
   anything in use would be deleted. In use means:
   - the live cluster's pods and workloads (spec, env values and status imageIDs);
   - ailab `kubernetes/apps`;
   - the platform's ailab overlay and pin sources (`--platform`, a checkout of cchifor/platform main);
   - the platform's own pin list (`--refs-file`).

   Comments are history, not use. Run it in Docker (python + PyYAML + jinja2), from a checkout of the branch:
   ```bash
   kubectl --context admin@ai get pods -A -o json > /tmp/scan/pods.json
   kubectl --context admin@ai get deploy,sts,ds,cronjob,job -A -o json > /tmp/scan/workloads.json
   git clone -q --depth 1 https://git.chifor.me/cchifor/platform.git /tmp/scan/platform
   # in a python container with pyyaml (and git) installed, platform mounted at /p:
   python3 /p/scripts/ci/list-ailab-pins.py > /tmp/scan/platform-pins.txt
   python3 scripts/zot-retention-pin-scan.py --kube-json /tmp/scan/pods.json --kube-json /tmp/scan/workloads.json \
       --platform /tmp/scan/platform --refs-file /tmp/scan/platform-pins.txt --json /tmp/scan/pin-scan.json
   ```
   Only `RESULT: PASS` (exit 0) goes on. A FAIL line names the reference, where it is used, and why it would go. Fix
   the policy (or protect the image) and re-run; never apply around a FAIL. The report also gives the would-delete
   totals: tags, manifests (index, platform children and referrers, ~4 MB of node1 metaDB writes each) and freed
   layer bytes (an upper bound: base layers shared with the mirror repos stay).
2. **Dry run, one GC cycle.** Apply with `-e registry_zot_retention_dry_run=true`. The `--check` run must show
   `mode: reload` with `storage.retention.dryRun` (plus `storage.retention.policies` if the policy changed too).
   `dryRun` is GLOBAL: while it is on, zot deletes nothing anywhere. deleteReferrers, deleteUntagged and the
   strive/ci-build/** and agentforge/** pruning all pause, so finish a deleteReferrers widening first. A reload
   restarts zot's background tasks, so a GC pass can start soon after it, and the next one `gcInterval` (1h)
   later. Each pass logs one line per tag, so count DISTINCT strive repo:tag pairs, not lines. Do it on the registry
   LXC (`ssh registry`, or `pct exec 5005 -- sh -c '...'` on ai-node3), with `SINCE` = the reload time (the
   `loaded new configuration settings` line):
   ```bash
   journalctl -u zot --since "$SINCE" -o cat | grep 'applied policy' | grep '"decision":"delete"' \
     | grep '"dry-run":true' | grep '"repository":"strive/' \
     | sed -E 's/.*"repository":"([^"]*)".*"tag":"([^"]*)".*/\1:\2/' | sort -u > /tmp/would-delete.txt
   wc -l < /tmp/would-delete.txt
   ```
   The count must match the `delete` total of a pin scan run at the same time. The scan models strive/** only, so
   agentforge/** deletions are filtered out above. Tags pushed, or crossing the 14-day line, between the two make
   small differences. Any listed tag that the scan's `--json` keeps, or that holds a digest the scan reports as in
   use, is a stop. Untagged and referrer deletions log `"dry-run":true` too, with other messages.
3. **Apply for real**: the same play without the `-e`. **Precondition (2026-11-20):** platform
   `scripts/ci/list-ailab-pins.py` lists airlock's `processingRuntimeAdmissions` images, so the daily protect job
   re-fetches them and `pulledWithin` covers them, and the gate passes. Until then the 8 admitted workflow runtimes
   survive only by `registry_zot_strive_keep_recent` (30) headroom. A live policy keeps re-evaluating every GC hour,
   and that headroom erodes with each new workflow pin, whether or not anyone applies again. The dry run (step 2)
   does not need it. Watch the GC hour with the abort budget of "Dropping orphaned
   referrers" step 3 (node1 writes, cp1 fsync p99). **Abort** = apply again with
   `-e registry_zot_retention_dry_run=true`. That reload stops all deletion from the next GC pass on; a pass already
   running finishes its repo.

## Enable Docker Hub upstream auth (recommended)

**Now optional (failover only) — see ADR 0014 Update 2026-06-23.** The docker.io sync upstream is
`https://mirror.gcr.io` (Google's anonymous Docker Hub pull-through, no 100-pulls/6h cap, same
digests), with `registry-1.docker.io` as failover. So cold fetches no longer 429 anonymously. The
Docker Hub token below only authenticates the **failover** path; set it only if mirror.gcr.io ever
lacks an image. On a cache miss the failover pulls Docker Hub directly (anonymous → 100-pulls/6h →
429); a free Docker Hub account lifts that to 200/6h+.

1. Create a read-only token: <https://app.docker.com/settings/personal-access-tokens>.
2. Set the username (non-secret) in `ansible/roles/registry_zot/defaults/main.yml` **or** a host/group
   var: `registry_zot_sync_dockerhub_user: "<dockerhub-user>"`.
3. Add the token to SOPS. ⚠️ The **existing** `registry.sops.yaml` embeds its own `encrypted_regex`
   (which predates this key), and SOPS uses the *embedded* regex — not `.sops.yaml`'s creation_rule —
   when editing an existing file. So a plain `sops <file>` edit would leave the new key **plaintext**.
   Re-encrypt from decrypted so the creation_rule's regex applies, then verify it is ciphertext:
   ```bash
   sops ansible/secrets/registry.sops.yaml     # set registry_sync_dockerhub_token: "<token>", save
   # re-encrypt so the new key is actually covered by the encrypted_regex:
   sops -d ansible/secrets/registry.sops.yaml > /tmp/r.yaml \
     && cp /tmp/r.yaml ansible/secrets/registry.sops.yaml \
     && sops -e -i ansible/secrets/registry.sops.yaml && rm -f /tmp/r.yaml
   grep registry_sync_dockerhub_token ansible/secrets/registry.sops.yaml   # MUST show ENC[...]
   ```
   A fresh encryption rotates EVERY ciphertext in the file (new IVs), so the diff cannot show whether
   the existing values survived the round-trip. Prove it by comparing plaintext hashes against `main`
   (prints no secrets), then confirm on the apply that the `ci` htpasswd and certbot-credentials
   tasks report `ok` (unchanged), not `changed`:
   ```bash
   git show main:ansible/secrets/registry.sops.yaml > /tmp/old.yaml
   for k in registry_ci_password cloudflare_dns_api_token registry_oidc_client_secret; do
     a=$(sops -d --input-type yaml --output-type yaml --extract "[\"$k\"]" /tmp/old.yaml | sha256sum)
     b=$(sops -d --extract "[\"$k\"]" ansible/secrets/registry.sops.yaml | sha256sum)
     [ "$a" = "$b" ] && echo "$k SAME" || echo "$k CHANGED"
   done; rm -f /tmp/old.yaml
   ```
4. `just registry` — renders `/etc/zot/sync-credentials.json` (mode 0640, root:zot) and restarts Zot.

Leaving `registry_zot_sync_dockerhub_user` empty runs the cache anonymously (works; cold fetches can
be rate-limited). quay.io / mcr.microsoft.com are not rate-limited.

## Pulling with Docker: the read-only `pull` identity

Anonymous clients have `read`, and curl/containerd/crane pull anonymously. **Docker on the classic
overlay2 image store does not**: zot answers its `/v2/` ping (any `docker/*` User-Agent) with a
basic-auth challenge, and dockerd then fails client-side with `no basic auth credentials` — the
manifest GET is never sent. Log in with the read-only user `pull` (zot policy `read` only):

```bash
# on a dev-worker (OpenBao af/dev-workers/common):
cred get common registry_pull_password | docker login registry.chifor.me -u pull --password-stdin
```

Its password is `registry_pull_password` in `ansible/secrets/registry.sops.yaml` (rendered into the
htpasswd by `just registry`), escrowed at `af/estate/registry` `pull_password`. Never hand out `ci`
(read/write) for pulls.

## Per-project push identities (app repos' publish CI)

An application repo's image-publish workflow gets its OWN zot user, confined to one repository
prefix (`read`/`create`/`update`, no `delete`), listed in `registry_zot_scoped_push_users`:

| user | repositories | consumer |
|---|---|---|
| `trueswarm-ci` | `trueswarm/**` | `cchifor/trueswarm` `.gitea/workflows/publish.yml` (Actions secrets `REGISTRY_USERNAME`/`REGISTRY_PASSWORD`) |
| `workbricks-ci` | `workbricks/**` | `cchifor/workbricks` image-publish CI (Actions secrets `REGISTRY_USERNAME`/`REGISTRY_PASSWORD`; Workbricks OR-12). The cell pulls anonymously, like every other image; the registry is LAN-only. |
| `strive-ci-push` | `strive/ci-build/**` | `cchifor/platform` e2e build-once (platform#2823): stack-smoke pushes `strive/ci-build/<image>:ci-<40-hex commit>` and the journey shards pull by digest. A `ci-` tag is kept while it is among the 10 most recently pushed in its repository OR was pushed within 24h (`registry_zot_strive_ci_build_keep_recent` / `_keep_within`); its retention policy precedes `strive/**`. NOT `strive/ci/**`: those 12 existing platform repos keep their `strive/**` rules. Entry carries `password_optional: true` until its SOPS key exists. |

A logged-in scoped user has **no** rights outside its prefix, not even read: zot's `anonymousPolicy`
applies only to unauthenticated requests. Use the credential for the push only (platform#2823 writes
it into a throwaway `DOCKER_CONFIG`); pulls stay anonymous or use `pull`.

zot authorizes a repo by its **longest matching pattern only** — policies are not merged — so each
scoped entry repeats the `**` grants (anonymous read, `ci`, `pull`, admins) via the template macro.
The password is `registry_scoped_push_passwords[<user>]` in `registry.sops.yaml` (escrow:
`af/estate/registry` `<user, - as _>_password`); it is set on the app repo as Actions secrets, and is
**never projected to dev-workers**. An agent ships an image by pushing a `v*` tag (or
`workflow_dispatch`) on its repo and then pinning the digest in the repo's deploy overlay, which
Flux reconciles. Adding a project = a new list entry + a SOPS key + `just registry` + the two repo
secrets (the role asserts the SOPS key exists before touching the htpasswd). To merge the entry
before the key exists, mark it `password_optional: true`: the play then skips that user with a
`SKIPPED` message instead of failing, and REMOVES any htpasswd line it has, so the user cannot log
in until the key exists. Drop the flag in the change that adds the key; while both exist, every
run prints a `STALE FLAG` message. Add the key
UNDER `registry_scoped_push_passwords` (the file's `encrypted_regex` covers that map; a new
top-level key would be stored in plaintext).

**Retiring a project:** removing the list entry drops its policy, but the htpasswd task only
adds/updates, so the user would still AUTHENTICATE. Remove it explicitly, then the other homes:
`ssh registry 'sudo htpasswd -D /etc/zot/htpasswd <user> && sudo systemctl restart zot'`, delete
the SOPS key and the `af/estate/registry` `<user, - as _>_password` seed field, and delete the app
repo's `REGISTRY_USERNAME`/`REGISTRY_PASSWORD` Actions secrets.

## Refresh a stale cached tag

On-demand sync caches a tag's content on first fetch and won't re-pull a mutable tag (e.g. upstream
ships a new `python:3.13-slim`) on its own. To force a refresh, delete the cached repo/tag on the LXC
so the next pull re-syncs:

```bash
# On the registry LXC (ssh registry):
skopeo delete --tls-verify=false docker://localhost/library/python:3.13-slim   # one tag
# or drop the whole cached repo and let it re-sync on demand:
sudo systemctl stop zot
sudo rm -rf /var/lib/registry/store/library/python
sudo systemctl start zot
```

`skopeo` honours the `ci` htpasswd creds for delete (anonymous is read-only); or use the stop/rm/start
path. Zot `gc` (every `gcInterval`, now 1h) reclaims the orphaned blobs.

> Note: if a cached mirror repo dir is left with a `.sync` staging dir but no `index.json` (e.g. a
> deleted/partial repo), on-demand re-sync will **not** repair it — `rm -rf` that repo dir entirely so
> the next pull syncs fresh.

## Disk / retention

The store lives on the mp0 data disk; its desired size is `kubernetes/infra/registry-lxc`
`data_gb` (including effective overrides). Check `pct config 5005` (ai-node3) and the filesystem for the live size. A
`storage.retention` policy (config.json.j2) bounds growth with policies evaluated **in order**,
each more specific one listed before the catch-all it would otherwise fall through to (zot applies
only the FIRST policy whose glob matches a repo):

- `strive/ci-build/**` (listed before `strive/**`) keeps a `ci-<40-hex commit>` tag while it is
  among the `registry_zot_strive_ci_build_keep_recent` (10) most recently pushed in its repo OR was
  pushed within `registry_zot_strive_ci_build_keep_within` (24h). Both rules sit in ONE keepTags
  entry, which zot ORs; the time floor stops a burst of pushes evicting a running pipeline's image.
  Every other tag there is pruned. These are cchifor/platform's per-pipeline e2e images
  (platform#2823); under `strive/**` a `ci-` tag would match no keep pattern and the hourly GC would
  delete it mid-pipeline. Never widen it to `strive/ci/**`: that namespace already holds platform
  repos (`strive/ci/platform-e2e-local`, ...) whose `latest`/`ailab*`/`sha-*` tags rely on
  `strive/**`, and a first-match policy there would strip them.
- `strive/**` keeps `latest`, `ailab` and `ailab-prev*`. It bounds two families, the `sha-<commit>` builds and
  the `ailab-<digest>` protect tags: each keeps the `registry_zot_strive_keep_recent` (30) most recently pushed
  tags OR any pushed within `registry_zot_strive_keep_within` (336h = 14 days). One keepTags entry per family,
  which zot v2.1.18 ORs, so the 30 newest always stay. The owner's target is 10. It drops to 10 once the platform
  protects airlock's admitted workflow runtimes (see the defaults comment) and the pin scan passes at 10. "Pushed" is zot's per-digest metaDB push time: re-tagging an
  old digest does not refresh it. An `ailab-<digest>` tag also stays while its digest was PULLED within the window:
  the platform's `protect-ailab-images` job GETs every pinned digest daily, which refreshes zot's pull time, so a
  pin stays protected however old it is. A digest that job does not fetch is not covered; the pin scan catches
  it. GC reclaims the rest. Until 2026-11-20 it was 100 sha- builds and every
  ailab-<digest> tag forever. Change it only through "Changing tag retention (dry run first)".
- `agentforge/**` keeps `latest` + the `registry_zot_agentforge_keep_recent` (40)
  most-recently-pushed tags per repo, under TWO patterns because the four repos do not all push
  the same tag shape: orchestrator, sandbox and p1-worker (agentforge's
  `.gitea/workflows/images.yml`: `VER=$(git rev-parse --short HEAD)`, `docker push
  ${repo}:${VER}` + `${repo}:latest`) push a bare short-sha tag (7-12 hex, NO `sha-` prefix — a
  different shape from strive's) plus `latest`, matched by `^[0-9a-f]{7,12}$`; agentforge-platform
  (`agentforge-platform/.gitea/workflows/images.yml`: `docker push "$IMAGE:${{ github.sha }}"` +
  `"$IMAGE:${{ steps.sha.outputs.short }}"`) pushes BOTH the full 40-hex `github.sha` tag and the
  short-sha tag, and never pushes `latest` at all — matched by the separate pattern
  `^[0-9a-f]{40}$`. Added 2026-09-02 after these four repos grew unbounded (184/184/183/240 tags)
  and filled the store — see ADR 0014 "Update (2026-09-02)".
- `**` (everything else, i.e. the mirror/cache repos) stays protected (`deleteUntagged:false`,
  keep all) so digest-pinned base images are never collected.

Both families' knobs exist for the same reason: the deployed ailab pins reference these images by
**digest**, and Zot's GC removes untagged manifests — a GC'd digest is un-rollback-able (only
rebuildable from git), so what is deployed must keep a tag that survives (for strive, its `ailab` /
`ailab-<digest>` tag; `scripts/zot-retention-pin-scan.py` checks it).
If the store ever fills again (writes fail with `blob upload unknown` / `provided digest did not
match` while reads still 200), grow it online — `pct resize <vmid> mp0 +NG` then bump `data_gb` +
`tofu apply` to match — and/or lower `registry_zot_strive_keep_recent` /
`registry_zot_agentforge_keep_recent`. For an immediate unblock without a role change: grow the
disk as above, or `skopeo delete` old `agentforge/**` tags by hand (see "Refresh a stale cached
tag" above for the delete pattern) — `ansible-playbook ansible/registry.yml` then converges the
new policy and GC (`gcInterval` 1h, `gcDelay` 2h) reclaims the freed blobs within ~3h.

## Thin-volume trim

> **The live registry, CT 5005 on ai-node3, is trimmed by ai-node3's nightly `pve-lxc-fstrim` sweep** (since
> 2026-11-22; [node-maintenance.md](node-maintenance.md), "LXC volume trim (nightly)"), like every other container.
> No separate unit: 5004's weekly unit below existed because its discard bursts hit the NVMe that carried cp1's etcd
> WAL, and the CP disks are on the QNAP since 2026-11-20 (ADR 0043). The sweep trims 4 GiB chunks with the IO-pressure
> guard: 13 min of work (`pve_lxc_fstrim_run_max_min`) inside ai-node3's 20-min slot (04:50-05:09, the rest is
> the unit's stop allowance), and its per-volume cursor continues a pass the next night.
> - **The first pass runs supervised**: start the sweep by hand on ai-node3 (`systemctl start pve-lxc-fstrim`; root,
>   via `scripts/node-ssh.py 192.168.0.4`). Its deadline counts from its own start, so each manual run does at most
>   13 min of work and exits; repeat it until both 5005 volumes have a complete pass:
>   `ls -l /var/lib/pve-lxc-fstrim/5005-rootfs.complete /var/lib/pve-lxc-fstrim/5005-mp0.complete` (the time of the
>   last complete pass; a `.cursor` file there means a pass is still in progress), and the beacon
>   `/var/lib/prometheus/node-exporter/pve_lxc_fstrim.prom` shows each volume's exit code (0). Read the result in
>   `journalctl -u pve-lxc-fstrim` and `lvs pve/vm-5005-disk-1 pve/data`. Supervised, so a multi-night first pass could neither keep 5003's volumes
>   waiting (never-completed volumes go first) nor trip `ProxmoxLxcTrimNotSucceeding` (50 h). On 2026-11-22 mp0 held
>   ~234 GiB allocated for 227 GiB in use.
> - **When the strive retention hold lifts** (bulk GC), run the same supervised passes the day after the first big
>   GC cycle, before the nightly sweep meets the backlog; expect node3 IO pressure and a thin-pool drop (`lvs
>   pve/vm-5005-disk-1 pve/data`). ai-node3 keeps thin-pool zeroing on, so a trimmed chunk that is written again
>   costs a zeroing write (node-maintenance.md "Thin-pool zeroing").
>
> The rest of this section describes CT 5004 on ai-node1, parked (stopped, net0 `link_down=1`, `backup` lock) until
> its decommission: its `registry-lxc-fstrim.timer` is **disabled**, and ai-node1's sweep still excludes 5004. The
> weekly unit block below stays until that decommission PR, because `ansible/roles/gitea_runner/tests/test-fstrim-chunked.sh`
> parses it.

Zot's GC frees blobs inside the container, but the host's `fstrim.timer` never reaches LXC mount
points, so freed space stays allocated in ai-node1's `local-lvm` thin pool. By 2026-10-06 mp0 held
377 GB allocated for 170 GB in use. That thin-pool fill measurably slowed the shared consumer QLC
NVMe that also carries cp1's etcd WAL (plans/2026-10-07-etcd-leader-churn-plan.md).
The pool's zeroing setting, which multiplies writes when trimmed chunks are reused, is a separate
knob: node-maintenance.md "Thin-pool zeroing".

`scripts/lxc-fstrim-chunked.sh <ctid> <mpN>` (tests: `scripts/tests/test-lxc-fstrim-chunked.sh`, run
in CI) works **entirely host-side**:
- It resolves the volume with `pct config` + `pvesm path` and reads its ext4 geometry with `tune2fs`.
- It mounts it privately (`nosuid,nodev,noexec`; the kernel shares the superblock with the
  container's mount).
- It trims with the host's `fstrim`, 8 GiB at a time.
- It never executes anything from inside the container. The first version ran `df`/`fstrim` through
  `nsenter -m`, i.e. the container's binaries with host-root credentials. It was withdrawn after the
  2026-10-07 Codex review.

It waits while host IO pressure (`/proc/pressure/io` some avg10) is above 45%, refuses to trim if
pressure is unreadable, and pauses 15 s between chunks. `touch /run/lxc-fstrim-chunked.stop` halts it,
including during a pressure wait.

Exit codes: 0 done or stopped, 1 volume, geometry or mount problem, 2 bad settings, 3 pressure never
dropped, 4 pressure unreadable, 5 an fstrim failed, 143 stopped by `systemctl stop` (after the in-flight chunk, mount released), 6 the host-side mount could not be released (`findmnt | grep lxc-fstrim`, then `umount` it by hand), 7 another run holds `/run/lxc-fstrim-chunked.lock`. Install on ai-node1 (root, via
`scripts/node-ssh.py`):

```bash
install -m 0755 lxc-fstrim-chunked.sh /usr/local/sbin/lxc-fstrim-chunked.sh
cat > /etc/systemd/system/registry-lxc-fstrim.service <<'UNIT'
[Unit]
Description=Chunked, IO-pressure-guarded fstrim of the registry LXC data volume (ct 5004 mp0)
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/lxc-fstrim-chunked.sh 5004 mp0
Nice=10
# The overall deadline. A oneshot is "activating" for its whole run, so RuntimeMaxSec= does nothing
# here (checked on systemd 255): TimeoutStartSec= is the limit. Latest start 03:46 Sunday (03:30 +
# 15 min RandomizedDelaySec + 1 min AccuracySec); SIGTERM by 23:31, done by 23:36 with the stop time
# below, so a run that keeps waiting out IO pressure never reaches Monday's ai-node1 runner windows.
TimeoutStartSec=19h45min
# On stop the script waits for the in-flight FITRIM (8 GiB at up to ~23 s/GiB is ~3 min), then
# unmounts (exit 143); leave it time to, and record that deliberate stop as clean rather than a
# failed unit. (A stop at the deadline above still shows as failed, Result=timeout: by design.)
TimeoutStopSec=300
SuccessExitStatus=143 130
UNIT
cat > /etc/systemd/system/registry-lxc-fstrim.timer <<'UNIT'
[Unit]
Description=Weekly registry LXC trim, CI-quiet hours (after the 00:00Z infra-pg dump and 02:00Z Velero)
[Timer]
OnCalendar=Sun *-*-* 03:30:00 UTC
RandomizedDelaySec=15min
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload && systemctl enable --now registry-lxc-fstrim.timer
```

**The script is under the role now (2026-11-20).** `pve_base` (tag `lxc_fstrim`, on for ai-node1) installs
`/usr/local/sbin/lxc-fstrim-chunked.sh` from the repo with the nightly LXC sweep
([node-maintenance.md](node-maintenance.md), "LXC volume trim (nightly)"), so the `install` line above is
for a host without it. The weekly unit above is still hand-installed; it runs the role's copy, whose
defaults are unchanged (no deadline, the same chunk and pauses). The sweep excludes 5004 on ai-node1 and
shares this script's lock and stop file, so the two never trim at once.

**The Sunday deadline (2026-11-19, #1858).** The script's pressure-wait budget (`PSI_WAIT_MAX_S`) is
per chunk, so on this 384 GiB volume a run can wait for many hours. `TimeoutStartSec=19h45min` (with
`TimeoutStopSec=300` for the in-flight chunk) ends it by 23:36 Sunday at the latest, whatever the
pressure: systemd SIGTERMs it, and the script stops at once, even mid-wait, releases the mount and exits
143. A run cut off there shows as failed (`Result=timeout`). The next week starts again from chunk 1.
**The live unit on ai-node1 predates this:** after merge, re-run the `cat > …registry-lxc-fstrim.service`
block above on the node (root, via `scripts/node-ssh.py`), then `systemctl daemon-reload`, and check
`systemctl show -p TimeoutStartUSec,TimeoutStopUSec registry-lxc-fstrim.service`.

ai-node1's CI runners trim their own disks nightly and stay off this trim's span: from 15 min before it
can start to 15 min after that deadline ([ci-runners.md](ci-runners.md), "Thin-pool trim"). ci-runner-1
and -4 end by 03:15 UTC, ci-runner-9 (04:15) skips Sundays (`gitea_runner_fstrim_days`), and the deadline
keeps the trim out of Monday's 00:00 window. Moving this timer, or raising the deadline, means moving their
windows or days too: `ansible/roles/gitea_runner/tests/test-fstrim-chunked.sh` reads `OnCalendar`,
`RandomizedDelaySec`, `TimeoutStartSec` and `TimeoutStopSec` above and fails if they collide, Sunday
into Monday included.

**Verify a run, including that it did not hurt etcd.** A trim is a discard burst on the same drive as
cp1's etcd WAL.
- `journalctl -u registry-lxc-fstrim`: one line per chunk, then `end: trimmed_total=<bytes> pool=<%>`.
  `lvs pve/vm-5004-disk-1 pve/data` should show the drop.
- Over the run's window, compare with the same-length window before it, per member (Prometheus):
  - `histogram_quantile(0.5|0.99, sum by (instance, le)(increase(etcd_disk_wal_fsync_duration_seconds_bucket[<window>])))`;
  - fsyncs over 1.024 s;
  - `increase(etcd_server_leader_changes_seen_total[<window>])`;
  - the leader at the time (`etcd_server_is_leader`).
- **Stop criteria** (touch the stop file):
  - cp1's WAL fsync p99 above 1 s for 2 consecutive minutes during the run;
  - any leader change while cp1 leads;
  - a `ControlPlaneRestartWave`.
- Recorded supervised run, 2026-10-06 19:24-19:44Z (the same chunk/pressure logic, an earlier
  version of the script):
  - 212 GB trimmed; pool 71.6 -> 59.8%; host IO PSI about 33 -> 15%.
  - cp1 fsync p50 83 -> 10 ms and p99 846 -> 734 ms vs the preceding 12 minutes, while node1 runner
    writes doubled.
  - One leader change at about 19:40Z, while cp2 (node2) led. The trim was on node1, so cp1 was only
    a follower; the trim is not a plausible cause.

## Capacity recovery, 2026-09-13

Platform main build 32171 repeatedly failed image uploads with `blob upload unknown to registry`.
Read-only checks found registry LXC 5004's 256 GiB mp0 filesystem at 100%, with less than 1 GiB
available. Node1's local-lvm pool had approximately 771 GiB free (56.07% data use; 2.29% metadata
use). The capacity target is 384 GiB. This preserves retained image tags and their rollback
history; no retention change or image deletion is part of this recovery.

After reviewing the capacity change, check the current size and pool availability again, then
use the existing grow-only recovery path on node1:

```sh
pvesm status
pct config 5004
pct resize 5004 mp0 384G
pct exec 5004 -- df -h /var/lib/registry
```

Do not resize a different mount or shrink a volume that already exceeds the target. Before a
subsequent OpenTofu apply, use the existing local state, verify the effective `data_gb` is 384
(or larger if already grown), and review the plan. A variable override can supersede the module
default; refreshing state alone does not correct an overridden desired size.
Verify a complete platform image build and its pin manifest before resuming a deployment.

## Verify it's working

```bash
# Catalog should list cached upstreams after CI has pulled through it:
curl -s https://registry.chifor.me/v2/_catalog | jq
# A transparent-path manifest resolves on demand (200), proving the mirror layout:
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Accept: application/vnd.oci.image.index.v1+json' \
  https://registry.chifor.me/v2/library/python/manifests/3.13-slim
# Zot logs show the upstream sync + which registry served it:
ssh registry 'sudo journalctl -u zot --since "10 min ago" | grep -E "syncing image|filtered out"'
```

The full, fail-closed end-to-end gate lives in `cchifor/platform`:
`scripts/ci/verify-registry-mirror.sh` (and the `registry-mirror-verify.yml` workflow) — it stands up a
throwaway Zot with this same sync shape on an internet-less network and proves base-image pulls resolve
only through the cache.
