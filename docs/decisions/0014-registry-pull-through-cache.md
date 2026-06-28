# ADR 0014 — registry.chifor.me as a pull-through cache for CI base images

**Status:** ACCEPTED (2026-06-22). Zot `sync` (on-demand) added to the `registry_zot` role; runner
`daemon.json` points `registry-mirrors` at `https://registry.chifor.me`. Validated end-to-end by
`cchifor/platform`'s `scripts/ci/verify-registry-mirror.sh` (a real Zot v2.1.2 container, fail-closed
on an internet-less network). Pairs with `cchifor/platform` PR for issue #565.
**Relates to:** ADR 0013 (self-hosted runners — the consumers), ADR 0001 (OpenTofu + Ansible).

## Context
`cchifor/platform` CI builds ~21 Docker images. Their base images are pulled from Docker Hub
**anonymously** (`python:3.13-slim` in 26 Dockerfiles, `node:22-slim`,
`nginxinc/nginx-unprivileged:1.27-alpine`, `busybox`; plus `quay.io/keycloak/keycloak:25.0` and
`mcr.microsoft.com/playwright`). Under load the anonymous pulls hit Docker Hub's rate limit:

```
toomanyrequests: You have reached your unauthenticated pull rate limit.
```

reddening `smoke`/`e2e`/`build`. The only defense was `scripts/ci/with-retry.sh` backoff — a band-aid
that has exhausted its attempts. We already run **Zot** (`registry.chifor.me`, ADR 0013's push/pull
target) but only for *our own* images, not as an upstream cache.

## Decision
Turn `registry.chifor.me` into an **on-demand pull-through cache** for the upstream base images, and
point the runners' container runtime at it as a **mirror** — without hardcoding `registry.chifor.me`
into any Dockerfile `FROM` line (images stay portable; local dev is unaffected).

1. **Zot `sync`, `onDemand: true`, transparent path layout** (`registry_zot/templates/config.json.j2`).
   Three upstreams with **disjoint** `content.prefix` and **no `destination`/`stripPrefix`**, so Zot
   serves each upstream at the exact requested path (`/v2/library/python/...`, `/v2/keycloak/...`,
   `/v2/playwright/...`) — which is what a Docker daemon `registry-mirrors` and a BuildKit
   `[registry."docker.io"] mirrors` require:
   - `quay.io` → `keycloak/**`
   - `mcr.microsoft.com` → `playwright`, `playwright/**`
   - `registry-1.docker.io` → `library/**`, `nginxinc/**`  (no global `**` — avoids flat-namespace
     ambiguity across upstreams)
2. **Runner daemon mirror** (`github_runner/templates/daemon.json.j2`):
   `"registry-mirrors": ["https://registry.chifor.me"]`. The Docker **daemon** mirror is Docker-Hub-only
   but transparently covers every `docker.io` pull on the **Engine** path (`docker compose build`) —
   the actual 429 source. quay/mcr are mirrored on the **BuildKit** path via config in the platform
   workflows (`build.yml`, `e2e-preflight.yml`), not here.
3. **Upstream auth on the cache (recommended).** On a cache miss Zot pulls from Docker Hub itself,
   copying the *full multi-arch manifest list* per image — anonymous egress exhausts Docker Hub's
   100-pulls/6h limit fast and the cold fetch then 429s (observed during validation). A free Docker
   Hub token on the docker.io upstream (`registry_zot_sync_dockerhub_user` + SOPS
   `registry_sync_dockerhub_token`) lifts the cache's own egress to 200/6h+. Optional but advised.

## Alternatives rejected
- **Hardcode `registry.chifor.me/dockerhub/...` in `FROM`** — couples images to the private registry,
  breaks local dev + portability. ❌
- **`with-retry.sh` backoff as the fix** — masks, doesn't resolve. Kept only as a thin backstop. ❌
- **`docker login docker.io` on the runners** — raises the limit but no caching, no outage resilience. ❌
- **Polled mirroring** — Zot's docs warn against polled Docker Hub mirroring (rate limits, no catalog);
  on-demand is the supported shape.
