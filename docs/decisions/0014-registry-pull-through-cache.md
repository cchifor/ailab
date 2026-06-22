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
