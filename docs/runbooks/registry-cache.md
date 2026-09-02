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
`storage.retention` policy (config.json.j2) bounds growth with policies evaluated **in order**,
each more specific one listed before the catch-all it would otherwise fall through to:

- `strive/**` keeps `latest` + the `registry_zot_strive_keep_recent` (100) most-recently-pushed
  `sha-<commit>` tags per repo and GC reclaims the rest.
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

Both count-based knobs exist for the same reason: the deployed ailab pins reference these images by
**digest**, and Zot's GC removes untagged manifests — a GC'd digest is un-rollback-able (only
rebuildable from git), so each window must comfortably outlive how far the live pin can lag `main`.
If the store ever fills again (writes fail with `blob upload unknown` / `provided digest did not
match` while reads still 200), grow it online — `pct resize <vmid> mp0 +NG` then bump `data_gb` +
`tofu apply` to match — and/or lower `registry_zot_strive_keep_recent` /
`registry_zot_agentforge_keep_recent`. For an immediate unblock without a role change: grow the
disk as above, or `skopeo delete` old `agentforge/**` tags by hand (see "Refresh a stale cached
tag" above for the delete pattern) — `ansible-playbook ansible/registry.yml` then converges the
new policy and GC (`gcInterval` 1h, `gcDelay` 2h) reclaims the freed blobs within ~3h.

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
