# Plan — an OpenBao-backed credential store for dsh

**Date:** 2026-09-10 · **Status:** DRAFT — §0 revised after an operator constraint and a second codex round
**Repo:** `cchifor/ailab` · **Subject:** `kubernetes/apps/apps/dsh/`

---

## 0. Read this first: the choice is not "OpenBao or a file"

**Operator constraint (2026-09-10), which governs everything below.** OpenBao is the estate's single
place to manage credentials. A credential that is *rotated* — or *newly added* — must become
available to every agent on every machine without any per-machine synchronisation step. That is why
the OpenBao agent exists.

**Both surviving options are the OpenBao agent.** The earlier draft of this section invited the
reading that the no-plugin option means "write a static file". It does not, and that framing was
wrong:

- **A — the agent renders the credentials document.** The bao agent authenticates, polls OpenBao,
  and re-renders the local projection when the stored value changes. No manual step on any machine.
  This is the estate's existing, proven path: every dev-worker's `~/.git-credentials` is rendered
  this way, and `git-credentials.ctmpl.j2` says so in its own header — *"rotating the PAT in OpenBao
  reaches every worker without a playbook run."*
- **B — the agent renders a sink token, and a dsh plugin does KV-v2 GETs at credential resolution.**

Neither is "static files", and neither weakens OpenBao's role as the source of truth. Note the
mechanism precisely, though: KV template updates are **polled**, not pushed. OpenBao does not
invalidate anything; the agent refreshes the projection.

### The enumeration question — verified, and it does not force the plugin

The requirement that could have decided this is *"a **new** credential added"*. A template naming one
path per stanza would need editing and redeploying for each new name — a per-machine synchronisation
step, exactly what the constraint forbids.

**It does not have to name paths.** OpenBao 2.5.5 pins `openbao-template@v1.0.1`, which registers the
`secrets` template function over `NewVaultListQuery` — a KV LIST. So a template can range over a
KV-v2 metadata prefix and render whatever is under it, and a credential added under that prefix
appears at the next render with no manifest change on any machine.

Caveats, since this is load-bearing:

- **One level per LIST.** Directory entries come back suffixed `/` and must be excluded or descended
  into; a template *can* issue successive LISTs, so a flat layout is convenient, not required.
  OpenBao's recursive `SCAN` is a different operation and there is no evidence `secrets` uses it.
- **Policy, not just capability.** This needs `list` on the metadata prefix and `read` covering
  *future* data paths under it. Today's GET access to one path establishes neither.
- **Absence needs its own treatment.** Ranging over returned names asserts nothing about an expected
  name existing. Deletion, an empty prefix, and permission loss each need specified behaviour;
  `error_on_missing_key` and `exit_on_retry_failure` govern *template* errors and agent exit, and do
  not by themselves erase a rendered destination or stop dsh using credentials it has already loaded.

### The variant I proposed to dodge the write conflict is dead

Rendering into the read-only `$DSH_HOME/.env` fallback layer would have left `.credentials.yaml`
writable for the UI. **It does not work.** In the pinned `@deepseek-ai/dsh-credentials-local@0.1.5-alpha.2`,
`dotenvFallback()` resolves through `launchEnvironmentOf(ctx)` — documented in
`@deepseek-ai/dsh-launch-environment` as an *"immutable launch-time environment snapshot"* filled
*"before any config entry mounts"*. Re-rendering `$DSH_HOME/.env` therefore **never reaches a running
dsh**; it needs a restart, which fails the operator's constraint outright.

Only the managed document is watched: `watch` defaults true, chokidar watches `spec.filename`, and
`notifyUpdated` fires per changed reference. **So option A must render `.credentials.yaml` itself.**

Two process notes, because both are recurrences of the mistake that has cost this session most:
codex found this, not me. And I first inspected `@latest` (0.0.1-rc.1), which is **not** the version
dsh 0.1.5-alpha.2 loads — checking one version and generalising to another is the same error as
checking one inference route and generalising to another.

### The price of A, stated plainly

`.credentials.yaml` is the **provider-managed writable** document. `describe()` returns
`writable: true` for anything not supplied by the inherited environment, so the Settings → Models
page still offers a write, accepts it, and loses it at the **next successful render**. Not an error,
not a conflict — the renderer compares bytes and atomically replaces; it does not merge, and it does
not take dsh's writer lock.

