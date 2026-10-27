#!/bin/sh
# cert-reloader: the edge-proxy sidecar that reloads Caddy when cert-manager renews the wildcard
# certificate. Caddy does not watch certificate files, so without this it keeps serving the old one
# until the pod restarts.
#
# Every RELOAD_INTERVAL seconds (default 300):
#   1. hash the mounted pair (/certs/tls.crt + tls.key; a directory mount, so a renewal swaps both
#      at once through the Secret volume's ..data symlink);
#   2. when the hash changed, validate the whole config, which loads and pairs the cert and key
#      (a malformed or mismatched pair fails here and is never loaded: Caddy keeps the old one);
#   3. reload through the localhost admin API, and remember the new hash ONLY after the reload
#      succeeded, so a failure is retried on the next pass.
# The first pass only records the hash: Caddy loaded these files when it started.
#
# Counters go to /config/reloader/metrics (Prometheus text), which Caddy serves on :9180
# /reloader-metrics for the PodMonitor (alert EdgeCertReloadFailing).
set -u
CONFIG="${CADDY_CONFIG:-/etc/caddy/Caddyfile}"
INTERVAL="${RELOAD_INTERVAL:-300}"
OUT_DIR=/config/reloader
mkdir -p "$OUT_DIR"

last=""
reloads=0
failures=0
invalid=0
last_success=0

write_metrics() {
	tmp="$OUT_DIR/metrics.tmp"
	{
		echo "# HELP edge_cert_reloads_total Caddy reloads after a certificate change."
		echo "# TYPE edge_cert_reloads_total counter"
		echo "edge_cert_reloads_total $reloads"
		echo "# HELP edge_cert_reload_failures_total Reload attempts that failed (retried next pass)."
		echo "# TYPE edge_cert_reload_failures_total counter"
		echo "edge_cert_reload_failures_total $failures"
		echo "# HELP edge_cert_invalid_total Passes that found an unloadable certificate pair."
		echo "# TYPE edge_cert_invalid_total counter"
		echo "edge_cert_invalid_total $invalid"
		echo "# HELP edge_cert_reload_last_success_seconds Unix time of the last successful reload."
		echo "# TYPE edge_cert_reload_last_success_seconds gauge"
		echo "edge_cert_reload_last_success_seconds $last_success"
	} >"$tmp" && mv "$tmp" "$OUT_DIR/metrics"
}

while true; do
	hash=$(cat /certs/tls.crt /certs/tls.key 2>/dev/null | sha256sum | cut -d' ' -f1)
	if [ -z "$last" ]; then
		last="$hash"
		echo "cert-reloader: watching /certs (initial $hash)"
	elif [ "$hash" != "$last" ]; then
		if ! caddy validate --config "$CONFIG" --adapter caddyfile >/dev/null 2>&1; then
			invalid=$((invalid + 1))
			echo "cert-reloader: new pair does not validate; keeping the loaded certificate"
		elif caddy reload --force --config "$CONFIG" --adapter caddyfile --address localhost:2019 >/dev/null 2>&1; then
			last="$hash"
			reloads=$((reloads + 1))
			last_success=$(date +%s)
			echo "cert-reloader: reloaded for $hash"
		else
			failures=$((failures + 1))
			echo "cert-reloader: reload failed; retrying next pass"
		fi
	fi
	write_metrics
	sleep "$INTERVAL"
done
