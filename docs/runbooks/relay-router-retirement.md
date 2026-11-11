# Relay to Router protected retirement transport

This change reserves a private ClusterIP Service and pod scoped network path for
Relay to call Router's `POST /internal/worker-retirement/v1`. It does not enable
the listener. The running Router image has no listener on port 8788, and neither
Deployment sets retirement configuration. The Service is not referenced by the
edge proxy or Cloudflare tunnel.

## Activation contract

Activate in a separate reviewed change after all of these are available and
recorded in that change:

1. Router PR `cchifor/llm-router#173` and its paired Relay caller are merged.
   Pin each runtime image by digest and record the source commits and image
   provenance. Run the paired test through the real Router Worker, signed Relay
   source, and both private TLS endpoints at those revisions. The previous wire
   test used a simulated Router execution callback and is not that proof.
2. Install the source SQL and cleanup authorization revision required by the
   paired protocol. Confirm the signed closure contains the target admission,
   the Router source policy and trust are pinned, and the source reader and
   transition signing identities are distinct. Record a recovery test from an
   uncertain Router HTTP reply by inspecting native and signed source state.
3. Take and verify a Router SQLite backup before applying any new native
   schema or runtime image. The existing `/data` RWO volume holds the live WAL;
   follow `docs/runbooks/llm-router.md` for its backup and rollback procedure.
   Preserve the Relay source database backup and its restore evidence as well.
4. Provision a dedicated CA, Router server certificate and private key, and
   a dedicated Relay client certificate and private key. Pin the exact Relay
   client DER certificate SHA-256 in Router's listener configuration. The
   server certificate must cover
   `llm-router-retirement.llm-router.svc.cluster.local`; Relay must verify that
   DNS name and the CA. Neither side may reuse the public edge credential.
5. Supply `ROUTER_RETIREMENT_SERVER_CONFIG_FILE` and
   `ROUTER_RETIREMENT_SERVER_CONFIG_SHA256` together. The pinned JSON must use
   `version: 1`, `host: "0.0.0.0"`, `port: 8788`, and fields `ca` and
   `certificate` as `{path, sha256}` pairs, plus `keyFile` and
   `clientCertificateSha256`. The config, CA, certificate, and key must be
   regular files at absolute paths. The file reader rejects Kubernetes Secret
   projection symlinks. The private key must have no group or other mode bits;
   public pinned files must not be group or other writable. Independently pin
   the source manifest using `ROUTER_PROVENANCE_FILE` and
   `ROUTER_PROVENANCE_SHA256`; its cleanup entries need the authorization,
   reader connection, transition connection, and their separate signing keys.
   The protected Worker also needs its trust, policy, encryption key, and
   budget authority configuration. Configure the Relay caller with this
   Service URL, CA, client identity, and its own pins.
6. Resolve file ownership under the Router pod's restricted security context
   before activation. It runs as uid 1000. A Kubernetes Secret file with mode
   `0400` is root owned and unreadable by Router; an `fsGroup` readable mode
   such as `0440` is rejected for private keys. The current file reader accepts
   a regular file owned by uid 0 or the process uid. A nonroot init container
   can copy projected input into a memory backed `emptyDir` as uid 1000, set
   private files to `0400` and public files to `0444`, then mount only that
   prepared directory read only into Router. Validate the exact image's
   ownership check and startup with this layout. Router's source design
   document currently calls for root owned keys, so reconcile that wording
   with the tested pod layout before approving activation.
7. Set `ROUTER_DRAIN_MS=40000` and keep
   `terminationGracePeriodSeconds >= 60`. The 40 second drain and 57 second
   watchdog are part of the private listener's refusal and recovery contract.
   Add a `containerPort` named `retirement-https` at 8788 for inspection. Do
   not add this port to the public `llm-router` Service or edge proxy.

## Network boundary and checks

`llm-router-retirement` selects only pods labeled `app: llm-router`, with a
single TCP port 8788. The additive Router NetworkPolicy admits it only from
pods labeled `app: relay` in namespace `relay`; Relay's egress policy permits
only that destination and port. Kubernetes port forwarding by a sufficiently
privileged operator can bypass NetworkPolicy, so the listener still requires
mTLS and the pinned client certificate. The existing public Router ingress
remains limited to the edge proxy on 8787, and metrics ingress to Prometheus on
9464.

Before and after an activation rollout, verify the rendered selectors and
endpoints, the image digest, the source and server config hashes, and the
listener's TLS peer checks. Send one valid retirement command for a disposable
fixture and verify the native and signed source observations. Repeat with a
wrong client certificate, an altered tuple, an expired `notAfter`, and a
connection loss after commit; those must refuse or enter the documented
uncertain recovery path. No command should be retried solely because its HTTP
reply was lost.

Rollback of a prepared Service or policy is removal through another reviewed
GitOps change. Rollback of an activated Worker requires inspecting native and
source retirement state before changing the image or restoring a database.
