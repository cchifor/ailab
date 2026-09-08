# Runbook: dsh (DeepSeek Harness)

An agentic coding UI at **<https://dsh.chifor.me>**, served from the `dsh` namespace and using the
estate's own models through LiteLLM. Manifests: `kubernetes/apps/apps/dsh/`.

**The defining constraint: this pod executes model-authored code.** The agent runs shell commands
and writes files, and a `danger-full-access` permission preset is selectable in its UI. Nearly every
design decision below follows from that, and several of them look strange until you hold it in mind.

---

## Topology

```
browser ──▶ Cloudflare Access ──▶ cloudflared ──▶ dsh Service :80
                                                      │
                                                 relay (TCP) :8080
                                                      │
                                                 dsh :3080 (loopback)
                                                      │
                             ┌────────────────────────┼────────────────────┐
                             ▼                        ▼                    ▼
                    LiteLLM (models)         searxng.dsh.svc:8080      CoreDNS
                                                      │
                                                 the internet
```

`dsh` itself has **no route to the internet**. Its NetworkPolicy allows exactly three destinations:
CoreDNS, LiteLLM, and the SearXNG Service. SearXNG does any reaching-out, and runs no
model-authored code.

| Component | What it is |
|---|---|
| `dsh` Deployment | 2 containers: `dsh` (the app) and `relay` (a raw TCP proxy) |
| `relay` | `dsh web` binds loopback only and rejects `--host 0.0.0.0`; the relay is what makes it reachable from the pod network at all |
| `searxng` Deployment | self-hosted metasearch; the only component with egress |
| `dsh-app` PVC | RWX nfs-csi — the npm-installed dsh tree |
| `dsh-home` PVC | RWO local-path — config, credentials, sessions |
| `dsh-workspace` PVC | RWO local-path — the agent's working directory |

> Both RWO volumes are `local-path`, which carries node affinity, so **the Deployment is pinned to
> one node** (currently talos-cp1). If that node is lost, dsh stays down until it returns. This
> predates the workspace volume; it is inherent to `dsh-home`.

---

## Getting in

dsh prints a **launch token** at startup and refuses everything else:

```
dsh web authentication required; reopen the URL printed by dsh web.
```

Two different secrets are involved, and conflating them wastes an afternoon:

| | lifetime | pinnable? |
|---|---|---|
| **launch token** (`?token=`) | `randomBytes` in an in-memory WeakMap — **changes every restart** | No. No `--token` flag, no env override |
| **cookie signing secret** | persisted in `$DSH_HOME/.credentials.yaml` | n/a — survives restarts |

A valid token on `GET /` mints a signed cookie; after that the token is not needed. Because the
signing secret persists, **cookies survive pod restarts** — only their own expiry ends them, and
`cookieMaxAgeDays` is set to 3650 in `cordis.patch.yml`, so this is once per browser rather than
monthly.

**To get a fresh login URL:**

```bash
P=$(kubectl -n dsh get pods --no-headers | grep -v install | grep Running | awk '{print $1}')
TOK=$(kubectl -n dsh logs "$P" -c dsh | grep -oE 'token=[A-Za-z0-9_-]+' | head -1 | cut -d= -f2)
echo "https://dsh.chifor.me/?token=$TOK"
```

Cloudflare Access is the real per-person gate; this cookie is a second layer. Revoke by deleting the
`client-connection/browser-session` record from `.credentials.yaml`.

> `cloudflared` deliberately sets **no `httpHostHeader` override** for this hostname. dsh's `/api`
> trust fence refuses any request whose Host is not a declared authority, and the pod declares
> `--trusted-host dsh.chifor.me`. Rewriting Host would 403 every request.

---

## The image, and why it is what it is

**`node:22` (Debian), not alpine and not slim.** Measured in-cluster:

| | alpine | node:22-slim | **node:22** |
|---|---|---|---|
| `bash` | ✗ | ✓ | ✓ |
| `python3` / `git` / `curl` | ✗ | ✗ | ✓ |
| `make` / `g++` | ✗ | ✗ | ✓ |

On alpine every Bash tool call failed with **`spawn bash ENOENT`** — dsh's shell backends spawn the
literal binary `bash`, and alpine ships only busybox `sh`. slim fixes that but leaves the agent with
no `python3`, `git` or `curl`. `make`/`g++` matter separately: they let npm build a native module
from source when no prebuilt matches.

### The install path is keyed on libc

