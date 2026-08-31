# ADR 0020 — Dev-worker credentials from OpenBao (per-VM AppRole + agent-rendered files)

**Status:** PROPOSED (2026-08-31). Implementation on `feat/openbao-dev-worker-creds`; the ansible
half ships **opt-in** (`dev_worker_enable_openbao: false`) so the cluster side can land and be
verified before any worker changes. Activation ceremony: `docs/runbooks/openbao-dev-workers.md`.
**Relates to:** ADR 0019 (OpenBao + ESO as the estate secret store — this extends that store to
consumers *outside* the cluster), 0018 (AgentForge v1 — the dev-worker agents that consume these
credentials), 0017 (Gitea master forge — the PAT being replaced is a Gitea PAT), 0007 (k8s exposure —
the LAN-NodePort convention this reuses).

## Context

The six dev-worker VMs (`dev-worker-1..6`, 192.168.0.8–.13 — `docs/runbooks/dev-workers.md`) are
where the interactive Claude Code / Codex agents actually run. Their credential story today is a
single line of Ansible:

- `ansible/roles/dev_worker/tasks/git_forge.yml` writes **one shared `chifor` read/write Gitea PAT**
  (`dev_worker_gitea_token`, SOPS) into `~/.git-credentials` on every worker, for every user in
  `dev_worker_users`. One credential, six VMs, unlimited blast radius — and **rotation means editing
  a SOPS file and re-running the playbook against all six hosts**.
- There is no other channel. Anything else an agent needs (a registry login, a service token) either
  does not exist or gets pasted into a shell — which means it lands in the agent's transcript, in
  `~/.bash_history`, and in whatever the agent decides to write to disk.
- Meanwhile the cluster side already has a real vault: OpenBao 2.5.5 with the `af` KV-v2 mount, ESO
  SecretStores, and the k8s-auth provisioner (ADR 0019). Every in-cluster consumer reads from it.
  The dev workers are the one estate tier still on static, hand-distributed secrets — and they are
  the tier running the *least* trusted code.

Two incidents shape the design:

- **The 768h expiry lockout (2026-08-25).** The operator-provisioner token hit its 768h max TTL.
  OpenBao 2.5.5 compiles out **both** `generate-root` and `operator rekey` (ADR 0019 finding (c);
  `docs/runbooks/openbao-recovery.md`), so an expired privileged token is a *total* lockout with no
  recovery short of the wipe + re-bootstrap ceremony — executed 2026-08-30, which is what left the
  never-expiring `openbao-breakglass-token` Secret behind. Any new long-lived identity that renews
  against a **max** TTL re-arms exactly that failure.
- **The 2026-07-24 SOPS-shape leak** (same runbook) is why the delivery path here is "the operator's
  SOPS file and the worker's disk, and nowhere else" rather than another k8s Secret.

## Decision

Make OpenBao the source of truth for dev-worker credentials, with a **per-VM identity** and
an on-host agent that renders/serves them — so an interactive agent never holds a vault token and
never has to print a secret value in order to use it.

1. **One AppRole per worker, with PERIODIC tokens.** A role and a read-only policy per worker, both
   named after the ansible `inventory_hostname` (`dev-worker-1` … `dev-worker-6`), so an audit entry
   names the machine. **`token_period=72h`, not a max TTL:** a periodic token's TTL resets on every
   renewal and has no expiry cliff, which is the direct antidote to the 2026-08-25 lockout. The
   policy grants `read` on `af/data/dev-workers/{common,<hostname>}` (plus `list`/`read` on the
   matching metadata paths, so the helper can enumerate) and **nothing else** — no write, no delete:
   rotation is a cluster-side operation, never something a compromised worker can perform.

   **No `*_bound_cidrs` — forced, not chosen.** The design intent was to pin each role to its
   worker's `/32`, which would have made a stolen secret-id useless off-box. It does not work through
   this Service: Cilium runs its default SNAT load-balancer mode (`kube-proxy-replacement=true`,
   `bpf-lb-mode` unset — verified against `cilium-config`), so under the default
   `externalTrafficPolicy: Cluster` OpenBao sees the **ingress node**, never 192.168.0.8–.13, and a
   CIDR binding would reject every dev-worker login. `externalTrafficPolicy: Local` does preserve the
   client IP, but with a single `openbao-0` replica only one node has a local endpoint and a dial to
   either of the other two is **dropped, not refused** — so the three-row `/etc/hosts` fanout would
   cost a full TCP timeout per wrong node. Per-worker isolation therefore rests on the per-worker
   policy plus a per-worker, independently revocable secret-id. `prometheus-lan.yaml` defers
   source-IP scoping for the same reason.
