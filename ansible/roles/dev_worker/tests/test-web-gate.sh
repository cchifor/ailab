#!/usr/bin/env bash
# Tests for the web terminal's authentication gate (tasks/web_gate.yml). Plain bash, following
# test-tmux-persistence.sh.
#
# [A] dw-access-verify's unit tests (tests/test_access_verify.py): forged / expired / other-app /
#     alg=none / HMAC-confusion tokens, key rotation, JWKS outage with a warm and a cold cache, and
#     the forward_auth status contract. Needs python3-jwt + python3-cryptography: absent, [A] is
#     NOT RUN — but CI sets REQUIRE_PYJWT=1, which makes that a hard failure.
# [B] The Caddyfile's security-critical shape. Every one of these was a way to reopen the shell:
#     the gate must live in ONE `route` (outside a route Caddy sorts `respond` AFTER `handle`/proxy,
#     so a 403 written first never fires — measured on Caddy 2.11.4); the Origin guard must precede
#     both auth paths, and both must precede the proxy; the bridge cookie must be set only after
#     basic_auth and only as Secure/HttpOnly/SameSite=Strict; a forged JWT must never fall back to
#     Basic; nothing — ttyd, the upload endpoint, the served page — may be reachable before the auth
#     directives, and uploads need the worker's own Origin exactly like WebSocket upgrades.
# [C] dw-upload's tests (tests/test_upload.py, stdlib only, always run): strict framing, Origin and
#     header checks, quota/file-count reservations under concurrency, no-replace publish, truncated
#     bodies leaving nothing behind, safe names.
#
# The live behaviour is asserted on every converge instead (web_gate.yml's self-check tasks) and
# was prototyped against a real Caddy: plans/2026-09-28-dw-web-terminal-auth-and-file-paste-plan.md.
#
# Usage: bash ansible/roles/dev_worker/tests/test-web-gate.sh   (exit 0 = pass)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROLE="$HERE/.."
CADDY="$ROLE/templates/Caddyfile.j2"
TASKS="$ROLE/tasks/web_gate.yml"
UNIT="$ROLE/templates/dw-access-verify.service.j2"
UPUNIT="$ROLE/templates/dw-upload.service.j2"
for f in "$CADDY" "$TASKS" "$UNIT" "$UPUNIT" "$ROLE/files/dw_access_verify.py" "$HERE/test_access_verify.py" "$ROLE/files/dw_upload.py" "$HERE/test_upload.py" "$ROLE/files/dw_paste.js"; do
	[ -r "$f" ] || { echo "FATAL: cannot read $f"; exit 2; }
done

FAILS=0
ok() { echo "  ok: $1"; }
bad() { echo "  FAIL: $1"; FAILS=$((FAILS + 1)); }

echo "[A] dw-access-verify unit tests"
if python3 -c 'import jwt, cryptography' 2>/dev/null; then
	out=$(cd "$HERE" && python3 -m unittest -q test_access_verify 2>&1)
	rc=$?
	echo "$out" | tail -3 | sed 's/^/    /'
	if [ "$rc" -eq 0 ]; then ok "unit tests pass"; else bad "unit tests fail"; fi
elif [ "${REQUIRE_PYJWT:-0}" = 1 ]; then
	bad "python3-jwt/python3-cryptography not importable and REQUIRE_PYJWT=1 — the validator's tests did not run"
else
	echo "  NOT RUN: python3-jwt/python3-cryptography not importable. CI sets REQUIRE_PYJWT=1 to require them."
fi

echo "[B] Caddyfile gate shape"
line() { grep -n -m1 -E "$1" "$CADDY" | cut -d: -f1; }
route=$(line '^  route \{')
origin=$(line 'respond @must_have_own_origin 403')
foreign=$(line 'respond @foreign_origin 403')
fwd=$(line '^    forward_auth @access_jwt ')
basic=$(line '^    basic_auth @lan_unauthenticated ')
cookie=$(line '^    header @lan_unauthenticated Set-Cookie ')
proxy=$(line '^      reverse_proxy 127\.0\.0\.1:7681')
upload=$(line '^    handle /_dw/upload \{')
page=$(line '^    handle @terminal_page \{')
static=$(line '^    handle_path /_dw/static/\* \{')

[ "$(grep -c -E '^  route \{' "$CADDY")" = 1 ] && ok "exactly one route block" || bad "expected exactly one site-level route block"
for v in route origin foreign fwd basic cookie proxy upload page static; do
	[ -n "${!v}" ] || bad "missing directive: $v"
done
if [ -n "$route" ] && [ -n "$origin" ] && [ -n "$foreign" ] && [ -n "$fwd" ] && [ -n "$basic" ] && [ -n "$cookie" ] && [ -n "$proxy" ]; then
	if [ "$route" -lt "$origin" ] && [ "$origin" -lt "$fwd" ] && [ "$foreign" -lt "$fwd" ] && [ "$fwd" -lt "$basic" ] \
		&& [ "$basic" -lt "$cookie" ] && [ "$cookie" -lt "$proxy" ]; then
		ok "order: route > origin guards > forward_auth > basic_auth > Set-Cookie > reverse_proxy"
		if [ -n "$upload" ] && [ -n "$page" ] && [ -n "$static" ] && [ "$cookie" -lt "$upload" ] && [ "$cookie" -lt "$page" ] && [ "$cookie" -lt "$static" ]; then
			ok "upload endpoint, static files and the served page all sit behind the auth directives"
		else
			bad "upload/static/page handlers must come after the auth directives (cookie=$cookie upload=$upload page=$page static=$static)"
		fi
	else
		bad "directive order broken (route=$route origin=$origin foreign=$foreign fwd=$fwd basic=$basic cookie=$cookie proxy=$proxy)"
	fi
