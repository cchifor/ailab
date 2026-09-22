// VENDORED from https://github.com/Eyalm321/dsh-claude-cli-provider
//   commit e149de694b18194a7439d0ab18bc53ec21be27f6 (2026-09-10), MIT. Upstream path: src/index.js
//
// Claude on the estate's Max subscription, served by the Claude Code CLI this pod runs as a child
// process. ADR 0030 records why that shape is permitted where ADR 0029's was not; the credential
// never passes through this file -- claude-cli.sh puts it in the child's environment.
//
// WHY A FILE AND NOT AN npm PACKAGE: see claude-cli-translate.mjs.
//
// DEVIATIONS FROM UPSTREAM IN THIS FILE, each marked inline with "ESTATE DEVIATION":
//   1. the two relative import specifiers, renamed with the files;
//   2. a config key `images` (default true = upstream's behaviour). With `images: false` the
//      catalog declares inputModalities ['text'] and the turn does not materialise attachments,
//      saying so in the prompt instead. Upstream hardcodes ['text','image'] and writes the bytes
//      out for Claude to open with its Read tool -- which the text-only tier does not have, so
//      the catalog would claim a capability the route cannot honour.
//   3. an empty 'error' listener on the child's stdin, for a concern that did not reproduce --
//      see the comment at the listener, which says so rather than implying a fixed bug.
// Nothing else.
//
// WHAT THIS FILE DELIBERATELY DOES NOT DO. It does not handle the credential and it does not set
// the tool policy; both live in claude-cli.sh. Upstream spawns the child with the whole of
// process.env and blanks two variables inline, which would hand the Claude Code child
// LITELLM_API_KEY, CODEX_API_KEY and every DSH_* besides. The wrapper scrubs those before exec, so
// that is not patched here -- but note that the upstream line REMAINS below, and a future edit
// that removes the wrapper's scrub as "redundant" would restore exactly the leak it prevents.
//
// To re-vendor: diff against upstream at the new commit, re-apply these deviations and the
// sibling files', then bump the commit in all three headers together.
/**
 * dsh-claude-cli-provider — Claude through the local `claude` CLI.
 *
 * Auth is the CLI's own subscription OAuth: this plugin never reads a credential
 * file and never sends an API key. That is the whole point — it is the dsh
 * equivalent of OpenClaw's `claude-cli` agentRuntime, so a Claude planning/review
 * tier costs subscription usage instead of metered API tokens.
 */
import { spawn } from 'node:child_process';
import { collectImages, materialise, describeImages } from './claude-cli-images.mjs';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
import { createInterface } from 'node:readline';
import { LlmAdapter, LlmError } from '@deepseek-ai/dsh-llm';
import { translateEvent, finalChunks, renderPrompt } from './claude-cli-translate.mjs';

export const name = 'claude-cli-provider';
export const inject = ['llm'];

export const PROVIDER = 'claude-cli';
export const SETTINGS_NS = 'llm-claude-cli';

const DEFAULT_MODELS = [
  { id: 'claude-opus-5[1m]', name: 'Claude Opus 5 (1M context)', contextWindow: 1_000_000 },
  { id: 'claude-opus-5', name: 'Claude Opus 5', contextWindow: 200_000 },
  { id: 'claude-sonnet-5', name: 'Claude Sonnet 5', contextWindow: 200_000 },
  { id: 'claude-fable-5', name: 'Claude Fable 5', contextWindow: 200_000 },
];

class ClaudeCliAdapter extends LlmAdapter {
  constructor(options, deps = {}) {
    super();
    this.options = options;
    // Supplied by apply(): reads bytes for an ImageAttachmentRef. Without it, images
    // are reported as unavailable rather than silently dropped.
    this.readImage = deps.readImage;
  }

  providerInfo(provider) {
    return { id: provider, name: 'Claude (local CLI, subscription)' };
  }

