#!/usr/bin/env bash
# Drives the real codex-router-key template (rendered with sed: its only Jinja is plain variables)
# against a fake `cred`: the key comes from the vault, the last good value is cached 0600 and used
# when the vault cannot be read, nothing but the key ever reaches stdout, and with neither the
# helper fails with a message on stderr. CI: .gitea/workflows/dev-worker-scripts.yaml.
set -euo pipefail
ROLE="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
fails=0
bad() { echo "FAIL: $*" >&2; fails=$((fails + 1)); }

sed -e 's/{{ dev_worker_codex_router_key_field }}/llm_router_codex_key/g' \
    -e 's/{{ dev_worker_codex_router_provider }}/llm-router/g' \
    -e 's#{{ dev_worker_codex_router_base_url }}#https://router.example/v1#g' \
    "$ROLE/templates/codex-router-key.j2" > "$work/codex-router-key"
chmod +x "$work/codex-router-key"
if grep -q '{{' "$work/codex-router-key"; then bad "template has Jinja this test does not render"; fi

# The fake cred: prints $FAKE_VALUE for exactly `get <this host> llm_router_codex_key`, else fails.
cat > "$work/cred" <<'SH'
#!/bin/sh
[ "$1" = get ] && [ "$2" = "$(hostname -s)" ] && [ "$3" = llm_router_codex_key ] && [ -n "${FAKE_VALUE:-}" ] || exit 1
printf '%s' "$FAKE_VALUE"
SH
chmod +x "$work/cred"
export HOME="$work/home" CODEX_ROUTER_CRED="$work/cred"
mkdir -p "$HOME"
cache="$HOME/.config/llm-router/codex.key"
run() { "$work/codex-router-key" 2>"$work/err"; }

out="$(FAKE_VALUE=lrk_first run)" || bad "vault read failed"
[ "$out" = lrk_first ] || bad "stdout is not exactly the key: '$out'"
[ "$(cat "$cache")" = lrk_first ] || bad "the key was not cached"
[ "$(stat -c %a "$cache")" = 600 ] || bad "cache mode is $(stat -c %a "$cache"), not 600"
[ "$(stat -c %a "${cache%/*}")" = 700 ] || bad "cache dir mode is $(stat -c %a "${cache%/*}"), not 700"

out="$(FAKE_VALUE=lrk_rotated run)" || bad "vault read failed after rotation"
[ "$out" = lrk_rotated ] && [ "$(cat "$cache")" = lrk_rotated ] || bad "a rotated key did not replace the cache"

out="$(FAKE_VALUE= run)" || bad "vault down with a cache: exit $?"
[ "$out" = lrk_rotated ] || bad "vault down: the cached key was not used ('$out')"

rm -f "$cache"
if out="$(FAKE_VALUE= run)"; then bad "vault down without a cache must fail"; fi
[ -z "$out" ] || bad "a failure printed to stdout: '$out'"
grep -q "no router key" "$work/err" || bad "a failure must say why on stderr"
ls "${cache%/*}" | grep -q tmp && bad "a temp file was left behind"

if [ "$fails" -gt 0 ]; then echo "$fails failure(s)" >&2; exit 1; fi
echo "codex-router-key: all checks passed"
