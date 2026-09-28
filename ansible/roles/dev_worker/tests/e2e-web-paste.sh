#!/usr/bin/env bash
# Browser end-to-end test of the web terminal: LAN login, the WebSocket through the gate, and file
# paste (clipboard, drag-and-drop, paperclip) landing in the focused pane as bracketed pastes.
#
# It runs a THROWAWAY copy of the real stack: a private tmux server whose pane records its raw input
# with bracketed paste on, ttyd, dw-upload and Caddy on high ports, all state under one temp dir.
# The Caddyfile is the role's own template rendered with jinja2, the page is spliced exactly as
# web_gate.yml does, and Chromium drives it through Playwright. Nothing touches the live ttyd, Caddy
# or tmux sessions. Not in CI (the runners carry no caddy/ttyd/Chromium); run it on a dev-worker
# after changing the gate, dw_paste.js or dw_upload.py, or after a ttyd bump:
#
#   PLAYWRIGHT_NODE_PATH=/workspace/c4/platform/tests/e2e/node_modules \
#     bash ansible/roles/dev_worker/tests/e2e-web-paste.sh
#
# Needs: caddy, ttyd, tmux, curl, python3 + jinja2, node, and a node_modules dir holding playwright
# with its Chromium installed. Exit 0 = every browser check passed.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROLE="$HERE/.."
: "${PLAYWRIGHT_NODE_PATH:?set PLAYWRIGHT_NODE_PATH to a node_modules dir that holds playwright}"
for tool in caddy ttyd tmux curl python3 node; do
	command -v "$tool" >/dev/null || { echo "FATAL: $tool not found"; exit 2; }
done

HTTPS_PORT=18443 HTTP_PORT=18080 TTYD_PORT=17681 UPLOAD_PORT=17683 VERIFY_PORT=17682
W="$(mktemp -d /tmp/dw-e2e-XXXXXX)"
SOCK="dwe2e$$"
cleanup() {
	kill $(jobs -p) 2>/dev/null || true
	tmux -L "$SOCK" kill-server 2>/dev/null || true
	rm -rf "$W"
}
trap cleanup EXIT
mkdir -p "$W/pastes" "$W/web/static" "$W/data"
chmod 0700 "$W/pastes"

PASS="e2e-$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
COOKIE="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
HASH="$(caddy hash-password --plaintext "$PASS")"

# The pane: bracketed paste on, raw mode, every byte appended to $W/captured.
cat >"$W/capture.py" <<'PY'
import os, sys, tty
sys.stdout.write("\x1b[?2004h")
sys.stdout.flush()
tty.setraw(0)
with open(sys.argv[1], "ab", buffering=0) as f:
    while True:
        b = os.read(0, 4096)
        if not b:
            break
        f.write(b)
PY
: >"$W/captured"
tmux -L "$SOCK" -f /dev/null new-session -d -s main -x 160 -y 40 "python3 $W/capture.py $W/captured"
ttyd -W -i 127.0.0.1 -p "$TTYD_PORT" tmux -L "$SOCK" attach -t main >"$W/ttyd.log" 2>&1 &
DW_UPLOAD_DIR="$W/pastes" DW_UPLOAD_ORIGINS="https://127.0.0.1:$HTTPS_PORT" DW_UPLOAD_LISTEN="127.0.0.1:$UPLOAD_PORT" \
	python3 "$ROLE/files/dw_upload.py" >"$W/upload.log" 2>&1 &

for _ in $(seq 1 40); do curl -fsS -o /dev/null "http://127.0.0.1:$TTYD_PORT/" && break; sleep 0.25; done

# The page, spliced exactly like web_gate.yml does it.
curl -fsS "http://127.0.0.1:$TTYD_PORT/" | python3 -c '
import sys
page = sys.stdin.read()
assert "window.term=" in page and "</body>" in page, "ttyd page lacks window.term or </body>"
head, tail = page.rsplit("</body>", 1)
sys.stdout.write(head + "<script src=\"/_dw/static/paste.js\" defer></script></body>" + tail)
' >"$W/web/index.html"
cp "$ROLE/files/dw_paste.js" "$W/web/static/paste.js"

# The role's own Caddyfile template, rendered with test values (plus throwaway ports, storage and
# `admin off`, so it cannot collide with the live Caddy's admin endpoint).The site "address" carries the port,
# so the rendered site block, Origin list and cookie all line up with https://127.0.0.1:18443.
python3 - "$ROLE/templates/Caddyfile.j2" "$W" "$HASH" "$COOKIE" <<PY >"$W/Caddyfile"
import sys, jinja2
tmpl, w, bcrypt_hash, cookie = sys.argv[1:5]
out = jinja2.Template(open(tmpl).read()).render(
    ansible_managed="e2e",
    ansible_default_ipv4={"address": "127.0.0.1:$HTTPS_PORT"},
    ansible_hostname="dw-e2e:$HTTPS_PORT",
    dev_worker_web_public_host="dw-e2e.invalid:$HTTPS_PORT",
    dev_worker_web_origins=["https://127.0.0.1:$HTTPS_PORT"],
    dev_worker_web_verify_port=$VERIFY_PORT,
    dev_worker_web_upload_port=$UPLOAD_PORT,
    dev_worker_web_root=w + "/web",
    dev_worker_web_lan_user="c4",
    dev_worker_web_lan_password_bcrypt=bcrypt_hash,
    dev_worker_web_lan_cookie_secret=cookie,
)
out = out.replace("reverse_proxy 127.0.0.1:7681", "reverse_proxy 127.0.0.1:$TTYD_PORT")
out = out.replace("  local_certs\n", "  local_certs\n  admin off\n  http_port $HTTP_PORT\n  https_port $HTTPS_PORT\n  skip_install_trust\n  storage file_system " + w + "/data\n", 1)
out = out.replace(":80 {", ":$HTTP_PORT {")
sys.stdout.write(out)
PY
XDG_DATA_HOME="$W/data" XDG_CONFIG_HOME="$W/data" caddy run --adapter caddyfile --config "$W/Caddyfile" >"$W/caddy.log" 2>&1 &
for _ in $(seq 1 60); do ss -ltn | grep -q ":$HTTPS_PORT " && break; sleep 0.25; done
ss -ltn | grep -q ":$HTTPS_PORT " || { echo "FATAL: caddy did not start"; tail -5 "$W/caddy.log"; exit 1; }

E2E_URL="https://127.0.0.1:$HTTPS_PORT/" E2E_PASS="$PASS" E2E_PASTES="$W/pastes" E2E_CAPTURED="$W/captured" \
	NODE_PATH="$PLAYWRIGHT_NODE_PATH" node "$HERE/e2e-web-paste.cjs"