The operator's constraint arguably says the UI *should not* be a competing writer. But silent loss is
not the way to express that: A needs the UI write path closed or clearly marked read-only, which is
work this plan does not currently specify. B gets it for free — §3 already rejects writes loudly.

### What is actually left to decide

| | A — agent renders `.credentials.yaml` | B — sink token + read at resolution |
|---|---|---|
| OpenBao is the source of truth | yes | yes |
| rotation reaches every machine, no manual step | yes | yes |
| a newly added credential appears | yes, via prefix LIST | yes — **only** under §2's prefix scoping |
| reaches a **running** dsh | yes — the document is watched | yes |
| plugin code | **none** | 9 abstract methods, alpha interface |
| what the **dsh container** holds | only the rendered credentials | a **Bao token** for every path its role allows |
| rotation latency | agent's poll/render interval | per credential resolution |
| behaviour during an OpenBao outage | last render keeps working | fails unless cached (§5) |
| UI writes | accepted, then silently lost | rejected loudly |
| §7 prerequisites (SA token, egress, role) | required | required |

**The discriminator is freshness at credential resolution, not centralisation** — the constraint is
met by both. Three corrections to how I previously argued that gap:

- **B does not revoke anything.** Changing or deleting a KV entry alters stored data; it does not
  invalidate the key at Gitea, a model provider, or any other issuer. B stops a cooperating consumer
  fetching the stale value on its next uncached read. A copy already taken stays valid until the
  *issuer* kills it.
- **"Per operation" holds only for consumers that go through the store.** Upstream code exists that
  captures a key from config or the launch environment at registration and bypasses the credentials
  service (`web-search-exa` is the upstream example). Replacing the provider does not give those
  routes per-call freshness. Which consumers are in scope has to be enumerated, not assumed.
- **A does not mean "no Bao token in the pod".** An in-pod agent authenticates and holds a token.
  The defensible claim is narrower: the **dsh container** — the one running model-authored code —
  need not receive a sink token, provided it cannot read the agent's auth material or sink.

So the recommendation stays conditional, and deliberately so: **A is viable only once the UI write
path is closed; B is viable only once its consumer coverage is enumerated.** The rest of this plan
specifies B, because that is what was asked for.

---

## 1. What exists

**Nothing to reuse.** Upstream ships exactly `@deepseek-ai/dsh-credentials` (the interface) and
`@deepseek-ai/dsh-credentials-local` (file-backed). Probed and 404: `dsh-credentials-openbao`,
`-vault`, `-remote`, `-http`, and the `dsh-experimental-*` prefix that caught me out on Agent Teams.
No community equivalent.

**The seam.** `CredentialProvider` is an abstract cordis Service constructed as
`super(ctx, "credentials")` — a **singleton service name**. `dsh-base` mounts
`- id: credentials / name: '@deepseek-ai/dsh-credentials-local'`. Adding a provider therefore means
**overriding that row**, never inserting a second one; two would collide on the service name.

**Nine abstract methods**, verified against `@deepseek-ai/dsh-credentials@0.1.5-alpha.2`
(`lib/types/index.d.ts` declares exactly nine `abstract` members): `resolve`, `describe`, `set`,
`unset` (reference half); `readRecord`, `describeRecord`, `listRecords`, `modifyRecord`,
`deleteRecord` (record half). `notifyUpdated` / `notifyRecordUpdated` are **protected concrete**
helpers the subclass calls, not members it must implement — eleven members in total, nine of them
to write.

---

## 2. Layering — keep the documented trust order, empty the env

The reference implementation layers deliberately:

```
inherited process environment      (read-only, WINS)
> $DSH_HOME/.credentials.yaml      (provider-managed, writable)
> <cwd>/.env   >   $DSH_HOME/.env  (read-only fallbacks)
```

Its stated reason for env winning: *"it cannot be edited from inside, so it must be visibly
read-only rather than silently shadow writes."*

That creates a bind: OpenBao **above** env inverts the documented order and breaks `-e` override;
OpenBao **below** env is never consulted, because `LITELLM_API_KEY` is set today.

**Resolution: keep the order, stop populating the env var.** OpenBao occupies the
provider-managed layer; `-e` still overrides for debugging; OpenBao is the normal source. Codex
independently arrived at the same shape — *"env override → explicitly mapped, read-only OpenBao
references"*.

**Scope is declared; names are not.** The draft mapped each reference to one path and field:

