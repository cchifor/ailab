// kit/claude-replica.mjs SPEC OUT - Layer (a) for the Claude seat: a faithful replica of llm-router 247b19f's
// sdk-resume launch (packages/adapters/claude-sdk streamClaudeSdk + seat-host streamClaudeCli) for a text task without
// tools: the conversation written as a synthetic transcript, `--resume <file> --fork-session`, the router's flags and
// env, stdin held open until the result. Run in docker with ANTHROPIC_BASE_URL pointing at the recording tap, so the
// Messages request Claude Code sends to Anthropic is recorded for the replica exactly as for the direct `claude -p`.
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
const [specFile, out] = process.argv.slice(2);
const spec = JSON.parse(fs.readFileSync(specFile, 'utf8'));
const dir = fs.mkdtempSync('/h/llm-router-sdk-');
const version = process.env.CC_VERSION ?? '2.1.281', model = spec.model;
const messages = [{ role: 'user', content: spec.turns[0] }]; // toAnthropicInput keeps a string content a string
const sessionId = randomUUID(); let parentUuid = null;
const rows = messages.map((m) => { const uuid = randomUUID(); const row = { type: m.role, uuid, parentUuid, sessionId, cwd: dir, version, timestamp: new Date().toISOString(), isSidechain: false, message: m }; parentUuid = uuid; return row; });
const file = `${dir}/history.jsonl`; fs.writeFileSync(file, rows.map((r) => JSON.stringify(r)).join('\n') + '\n', { mode: 0o600 });
const LOCKED = ['--setting-sources', '', '--settings', '{"disableAllHooks":true}'];
const args = ['-p', '--model', model, '--output-format', 'stream-json', '--verbose', '--include-partial-messages', '--input-format', 'stream-json', '--tools', '', '--strict-mcp-config', '--mcp-config', JSON.stringify({ mcpServers: {} }), '--allowedTools', 'mcp__router__*', '--disable-slash-commands', '--no-session-persistence', ...LOCKED, '--resume', file, '--fork-session', '--permission-prompt-tool', 'stdio', '--max-turns', '1', '--system-prompt', spec.system];
const child = spawn('/cc/node_modules/@anthropic-ai/claude-code-linux-x64/claude', args, { cwd: dir, stdio: ['pipe', 'pipe', 'pipe'],
  env: { PATH: '/usr/local/bin:/usr/bin:/bin', HOME: '/h', CLAUDE_CONFIG_DIR: '/h', LANG: 'C.UTF-8', CI: 'true', DISABLE_AUTOUPDATER: '1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1',
    CLAUDE_CODE_RESUME_INTERRUPTED_TURN: '1', ENABLE_TOOL_SEARCH: 'false', CLAUDE_CODE_OAUTH_TOKEN: process.env.CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_BASE_URL: process.env.ANTHROPIC_BASE_URL } });
const log = fs.createWriteStream(out);
let buf = '';
child.stdout.on('data', (c) => { log.write(c); buf += c; if (/"type":"result"/.test(buf)) child.stdin.end(); });
child.stderr.on('data', (c) => log.write(c));
const timer = setTimeout(() => child.kill('SIGKILL'), 180000);
child.on('close', (code) => { clearTimeout(timer); log.end(); console.log('exit', code); });