fi
[ "$(grep -c 'reverse_proxy 127.0.0.1:7681' "$CADDY")" = 1 ] && ok "ttyd is proxied in exactly one place" || bad "ttyd must be proxied exactly once (inside the gate)"
[ "$(grep -c 'reverse_proxy' "$CADDY")" = 2 ] && ok "exactly two proxies (ttyd + dw-upload)" || bad "unexpected reverse_proxy count: something new reaches a backend"
[ "$(grep -c 'file_server' "$CADDY")" = 2 ] && ok "exactly two file_server blocks (page + static)" || bad "unexpected file_server count"
grep -q -E '^    @lan_unauthenticated \{' "$CADDY" && awk '/^    @lan_unauthenticated \{/,/^    \}/' "$CADDY" | grep -q 'not header Cf-Access-Jwt-Assertion \*' \
	&& ok "a request carrying a JWT header never falls back to Basic" || bad "@lan_unauthenticated must exclude requests carrying Cf-Access-Jwt-Assertion"
grep -E '^    header @lan_unauthenticated Set-Cookie' "$CADDY" | grep -q 'Secure; HttpOnly; SameSite=Strict' \
	&& ok "bridge cookie is Secure/HttpOnly/SameSite=Strict" || bad "bridge cookie lost Secure/HttpOnly/SameSite=Strict"
grep -E '^      expression .*Sec-WebSocket-Key.*path\('"'"'/_dw/upload'"'"'\)' "$CADDY" >/dev/null && ok "WebSocket upgrades and uploads need the worker's own Origin" || bad "Origin guard must cover WebSocket upgrades AND /_dw/upload"
grep -q 'max_size 64MiB' "$CADDY" && grep -q 'MAX_BYTES = 64 \* 1024 \* 1024' "$ROLE/files/dw_upload.py" && grep -q 'MAX_BYTES = 64 \* 1024 \* 1024' "$ROLE/files/dw_paste.js" && ok "64 MiB limit agrees across Caddy, dw-upload and dw_paste.js" || bad "the 64 MiB upload limit disagrees between Caddy, dw-upload and dw_paste.js"
grep -q -E '^\s*admin off' "$CADDY" && bad "admin off breaks ordinary reloads" || ok "admin endpoint left on (reloads work)"

echo "[B] web_gate.yml wiring"
grep -q 'mode: "0640"' "$TASKS" && grep -q 'group: caddy' "$TASKS" && ok "Caddyfile is 0640 root:caddy" || bad "Caddyfile must be 0640 root:caddy (it holds the hash + cookie secret)"
grep -q 'caddy validate --adapter caddyfile' "$TASKS" && ok "Caddyfile is validated before it lands" || bad "no caddy validate on the template"
health=$(grep -n -m1 'Wait for dw-access-verify' "$TASKS" | cut -d: -f1)
caddyfile=$(grep -n -m1 'Lay down the Caddyfile' "$TASKS" | cut -d: -f1)
[ -n "$health" ] && [ -n "$caddyfile" ] && [ "$health" -lt "$caddyfile" ] \
	&& ok "validator is healthy before the Caddyfile goes live" || bad "the Caddyfile must land after the validator health wait"
for check in 'status_code: 401' 'Cf-Access-Jwt-Assertion: forged' 'Origin: https://attacker.example'; do
	grep -q "$check" "$TASKS" && ok "self-check present: $check" || bad "self-check missing: $check"
done
grep -q '^DynamicUser=yes' "$UNIT" && grep -q '^ProtectSystem=strict' "$UNIT" && ok "validator unit is sandboxed" || bad "validator unit lost DynamicUser/ProtectSystem"
grep -q '^ProtectSystem=strict' "$UPUNIT" && grep -q '^ReadWritePaths={{ dev_worker_web_pastes_dir }}$' "$UPUNIT" && grep -q '^ProtectHome=yes' "$UPUNIT" && ok "dw-upload can write only the pastes directory" || bad "dw-upload unit lost ProtectSystem=strict / ReadWritePaths / ProtectHome"
splice=$(grep -n -m1 "Serve ttyd's page with the paste script" "$TASKS" | cut -d: -f1)
upstart=$(grep -n -m1 'Enable and (re)start dw-upload' "$TASKS" | cut -d: -f1)
[ -n "$splice" ] && [ -n "$upstart" ] && [ -n "$caddyfile" ] && [ "$splice" -lt "$caddyfile" ] && [ "$upstart" -lt "$caddyfile" ] && ok "page and dw-upload are in place before the Caddyfile routes to them" || bad "page splice / dw-upload start must precede the Caddyfile"
for check in 'paste.js" defer></script>' 'Origin: https://attacker.example' 'status_code: 201'; do
	grep -q "$check" "$TASKS" && ok "paste self-check present: $check" || bad "paste self-check missing: $check"
done

echo "[C] dw-upload tests"
out=$(cd "$HERE" && python3 -m unittest -q test_upload 2>&1)
rc=$?
echo "$out" | tail -3 | sed 's/^/    /'
if [ "$rc" -eq 0 ]; then ok "dw-upload tests pass"; else bad "dw-upload tests fail"; fi

echo
if [ "$FAILS" -eq 0 ]; then echo "PASS"; exit 0; fi
echo "FAILED: $FAILS check(s)"; exit 1
