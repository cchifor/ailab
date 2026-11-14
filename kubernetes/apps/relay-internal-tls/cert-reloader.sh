#!/bin/sh
# cert-reloader: the sidecar of relay-hub-tls and relay-router-tls. Caddy does not watch certificate
# files, so without this it keeps serving the old certificate after cert-manager renews it, until the
# Pod restarts. A simplified kubernetes/apps/apps/edge/proxy/cert-reloader.sh (no metrics; one JSON
# line per event on stdout).
#
# Each pass (RELOAD_INTERVAL seconds, 60 by default; every 5 s until the first success):
#   1. hash the mounted pair (/certs is a directory mount, so a renewal swaps tls.crt and tls.key
#      together through the Secret volume's ..data symlink);
#   2. if that hash is not the one Caddy is known to have loaded (including on the first pass: a
#      renewal may have landed before this container started), validate the whole config, which
#      loads and pairs the certificate and key; a broken pair is never loaded and Caddy keeps the old;
#   3. reload through the localhost admin API with --force (the Caddyfile text is unchanged, only the
#      files are new), and remember the hash only after the reload succeeded.
set -u
CONFIG=/etc/caddy/Caddyfile
INTERVAL="${RELOAD_INTERVAL:-60}"
loaded=""
while true; do
	hash=$(cat /certs/tls.crt /certs/tls.key 2>/dev/null | sha256sum | cut -d' ' -f1)
	if [ "$hash" != "$loaded" ]; then
		if ! caddy validate --config "$CONFIG" --adapter caddyfile >/dev/null 2>&1; then
			echo '{"cert_reloader":"invalid","detail":"the mounted pair does not validate; Caddy keeps its certificate"}'
		elif caddy reload --force --config "$CONFIG" --adapter caddyfile --address localhost:2019 >/dev/null 2>&1; then
			loaded="$hash"
			echo "{\"cert_reloader\":\"loaded\",\"sha256\":\"$hash\"}"
		else
			echo '{"cert_reloader":"reload-failed","detail":"admin API not up yet or Caddy refused; retrying"}'
		fi
	fi
	if [ -n "$loaded" ]; then sleep "$INTERVAL"; else sleep 5; fi
done
