# Public-edge 5xx evidence record - 2026-10-06 ~18:06-18:10Z

**What:** redacted, aggregated preservation of the HTTP 5xx responses returned by the public edge (Traefik, namespace `platform-edge`, fronted by Cloudflare tunnels `cloudflared*` in namespace `edge`) on 2026-10-06 around 18:06-18:10Z, which had not been attributed. Loki retention is 168 h, so the source log lines expire around 2026-10-13T18:10Z.

**Collected:** 2026-10-07T07:59Z (UTC), read-only, via `kubectl --context admin@ai` (Loki and Prometheus reached through the API-server service proxy; nothing in the cluster was changed). Prometheus TSDB earliest sample: 2026-09-22T00:00Z, so the incident window is inside Prometheus retention.

**Window:** 2026-10-06T17:50:00Z to 2026-10-06T18:30:00Z (items 1, 4). Whole-day context: 2026-10-06T00:00Z-24:00Z (item 2). Logs at warning/error level: 18:00-18:15Z (item 3).

**Redaction:** only the requested fields are kept per 5xx line. Client addresses/ports/usernames, headers, cookies, tokens, query strings and Authorization data were never written to this file. `RequestPath` has the query string removed but otherwise is verbatim (it contains resource identifiers such as app slugs and connection UUIDs, as requested). Log-message templates in sections 3 have ids, uuids, IPs, emails, tokens and timestamps replaced by placeholders; `<n>` = number, `<id>`/`<hex>`/`<uuid>`/`<ip>` as named. `ServiceAddr` and Pod IPs are cluster-internal pod addresses, not client IPs. Raw Loki output was processed in memory only and not saved.

**Time units:** `OriginDuration` and `Duration` are nanoseconds in the Traefik log; the tables below show milliseconds. Per-minute buckets are labelled by the minute they START (UTC); a bucket covers (t, t+60s] as returned by `count_over_time(...[1m])` at step 60.

## Exact queries used

Loki base: `/api/v1/namespaces/monitoring/services/loki:3100/proxy/loki/api/v1/` (`query_range`, URL-encoded). `TR` = `{namespace="platform-edge",container="traefik"}`; `ACC` = `TR | json | DownstreamStatus=~"[0-9]+"` (access-log lines only).

| Id | Endpoint | LogQL / PromQL | Range, step |
|---|---|---|---|
| L1 | loki query_range | TR \| json \| DownstreamStatus >= 500  (limit=5000, direction=forward) | 17:50:00Z-18:30:00Z |
| L2 | loki query_range | sum(count_over_time(ACC [1m])) | 17:51:00Z-18:30:00Z, step 60 |
| L3 | loki query_range | sum(count_over_time(ACC \| DownstreamStatus >= 200 and DownstreamStatus < 300 [1m]))  (same for 300-399, 400-499; and `ACC \| DownstreamStatus >= 500`) | 17:51:00Z-18:30:00Z, step 60 |
| L4 | loki query_range | sum(count_over_time(TR [1m]))  (all log lines, to prove every line is an access-log line) | 17:51:00Z-18:30:00Z, step 60 |
| L5 | loki query_range | sum by (ServiceName, RouterName, DownstreamStatus, OriginStatus) (count_over_time(TR \| json \| DownstreamStatus >= 500 [1m])) | 17:51:00Z-18:30:00Z, step 60 |
| L6 | loki query_range (instant at end) | sum by (DownstreamStatus) (count_over_time(ACC [40m]));  sum by (OriginStatus) (count_over_time(TR \| json \| DownstreamStatus >= 500 [40m]));  sum(count_over_time(ACC \| OriginStatus >= 500 \| DownstreamStatus < 500 [40m])) | evaluated at 18:30:00Z |
| L7 | loki query_range | same expression as L5, run in 3-hour chunks | 2026-10-06T00:01Z-24:00Z, step 60 |
| L8 | loki query_range | sum(count_over_time(ACC [1h]));  ... \| DownstreamStatus >= 400 and DownstreamStatus < 500 [1h]);  ... \| DownstreamStatus >= 500 [1h]) | 2026-10-06T01:00Z-2026-10-07T00:00Z, step 3600 |
| L9 | loki query_range | TR != "DownstreamStatus"  (Traefik non-access log lines) | 17:50:00Z-18:30:00Z |
| L10 | loki query_range + series | {namespace="strive-ailab",pod="<each pod>",container="<each container>"}  (pods discovered with sum by (pod,container) (count_over_time({namespace="strive-ailab"}[900s])); level classified client-side: JSON level/severity keys, Python-logging LEVEL: prefix, logfmt level=, Keycloak WARN/ERROR tokens) | 18:00:00Z-18:15:00Z |
| L11 | loki query_range | {namespace="edge",pod="<each pod>",container="cloudflared"} | 17:50:00Z-18:30:00Z |
| P1 | prometheus query_range | kube_pod_container_status_restarts_total{namespace=~"platform-edge\|edge\|strive-ailab"} | 17:50:00Z-18:30:00Z, step 30 |
| P2 | prometheus query_range | kube_pod_status_ready{condition="true",namespace=~"platform-edge\|edge\|strive-ailab"};  kube_pod_container_status_ready{namespace=~"..."};  kube_pod_status_phase{phase=~"Pending\|Failed\|Unknown",namespace=~"..."} > 0 | 17:50:00Z-18:30:00Z, step 30 |
| P3 | prometheus query | min_over_time(kube_pod_status_ready{condition="true",namespace=~"platform-edge\|edge\|strive-ailab"}[40m]);  max_over_time(kube_pod_container_status_restarts_total{namespace=~"..."}[40m]) | evaluated at 18:30:00Z |
| P4 | prometheus query_range | count by (namespace, endpointslice, ready, serving) (kube_endpointslice_endpoints{namespace=~"platform-edge\|strive-ailab"});  count by (namespace, endpointslice) (kube_endpointslice_info{namespace=~"platform-edge\|strive-ailab"}) | 17:50:00Z-18:30:00Z, step 30 |
| P5 | prometheus label values + query | /label/__name__/values (whole TSDB) filtered for "cloudflared", "tunnel", "traefik", "kube_endpoint"; up{namespace=~"platform-edge\|edge\|strive-ailab"} | whole TSDB; up over window |
| P6 | prometheus query_range / query | raw http_server_request_duration_seconds_count{http_status_code=~"5.."} (used);  sum by (job, http_method, http_route, http_status_code) (increase(http_server_request_duration_seconds_count{http_status_code=~"5.."}[1m])) (run, not used: unreliable at 30 s scrape);  sum by (http_status_code) (increase(http_server_request_duration_seconds_count{job="gatekeeper"}[40m])) | raw: 17:50:00Z-18:30:00Z step 30; increase[40m] evaluated at 18:30:00Z |
| P7 | prometheus query | kube_pod_info{pod_ip=~"10.244.0.213\|10.244.0.10"} (IP to pod mapping) | at 18:08:00Z |
| K1 | kubectl | kubectl --context admin@ai get events -A -o json  (earliest/latest timestamps, count of events in window) | now |

## 1. Traefik access-log lines with DownstreamStatus >= 500 (17:50-18:30Z)

**18 lines**, all from Traefik pod `traefik-6cf45b55cf-84wsb` (the only Traefik pod in the window). Status mix: 6x 500, 1x 502, 11x 503. OriginStatus mix: 6x OriginStatus=0, 7x OriginStatus=200, 1x OriginStatus=502, 4x OriginStatus=503.

