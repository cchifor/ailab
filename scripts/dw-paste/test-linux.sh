#!/usr/bin/env bash
# End-to-end test of dw-paste-linux.sh on X11, inside a throwaway Ubuntu container: Xvfb, an xterm
# whose title carries the dev-worker marker, a pane program that records its raw input with
# bracketed paste on, and a stand-in scp that copies into a local "remote" tree instead of the
# network. Checks: self-test (image, file list), a real Ctrl+Shift+V run (image -> uploaded -> path
# bracket-pasted + typed space), clipboard restored, text passes through, a window without the
# marker gets a plain paste.
#
#   bash scripts/dw-paste/test-linux.sh        # on any host with docker (a dev-worker works)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${DW_PASTE_TEST_IMAGE:-ubuntu:24.04}"

docker run --rm -i -v "$HERE:/src:ro" "$IMAGE" bash -s <<'IN_CONTAINER'
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null && apt-get install -y -qq xvfb xclip xdotool xterm python3 >/dev/null 2>&1 || { echo "FATAL: apt"; exit 2; }
Xvfb :99 -screen 0 1024x768x24 >/dev/null 2>&1 &
export DISPLAY=:99 HOME=/root
sleep 1
FAILS=0
ok() { echo "  ok: $1"; }
bad() { echo "  FAIL: $1"; FAILS=$((FAILS + 1)); }

# stand-in scp: copy the files to /remote<dir>
mkdir -p /fakebin /remote/workspace/c4/pastes
cat >/fakebin/scp <<'SCP'
#!/usr/bin/env bash
args=(); for a in "$@"; do case "$a" in -q|-o) ;; BatchMode=*|ConnectTimeout=*) ;; *) args+=("$a") ;; esac; done
dest="${args[-1]#*:}"; unset 'args[-1]'
[ -n "${FAKE_SCP_DELAY:-}" ] && sleep "$FAKE_SCP_DELAY"
cp -- "${args[@]}" "/remote$dest"
SCP
chmod +x /fakebin/scp
export PATH=/fakebin:$PATH
cp /src/dw-paste-linux.sh /tmp/dwp.sh