```yaml
map:
  LITELLM_API_KEY: estate/litellm#master_key          # REJECTED -- see below
```

**That form cannot satisfy the operator constraint**, and the contradiction was mine: a credential
added in OpenBao would still need this mapping distributed to every machine before any agent could
resolve it — the per-machine synchronisation step the constraint exists to remove. §0's table
promised B automatic discovery while §2 made it impossible.

The security property that per-name mapping was protecting is *"a settings edit must not be able to
reach an arbitrary OpenBao path"*. **Prefix scoping preserves it without freezing the names.**
Config declares authorised prefixes, and a reference resolves to a path derived **inside** one:

```yaml
scopes:
  - prefix: af/dsh          # KV-v2 mount + prefix; data/ and metadata/ segments implied
    field: value            # which field of the document carries the secret
```

`LITELLM_API_KEY` resolves at `af/data/dsh/LITELLM_API_KEY`, field `value`. Derivation is confined to
operator-declared prefixes, so a settings edit still reaches nothing outside them, and a credential
added under a declared prefix is resolvable everywhere with no config change. Constraints this
carries:

- **Reference names become path segments.** They must be validated against the KV path grammar and
  rejected — never escaped, never silently rewritten — so a crafted reference cannot traverse out of
  its prefix.
- **Prefix order must be total and declared**, since two scopes could otherwise both offer a name.
- **B's discovery is the resolver's, not the template's.** `listRecords`/`describe` need `list` on
  `af/metadata/dsh/` at request time; the §9 template test exercises the agent's LIST, which is a
  *different* code path and does not establish the resolver's. Both need testing.
- **A uses the same layout**, so this is shared groundwork rather than B-only cost.

---

## 3. Writes and records — out of scope, for a concrete reason

`set`/`unset` **reject**. The contract requires it: a write must not *"appear to succeed while
resolution keeps returning the shadowing value"*.

**The record half stays on a local writable store, and remote records are deferred.** The reason is
not effort. `modifyRecord` is specified as a serialized read-modify-write whose exclusion *"holds
across processes"*, because that is *"what makes a token refresh safe"*. KV-v2 CAS **does not
provide this**: CAS protects the stored version, but by the time a write is rejected the callback
has already performed its external refresh. Exclusion must span the whole read → callback → commit
sequence, a JavaScript mutex covers one process only, and retrying the callback is unsafe because
the contract never promises it is retryable. An issuer refresh can also succeed while the subsequent
OpenBao write fails, with no shared transaction to unwind it.

**Before replacing the provider, enumerate existing records.** `listRecords()` returning `[]` must
mean *an intentionally empty store*, never *records I cannot see*. If the live `.credentials.yaml`
holds grant records, they must be preserved or migrated, not silently orphaned.

---

## 4. Encapsulation — the agent owns auth, the plugin owns the seam

The plugin does **not** authenticate. The OpenBao agent owns k8s auth, renewal and the sink; the
plugin reads what the agent produced. That mirrors the estate's `cred` helper and keeps renewal
logic out of the model's process.

Two shapes, and the choice is the §0 decision:

- **A — agent renders `.credentials.yaml`** → the dsh container receives no sink token, and the
  document's watcher carries rotations and additions into a running process. Requires closing the
  UI write path, which would otherwise accept a write and lose it at the next render.
- **B — agent renders a sink token, plugin reads KV-v2 at credential resolution** → freshness at
  every resolution, at the cost of a token inside the container that runs model-authored code.

In both, the agent authenticates and holds a token; the difference is whether the **dsh container**
gets one. Keeping the agent's sink and auth material unreadable from that container is what makes
A's claim true, and it is a deployment property, not a plugin property.

---

## 5. Outage behaviour — decided up front, not discovered

`CredentialInfo` is `{configured, source?, writable}` and **cannot express "unknown because the
backend is unavailable"**. Returning `configured: false` during an outage asserts something not
established. So behaviour is specified rather than left to fall out:

| condition | behaviour |
|---|---|
| explicit env override present | resolve locally, do not contact OpenBao |
| successful read | return value and source |
| confirmed missing/deleted | report unconfigured; suppress lower-source fallback |
| timeout / sealed / network / 5xx | fail the operation, sanitised "credential store unavailable" |
| permission denied | report access failure — **not** "missing" |
| malformed response | report a data/configuration error |

