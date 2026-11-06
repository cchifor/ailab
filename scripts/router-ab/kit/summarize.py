"""kit/summarize.py KIT RUN - one gate run's metrics from its tap recordings and Codex events (worker side), as JSON.

From the tap (rec/<run>/*.meta.json, every POST .../responses): per-request time to first delta, uncached prompt tokens
(input minus cached), non-200 or non-completed responses (router errors on the router arm). From the Codex event
stream(s): tool items, failed non-command tool items (rejected calls), reconnects (retries). Prints one JSON object.
"""
import glob, json, os, statistics, sys

kit, run = sys.argv[1:3]
ttft, errors, spans, reqs, trajectories, output_tokens = [], 0, [], [], [], 0
for f in sorted(glob.glob(f'{kit}/rec/{run}/*.meta.json')):
    m = json.load(open(f))
    if m['method'] != 'POST' or not m['path'].endswith('/responses'):
        continue
    spans.append((m['tSent'], m['tEnd'] or m['tSent']))
    if m.get('trajectory'):
        trajectories.append(m['trajectory'])
    if m['status'] != 200 or m.get('terminal') not in ('response.completed', 'response.incomplete'):
        errors += 1
    if m.get('tFirstDelta'):
        ttft.append((m['tFirstDelta'] - m['tSent']) / 1000)
    u = m.get('usage') or {}
    output_tokens += u.get('output') or 0
    if u.get('input') is not None:
        reqs.append((m['tSent'], u['input'] - (u.get('cached') or 0)))
tools = rejected = retries = 0
for f in sorted(glob.glob(f'{kit}/runs/{run}/events*.jsonl')):
    for line in open(f):
        try:
            e = json.loads(line)
        except Exception:
            continue
        it = e.get('item') or {}
        if e.get('type') == 'item.completed' and it.get('type') in ('command_execution', 'file_change', 'mcp_tool_call'):
            tools += 1
            if it.get('status') == 'failed' and it.get('type') != 'command_execution':
                rejected += 1
        if e.get('type') == 'error' and 'Reconnecting' in str(e.get('message')) and 'WebSocket' not in str(e.get('message')) and 'ws://' not in str(e.get('message')):
            retries += 1
t = json.load(open(f'{kit}/runs/{run}/times.json'))
# Session time from the tap: first request sent to last response ended, without the time before the first request
# (process start; with the ChatGPT login also Codex's websocket attempts, which the tap refuses: a harness artefact).
# For a resumed (two-process) session the largest gap, the process restart between the turns, is left out too.
spans.sort()
wall = None
if spans:
    gaps = [b[0] - a[1] for a, b in zip(spans, spans[1:])]
    wall = (spans[-1][1] - spans[0][0] - (max(gaps) if gaps and os.path.exists(f'{kit}/runs/{run}/events.2.jsonl') else 0)) / 1000
# Uncached prompt tokens, without the first request of each session (each Codex process): that request's cache depends
# on the client's request shape (with an API provider Codex 0.166 never gets a cross-conversation prefix hit; ailab
# plans/2026-11-05-router-transparency-and-planning-revalidation.md section 1.4), so it is reported apart.
reqs.sort()
firsts = {0}
if len(reqs) > 1 and os.path.exists(f'{kit}/runs/{run}/events.2.jsonl'):
    gaps = [reqs[i + 1][0] - reqs[i][0] for i in range(len(reqs) - 1)]
    firsts.add(gaps.index(max(gaps)) + 1)
unc = sum(x for i, (_, x) in enumerate(reqs) if i not in firsts)
unc_first = sum(x for i, (_, x) in enumerate(reqs) if i in firsts)
print(json.dumps({'wall_s': round(wall if wall is not None else t['end'] - t['start'], 1), 'process_wall_s': round(t['end'] - t['start'], 1), 'rc': t['rc'], 'tool_calls': tools, 'rejected': rejected,
                  'ttft_s': round(statistics.median(ttft), 2) if ttft else None, 'uncached_tokens': unc, 'uncached_first_requests': unc_first,
                  'requests': len(ttft), 'output_tokens': output_tokens, 'trajectories': trajectories, 'transport_errors': errors, 'retries': retries}))