| # | time (UTC) | entryPoint | RouterName | ServiceName | ServiceAddr | Method | RequestPath (no query) | RequestHost | DS | OS | OriginDur ms | Dur ms | Retry | Traefik pod |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 2026-10-06T18:06:40Z | web | strive-sandboxes-ailab-airlock-sandboxes-a7b3d110eb742e1f6978@kubernetescrd | strive-sandboxes-ailab-sandbox-errors-errorpage-service@kubernetescrd | 10.244.0.213:8080 | GET | /sandbox/app-canon-live-309984655/strive/ | apps.strive.place | 503 | 200 | 7.1 | 19.3 | 0 | traefik-6cf45b55cf-84wsb |
| 2 | 2026-10-06T18:06:41Z | web | strive-sandboxes-ailab-airlock-sandboxes-a7b3d110eb742e1f6978@kubernetescrd | strive-sandboxes-ailab-sandbox-errors-errorpage-service@kubernetescrd | 10.244.0.213:8080 | GET | /sandbox/app-canon-live-309984655/strive/ | apps.strive.place | 503 | 200 | 1.1 | 9.4 | 0 | traefik-6cf45b55cf-84wsb |
| 3 | 2026-10-06T18:06:43Z | web | strive-sandboxes-ailab-airlock-sandboxes-a7b3d110eb742e1f6978@kubernetescrd | strive-sandboxes-ailab-sandbox-errors-errorpage-service@kubernetescrd | 10.244.0.213:8080 | GET | /sandbox/app-canon-live-309984655/strive/ | apps.strive.place | 503 | 200 | 1.6 | 10.5 | 0 | traefik-6cf45b55cf-84wsb |
| 4 | 2026-10-06T18:06:45Z | web | strive-sandboxes-ailab-airlock-sandboxes-a7b3d110eb742e1f6978@kubernetescrd | strive-sandboxes-ailab-sandbox-errors-errorpage-service@kubernetescrd | 10.244.0.213:8080 | GET | /sandbox/app-canon-live-309984655/strive/ | apps.strive.place | 503 | 200 | 1.2 | 7.5 | 0 | traefik-6cf45b55cf-84wsb |
| 5 | 2026-10-06T18:06:46Z | web | strive-sandboxes-ailab-airlock-sandboxes-a7b3d110eb742e1f6978@kubernetescrd | strive-sandboxes-ailab-sandbox-errors-errorpage-service@kubernetescrd | 10.244.0.213:8080 | GET | /sandbox/app-canon-live-309984655/strive/ | apps.strive.place | 503 | 200 | 0.9 | 6.9 | 0 | traefik-6cf45b55cf-84wsb |
| 6 | 2026-10-06T18:06:48Z | web | strive-sandboxes-ailab-airlock-sandboxes-a7b3d110eb742e1f6978@kubernetescrd | strive-sandboxes-ailab-sandbox-errors-errorpage-service@kubernetescrd | 10.244.0.213:8080 | GET | /sandbox/app-canon-live-309984655/strive/ | apps.strive.place | 503 | 200 | 1.0 | 6.8 | 0 | traefik-6cf45b55cf-84wsb |
| 7 | 2026-10-06T18:06:49Z | web | strive-sandboxes-ailab-airlock-sandboxes-a7b3d110eb742e1f6978@kubernetescrd | strive-sandboxes-ailab-sandbox-errors-errorpage-service@kubernetescrd | 10.244.0.213:8080 | GET | /sandbox/app-canon-live-309984655/strive/ | apps.strive.place | 503 | 200 | 1.3 | 42.3 | 0 | traefik-6cf45b55cf-84wsb |
| 8 | 2026-10-06T18:07:50Z | web | strive-ailab-web-e1fade25a5de4567b1ba@kubernetescrd | strive-ailab-web-e1fade25a5de4567b1ba@kubernetescrd | 10.244.0.213:8080 | GET | /sandbox/app-lifecycle-live-310054244/strive/ | strive.place | 503 | 503 | 1.5 | 103.3 | 0 | traefik-6cf45b55cf-84wsb |
| 9 | 2026-10-06T18:07:50Z | web | strive-ailab-web-e1fade25a5de4567b1ba@kubernetescrd | strive-ailab-web-e1fade25a5de4567b1ba@kubernetescrd | 10.244.0.213:8080 | GET | /sandbox/app-lifecycle-live-310054244/strive/ | strive.place | 503 | 503 | 1.7 | 11.9 | 0 | traefik-6cf45b55cf-84wsb |
| 10 | 2026-10-06T18:07:50Z | web | strive-ailab-web-e1fade25a5de4567b1ba@kubernetescrd | strive-ailab-web-e1fade25a5de4567b1ba@kubernetescrd | 10.244.0.213:8080 | GET | /sandbox/app-lifecycle-live-310054244/strive/ | strive.place | 503 | 503 | 1.8 | 15.4 | 0 | traefik-6cf45b55cf-84wsb |
| 11 | 2026-10-06T18:07:52Z | web | strive-ailab-web-e1fade25a5de4567b1ba@kubernetescrd | strive-ailab-web-e1fade25a5de4567b1ba@kubernetescrd | 10.244.0.213:8080 | GET | /sandbox/app-lifecycle-live-310054244/strive/ | strive.place | 503 | 503 | 1.0 | 6.5 | 0 | traefik-6cf45b55cf-84wsb |
| 12 | 2026-10-06T18:09:22Z | web | strive-ailab-airlock-90316d81951149c7a589@kubernetescrd | (absent) | (absent) | GET | /api/airlock/v1/apps/app-runner-report-live-310143273/sources/getUsers/records/count | strive.place | 500 | 0 | 0.0 | 1.6 | 0 | traefik-6cf45b55cf-84wsb |
| 13 | 2026-10-06T18:09:22Z | web | strive-ailab-airlock-90316d81951149c7a589@kubernetescrd | (absent) | (absent) | GET | /api/airlock/v1/apps/app-runner-report-live-310143273/sources/getOrders/records/count | strive.place | 500 | 0 | 0.0 | 1.0 | 0 | traefik-6cf45b55cf-84wsb |
| 14 | 2026-10-06T18:09:23Z | web | strive-ailab-airlock-90316d81951149c7a589@kubernetescrd | (absent) | (absent) | GET | /api/airlock/v1/apps/app-runner-report-live-310143273/sources/getUsers/records/count | strive.place | 500 | 0 | 0.0 | 2.7 | 0 | traefik-6cf45b55cf-84wsb |
| 15 | 2026-10-06T18:09:23Z | web | strive-ailab-airlock-90316d81951149c7a589@kubernetescrd | (absent) | (absent) | GET | /api/airlock/v1/apps/app-runner-report-live-310143273/sources/getOrders/records/count | strive.place | 500 | 0 | 0.0 | 1.9 | 0 | traefik-6cf45b55cf-84wsb |
| 16 | 2026-10-06T18:10:10Z | web | strive-ailab-airlock-90316d81951149c7a589@kubernetescrd | strive-ailab-airlock-90316d81951149c7a589@kubernetescrd | 10.244.0.10:5100 | GET | /api/airlock/v1/apps/contextual-docs-45ac73f9-4e3b-4316-af3d-87c658340bfc/storage/StorageBrowser/connections/d3fe0ca3-fb4b-4b45-8bca-e52c150b4cbb/items | strive.place | 502 | 502 | 7501.5 | 7516.1 | 0 | traefik-6cf45b55cf-84wsb |
| 17 | 2026-10-06T18:10:25Z | web | strive-ailab-integration-84e66188dd79e796cd88@kubernetescrd | (absent) | (absent) | GET | /api/integration/v1/connections/208baca2-52bd-4c84-b41c-42747e9d3034/data | strive.place | 500 | 0 | 0.0 | 64.0 | 0 | traefik-6cf45b55cf-84wsb |
| 18 | 2026-10-06T18:10:31Z | web | strive-ailab-notification-a4e6de499e603fea2c00@kubernetescrd | (absent) | (absent) | GET | /api/notification/v1/notifications/unread-count | strive.place | 500 | 0 | 0.0 | 12.9 | 0 | traefik-6cf45b55cf-84wsb |

