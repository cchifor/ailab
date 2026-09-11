# ADR 0022 — LiteLLM's `model_list` is the single source of truth for every model consumer

**Status:** ACCEPTED (2026-09-11). Implemented in #666; the two cloud3 routes that motivated it
landed in #663 and #664 (serving side: cloudlab issue #7, PR #11).
**Relates to:** ADR 0015 (the LLM appliance behind LiteLLM + Open WebUI), 0018 (the dev-worker
agents that consume these routes through `litellm-local`), 0019 (Flux as the only path into the
cluster), and the dsh runbook (`docs/runbooks/dsh.md`, the provider block).

## Context

A model that LiteLLM serves does not reach the places people pick models until two more places
know its name:

| consumer | where the name lived | how it reached the pod |
|---|---|---|
| Open WebUI | `OPENAI_API_CONFIGS` connection `"0"`, a static `model_ids` allowlist that decides the **Local** group of the picker (`kubernetes/apps/apps/ai/open-webui.yaml`) | pod-template env, so an edit rolls the pod |
| dsh | the `models:` rows of the `litellm` provider block in `kubernetes/apps/apps/dsh/settings.seed.yaml` | `reconcile-provider.js` splices exactly that block into the live `settings.yaml` at every pod boot; the seed rides a content-hashed ConfigMap, so an edit rolls the pod |
| LiteLLM itself | `checksum/config` on the proxy's pod template | the only rollout trigger, because the ConfigMap is a volume mount and there is no reloader |

Every one of those was a hand edit, with nothing forcing them to agree. Commit c90ff86
(2026-09-08, "Registered in three places, all of which are needed for it to be usable") is the
process working; 2026-09-11 is the process failing: `qwen3.8-flash-next-q6-cloud` and
`qwen3.8-27b-fp8-cloud` were added to `litellm.yaml` and to nothing else, so they were absent from
dsh and from Open WebUI's Local group, and the checksum was stamped with the wrong extraction
(sha256 of the bare YAML scalar instead of the documented `yq | sha256sum` recipe) twice in one
day before the CI gate caught it. Open WebUI masks the first half of that failure by its code
path (not observed in the UI that day): its second connection discovers every LiteLLM route, and
any id not claimed by the Local allowlist is shown under **External** — present, but not in the
group the two-connection design exists for.

Three shapes of automation were on the table:

1. **Runtime discovery in each consumer.** Open WebUI cannot: a non-empty `model_ids` makes it skip
   `/v1/models` for that connection and there is no wildcard, so the Local/External split and
   discovery are mutually exclusive on one connection. A key-scoped `/v1/models` (a virtual key whose
   `models` is the self-hosted set) would restore both, but virtual keys need a database the main
   proxy does not have — `litellm-local` runs that way; the main proxy answers a non-master key with
   `400 No connected db`. dsh could fetch `/model/info` in its `seed-settings` init container, but
   that container has no LiteLLM key today, the master key would list the paid providers too, and
   the init only runs when the dsh pod rolls — which a LiteLLM change does not cause. So "automatic"
   at runtime still needs a rollout, plus a new credential and a new boot-time dependency.
2. **A generator that derives the consumer lists from `litellm.yaml`, with a CI check** — the shape
   the repo already uses for the broker seat inventory (`scripts/gen-broker-inventory.py --check`).
3. **A CI check only** (drift fails, but every place is still edited by hand).

## Decision

**`litellm.yaml`'s `model_list` is the source; the consumers are derived, and drift fails CI.**

`scripts/gen-litellm-consumers.py` owns three spans end to end:

- Open WebUI's `"0".model_ids` array (that one line; the rest of the JSON byte-identical, verified
  by requiring canonical compact JSON before rewriting);
- the `- id:` rows of dsh's `litellm` provider block (the block shape `reconcile-provider.js` walks
  is preserved; a unit test runs the real reconcile script under node against the output);
- LiteLLM's `checksum/config`, derived by importing `scripts/check-inline-hashes.py`'s function so
  the gate and the generator can never disagree.

A route is **consumer-visible** when its `litellm_params.api_base` host is a private IPv4 or ends
in `.svc.cluster.local`, its `model_info.mode` is unset or `chat`, and `model_info.hidden` is not
`true` (`hidden` must be a YAML boolean; anything else is an error). Paid providers and the
embedding model have no `api_base` and are never listed. Order is `model_list` order. An empty
selection is refused, never rendered. `input: [text, image]` is emitted for dsh only where the
LiteLLM entry says `supports_vision: true`, which by repo convention is only set after a probe.

`--check` runs in the manifests workflow next to the inline-hash gate and fails closed; `--write`
applies. `just af-verify-litellm` / `just af-gen-litellm` pair them the way the broker recipes do.
Adding a model is one edit in `litellm.yaml`, one command, one commit
(`docs/runbooks/model-registration.md`).

## Consequences

- **A forgotten consumer is impossible, a forgotten generator run is a red check.** The residual
  human steps are the ones that need judgement: the `model_info` numbers (context arithmetic from
  the host's launch args), a vision claim (only after a probe), and fallback chains.
- **`hidden: true` is the opt-out, and it is partial by design.** It removes a route from dsh and
  from Open WebUI's Local group; Open WebUI's second connection still discovers it under External,
  because LiteLLM does not know the key and keeps advertising the name. The always-on fallback
  `qwen3.6-35b-a3b-local` is hidden this way: reachable by name and as a fallback target, exactly as
  before, without appearing in pickers it never appeared in.
- **The generated allowlist is in `model_list` order** (it used to be hand-ordered). How Open
  WebUI renders that order in its picker was not verified; the lever on this side is the order of
  `model_list` itself.
- **Nothing changed at runtime.** Pods roll exactly as they did (checksum annotation, ConfigMap
  hash, env); LiteLLM is still read only at startup. What changed is that the three edits are one.
- **The `config.yaml` block is located the way `check-inline-hashes.py` locates it** — the first
  `config.yaml: |` literal block in `litellm.yaml` — so `litellm-config` must stay the first such
  ConfigMap in that file.
- **Two follow-ups are explicitly not done.** (a) Runtime discovery, if ever wanted: a `model_info`
  marker LiteLLM returns on `/model/info`, the key in dsh's init container, and a trigger that rolls
  dsh when `litellm-config` changes — or a database on the main proxy so a scoped key can filter
  `/v1/models` for Open WebUI. (b) Replacing the hand-stamped checksum with a kustomize
  `configMapGenerator` (as dsh already does), which would make the annotation unnecessary; the
  generator stamping it removes the hand step with a smaller blast radius, so this waits.