```
/app/${DSH_VERSION}-${DSH_BUILD}      e.g. /app/0.1.2-rc.1-glibc
```

Moving musl → glibc invalidates a tree npm resolved for the other libc, and the installer's
`.installed` marker check is **per-directory** — without a distinct path it reports "already
installed" and the mismatch is silent. `DSH_BUILD` rides the same kustomize replacement as
`DSH_VERSION`, from the Job, so the Deployment cannot end up waiting on a path the installer never
creates.

**Bump `DSH_BUILD` on any base-image libc change.** The Job's name encodes it too, because a Job's
pod template is immutable — the `kustomize.toolkit.fluxcd.io/force` annotation is what lets Flux
replace it.

---

## The agent's workspace and what confines it

`workingDir: /workspace`, backed by its own PVC.

This matters because dsh derives the sandbox root from **`process.cwd()`**:

```yaml
sandbox-policy:
  workspaceRoot: !!js process.cwd()
  mode:          !!js process.env.DSH_PERMISSION_MODE ?? 'workspace-write'
```

The container's cwd used to be `/`, which made the sandbox root **the entire filesystem** — model
code could read `/dsh-home/.credentials.yaml` (the cookie signing secret) and rewrite `settings.yaml`
and the trust config.

**Two honest limits:**

1. This is a *policy* layer (landlock), not a Unix permission. A shell inside the container can
   still read those files; what changes is what dsh's own tools will do.
2. It does nothing under the **`danger-full-access`** preset, whose purpose is to lift the sandbox.

The pre-existing workspace record in `storages/workspace.json` still names `/dsh-home`. It was left
alone deliberately — it keys workspaces by UUID with attached session ids, and editing it can orphan
sessions. Use the UI's directory picker to move an existing workspace.

---

## Capabilities

`dsh --profile web --dump-config` prints the resolved plugin tree. Upstream ships **27 of 145 rows
disabled**; seven are enabled in `cordis.patch.yml`:

`compaction-basic`, `command-compact`, `agent-instructions`, `plan-mode`, `tool-todo`, `tool-skill`,
`skill-filesystem`

Compaction is the one that matters most — without it a long session simply fills the context window
and ends.

> **Every enabled row restates its FULL config.** A patch **replaces** a row's config rather than
> merging into it. Enabling a row with a bare `disabled: false` silently erases whatever config it
> had. `plan-mode`'s config is a 37-line prompt section; losing it leaves plan mode enabled but mute.
> The rows in `cordis.patch.yml` were generated from `--dump-config` output so they are byte-exact.

### Two patch forms, and the trap

```yaml
- id: existing-row        # OVERRIDE — only works if the row already exists
  config: { ... }

- insert:                 # INSERT — required for a NEW row
    - id: new-row
      name: './thing.mjs'
```

An override whose target does not exist is **skipped** with a single log line:

```
dsh: [.../cordis.patch.yml] patch: entry "searxng-web" not found
```

…and everything downstream referencing it fails at runtime. This shipped once and was caught only by
resolving the patch against the running instance.

---

## Web search

Live and working. `web_search` returns real results; **`web_fetch` is deliberately off**.

```
dsh ──▶ searxng.dsh.svc:8080 ──▶ search engines
```

The provider is a **file**, `searxng-search.mjs`, shipped in the content-hashed ConfigMap and
installed by the init container beside `cordis.patch.yml`, referenced as `name: './searxng-search.mjs'`.

### Why a file and not an npm plugin

This is the single most expensive lesson in this runbook. `dsh-searxng-web` exists on npm, is
compatible, and **cannot be used here**:

dsh resolves a row's `name:` from the **profile directory**, not the dsh install tree. That
directory's `node_modules` is a symlink farm holding dsh's *own dependency closure* and nothing else
— verified: 195 packages in `/app`, 193 in the farm, and the three absent ones are exactly those dsh
does not depend on. An npm-installed sibling therefore throws `MODULE_NOT_FOUND`, `boot()` rethrows,
and **the pod crash-loops**. It does not degrade to "search is broken"; the web UI goes down.

A relative specifier resolves against that same directory, so a file works. It also removes the
supply-chain question entirely: no third-party code in the process that runs model-authored tool
calls.

### Why `web_fetch` is off

SearXNG is a *metasearch* engine — it does not fetch arbitrary pages for a caller. dsh's built-in
`http` fetch provider does, but it fetches **from the dsh pod**, which has no egress, so every call
would fail. Advertising a tool that always fails wastes the model's turns discovering it doesn't
work. `tool-web` is therefore configured `fetch: false`.