DS = DownstreamStatus (what Traefik returned to cloudflared), OS = OriginStatus (status Traefik recorded from the upstream; 0 means no upstream response was recorded, i.e. Traefik generated the response). Router and Service names are verbatim from the log. In the aggregate tables of section 2 the `@kubernetescrd` suffix is stripped for width.

Groups (by time and route):

| Group | Time (UTC) | Lines | DS / OS | Router -> Service | ServiceAddr |
|---|---|---|---|---|---|
| A | 18:06:40-18:06:49 | 7 | 503 / 200 | airlock-sandboxes router -> sandbox-errors errorpage service | 10.244.0.213:8080 |
| B | 18:07:50-18:07:52 | 4 | 503 / 503 | web router -> web service | 10.244.0.213:8080 |
| C | 18:09:22-18:09:23 | 4 | 500 / 0 | airlock router -> (no service) | (absent) |
| D | 18:10:10 | 1 | 502 / 502 | airlock router -> airlock service | 10.244.0.10:5100 |
| E | 18:10:25 | 1 | 500 / 0 | integration router -> (no service) | (absent) |
| F | 18:10:31 | 1 | 500 / 0 | notification router -> (no service) | (absent) |

ServiceAddr to pod (Prometheus `kube_pod_info` at 18:08Z): `10.244.0.213` = pod `web-7c4686f946-nrf4x` (namespace strive-ailab, node talos-cp2, Ready for the entire window, 0 restarts); `10.244.0.10` = pod `airlock-668c5b5668-tcdrm` (strive-ailab, talos-cp2, Ready for the entire window, 0 restarts). `kube_pod_info` also lists `10.244.0.213` against a Velero kopia-maintain Job pod (phase Succeeded at 18:08Z), i.e. a completed pod whose recorded IP is the same; the Ready, running pod with that IP is the web pod.

Related Traefik lines around the 502 (not >= 500, shown for context): the lines whose path contains `StorageBrowser/connections` in the window are 18:10:00 GET `.../connections` 200 (0.1 s); 18:10:02 GET `.../items` DS 499 / OS 499 (2.2 s); 18:10:03 GET `.../connections` 200 (0.2 s); 18:10:10 GET `.../items` DS 502 / OS 502 (7.5 s, row 16 above). Traefik logged 15 requests with status 499 in the whole window.

## 2. Aggregates

### 2a. Requests per minute, 17:50-18:30Z (all statuses, access-log lines only)

Window total: **2819** requests; every Traefik log line in the window was an access-log line (L4 = L2, 2819 = 2819), so there were **no Traefik main-log (non-access) lines** in the window. Minutes with 0 are real zero-line minutes in Loki (traffic in this window is bursty: a ~122-request burst roughly every 3 minutes plus a busier period 18:06-18:11).

| Minute (UTC) | Total | 2xx | 3xx | 4xx | 5xx |
|---|---|---|---|---|---|
| 17:50 | 0 | 0 | 0 | 0 | 0 |
| 17:51 | 123 | 111 | 4 | 8 | 0 |
| 17:52 | 0 | 0 | 0 | 0 | 0 |
| 17:53 | 0 | 0 | 0 | 0 | 0 |
| 17:54 | 122 | 111 | 4 | 7 | 0 |
| 17:55 | 0 | 0 | 0 | 0 | 0 |
| 17:56 | 0 | 0 | 0 | 0 | 0 |
| 17:57 | 122 | 111 | 4 | 7 | 0 |
| 17:58 | 0 | 0 | 0 | 0 | 0 |
| 17:59 | 2 | 1 | 0 | 1 | 0 |
| 18:00 | 129 | 115 | 4 | 10 | 0 |
| 18:01 | 0 | 0 | 0 | 0 | 0 |
| 18:02 | 15 | 1 | 9 | 5 | 0 |
| 18:03 | 123 | 111 | 5 | 7 | 0 |
| 18:04 | 0 | 0 | 0 | 0 | 0 |
| 18:05 | 0 | 0 | 0 | 0 | 0 |
| 18:06 | 241 | 195 | 25 | 14 | 7 |
| 18:07 | 144 | 125 | 6 | 9 | 4 |
| 18:08 | 275 | 251 | 7 | 17 | 0 |
| 18:09 | 273 | 235 | 14 | 20 | 4 |
| 18:10 | 347 | 302 | 22 | 20 | 3 |
| 18:11 | 155 | 118 | 20 | 17 | 0 |
| 18:12 | 122 | 111 | 4 | 7 | 0 |
| 18:13 | 0 | 0 | 0 | 0 | 0 |
| 18:14 | 2 | 0 | 2 | 0 | 0 |
| 18:15 | 122 | 111 | 4 | 7 | 0 |
| 18:16 | 0 | 0 | 0 | 0 | 0 |
| 18:17 | 0 | 0 | 0 | 0 | 0 |
| 18:18 | 122 | 111 | 4 | 7 | 0 |
| 18:19 | 10 | 5 | 5 | 0 | 0 |
| 18:20 | 4 | 2 | 2 | 0 | 0 |
| 18:21 | 122 | 111 | 4 | 7 | 0 |
| 18:22 | 0 | 0 | 0 | 0 | 0 |
| 18:23 | 0 | 0 | 0 | 0 | 0 |
| 18:24 | 122 | 111 | 4 | 7 | 0 |
| 18:25 | 0 | 0 | 0 | 0 | 0 |
| 18:26 | 0 | 0 | 0 | 0 | 0 |
| 18:27 | 122 | 111 | 4 | 7 | 0 |
| 18:28 | 0 | 0 | 0 | 0 | 0 |
| 18:29 | 0 | 0 | 0 | 0 | 0 |

Window status distribution (DownstreamStatus): 200=2322, 201=46, 202=46, 204=46, 302=157, 400=14, 401=60, 404=92, 405=1, 409=1, 422=1, 499=15, 500=6, 502=1, 503=11. Cross-check: lines with OriginStatus >= 500 but DownstreamStatus < 500: 0.

### 2b. 5xx per minute per ServiceName, 17:50-18:30Z

Only minutes with at least one 5xx are listed (all other minutes in the window had 0). `ServiceName` is empty on lines where Traefik recorded no service; those are labelled `(none)` and distinguished by RouterName.

| Minute (UTC) | ServiceName | RouterName | DS | OS | Count |
|---|---|---|---|---|---|
| 18:06 | strive-sandboxes-ailab-sandbox-errors-errorpage-service | strive-sandboxes-ailab-airlock-sandboxes-a7b3d110eb742e1f6978 | 503 | 200 | 7 |
| 18:07 | strive-ailab-web-e1fade25a5de4567b1ba | strive-ailab-web-e1fade25a5de4567b1ba | 503 | 503 | 4 |
| 18:09 | (none) | strive-ailab-airlock-90316d81951149c7a589 | 500 | 0 | 4 |
| 18:10 | (none) | strive-ailab-integration-84e66188dd79e796cd88 | 500 | 0 | 1 |
| 18:10 | (none) | strive-ailab-notification-a4e6de499e603fea2c00 | 500 | 0 | 1 |
| 18:10 | strive-ailab-airlock-90316d81951149c7a589 | strive-ailab-airlock-90316d81951149c7a589 | 502 | 502 | 1 |

Window totals per ServiceName:

| ServiceName | RouterName (only when ServiceName absent) | DS | OS | 5xx count |
|---|---|---|---|---|
| strive-sandboxes-ailab-sandbox-errors-errorpage-service |  | 503 | 200 | 7 |
| strive-ailab-web-e1fade25a5de4567b1ba |  | 503 | 503 | 4 |
| (none) | strive-ailab-airlock-90316d81951149c7a589 | 500 | 0 | 4 |
| (none) | strive-ailab-integration-84e66188dd79e796cd88 | 500 | 0 | 1 |
| (none) | strive-ailab-notification-a4e6de499e603fea2c00 | 500 | 0 | 1 |
| strive-ailab-airlock-90316d81951149c7a589 |  | 502 | 502 | 1 |

