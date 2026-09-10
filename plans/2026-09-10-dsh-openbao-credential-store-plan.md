# Plan — an OpenBao-backed credential store for dsh

**Date:** 2026-09-10 · **Status:** DRAFT, aligned with a codex cross-analysis
**Repo:** `cchifor/ailab` · **Subject:** `kubernetes/apps/apps/dsh/`

---

## 0. Read this first: you may not need the plugin

The ask was "a credential-store plugin based on the OpenBao agent". Working the design through with
codex surfaced a cheaper answer that meets most of the goal with **no plugin at all**, and it would
be dishonest to bury it under an 11-method implementation.

dsh's existing provider, `dsh-credentials-local`, already reads a **provider-managed writable
document** at `$DSH_HOME/.credentials.yaml`, and hot-publishes external edits to that file through
the seam. An OpenBao **agent sidecar can render exactly that file** from a template.

| | agent renders `.credentials.yaml` | plugin + sink token + per-operation GET |
|---|---|---|
| plugin code | **none** | 11 abstract methods, alpha interface |
| what the pod holds | **only the rendered credential** | a **Bao token** that can fetch every path its role allows |
| freshness | the agent's template refresh interval | **per operation**, genuinely live |
| blast radius if the agent's shell is abused | one secret | anything the AppRole permits |
| Settings → Models page | keeps working (the file stays writable) | breaks unless writes are handled |

**The plugin earns its place only if "live" must mean *per operation*.** If a refresh interval
measured in seconds-to-minutes is acceptable, agent rendering is better on every axis that matters
here — less code, smaller blast radius, and no new failure mode in the credential path of a pod that
executes model-authored code.

This also retires an argument I made earlier and got wrong. I claimed the objection to an agent was
that it puts a sink token in the pod. The correct response is not "no agent" — it is **have the agent
render the secret rather than hand over a token**.

The rest of this plan specifies the plugin, because that is what was asked for and because the
per-operation property is a real requirement if rotation must land inside a single session. But
§0 is the recommendation.

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

**Eleven abstract methods**: `resolve`, `describe`, `set`, `unset` (reference half); `readRecord`,
`describeRecord`, `listRecords`, `modifyRecord`, `deleteRecord` (record half); plus the protected
`notifyUpdated` / `notifyRecordUpdated`.

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

**Mapping is explicit, never inferred.** A reference name maps to a path and field in config:

```yaml
map:
  LITELLM_API_KEY: estate/litellm#master_key
```

Deriving a path from a reference name would let a settings edit reach an arbitrary OpenBao path.

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

- **Agent renders the secret** → dsh never holds a Bao token. Preferred.
- **Agent renders a sink token, plugin does KV-v2 GETs** → true per-operation freshness, at the cost
  of a token in the pod.

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

- exactly **one** credentials service after a **cold** boot on a fresh home
- rotation reaches the **next operation** under the chosen freshness promise
- env override, remote deletion, and stale fallback each behave deliberately
- delayed initial auth, agent restart, sink replacement, OpenBao outage **and recovery**
- read-only UI behaviour: rejected writes, and what the Models page does during an outage
- existing records preserved or migrated — enumerated **before** the switch
- external-change notifications actually fire, so UI badges update

---

## 9. Sequence

1. **Decide §0.** Agent-rendered file, or plugin with per-operation reads. Everything else follows.
2. Land the prerequisites in §7 — they are needed either way.
3. If agent-rendering: template `.credentials.yaml`, stop populating the env var, done.
4. If plugin: spike the seam on a fresh home first (row override + `resolve` + cold boot), then
   implement the reference half, keep records local, then §8.
