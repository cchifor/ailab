// VENDORED from https://github.com/Eyalm321/dsh-claude-cli-provider
//   commit e149de694b18194a7439d0ab18bc53ec21be27f6 (2026-09-10), MIT. Upstream path: src/translate.js
//
// WHY A FILE AND NOT AN npm PACKAGE. A cordis row's `name:` resolves from the PROFILE
// directory, whose node_modules is a symlink farm of dsh's OWN dependency closure. A package
// installed beside it throws MODULE_NOT_FOUND, boot() rethrows, and a 1-replica Recreate
// Deployment CRASH-LOOPS -- the web UI goes down, it does not degrade. searxng-search.mjs and
// openbao-credentials.mjs are here for the same reason; docs/runbooks/dsh.md calls it "the single
// most expensive lesson in this runbook".
//
// The one bare import in this family, `@deepseek-ai/dsh-llm`, IS in dsh's closure -- verified on
// the live pod 2026-09-22: it resolves from /dsh-home/profiles/web and exposes LlmAdapter,
// LlmError and LlmAdapter.prototype.prepareCall.
//
// DEVIATIONS FROM UPSTREAM IN THIS FILE: none (byte-identical).
//
// To re-vendor: diff against upstream at the new commit, re-apply the deviations listed above and
// in the sibling files, then bump the commit in all three headers together.
/**
 * Pure translation: `claude -p --output-format stream-json` events -> dsh StreamChunks.
 * Kept free of I/O so it is unit-testable against fixtures.
 */

/** Map Claude's stop_reason onto dsh's FinishReason vocabulary. */
export function finishReasonOf(stopReason) {
  switch (stopReason) {
    case 'end_turn':
    case 'stop_sequence':
      return 'stop';
    case 'max_tokens':
      return 'length';
    case 'tool_use':
      return 'tool-calls';
    default:
      return 'stop';
  }
}

/** Claude usage -> dsh TokenUsage (cache reads/writes folded into input). */
export function usageOf(u = {}) {
  const input =
    (u.input_tokens ?? 0) +
    (u.cache_creation_input_tokens ?? 0) +
    (u.cache_read_input_tokens ?? 0);
  return { inputTokens: input, outputTokens: u.output_tokens ?? 0 };
}


/** How much of a tool argument or result is worth keeping in the log. */
export const OBSERVE_LIMIT = 800;

/** Argument names that carry the action itself, most specific first. */
export const PRIMARY = ['command', 'file_path', 'path', 'pattern', 'query', 'url', 'prompt', 'content'];

/** Trim to `limit`, saying how much was cut rather than trailing off into an ellipsis. */
export function clip(text, limit = OBSERVE_LIMIT) {
  const s = String(text ?? '');
  if (s.length <= limit) return s;
  return `${s.slice(0, limit)}… [${s.length - limit} more chars]`;
}

/**
 * One line describing a tool call Claude made inside its own loop.
 *
 * Readable first: `Bash: git status` beats a JSON dump of {"command":"git status"}. A single
 * string argument is the whole story for most tools, so it is shown bare; anything else keeps
 * its JSON, because guessing which key matters loses information.
 */
export function describeToolUse(block) {
  const name = block?.name ?? 'tool';
  const input = block?.input;
  if (input == null) return name;
  const keys = input && typeof input === 'object' ? Object.keys(input) : [];
  if (keys.length === 1 && typeof input[keys[0]] === 'string') {
    return `${name}: ${clip(input[keys[0]])}`;
  }
  // Most tools have one argument that IS the action and others that merely describe it —
  // Bash carries `command` plus a `description` written for a human. Showing the raw JSON
  // buries the command inside the commentary, so lead with the argument that matters and
  // append the rest only when there is more worth knowing.
  const lead = PRIMARY.find((k) => typeof input[k] === 'string' && input[k]);
  if (lead) {
    const rest = keys.filter((k) => k !== lead && k !== 'description');
    const tail = rest.length ? ` (+${rest.join(', ')})` : '';
    return `${name}: ${clip(input[lead], OBSERVE_LIMIT - tail.length)}${tail}`;
  }
  return `${name}: ${clip(safeJson(input))}`;
}

/** Chunks that record something without asking the harness to act on it. */
function observation(state, text) {
  const index = state.nextIndex++;
  return [
    { type: 'block-start', index, blockType: 'reasoning' },
    { type: 'reasoning-delta', index, text },
    { type: 'block-end', index, block: { type: 'reasoning', text } },
  ];
}

/**
 * Translate one parsed stream-json event into zero or more StreamChunks.
 * `state` carries the running block index across calls.
 */
