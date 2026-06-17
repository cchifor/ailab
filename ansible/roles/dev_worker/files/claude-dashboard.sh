#!/bin/bash
# Managed by ansible (role: dev_worker). Build (or attach to) the multi-window 'sessions' tmux session.
SESSION=sessions

# Attach only from a TTY (interactive shell). When run non-interactively (the systemd unit at boot),
# skip the attach — `tmux attach` without a TTY fails and would mark the unit failed even though the
# session was created fine.
attach_if_tty() {
  if [ -t 0 ] && [ -t 1 ]; then
    exec tmux attach -t "$SESSION"
  fi
}

# Backward compat: an earlier launcher named the session 'dashboard'. Rename in place if present.
if tmux has-session -t dashboard 2>/dev/null && ! tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux rename-session -t dashboard "$SESSION"
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
  attach_if_tty
  exit 0
fi

# `home` is a plain shell (typing exit closes it). Every other long-lived window is wrapped in
# `while true; do CMD; sleep 0.5; done` so a quit rebuilds the view instead of killing the window.
# Order matters — windows are created in the order presented to the user.
tmux new-session  -d -s "$SESSION" -n home -c "/workspace/$USER"
tmux new-window   -t "$SESSION:" -n system  'while true; do htop; sleep 0.5; done'
tmux new-window   -t "$SESSION:" -n jobs    'while true; do journalctl -u "claude-job@*.service" -f --no-pager 2>/dev/null || sudo journalctl -f; sleep 0.5; done'
tmux new-window   -t "$SESSION:" -n github -c "/workspace/$USER" 'while true; do gh-dash; sleep 0.5; done'
tmux split-window -t "$SESSION:github" -v -l 35% 'while true; do watch -tc -n 15 gh-actions-fleet; sleep 0.5; done'
tmux select-pane  -t "$SESSION:github.1"
tmux new-window   -t "$SESSION:" -n docker  'while true; do oxker; sleep 0.5; done'
# cluster: k9s in a retry loop. On a missing kubeconfig (no fan-out yet) print a one-shot kubectl
# probe and sleep — never a long-running `watch` fallback (that would trap the loop forever).
tmux new-window   -t "$SESSION:" -n cluster 'while true; do clear; k9s --kubeconfig=$HOME/.kube/config 2>/dev/null || { echo "k9s exited (retrying in 2s). kubectl probe:"; kubectl --kubeconfig=$HOME/.kube/config get nodes 2>&1 | head -6; }; sleep 2; done'
tmux new-window   -t "$SESSION:" -n cheats  'while true; do clear; pr -m -t -w $(tput cols) /usr/local/share/cheat-tmux.txt /usr/local/share/cheat-linux.txt | less -R; done'
tmux select-window -t "$SESSION:home"
attach_if_tty
