"""kit/claude_diff.py REC_DIRECT REC_REPLICA - Layer (a) for the Claude seat (worker side): the last Messages request
Claude Code sent in each recorded run (tap metas and bodies), compared field by field. Declared differences of the
router's sdk-resume transport are listed, not failed: the working directory named in the environment note, the
per-session metadata, the hashed tool names (mcp__router__t_<sha256>) and the transcript markers (the env note's
position, "Continue from where you left off."). Prints JSON: equal fields, differing fields, declared and undeclared.
"""
import glob, json, re, sys

def last_post(d):
    out = None
    for f in sorted(glob.glob(f'{d}/*.meta.json')):
        m = json.load(open(f))
        if m['method'] == 'POST' and m['path'].endswith('/v1/messages'):
            out = json.load(open(f.replace('.meta.json', '.req')))
    return out

D, X = last_post(sys.argv[1]), last_post(sys.argv[2])
if not D or not X:
    print(json.dumps({'error': 'missing recording'})); sys.exit()
FIELDS = ['model', 'system', 'thinking', 'max_tokens', 'output_config', 'context_management', 'stream']
equal = [f for f in FIELDS if json.dumps(D.get(f), sort_keys=True) == json.dumps(X.get(f), sort_keys=True)]
diff = [f for f in FIELDS if f not in equal]
norm = lambda s: re.sub(r'/h(/llm-router-sdk-\w+)?', '/h', s)
def user_texts(b):
    out = []
    for m in b['messages']:
        if m['role'] != 'user': continue
        c = m['content']
        for t in ([c] if isinstance(c, str) else [x.get('text', '') for x in c if x.get('type') == 'text']):
            if t and t != 'Continue from where you left off.': out.append(t)
    return out
def sys_texts(b):
    return sorted(norm(json.dumps(m['content'])) for m in b['messages'] if m['role'] == 'system' and m['content'])
undeclared = [f'{f} differs' for f in diff]
if user_texts(D) != user_texts(X): undeclared.append('user messages differ')
tools_d = sorted(t.get('description', '') for t in D.get('tools', [])); tools_x = sorted(t.get('description', '') for t in X.get('tools', []))
if tools_d != tools_x: undeclared.append('tool descriptions differ')
declared = []
if sys_texts(D) != sys_texts(X): declared.append('system notes differ (position / per-turn reminders)')
if any('Continue from where you left off.' in json.dumps(m['content']) for m in X['messages']): declared.append('resume marker "Continue from where you left off."')
if [t.get('name') for t in D.get('tools', [])] != [t.get('name') for t in X.get('tools', [])]: declared.append('tool names hashed')
if D.get('metadata') != X.get('metadata'): declared.append('per-session metadata')
print(json.dumps({'equal': equal, 'declared': declared, 'undeclared': undeclared}))
