---
name: local-model-agent
description: Run a task as an agent on one of the estate's local models (qwen-27b, flash-next) through the LLM router, in the background, and collect its answer. Use when the user asks to spawn, start, run or delegate an agent "on qwen", "using qwen-27b", "with flash-next" or "on a local model". Claude's own sub-agents (the Task tool) cannot do this: they run on Claude models only.
---

# Agents on the local models

This worker's router key reaches two local models. They cost no subscription quota.

| model | where | speed | context | notes |
|---|---|---|---|---|
| `qwen-27b` | cloud2, vLLM (Qwen3.8-27B) | ~116 tok/s output, ~1,400 tok/s prompt | 262k | sees images; the better local choice for coding work |
| `flash-next` | cloud3, llama.cpp (Qwen3.8-Flash-Next) | ~60 tok/s output, ~1,700 tok/s prompt | 262k | sees images; copies exact values from tool output unreliably (times, numbers, paths): check what it reports |

cloud3 serves one model at a time, so the first `flash-next` request after another cloud3 model waits for the swap
(up to a minute or two).

## How

The agent is a `local-agent` process: a Codex CLI run on the local model, with Codex as the harness that runs the
model's tool calls (shell, file edits) and talks to the router with this worker's key. Run it with the Bash tool and
`run_in_background: true`, one process per agent. Several can run at once.

```bash
out=/tmp/agent-$(date +%s)-$RANDOM
local-agent qwen-27b -C "$PWD" "<the task, self-contained>" > "$out.answer" 2> "$out.err"; echo "exit $? answer $out.answer err $out.err"
```

- The first argument picks the model: `qwen-27b` or `flash-next`.
- `-C <dir>` is the directory the agent works in. It can write only there and in temp directories.
- When the background command finishes, its output names the files: read `$out.answer` for the agent's final
  message. `$out.err` names the transcript (the commands the agent ran and their output), kept for 3 days.

## Writing the task

- Make it self-contained: the agent sees only the prompt and the directory, not this conversation. State the goal,
  the files involved, what "done" means, and that it should end with a short summary of what it changed.
- Keep tasks scoped. These models are weaker than Claude and GPT-6 at long multi-step work. Prefer several small
  agents over one large one, and check their changes (`git diff`) before building on them.
- Never put secrets, tokens or `cred` output in the prompt.

## When it fails

- `403 ROUTE_NOT_ALLOWED`: the model name is not on this worker's key. Only `qwen-27b` and `flash-next` are.
- `401`: the router key needs rotating. See docs/runbooks/dev-workers.md, "Codex through the router".
- A timeout on `flash-next` right after another cloud3 model: the swap. Retry once.
- The agent has no web search and cannot start sub-agents of its own: the router does not offer those tools on
  local models.
