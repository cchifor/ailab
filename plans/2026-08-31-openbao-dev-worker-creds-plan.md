# OpenBao-backed credentials for dev-worker agents — Implementation Plan

> **For agentic workers:** implement task-by-task; each task lists its exact files and content.
> Steps use checkbox (`- [ ]`) syntax for tracking.

> **Post-implementation amendments (2026-08-31).** Four premises below were corrected during
> implementation — where this plan and the shipped code disagree, the code and ADR 0020 are right:
> 1. Role/policy names are `dev-worker-1..6` (the bare `inventory_hostname`), NOT
>    `dev-worker-<inventory_hostname>` — the template stutters to `dev-worker-dev-worker-1`.
> 2. `token_bound_cidrs`/`secret_id_bound_cidrs` were DROPPED: Cilium runs default SNAT LB mode
>    (verified against cilium-config), so through the NodePort OpenBao sees the ingress node, never
>    the worker's IP — a /32 binding would reject every login. ADR 0020 documents the weakened
>    threat model and the follow-ups that could restore source-IP scoping.
> 3. The breakglass Secret's data key is `root_token`, not `token`.
> 4. The "Cloudflare drift" premise (openbao.chifor.me in tfstate but not config) is FALSE — all
>    four artefacts are codified and consistent; no drift issue was opened. The Cloudflare
>    exposure path remains rejected on the merits (third-party hop; Access's browser gate cannot
>    be traversed by a headless `bao agent`).

**Goal:** Make OpenBao the source of truth for dev-worker credentials: expose OpenBao to the mgmt
LAN, give each dev-worker VM its own AppRole identity with an auto-renewing `bao agent`, deliver
secrets to interactive Claude/Codex agents through a `cred` helper + agent-rendered files, and
document the whole ceremony in ailab docs.

**Architecture:** Three additions. (1) Cluster side: a `openbao-lan` NodePort Service + a LAN SAN on
the `openbao-tls` Certificate + an idempotent daily `openbao-devworker-provision` Job (official
`openbao/openbao` image, breakglass-token-authenticated) that enables AppRole, writes per-worker
policies/roles and seed-patches the `af/dev-workers/*` KV paths from a committed SOPS seeds Secret.
(2) Worker side (ansible `dev_worker` role, new opt-in `openbao.yml` task file): pinned `bao` CLI,
ailab-root-ca trust, `/etc/hosts` entries, an `openbao-agent` systemd service (AppRole auto-auth →
periodic token, sink + template rendering of `~/.git-credentials`), a `/usr/local/bin/cred` helper,
and a managed CLAUDE.md block instructing agents to use it. (3) Docs: ADR 0020, a new runbook with
the activation + per-worker secret-id mint ceremony, and updates to the dev-workers + openbao
recovery runbooks.

**Tech stack:** Flux/kustomize manifests, OpenBao 2.5.5 (AppRole, KV-v2 patch, periodic tokens),
bao agent (auto-auth/sink/template), Ansible (SOPS-age secrets), Gitea PR flow.

**Spec:** the conversation design (2026-08-31): AppRole per worker with CIDR binding + periodic
tokens (768h-expiry antidote), persistent secret-id files (reboot-safe; wrapped/one-use delivery
rejected because nothing re-delivers on boot), LAN NodePort not Cloudflare, seeds-authoritative KV
per estate convention, agents never hold the vault token in context and never print values.

## Global constraints

- **No AI attribution anywhere** — no `Co-Authored-By`, no "Generated with" footers in commits, PR
  bodies, code comments, or docs. (Estate standing rule; PRs carrying them get held.)
- **Never print or log secret values.** Key NAMES/paths/lengths only. `no_log: true` on any ansible
  task touching a secret value. The devworker-provision script must never `echo` a token.
- Comment style: match the repo — block comments explain WHY and cite incidents/ADRs, not what the
  next line does. Do not write universal-quantifier comments ("every X…") without counting.
- All new k8s manifests join `kubernetes/apps/infrastructure/security/openbao/kustomization.yaml`
  and must `kustomize build` cleanly (run `kustomize build kubernetes/apps/infrastructure/security/openbao`
  — SOPS files are plain YAML with encrypted values, they build fine).
- Ansible must pass `ansible-lint` no worse than the existing baseline (repo runs `just lint` with
  `|| true`, but do not introduce NEW lint errors); shell scripts must pass `shellcheck`.
- Shared names (interfaces between tasks) — use EXACTLY these:
  - Service `openbao-lan`, namespace `openbao`, port 8200, **nodePort 30820**
  - LAN DNS name **`openbao.lan.chifor.me`** → node IPs 192.168.0.41/.42/.43 (via /etc/hosts)
  - AppRole mount path `approle`; roles + policies `dev-worker-<inventory_hostname>` for
    dev-worker-1..6 (IPs 192.168.0.8–.13, vmids 4201–4206)
  - KV paths `af/dev-workers/common` and `af/dev-workers/<inventory_hostname>`; first seeded field:
    `gitea_pat`
  - Job `openbao-devworker-provision`; ConfigMap `openbao-devworker-provision-script`; seeds Secret
    `openbao-devworker-seeds` (file `devworker-seeds.sops.yaml`)
  - Worker files: `/etc/openbao-agent/agent.hcl`, `/etc/openbao-agent/role-id`,
    `/etc/openbao-agent/secret-id`, sink `/run/openbao-agent/token`, system user+group
    `openbao-agent`, helper `/usr/local/bin/cred`
  - Ansible: task file `openbao.yml`, toggle `dev_worker_enable_openbao` (default **false**), vars
    `dev_worker_openbao_addr`, `dev_worker_openbao_version`, `dev_worker_openbao_sha256`,
    `dev_worker_openbao_credentials` (per-host map, SOPS)
  - ADR `docs/decisions/0020-dev-worker-openbao-credentials.md`; runbook
    `docs/runbooks/openbao-dev-workers.md`