2. **LAN exposure via a NodePort, not the tunnel.** Service `openbao-lan` (ns `openbao`, nodePort
   **30820**) mirroring `kubernetes/apps/infrastructure/monitoring/prometheus-lan.yaml`, plus an
   `openbao.lan.chifor.me` SAN on the `openbao-tls` Certificate and three `/etc/hosts` rows
   (192.168.0.41/.42/.43) on each worker. The workers share the flat mgmt LAN with the Talos nodes
   but sit outside the cluster, so the ClusterIP every other consumer uses is not reachable to them.
   The Service selector copies the chart's `openbao-active` selector, so while the vault is **sealed**
   that label is absent, the Service has no endpoints, and the LAN sees nothing rather than a 503.
3. **A root-owned `bao agent` per worker, and a `cred` helper for everything else.** AppRole
   auto-auth from `/etc/openbao-agent/{role-id,secret-id}` → a periodic token in a group-readable
   sink at `/run/openbao-agent/token` (tmpfs, systemd `RuntimeDirectory=`). The agent renders
   file-shaped credentials directly (`~/.git-credentials` from `af/dev-workers/common`), and
   `/usr/local/bin/cred` — usable by members of the `openbao-agent` group — covers ad-hoc reads.
   `cred exec <name> <field> <ENV_VAR> -- <cmd>` injects a secret into a child process environment,
   so the preferred usage never materialises a value in the terminal at all. A managed CLAUDE.md
   block tells the agents this, in those words.
4. **The KV subtree is seed-authoritative.** `af/dev-workers/common` and
   `af/dev-workers/<hostname>`, seed-patched from the SOPS `devworker-seeds.sops.yaml` on every
   provision run — the same contract as `operator-seeds.sops.yaml`: keys present in the seed **win**
   over live KV, keys absent from it survive untouched. This is what makes the subtree self-heal
   after a wipe, and it is why rotating a seeded value only in the vault gets reverted on the next
   daily run.
5. **Provisioning is a declarative daily Job, not a ceremony.** `openbao-devworker-provision`
   (the official `quay.io/openbao/openbao` image, `ttlSecondsAfterFinished: 86400` so Flux re-runs it daily)
   enables the `approle` mount, upserts the six policies/roles, and seed-patches the KV. It
   authenticates with the never-expiring `openbao-breakglass-token` Secret, because enabling an auth
   mount and writing policies are root operations that the scoped provisioner token deliberately
   cannot perform. The secret reference is `optional: false`: the Job goes red if that Secret is ever
   absent, rather than silently skipping.
6. **Secret-id issuance stays an operator ceremony.** The Job mints no login credentials. Role-ids
   and secret-ids are read out by hand into `ansible/secrets/dev-worker.sops.yaml`
   (`dev_worker_openbao_credentials`, a per-host map), so the only two places a worker's login
   credential ever exists are that SOPS file and `/etc/openbao-agent` on the worker itself — never a
   k8s Secret, never a CI variable.

## Rejected / out of scope

