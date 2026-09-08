// A dsh web SEARCH provider backed by this estate's own SearXNG.
//
// WHY THIS FILE EXISTS RATHER THAN AN npm PLUGIN. dsh resolves a profile row's `name:` as an ESM
// specifier relative to the profile's cordis.yml (/dsh-home/profiles/web/), NOT from the dsh
// installation tree: profile-boot calls boot() with no bareModuleBaseUrl, so bare names resolve
// through /dsh-home/profiles/web/node_modules and /dsh-home/profiles/node_modules -- and the
// latter holds only symlinks to dsh's OWN dependency closure. A package npm-installed into
// /app/<ver>/node_modules alongside @deepseek-ai/dsh is therefore invisible, and naming it in a
// row is a FAIL-LOUD boot crash, not a degraded tool:
//     Error: dsh: plugin tree failed to load: ... failed to import loader entry searxng-web
//     (dsh-searxng-web): Cannot find package 'dsh-searxng-web' imported from /dsh-home/profiles/web/
// MEASURED in the running pod, not inferred. A relative `name: './searxng-search.mjs'` resolves
// against that same base, so shipping the provider as one file in the seed ConfigMap needs no
// registry, no egress from this pod, and no write to the app volume.
//
// SEARCH ONLY, DELIBERATELY. SearXNG has no page-fetch endpoint -- its whole route table is 22
// rules and /image_proxy is HMAC-gated and refuses any non-image content type -- so no provider
// can implement web_fetch "through" it. fetchProvider stays on dsh's own `http` provider, which
// pins validated DNS answers into the connector and follows same-origin redirects only:
// protection this file could not match and has no reason to replace.
export const name = 'searxng-search';
export const inject = ['web'];

/** The provider id named by the `web` row's `searchProvider`. */
const PROVIDER_ID = 'searxng';
const DEFAULT_BASE_URL = 'http://searxng.dsh.svc.cluster.local:8080';
const DEFAULT_TIMEOUT_MS = 15000;
const DEFAULT_MAX_SNIPPET_CHARS = 400;

/** Trim one string field, returning undefined rather than an empty string. */
function text(value) {
  if (typeof value !== 'string') return undefined;
  const trimmed = value.trim();
  return trimmed === '' ? undefined : trimmed;
}

/**
 * Normalize a SearXNG date to ISO-8601. SearXNG emits `publishedDate` on some engines and
 * `pubdate` on others, and neither is guaranteed parseable; an unparseable value is dropped
 * rather than passed through, because `publishedAt` is documented as an ISO-8601 string.
 */
function publishedAt(result) {
  const raw = text(result.publishedDate) ?? text(result.pubdate);
  if (raw === undefined) return undefined;
  const parsed = Date.parse(raw);
  return Number.isNaN(parsed) ? undefined : new Date(parsed).toISOString();
}

export function apply(ctx, config = {}) {
  const baseUrl = (text(config.baseUrl) ?? DEFAULT_BASE_URL).replace(/\/+$/, '');
  const timeoutMs =
    typeof config.timeoutMs === 'number' && config.timeoutMs > 0 ? config.timeoutMs : DEFAULT_TIMEOUT_MS;
  const maxSnippetChars =
    typeof config.maxSnippetChars === 'number' && config.maxSnippetChars > 0
      ? config.maxSnippetChars
      : DEFAULT_MAX_SNIPPET_CHARS;
  const safesearch = typeof config.safesearch === 'number' ? config.safesearch : 0;
  const language = text(config.language);
  const categories = text(config.categories);
  const engines = text(config.engines);

  ctx.web.registerSearchProvider({
    id: PROVIDER_ID,
    // Documented as a cheap LOCAL check that must not touch the network, so it reports
    // configuration, not reachability. A dead SearXNG surfaces as a tool error carrying the HTTP
    // detail, which is far more diagnosable than the tool quietly disappearing.
    available: () => baseUrl.length > 0,

    async search(request, signal) {
      const url = new URL(baseUrl + '/search');
      url.searchParams.set('q', request.query);
      url.searchParams.set('format', 'json');
      url.searchParams.set('safesearch', String(safesearch));
      if (language !== undefined) url.searchParams.set('language', language);
      if (categories !== undefined) url.searchParams.set('categories', categories);
      if (engines !== undefined) url.searchParams.set('engines', engines);

      // Our own deadline, chained to the seam's cancellation. AbortSignal.any is avoided so this
      // keeps working on the Node 20 floor dsh declares.
      const controller = new AbortController();
      const abort = () => controller.abort(signal?.reason);
      if (signal !== undefined) {
        if (signal.aborted) abort();
        else signal.addEventListener('abort', abort, { once: true });
      }
      const timer = setTimeout(
        () => controller.abort(new Error('searxng: no response within ' + timeoutMs + 'ms')),
        timeoutMs,
      );

      let response;
      try {
        response = await fetch(url, {
          signal: controller.signal,
          redirect: 'error',
          headers: { accept: 'application/json' },
        });
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener('abort', abort);
      }

      if (!response.ok) {
        // 403 is the one failure worth naming: SearXNG ships `formats: [html]` and refuses
        // format=json until settings.yml lists it.
        throw new Error(
          response.status === 403
            ? 'searxng: HTTP 403 for format=json -- add "json" under search.formats in settings.yml'
            : 'searxng: HTTP ' + response.status + ' from ' + url.origin + '/search',
        );
      }
      // An unrecognized `format` value does not error: webapp.py silently coerces it to html and
      // still answers 200. Sniff the content type rather than trusting the status.
      const contentType = response.headers.get('content-type') ?? '';
      if (!/\bjson\b/i.test(contentType)) {
        throw new Error(
          'searxng: expected JSON, got ' + (contentType || 'no content-type') + ' -- format=json was not honoured',
        );
      }
      const payload = await response.json();

      // Deduplicate by URL. SearXNG merges engines, but the same page still arrives twice often
      // enough to matter, and a duplicated citation wastes the model's result budget.
      const seen = new Set();
      const sources = [];
      for (const result of Array.isArray(payload?.results) ? payload.results : []) {
        const href = text(result?.url);
        if (href === undefined || seen.has(href)) continue;
        seen.add(href);
        const title = text(result.title);
        const snippet = text(result.content)?.slice(0, maxSnippetChars);
        const date = publishedAt(result);
        sources.push({
          url: href,
          ...(title !== undefined ? { title } : {}),
          ...(snippet !== undefined ? { snippet } : {}),
          ...(date !== undefined ? { publishedAt: date } : {}),
        });
      }

      // `maxResults` is enforced by the seam, which also owns `truncated`. SearXNG has no
      // result-count parameter (only pageno), so there is nothing to push down into the request.
      const answer = text(payload?.answers?.[0]?.answer) ?? text(payload?.answers?.[0]);
      const infobox = text(payload?.infoboxes?.[0]?.content);
      const parts = [answer, infobox].filter((part) => part !== undefined);

      if (sources.length === 0) {
        // A zero-result answer is usually an ENGINE failure, not an empty web. Name the engines
        // that failed so the outcome is diagnosable instead of reading as "nothing exists".
        const dead = Array.isArray(payload?.unresponsive_engines)
          ? payload.unresponsive_engines.map((entry) => (Array.isArray(entry) ? entry.join(': ') : String(entry)))
          : [];
        if (dead.length > 0) parts.push('No results. Unresponsive SearXNG engines: ' + dead.join('; ') + '.');
      }

      return {
        sources,
        truncated: false,
        ...(parts.length > 0 ? { content: parts.join('\n\n') } : {}),
      };
    },
  });
}
