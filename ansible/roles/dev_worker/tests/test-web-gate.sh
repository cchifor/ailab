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
#     Basic; nothing may reach ttyd outside the route.
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
for f in "$CADDY" "$TASKS" "$UNIT" "$ROLE/files/dw_access_verify.py" "$HERE/test_access_verify.py"; do
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
proxy=$(line '^    reverse_proxy 127\.0\.0\.1:7681')

[ "$(grep -c -E '^  route \{' "$CADDY")" = 1 ] && ok "exactly one route block" || bad "expected exactly one site-level route block"
for v in route origin foreign fwd basic cookie proxy; do
	[ -n "${!v}" ] || bad "missing directive: $v"
done
if [ -n "$route" ] && [ -n "$origin" ] && [ -n "$foreign" ] && [ -n "$fwd" ] && [ -n "$basic" ] && [ -n "$cookie" ] && [ -n "$proxy" ]; then
	if [ "$route" -lt "$origin" ] && [ "$origin" -lt "$fwd" ] && [ "$foreign" -lt "$fwd" ] && [ "$fwd" -lt "$basic" ] \
		&& [ "$basic" -lt "$cookie" ] && [ "$cookie" -lt "$proxy" ]; then
		ok "order: route > origin guards > forward_auth > basic_auth > Set-Cookie > reverse_proxy"
	else
		bad "directive order broken (route=$route origin=$origin foreign=$foreign fwd=$fwd basic=$basic cookie=$cookie proxy=$proxy)"
	fi
fi
[ "$(grep -c 'reverse_proxy' "$CADDY")" = 1 ] && ok "ttyd is proxied in exactly one place" || bad "more than one reverse_proxy: something reaches ttyd outside the gate"
grep -q -E '^    @lan_unauthenticated \{' "$CADDY" && awk '/^    @lan_unauthenticated \{/,/^    \}/' "$CADDY" | grep -q 'not header Cf-Access-Jwt-Assertion \*' \
	&& ok "a request carrying a JWT header never falls back to Basic" || bad "@lan_unauthenticated must exclude requests carrying Cf-Access-Jwt-Assertion"
grep -E '^    header @lan_unauthenticated Set-Cookie' "$CADDY" | grep -q 'Secure; HttpOnly; SameSite=Strict' \
	&& ok "bridge cookie is Secure/HttpOnly/SameSite=Strict" || bad "bridge cookie lost Secure/HttpOnly/SameSite=Strict"
grep -q 'header Sec-WebSocket-Key \*' "$CADDY" && ok "WebSocket upgrades need their own Origin" || bad "WebSocket Origin guard missing"
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

echo
if [ "$FAILS" -eq 0 ]; then echo "PASS"; exit 0; fi
echo "FAILED: $FAILS check(s)"; exit 1
