#!/bin/sh
# cert-reloader: the edge-proxy sidecar that reloads Caddy when cert-manager renews the wildcard
# certificate. Caddy does not watch certificate files, so without this it keeps serving the old one
# until the pod restarts.
#
# Each pass (every RELOAD_INTERVAL seconds, 300 by default; every 10 s until the first success):
#   1. hash the mounted pair (/certs/tls.crt + tls.key; a directory mount, so a renewal swaps both
#      at once through the Secret volume's ..data symlink);
#   2. if that hash is not the one Caddy is known to have loaded - including on the FIRST pass, so a
#      renewal that landed before this sidecar (re)started is never taken as already loaded -
#      validate the whole config, which loads and pairs the cert and key (a malformed or mismatched
#      pair fails here and is never loaded: Caddy keeps serving the old one);
#   3. reload through the localhost admin API, and record the hash as loaded ONLY after the reload
#      succeeded, so a failure is retried on the next pass.
#
# Metrics, /config/reloader/metrics (Prometheus text; Caddy serves it on :9180 /reloader-metrics):
#   edge_cert_reloads_total, edge_cert_reload_failures_total, edge_cert_invalid_total,
#   edge_cert_reload_last_success_seconds, edge_cert_reloader_heartbeat_seconds (every pass),
#   edge_cert_files_not_loaded (1 while the mounted pair differs from what Caddy loaded).
# With cert-manager's own expiry metric these say what Caddy SERVES: the issued certificate is fresh
# (EdgeCertExpiringSoon), the files are what Caddy loaded (EdgeCertNotLoaded), and this loop is alive
# (EdgeCertReloaderStale). Rules: infrastructure/monitoring/edge-proxy-rules.yaml.
set -u
CONFIG="${CADDY_CONFIG:-/etc/caddy/Caddyfile}"
INTERVAL="${RELOAD_INTERVAL:-300}"
OUT_DIR=/config/reloader
mkdir -p "$OUT_DIR"

loaded=""
reloads=0
failures=0
invalid=0
last_success=0

write_metrics() {
	not_loaded=0
	[ "$hash" = "$loaded" ] || not_loaded=1
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
		echo "# HELP edge_cert_reloader_heartbeat_seconds Unix time of this loop's last pass."
		echo "# TYPE edge_cert_reloader_heartbeat_seconds gauge"
		echo "edge_cert_reloader_heartbeat_seconds $(date +%s)"
		echo "# HELP edge_cert_files_not_loaded 1 while the mounted certificate pair is not the one Caddy loaded."
		echo "# TYPE edge_cert_files_not_loaded gauge"
		echo "edge_cert_files_not_loaded $not_loaded"
	} >"$tmp" && mv "$tmp" "$OUT_DIR/metrics"
}

echo "cert-reloader: watching /certs"
while true; do
	hash=$(cat /certs/tls.crt /certs/tls.key 2>/dev/null | sha256sum | cut -d' ' -f1)
	if [ "$hash" != "$loaded" ]; then
		if ! caddy validate --config "$CONFIG" --adapter caddyfile >/dev/null 2>&1; then
			invalid=$((invalid + 1))
			echo "cert-reloader: the mounted pair does not validate; Caddy keeps the certificate it has"
		elif caddy reload --force --config "$CONFIG" --adapter caddyfile --address localhost:2019 >/dev/null 2>&1; then
			loaded="$hash"
			reloads=$((reloads + 1))
			last_success=$(date +%s)
			echo "cert-reloader: Caddy loaded $hash"
		else
			failures=$((failures + 1))
			echo "cert-reloader: reload failed (admin API not up yet, or Caddy refused); retrying"
		fi
	fi
	write_metrics
	if [ -n "$loaded" ]; then sleep "$INTERVAL"; else sleep 10; fi
done