export function translateEvent(event, state) {
  const out = [];
  if (!event || typeof event !== 'object') return out;

  // Only assistant turns carry model content. `system` (init/hooks) is noise here;
  // `result` closes the turn.
  if (event.type === 'assistant' && event.message) {
    const msg = event.message;
    for (const block of msg.content ?? []) {
      if (block.type === 'text' && block.text) {
        const index = state.nextIndex++;
        out.push({ type: 'block-start', index, blockType: 'text' });
        out.push({ type: 'text-delta', index, text: block.text });
        out.push({ type: 'block-end', index, block: { type: 'text', text: block.text } });
      } else if (block.type === 'thinking' && block.thinking) {
        const index = state.nextIndex++;
        out.push({ type: 'block-start', index, blockType: 'reasoning' });
        out.push({ type: 'reasoning-delta', index, text: block.thinking });
        out.push({ type: 'block-end', index, block: { type: 'reasoning', text: block.thinking } });
      } else if (block.type === 'tool_use') {
        // Only forward a tool call when the harness is the one that will run it.
        //
        // With tools isolated, `claude -p` has none of its own, so any tool_use it emits
        // belongs to the harness and must be passed along. With tools NOT isolated, Claude
        // owns its loop and has ALREADY executed this call itself — forwarding it asks the
        // harness to execute a tool it does not have, and the turn then waits for a result
        // that will never arrive. That is two executors for one call, and it was the cause
        // of tool-using turns hanging for minutes while non-tool turns returned in seconds.
        if (state.ownsToolLoop === false) {
          // Claude has already run this itself. Forwarding it as a tool call asks the harness
          // to execute a tool it does not have, and the turn hangs waiting for a result that
          // never comes (ab809af). Recording it as an observation keeps the move visible —
          // the whole point of a pane per agent — without asking anyone to execute anything.
          // blockText() drops reasoning blocks, so this never returns as input on a later turn.
          if (state.observeTools !== false) out.push(...observation(state, `↪ ${describeToolUse(block)}`));
          continue;
        }
        const index = state.nextIndex++;
        const args = JSON.stringify(block.input ?? {});
        out.push({ type: 'block-start', index, blockType: 'tool-call' });
        out.push({ type: 'tool-call-delta', index, argumentsDelta: args });
        out.push({
          type: 'block-end',
          index,
          block: { type: 'tool-call', id: block.id, name: block.name, arguments: args },
        });
      }
    }
    if (msg.usage) {
      state.usage = usageOf(msg.usage);
    }
    if (msg.stop_reason) {
      state.stopReason = msg.stop_reason;
    }
  }

  // `claude -p` reports the outcome of its own tool calls as user messages carrying
  // tool_result blocks, which were dropped on the floor. A move without its outcome is half a
  // record, so those are observed too when Claude owns the loop.
  if (event.type === 'user' && event.message && state.ownsToolLoop === false && state.observeTools !== false) {
    for (const block of event.message.content ?? []) {
      if (block?.type !== 'tool_result') continue;
      const body = clip(renderToolResult(block.content));
      const label = block.is_error ? '⚠ tool failed' : '↩';
      out.push(...observation(state, body ? `${label} ${body}` : `${label} (no output)`));
    }
  }

  if (event.type === 'result') {
    if (event.usage) state.usage = usageOf(event.usage);
    state.sawResult = true;
    if (event.is_error) state.errorText = event.result ?? 'claude CLI reported an error';
  }

  return out;
}

/** Terminal chunks emitted once the process ends cleanly. */
export function finalChunks(state) {
  const out = [];
  if (state.usage) out.push({ type: 'usage', usage: state.usage });
  out.push({ type: 'finish', reason: finishReasonOf(state.stopReason) });
  return out;
}

/** Flatten dsh Messages into a single prompt for the CLI's one-shot print mode. */
/**
 * Flatten one tool result into text.
 *
 * Tool results carry a block ARRAY, not a string. `String(content)` on that yields
 * "[object Object]" — which is what Claude was being handed for every tool result in the
 * transcript, and it noticed. Text blocks render as their text; anything else is kept as
 * JSON so the information survives rather than becoming a placeholder.
 */
export function renderToolResult(content) {
  if (content == null) return '';
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((c) => (typeof c === 'string' ? c : c?.type === 'text' ? (c.text ?? '') : safeJson(c)))
      .filter(Boolean)
      .join('\n');
  }
  return safeJson(content);
}

function safeJson(value) {
  try { return JSON.stringify(value); } catch { return ''; }
}

/** The text a single content block contributes to the rendered prompt. */
export function blockText(b) {
  if (b?.type === 'text') return b.text ?? '';
  if (b?.type === 'tool-result') return renderToolResult(b.content);
  return '';
}

export function renderPrompt(messages = [], system) {
  const parts = [];
  if (system) parts.push(`<system>\n${system}\n</system>`);
  for (const m of messages) {
    const text = (m.content ?? [])
      .map(blockText)
      .filter(Boolean)
      .join('\n');
    if (!text) continue;
    parts.push(m.role === 'assistant' ? `<assistant>\n${text}\n</assistant>` : text);
  }
  return parts.join('\n\n');
}
