# Runbook: specpulse on ailab

specpulse (`cchifor/specpulse`) is a single-replica web app with one SQLite database. It runs at
**https://specpulse.chifor.me**, which is **private**: the admin tier of the LAN (people's devices and the owner's VM)
only, never the tunnel. It uses the app's own login.

Owner decisions (2026-11-26, operator session):
- GitOps on ailab;
- private exposure with the app's own login, no Authelia;
- a new Router key on `claude-sub-opus` only;
- start empty;
- deploy only from specpulse `main`.

## Where things are

| What | Where |
|---|---|
| Manifests | `kubernetes/apps/apps/specpulse/`. `manifests.yaml` is a **verbatim copy** of the specpulse repo's `deploy/manifests/manifests.yaml` (the chart rendered; no Secret, no infrastructure names). Every cluster value is set by `kustomization.yaml`: the image digest, the PVC `storageClassName: qnap-iscsi`, and the seats file. |
| Secret | `specpulse-secrets.sops.yaml` (SOPS/age): `secret-key` (Fernet; it must stay the same for the life of the database), `session-secret`, `router-key`, and optionally `github-token`. |
| Router key | `key_f01d078c1542`, route `claude-sub-opus` only, 90 days. It is separate from the controller's key `specpulse`. |
| Image | `registry.chifor.me/specpulse/specpulse`, pushed by specpulse's `publish.yml` as `sha-<40 hex>` with the scoped push user `specpulse-ci` (`specpulse/**`; docs/runbooks/registry-cache.md). |
| Edge | The `specpulse` entry in `kubernetes/edge/hostnames.yaml` (`exposure: private`, `auth: native`). The Caddy route admits the admin tier only. |
| Data | PVC `specpulse-data`, 5Gi, `qnap-iscsi` (RWO, Retain). It holds the database, clones and `/data/backups`. Velero file-system backup runs daily; the pod's pre-backup hook writes an app-consistent copy first. |

## Update to a new specpulse release
1. specpulse merges to `main`. Run `publish.yml` (workflow_dispatch on `main`) and note the run and the commit.
2. Resolve the digest from the registry, not from logs (accept every manifest type, so the digest is the one kubelet pulls):
   `curl -sI -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' https://registry.chifor.me/v2/specpulse/specpulse/manifests/sha-<commit> | grep -i docker-content-digest`.
3. Copy `deploy/manifests/manifests.yaml` from that commit over `kubernetes/apps/apps/specpulse/manifests.yaml`. Set
   `images[0].digest` in `kustomization.yaml`. Check that the specpulse README lists no new placeholder.
4. `kubectl kustomize` the directory with a dummy Secret: no `PLACEHOLDER` may remain.
5. Open the PR. Flux rolls it as a Recreate, and the `backup` init container copies the database before migrations.

## Dependencies to keep in mind
- **The Router seat hairpins through Cloudflare.** `https://router.chifor.me` resolves from pods to Cloudflare public addresses
  (checked 2026-11-26), which the NetworkPolicy's 443 egress allows; it excludes 10/8, 172.16/12, 192.168/16 and
  169.254/16. If the cluster resolver ever returned the edge VIP (192.168.0.51) for it, every Router call would fail.
  The Router admits only its own edge component, so an in-cluster path would be an architected Router change
  (a policy plus a cluster-local URL), not a specpulse one.
- **Changing the seats patch:** update the `checksum/seats` value in `kustomization.yaml` in the same change (the comment
  there gives the command), or the pod keeps the old seat until `kubectl -n specpulse rollout restart deploy/specpulse`.
- **The ingress rule** in the rendered manifests admits namespace `edge`, pods `app.kubernetes.io/name=edge-proxy`
  (the chart's defaults, verified against the live proxy pods on 2026-11-27). If the edge proxy is ever relabelled,
  change the chart values in specpulse, not this copy.
- **The DNS record** for specpulse is already in `kubernetes/edge/dns-records.json` (the renderer emits every registry host;
  `lan_dns` only governs the gateway's local record). A private A record to the VIP serves only the admin tier, so an
  earlier `just edge-dns-apply` for another host publishing it is harmless; the planned apply follows the LAN smoke.

## First admin (the owner)
The password is read from stdin, never from the command line:
`kubectl --context admin@ai -n specpulse exec -i deploy/specpulse -c specpulse -- specpulse user add --db /data/specpulse.db <name> --role admin < <password-file>`.

## GitHub token (for the private repositories it analyses)
The token is a **fine-grained** token whose **resource owner is the organization that owns the analysed repositories**
(`kartrainc` for `kartra-ai` and `kartra-v4`), with Repository → **Contents: Read-only** and nothing else. A token
whose resource owner is a personal account cannot see an organization's private repositories at all: they answer 404,
and the run fails at its git preflight (2026-11-27, the first token). Never use a `gh` login or a classic `repo` token.
Those can write to every repository the account reaches, including the GitHub mirror Flux deploys from.

1. **Validate before storing.** The token goes only to api.github.com, in a header, and is never printed. It must read
   each analysed repository and its branch. A non-mutating write probe (`POST /repos/R/git/refs` with an all-zero
   sha) must answer 403 on each of them and on `cchifor/ailab`; 422 means write access, so stop. It must see no
   private repository outside that organization.
2. **Store it** as `stringData.github-token` with a `sops edit` editor shim that reads it from the owner's clipboard or
   a private file (never the terminal or argv). Then clear the clipboard or delete the file.
3. **Take effect.** The `github-token` init container sets it (`specpulse secret set github_token`) on every start, so
   the Secret stays the source of truth. To switch the live app WITHOUT a restart (a long analysis is running), pipe
   the same value into the running container: `specpulse secret set github_token --value-file <tmp> --db
   /data/specpulse.db`, with the temp file under `/tmp`, removed at once. Otherwise roll the pod
   (`kubectl -n specpulse rollout restart deploy/specpulse`).
4. **Revoke the token it replaces** on GitHub, and confirm the old one answers 401. A rotation is complete only then.

Rotation record: 2026-11-27, `e0784b` (personal resource owner, from #2054) was replaced by `310274` (`kartrainc`,
Contents read-only; #2057). `e0784b` was revoked by the owner the same day; `/user` and both repositories answer 401.

## Making it reachable by name (after the first deploy)
1. `just edge-smoke --host specpulse` from the LAN. It expects 303 to `/login` anonymously.
2. Then set `lan_dns: true`, run `just edge-render`, and merge. Then `just edge-gateway-plan` / `edge-gateway-apply`, and
   `just edge-dns-apply` for the A record to the VIP (docs/runbooks/edge-ingress.md "Add a hostname").

## Restart, restore, recovery
Restart only with `kubectl -n specpulse rollout restart deploy/specpulse`. To restore or repair the volume, use the
specpulse chart's `maintenance: true` mode (an idle pod with the same mounts). Its README and `docs/operations.md`
section 14 have the steps.