### 2c. Whole day 2026-10-06 (00:00-24:00Z), for context

Total 5xx on 2026-10-06 in the Traefik access log: **25** (18 inside the 17:50-18:30 window, 7 outside it). All 5xx on the day fall in 18:06-18:10Z and 19:44-19:46Z; every other minute of the day had 0.

| Minute (UTC) | ServiceName | RouterName | DS | OS | Count |
|---|---|---|---|---|---|
| 18:06 | strive-sandboxes-ailab-sandbox-errors-errorpage-service | strive-sandboxes-ailab-airlock-sandboxes-a7b3d110eb742e1f6978 | 503 | 200 | 7 |
| 18:07 | strive-ailab-web-e1fade25a5de4567b1ba | strive-ailab-web-e1fade25a5de4567b1ba | 503 | 503 | 4 |
| 18:09 | (none) | strive-ailab-airlock-90316d81951149c7a589 | 500 | 0 | 4 |
| 18:10 | (none) | strive-ailab-integration-84e66188dd79e796cd88 | 500 | 0 | 1 |
| 18:10 | (none) | strive-ailab-notification-a4e6de499e603fea2c00 | 500 | 0 | 1 |
| 18:10 | strive-ailab-airlock-90316d81951149c7a589 | strive-ailab-airlock-90316d81951149c7a589 | 502 | 502 | 1 |
| 19:44 | strive-ailab-web-e1fade25a5de4567b1ba | strive-ailab-web-e1fade25a5de4567b1ba | 503 | 503 | 6 |
| 19:46 | strive-ailab-airlock-90316d81951149c7a589 | strive-ailab-airlock-90316d81951149c7a589 | 502 | 502 | 1 |

Day totals per ServiceName:

| ServiceName | RouterName (only when ServiceName absent) | DS | OS | 5xx count |
|---|---|---|---|---|
| strive-ailab-web-e1fade25a5de4567b1ba |  | 503 | 503 | 10 |
| strive-sandboxes-ailab-sandbox-errors-errorpage-service |  | 503 | 200 | 7 |
| (none) | strive-ailab-airlock-90316d81951149c7a589 | 500 | 0 | 4 |
| strive-ailab-airlock-90316d81951149c7a589 |  | 502 | 502 | 2 |
| (none) | strive-ailab-integration-84e66188dd79e796cd88 | 500 | 0 | 1 |
| (none) | strive-ailab-notification-a4e6de499e603fea2c00 | 500 | 0 | 1 |

Hourly request / 4xx / 5xx counts for the day (UTC hour starting):

| Hour | Total | 4xx | 5xx |
|---|---|---|---|
| 00:00 | 27 | 1 | 0 |
| 01:00 | 42 | 0 | 0 |
| 02:00 | 2 | 1 | 0 |
| 03:00 | 127 | 0 | 0 |
| 04:00 | 31 | 0 | 0 |
| 05:00 | 10 | 2 | 0 |
| 06:00 | 5 | 1 | 0 |
| 07:00 | 165 | 21 | 0 |
| 08:00 | 2 | 0 | 0 |
| 09:00 | 8 | 1 | 0 |
| 10:00 | 280 | 0 | 0 |
| 11:00 | 41 | 2 | 0 |
| 12:00 | 168 | 80 | 0 |
| 13:00 | 114 | 0 | 0 |
| 14:00 | 9 | 1 | 0 |
| 15:00 | 4 | 2 | 0 |
| 16:00 | 419 | 31 | 0 |
| 17:00 | 1397 | 76 | 0 |
| 18:00 | 2959 | 190 | 18 |
| 19:00 | 1916 | 129 | 7 |
| 20:00 | 1346 | 47 | 0 |
| 21:00 | 4 | 1 | 0 |
| 22:00 | 4 | 1 | 0 |
| 23:00 | 0 | 0 | 0 |

## 3. Logs at warning/error level, strive-ailab, 18:00-18:15Z

Level classification is client-side (see L10). Gatekeeper logs use the Python-logging form `LEVEL:logger:message`; every gatekeeper line in the window was `INFO`.

### 3a. Count per pod (all pods that logged in the window)

| Pod / container | Log lines | warning | error |
|---|---|---|---|
| airlock-668c5b5668-tcdrm/airlock | 1728 | 5 | 43 |
| airlock-reaper-29855160-wz28g/reaper | 1 | 0 | 0 |
| airlock-reaper-29855165-h6vnn/reaper | 1 | 0 | 0 |
| airlock-reaper-29855170-vwx7v/reaper | 1 | 0 | 0 |
| deepagent-85b9c44d77-gnlkc/deepagent | 345 | 0 | 0 |
| digest-8b6bf5c89-vpn24/digest | 657 | 0 | 0 |
| digest-worker-5f4b8889b6-9cwk6/worker | 45 | 0 | 0 |
| e2e-personas-seed-kxjwv/kcadm | 18 | 0 | 0 |
| gatekeeper-65b8f6946d-8ttzl/gatekeeper | 4313 | 0 | 0 |
| gatekeeper-65b8f6946d-v6f6v/gatekeeper | 3780 | 0 | 0 |
| hatchet-api-7c8cf5654-mgldn/api | 81 | 0 | 0 |
| hatchet-engine-6df4f9fdb5-dqg6b/engine | 1 | 0 | 0 |
| integration-748b9dffd5-dvfvw/integration | 1251 | 0 | 5 |
| integration-worker-5b8b498864-dtzg9/worker | 360 | 3 | 0 |
| keycloak-69d667cf57-t56dh/keycloak | 12 | 12 | 0 |
| keycloak-realm-seed-wdqxd/kcadm | 4 | 0 | 0 |
| keycloak-realm-sync-zdq5t/realm-sync | 68 | 0 | 0 |
| keycloak-realm-sync-zdq5t/wait-for-realm | 2 | 0 | 0 |
| knowledge-7474c68589-xwxbh/knowledge | 845 | 0 | 0 |
| load-personas-seed-ntbcp/kcadm | 1 | 0 | 0 |
| mcp-5ddff798cf-pb4wd/mcp | 12 | 2 | 4 |
| mcp-worker-6cfcb97756-f2jwq/worker | 12 | 0 | 0 |
| notification-59bff794f6-qrxnb/notification | 432 | 10 | 0 |
| openbao-platform-pg-sync-bootstrap-zbf7q/sync | 35 | 0 | 0 |
| ops-admin-persona-seed-z8867/kcadm | 2 | 0 | 0 |
| profile-64757dd8bd-b5xd9/profile | 696 | 0 | 0 |
| s2s-rotation-loadgen/loadgen | 92 | 0 | 0 |
| sentinel-75bcbc5679-8w8sk/sentinel | 301 | 0 | 0 |
| strive-pg-10/postgres | 16 | 0 | 0 |
| strive-pg-11/postgres | 9 | 0 | 0 |
| strive-pg-8/postgres | 11 | 0 | 0 |
| strive-pg-harness-bootstrap-8544z/bootstrap | 3 | 0 | 0 |
| strive-pg-init-databases-qzxq5/psql | 70 | 0 | 0 |
| tms-6c5595b75f-k6n79/tms | 555 | 0 | 2 |
| workflow-7c5cc9cdf7-2p5w5/workflow | 789 | 0 | 12 |
| workflow-worker-7f9f6b8c9c-7j4rd/worker | 412 | 0 | 5 |

Gatekeeper: both pods (`gatekeeper-65b8f6946d-8ttzl`, `gatekeeper-65b8f6946d-v6f6v`) logged **0 warning and 0 error** lines in 18:00-18:15Z (4313 and 3780 INFO lines).

### 3b. Distinct warning/error message templates

