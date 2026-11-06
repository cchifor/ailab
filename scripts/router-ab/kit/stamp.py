"""kit/stamp.py START OUT - copies stdin (Claude Code's stream-json) to OUT and writes OUT.result_s: the seconds from
START (epoch seconds, taken just before the CLI was started) to the arrival of the `result` event, i.e. the time to the
answer as the client sees it, CLI start-up included and its shutdown excluded (the router's own CLI spawn is likewise
inside the router arm's time)."""
import sys, time

start, out = float(sys.argv[1]), sys.argv[2]
got = None
with open(out, 'w') as f:
    for line in sys.stdin:
        f.write(line)
        if got is None and '"type":"result"' in line:
            got = time.time() - start
if got is not None:
    open(out + '.result_s', 'w').write(f'{got:.3f}\n')