  /**
   * The model catalog, in the shape the harness validates.
   *
   * Two things it rejects outright, and both were wrong here: every entry must carry `provider`
   * matching the route it was asked about, and the modality key is `inputModalities` — this
   * returned `modalities`, which is simply ignored. The whole catalog is discarded on the first
   * bad entry, so the UI listed no Claude models at all while turns kept working, because
   * `stream()` never consults the catalog. A capability can be broken and invisible for as long
   * as nobody opens the picker.
   *
   * Images are declared because this provider materialises them for `claude -p`: saying
   * text-only would be a negative capability claim, not a missing one.
   */
  async listModels(provider = PROVIDER) {
    return this.options.models.map((m) => ({
      provider,
      id: m.id,
      name: m.name,
      contextWindow: m.contextWindow,
      // ESTATE DEVIATION (upstream: an unconditional ['text', 'image']).
      inputModalities: this.options.images ? ['text', 'image'] : ['text'],
    }));
  }

  /**
   * One-generation call handle, required by dsh-llm >= 0.1.1.
   *
   * The base class grew this method and the runtime now calls it on EVERY dispatch, so an
   * adapter that inherits from an older copy of `@deepseek-ai/dsh-llm` — which is what a
   * `link:`ed plugin with its own `node_modules` does — kills the turn outright with
   * `registration.adapter.prepareCall is not a function`. Defining it here means the class
   * satisfies the contract whichever copy `LlmAdapter` came from, which is the only version
   * relationship this plugin can actually control: the app checkout's dsh always wins at
   * runtime, the plugin's dep is only what it was built against.
   *
   * Same body as the base implementation: resolve the exact model, then hand back a stream
   * entry point bound to this adapter generation.
   */
  async prepareCall(provider, model, signal) {
    return {
      model: await this.resolveModel(provider, model, signal),
      stream: (options) => this.stream(options),
    };
  }