- **Cloudflare tunnel exposure (`openbao.chifor.me`) instead of the LAN NodePort.** It would route
  vault traffic through a third party for a hop that never leaves the LAN, and the hostname is
  **Access-gated with a 30m browser session** — a `bao agent` cannot pass an interactive Access
  login, so this path would additionally require Access service tokens distributed to all six
  workers: a second credential to solve the credential problem.
  *Correction of record:* the plan behind this ADR recorded a suspected config/state **drift** for
  `openbao.chifor.me` (a CNAME + Zero Trust Access app allegedly live in state but absent from the
  config). Re-verified 2026-08-31 across the four artefacts and **there is no drift** — the route is
  fully codified: `kubernetes/infra/cloudflare/variables.tf` (`"openbao"` in the `tunnel_hostnames`
  default, with the rationale in that variable's own description),
  `kubernetes/infra/cloudflare/access.tf` (`cloudflare_zero_trust_access_application.openbao`, 30m
  session, `allow_me` — and `dns.tf`'s `depends_on` makes the Access app exist before the CNAME
  resolves), and `kubernetes/apps/apps/edge/cloudflared.yaml` (the `openbao.chifor.me` ingress rule
  to `https://openbao-ui.openbao.svc.cluster.local:8200` with a pinned `originServerName` + CA pool).
  The local `kubernetes/infra/cloudflare/terraform.tfstate` (gitignored; lives in the main checkout)
  holds exactly those two objects — `cloudflare_dns_record.tunnel["openbao"]` and
  `cloudflare_zero_trust_access_application.openbao` — and nothing extra. So the rejection above
  stands on the merits, not on drift, and **no drift issue should be opened**.
- **Wrapped / one-use secret-ids** (`secret_id_num_uses=1`, response-wrapped delivery). Rejected
  because nothing re-delivers one on reboot: the VM comes back, the agent has no login credential,
  and the worker is dead until an operator re-mints. The durable secret-id file is the deliberate
  trade — bounded by a 0600 root-owned file, a read-only policy behind it, and per-worker revocation.
- **Distributing secret-ids as k8s Secrets** (ESO → a Secret → some pull mechanism). Rejected: it
  widens the audience from "the operator's SOPS file" to "anything with Secret read in that
  namespace", in order to deliver a credential to a consumer that is not in the cluster at all.
- **Cert auth (`auth/cert`) instead of AppRole.** There is no client-certificate PKI for the VMs;
  standing one up (issuance, renewal, revocation for six hosts) is strictly more machinery than a
  role-id/secret-id pair, for the same property. (It would, however, give back the machine-binding
  the SNAT path costs us — see the source-IP follow-up.)
- **Extending the agentforge provisioner to own these roles.** The provisioner is the *tenant/broker*
  write path, owned by `cchifor/agentforge`'s `bootstrap.py`. Dev-worker VMs are an estate concern
  (they exist in this repo's tofu + ansible), so their provisioning stays estate-side, in this repo,
  where the worker list sits next to the inventory it mirrors.
- **Per-worker Gitea PATs.** Deliberately out of scope: the first seeded field is the *existing*
  shared `chifor` PAT, so this change moves the distribution channel without also changing the
  credential. Splitting the PAT is a follow-up (below).
- **An ingress CiliumNetworkPolicy on the OpenBao pod** restricting who may reach :8200. Not in this
  slice; see follow-ups.

## Consequences

- **The full OpenBao API becomes reachable from the mgmt LAN.** A NodePort cannot path-scope, so
  `https://<node-ip>:30820` exposes everything `openbao.openbao.svc:8200` exposes. Accepted on the
  same terms as `prometheus-lan` (that manifest's header): the mgmt LAN is single-operator and
  private, node IPs have no WAN route, and every request still needs TLS **plus** a valid token —
  while the dev-worker AppRoles carry read-only policies scoped to their own KV subtree. If LAN trust
  ever changes, the fix is an authenticating proxy in front and a repointed Service selector, not a
  redesign of the worker side (which only knows a URL).
- **Sandbox pods cannot reach the NodePort — verified, and it is an explicit deny, not merely an
  omission.** The AgentForge sandbox is where genuinely untrusted agent code runs, so a new node-IP
  service is exactly the kind of change that could quietly widen it. Checked 2026-08-31 against
  `kubernetes/apps/infrastructure/agentforge-sandbox/` and the live cluster
  (`kubectl --context admin@ai -n agentforge-sandbox get cnp <name> -o jsonpath='{.spec.egressDeny}'`
  for each of the three sandbox CNPs; all three are live and match git):
  - `networkpolicy.yaml` — a namespace-wide `default-deny-all` NetworkPolicy (both policyTypes, no
    rules) is the baseline: nothing egresses unless a CNP union-adds it.
  - `sandbox-test-zero-egress` (trust-class=**test**) — `egressDeny: toEntities: [all]`.
  - `sandbox-agent-egress` (trust-class=**agent**) — allows *only* kube-dns (L7-locked to three
    broker FQDNs) and `component=broker` pods on 8700, and denies
    `toEntities: [world, host, remote-node]` plus `169.254.169.254/32`, `169.254.0.0/16`, `::/0`.
  - `sandbox-prepare-egress` (trust-class=**prepare**) — allows only `component=pkgcache` pods on
    4873/3141, with the identical deny belt.
  A node IP (192.168.0.41/.42/.43) carries the Cilium `host` identity from the local node and
  `remote-node` from the other two; both appear in the `egressDeny` of the two classes that have any
  egress at all, and Cilium evaluates `egressDeny` with precedence over every allow. So
  `openbao-lan:30820` is unreachable from all three sandbox trust classes — by explicit deny layered
  on default-deny. **No policy change is required by this ADR, and none was made.**
  Residual: the boundary canary matrix
  (`kubernetes/apps/infrastructure/agentforge-sandbox/egress-canary.yaml`,
  `docs/runbooks/sandbox-boundary-canary.md`) probes OpenBao only at its **ClusterIP**
  (`openbao.openbao.svc.cluster.local:8200`) and has no node-IP:NodePort probe — so this property is
  *enforced* but not yet *asserted* by the suite. See follow-ups.
- **A durable secret-id lives on each worker's disk**, 0600 root:root, behind a read-only policy. A
  root compromise of a worker yields that worker's read-only subtree — which, until per-worker PATs
  land, contains the same shared Gitea PAT the worker already had sitting in `~/.git-credentials`.
  So this change does not *widen* the worker-compromise blast radius; it makes that radius explicit,
  enumerable, and independently revocable.
  **Residual (the cost of no CIDR binding):** a secret-id lifted off a worker is usable from anywhere
  that can reach `:30820` — i.e. anywhere on the mgmt LAN — not only from that worker. It still buys
  only that one worker's read-only subtree, and revoking it is a single accessor destroy, but it
  means a secret-id must be treated as a live credential rather than a machine-bound one: rotate it
  when a worker is rebuilt, and destroy the old accessor rather than leaving it valid.
- **The breakglass root token gains a daily consumer.** `openbao-devworker-provision` mounts it once
  a day. That is a real increase in the token's exposure surface (it was operator-only before),
  accepted because the alternative is a hand-run ceremony that drifts. It also makes the Secret
  load-bearing: losing it stops this subtree converging, so `docs/runbooks/openbao-recovery.md` now
  lists it as a dependency.
- **`git_forge.yml` yields ownership of `~/.git-credentials`** when `dev_worker_enable_openbao` is
  on — the file becomes agent-rendered from `af/dev-workers/common`. Two writers of one path is a
  silent-flapping bug, so exactly one of the two paths runs.
- **A new operator ceremony exists** (six secret-id mints) and it does **not** survive an OpenBao
  wipe: the roles and the KV re-materialise from git, but every secret-id is invalidated. Recorded in
  both runbooks.
- **Rotation gains a rule to remember:** a seeded KV value must be rotated in the vault **and** in
  `devworker-seeds.sops.yaml` in the same change, or the daily Job reverts it.

## Follow-ups

- **Per-worker Gitea bot PATs** replacing the shared `chifor` PAT in `af/dev-workers/common` — the
  point of the per-worker subtree, and the change that makes a worker compromise actually contained.
- **Recover source-IP scoping**, which the SNAT path took away (Decision §1). An ingress CNP on the
  OpenBao pod cannot help while the client IP is rewritten to the ingress node; the options are
  `externalTrafficPolicy: Local` once OpenBao runs more than one replica (so every node has a local
  endpoint and the `/etc/hosts` fanout stops timing out), or an authenticating/source-scoping proxy
  in front of :30820. Either would restore `secret_id_bound_cidrs` as a real control.
- **Add a node-IP NodePort probe to the sandbox boundary canary** (`192.168.0.41:30820` as a deny
  probe alongside the existing ClusterIP OpenBao probe), so the property argued above is asserted by
  the suite rather than by reading policy.
- **Alerting on the daily Job** — a silently failing `openbao-devworker-provision` means the subtree
  stops converging with nothing going red on the workers.