Adding real fetch needs its own component; see `docs/plans/` if one exists, or the discussion on the
PR that introduced search.

### SearXNG tuning worth knowing

| setting | why |
|---|---|
| `search.formats: [html, json]` | **the whole point** — SearXNG serves HTML only by default and the JSON API 403s without this |
| `engines: google disabled: false` | upstream ships Google **disabled**; without this it is never queried |
| `enable_metrics: true` | with metrics off, `count_error()` returns immediately and `/stats/errors` answers `{}` — monitoring reports "no failures" forever |
| `startpage disabled: true` | behind a proof-of-work interstitial served as HTTP 200, so it fails as a JSON parse error, is **never suspended**, and is re-queried on every search |
| `suspended_times.SearxEngineCaptcha: 300` | a CAPTCHA otherwise takes an engine dark for an hour |
| `request_timeout: 5.0` | one hanging engine holds the entire response for this long |

`pool_maxsize` and `keepalive_expiry` were **removed upstream** by the curl_cffi migration; the
settings loader iterates its schema, not your keys, so stale options are ignored silently rather than
erroring.

**SearXNG will not boot** with the upstream placeholder secret (`server.secret_key is not changed`),
and its entrypoint owns `/etc/searxng` — it copies a template, `sed -i`s it and chowns the directory.
So the ConfigMap is seeded into a writable emptyDir by an init container that also generates the key.

---

## Diagnosing

### The agent says a tool is unavailable or a command fails oddly

Check the binary exists before anything else — this accounted for every reported fault once:

```bash
kubectl -n dsh exec deploy/dsh -c dsh -- sh -c 'for t in bash python3 git curl; do printf "%-8s %s\n" $t "$(command -v $t || echo MISSING)"; done'
```

### Models disappeared from the picker

`settings.yaml` lives on the PVC and is **owned by dsh** (the Settings UI writes it). The
`reconcile-provider.js` init script rewrites only the `litellm` provider block from the seed on every
boot, preserving everything else. If model ids were renamed in LiteLLM and dsh still lists the old
ones, check that script's output:

```bash
kubectl -n dsh logs deploy/dsh -c seed-settings
```

### Pod stuck in `Init:0/2`

**Usually it is not stuck.** The init sequence mounts NFS and runs two init containers; a slow start
looks identical to a stall in `kubectl get pods`. Read the pod *status* before acting — a healthy pod
was once deleted for no reason because the summary column lagged:

```bash
kubectl -n dsh get pod <pod> -o jsonpath='{range .status.initContainerStatuses[*]}{.name}: {.state}{"\n"}{end}'
```

If `wait-for-install` is genuinely waiting, look at the install Job, which gates it via
`.installed`:

```bash
kubectl -n dsh get job
kubectl -n dsh logs job/dsh-install-<version>-<build>
```

### Web search returns nothing

```bash
# is SearXNG healthy?
kubectl -n dsh exec deploy/dsh -c dsh -- node -e 'fetch("http://searxng.dsh.svc.cluster.local:8080/healthz").then(r=>console.log(r.status))'

# which engines failed on the last query? unresponsive_engines is emitted unconditionally
kubectl -n dsh exec deploy/searxng -- python3 -c "..."   # or read /stats/errors, needs enable_metrics
```

Individual engines failing is **normal**. A live probe saw DuckDuckGo return a CAPTCHA and Startpage
malformed JSON, and the query still returned 32 results from the rest. That is metasearch working as
intended, not a fault.

### Verifying a config change before it merges

The highest-value habit in this runbook. Resolve the patch against the running instance with a
throwaway `DSH_HOME`, which is non-destructive:

```bash
P=$(kubectl -n dsh get pods --no-headers | grep -v install | grep Running | awk '{print $1}')
kubectl -n dsh exec $P -c dsh -- sh -c '
  mkdir -p /tmp/t/profiles/web
  cp /dsh-home/settings.yaml /tmp/t/settings.yaml
  cp /dsh-home/profiles/web/package.json /tmp/t/profiles/web/
  # copy your candidate cordis.patch.yml into /tmp/t/profiles/web/ first
  cd /app/*/ && DSH_HOME=/tmp/t ./node_modules/.bin/dsh --profile web --dump-config 2>&1 >/dev/null | head
'
```

Loader errors go to **stderr** and the process still exits 0, so a change that silently drops a row
looks like success unless you read stderr explicitly.