  async *stream(options) {
    const trace = process.env.CLAUDE_CLI_TRACE
      ? (m) => { try { require('node:fs').appendFileSync(process.env.CLAUDE_CLI_TRACE, `${Date.now()} ${m}\n`); } catch {} }
      : () => {};
    const { command, timeoutMs, cwd, extraArgs, isolateTools, observeTools, images } = this.options;
    // `claude -p` is itself an agent: left alone it runs ITS OWN tool loop with
    // ITS OWN MCP servers, ignoring the tool schemas dsh passed us. That produces
    // confusing "Claude requested permissions to use mcp__..." failures inside a
    // dsh turn. So by default we strip Claude's tooling and use it as a pure
    // reasoning/text tier; dsh's own runtime (DeepSeek et al) owns tool execution.
    // Per-call wins over adapter config; adapter config is what a profile actually sets.
    // (These were read from the wrong object until 2026-08-20, so the adapter setting was
    // silently ignored and Claude always kept its own tools.)
    const isolated = options.isolateTools ?? isolateTools;
    const isolate = isolated
      ? ['--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}']
      : [];
    const args = [
      '-p',
      '--output-format', 'stream-json',
      '--verbose',                       // required with -p + stream-json
      '--model', options.model,
      ...isolate,
      ...extraArgs,
    ];

    // Images arrive as opaque attachment refs; claude -p takes text. Write the bytes to a
    // private per-turn directory and name the paths in the prompt — Claude Code reads image
    // files with its own tools, which is exactly the non-isolated mode.
    // ESTATE DEVIATION. Upstream always materialises. Writing the bytes out and naming the
    // paths only helps when Claude can open them, which needs its Read tool; on the text-only
    // tier it has none, so this would spend I/O telling the model to read files it cannot.
    const attachments = collectImages(options.messages);
    const media = images
      ? await materialise(attachments, this.readImage)
      : { dir: null, files: [], failed: 0, cleanup: async () => {} };
    if (media.files.length || media.failed) {
      trace(`images: ${media.files.length} materialised, ${media.failed} unreadable`);
    } else if (!images && attachments.length) {
      trace(`images: ${attachments.length} attached, not materialised (text-only route)`);
    }

    trace(`isolated=${isolated} optionsIsolate=${options.isolateTools} adapterIsolate=${isolateTools} tools=${(options.tools||[]).length}`);
    trace(`spawn ${args.join(' ').slice(0,160)}`);
    const child = spawn(command, args, {
      cwd: cwd || process.cwd(),
      stdio: ['pipe', 'pipe', 'pipe'],
      // Strip any API key so a stray env var cannot silently switch this to metered billing.
      env: { ...process.env, ANTHROPIC_API_KEY: '', ANTHROPIC_AUTH_TOKEN: '' },
    });

    const state = { nextIndex: 0, usage: undefined, stopReason: undefined, sawResult: false, errorText: undefined , ownsToolLoop: isolated,
      observeTools: options.observeTools ?? observeTools };
    let stderr = '';
    child.stderr.on('data', (d) => { stderr += String(d); });

    // `timeoutMs` is an IDLE timeout, not a wall on the whole turn: the timer is re-armed on
    // every byte the child writes to stdout, so a turn that is still streaming is never killed
    // however long it runs, while one that has genuinely hung still is.
    //
    // It was an absolute wall until 2026-08-21, when a healthy agentic turn was SIGKILLed at
    // exactly 600s mid-stream. Chunks had been arriving right up to the kill (11:50:38,
    // 11:52:21, 11:52:50, and one as it died at 11:54:10) and every token it had produced was
    // discarded, surfacing to the user as `claude CLI exited null`.
    // Stop has to reach the subprocess. The harness cancels a turn by aborting the adapter's
    // signal, and `claude -p` is a child process that knows nothing about it: without this the
    // harness stops listening while the child runs on, spending tokens on output nobody reads
    // and holding the session busy. Pressing Stop looked like it did nothing because, outside
    // the harness, nothing had changed.
    const signal = options.signal;
    let aborted = signal?.aborted === true;
    let onAbort;

    let killedIdle = false;
    let timer;
    const idleLabel = timeoutMs >= 1000 ? `${Math.round(timeoutMs / 1000)}s` : `${timeoutMs}ms`;
    const rearm = () => {
      if (!timeoutMs) return;
      clearTimeout(timer);
      timer = setTimeout(() => {
        killedIdle = true;
        trace(`idle timeout: no stdout for ${idleLabel}, killing`);
        child.kill('SIGKILL');
      }, timeoutMs);
    };

    // readline is created here, and the raw progress listener attached in the same tick, so
    // stdout is never resumed before readline is listening. Attaching the listener first would
    // put the stream in flowing mode and lose the head of the turn.
    if (aborted) child.kill('SIGKILL');            // cancelled before the spawn settled
    else if (signal) {
      onAbort = () => {
        aborted = true;
        trace('aborted by caller: killing the CLI');
        child.kill('SIGKILL');
      };
      signal.addEventListener('abort', onAbort, { once: true });
    }

    const rl = createInterface({ input: child.stdout, crlfDelay: Infinity });
    child.stdout.on('data', rearm);
    rearm();

    // ESTATE DEVIATION. A raised concern that did NOT reproduce, kept as insurance and labelled
    // honestly so nobody later "confirms" a bug that was never shown.
    //
    // The concern: claude-cli.sh exits 78 when the pinned binary is missing -- this design's own
    // degraded state -- so the write below can land on an already-closed pipe, and an unhandled
    // 'error' on a stream is a process-level throw, which on a 1-replica Recreate Deployment is
    // the web UI going down. MEASURED in this pod on node 24 (2026-09-22): a child that exits
    // immediately followed by a 200 KB stdin write does NOT raise an uncaught exception, with or
    // without this listener. The listener costs nothing and removes the question on a future
    // runtime; the real reporting of that case is the exit code and stderr handled further down.
    child.stdin.on('error', () => {});

    // one-shot prompt on stdin, then EOF
    // ESTATE DEVIATION. Said plainly rather than dropped silently: a model asked about a
    // screenshot it was never given answers the wrong question confidently.
    const described = images
      ? describeImages(media.files, media.failed)
      : (attachments.length
          ? `[${attachments.length} image${attachments.length > 1 ? 's were' : ' was'} attached. This route is text-only and cannot read ${attachments.length > 1 ? 'them' : 'it'}.]`
          : '');
    child.stdin.end(renderPrompt(options.messages, options.system) + (described ? `\n\n${described}` : ''));

    const exited = new Promise((resolve) => child.on('close', (code, signal) => resolve({ code, signal })));

    try {
      for await (const line of rl) {
        const trimmed = line.trim();
        if (!trimmed || trimmed[0] !== '{') continue;
        let event;
        try { event = JSON.parse(trimmed); } catch { continue; }  // tolerate non-JSON noise
        trace(`recv ${event.type}${event.subtype ? '/' + event.subtype : ''}`);
        for (const chunk of translateEvent(event, state)) { trace(`emit ${chunk.type}`); yield chunk; }
      }

      const { code, signal } = await exited;
      // Our own kill is named as such, with the idle duration. Anything else (including a
      // SIGKILL from outside this process) reports the raw exit and must not be dressed up as
      // a timeout, or a crash gets misdiagnosed as a slow turn.
      // A cancelled turn is not a failing CLI, and must not be dressed up as an idle timeout:
      // the child produced nothing because we killed it.
      if (aborted) throw new LlmError('claude CLI turn aborted by caller', 'ABORTED');
      if (killedIdle) {
        throw new Error(
          `claude CLI killed after ${idleLabel} with no output (idle timeout)` +
            (stderr ? `: ${stderr.slice(-500)}` : ''),
        );
      }
      if (state.errorText) throw new Error(`claude CLI: ${state.errorText}`);
      if (code !== 0) {
        const how = `${code}${signal ? ` (signal ${signal})` : ''}`;
        throw new Error(`claude CLI exited ${how}${stderr ? `: ${stderr.slice(-500)}` : ''}`);
      }
      for (const chunk of finalChunks(state)) yield chunk;
    } finally {
      await media.cleanup();
      if (signal && onAbort) signal.removeEventListener('abort', onAbort);
      clearTimeout(timer);
      if (child.exitCode === null) child.kill('SIGKILL');
    }
  }
}

