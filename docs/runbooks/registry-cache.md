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

The `sync` config is rendered into `/etc/zot/config.json`; a change triggers the `restart zot` handler.

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
3. Add the token to SOPS:
   ```bash
   sops ansible/secrets/registry.sops.yaml     # set registry_sync_dockerhub_token: "<token>"
   ```
   (The `.sops.yaml` `registry` rule already encrypts `registry_sync_dockerhub_token`.)
4. `just registry` — renders `/etc/zot/sync-credentials.json` (mode 0640, root:zot) and restarts Zot.

Leaving `registry_zot_sync_dockerhub_user` empty runs the cache anonymously (works; cold fetches can
be rate-limited). quay.io / mcr.microsoft.com are not rate-limited.

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

The store lives on the mp0 data disk (now **192 GiB**, `kubernetes/infra/registry-lxc` `data_gb`). A
`storage.retention` policy (config.json.j2) bounds growth: `strive/**` keeps `latest` + the
`registry_zot_strive_keep_recent` (100) most-recently-pushed `sha-<commit>` tags per repo and GC
reclaims the rest; mirror/cache repos are protected (`deleteUntagged:false`, keep all) so
digest-pinned base images are never collected. If the store ever fills again (writes fail with
`blob upload unknown` / `provided digest did not match` while reads still 200), grow it online —
`pct resize <vmid> mp0 +NG` then bump `data_gb` + `tofu apply` to match — and/or lower
`registry_zot_strive_keep_recent`.

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