- Branch `feat/openbao-dev-worker-creds`; commits `feat(openbao): …` / `feat(dev-worker): …` /
  `docs(openbao): …`; push to `origin` (this scratchpad clone's origin IS git.chifor.me).
- Implementer agents do NOT run git commands; the orchestrator commits with explicit paths.

---

### Task A: Kubernetes — LAN exposure + devworker provision Job

**Files:**
- Create: `kubernetes/apps/infrastructure/security/openbao/openbao-lan.yaml`
- Create: `kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml`
- Create: `kubernetes/apps/infrastructure/security/openbao/devworker-seeds.sops.yaml` (placeholder,
  see step 3 — real encryption happens in the operator ceremony or by the orchestrator if the age
  key is available locally)
- Modify: `kubernetes/apps/infrastructure/security/openbao/tls.yaml` (add one SAN)
- Modify: `kubernetes/apps/infrastructure/security/openbao/kustomization.yaml` (3 new resources)

- [ ] **Step A1: `openbao-lan.yaml`** — mirror the `prometheus-lan.yaml` pattern (LAN-only NodePort,
  deliberate-trade-off comment). Selector copies the chart's own `openbao-active` Service selector so
  a sealed pod (label absent) has no endpoints:

```yaml
# LAN-only NodePort in front of the OpenBao API — the dev-worker VMs' path to their credentials
# (ADR 0020). The dev workers (192.168.0.8-.13) sit on the flat mgmt LAN outside the cluster; their
# per-host `bao agent` (AppRole auto-auth) and the `cred` helper dial this Service at
# https://openbao.lan.chifor.me:30820 (/etc/hosts -> .41/.42/.43; the name is a SAN on openbao-tls).
#
# Exposure trade-off (deliberate, same shape as monitoring/prometheus-lan.yaml): a NodePort cannot
# path-scope, so the full OpenBao API is reachable from the LAN. Accepted because (a) the mgmt LAN
# is single-operator/private and node IPs have no WAN route; (b) every request still needs TLS +
# a valid token, and the dev-worker AppRole logins are CIDR-bound per /32 with read-only policies;
# (c) the LAN alternative (Cloudflare tunnel) would route vault traffic through a third party —
# rejected in ADR 0020. If LAN trust ever changes, put an authenticating proxy in front and repoint.
#
# Selector matches the chart's openbao-active Service (label managed by the server at runtime), so
# while the vault is SEALED the label is absent and this Service has NO endpoints — a sealed vault
# is unreachable from the LAN rather than answering 503s.
apiVersion: v1
kind: Service
metadata:
  name: openbao-lan
  namespace: openbao
  labels:
    app.kubernetes.io/name: openbao-lan
    app.kubernetes.io/part-of: agentforge
spec:
  type: NodePort
  selector:
    app.kubernetes.io/instance: openbao
    app.kubernetes.io/name: openbao
    component: server
    openbao-active: "true"
  ports:
    - name: https-api
      port: 8200
      targetPort: 8200
      nodePort: 30820
      protocol: TCP
```

- [ ] **Step A2: `tls.yaml`** — add `openbao.lan.chifor.me` to `dnsNames` (after the
  `openbao-active…` entry) and extend the SAN comment block with one line explaining it is the
  LAN NodePort name for dev workers (ADR 0020) and that cert-manager re-issues but the server only
  re-reads on pod restart (activation runbook step).

- [ ] **Step A3: `devworker-seeds.sops.yaml`** — a SOPS-encrypted Secret carrying seed JSON for the
  dev-worker KV paths (same durability convention as `operator-seeds.sops.yaml`: **the seed value
  wins over live KV on every provision run** — rotate = update this file too). The repo `.sops.yaml`
  generic rule already encrypts `data|stringData` for `*.sops.yaml` under kubernetes/. Author it
  UNENCRYPTED locally then encrypt in place with
  `sops encrypt -i kubernetes/apps/infrastructure/security/openbao/devworker-seeds.sops.yaml`
  (the orchestrator does this; if the age private key is not available locally, ship the file with a
  placeholder PAT and the runbook ceremony fills it). Plaintext form:

```yaml
# Seed values for the dev-worker KV subtree (af/dev-workers/*) — mounted into
# openbao-devworker-provision (see devworker-provision-job.yaml). SAME CONTRACT as
# operator-seeds.sops.yaml: the Job `bao kv patch`es each path with this JSON on every run, so
# (a) keys absent from the seed survive untouched, (b) keys PRESENT here overwrite live KV — the
# seed is authoritative, and rotating a seeded credential only in the vault gets silently reverted
# on the next daily run. Rotate = update KV AND re-encrypt this file in the same change.
# After an OpenBao wipe (docs/runbooks/openbao-recovery.md) this is what restores the subtree.
#
# gitea_pat = the shared `chifor` read/write forge PAT the workers use today
# (ansible dev_worker_gitea_token; git_forge.yml). Per-worker PATs are the planned follow-up.
apiVersion: v1
kind: Secret
metadata:
  name: openbao-devworker-seeds
  namespace: openbao
  labels:
    app.kubernetes.io/name: openbao-devworker-seeds
    app.kubernetes.io/component: bootstrap
    app.kubernetes.io/part-of: agentforge
type: Opaque
stringData:
  common.json: |
    {"gitea_pat": "REPLACE-WITH-dev_worker_gitea_token"}
```

- [ ] **Step A4: `devworker-provision-job.yaml`** — ConfigMap (inline script) + Job. Pattern-match
  `provision-job.yaml`: force annotation, `ttlSecondsAfterFinished: 86400` (daily re-run =
  convergence + seed re-assert), fail-closed, non-root. Uses the official
  `openbao/openbao:2.5.5` image (`bao` CLI + busybox sh). Auth = the live
  `openbao-breakglass-token` Secret (exists since the 2026-08-30 re-bootstrap; NOT in git —
  documented in the runbook; `optional: false` so the Job fails loudly if it is ever missing).
  Content:

```yaml
# Dev-worker AppRole provisioning (ADR 0020) — the declarative half of giving each dev-worker VM its
# own OpenBao identity. Enables the `approle` auth mount, writes one read-only policy + one AppRole
# role per worker (CIDR-bound to the worker's /32, PERIODIC tokens so a renewing agent never hits
# the 768h max-TTL cliff that locked the estate out on 2026-08-25), and seed-patches the
# af/dev-workers/* KV paths from openbao-devworker-seeds (seed-wins contract, see that file).
#
# AUTH: the openbao-breakglass-token Secret (never-expiring root, minted during the 2026-08-30
# re-bootstrap; deliberately NOT in git — see docs/runbooks/openbao-recovery.md). Root is required
# because the original provision Job revoked its root token and scoped tokens cannot enable auth
# mounts or write policies. optional:false -> if the breakglass Secret is ever absent the Job goes
# red instead of silently skipping (fail-closed, like every openbao bootstrap Job here).
#
# What this Job does NOT do: mint per-worker secret-ids. Issuance is an operator ceremony
# (docs/runbooks/openbao-dev-workers.md) so the only place a login credential ever exists is the
# operator's SOPS file + the worker's /etc/openbao-agent — never a k8s Secret.
apiVersion: v1
kind: ConfigMap
metadata:
  name: openbao-devworker-provision-script
  namespace: openbao
  labels:
    app.kubernetes.io/name: openbao-devworker-provision
    app.kubernetes.io/component: bootstrap
    app.kubernetes.io/part-of: agentforge
data:
  provision.sh: |
    #!/bin/sh
    # Idempotent: every write is an upsert; safe to re-run daily. Fail-closed: set -eu, no || true.
    # NEVER echo token material — BAO_TOKEN comes in via env from the mounted Secret.
    set -eu

    WORKERS="dev-worker-1:192.168.0.8 dev-worker-2:192.168.0.9 dev-worker-3:192.168.0.10 \
    dev-worker-4:192.168.0.11 dev-worker-5:192.168.0.12 dev-worker-6:192.168.0.13"

    bao token lookup >/dev/null || { echo "breakglass token invalid; aborting" >&2; exit 1; }

    # Auth mount: enable once; `bao auth enable` errors if present, so probe the list first.
    if ! bao auth list -format=json | grep -q '"approle/"'; then
      bao auth enable approle
    fi

    for entry in $WORKERS; do
      host="${entry%%:*}"; ip="${entry##*:}"
      # Read-only per-worker policy: its own subtree + the shared one. List on metadata so the
      # `cred list` helper works. No delete, no write — rotation happens cluster-side.
      bao policy write "dev-worker-${host}" - <<EOF
    path "af/data/dev-workers/common"        { capabilities = ["read"] }
    path "af/data/dev-workers/${host}"       { capabilities = ["read"] }
    path "af/data/dev-workers/common/*"      { capabilities = ["read"] }
    path "af/data/dev-workers/${host}/*"     { capabilities = ["read"] }
    path "af/metadata/dev-workers"           { capabilities = ["list"] }
    path "af/metadata/dev-workers/*"         { capabilities = ["read", "list"] }
    EOF
      # PERIODIC tokens (token_period): TTL resets to 72h on every renewal, no max-TTL cliff — the
      # antidote to the 2026-08-25 768h expiry lockout. secret_id_ttl=0 + num_uses=0: the secret-id
      # is a durable per-VM bootstrap credential (reboot-safe), useless off-box (CIDR-bound /32).
      bao write "auth/approle/role/dev-worker-${host}" \
        token_policies="dev-worker-${host}" \
        token_period=72h \
        token_bound_cidrs="${ip}/32" \
        secret_id_bound_cidrs="${ip}/32" \
        secret_id_ttl=0 \
        secret_id_num_uses=0
    done

    # Seed-patch the shared subtree (kv patch = read-modify-write: seed keys win, others survive —
    # the operator-seeds contract). Initialise the path first if it has never been written, since
    # `kv patch` requires an existing version.
    if ! bao kv get -mount=af dev-workers/common >/dev/null 2>&1; then
      bao kv put -mount=af dev-workers/common placeholder=init
    fi
    bao kv patch -mount=af dev-workers/common @/etc/openbao-devworker-seeds/common.json
    echo "devworker provision complete"
---
apiVersion: batch/v1
kind: Job
metadata:
  name: openbao-devworker-provision
  namespace: openbao
  annotations:
    # WEDGE GUARD — same rationale as provision-job.yaml (2026-07-28 immutable-Job outage): scope
    # delete+recreate to THIS Job so a template change never wedges the openbao Kustomization.
    kustomize.toolkit.fluxcd.io/force: "enabled"
  labels:
    app.kubernetes.io/name: openbao-devworker-provision
    app.kubernetes.io/component: bootstrap
    app.kubernetes.io/part-of: agentforge
spec:
  backoffLimit: 6
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels:
        app.kubernetes.io/name: openbao-devworker-provision
        app.kubernetes.io/component: bootstrap
        app.kubernetes.io/part-of: agentforge
    spec:
      restartPolicy: OnFailure
      enableServiceLinks: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 100
        runAsGroup: 1000
        seccompProfile: { type: RuntimeDefault }
      containers:
        - name: provision
          # The official OpenBao image (bao CLI + sh) — small, and version-locked to the server.
          image: openbao/openbao:2.5.5
          command: ["/bin/sh", "/scripts/provision.sh"]
          env:
            - { name: BAO_ADDR, value: "https://openbao-0.openbao-internal.openbao.svc.cluster.local:8200" }
            - { name: BAO_CACERT, value: "/tls/ca.crt" }
            - name: BAO_TOKEN
              valueFrom:
                secretKeyRef:
                  name: openbao-breakglass-token
                  key: token
                  optional: false
          volumeMounts:
            - { name: tls, mountPath: /tls, readOnly: true }
            - { name: script, mountPath: /scripts, readOnly: true }
            - { name: seeds, mountPath: /etc/openbao-devworker-seeds, readOnly: true }
          resources:
            requests: { cpu: 10m, memory: 32Mi }
            limits: { cpu: 250m, memory: 128Mi }
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: { drop: ["ALL"] }
            readOnlyRootFilesystem: true
            seccompProfile: { type: RuntimeDefault }
      volumes:
        - name: tls
          secret:
            secretName: openbao-tls
            items:
              - { key: ca.crt, path: ca.crt }
        - name: script
          configMap:
            name: openbao-devworker-provision-script
            defaultMode: 0555
        - name: seeds
          secret:
            secretName: openbao-devworker-seeds
```

  **Verify before finalizing:** the breakglass Secret's data KEY name — run
  `kubectl --context admin@ai -n openbao get secret openbao-breakglass-token -o jsonpath='{.data}' | python -c "import sys,json;print(list(json.load(sys.stdin).keys()))"`
  (prints key NAMES only) and adjust `secretKeyRef.key` if it is not `token`. Also confirm the
  `openbao/openbao:2.5.5` image tag exists (`docker manifest inspect` or the GH releases page) and
  note its user (the image runs as user `openbao` uid 100 — if `runAsUser: 100` conflicts with the
  image, drop to the image default but keep runAsNonRoot).

- [ ] **Step A5: kustomization.yaml** — add, after the `provisioner-deploy.yaml` line:

```yaml
  - openbao-lan.yaml # LAN NodePort :30820 for the dev-worker bao agents (ADR 0020)
  - devworker-seeds.sops.yaml # SOPS seed values for af/dev-workers/* (seed-wins contract)
  - devworker-provision-job.yaml # approle + per-worker policies/roles + KV seed (breakglass-auth)
```

- [ ] **Step A6: validate** — `kustomize build kubernetes/apps/infrastructure/security/openbao`
  must succeed; extract the ConfigMap script to a temp file and `shellcheck -s sh` it (busybox sh —
  no bashisms).

### Task B: Ansible — dev_worker role OpenBao integration

**Files:**
- Create: `ansible/roles/dev_worker/tasks/openbao.yml`
- Create: `ansible/roles/dev_worker/templates/openbao-agent.hcl.j2`
- Create: `ansible/roles/dev_worker/templates/openbao-agent.service.j2`
- Create: `ansible/roles/dev_worker/templates/git-credentials.ctmpl.j2`
- Create: `ansible/roles/dev_worker/files/cred` (helper script)
- Create: `ansible/roles/dev_worker/files/ailab-root-ca.crt` (public CA cert — extract with
  `kubectl --context admin@ai -n openbao get secret openbao-tls -o jsonpath='{.data.ca\.crt}' | base64 -d`;
  it is the CN=ailab-root-ca public certificate, safe to commit; precedent: versitygw-ca.yaml)
- Create: `ansible/roles/dev_worker/tests/test-cred-helper.sh`
- Modify: `ansible/roles/dev_worker/defaults/main.yml` (new vars block)
- Modify: `ansible/roles/dev_worker/tasks/main.yml` (import after `git_forge.yml`)
- Modify: `ansible/roles/dev_worker/tasks/git_forge.yml` (skip when openbao owns the file)
- Modify: `.sops.yaml` (extend dev-worker encrypted_regex)
- Modify: `ansible/secrets/dev-worker.sops.yaml.example` (document the new keys)
- Modify: `ansible/group_vars/dev_workers.yml` (commented toggle line)

- [ ] **Step B1: defaults** — append to `defaults/main.yml` (before the secrets block), following
  the existing comment idiom:

```yaml
# ---- OpenBao credential plumbing (ADR 0020; opt-in until the cluster side is active) ----
# Per-worker AppRole identity: a root-owned bao agent keeps a PERIODIC token renewed (the 768h
# max-TTL expiry that locked the estate out on 2026-08-25 cannot hit a periodic token) and renders
# credential files; interactive agents fetch ad-hoc secrets via /usr/local/bin/cred, which reads
# the group-readable sink token. Activation ceremony: docs/runbooks/openbao-dev-workers.md.
dev_worker_enable_openbao: false
dev_worker_openbao_addr: "https://openbao.lan.chifor.me:30820" # openbao-lan NodePort (SAN on openbao-tls)
# /etc/hosts rows for the LAN name — the workers resolve via public DNS (cloud-init), so the
# cluster-only name is pinned host-side. All three node IPs: Go clients dial them in order, so a
# drained node is a retry, not an outage.
dev_worker_openbao_hosts_entries:
  - "192.168.0.41 openbao.lan.chifor.me"
  - "192.168.0.42 openbao.lan.chifor.me"
  - "192.168.0.43 openbao.lan.chifor.me"
# Pinned like herdr: upstream publishes SHA256SUMS per release; recompute on bump.
dev_worker_openbao_version: "2.5.5"
dev_worker_openbao_sha256: "REPLACE-AT-IMPL-TIME"
```

  and to the SECRETS block at the bottom:

```yaml
# openbao AppRole login (per-host map keyed by inventory_hostname; minted by the ceremony in
# docs/runbooks/openbao-dev-workers.md and stored ONLY in the local SOPS file + the worker).
# Shape: { dev-worker-1: { role_id: "...", secret_id: "..." }, ... }
dev_worker_openbao_credentials: {}
```

- [ ] **Step B2: `tasks/openbao.yml`** — the full task file. Requirements it must implement
  (write real tasks for each; every secret-touching task gets `no_log: true`):
  1. `ansible.builtin.assert` that `dev_worker_openbao_credentials[inventory_hostname].role_id` and
     `.secret_id` are non-empty and `dev_worker_openbao_sha256` is not the REPLACE placeholder.
  2. Install the ailab root CA: copy `files/ailab-root-ca.crt` to
     `/usr/local/share/ca-certificates/ailab-root-ca.crt` (0644), notify/run `update-ca-certificates`
     when changed (use a `register` + `when: changed` command, role has no handlers dir — check; if
     other tasks use handlers, use a handler).
  3. `/etc/hosts` rows via `ansible.builtin.blockinfile` (marker comment
     `# {mark} ANSIBLE MANAGED - openbao-lan`) with the three entries.
  4. Download + install the pinned bao CLI:
     `https://github.com/openbao/openbao/releases/download/v{{ dev_worker_openbao_version }}/bao_{{ dev_worker_openbao_version }}_linux_amd64.deb`
     — **verify the exact asset name against the release page at impl time** (fall back to the
     `.tar.gz`+unarchive pattern like k9s if the deb name differs), `checksum: "sha256:{{ dev_worker_openbao_sha256 }}"`,
     then `apt: deb=...` (or unarchive to /usr/local/bin with a version marker file, matching helm/k9s).
  5. System user+group `openbao-agent` (system: true, home /etc/openbao-agent, shell /usr/sbin/nologin,
     create_home false); add each `dev_worker_users[*].name` to the `openbao-agent` group
     (`ansible.builtin.user: append: true, groups: openbao-agent`).
  6. Dirs: `/etc/openbao-agent` (0750 root:openbao-agent). `/run/openbao-agent` is created by
     systemd `RuntimeDirectory=` — do NOT create it in ansible (tmpfs).
  7. `role-id` (0640 root:openbao-agent) and `secret-id` (0600 root:root) files from
     `dev_worker_openbao_credentials[inventory_hostname]`, content = the bare value + `\n`,
     `no_log: true`. Restart the agent when either changes.
  8. Template `openbao-agent.hcl.j2` → `/etc/openbao-agent/agent.hcl` (0640 root:openbao-agent),
     `git-credentials.ctmpl.j2` → `/etc/openbao-agent/git-credentials.ctmpl` (0640).
  9. Unit `openbao-agent.service.j2` → `/etc/systemd/system/openbao-agent.service`; enable+start
     (daemon_reload) — but only start when `dev_worker_enable_openbao`; the whole file is gated by
     the import `when` anyway.
  10. Install `files/cred` → `/usr/local/bin/cred` (0755).
  11. Managed CLAUDE.md block per user: `ansible.builtin.blockinfile` on
      `{{ item.home }}/.claude/CLAUDE.md` (create: true, owner item.name, mode 0644, marker
      `# {mark} ANSIBLE MANAGED - openbao credentials`) with content:

```markdown
## Credentials (OpenBao)

Secrets for this worker live in OpenBao, not in files or env vars. Fetch them with the `cred`
helper (wraps the local bao agent's token):

- `cred list` — names available to this worker
- `cred get <name> <field>` — print one field (pipe it straight into the consumer)
- `cred exec <name> <field> <ENV_VAR> -- <command...>` — run a command with the secret injected
  as an env var, without it ever appearing in your transcript (PREFER this)

NEVER print credential values into the conversation, logs, or files — use names/lengths when
discussing them. Git credentials for git.chifor.me are already provisioned; just use git.
```

- [ ] **Step B3: `templates/openbao-agent.hcl.j2`**:

```hcl
# Managed by ansible (role: dev_worker, ADR 0020). bao agent: AppRole auto-auth -> PERIODIC token
# (period from the role, renewed by the agent forever — immune to the 768h max-TTL cliff), sink for
# the cred helper (group-readable), and template rendering for file-shaped credentials.
vault {
  address = "{{ dev_worker_openbao_addr }}"
  # System trust already includes ailab-root-ca (update-ca-certificates), pinned again here so the
  # agent does not depend on the system store.
  ca_cert = "/usr/local/share/ca-certificates/ailab-root-ca.crt"
  retry { num_retries = -1 }
}

auto_auth {
  method "approle" {
    config = {
      role_id_file_path   = "/etc/openbao-agent/role-id"
      secret_id_file_path = "/etc/openbao-agent/secret-id"
      # The secret-id is a DURABLE per-VM credential (CIDR-bound /32, useless off-box): the agent
      # must be able to re-login after every reboot with no re-delivery machinery, so the file
      # stays. One-use wrapped delivery was considered and rejected in ADR 0020.
      remove_secret_id_file_after_reading = false
    }
  }
  sink "file" {
    config = {
      path = "/run/openbao-agent/token"
      mode = 0640
    }
  }
}

template_config {
  exit_on_retry_failure = true
}

{% for user in dev_worker_users %}
template {
  source      = "/etc/openbao-agent/git-credentials.ctmpl"
  destination = "{{ user.home }}/.git-credentials"
  perms       = "0600"
  command     = "chown {{ user.name }}:{{ user.name }} {{ user.home }}/.git-credentials"
}
{% endfor %}
```

  Note: the sink file mode is 0640 but the file is written as root:root by the agent — the unit
  (B4) uses `ExecStartPost`/`Group=` so the group is `openbao-agent`: run the service with
  `Group=openbao-agent` and `UMask=0027`; verify at impl time that the sink lands root:openbao-agent
  readable; if the agent chowns it root:root, add an `ExecStartPost=/bin/sh -c 'chgrp ...'` loop —
  prefer the simplest thing that yields a group-readable sink.

- [ ] **Step B4: `templates/openbao-agent.service.j2`**:

```ini
# Managed by ansible (role: dev_worker, ADR 0020). Keeps this worker's OpenBao periodic token alive
# and renders file-shaped credentials (see agent.hcl). Root: it writes user homes and owns the
# secret-id; the sink is group-readable (openbao-agent) for the cred helper.
[Unit]
Description=OpenBao agent (dev-worker credential plumbing)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/bao agent -config=/etc/openbao-agent/agent.hcl
Group=openbao-agent
UMask=0027
RuntimeDirectory=openbao-agent
RuntimeDirectoryMode=0750
Restart=on-failure
RestartSec=5
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
```

  (Adjust `ExecStart` path to where B2.4 installs bao — `/usr/bin/bao` for the deb,
  `/usr/local/bin/bao` for a tarball install. Keep them consistent.)

- [ ] **Step B5: `templates/git-credentials.ctmpl.j2`**:

```
{{ '{{' }} with secret "af/data/dev-workers/common" {{ '}}' }}https://{{ dev_worker_gitea_user }}:{{ '{{' }} .Data.data.gitea_pat {{ '}}' }}@git.chifor.me
{{ '{{' }} end {{ '}}' }}
```

  (Jinja2-escaped consul-template markup: the RENDERED .ctmpl on disk must read
  `{{ with secret "af/data/dev-workers/common" }}https://chifor:{{ .Data.data.gitea_pat }}@git.chifor.me{{ end }}`.)

- [ ] **Step B6: `files/cred`** — POSIX-sh helper, shellcheck-clean:

```sh
#!/bin/sh
# cred — dev-worker credential helper (ansible role dev_worker, ADR 0020). Fetches secrets from
# OpenBao via the local bao agent's sink token so interactive agents never handle the token and
# never need to print values: `cred exec` injects into a child process env instead.
#   cred list                                     # names under af/dev-workers/ visible to this host
#   cred get <name> <field>                       # print ONE field (pipe it, don't paste it)
#   cred exec <name> <field> <ENV_VAR> -- cmd...  # run cmd with the field as ENV_VAR (preferred)
set -eu

BAO_ADDR="${BAO_ADDR:-$(sed -n 's/^ *address *= *"\(.*\)"/\1/p' /etc/openbao-agent/agent.hcl | head -n1)}"
TOKEN_FILE=/run/openbao-agent/token
[ -r "$TOKEN_FILE" ] || { echo "cred: $TOKEN_FILE not readable — is openbao-agent running and are you in the openbao-agent group?" >&2; exit 1; }
export BAO_ADDR
BAO_TOKEN="$(cat "$TOKEN_FILE")"
export BAO_TOKEN

usage() { sed -n '3,8p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

cmd="${1:-}"
case "$cmd" in
  list)
    bao kv list -mount=af dev-workers 2>/dev/null || true
    bao kv list -mount=af "dev-workers/$(hostname)" 2>/dev/null || true
    ;;
  get)
    [ $# -eq 3 ] || usage
    name="$2"; field="$3"
    # Host-specific path wins; fall back to common.
    bao kv get -mount=af -field="$field" "dev-workers/$(hostname)/$name" 2>/dev/null \
      || bao kv get -mount=af -field="$field" "dev-workers/$name" 2>/dev/null \
      || bao kv get -mount=af -field="$field" "dev-workers/common" \
      | { [ "$name" = common ] && cat || cat; }
    ;;
  exec)
    [ $# -ge 6 ] || usage
    name="$2"; field="$3"; var="$4"
    [ "$5" = "--" ] || usage
    shift 5
    val="$("$0" get "$name" "$field")"
    env "$var=$val" "$@"
    ;;
  *) usage ;;
esac
```

  **Simplify `get` at impl time** — the fallback chain above is sketched; make it clean:
  try `dev-workers/<hostname>/<name>`, then `dev-workers/common` when `name` = `common`, then
  error. Names: `cred get common gitea_pat` must work. Keep it obvious and shellcheck-clean;
  the test (B8) pins the contract.

- [ ] **Step B7: wiring** — `tasks/main.yml`: after the git_forge import add:

```yaml
# Needs users (users.yml). Owns ~/.git-credentials when enabled — git_forge.yml yields (see there).
- name: OpenBao credential plumbing (bao agent + cred helper — ADR 0020)
  ansible.builtin.import_tasks: openbao.yml
  when: dev_worker_enable_openbao | bool
  tags: [openbao]
```

  `tasks/git_forge.yml`: change the outer `when` to also require
  `not (dev_worker_enable_openbao | bool)` and extend the header comment: when OpenBao plumbing is
  enabled the bao agent renders `~/.git-credentials` from `af/dev-workers/common` and this task
  yields ownership (ADR 0020) — but the `credential.helper store` git-config task MOVES to
  openbao.yml too (both paths need it; keep it running in both, duplicated is fine, comment why).

- [ ] **Step B8: `tests/test-cred-helper.sh`** — self-contained like `test-tmux-persistence.sh`
  (look at its structure first and copy the harness conventions). Test WITHOUT a real vault: put a
  stub `bao` on PATH (a function/script that asserts argv and emits a canned value), point
  `TOKEN_FILE`… the helper hardcodes `/run/openbao-agent/token` — make the test create a temp dir
  and run the helper with `TOKEN_FILE` overridable via env
  (`TOKEN_FILE="${CRED_TOKEN_FILE:-/run/openbao-agent/token}"` in the helper). Assert: `get`
  prints the stub value; `exec` injects it into a child env without it appearing in the test's
  stdout; missing token file exits 1 with the group hint; bad usage exits 2. Wire the test into
  whatever runner `.gitea/workflows/dev-worker-scripts.yaml` invokes (read the workflow's steps
  and follow — if it runs each `tests/*.sh`, the new file is picked up automatically).

- [ ] **Step B9: `.sops.yaml`** — extend the dev-worker rule's `encrypted_regex` with
  `|dev_worker_openbao_.*`:

```yaml
    encrypted_regex: ^(dev_worker_restic_password|dev_worker_cf_tunnel_token|dev_worker_admin_password|dev_worker_gitea_token|dev_worker_agentforge_.*|dev_worker_openbao_.*)$
```

  `ansible/secrets/dev-worker.sops.yaml.example` — add the new keys with placeholder values and a
  comment pointing at the mint ceremony. `ansible/group_vars/dev_workers.yml` — add a commented
  `# dev_worker_enable_openbao: true  # needs dev_worker_openbao_credentials (mint ceremony: docs/runbooks/openbao-dev-workers.md)`
  line to the optional-features block.

- [ ] **Step B10: validate** — `ansible-lint ansible/roles/dev_worker` introduces no new errors
  vs `main`; `shellcheck ansible/roles/dev_worker/files/cred ansible/roles/dev_worker/tests/test-cred-helper.sh`;
  `bash ansible/roles/dev_worker/tests/test-cred-helper.sh` passes locally;
  `ansible-playbook ansible/dev-workers.yml --syntax-check -i inventory/hosts.yml` (from repo root;
  if the control env lacks pieces, at minimum `python -c "import yaml,glob; [yaml.safe_load_all(open(f).read()) for f in glob.glob('ansible/roles/dev_worker/**/*.yml', recursive=True)]"`).

### Task C: Documentation

**Files:**
- Create: `docs/decisions/0020-dev-worker-openbao-credentials.md`
- Create: `docs/runbooks/openbao-dev-workers.md`
- Modify: `docs/runbooks/dev-workers.md` (pointer section)
- Modify: `docs/runbooks/openbao-recovery.md` (dev-worker seeds + breakglass consumer)

- [ ] **Step C1: ADR 0020** — follow the exact structure of `docs/decisions/0019-*.md` (read it
  first: status/context/decision/consequences style). Content to cover:
  - Context: dev workers hold one shared broad `chifor` PAT in `~/.git-credentials` (SOPS→ansible,
    rotation = re-run playbook); interactive agents have no scoped identity; OpenBao already holds
    the orchestrator-side credentials (ADR 0019); the 2026-08-25 768h token expiry incident.
  - Decision: per-worker AppRole (CIDR /32-bound, periodic 72h tokens), root-owned bao agent with
    group-readable sink, `cred` helper + managed CLAUDE.md so agents fetch-on-demand and never
    print values, LAN NodePort `openbao-lan` :30820 with SAN `openbao.lan.chifor.me`,
    seed-authoritative `af/dev-workers/*` KV, daily breakglass-authenticated provision Job.
  - Alternatives rejected, with one-line why each: Cloudflare-tunnel exposure (vault traffic
    through a third party; note the live `openbao.chifor.me` drift found 2026-08-31 — tfstate has
    a CNAME + ZT Access app absent from config, tracked in a Gitea issue, do NOT build on it);
    wrapped one-use secret-ids (nothing re-delivers on reboot); k8s Secrets as the distribution
    channel for secret-ids (widens the audience vs SOPS-only); cert auth (no client-cert PKI for
    VMs); extending the agentforge provisioner (estate concern kept estate-side).
  - Consequences incl. residuals: full API reachable on trusted LAN (mirror prometheus-lan
    trade-off text); persistent secret-id on worker disk (CIDR-bound); breakglass root used by a
    daily Job; sandbox pods must not reach the NodePort — CHECK the agentforge sandbox
    CiliumNetworkPolicy egress rules (kubernetes/apps/apps/agentforge/) and state the actual
    finding (allowlist shape ⇒ node-IP NodePort unreachable, or flag as follow-up if not).
  - Follow-ups: per-worker Gitea bot PATs; ingress CNP on the OpenBao pod; Cloudflare drift issue.
- [ ] **Step C2: runbook `openbao-dev-workers.md`** — operational, copy the tone/shape of
  `docs/runbooks/dev-workers.md`. Sections:
  1. What/why one-pager + component map (agent, sink, cred, Job, Service, seeds).
  2. **Activation ceremony** (ordered, exact commands):
     a. merge PR → Flux applies openbao Kustomization;
     b. cert re-issue: `kubectl --context admin@ai -n openbao delete secret openbao-tls` is NOT
        needed — cert-manager re-issues on spec change; wait for the Certificate Ready condition,
        then `kubectl --context admin@ai -n openbao delete pod openbao-0` (unsealer re-unseals;
        server re-reads the cert) and verify the SAN:
        `openssl s_client -connect 192.168.0.41:30820 -servername openbao.lan.chifor.me </dev/null 2>/dev/null | openssl x509 -noout -ext subjectAltName`;
     c. fill the seeds: decrypt `devworker-seeds.sops.yaml`, replace the placeholder with the live
        `dev_worker_gitea_token` value, re-encrypt, commit (values never in terminal output — edit
        with `sops edit`);
     d. verify the Job: `kubectl --context admin@ai -n openbao logs job/openbao-devworker-provision`
        ends with `devworker provision complete`;
     e. **secret-id mint ceremony** (per worker, from the Windows box or WSL):
        `kubectl --context admin@ai -n openbao port-forward svc/openbao 8200:8200` in one shell;
        in another, with `BAO_ADDR=https://127.0.0.1:8200 BAO_SKIP_VERIFY=... ` — NO: use
        `BAO_CACERT` pointed at the exported ailab-root-ca (the port-forward presents the svc SAN
        `openbao.openbao.svc`, so add `BAO_TLS_SERVER_NAME=openbao.openbao.svc`), authenticate with
        the breakglass token READ FROM the Secret without echoing
        (`BAO_TOKEN=$(kubectl ... -o jsonpath='{.data.token}' | base64 -d)` inside the command,
        never printed), then per worker:
        `bao read -field=role_id auth/approle/role/dev-worker-N/role-id` and
        `bao write -field=secret_id -f auth/approle/role/dev-worker-N/secret-id`
        → paste into `ansible/secrets/dev-worker.sops.yaml` via `sops edit` under
        `dev_worker_openbao_credentials`. Note: the mint runs from the operator LAN, and
        secret_id_bound_cidrs only constrains LOGIN, not minting.
     f. flip `dev_worker_enable_openbao: true` in group_vars, run `just dev-workers` (from WSL,
        `ANSIBLE_CONFIG` set — cite the existing WSL note in dev-workers.md);
     g. verify on a worker: `systemctl status openbao-agent`, `sudo -u c4 cred list`,
        `cred get common gitea_pat | wc -c` (length only), `git ls-remote https://git.chifor.me/cchifor/ailab.git HEAD`.
  3. **Rotation**: rotate a KV value (e.g. the gitea PAT) = write KV + update seeds file same
     change; rotate a worker's secret-id = re-mint (old ones: `bao write auth/approle/role/<r>/secret-id-accessor/destroy ...` — document lookup via `bao list auth/approle/role/<r>/secret-id`);
     rotate role/policy = edit the provision script, merge, Job converges within a day (or delete
     the Job and let Flux recreate).
  4. **Failure modes**: agent down → token expires after 72h period lapse → agent re-logins via
     approle on restart (nothing to do); OpenBao sealed → openbao-lan has no endpoints (by design);
     `cred` permission error → user not in openbao-agent group; NodePort unreachable → check
     /etc/hosts block + node up; after an OpenBao WIPE → provision Job re-creates
     roles/policies/seeds but ALL secret-ids are invalid → re-run the mint ceremony (e).
  5. Adding a new secret: `bao kv put/patch af/dev-workers/...` + seeds file if it must survive a
     wipe; adding a new worker: extend the WORKERS list in the provision script + mint.
- [ ] **Step C3: `dev-workers.md`** — add a short `## Credentials via OpenBao (ADR 0020)` section
  after "One-time manual steps": what changes when `dev_worker_enable_openbao` is on (git
  credentials become agent-rendered; `cred` helper; CLAUDE.md block), link to the new runbook.
  Update the "One-time manual steps" intro to note the OAuth logins are still manual by design.
- [ ] **Step C4: `openbao-recovery.md`** — read it first; add (a) `openbao-devworker-provision` to
  whatever consumer/dependency inventory it keeps (it consumes the breakglass token Secret and the
  devworker seeds), (b) a note in the seeds/recovery section: `af/dev-workers/*` is restored by
  `devworker-seeds.sops.yaml` on the next Job run, but per-worker AppRole secret-ids do NOT survive
  a wipe — the mint ceremony (openbao-dev-workers.md) must be re-run for all six workers.
  Do NOT rewrite unrelated sections.

### Task D (orchestrator, after A–C): integrate, commit, PR, review

- [ ] Reconcile drift between the three agents' outputs (names per Global constraints).
- [ ] Encrypt `devworker-seeds.sops.yaml` if the age key is available locally (check
  `sops filestatus`, `%APPDATA%/sops/age/keys.txt` / WSL `~/.config/sops/age/keys.txt`); else leave
  placeholder + runbook covers it. Verify with `sops filestatus` that the committed file IS
  encrypted; NEVER commit a plaintext PAT.
- [ ] Full validation: kustomize build, shellcheck, cred tests, ansible-lint delta, yaml sanity.
- [ ] Commit in 3 commits (k8s / ansible / docs+plan) with explicit `git add` paths; push branch;
  open PR on git.chifor.me via API; open the Cloudflare-drift issue; codex cross-review
  (`codex exec -m gpt-5.5`); address findings; watch CI (dev-worker-scripts triggers on the role
  paths; broker-inventory/tenant-guard should not).
