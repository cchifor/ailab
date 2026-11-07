// providers/lib/claude-login.mjs - will the worker's Claude login last for the run? (no network, no secrets)

/** A full gate takes about 25 minutes: a token must outlive it, or the direct arm dies part way and leaves no baseline. */
export const LOGIN_MARGIN_MS = 40 * 60_000;

/** What is wrong with a login that expires at `expiresAt` (ms), or undefined. Never sees the token itself. */
export function loginProblem(expiresAt, now = Date.now()) {
  if (!Number.isFinite(expiresAt) || expiresAt <= 0) return 'the worker has no readable Claude login: the direct arm cannot run';
  if (expiresAt - now >= LOGIN_MARGIN_MS) return undefined;
  const when = new Date(expiresAt).toISOString();
  return `the worker's Claude login ${expiresAt <= now ? 'expired' : 'expires'} at ${when}: run \`claude -p ok\` on the worker once, which refreshes it, then rerun`;
}