| Count | Level | Pod(s) | Template (ids/uuids/IPs/timestamps stripped) |
|---|---|---|---|
| 17 | error | airlock-668c5b5668-tcdrm x17 | Request Failed: GET /api/v1/apps/<id> -> 400 AppNotFound \| msg: App not found. (ID: <id>) |
| 10 | error | workflow-7c5cc9cdf7-2p5w5 x10 | Request Failed: DELETE /api/v1/workflows/<uuid> -> 404 HTTPException \| msg: <n>: Workflow not found. (ID: <uuid>) |
| 9 | warning | notification-59bff794f6-qrxnb x9 | <ts> - app.services.ingest - WARNING - Render attempt <n>/<n> failed for event <uuid> (com.platform.connection.deleted): no renderer registered for event_type=com.platform.connection.deleted |
| 7 | error | airlock-668c5b5668-tcdrm x7 | Request Failed: GET /api/v1/app-operations/latest -> 400 AppOperationNotFound \| msg: App operation not found. (ID: <id>:purge) |
| 7 | error | airlock-668c5b5668-tcdrm x7 | Request Failed: GET /api/v1/app-operations/latest -> 400 AppOperationNotFound \| msg: App operation not found. (ID: <id>:soft_delete) |
| 6 | warning | keycloak-69d667cf57-t56dh x6 | <ts> WARN [org.keycloak.cookie.DefaultCookieProvider] (executor-thread-<n>) Non-secure context detected; cookies are not secured, and will not be available in cross-origin POST requests |
| 5 | warning | keycloak-69d667cf57-t56dh x5 | <ts> WARN [org.keycloak.events] (executor-thread-<n>) type="CLIENT_LOGIN_ERROR", realmId="<uuid>", realmName="strive", clientId="e2e-readiness-probe", userId="null", ipAddress="<ip>", error="client_no |
| 5 | error | workflow-worker-7f9f6b8c9c-7j4rd x5 | ERROR:worker.runner:workflow run <uuid> failed |
| 3 | error | airlock-668c5b5668-tcdrm x3 | Request Failed: GET /api/v1/internal/shares/resolve-by-name -> 400 ShareLinkNotFound \| msg: ShareLink not found. (ID: unknown) |
| 3 | warning | integration-worker-5b8b498864-dtzg9 x3 | [WARNING] -- <ts> - THE TIME TO START THE TASK RUN IS TOO LONG, THE EVENT LOOP MAY BE BLOCKED. See https://docs.hatchet.run/blog/<id> for details and debugging help. time to start: <n> |
| 2 | warning | mcp-5ddff798cf-pb4wd x2 | <ts> - app.api.v1.endpoints.provider_activation - WARNING - cross-service activation failed for integration <uuid> provider <id>: Provider '<id>' at https://api.githubcopilot.com/mcp/ is not reachable |
| 2 | error | mcp-5ddff798cf-pb4wd x2 | <ts> - app.gateway.client_pool - ERROR - Failed to connect to provider '<id>' at https://api.githubcopilot.com/mcp/: Client error '<n> Unauthorized' for url 'https://api.githubcopilot.com/mcp/' |
| 2 | error | mcp-5ddff798cf-pb4wd x2 | <ts> - app.services.provider_service - ERROR - Failed to connect provider '<id>': Provider '<id>' at https://api.githubcopilot.com/mcp/ is not reachable: Client error '<n> Unauthorized' for url 'https |
| 2 | error | airlock-668c5b5668-tcdrm x2 | Request Failed: GET /api/v1/apps/<id>/params -> 400 AppNotFound \| msg: App not found. (ID: <id>) |
| 2 | error | airlock-668c5b5668-tcdrm x2 | Request Failed: GET /api/v1/apps/<id>/releases/source -> 404 HTTPException \| msg: <n>: shared runtime disabled |
| 2 | error | integration-748b9dffd5-dvfvw x2 | Request Failed: GET /api/v1/connections/<uuid> -> 404 HTTPException \| msg: <n>: Integration not found. (ID: <uuid>) |
| 1 | warning | notification-59bff794f6-qrxnb x1 | <ts> - app.services.ingest - WARNING - Render attempt <n>/<n> failed for event <uuid> (platform_store.file.added): no renderer registered for event_type=platform_store.file.added |
| 1 | warning | keycloak-69d667cf57-t56dh x1 | <ts> WARN [org.keycloak.events] (executor-thread-<n>) type="LOGIN_ERROR", realmId="<uuid>", realmName="strive", clientId="gatekeeper", userId="null", ipAddress="<ip>", error="invalid_redirect_uri", re |
| 1 | error | airlock-668c5b5668-tcdrm x1 | Request Failed: GET /api/v1/app-operations/latest -> 400 AppOperationNotFound \| msg: App operation not found. (ID: contextual-docs-<uuid>:purge) |
| 1 | error | airlock-668c5b5668-tcdrm x1 | Request Failed: GET /api/v1/app-operations/latest -> 400 AppOperationNotFound \| msg: App operation not found. (ID: contextual-docs-<uuid>:soft_delete) |
| 1 | error | airlock-668c5b5668-tcdrm x1 | Request Failed: GET /api/v1/apps/<id> -> 400 AppNotFound \| msg: App not found. (ID: contextual-docs-<uuid>) |
| 1 | error | airlock-668c5b5668-tcdrm x1 | Request Failed: GET /api/v1/apps/<id>/sources/getUsers/records/count -> 400 AppNotFound \| msg: App not found. (ID: <id>) |
| 1 | error | airlock-668c5b5668-tcdrm x1 | Request Failed: GET /api/v1/apps/<id>/storage/StorageBrowser/operations -> 400 AppNotFound \| msg: App not found. (ID: contextual-docs-<uuid>) |
| 1 | error | integration-748b9dffd5-dvfvw x1 | Request Failed: GET /api/v1/connections/<uuid>/data -> 404 HTTPException \| msg: <n>: Integration not found. (ID: <uuid>) |
| 1 | error | tms-6c5595b75f-k6n79 x1 | Request Failed: GET /api/v1/tenants/<uuid> -> 404 HTTPException \| msg: <n>: Tenant not found. (ID: <uuid>) |
| 1 | error | tms-6c5595b75f-k6n79 x1 | Request Failed: GET /api/v1/tenants/by-slug/not-my-tenant -> 404 HTTPException \| msg: <n>: Tenant not found. (ID: not-my-tenant) |
| 1 | error | workflow-7c5cc9cdf7-2p5w5 x1 | Request Failed: GET /api/v1/workflows/<uuid> -> 404 HTTPException \| msg: <n>: Workflow not found. (ID: <uuid>) |
| 1 | error | workflow-7c5cc9cdf7-2p5w5 x1 | Request Failed: PATCH /api/v1/workflows/<uuid> -> 400 HTTPException \| msg: <n>: There was an error parsing the body |
| 1 | error | integration-748b9dffd5-dvfvw x1 | Request Failed: POST /api/v1/connections -> 400 AlreadyExistsError \| msg: Integration already exists. (Identifier: internal.sample@<uuid>) |
| 1 | error | integration-748b9dffd5-dvfvw x1 | Request Failed: POST /api/v1/integrations/<uuid>/oauth/start -> 422 HTTPException \| msg:  |
| 1 | warning | airlock-668c5b5668-tcdrm x1 | app <id>: recordTypes not in the record-type registry (advisory; <id>=true refuses them): getUsers: 'user_row' |
| 1 | warning | airlock-668c5b5668-tcdrm x1 | app <id>: recordTypes not in the record-type registry (advisory; <id>=true refuses them): getUsers: 'user_row'; getOrders: 'order_row' |
| 1 | warning | airlock-668c5b5668-tcdrm x1 | app <id>: recordTypes not in the record-type registry (advisory; <id>=true refuses them): news: 'news_item'; progress: 'news_run_marker' |
| 1 | warning | airlock-668c5b5668-tcdrm x1 | app <id>: recordTypes not in the record-type registry (advisory; <id>=true refuses them): search: 'search_index'; docs: 'search_index'; results: 'extraction_result'; chat: 'message' |
| 1 | warning | airlock-668c5b5668-tcdrm x1 | app contextual-docs-<uuid>: recordTypes not in the record-type registry (advisory; <id>=true refuses them): search: 'search_index'; docs: 'search_index'; chat: 'message' |

