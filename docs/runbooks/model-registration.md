# Runbook: registering a model across the estate (LiteLLM → dsh, Open WebUI)

The serving side of a model lives where the model runs (ailab's LXCs: `ai-model-swap.md`; the
cloudlab GPU cluster: `git.chifor.me/cchifor/cloudlab`, `host/llama-swap.yaml` and runbook §11/§13).
This runbook is the **registration** side: how a model that is already answering on some `/v1`
endpoint becomes a route in LiteLLM and, from that one edit, a choice in dsh and in Open WebUI.
Decision record: ADR 0022.

## The shape

```
serving host (/v1)  ->  litellm.yaml model_list (the ONE edit)
                            |-> just af-gen-litellm  ->  open-webui.yaml  "0".model_ids   (Local group)
                            |                        ->  dsh settings.seed.yaml  models:  (picker)
                            |                        ->  litellm.yaml checksum/config     (rollout)
                            '-> CI: just af-verify-litellm fails closed on drift
```

Open WebUI's second connection discovers **everything** LiteLLM serves; ids claimed by the generated
allowlist on the first connection show under **Local**, every other id shows under **External**
(by the merge rule in Open WebUI's code — first connection to claim an id wins). dsh shows exactly
what its seed names.

## 1. Before touching the repo

- The backend answers `GET /v1/models` and a real completion **with content**, not just HTTP 200
  (the 2026-09-10 cloud2 incident answered 200 with garbage for two hours; cloudlab's
  `scripts/vllm-test.sh health` is the content-validating probe for vLLM routes).
- You know the host's window and slots: LiteLLM's `max_input_tokens` is the per-request context the
  backend actually serves (`-c / --parallel` for llama.cpp, `--max-model-len` for vLLM) **minus**
  output headroom. The existing entries document their own: 49152 on the cloud2 xhigh route and
  the FP8 route (xhigh reasoning measured 16–39k tokens per answer), 32768 on the Flash-Next tier,
  ~8K on the coder route — copy the reasoning, not the number. Where prefill is slow (offloaded
  experts) also judge the cap against `litellm_settings.request_timeout` (900 s) using the
  measured prefill rate plus the cold load; the Q6 route's provisional 98304 was derived that way
  and its comment shows the arithmetic.
- Vision is claimed only after a distinct-image probe (three different images must give three
  different answers; `prompt_tokens` is not evidence). No probe, no `supports_vision`.

## 2. The one edit: `kubernetes/apps/apps/ai/litellm.yaml`

Add an entry to `model_list` in the tier's section, in the shape the neighbours use:

```yaml
      - model_name: <name>-cloud            # -cloud for cloudlab backends, -local for ailab nodes
        litellm_params:
          model: openai/<served name>        # what the backend answers to (llama-swap id, --served-model-name)
          api_base: http://<host>:<port>/v1
          api_key: "sk-noauth"
          temperature: 1.0                   # sampling lives HERE, not on the host
          top_p: 0.95
          extra_body:
            top_k: 20
        model_info: { max_input_tokens: <window minus headroom> }
```

Conventions the comments around the existing entries enforce, and the generator relies on:

- **One `model_name` per backend engine and box.** Never pool two engines under one name; LiteLLM
  would route silently to the slower one with no way to tell which answered.
- **A dated comment block above the entry** with the measured numbers and the context arithmetic.
- **Visibility is derived, not declared.** The route is listed in dsh and in Open WebUI's Local
  group when `api_base` is a private IPv4 or a `.svc.cluster.local` host, `model_info.mode` is
  unset or `chat`, and `model_info.hidden` is not `true`. To keep a route reachable by name but out
  of dsh and of Open WebUI's Local group (a fallback-only route, a route under test) set
  `model_info: { hidden: true }` — a YAML boolean, not a string. It still appears under Open WebUI's
  External group via discovery.
- **Fallbacks** are a separate `router_settings.fallbacks` line and resolve only against this
  proxy's `model_list`. New routes get none by default; that is a routing decision, make it
  explicitly.
- Do **not** add cloudlab routes to `litellm-local.yaml` (structurally local-only, the agentforge
  workers' proxy) or to `litellm-vkeys.yaml`.

## 3. Generate, verify, commit

```bash
just af-gen-litellm         # = python3 scripts/gen-litellm-consumers.py --write
just af-verify-litellm      # = ... --check ; what .gitea/workflows/manifests.yaml runs
python3 scripts/check-inline-hashes.py
python3 -m unittest discover -s scripts/tests -p "test_*.py"
git status --porcelain      # then git add the exact paths — never git add -A
```

`--write` reports each span it changed. Expect three: `open-webui.yaml` (`"0".model_ids`),
`settings.seed.yaml` (the `models:` rows) and `litellm.yaml` (`checksum/config`). If it refuses,
read the message: an empty selection, a non-boolean `hidden`, a `model_name` that does not
round-trip as a plain YAML string, a hand-edited line inside a generated span, or a non-canonical
`OPENAI_API_CONFIGS` value are all errors rather than silent rewrites. Review the diff, commit the
`litellm.yaml` entry and the three generated spans together, open the PR on Gitea.

## 4. After the merge

Gitea mirrors `main` to GitHub in a few seconds and Flux picks it up on its next reconcile
interval; each consumer then rolls on its own trigger:

| consumer | trigger | what to look at |
|---|---|---|
| LiteLLM | `checksum/config` changed → new pod template, rolling update (the Deployment in `litellm.yaml`: 2 replicas, `maxUnavailable: 0`) | `kubectl --context admin@ai -n ai get pods -l app.kubernetes.io/name=litellm` — both pods on the new template; then, in-cluster, `GET http://litellm.ai.svc:4000/v1/models` with the master key lists the name |
| Open WebUI | the env value changed → rollout | the picker shows the route under **Local**; `ENABLE_PERSISTENT_CONFIG=false`, so env is authoritative on every boot |
| dsh | the `dsh-relay` ConfigMap hash changed → the 1-replica `Recreate` Deployment rolls; the `seed-settings` init container splices the block | `kubectl --context admin@ai -n dsh logs deploy/dsh -c seed-settings` prints `was:` / `now:` id lists |

Then prove the route **through the proxy**, not only direct-to-backend: a greedy fixed prompt
(`Reply with exactly: OK`), a request in thinking mode that terminates (`finish_reason: stop`),
and a streamed request — cloudlab runbook §11 records why each of those has failed silently before.
Note that the proxy is ClusterIP-only: this step runs in-cluster (a pod, or `kubectl port-forward`),
not from a dev-worker.

## 5. Removing, hiding, renaming

- **Hide** (keep the name resolvable, drop it from dsh and from Open WebUI's Local group):
  `model_info: { hidden: true }`, then `just af-gen-litellm`. Open WebUI's External group still
  shows it.
- **Remove**: first check `router_settings.fallbacks` and known callers for the name; then delete
  the entry, regenerate, commit, merge, and confirm Flux has rolled the proxy and both consumers
  (step 4); only then stop the backend. Routing is restored before a backend is disabled, never
  the reverse — a local regeneration changes nothing live.
- **Rename**: don't, if chats reference it. Open WebUI stores the model id per chat and there is no
  prefix or alias layer by design; a renamed route strands every existing chat on that id.
- **Rename without stranding anyone -- two names, one backend**: keep the old entry and mark it
  `model_info: { hidden: true }`, add a second entry under the new `model_name` with **identical**
  `litellm_params`, then `just af-gen-litellm`. The pickers show only the new name; the old one
  keeps answering for every chat and session pinned to it. Done for `qwen3.8-27b-fp8-cloud` →
  `qwen3.8-27b-fp8-unc-cloud` on 2026-09-11 (the comment on those entries says why).
  The two `litellm_params` blocks must stay byte-identical: they are one deployment, and any
  drift makes "which name did you use" a behavioural question.

## 6. Traps this procedure was written against

- **The checksum recipe.** The annotation is sha256 of the `config.yaml` block **as `yq -r`
  extracts it**, first 12 hex — not of the bare YAML scalar. Two of today's commits stamped the
  wrong value; the generator stamps the right one and CI checks it.
- **A non-empty `model_ids` disables discovery for that Open WebUI connection**, and there is no
  wildcard. That is why the Local group is generated rather than discovered, and why the second
  connection exists.
- **dsh lists whatever the seed names and only fails when a user picks a dead id.** The generator
  cannot produce a name that is not in `model_list`; a route that is in `model_list` but not
  actually served still fails at selection time — a 404 for a served name the backend does not
  know, a connection error or timeout for a backend that is down, or a silent fallback if one is
  configured. The probe in step 4 is what catches that before a user does.
- **cloud3 holds one model at a time.** Every cloud3 route (`qwen3-coder-30b-a3b-cloud`,
  `qwen3.5-122b-cloud`, `qwen3.8-flash-next-cloud`, `qwen3.8-flash-next-q6-cloud`,
  `qwen3.8-27b-fp8-cloud`) shares one llama-swap; a request for a different one swaps the whole
  model (52–110 s). Do not fan an agent team across two cloud3 routes.
- **`--parallel 1` routes and abandoned requests.** The pinned LiteLLM (1.91.0) does not propagate a
  client's disconnect to the backend (`litellm.yaml`'s own note, measured 2026-08 in
  `ai-model-swap.md`), so an abandoned request keeps a single-slot backend busy until its
  generation ends or `request_timeout` (900 s) cuts it. Prefer `--parallel ≥ 2` on the host for
  anything users pick interactively.
- **Comments inside `config.yaml` change the checksum.** That is correct (the pod re-reads the
  file), just not obvious: a comment-only edit rolls LiteLLM.