python3 - <<'PY'
import struct, zlib
def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
raw = b"".join(b"\x00" + b"\xff\x00\x00" * 4 for _ in range(4))
open("/tmp/shot.png", "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
PY
echo '%PDF-1.4 t' >"/tmp/a report (1).pdf"; cp /tmp/shot.png /tmp/b.png

echo "[1] --self-test with a screenshot on the clipboard"
xclip -selection clipboard -t image/png -i /tmp/shot.png
out="$(bash /tmp/dwp.sh --self-test c4@10.0.0.9)"; echo "$out" | sed 's/^/    /'
f="$(ls /remote/workspace/c4/pastes/*-screenshot.png 2>/dev/null | head -1)"
[ -n "$f" ] && cmp -s "$f" /tmp/shot.png && ok "screenshot uploaded intact" || bad "screenshot not uploaded"

echo "[2] --self-test with two copied files (text/uri-list)"
printf 'file:///tmp/a%%20report%%20%%281%%29.pdf\r\nfile:///tmp/b.png\r\n' | xclip -selection clipboard -t text/uri-list -i
out="$(bash /tmp/dwp.sh --self-test c4@10.0.0.9)"; echo "$out" | sed 's/^/    /'
ls /remote/workspace/c4/pastes | grep -q -- '-a_report_1_.pdf$' && ls /remote/workspace/c4/pastes | grep -q -- '-b.png$' \
	&& ok "both files uploaded with safe names" || bad "file list not uploaded"

echo "[3] Ctrl+Shift+V in a dev-worker terminal"
cat >/tmp/capture.py <<'PY'
import os, sys, tty
sys.stdout.write("\x1b[?2004h"); sys.stdout.flush()
tty.setraw(0)
with open("/tmp/cap", "ab", buffering=0) as f:
    while True:
        b = os.read(0, 4096)
        if not b: break
        f.write(b)
PY
: >/tmp/cap
xterm -T 'dev-worker-9 [c4@10.0.0.9] main:claude' -e python3 /tmp/capture.py &
for _ in $(seq 1 30); do w="$(xdotool search --name 'dev-worker-9' 2>/dev/null | head -1)"; [ -n "$w" ] && break; sleep 0.2; done
xdotool windowfocus --sync "$w"; sleep 0.5
rm -f /remote/workspace/c4/pastes/*
xclip -selection clipboard -t image/png -i /tmp/shot.png
bash /tmp/dwp.sh; sleep 0.8
remote="$(ls /remote/workspace/c4/pastes/ | head -1)"
python3 - "$remote" <<'PY' && ok "path bracket-pasted, then a typed space, no Enter" || bad "paste not as expected: $(cat -v /tmp/cap)"
import sys
cap = open("/tmp/cap", "rb").read()
want = b"\x1b[200~/workspace/c4/pastes/" + sys.argv[1].encode() + b"\x1b[201~ "
sys.exit(0 if want in cap and b"\r" not in cap and b"\n" not in cap else 1)
PY
xclip -selection clipboard -t TARGETS -o | grep -qx image/png && ok "clipboard restored (still an image)" || bad "clipboard not restored"

echo "[4] text passes through as an ordinary paste"
: >/tmp/cap
printf 'plain words' | xclip -selection clipboard -i
bash /tmp/dwp.sh; sleep 0.5
grep -q $'\e\[200~plain words\e\[201~' /tmp/cap && ok "text pasted normally" || bad "text not pasted: $(cat -v /tmp/cap)"
[ -z "$(ls /remote/workspace/c4/pastes/ | sed -n 2p)" ] && ok "nothing extra uploaded for text" || bad "text caused an upload"

echo "[5] a window without the marker gets a plain paste, no upload"
xterm -T 'plain terminal' -e python3 /tmp/capture.py &
for _ in $(seq 1 30); do w2="$(xdotool search --name 'plain terminal' 2>/dev/null | head -1)"; [ -n "$w2" ] && break; sleep 0.2; done
xdotool windowfocus --sync "$w2"; sleep 0.5
n="$(ls /remote/workspace/c4/pastes | wc -l)"
xclip -selection clipboard -t image/png -i /tmp/shot.png
bash /tmp/dwp.sh; sleep 0.5
[ "$(ls /remote/workspace/c4/pastes | wc -l)" = "$n" ] && ok "no upload outside a dev-worker terminal" || bad "uploaded from a non-worker window"

echo "[6] an image on the clipboard in an unmarked window never pastes stale PRIMARY text"
: >/tmp/cap2
cat >/tmp/capture2.py <<'PY'
import os, sys, tty
sys.stdout.write("\x1b[?2004h"); sys.stdout.flush()
tty.setraw(0)
with open("/tmp/cap2", "ab", buffering=0) as f:
    while True:
        b = os.read(0, 4096)
        if not b: break
        f.write(b)
PY
xterm -T 'another plain terminal' -e python3 /tmp/capture2.py &
for _ in $(seq 1 30); do w3="$(xdotool search --name 'another plain terminal' 2>/dev/null | head -1)"; [ -n "$w3" ] && break; sleep 0.2; done
xdotool windowfocus --sync "$w3"; sleep 0.5
printf 'rm -rf stale\n' | xclip -selection primary -i
xclip -selection clipboard -t image/png -i /tmp/shot.png
bash /tmp/dwp.sh; sleep 0.5
grep -q stale /tmp/cap2 && bad "stale PRIMARY text was pasted" || ok "nothing pasted (no stale PRIMARY)"

echo "[7] GNOME Terminal is recognised (its 21-char name is truncated in /proc/<pid>/comm)"
cp "$(command -v xterm)" /usr/local/bin/gnome-terminal-server
: >/tmp/cap
gnome-terminal-server -T 'dev-worker-8 [c4@10.0.0.8] main:codex' -e python3 /tmp/capture.py &
for _ in $(seq 1 30); do w4="$(xdotool search --name 'dev-worker-8' 2>/dev/null | head -1)"; [ -n "$w4" ] && break; sleep 0.2; done
xdotool windowfocus --sync "$w4"; sleep 0.5
before="$(ls /remote/workspace/c4/pastes | wc -l)"
xclip -selection clipboard -t image/png -i /tmp/shot.png
bash /tmp/dwp.sh; sleep 0.8
[ "$(ls /remote/workspace/c4/pastes | wc -l)" -gt "$before" ] && grep -q $'\e\[200~/workspace/c4/pastes/' /tmp/cap \
	&& ok "gnome-terminal-server window recognised, path pasted" || bad "gnome-terminal-server not recognised"

echo "[8] focus moves to another window during the upload: nothing pasted, paths left on the clipboard"
: >/tmp/cap; : >/tmp/cap2
xdotool windowfocus --sync "$w4"; sleep 0.3
xclip -selection clipboard -t image/png -i /tmp/shot.png
FAKE_SCP_DELAY=1.5 bash /tmp/dwp.sh &
helper=$!
sleep 0.5
xdotool windowfocus --sync "$w3"
wait "$helper" # only the helper: a bare `wait` would also wait for Xvfb and the xterms, forever
sleep 0.5
if grep -q '/workspace/c4/pastes' /tmp/cap /tmp/cap2; then bad "a path was pasted after focus moved"; else ok "nothing pasted after focus moved"; fi
xclip -selection clipboard -o 2>/dev/null | grep -q '^/workspace/c4/pastes/.*-screenshot\.png$' && ok "the worker path is on the clipboard instead" || bad "path not left on the clipboard"

echo
[ "$FAILS" = 0 ] && echo PASS || { echo "FAILED: $FAILS"; exit 1; }
IN_CONTAINER
