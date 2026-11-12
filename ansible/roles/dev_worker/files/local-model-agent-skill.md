---
name: local-model-agent
description: >-
  Run a task as an agent on the estate's local model (qwen-27b) through the LLM router, in the background,
  and collect its answer. Use when the user asks to spawn, start, run or delegate an agent "on qwen",
  "using qwen-27b" or "on a local model". Claude's own sub-agents (the Task tool) cannot do this: they run
  on Claude models only.
---

# Agents on the local models

This worker's router key reaches the estate's local model. It costs no subscription quota.

| model | where | speed | context | notes |
|---|---|---|---|---|
| `qwen-27b` | cloud2 and cloud3, vLLM (Qwen3.8-27B, the same checkpoint on both) | ~116 tok/s output, ~1,400 tok/s prompt | 262k | sees images; the router keeps each agent's conversation on one of the two machines |

`flash-next` was retired on 2026-11-05: use `qwen-27b`.

## How

The agent is a `local-agent` process: a Codex CLI run on the local model, with Codex as the harness that runs the
model's tool calls (shell, file edits) and talks to the router with this worker's key. Run it with the Bash tool and
`run_in_background: true`, one process per agent. Several can run at once.

```bash
out=/tmp/agent-$(date +%s)-$RANDOM
local-agent qwen-27b -C "$PWD" "<the task, self-contained>" > "$out.answer" 2> "$out.err"; echo "exit $? answer $out.answer err $out.err"
```

- The first argument is the model: `qwen-27b`.
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

- `403 ROUTE_NOT_ALLOWED`: the model name is not on this worker's key. Only `qwen-27b` is.
- `401`: the router key needs rotating. See docs/runbooks/dev-workers.md, "Codex through the router".
- `429 ... busy`: both machines are at their concurrency. Wait a minute and retry.
- The agent has no web search and cannot start sub-agents of its own: the router does not offer those tools on
  local models.
