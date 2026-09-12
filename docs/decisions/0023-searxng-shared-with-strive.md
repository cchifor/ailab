# ADR 0023 — SearXNG in `dsh` is shared with the strive platform's search steps

**Status:** ACCEPTED (2026-09-12). Implemented in the PR that carries this ADR: one extra `from`
element on `searxng-allow` in `kubernetes/apps/apps/dsh/searxng.yaml`. The consuming side lands in
`cchifor/platform` (the `platform_search` catalog entry, `PLATFORM_SEARCH_URL` on the workflow API
and worker, and the workflow chart's `networkPolicy.extraEgress` to `dsh`/`app=searxng`:8080).
**Relates to:** the dsh runbook (`docs/runbooks/dsh.md` — why SearXNG exists and what it does not
do), ADR 0019 (Flux as the only path into the cluster; this element ships through it, never by
hand), ADR 0006 (Cilium, which is what makes a NetworkPolicy real here).

## Context

SearXNG was deployed in `dsh` for one reason: to give the dsh agent web search without giving the
pod that runs model-authored code a route to the internet (the header of `searxng.yaml`). Its
policy admitted ingress from the `app=dsh` pod only, and egress to DNS plus 443 on public addresses
with every private range excluded.

The strive platform in `strive-ailab` is gaining a **Platform Search** connection type — a managed,
zero-setup search backend for its `web_search` workflow steps, first used by the Topic News app —
and the platform's program plan (cchifor/platform, "App Parameters, Platform Search and the Topic
News journal", D4) names this instance as that backend: `searxng.dsh.svc.cluster.local:8080`,
`/search?format=json`. Today those steps need a Tavily key (paid; not held for ailab) or run a fake
provider.

Verified live before this was written (2026-09-12, `kubectl` against the ailab cluster):

- `strive-ailab` has NO default-deny. The `workflow` and `integration` pods are selected by their
  chart NetworkPolicies (Ingress+Egress). The `workflow-worker` pod (labels
  `strive.io/component=hatchet-worker`, `strive.io/service=workflow-worker`) is selected by no
  policy at all — it already has unrestricted egress, the internet included.
- `strive.io/service` is stamped on every chart-managed service pod by the platform chart
  (`deploy/helm/templates/_helpers.tpl`: `strive.io/service: {{ .Chart.Name }}`) and on the raw
  worker manifests (`deploy/components/workers/workflow-worker.yaml`). Only the three pods
  `workflow-worker`, `workflow` and `integration` carry one of the three admitted values; every
  other `strive.io/service` value in the namespace (`postgres` on the CNPG pods, `ci-objectstore*`,
  `airlock` on the reaper CronJob pods, the other hatchet workers `digest-worker` /
  `integration-worker` / `mcp-worker`, and the rest of the chart services) is excluded by `In`.
- The platform's sandboxes run in `strive-sandboxes-ailab`, a different namespace. The only
  RoleBinding in `strive-ailab` is CNPG's `strive-pg`; no tenant-facing identity can create pods
  there.
- The live `searxng-allow` object was byte-identical to `main`'s render (`kubectl diff`, exit 0),
  and a `/healthz` probe from the worker pod timed out — the current policy does block it.

Three shapes were on the table:

1. **A second SearXNG for the platform**, in its chart or as a raw component. Two instances with
   the same engine list behind the same residential IP double the CAPTCHA/suspension exposure
   `searxng.yaml` documents, duplicate a settings file whose every line was measured, and add a
   second internet-egress pod for no isolation gain — the callers are the platform's own services
   either way.
2. **Tavily per tenant**, which is what `web_search` does today when handed a key. A paid vendor, a
   secret per tenant, and every query leaving for a third party: the exact trade SearXNG was chosen
   to avoid for dsh.
3. **Share this instance** by admitting the platform's search services to `searxng-allow`. One
   `from` element, three service names, egress untouched.

## Decision

**Option 3.** `searxng-allow.spec.ingress[0].from` gains ONE element:

```yaml
- namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: strive-ailab } }
  podSelector:
    matchExpressions:
      - { key: strive.io/service, operator: In, values: [workflow-worker, workflow, integration] }
```

- **Both selectors in one element.** A `from` entry carrying a namespaceSelector AND a podSelector
  matches only pods that satisfy both. As two entries they would be OR'ed, and the namespaceSelector
  alone would admit every pod in `strive-ailab` — the trap `dsh-allow` already documents for `edge`.
- **`kubernetes.io/metadata.name`** is stamped on every namespace by the API server and is
  immutable, so the namespace half cannot be spoofed by relabelling.
- **These three services, and only these.** `workflow-worker` runs the `web_search` handler.
  `workflow` (the API) executes the same handler when it dry-runs a workflow — its dry run skips
  only the handlers that mutate something outside the run. `integration` owns the connection and is
  admitted for its health probe, which is passive in the first release and so does not dial yet.
  `In` rather than `Exists` on `strive.io/service`, because `Exists` would admit every platform
  service (airlock, web, tms, …) that has no business here.
- **Port 8080 only; egress untouched.** SearXNG still reaches DNS and 443 on public addresses with
  the private ranges excluded. The Service stays ClusterIP; no Ingress, no cloudflared route.
- **Where it lives:** this repo, through Flux's `apps` Kustomization, like every other object in
  `dsh` (ADR 0019). The platform side — `PLATFORM_SEARCH_URL` set to
  `http://searxng.dsh.svc.cluster.local:8080` on the workflow API and worker, plus the chart's
  `extraEgress` to `dsh`/`app=searxng`:8080 for the chart-selected API pod — is a `cchifor/platform`
  change and ships separately.

## Consequences

- **What a strive service pod gains is search queries against public engines through this pod** —
  the capability the dsh agent already has — and nothing toward the internet that the platform's
  own policies do not already grant it (the worker's egress is unrestricted today; the API pod's is
  whatever its chart policy says). SearXNG has no page-fetch surface: it takes a query string, never
  a URL to GET (`image_proxy: false`; the runbook's "SearXNG does not fetch arbitrary pages for a
  caller" holds for every caller). The containment argument in `searxng.yaml` is therefore
  unchanged; what changed is who may ask it a question. `dsh-allow` is untouched, so `strive-ailab`
  still cannot reach the dsh pod itself.
- **The tenant boundary is the platform's, not SearXNG's.** SearXNG has no authentication; this
  policy admits by pod identity. Every strive tenant's Platform Search connection shares the one
  instance, and the platform attributes queries to tenants through its own connection ids. Nothing
  here logs queries (`general.debug: false`) and there is no per-tenant accounting; wanting that is
  the point at which option 1 becomes the right answer.
- **Shared engine budgets.** A CAPTCHA from an engine suspends it for both consumers
  (`suspended_times.SearxEngineCaptcha: 300`). The platform's default cadence — one Topic News run a
  day per app, at most eight queries a run against this instance (one per topic), plus dry-run
  validations — is a handful of queries a day. The cadence is an enum of four presets, not a free
  cron, so the maximum is bounded too: the hourly preset is 24 runs × 8 topics = up to 192 queries
  a day per app, and that ceiling grows only with the number of apps. A runaway platform loop would
  show up as dsh searches degrading, and `/stats/errors` (metrics stay on) is where to look. The
  uwsgi ceiling (`SEARXNG_UWSGI_WORKERS=2` × `SEARXNG_UWSGI_THREADS=2`, four concurrent requests)
  is shared too; queued requests wait rather than fail.
- **The platform's own SSRF guard would refuse this address** (an RFC1918 ClusterIP), which is why
  its SearXNG provider talks to `PLATFORM_SEARCH_URL` with a plain client rather than through that
  guard: an operator-configured cluster address, not user input. That is the platform's call,
  recorded here because it is why "just point the crawler at it" was never a shape on the table.
- **If `strive-ailab` ever gets a default-deny**, the worker (a raw manifest, not chart-selected)
  needs an explicit egress rule to `dsh`/`app=searxng`:8080, and an active `integration` probe would
  need the same through `integration.networkPolicy.extraEgress`; the API pod already gets one from
  the chart. This element on the `dsh` side is unaffected either way.
- **Verification** (read-only). `kubectl -n dsh get netpol searxng-allow -o yaml` shows the element
  once Flux's `apps` Kustomization has reconciled. From a worker pod,
  `kubectl -n strive-ailab exec deploy/workflow-worker -- python -c "import urllib.request;print(urllib.request.urlopen('http://searxng.dsh.svc.cluster.local:8080/healthz',timeout=5).status)"`
  answers `200` (it timed out before this element), and `/search?q=test&format=json` returns JSON.
  The platform's own gate for the round trip is a Topic News run whose `news_item` rows carry a
  `source` and a `published_at` — the fake provider produces neither. The manifest's shape (one
  rule, two peers, the AND'ed strive element, port 8080, egress unchanged) is pinned by
  `scripts/tests/test_searxng_netpol.py` (`python3 -m unittest discover -s scripts/tests -p "test_*.py"`).