export function resolveOptions(config = {}) {
  return {
    command: config.command ?? 'claude',
    // Idle timeout: milliseconds with no stdout from the CLI before it is killed. Not a
    // ceiling on turn duration, since a streaming turn may run indefinitely.
    timeoutMs: config.timeoutMs ?? 600_000,
    cwd: config.cwd ?? '',
    isolateTools: config.isolateTools ?? true,
    // When Claude owns the tool loop its work is invisible to the harness: a request and a
    // reply with nothing in between. Recording its calls is the default, because an action
    // nothing recorded is indistinguishable from one that never happened.
    observeTools: config.observeTools ?? true,
    // ESTATE DEVIATION. Upstream has no such key and always claims image support. With
    // the tool-less tier the model has no way to OPEN a materialised file, so claiming it would
    // be a capability the route cannot honour. Default true = upstream behaviour.
    images: config.images ?? true,
    extraArgs: config.extraArgs ?? [],
    models: config.models?.length ? config.models : DEFAULT_MODELS,
  };
}

export function apply(ctx, config) {
  const options = resolveOptions(config);
  // The attachment service is resolved lazily: it is composed by the host and may register
  // after this plugin. Resolving per call also means a deployment without attachments simply
  // reports images as unavailable instead of failing to mount.
  const readImage = async (ref) => {
    const store = ctx.get('attachments');
    if (!store?.readImage) throw new Error('no attachment service');
    const out = await store.readImage(ref);
    return out?.data ?? out;
  };
  const adapter = new ClaudeCliAdapter(options, { readImage });

  // Shape is fixed by dsh-llm's commit(): provider/displayName/settingsNs/settingsPath,
  // each non-empty. (Not {id,name,models} — that throws INVALID_DIRECTORY.)
  ctx.llm.registerConfigurableProviders?.([
    {
      provider: PROVIDER,
      displayName: 'Claude (local CLI, subscription)',
      settingsNs: SETTINGS_NS,
      settingsPath: [],
    },
  ]);
  ctx.llm.registerAdapter([PROVIDER], adapter);
}

export { ClaudeCliAdapter, DEFAULT_MODELS };
