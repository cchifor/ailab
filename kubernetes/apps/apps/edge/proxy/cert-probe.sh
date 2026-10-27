#!/bin/sh
# cert-probe: the edge-proxy sidecar that reads the certificate THIS pod's Caddy actually serves.
# The other certificate alerts see the issued Secret (cert-manager) and the reloader's view of the
# mounted files; neither can see a node whose Secret projection went stale, where Caddy and the
# reloader agree on old files. This probe asks Caddy itself, over TLS on localhost, every
# PROBE_INTERVAL seconds (default 60), with a registry hostname as SNI.
#
# Metrics, /config/reloader/probe (Prometheus text; Caddy serves it on :9180 /probe-metrics):
#   edge_cert_served_not_after_seconds  expiry of the served certificate (absent while a probe fails)
#   edge_cert_probe_success             1 when the last probe read a certificate, else 0
#   edge_cert_probe_heartbeat_seconds   Unix time of the last probe
# Alerts EdgeCertServedExpiringSoon and EdgeCertProbeFailing (monitoring/edge-proxy-rules.yaml).
set -u
INTERVAL="${PROBE_INTERVAL:-60}"
SNI="${PROBE_SNI:-edge-canary.chifor.me}"
OUT_DIR=/config/reloader
mkdir -p "$OUT_DIR"

while true; do
	end=$(echo | openssl s_client -connect 127.0.0.1:8443 -servername "$SNI" 2>/dev/null \
		| openssl x509 -noout -enddate -dateopt iso_8601 2>/dev/null | sed -n 's/^notAfter=//p')
	ts=""
	[ -n "$end" ] && ts=$(date -u -d "$(echo "$end" | sed 's/Z$//')" +%s 2>/dev/null)
	tmp="$OUT_DIR/probe.tmp"
	{
		echo "# HELP edge_cert_probe_success 1 when the last probe read the certificate Caddy serves."
		echo "# TYPE edge_cert_probe_success gauge"
		if [ -n "$ts" ]; then
			echo "edge_cert_probe_success 1"
			echo "# HELP edge_cert_served_not_after_seconds Expiry (Unix time) of the certificate Caddy serves."
			echo "# TYPE edge_cert_served_not_after_seconds gauge"
			echo "edge_cert_served_not_after_seconds $ts"
		else
			echo "edge_cert_probe_success 0"
		fi
		echo "# HELP edge_cert_probe_heartbeat_seconds Unix time of the last probe."
		echo "# TYPE edge_cert_probe_heartbeat_seconds gauge"
		echo "edge_cert_probe_heartbeat_seconds $(date +%s)"
	} >"$tmp" && mv "$tmp" "$OUT_DIR/probe"
	sleep "$INTERVAL"
done
