// tasks/registry.mjs - the task registry: tasks are data. A provider runs every task whose `kind` it supports; the
// grader named by `grader` (tasks/graders.mjs) decides pass/fail. Adding a task is one entry here (plus its fixture
// under tasks/fixtures/ when it needs a workspace); `gate: false` keeps a task out of the default release gate (run it
// with --tasks <id>).
//
// `known` declares a measured, understood deviation that is accepted until a named fix lands, per margin id
// (core/margins.mjs): { bound, reason }. The metric is judged against `bound` instead of the §9.6 bound; the report
// prints the reason, and says so once the deviation no longer shows. Every entry names its measurement and its fix.
//
// Kinds:
//   codex-exec      one `codex exec` session in a fresh copy of `fixture` (prompt), optionally resumed (`turns`)
//   chat            a chat conversation of `turns` user messages, optional `tools` (function definitions)
//   claude-agentic  an agent loop over the shared workspace tools (kit/tools.mjs) in a copy of `fixture`
export const TASKS = [
  {
    id: 'code-edit', kind: 'codex-exec', providers: ['codex', 'codex-control', 'qwen', 'qwen-cloud3', 'qwen-cloud1'], gate: true, fixture: 'calc', cap_min: 8,
    prompt: 'The unit tests in `test_calc.py` fail. Find the bugs in `calc.py` and fix them so that `python3 -m unittest -q test_calc` passes. Do not modify `test_calc.py`. Run the tests to confirm, then reply with one line per bug you fixed.',
    grader: { name: 'python-unittest', module: 'test_calc', protect: ['test_calc.py'] },
  },
  {
    id: 'secret-continuity', kind: 'codex-exec', providers: ['codex'], gate: true, fixture: null, cap_min: 4,
    prompt: 'This is a memory test across two turns. Privately, in your own reasoning only, choose a random 6-digit number (not 123456 or any other obvious pattern). Do not write the number anywhere in your reply and do not run any command. Reply with exactly one line: SUM=<the sum of the number\'s six digits>',
    turns: ['Now reveal the 6-digit number you chose in the previous turn. Do not run any command. Reply with exactly one line: NUM=<the number>. If you cannot recall it, reply exactly: NUM=UNKNOWN'],
    grader: { name: 'secret-sum' },
  },
  {
    id: 'math', kind: 'chat', providers: ['claude'], gate: true,
    turns: ['How many integers from 1 to 1000 inclusive are divisible by 3 or by 5, but not by 15? Reply with exactly one line: ANSWER=<number>'],
    grader: { name: 'regex-equals', pattern: 'ANSWER=(\\d+)', expect: '401' },
  },
  {
    id: 'tool-choice', kind: 'chat', providers: ['claude'], gate: true, tools: 'catalog',
    turns: ['How much is 250 euros in Japanese yen right now?'],
    grader: { name: 'tool-call', tool: 'convert_currency', args: { amount: 250, from_currency: 'EUR', to_currency: 'JPY' } },
  },
  {
    id: 'recall', kind: 'chat', providers: ['claude'], gate: true,
    turns: ['My project code is ZEBRA-42-QX. Please remember it. Reply with only the word: noted', 'What is my project code written backwards, character by character? Reply with exactly one line: CODE=<reversed code>'],
    grader: { name: 'regex-equals', pattern: 'CODE=(\\S+)', expect: 'XQ-24-ARBEZ' },
  },
  {
    id: 'stock-agentic', kind: 'claude-agentic', providers: ['claude'], gate: true, fixture: 'stock', hidden: 'stock-hidden', max_steps: 40, cap_min: 10,
    grader: { name: 'node-hidden-tests', protect: ['tests'] },
    known: {
      // baseline 2026-11-05 (image 78915e24): 1.28-1.37x over 3 pairs, the same 5 model calls and 10 tools in both arms.
      // The sdk-resume transport starts one Claude Code process per request, about +1.6 s before the first content of
      // every call. The Claude real-session design removes it; retire this entry when it lands.
      wall: { bound: 1.5, reason: 'sdk-resume starts one Claude Code process per request (about +1.6 s a model call); removed by the real-session design' },
    },
  },
  {
    // the long probe: 10 behaviours discoverable only one failure at a time (15-40 dependent calls)
    id: 'textkit-agentic', kind: 'claude-agentic', providers: ['claude'], gate: false, fixture: 'textkit', hidden: null, max_steps: 40, cap_min: 15,
    grader: { name: 'node-hidden-tests', protect: ['tests'] },
  },
];

/** The function catalog for `tools: 'catalog'` chat tasks. */
export const CATALOG = [
  { type: 'function', function: { name: 'get_weather', description: 'Current weather for a city: conditions (rain, snow, clear, ...) and temperature.', parameters: { type: 'object', properties: { city: { type: 'string' }, unit: { type: 'string', enum: ['celsius', 'fahrenheit'] } }, required: ['city', 'unit'] } } },
  { type: 'function', function: { name: 'get_stock_price', description: 'Latest trade price for a stock ticker.', parameters: { type: 'object', properties: { ticker: { type: 'string' } }, required: ['ticker'] } } },
  { type: 'function', function: { name: 'convert_currency', description: 'Convert an amount between two currencies at the current rate.', parameters: { type: 'object', properties: { amount: { type: 'number' }, from_currency: { type: 'string', description: 'ISO 4217 code' }, to_currency: { type: 'string', description: 'ISO 4217 code' } }, required: ['amount', 'from_currency', 'to_currency'] } } },
  { type: 'function', function: { name: 'search_flights', description: 'Search flights between two airports on a date.', parameters: { type: 'object', properties: { origin: { type: 'string' }, destination: { type: 'string' }, date: { type: 'string' } }, required: ['origin', 'destination', 'date'] } } },
];
export const CHAT_SYSTEM = 'You are a careful assistant. Follow the requested output format exactly.';

export const tasksFor = (provider, { only, all } = {}) =>
  TASKS.filter((t) => t.providers.includes(provider.name) && provider.kinds.includes(t.kind) && (only ? only.includes(t.id) : all || t.gate));
