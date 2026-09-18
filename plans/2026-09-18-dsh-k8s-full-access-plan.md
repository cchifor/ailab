# dsh: full Kubernetes access, one binding, no admission guard

## Context

dsh (https://dsh.chifor.me, `kubernetes/apps/apps/dsh/`) is the operator's agent harness. The
operator has decided it gets **full access to the ailab Kubernetes cluster** and that the
implementation must be simple. Three PRs in the last day tried to give it a *bounded* identity
and the result does not work; the operator's direction is to stop bounding it.

What is live on `main` today (#770, #771, #772, all merged) and why it fails:

1. `k8s-admin.yaml` — ServiceAccount `dsh-k8s-admin` + a namespace-scoped Role over Jobs,
   ConfigMaps, PVCs, NetworkPolicies, Pod reads. Anything outside `dsh` is forbidden, including
   reading the very ValidatingAdmissionPolicy that denies it (cluster-scoped).
2. `k8s-admin-admission.yaml` — `ValidatingAdmissionPolicy/dsh-k8s-admin-guard` +
   binding. It admits, for this identity, only Jobs and NetworkPolicies named
   `^dsh-[a-z0-9-]+-acceptance-[a-z0-9]+$`, Jobs whose only Secret references are two named
   keys, and NetworkPolicies whose only egress is TCP 6443 to the three control planes. The
   agent's real acceptance workloads (an "external loop" that needs internet egress and other
   credentials) fail two of the three expressions — the "two denials" it reports. PR #774
   tries to punch a name-based hole through both expressions; reviewer-codex correctly called
   that a bypass. The live policy also carries CEL type-check warnings on every expression
   (each Job expression is type-checked against NetworkPolicy and vice versa).
3. **There is no `kubectl` in the pod at all** (`command -v kubectl` is empty in the live
   container). PR #773 adds one via an init container copying from
   `mirror.gcr.io/bitnami/kubectl:1.31.1`. That exact reference does not resolve: an
   unauthenticated manifest GET on `mirror.gcr.io/v2/bitnami/kubectl/manifests/1.31.1` returns
   **404** while the same request for `library/node:24` and `bitnamilegacy/kubectl:1.31.4`
   returns 200 (checked 2026-09-18; Bitnami moved its catalog to `bitnamilegacy` in 2025-08),
   and no node in the cluster has any `kubectl` image cached (`.status.images` on all seven
   nodes). Merging #773 as-is would therefore fail the image pull on the single-replica,
   Recreate Deployment and take dsh down.
4. What DOES work and stays: the network path. From the live container, `curl --cacert
   /var/run/secrets/kubernetes.io/serviceaccount/ca.crt` with the projected token as a bearer
   (so the serving certificate WAS validated against the projected CA, not `-k`) gets 200 from
   `https://10.96.0.1/version`, `https://kubernetes.default.svc/version`,
   `https://192.168.0.41:6443/version` and the VIP `.40:6443`; a `SelfSubjectReview` POST
   returned `username: system:serviceaccount:dsh:dsh-k8s-admin` (identity proven, not just
   discovery). That is `networkpolicy.yaml` (10.96.0.1/32:443 + CP IPs :443/:6443) plus
   `api-egress-cilium.yaml` (`toEntities: [kube-apiserver]`). Authorization is what changes
   below; the transport does not.

Cluster: Talos, Kubernetes **v1.31.4**, all seven nodes `amd64`, Cilium, Flux reconciling
`kubernetes/apps/apps` from the GitHub mirror with `prune: true`, `wait: true`, `timeout: 5m`.

## Approach

Replace the bounded design with the smallest thing that grants full access.

### 1. RBAC: one ClusterRoleBinding to `cluster-admin`

`k8s-admin.yaml` becomes: the ServiceAccount (unchanged name, so the Deployment's
`serviceAccountName` and the projected token do not change) + a cluster-scoped
`ClusterRoleBinding` `dsh-k8s-admin` → `ClusterRole/cluster-admin`, subject
`ServiceAccount dsh-k8s-admin` with `namespace: dsh` spelled out. The Role and RoleBinding are
deleted (Flux prunes them). No custom ClusterRole: "full access" is exactly the built-in
wildcard ClusterRole `cluster-admin`, and a hand-written wildcard would be the same grant with
more lines to review.

### 2. Admission: delete the guard

`k8s-admin-admission.yaml` is deleted and dropped from `kustomization.yaml`. Flux prunes the
VAP and its binding. Verified live 2026-09-18: all four obsolete objects
(`Role/dsh-k8s-admin`, `RoleBinding/dsh-k8s-admin`, `ValidatingAdmissionPolicy/dsh-k8s-admin-guard`,
`ValidatingAdmissionPolicyBinding/dsh-k8s-admin-guard`) are entries in the `apps`
Kustomization's `status.inventory`, carry BOTH ownership labels
(`kustomize.toolkit.fluxcd.io/name=apps`, `kustomize.toolkit.fluxcd.io/namespace=flux-system`),
and carry no `kustomize.toolkit.fluxcd.io/prune`, `reconcile` or `ssa` annotation — so the
controller's prune step deletes them when they leave the rendered set. Nothing else references
them: `scripts/`, `tests/` and the other VAPs (`agentforge-*-guard`) do not mention
`dsh-k8s-admin-guard`.

RBAC is not admission. The `dsh` namespace keeps `pod-security.kubernetes.io/enforce: baseline`
(see `namespace.yaml`), and the `agentforge-*` guards keep matching what they match today
(their `matchConditions` key on other usernames, or their bindings on other namespaces —
verified live, none matches `system:serviceaccount:dsh:dsh-k8s-admin`). The runbook says so:
cluster-admin makes every API verb *authorized*; it does not make a privileged Pod in `dsh`
*admissible*.

### 3. kubectl in the runtime: download once, cache on the home volume, verify at every Pod initialization

A new init container `install-kubectl` in `deployment.yaml`, LAST in the init sequence (after
`fix-ownership`, which chowns `/dsh-home`, and after `seed-settings`), running the image every
other container in this pod already uses (`mirror.gcr.io/library/node:24` — verified in the
live container: `/usr/bin/curl`, `/usr/bin/sha256sum`, `/usr/bin/install`, `/usr/bin/mv`,
`/usr/bin/chmod`, `/usr/bin/uname` all present; `uname -m` = `x86_64`; `id` = uid 1000 gid 1000).
Its `securityContext` is spelled out like the other uid-1000 containers — `runAsNonRoot: true`,
`runAsUser: 1000`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`,
`capabilities: {drop: [ALL]}` — because sibling settings are not inherited. Mounts: `home`
at `/dsh-home` and the existing `tmp` emptyDir at `/tmp`. Small requests/limits like
`fix-ownership`.

- Target: `/dsh-home/.local/bin/kubectl`. That directory is **already on the container PATH**
  (first entry) and already holds a deployment-owned tool (`git-credential-openbao`), so no
  PATH change and no new volume. The script `mkdir -p`s it itself so a fresh home volume works
  whatever the init order.
- Shape: `command: ["/bin/sh", "-c"]` + a multi-line `args:` program, exactly like the three
  existing init containers, so `scripts/tests/test_dsh_embedded_shell.py` (run by the
  `broker-inventory` workflow's "Script unit tests" step) `sh -n`-parses it like the others.
- Script contract (POSIX `sh`, `set -u`, deliberately NOT `set -e`: every step is guarded so no
  failure can exit non-zero and stop the Recreate rollout; quoted paths throughout; no `eval`,
  nothing sourced from the writable home):
  1. `uname -m` must be `x86_64`, else degrade (only the amd64 checksum is pinned).
  2. Cache hit: `/dsh-home/.local/bin/kubectl` exists AND `sha256sum` of it equals
     `KUBECTL_SHA256` → `chmod 0755` it (matching bytes do not prove the mode; a 0644 file
     would otherwise be accepted and unusable) → log "already present (checksum verified)" →
     exit 0. Normal boot: no network, sub-second.
  3. Otherwise whatever is at the destination is stale or unverified: remove it (and any
     leftover `kubectl.new`) FIRST, so nothing unverified ever stays on PATH while this script
     reports success. Invariant the runbook states: after this init container, the file is
     either the pinned binary or absent.
  4. Download `https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl` to
     `/tmp/kubectl` with `curl -fsSL --proto '=https' --proto-redir '=https' --connect-timeout
     10 --max-time 120` (HTTPS only, including redirects; `-f` turns HTTP errors into failures;
     the image's CA store validates the certificate), verify `sha256sum` against the pin.
     Three attempts, 5 s apart. Worst case is bounded: 3 × (10 + 120) + 2 × 5 = 400 s.
  5. Publish atomically: `install -m 0755 /tmp/kubectl /dsh-home/.local/bin/kubectl.new`
     (same filesystem as the destination) then `mv -f kubectl.new kubectl` (a rename, so a
     reader never sees a truncated executable). Remove `/tmp/kubectl`. Log the installed
     checksum. Exit 0.
  6. Any failure (arch, chmod, rm, mkdir, three downloads, install, mv) → `degrade`: remove
     partial files, print a `WARNING:` block that names the cause, says dsh is booting WITHOUT
     kubectl this roll, that the API is still reachable with curl + the projected token
     (runbook), and that the retry is a pod roll (`kubectl --context admin@ai -n dsh rollout
     restart deploy/dsh`) because a completed init container is not re-run by a container
     restart. Exit 0. Same trade `seed-settings` makes for the plugin closure: a missing
     optional tool must not become a dead harness.
- Pins: `KUBECTL_VERSION=v1.31.4` (matches the server exactly),
  `KUBECTL_SHA256=298e19e9c6c17199011404278f0ff8168a7eca4217edad9097af577023a5620f`
  (from `dl.k8s.io/release/v1.31.4/bin/linux/amd64/kubectl.sha256`, fetched 2026-09-18 and
  confirmed against the ACTUAL bytes: the 56,381,592-byte download hashed to that value
  locally). The pin is static in the manifest; the script never fetches a checksum at
  runtime. A bump edits both values; that changes the pod template, rolls the pod, and the
  mismatch branch re-downloads.
- Egress: init containers run under the pod's NetworkPolicy; `dl.k8s.io` is public 443, which
  `dsh-allow` already permits (`0.0.0.0/0:443` minus private ranges, plus CoreDNS :53).
  Verified FROM THE LIVE POD 2026-09-18 with the exact curl flags above (`-o /dev/null`):
  `http=200 redirects=0 bytes=56381592 time=0.86s`. No redirect, so no second host to allow.
- Scope of the check: this is verification at Pod initialization. uid 1000 (the agent) can
  modify the binary afterwards and a plain container restart does not re-run init containers.
  That is the same trade the deployment already documents for `AGENTS.md`, `.gitconfig` and
  the credential helper; the agent and this init container are the SAME uid, so a symlink or
  swap under `/dsh-home` gains the agent nothing it could not do directly.
  <!-- codex: Publish through a verified temporary file beside the destination followed by an atomic rename; installing directly over kubectl can leave a truncated executable after an interrupted copy or full disk. Moving directly from /tmp is not an atomic rename because tmp and home are different volumes, and existing destination or parent-directory symlinks must not redirect installation into other persistent files. -->

Why not the alternatives:

- PR #773's image copy: the exact reference is a 404 (above). The distroless images
  (`registry.k8s.io/kubectl`; `rancher/kubectl`, itself scratch-based and marked deprecated by
  its project) have no shell, so an init container cannot `cp` out of them; `alpine/k8s` has a
  shell but is 301 MiB compressed for one 56 MB binary; `bitnamilegacy/kubectl` resolves but is
  a frozen catalog.
- Putting kubectl in `install-job.yaml` beside uv: the Job's pod template is immutable, so
  every kubectl change means renaming the Job AND the three `replacements` sources in
  `kustomization.yaml` — more moving parts. (The uv install there downloads an installer
  script to `/tmp`, checks curl's status, and executes it — not a pipe — but with no checksum.)
  The init container pins a checksum and rolls with the Deployment.

### 4. Network: unchanged rules, corrected comments

`networkpolicy.yaml` and `api-egress-cilium.yaml` keep their rules exactly (proven reachable,
above). Three comments become false with cluster-admin and are rewritten: the one calling the
identity "namespace-scoped"; the internet-egress rationale that says this pod "holds strictly
less" than a dev-worker VM; and the claim that the private-range exclusions "protect against a
mistake or a prompt injection pointing INWARD" — with cluster-admin, `kubectl exec`/`proxy`
through the API is an inward path the network rules never see. The rules still stop direct
LAN/pod-network connections; the comment must say only that.

The same correction, as a dated one-paragraph note, goes at the top of
`operator-ssh-networkpolicy.yaml` (the approved-host list bounds the agent's direct SSH path;
the identity can rewrite the policy) and into two comments in `searxng.yaml`: its header still
says dsh has "no internet" / "DNS + LiteLLM only" (stale since 2026-09-09) and line 170 says
searxng "needs no API access. Same reasoning as dsh itself" (inverted by this change). Rules in
all three files are untouched.

### 5. Tell the agent

`agents.seed.md` (installed as `$DSH_HOME/AGENTS.md` every boot) gets a short "Kubernetes
access" section: `kubectl` is on PATH at `/dsh-home/.local/bin/kubectl`; it uses the
in-cluster configuration (the `KUBERNETES_SERVICE_HOST`/`_PORT` variables plus the projected
token and CA under `/var/run/secrets/kubernetes.io/serviceaccount/`), which SURVIVES the
subprocess scrub — verified in the live install: `@deepseek-ai/dsh-subprocess/lib/index.js`
drops only names matching `/KEY|PASSWORD|SECRET|TOKEN/i` and names starting `DSH_`, and
`KUBERNETES_*` matches neither; the identity is `system:serviceaccount:dsh:dsh-k8s-admin`,
bound to `cluster-admin`; do NOT create `~/.kube/config` (a kubeconfig in the persistent home
would override the in-cluster configuration on every later session, and there is none today —
to be confirmed live); and if `kubectl` is missing, that is the documented degraded boot: the
`install-kubectl` init container's log says why and a pod roll retries.

### 6. Docs

- `docs/runbooks/dsh-k8s-admin.md` rewritten: what the identity is now (cluster-admin), what
  that means in this estate (every Secret in the cluster — SOPS-materialised Secrets, OpenBao
  break-glass token, Gitea admin, Cloudflare tunnel credentials — plus Flux itself and `exec`
  into any pod — is readable/usable from a pod that executes model-authored code); RBAC vs
  admission (PSA baseline stays); how to verify (`auth can-i --list --as=…`, `kubectl auth
  whoami` in the pod); the curl fallback for the degraded boot, tested, reading the projected
  token at request time into a shell variable and passing it as a header (never into a
  kubeconfig, a log, `set -x` output or a command-line argument that `ps` would show); how to
  revoke (delete the one ClusterRoleBinding from git, then confirm `auth can-i` flips) and what
  revocation does NOT do (undo bindings, tokens or other access created while cluster-admin
  was held — it is not an incident-recovery procedure); how to bump kubectl; and what the
  history was (#770–#774) so nobody re-derives the bounded design by accident.
- `docs/runbooks/dsh.md`: a short "Kubernetes access" subsection under the topology (identity is
  cluster-admin; kubectl on PATH; link to `dsh-k8s-admin.md`, which today has NO inbound link
  anywhere in the repo); the sentence "Private ranges remain excluded from web egress" qualified
  the same way as section 4; the "Pod stuck in `Init:0/3`" heading and "runs three init
  containers" sentence become `Init:0/4` / four (and `deployment.yaml`'s cross-reference to that
  heading follows); `kubectl` appended to the toolchain probe loop; the "Why `web_fetch` is off"
  paragraph's "which has no egress" corrected to the 2026-09-09 posture.
- `docs/decisions/0025-dsh-cluster-admin.md`, a short ADR: ADR 0021 §5 defines Tier A
  (agent-readable) as never gaining "anything cluster-scoped" and its Consequences record the
  deliberate removal of the dev-workers' cluster-wide escalation helper; this grant is a stated
  EXCEPTION to that tier, by operator decision on 2026-09-18, with the abandoned bounded design
  (#770–#774) as the context and the one-object revocation path. One cross-reference line is
  added to ADR 0021 §5 so a reader of the tier definition finds the exception.
- `deployment.yaml` / `kustomization.yaml` / `networkpolicy.yaml` / `operator-ssh-networkpolicy.yaml`
  / `searxng.yaml` comments updated (section 4).
- `README.md`: the dsh row is REWRITTEN, not appended to — it currently says "Toolchain on
  `node:22`" (every container is `node:24`) and "keeps **no internet egress** — DNS + LiteLLM +
  SearXNG only; `web_fetch` deliberately off" (false since 2026-09-09) — so the row states the
  current posture: public IPv4 HTTP(S) allowed with private ranges excluded, kubectl v1.31.4 on
  PATH, identity cluster-admin, and links the two runbooks. The runbooks index line (README:125)
  gains `dsh-k8s-admin.md`.

### 7. Supersede the open PRs

After this merges AND the post-merge verification below passes: comment on #773 and #774
naming the superseding PR and close them. #769 is already closed.

### Security statement (goes in the PR body verbatim)

This binds ServiceAccount `dsh/dsh-k8s-admin` to the `cluster-admin` ClusterRole. The grant is
identity-wide: every container of the dsh pod (including `relay` and the init containers,
because the token is automounted) and any other workload that runs as that ServiceAccount —
today none of the operator-applied acceptance Jobs do (`automountServiceAccountToken: false`,
no `serviceAccountName`), but any future Job naming it would — can do anything in the cluster
that the API allows. Everything the cluster holds is reachable from a pod that executes
model-authored tool calls. This is the operator's explicit decision ("Provide dsh full access
to k8s … Simplify"). It is a deliberate EXCEPTION to ADR 0021 §5 Tier A, which gives agents
nothing cluster-scoped — the dev-worker VMs have passwordless sudo and unrestricted internet but
only namespace-scoped Kubernetes credentials, and their cluster-wide escalation helper was
removed on purpose; ADR 0025 records the exception. Revoking the grant is one object (delete the
ClusterRoleBinding from git, then confirm `auth can-i` flips); that revokes future use only — it
does not undo anything created while the grant was held.

## Critical files

| Path | Change |
|---|---|
| `kubernetes/apps/apps/dsh/k8s-admin.yaml` | SA kept; Role + RoleBinding → ClusterRoleBinding to `cluster-admin` |
| `kubernetes/apps/apps/dsh/k8s-admin-admission.yaml` | **deleted** |
| `kubernetes/apps/apps/dsh/kustomization.yaml` | drop the admission entry; fix the k8s-admin comment |
| `kubernetes/apps/apps/dsh/deployment.yaml` | new `install-kubectl` init container (+ `home`, `tmp` mounts); fix the automount comment |
| `kubernetes/apps/apps/dsh/networkpolicy.yaml` | comments only (three, section 4) |
| `kubernetes/apps/apps/dsh/operator-ssh-networkpolicy.yaml` | header comment only (section 4) |
| `kubernetes/apps/apps/dsh/searxng.yaml` | two comments only (section 4) |
| `kubernetes/apps/apps/dsh/agents.seed.md` | new "Kubernetes access" section (content-hashed ConfigMap → rolls the pod, which this change needs anyway) |
| `docs/runbooks/dsh-k8s-admin.md` | rewritten |
| `docs/runbooks/dsh.md` | Kubernetes-access subsection + link; egress sentence; `Init:0/4`; probe loop; web_fetch paragraph |
| `docs/decisions/0025-dsh-cluster-admin.md` | new ADR (exception to ADR 0021 Tier A) |
| `docs/decisions/0021-agent-credential-plane-and-helm-testns.md` | one cross-reference line in §5 |
| `README.md` | dsh row rewritten; runbooks index line |

Not touched: `api-egress-cilium.yaml`, `install-job.yaml`, `openbao-eso.yaml`, `namespace.yaml`,
the agent teams, every NetworkPolicy/CiliumNetworkPolicy RULE.

## Verification

Every host-side command below carries `--context admin@ai` (the default context is a
different cluster).

Pre-merge (this branch):

1. `kubectl kustomize kubernetes/apps/apps/dsh` renders; the rendered stream contains exactly
   one `ClusterRoleBinding` (`dsh-k8s-admin` → `cluster-admin`, subject `dsh/dsh-k8s-admin`),
   no `Role`, no `RoleBinding`, no `ValidatingAdmissionPolicy*`.
2. Server-side dry-run of the rendered SA + CRB **as Flux's identity**
   (`--as=system:serviceaccount:flux-system:kustomize-controller`) — proves none of the
   `agentforge-*-guard` policies catches it.
3. Server-side dry-run of the rendered Deployment, AND of a Pod built from its template in
   namespace `dsh` (PodSecurity gates Pods, not Deployments — `namespace.yaml` records exactly
   this distinction).
4. The init script, byte-identical to the manifest, executed INSIDE the live dsh container
   (same image, same `/bin/sh`, uid 1000, read-only rootfs, the pod's real egress) against a
   scratch directory under `/tmp` — never `/dsh-home`. Cases and expected outcomes:
   fresh directory → installed, mode 0755, hash = pin, exit 0; second run → "already present",
   no download, exit 0; correct bytes with mode 0644 → accepted AND repaired to 0755; wrong
   pin → the cached file is removed, three bounded attempts fail the checksum, WARNING block,
   exit 0, nothing left at the destination; stale wrong-content file with the right pin →
   removed, re-downloaded, installed. `kubectl version --client` from the installed binary
   prints v1.31.4. Also `ls -la /dsh-home/.kube` → absent.
5. Local, same commands CI runs: `python -m unittest discover -s scripts/tests -p "test_*.py"`
   (this is the gate that actually inspects the Deployment edit — `test_dsh_embedded_shell.py`
   `sh -n`s the new script, `test_dsh_pod_security.py` checks the pod-level fsGroup policy) and
   `kubectl kustomize` of the dsh path (docker is not available on this workstation, so
   `scripts/manifest-lint.sh`'s kubeconform half runs only in CI). CI: `manifests`,
   `rules-lint`, `broker-inventory` green (the inline-hash and litellm-drift steps read nothing
   this PR touches); both reviewer personas clean.

Post-merge (live):

6. The merge commit is on `github.com/cchifor/ailab` `main` (the mirror), the `flux-system`
   GitRepository's artifact revision names it, and the `apps` Kustomization reports
   `Ready=True` with `lastAppliedRevision` = that sha and `observedGeneration` current. Rollout
   failures show up there (`wait: true`, `timeout: 5m`), so an older Ready is not success.
7. `get validatingadmissionpolicy dsh-k8s-admin-guard` → NotFound;
   `get validatingadmissionpolicybinding dsh-k8s-admin-guard` → NotFound;
   `get role,rolebinding -n dsh dsh-k8s-admin` → NotFound; `get clusterrolebinding dsh-k8s-admin
   -o yaml` shows `roleRef.name: cluster-admin` and the single SA subject in `dsh`.
8. `auth can-i --list --as=system:serviceaccount:dsh:dsh-k8s-admin` shows `*.* [] [] [*]`;
   `auth can-i get secrets -A --as=…` → yes; `create clusterroles --as=…` → yes.
9. Admission, not just authorization: as the SA (`--as=…`), server-side dry-run a Job named
   outside the old regex (e.g. `dsh-native-probe-x`) that references a Secret the old guard
   did not allow, and a NetworkPolicy named likewise with internet egress → both admitted
   (`--dry-run=server`, nothing created). Repeat the same two dry-runs from inside the pod with
   the installed kubectl.
10. New dsh pod Running 2/2 AND `logs <pod> -c install-kubectl` says "installed" (or "already
    present (checksum verified)" on a later roll); in the container: `command -v kubectl` =
    `/dsh-home/.local/bin/kubectl`, `sha256sum` of it = pin, mode 0755, `kubectl version
    --client` = v1.31.4, `kubectl auth whoami` = the SA, `kubectl get nodes` lists 7 nodes,
    `kubectl get validatingadmissionpolicy` lists the agentforge guards (the read it could not
    do before). Running 2/2 alone is NOT the signal — it is also what the degraded boot looks
    like.
11. Through the actual dsh tool shell (a session at https://dsh.chifor.me): `kubectl auth
    whoami` and `kubectl get ns`. The environment analysis in section 5 predicts success; this
    is the operator-facing acceptance and is reported as done only once observed.
12. Close #773 / #774 with the superseding link.

<!-- codex-review-status: finalized -->
