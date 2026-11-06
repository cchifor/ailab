"""kit/same_body.py REQ META TARGET DIRECT_MODEL ROUTER_MODEL N GREEDY - Layer (a), worker side: the SAME recorded Codex
Responses body sent N times each way, alternating: DIRECT (TARGET chatgpt: chatgpt.com with this host's own login, the
access token read in memory and never printed or refreshed; TARGET vllm: the model server's own /v1/responses) and VIA
the router (the worker key from codex-router-key). GREEDY=1 sets temperature 0 and 160 output tokens (local models).
Prints one JSON object: per call input tokens, output item types and an output hash, then the verdict fields.
"""
import hashlib, json, os, subprocess, sys, time, urllib.request, uuid

req, meta, target, dmodel, rmodel, n, greedy = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], int(sys.argv[6]), sys.argv[7] == '1'
raw = open(req, 'rb').read()
if json.load(open(meta)).get('reqEncoding') == 'zstd':
    raw = subprocess.run(['zstd', '-dc'], input=raw, capture_output=True, check=True).stdout
body = json.loads(raw)
for k in ('prompt_cache_key', 'client_metadata'):
    body.pop(k, None)
body['stream'] = True
if greedy:
    body.update({'temperature': 0, 'top_p': 1, 'max_output_tokens': 160})
key = subprocess.run(['/usr/local/bin/codex-router-key'], capture_output=True, text=True, check=True).stdout.strip()
UA = 'codex_exec/0.166.0 (Ubuntu 24.4.0; x86_64) xterm-256color (codex_exec; 0.166.0)'

def call(arm):
    b = dict(body); sid = str(uuid.uuid4())
    h = {'content-type': 'application/json', 'accept': 'text/event-stream', 'originator': 'codex_exec', 'user-agent': UA, 'session_id': sid}
    if arm == 'router':
        url = 'https://router.chifor.me/v1/responses'; b['model'] = rmodel; h['authorization'] = 'Bearer ' + key
    elif target == 'chatgpt':
        tok = json.load(open(os.path.expanduser('~/.codex/auth.json')))['tokens']
        url = 'https://chatgpt.com/backend-api/codex/responses'; b['model'] = dmodel
        h.update({'authorization': 'Bearer ' + tok['access_token'], 'chatgpt-account-id': tok['account_id']})
    else:
        url = os.environ['SERVER_URL'].rstrip('/') + '/responses'; b['model'] = dmodel
    items, gen, usage, t0, declared, trajectory = [], '', {}, time.time(), {}, None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(b).encode(), headers=h), timeout=600) as r:
            # the router's own declarations of what it adapted or ignored (no secrets in these headers)
            declared = {k.lower(): v for k, v in r.headers.items() if k.lower() in ('x-router-adapted', 'x-router-ignored-params')}
            trajectory = r.headers.get('x-trajectory-id')
            for line in r:
                line = line.decode('utf-8', 'replace').strip()
                if not line.startswith('data:'):
                    continue
                try:
                    d = json.loads(line[5:])
                except Exception:
                    continue
                t = d.get('type', '')
                if t.endswith('.delta'):
                    gen += d.get('delta', '') if isinstance(d.get('delta'), str) else ''
                if t == 'response.output_item.done':
                    items.append(d['item'].get('type'))
                if t in ('response.completed', 'response.incomplete'):
                    usage = d['response'].get('usage') or {}
    except urllib.error.HTTPError as e:
        return {'arm': arm, 'http': e.code, 'error': e.read()[:200].decode('utf-8', 'replace')}
    except Exception as e:  # noqa: BLE001 - reported, and the verdict fields fail closed
        return {'arm': arm, 'error': str(e)[:200]}
    return {'arm': arm, 'input': usage.get('input_tokens'), 'items': items, 'hash': hashlib.sha256(gen.encode()).hexdigest()[:12], 's': round(time.time() - t0, 1), 'trajectory': trajectory, **({'declared': declared} if declared else {})}

calls = []
for i in range(n):
    for arm in (('direct', 'router') if i % 2 == 0 else ('router', 'direct')):
        calls.append(call(arm))
# every verdict fails closed: a call that errored, or that streamed nothing, counts against it
ok = [c for c in calls if 'error' not in c and c.get('input') is not None]
d = [c for c in ok if c['arm'] == 'direct']; r = [c for c in ok if c['arm'] == 'router']
complete = len(ok) == len(calls)
print(json.dumps({'calls': calls, 'complete': complete,
                  'same_input_tokens': complete and len({c['input'] for c in calls}) == 1,
                  'same_items': complete and len({tuple(c.get('items') or []) for c in calls}) == 1,
                  'router_hash_matches_direct': complete and bool({c['hash'] for c in r} & {c['hash'] for c in d})}))