Traceback continuation lines (no level token) belonging to the `workflow run <uuid> failed` errors, by exception line (quoted values masked): worker.handlers._template.TemplateEvaluationError: number() cannot read '<str>' as a number x5; weld.workflow.errors.PermanentStepError: map: record <n>: set.price: number() cannot read '<str>' as a number x5.

### 3c. Gatekeeper `api.access` lines (HTTP statuses logged by gatekeeper)

Status distribution, both pods, 18:00-18:15Z: 200=5005, 302=86, 401=22. **No 5xx.** The 4xx responses were: GET /auth -> 401 x14; GET /auth/userinfo -> 401 x7; GET /auth/session -> 401 x1.

| Minute (UTC) | Statuses logged by gatekeeper api.access |
|---|---|
| 18:03 | 200 x338, 302 x4, 401 x3 |
| 18:04 | 200 x225 |
| 18:05 | 200 x225 |
| 18:06 | 200 x424, 302 x17, 401 x3 |
| 18:07 | 200 x367, 302 x4 |
| 18:08 | 200 x492, 302 x5 |
| 18:09 | 200 x469, 302 x10, 401 x3 |
| 18:10 | 200 x536, 302 x15, 401 x1 |
| 18:11 | 200 x336, 302 x14, 401 x1 |
| 18:12 | 200 x340, 302 x3, 401 x3 |
| 18:13 | 200 x224 |

Log continuity (gaps between consecutive log lines; the S2S rotation load generator keeps both pods continuously logging): `gatekeeper-65b8f6946d-8ttzl`: 4313 lines, first 18:00:01, last 18:14:59, max gap 3.7 s over 18:00-18:15 (3.7 s over 18:05-18:12); `gatekeeper-65b8f6946d-v6f6v`: 3780 lines, first 18:00:00, last 18:14:59, max gap 2.6 s over 18:00-18:15 (2.6 s over 18:05-18:12).

### 3d. Other namespaces (not requested, preserved because they fall in the incident window)

**Traefik (`platform-edge`)**: 0 non-access log lines in 17:50-18:30Z (Traefik runs with `--log.level=INFO` and `--accesslog=true --accesslog.format=json`, no access-log filters, so every request appears in the access log and no Traefik error/info line explaining a 5xx was emitted).

**cloudflared (`edge`)**, 17:50-18:30Z: lines per pod = `cloudflared-7dc9f56777-g9gml` 4, `cloudflared-strive-69d5b5ff6d-8jtwn` 116; level counts = ERR=120. Every cloudflared line in the window was `ERR`; the only message shapes are `Request failed error="stream <n> canceled by remote with error code <n>"` (error code 0 in all 86 stream-cancel lines) and `Request failed error="Incoming request ended abruptly: context canceled"`, each logged as a pair (a `Request failed ... dest=...` line plus a connection-level line with `ingressRule=` and `originService=http://traefik.platform-edge.svc.cluster.local:<n>` or the gitea/llm-router origin). Events per minute (counting the `dest=` lines only):

| Minute (UTC) | cloudflared deployment | Error class | Events |
|---|---|---|---|
| 18:06 | cloudflared-strive-69d5b5ff6d | Incoming request ended abruptly: context canceled | 6 |
| 18:06 | cloudflared-strive-69d5b5ff6d | stream canceled by remote | 2 |
| 18:07 | cloudflared-strive-69d5b5ff6d | stream canceled by remote | 6 |
| 18:08 | cloudflared-strive-69d5b5ff6d | stream canceled by remote | 11 |
| 18:09 | cloudflared-strive-69d5b5ff6d | stream canceled by remote | 4 |
| 18:10 | cloudflared-strive-69d5b5ff6d | Incoming request ended abruptly: context canceled | 7 |
| 18:10 | cloudflared-strive-69d5b5ff6d | stream canceled by remote | 12 |
| 18:11 | cloudflared-strive-69d5b5ff6d | Incoming request ended abruptly: context canceled | 3 |
| 18:11 | cloudflared-strive-69d5b5ff6d | stream canceled by remote | 7 |
| 18:14 | cloudflared-7dc9f56777 | Incoming request ended abruptly: context canceled | 1 |
| 18:19 | cloudflared-7dc9f56777 | stream canceled by remote | 1 |

Destinations of the cloudflared `Request failed` events (host + path, ids normalised, query strings removed):

| Deployment | Destination | Error class | Events | First | Last |
|---|---|---|---|---|---|
| cloudflared-strive-69d5b5ff6d | strive.place/api/notification/v1/stream | stream canceled by remote | 42 | 18:06 | 18:11 |
| cloudflared-strive-69d5b5ff6d | apps.strive.place/api/notification/v1/notifications/unread-count | Incoming request ended abruptly: context canceled | 2 | 18:06 | 18:06 |
| cloudflared-strive-69d5b5ff6d | apps.strive.place/api/profile/v1/me/preferences | Incoming request ended abruptly: context canceled | 2 | 18:06 | 18:06 |
| cloudflared-strive-69d5b5ff6d | apps.strive.place/api/notification/v1/notifications | Incoming request ended abruptly: context canceled | 2 | 18:06 | 18:06 |
| cloudflared-strive-69d5b5ff6d | strive.place/api/integration/v1/connections/<uuid>/data | Incoming request ended abruptly: context canceled | 2 | 18:10 | 18:10 |
| cloudflared-strive-69d5b5ff6d | strive.place/api/workflow/v1/workflows/<uuid> | Incoming request ended abruptly: context canceled | 2 | 18:11 | 18:11 |
| cloudflared-7dc9f56777 | git.chifor.me/api/v1/repos/cchifor/platform/commits/<id>/statuses | Incoming request ended abruptly: context canceled | 1 | 18:14 | 18:14 |
| cloudflared-7dc9f56777 | router.chifor.me/admin/v1/events | stream canceled by remote | 1 | 18:19 | 18:19 |
| cloudflared-strive-69d5b5ff6d | strive.place/auth/userinfo | Incoming request ended abruptly: context canceled | 1 | 18:11 | 18:11 |
| cloudflared-strive-69d5b5ff6d | strive.place/api/airlock/v1/apps/<id>/storage/StorageBrowser/connections/<uuid>/items | Incoming request ended abruptly: context canceled | 1 | 18:10 | 18:10 |
| cloudflared-strive-69d5b5ff6d | strive.place/api/notification/v1/notifications/unread-count | Incoming request ended abruptly: context canceled | 1 | 18:10 | 18:10 |
| cloudflared-strive-69d5b5ff6d | strive.place/api/notification/v1/notifications | Incoming request ended abruptly: context canceled | 1 | 18:10 | 18:10 |
| cloudflared-strive-69d5b5ff6d | strive.place/api/profile/v1/me/preferences | Incoming request ended abruptly: context canceled | 1 | 18:10 | 18:10 |
| cloudflared-strive-69d5b5ff6d | strive.place/api/integration/v1/connections | Incoming request ended abruptly: context canceled | 1 | 18:10 | 18:10 |

## 4. Prometheus, 17:50-18:30Z

### 4a. Availability of signals