Bounded deadlines and retries; no unbounded wait on the agent. Recovery must let the **next**
operation succeed without restarting dsh.

**This is a UI concern too.** The settings controller describes references with `Promise.all`, so one
failing reference rejects the whole description batch — an outage degrades the Models page, not just
inference.

A last-known-good cache is a legitimate availability choice, but it weakens rotation and revocation.
If adopted, it needs a maximum stale age and a visible degraded state. Note also that *"the sidecar
is running"* is not evidence of a recent successful fetch.

---

## 6. Delivery

File-plugin in the content-hashed ConfigMap — the `searxng-search.mjs` idiom already in this
directory. No registry, no runtime install, and the row's relative `name:` resolves against the
profile directory.

**A boot-order fact that must shape the tests.** `@deepseek-ai/dsh-credentials` resolves from the
profile **only after a real boot**: the fallback module farm at `$DSH_HOME/profiles/node_modules`
(186 entries, mirroring dsh's own closure) is materialised at boot, **not** by `--dump-config`. It is
absent on a fresh home. Verified. So a warm profile or a config dump **cannot** establish
compatibility — a **fresh-home real boot** is the only valid check.

The security benefit comes from reviewed, pinned code, not from the `.mjs` extension. Treat it as
maintained software: source outside the manifest, reproducible artifact, compatibility pinned to the
exact dsh version.

---

## 7. Prerequisites this change does not itself provide

1. **A ServiceAccount and projected token.** The pod has `automountServiceAccountToken: false` and no
   SA, so there is no identity for k8s auth.
2. **Egress to OpenBao.** Egress excludes `10.0.0.0/8` and `192.168.0.0/16`, so neither
   `openbao.openbao.svc:8200` nor `openbao.lan.chifor.me:30820` is reachable today. NetworkPolicy
   selects **pods**, not containers — this grants the whole pod reach to OpenBao, including the
   container running model-authored code.
3. **An OpenBao role and policy** scoped to exactly the paths mapped in config, created through the
   `agentforge-provisioner` operator path (OpenBao 2.5.5 has no root recovery).

---

## 8. Acceptance

Largely codex's list; each is a thing that has to be shown, not argued:

- the prefix-LIST template renders every key under `af/dsh/` and picks up a **newly added** one with
  no template edit — the operator constraint, shown rather than argued
- for B, the **resolver** discovers the same added key with no config change, and a reference name
  crafted to traverse outside its declared prefix is rejected rather than escaped
- **consumer coverage enumerated**: every credential user that goes through the service, and every
  one that captures a value at registration and bypasses it (§0). Whatever freshness is promised is
  promised only for the first set.
- exactly **one** credentials service after a **cold** boot on a fresh home
- rotation reaches the **next operation** under the chosen freshness promise
- env override, remote deletion, and stale fallback each behave deliberately
- delayed initial auth, agent restart, sink replacement, OpenBao outage **and recovery**
- read-only UI behaviour: rejected writes, and what the Models page does during an outage
- existing records preserved or migrated — enumerated **before** the switch
- external-change notifications actually fire, so UI badges update

---

## 9. Sequence

1. **Decide §0 — A or B.** The operator constraint is met by both; the discriminator is freshness at
   credential resolution versus the agent's render interval, priced against closing dsh's UI write
   path (A) or 11 methods against an alpha interface (B). Everything else follows.
2. Land the prerequisites in §7 — needed either way. The bao agent sidecar needs the same
   ServiceAccount token, egress and OpenBao role the plugin would, plus `list` on the metadata
   prefix and `read` covering future data paths under it.
3. **Enumerate the consumers.** Which credential users actually go through the credentials service,
   and which capture a value at registration and bypass it. This bounds what either option can
   promise and is required before B can be specified, not after.
4. Prove the prefix-LIST template renders an `af/dsh/*` layout and picks up an added key, with
   deletion, empty-prefix and permission-loss behaviour observed rather than assumed. Both options
   need this layout, so it is not throwaway work. For B this is **not sufficient**: the resolver's
   own discovery (§2) is a separate code path and needs its own test, including a reference name
   that tries to traverse out of its declared prefix.
5. If A: template `.credentials.yaml`, stop populating `LITELLM_API_KEY`, close or visibly disable
   the UI write path, then the §8 items that apply.
6. If B: spike the seam on a fresh home first (row override + `resolve` + cold boot), then implement
   the reference half, keep records local, then §8.
