#!/usr/bin/env node
/**
 * Claude Code status line.
 *
 * Reads the session JSON that Claude Code writes to stdin and prints one
 * flowing sequence of segments: model/effort, context window, 5h + 7d
 * subscription limits, session spend, then location. Segments are packed onto
 * a line until the next one no longer fits, then wrap to the line below.
 *
 * Wired up via the `statusLine` key in ~/.claude/settings.json.
 */

const C = {
  reset: '\x1b[0m',
  dim: '\x1b[2m',
  bold: '\x1b[1m',
  red: '\x1b[31m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  blue: '\x1b[34m',
  magenta: '\x1b[35m',
  cyan: '\x1b[36m',
  gray: '\x1b[90m',
};

const DOT = `${C.gray} · ${C.reset}`;
const SEP = `${C.gray} │ ${C.reset}`;
// Claude Code renders the status line with paddingX 0, so the whole terminal
// width is usable; keep one column of slack for ambiguous-width glyphs.
const WIDTH = Math.max(40, (parseInt(process.env.COLUMNS, 10) || 120) - 1);

const visibleLength = (s) => s.replace(/\x1b\[[0-9;]*m/g, '').length;

// Green until `warn`, yellow until `bad`, red past it.
function heat(pct, warn, bad) {
  if (pct >= bad) return C.red;
  if (pct >= warn) return C.yellow;
  return C.green;
}

function bar(pct, width, color) {
  const filled = Math.min(width, Math.max(0, Math.round((pct / 100) * width)));
  return `${color}${'█'.repeat(filled)}${C.gray}${'░'.repeat(width - filled)}${C.reset}`;
}

function tokens(n) {
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, '')}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`;
  return String(n);
}

// "2h51m", "1d3h", "12m" — coarse enough to stay short at any horizon.
function until(epochSeconds) {
  const secs = epochSeconds - Math.floor(Date.now() / 1000);
  if (secs <= 0) return 'now';
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (d > 0) return `${d}d${h}h`;
  if (h > 0) return `${h}h${m}m`;
  return `${m}m`;
}

function duration(ms) {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${h}h${m}m`;
  if (m > 0) return `${m}m${s % 60}s`;
  return `${s}s`;
}

function limitSegment(label, limit) {
  const pct = Math.round(limit.used_percentage);
  const color = heat(pct, 60, 85);
  const resets = limit.resets_at ? `${C.gray} ↻${until(limit.resets_at)}${C.reset}` : '';
  return `${C.dim}${label}${C.reset} ${bar(pct, 10, color)} ${color}${String(pct).padStart(2)}%${C.reset}${resets}`;
}

/** Greedily pack segments into lines no wider than `width`. */
function flow(segments, width) {
  const sepLen = visibleLength(SEP);
  const lines = [];
  let line = '';
  let len = 0;
  for (const seg of segments) {
    const segLen = visibleLength(seg);
    if (line === '') {
      line = seg;
      len = segLen;
    } else if (len + sepLen + segLen <= width) {
      line += SEP + seg;
      len += sepLen + segLen;
    } else {
      lines.push(line);
      line = seg;
      len = segLen;
    }
  }
  if (line !== '') lines.push(line);
  return lines;
}

function build(d) {
  const segments = [];

  const model = (d.model?.display_name || d.model?.id || 'claude').replace(
    /\s*\(1M context\)/,
    ` ${C.cyan}1M${C.reset}`,
  );
  let ident = `${C.bold}${C.magenta}●${C.reset} ${C.bold}${model}${C.reset}`;
  if (d.effort?.level) ident += `${DOT}${C.dim}${d.effort.level}${C.reset}`;
  if (d.fast_mode) ident += `${DOT}${C.yellow}⚡fast${C.reset}`;
  if (d.thinking?.enabled === false) ident += `${DOT}${C.dim}no-think${C.reset}`;
  if (d.output_style?.name && d.output_style.name !== 'default') {
    ident += `${DOT}${C.dim}${d.output_style.name}${C.reset}`;
  }
  segments.push(ident);

  const ctx = d.context_window;
  if (ctx && ctx.context_window_size) {
    const pct = Math.round(ctx.used_percentage ?? 0);
    const color = heat(pct, 60, 85);
    let seg =
      `${C.dim}ctx${C.reset} ${bar(pct, 10, color)} ${color}${pct}%${C.reset}` +
      `${C.gray} ${tokens(ctx.total_input_tokens ?? 0)}/${tokens(ctx.context_window_size)}${C.reset}`;
    if (d.exceeds_200k_tokens) seg += ` ${C.yellow}200k+${C.reset}`;
    segments.push(seg);
  }

  const rl = d.rate_limits;
  if (rl?.five_hour) segments.push(limitSegment('5h', rl.five_hour));
  if (rl?.seven_day) segments.push(limitSegment('7d', rl.seven_day));

  const cost = d.cost;
  if (cost) {
    const bits = [];
    if (typeof cost.total_cost_usd === 'number') {
      bits.push(`${C.green}$${cost.total_cost_usd.toFixed(2)}${C.reset}`);
    }
    if (cost.total_lines_added || cost.total_lines_removed) {
      bits.push(
        `${C.green}+${cost.total_lines_added || 0}${C.reset}${C.gray}/${C.reset}${C.red}-${cost.total_lines_removed || 0}${C.reset}`,
      );
    }
    if (cost.total_duration_ms) bits.push(`${C.gray}${duration(cost.total_duration_ms)}${C.reset}`);
    if (bits.length) segments.push(bits.join(' '));
  }

  // Location trails everything else so the readouts keep fixed columns.
  const repo = d.workspace?.repo;
  const place = repo
    ? `${C.blue}${repo.owner}/${repo.name}${C.reset}`
    : `${C.blue}${(d.workspace?.current_dir || d.cwd || '').split(/[\\/]/).pop()}${C.reset}`;
  const branch = d.worktree?.branch;
  segments.push(branch ? `${place}${C.gray}@${C.reset}${C.cyan}${branch}${C.reset}` : place);

  return segments;
}

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => (buf += chunk));
process.stdin.on('end', () => {
  try {
    process.stdout.write(flow(build(JSON.parse(buf)), WIDTH).join('\n'));
  } catch (err) {
    // A status line must never take the session down; degrade to a hint.
    process.stdout.write(`${C.red}statusline: ${String(err.message).slice(0, 120)}${C.reset}`);
  }
});