| Signal | Exists? | Result for the window |
|---|---|---|
| `kube_pod_container_status_restarts_total` (platform-edge, edge, strive-ailab) | yes | 79 container series seen; **0 changes** in the window; `max_over_time` over the window is 0 for all 78 series evaluable at 18:30Z (so also 0 historic restarts for those pods). Traefik pod, 4 cloudflared pods, 2 gatekeeper pods, airlock, integration, notification, web: all 0. |
| `kube_pod_status_ready` / `kube_pod_container_status_ready` | yes | see 4c: no readiness change on any long-running pod; only Job/CronJob pods show changes (completion) |
| `kube_endpoint_address` / `kube_endpoint_*` | **no** | no series with a `kube_endpoint_` prefix exist in the TSDB (only `kube_endpointslice_*`) |
| `kube_endpointslice_endpoints`, `_info` | yes (platform-edge: 1 slice, strive-ailab: 24 slices; none for `edge`, cloudflared has no Service) | 25 slices examined; **no change** in ready/serving endpoint counts or slice existence in the window |
| cloudflared metrics (series names containing `cloudflared` or `tunnel`) | **no** | no such series anywhere in the TSDB; `up{namespace=~"edge\|platform-edge"}` has no targets - neither `edge` nor `platform-edge` is scraped |
| Traefik metrics (series names containing `traefik`) | **no** | no such series in the TSDB although the Traefik deployment sets `--metrics.prometheus=true` on entrypoint `metrics` (:9100); it is not scraped |
| `up` for strive-ailab targets | yes | 7 targets, 560 samples in the window, minimum `up` = 1 (no failed scrape) |
| `http_server_request_duration_seconds_count` 5xx by route | yes (jobs: airlock, digest, gatekeeper, integration, knowledge, mcp, notification, profile, sentinel, tms, workflow) | see 4d |

### 4b. Container restarts

No restart-counter change for any of the 79 container series in platform-edge, edge and strive-ailab (P1). Series that only exist for part of the window are short-lived Job/CronJob pods (first/last sample inside the window), all with counter 0: `strive-pg-harness-bootstrap-7mpnt` 17:54-18:09; `strive-pg-harness-bootstrap-8544z` 18:14-18:29; `e2e-personas-seed-kvlms` 18:25-18:30; `e2e-personas-seed-kxjwv` 18:04-18:15; `e2e-personas-seed-mnf28` 17:50-17:55; `keycloak-realm-seed-58ks7` 18:24-18:30; `keycloak-realm-seed-skz6l` 17:50-17:52; `keycloak-realm-seed-wdqxd` 18:05-18:14; `keycloak-realm-seed-znqtq` 17:54-18:04; `load-personas-seed-fc294` 17:54-18:04; `load-personas-seed-ntbcp` 18:15-18:25; `ops-admin-persona-seed-2c7ln` 17:54-18:05; `ops-admin-persona-seed-z8867` 18:15-18:26; `strive-pg-init-databases-824vv` 17:53-18:03; `strive-pg-init-databases-lgdkx` 18:23-18:30; `strive-pg-init-databases-qzxq5` 18:04-18:13; `keycloak-realm-sync-7cktl` 17:54-18:04; `keycloak-realm-sync-kr5gm` 17:50-17:52; `keycloak-realm-sync-zdq5t` 18:14-18:24; `airlock-reaper-29855135-9tldj` 17:50-17:50; `airlock-reaper-29855140-7j2sl` 17:50-17:55; `airlock-reaper-29855145-6qj2t` 17:50-18:00; `airlock-reaper-29855150-2ckq4` 17:50-18:05; `airlock-reaper-29855155-vkbcx` 17:55-18:10; `airlock-reaper-29855160-wz28g` 18:00-18:15; `airlock-reaper-29855165-h6vnn` 18:05-18:20; `airlock-reaper-29855170-vwx7v` 18:10-18:25; `airlock-reaper-29855175-g929w` 18:15-18:30; `airlock-reaper-29855180-xpxrb` 18:20-18:30; `airlock-reaper-29855185-bgpnj` 18:25-18:30; `openbao-platform-pg-sync-bootstrap-fttfs` 17:50-18:03; `openbao-platform-pg-sync-bootstrap-zbf7q` 18:04-18:30.

### 4c. Pod readiness changes

Pods Ready=1 for the **entire** window (min_over_time = 1, 34 pods, includes `traefik-6cf45b55cf-84wsb`, the 4 cloudflared pods, both gatekeeper pods, airlock, integration, notification, web): `airlock-668c5b5668-tcdrm`, `ci-objectstore-5855b87d76-t7479`, `cloudflared-7dc9f56777-g9gml`, `cloudflared-7dc9f56777-w927j`, `cloudflared-strive-69d5b5ff6d-4kpxl`, `cloudflared-strive-69d5b5ff6d-8jtwn`, `deepagent-85b9c44d77-gnlkc`, `digest-8b6bf5c89-vpn24`, `digest-worker-5f4b8889b6-9cwk6`, `gatekeeper-65b8f6946d-8ttzl`, `gatekeeper-65b8f6946d-v6f6v`, `harness-75f68c696f-lvkjh`, `hatchet-api-7c8cf5654-mgldn`, `hatchet-engine-6df4f9fdb5-dqg6b`, `integration-748b9dffd5-dvfvw`, `integration-worker-5b8b498864-dtzg9`, `keycloak-69d667cf57-t56dh`, `knowledge-7474c68589-xwxbh`, `mcp-5ddff798cf-pb4wd`, `mcp-worker-6cfcb97756-f2jwq`, `notification-59bff794f6-qrxnb`, `objectstore-7fd787c788-vgz7g`, `profile-64757dd8bd-b5xd9`, `s2s-rotation-loadgen`, `sentinel-75bcbc5679-8w8sk`, `strive-pg-10`, `strive-pg-11`, `strive-pg-8`, `tms-6c5595b75f-k6n79`, `traefik-6cf45b55cf-84wsb`, `valkey-master-0`, `web-7c4686f946-nrf4x`, `workflow-7c5cc9cdf7-2p5w5`, `workflow-worker-7f9f6b8c9c-7j4rd`.

Pods with any Ready=0 sample in the window (44): all are Job/CronJob pods (reaper, seed, sync, bootstrap, init-databases, expiry, backup, k6): `airlock-reaper-29855140-7j2sl`, `airlock-reaper-29855145-6qj2t`, `airlock-reaper-29855150-2ckq4`, `airlock-reaper-29855155-vkbcx`, `airlock-reaper-29855160-wz28g`, `airlock-reaper-29855165-h6vnn`, `airlock-reaper-29855170-vwx7v`, `airlock-reaper-29855175-g929w`, `airlock-reaper-29855180-xpxrb`, `airlock-reaper-29855185-bgpnj`, `ci-objectstore-bucket-init-8s5wd`, `ci-objectstore-expiry-29851420-dflt8`, `ci-objectstore-expiry-29852860-ckn5f`, `ci-objectstore-expiry-29854300-6phz7`, `e2e-personas-seed-kvlms`, `e2e-personas-seed-kxjwv`, `e2e-personas-seed-mnf28`, `k6-weekly-soak-29851320-pfwmk`, `keycloak-realm-seed-58ks7`, `keycloak-realm-seed-skz6l`, `keycloak-realm-seed-wdqxd`, `keycloak-realm-seed-znqtq`, `keycloak-realm-sync-7cktl`, `keycloak-realm-sync-kr5gm`, `keycloak-realm-sync-zdq5t`, `load-personas-seed-fc294`, `load-personas-seed-ntbcp`, `openbao-platform-pg-sync-29851427-n9ldq`, `openbao-platform-pg-sync-29852867-5kwvr`, `openbao-platform-pg-sync-29854307-fjkn8`, `openbao-platform-pg-sync-bootstrap-fttfs`, `openbao-platform-pg-sync-bootstrap-zbf7q`, `ops-admin-persona-seed-2c7ln`, `ops-admin-persona-seed-z8867`, `strive-pg-backup-composition-20261001-v2-tx464`, `strive-pg-backup-diagnostic-20261001-8c7j5`, `strive-pg-harness-bootstrap-7mpnt`, `strive-pg-harness-bootstrap-8544z`, `strive-pg-init-databases-824vv`, `strive-pg-init-databases-lgdkx`, `strive-pg-init-databases-qzxq5`, `workflow-artifact-expiry-29851460-4xqsn`, `workflow-artifact-expiry-29852900-ht8jn`, `workflow-artifact-expiry-29854340-8rt4z`.