- **A dedicated `registry:2` proxy cache** — viable fallback if Zot couldn't serve transparent paths;
  validation proved Zot can, so we reuse the registry we already run. ✅

## Consequences
- **Mutable-tag staleness.** On-demand caches a tag's content on first fetch and does not re-pull a
  mutable tag (`:3.13-slim`) without polling (which we don't enable). For CI base images this is an
  acceptable, *more reproducible* trade-off. Manual refresh: see `docs/runbooks/registry-cache.md`.
- **Single point of dependency.** If `registry.chifor.me` is down, the Docker daemon mirror **soft-falls
  back** to Docker Hub directly (degraded, not broken). `with-retry.sh` remains the backstop.
- **Disk.** Cached upstream images share the registry LXC's 64 GiB data disk with our own images; Zot
  `gc`/`dedupe` (already on) bound growth. Watch headroom; bump the `mp0` disk if needed.
- **Contract sync.** The runner `daemon.json` mirror is also encoded in `cchifor/platform`'s
  `infra/runner/provision.sh` and asserted by its `runner-health.yml` canary — keep the two in step.

## Update (2026-06-23) — mirror.gcr.io upstream, catch-all docker.io, retention, 192 GiB

A CI outage exposed three gaps in the original shape; all fixed here (applied live, then committed):

1. **Anonymous Docker Hub 429.** The "soft fallback / `with-retry` backstop" was insufficient: on a
   cache miss Zot pulled Docker Hub **anonymously** and hit `toomanyrequests: unauthenticated pull
   rate limit`, failing CI. Fix: point the **docker.io sync upstream at Google's anonymous Docker
   Hub pull-through `https://mirror.gcr.io`** (primary), with `registry-1.docker.io` as failover.
   mirror.gcr.io serves the **same digests** (digest-pinned pulls match) and has no anon cap, so
   `registry_zot_sync_dockerhub_user` is now optional (it only authenticates the failover path).
2. **Disjoint prefixes were too narrow.** docker.io was `library/**` + `nginxinc/**` only, so the
   platform stacks' other docker.io images (`pgvector`, `grafana`, `prom`, `qdrant`, `valkey`,
   `rustfs`, `curlimages`, `dpage`, …) 404'd on the mirror. Fix: docker.io is now the **catch-all**
   (`"**"`), kept LAST in the registries list; quay.io (`keycloak/**`) and mcr (`playwright*`) keep
   specific prefixes and are listed first, so the "no upstream confusion" guarantee holds via order.
3. **`gc`/`dedupe` did NOT bound growth** (the optimistic bit of the old "Disk" consequence): with no
   retention, one `sha-<commit>` tag accrued per repo per platform `main` build until the 64 GiB
   store hit 100% → blob writes failed (`blob upload unknown` / `provided digest did not match`)
   while reads still served. Fix: a `storage.retention` policy — `strive/**` keeps `latest` + the 100
   most-recently-pushed sha tags (GC reclaims the rest); the mirror/cache repos are explicitly
   **protected** (`deleteUntagged:false`, keep all) so digest-pinned base images are never collected.
   The mp0 data disk was bumped **64 → 192 GiB** for headroom.

## Update (2026-06-28) — strive retention 25 → 100

`registry_zot_strive_keep_recent` was raised **25 → 100**. The count-based depth of 25, at the
measured ~5-6 strive builds/day, only covered ~4 days of `sha-<commit>` history — so it GC-pruned
the **deployed** `sha-b80a376c` build family (9 strive services + 2 workers) out from under the
live ailab pins. The blobs stayed on node caches, so running pods kept working, but every reschedule
hit a registry **404 → ImagePullBackOff** (and wedged the gatekeeper HelmRelease into an
upgrade→rollback loop). A deployed digest that's GC'd is unrecoverable (a bare digest can't be
re-pulled, only rebuilt), and re-pinning forward off a pruned sha can surface newly-required config
— so the retention window must comfortably exceed how far the ailab pin can lag main. 100 sha tags
≈ 2-3 weeks of headroom; still count-based (NOT a time window, which prunes non-deterministically).
Repos sit ~28 tags today, so no immediate disk impact; the 192 GiB disk + `gc`/`dedupe` bound growth.