Ready transitions (1 -> 0) observed on the same Job pods, at pod completion: `e2e-personas-seed-kvlms` 18:27:00 1->0; `e2e-personas-seed-kxjwv` 18:06:00 1->0; `keycloak-realm-sync-7cktl` 17:55:00 1->0; `load-personas-seed-fc294` 17:55:00 1->0; `load-personas-seed-ntbcp` 18:15:30 1->0; `ops-admin-persona-seed-2c7ln` 17:56:00 1->0; `ops-admin-persona-seed-z8867` 18:16:30 1->0. No long-running pod had a readiness transition. Pods in phase Pending/Failed/Unknown during the window: `keycloak-realm-sync-zdq5t` (Pending, sample at 18:14), `strive-pg-harness-bootstrap-8544z` (Pending, sample at 18:14) (Job pods starting up).

### 4d. HTTP 5xx recorded by the applications (`http_server_request_duration_seconds_count`, `http_status_code=~"5.."`)

Raw counters (step 30 s, exact sample values) for every 5xx series that exists in the window. Per-minute `increase(...[1m])` was also run but is not used here: with a 30 s scrape interval a 1-minute range can hold a single sample, so it under-reports (it returned a row for integration but none for airlock for the same 18:10:30 step). The raw transitions are authoritative.

| Job | Method | Route | Status | Value at 17:50 | Value at 18:30 | Transitions |
|---|---|---|---|---|---|---|
| airlock | GET | /api/v1/apps/{app_id}/storage/{component_id}/connections/{connection_id}/items | 502 | 2 | 4 | 18:10:30 2->4 |
| integration | GET | /api/v1/internal/storage/connections/{connection_id}/items | 502 | 2 | 4 | 18:10:30 2->4 |
| gatekeeper | POST | /auth/token | 503 | 55 | 55 | none |
| gatekeeper | POST | /auth/token | 503 | 55 | 55 | none |

Gatekeeper: its two `POST /auth/token` 503 series (one per replica, constant at 55, no change in the window) pre-date the window; there is **no gatekeeper 5xx increase in 17:50-18:30Z**. Gatekeeper response statuses in the window (`increase` over 40 min, summed over replicas, approximate because Prometheus extrapolates): 200=11592.9, 302=115.7, 401=57.3, 403=14.1, 405=0.0, 503=0.0. Among the jobs that expose this metric (airlock, digest, gatekeeper, integration, knowledge, mcp, notification, profile, sentinel, tms, workflow), the only 5xx counter change in the window is the 502 on the airlock and integration storage `.../items` routes at 18:10:30 (the Traefik 502 of group D); none recorded a 5xx for the routes behind the Traefik 500s (groups C, E, F: `/api/airlock/...getUsers|getOrders/records/count`, `/api/integration/v1/connections/<uuid>/data`, `/api/notification/v1/notifications/unread-count`). `web` and the sandbox upstreams (groups A, B) do not expose this metric.

## 5. Kubernetes events

Verified with one `kubectl --context admin@ai get events -A -o json` (K1): 1663 events exist cluster-wide, the earliest timestamp is **2026-10-07T06:54:52Z** and the latest 2026-10-07T07:55:32Z; **0 events** have a first/last timestamp inside 2026-10-06T17:50-18:30Z. Events from the incident window are no longer available (the retained span of about 1 h is consistent with the default Kubernetes event TTL).

## Limitations (what could not be recovered, and why)

- **Kubernetes events from the window are gone** (earliest remaining event is 2026-10-07T06:54:52Z; events are short-lived). Pod scheduling/probe-failure/Unhealthy events, and any Endpoints churn events, cannot be recovered.
- **No cloudflared or Traefik metrics exist**: neither `edge` nor `platform-edge` is scraped by Prometheus (no targets, no `cloudflared*`/`traefik*` series), so per-router/per-service Traefik status counts, retry counters, tunnel connection/HA state and request-error counters for the window are unrecoverable from metrics. Only the Traefik access-log lines (Loki) and cloudflared `ERR` log lines remain.
- **No `kube_endpoint_address` series** exists (only `kube_endpointslice_*`), and `edge` has no EndpointSlices; endpoint-level churn is therefore only visible at EndpointSlice granularity (no change observed) and at the 30 s sample interval.
- **The Traefik log is INFO level**: Traefik does not log the reason for a proxy-generated response (e.g. which middleware or which dial/transport error produced a 500/502). For the 500s with OriginStatus 0 and no ServiceName, the log does not say which middleware or router stage produced the response. For the 503s with ServiceName = the sandbox errorpage service (group A), the recorded OriginStatus 200 is the error-page service's own response; the status of the sandbox upstream whose response was replaced is not in the line.
- **Header and client-side data were deliberately not preserved** (client addresses, headers, cookies, query strings), so individual requests cannot be correlated to a user/session from this record; paths retain resource identifiers.
- **Retention windows**: Loki lines expire ~2026-10-13T18:10Z (168 h); Prometheus retains from 2026-09-22 so metrics outlive the logs. This record contains aggregates and the 18 redacted 5xx lines, not the other ~2800 access-log lines in the window.
- **The application pods that served the window have since been replaced**: at collection time the current gatekeeper, airlock, integration and notification pods were ~44 minutes old (created about 07:04Z on 2026-10-07), and Prometheus `kube_pod_info` for `web-7c4686f946-nrf4x` ends at 18:43:30Z on 2026-10-06. Their logs survive only as Loki streams (pod label, retention 168 h); the live pods cannot be inspected for the incident. Traefik (`traefik-6cf45b55cf-84wsb`) and the cloudflared pods are the same pods as in the window.
- **Counting caveats**: per-minute counts use Loki `count_over_time` buckets labelled by minute start; Prometheus `increase()` values are extrapolated; Prometheus counters for the airlock/integration 502 routes rose by 2 between 18:10:00 and 18:10:30 whereas the Traefik log has one 502 line (and one 499 line on the same route) in that interval - the difference was not resolved.

## Summary (what the evidence shows, without speculation)

- 18 requests returned 5xx through the single Traefik pod in 17:50-18:30Z, in six short clusters between 18:06:40 and 18:10:31Z (11x 503, 6x 500, 1x 502); 7 further 5xx (6x 503 web, 1x 502 airlock) occurred at 19:44-19:46Z and none at any other time on 2026-10-06.
- **Edge-generated (OriginStatus 0, no ServiceName/ServiceAddr, OriginDuration 0):** the 6 HTTP 500s at 18:09:22-18:10:31Z on the airlock, integration and notification routers; no application metric recorded a corresponding 5xx.
- **Upstream status recorded (OriginStatus != 0):** the 4x 503 on the web service (OriginStatus 503, 18:07:50); the 7x 503 logged against the sandbox errorpage service with OriginStatus 200 (18:06:40-49; the line does not carry the status of the upstream whose response was replaced); and the 502 on airlock (OriginStatus 502 after 7.5 s, 18:10:10), coinciding with 502 counter increments on the airlock and integration application metrics at 18:10:30.
- No container restarts, readiness changes, EndpointSlice changes or failed scrapes were recorded for traefik, cloudflared, gatekeeper or the application pods; gatekeeper logged no warning/error lines and no 5xx; cloudflared logged 120 `ERR` lines (stream canceled by remote / context canceled) between 18:06 and 18:11Z, coinciding with the busier traffic period (241-347 requests/min vs ~122 per 3-minute burst otherwise).
- The data preserved here do not identify the component that produced the 500s with OriginStatus 0; Kubernetes events and Traefik/cloudflared metrics for the window do not exist.
