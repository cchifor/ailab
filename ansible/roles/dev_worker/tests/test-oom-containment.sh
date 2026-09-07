#!/usr/bin/env bash
# Self-contained behavioural test for the dev_worker interactive-OOM containment (plain bash — no
# bats, no molecule, following tests/test-tmux-persistence.sh).
#
# Pins the fix for the incident that ate a live Claude Code session on dev-worker-1 on 2026-09-07.
# A mutation-testing harness produced a pytest that reached 7.5 GiB on a 12 GiB worker; the kernel's
# GLOBAL OOM killer killed that one process and nothing else. The session died anyway, because tmux
# 3.4 links against libsystemd and runs every pane in a transient `tmux-spawn-<uuid>.scope`, and
# systemd's stock DefaultOOMPolicy is `stop`: the user manager tore down the whole scope, SIGTERMing
# `claude` and then SIGKILLing the pane's shell 90s later when the stop timed out. The kernel killed
# the test; systemd killed the session.
#
# Section [A] drives REAL transient systemd scopes and proves the escalation both ways, so it asserts
# the actual mechanism rather than a paraphrase of it. Each probe allocates at most 64 MiB and that
# allocation is confined to its own cgroup, so it is safe to run on a live worker -- but it is not
# free: on a host ALREADY at the edge the extra 64 MiB can tip it, which is why a probe that never
# starts is reported as such rather than counted as a kill. It needs a running per-user systemd
# manager: absent, it is reported as NOT RUN and the suite still checks everything else — but CI sets
# REQUIRE_SYSTEMD=1, which makes that a hard failure. Same "no soft skips" rule as
# test-tmux-persistence.sh: a developer on Windows may run a partial suite and be told so loudly.
#
# Section [B] pins the role wiring, and specifically the decisions that are SILENTLY wrong if someone
# tidies them later. Every one of them was measured on dev-worker-1, not assumed.
#
# Usage: bash ansible/roles/dev_worker/tests/test-oom-containment.sh   (exit 0 = pass)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROLE="$HERE/.."
POLICY_TMPL="$ROLE/templates/user-oom-policy.conf.j2"
SLICE_TMPL="$ROLE/templates/tmux-slice-memory-cap.conf.j2"
TASKS="$ROLE/tasks/oom_containment.yml"
MAIN="$ROLE/tasks/main.yml"
DEFAULTS="$ROLE/defaults/main.yml"
for f in "$POLICY_TMPL" "$SLICE_TMPL" "$TASKS" "$MAIN" "$DEFAULTS"; do
	[ -r "$f" ] || { echo "FATAL: cannot read $f"; exit 2; }
done

WORK="$(mktemp -d)"
UNITS=()
PSOCK=""
cleanup() {
	for u in ${UNITS[@]+"${UNITS[@]}"}; do systemctl --user stop "$u" >/dev/null 2>&1; done
	# Private socket only, never the user's default server.
	[ -n "$PSOCK" ] && command -v tmux >/dev/null 2>&1 && tmux -L "$PSOCK" kill-server 2>/dev/null
	rm -rf "$WORK"
}
trap cleanup EXIT

PASS=0; FAIL=0; SKIP=0
ok()   { printf '  ok       %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  FAIL     %s\n' "$1"; FAIL=$((FAIL+1)); }
skip() { printf '  NOT RUN  %s\n' "$1"; SKIP=$((SKIP+1)); }
check() { if [ "$1" = 0 ]; then ok "$2"; else bad "$2"; fi; }

# ---------------------------------------------------------------------------
# [A] behavioural: does OOMPolicy actually decide whether bystanders survive?
# ---------------------------------------------------------------------------
echo "[A] transient-scope OOM escalation (real systemd scopes, 64 MiB cgroup)"

have_systemd=0
if command -v systemd-run >/dev/null 2>&1 &&
   [ -n "${XDG_RUNTIME_DIR:-}" ] &&
   systemctl --user show -p Version >/dev/null 2>&1; then
	have_systemd=1
fi

if [ "$have_systemd" = 0 ]; then
	if [ "${REQUIRE_SYSTEMD:-0}" = 1 ]; then
		bad "REQUIRE_SYSTEMD=1 but no per-user systemd manager is reachable"
	else
		skip "no per-user systemd manager here (needs Linux + a live user session)"
	fi
elif ! command -v python3 >/dev/null 2>&1; then
	skip "python3 absent — cannot drive the memory bomb"
else
	# The payload lives in its own file so the quoting stays legible: one scope holding a "survivor"
	# (standing in for the pane's bash + claude) and a bomb (standing in for the runaway pytest).
	# MemoryMax is what kills the bomb; OOMPolicy is what decides whether the survivor dies with it.
	# Only the policy varies between the two runs.
	PAYLOAD="$WORK/payload.sh"
	{
		printf '%s\n' '#!/usr/bin/env bash'
		printf '%s\n' '# $1 = file to publish the survivor pid into'
		printf '%s\n' 'sleep 25 &'
		printf '%s\n' 'echo $! > "$1"'
		printf '%s\n' 'python3 -c "'
		printf '%s\n' 'x = []'
		printf '%s\n' 'while True:'
		printf '%s\n' '    x.append(bytearray(4 * 1024 * 1024))'
		printf '%s\n' '" >/dev/null 2>&1'
		printf '%s\n' 'wait'
	} > "$PAYLOAD"
	chmod +x "$PAYLOAD"

	probe_policy() {
		local policy="$1" unit mark survivor rc
		unit="oomtest-${policy}-$$-${RANDOM}.scope"
		UNITS+=("$unit")
		mark="$WORK/pid.$policy"
		systemd-run --user --scope --quiet --unit="$unit" \
			-p MemoryAccounting=yes -p MemoryMax=64M -p MemorySwapMax=0 -p OOMPolicy="$policy" \
			bash "$PAYLOAD" "$mark" >/dev/null 2>&1 &
		# 30s, not 12: on a box that is already tight the scope's bash can be slow to get scheduled
		# and write the pid. "nostart" is reported SEPARATELY from "killed" on purpose -- folding
		# the two together let a probe that never even started satisfy the `= killed` assertion,
		# i.e. the stop case could go green without a single OOM having happened.
		for _ in $(seq 1 150); do [ -s "$mark" ] && break; sleep 0.2; done
		survivor="$(cat "$mark" 2>/dev/null)"
		if [ -z "$survivor" ]; then
			systemctl --user stop "$unit" >/dev/null 2>&1
			printf 'nostart'
			return
		fi
		sleep 6
		if kill -0 "$survivor" 2>/dev/null; then rc=survived; else rc=killed; fi
		kill "$survivor" 2>/dev/null
		systemctl --user stop "$unit" >/dev/null 2>&1
		printf '%s' "$rc"
	}

	r_stop="$(probe_policy stop)"
	r_continue="$(probe_policy continue)"

	# Both probes dying is the signature of a host that was already out of memory, not of the
	# policy under test -- call that out instead of reporting a plain failure that sends the next
	# reader hunting for a regression that is not there.
	if [ "$r_stop" = nostart ] || [ "$r_continue" = nostart ]; then
		bad "a probe scope never started (stop=$r_stop continue=$r_continue) — host too loaded to test"
	else
		[ "$r_stop" = killed ]
		check $? "OOMPolicy=stop kills the bystander (reproduces the incident)"
		[ "$r_continue" = survived ]
		check $? "OOMPolicy=continue keeps the bystander alive (the fix)"
	fi
fi

# tmux must not pin OOMPolicy itself, or the manager default this role sets is a no-op. tmux 3.4
# passes only Description/Slice/PIDs/CollectMode on StartTransientUnit.
if ! command -v tmux >/dev/null 2>&1; then
	skip "tmux absent — cannot check its transient-unit properties"
elif ! command -v strings >/dev/null 2>&1; then
	skip "strings(1) absent — cannot check tmux's transient-unit properties"
else
	! strings "$(command -v tmux)" | grep -qx OOMPolicy
	check $? "tmux does not hard-code OOMPolicy (so the manager default reaches its panes)"

	# The strings check is circumstantial; this is the end-to-end proof. Drive a REAL tmux server on
	# a PRIVATE socket (never the user's -- this suite is expected to run on a live dev-worker, where
	# killing the wrong server would destroy exactly what it protects), find the pane's own transient
	# scope through its cgroup, and assert the pane INHERITED the manager default. Comparing against
	# the manager rather than a hard-coded "continue" makes this the same invariant before and after
	# the role has converged, so it is meaningful on an unconverged box too.
	if [ "$have_systemd" = 0 ]; then
		skip "no per-user systemd manager — cannot read a real pane's OOMPolicy"
	else
		PSOCK="oomprobe-$$"
		tmux -L "$PSOCK" kill-server 2>/dev/null
		if tmux -L "$PSOCK" new-session -d -s probe "sleep 30" 2>/dev/null; then
			# tmux registers the scope with systemd over D-Bus asynchronously, so both the pane pid
			# and the unit can lag the new-session call. Poll for a LOADED unit rather than reading
			# once -- reading too early returns an empty OOMPolicy, which looks exactly like a real
			# inheritance failure.
			pscope=""
			for _ in $(seq 1 50); do
				ppid="$(tmux -L "$PSOCK" list-panes -F '#{pane_pid}' 2>/dev/null | head -1)"
				if [ -n "$ppid" ] && [ -r "/proc/$ppid/cgroup" ]; then
					pscope="$(awk -F/ '/^0::/ {print $NF}' "/proc/$ppid/cgroup" 2>/dev/null)"
					case "$pscope" in
						*.scope)
							[ "$(systemctl --user show "$pscope" -p LoadState --value 2>/dev/null)" = loaded ] && break
							;;
					esac
					pscope=""
				fi
				sleep 0.2
			done
			if [ -z "$pscope" ]; then
				skip "pane never landed in a loaded transient scope (tmux built without systemd?)"
			else
				pane_policy="$(systemctl --user show "$pscope" -p OOMPolicy --value 2>/dev/null)"
				mgr_policy="$(systemctl --user show -p DefaultOOMPolicy --value 2>/dev/null)"
				[ -n "$pane_policy" ] && [ "$pane_policy" = "$mgr_policy" ]
				check $? "a real pane inherits the manager default (pane=$pane_policy manager=$mgr_policy)"
			fi
			tmux -L "$PSOCK" kill-server 2>/dev/null
			PSOCK=""
		else
			skip "could not start a private tmux server"
		fi
	fi
fi

# ---------------------------------------------------------------------------
# [B] the role wiring, and the decisions that are silently wrong if tidied
# ---------------------------------------------------------------------------
echo "[B] role wiring"

grep -q '^\[Manager\]' "$POLICY_TMPL" &&
	grep -q '^DefaultOOMPolicy={{ dev_worker_user_oom_policy }}$' "$POLICY_TMPL"
check $? "policy template sets [Manager] DefaultOOMPolicy"

grep -q '^dev_worker_user_oom_policy: continue$' "$DEFAULTS"
check $? "default policy is 'continue'"

# USER manager, not the system manager. In /etc/systemd/system.conf.d this would ALSO stop the
# memory-capped claude-job@/agentforge units from failing as a unit, which is exactly what their caps
# exist to do. The incident was in a per-user tmux scope; the change belongs there and nowhere else.
grep -q 'dest: /etc/systemd/user\.conf\.d/10-oom-policy\.conf' "$TASKS" &&
	! grep -q '/etc/systemd/system\.conf\.d' "$TASKS"
check $? "policy lands in the USER manager config, never the system one"

# daemon-REEXEC, not daemon-reload. Manager configuration is parsed only at manager start, so a
# reload leaves DefaultOOMPolicy at the stock 'stop' while every file on disk looks correct.
grep -q 'systemctl --user daemon-reexec' "$TASKS"
check $? "applies the policy with daemon-reexec (a reload would silently not take effect)"

grep -q 'dest: /etc/systemd/user/app-tmux\.slice\.d/10-memory-cap\.conf' "$TASKS"
check $? "slice cap lands in the app-tmux.slice drop-in dir"

grep -q '^MemoryMax={{ dev_worker_tmux_memory_max }}$' "$SLICE_TMPL"
check $? "slice template sets MemoryMax"

# Swap denied. With a swap allowance the runaway reaches MemoryMax in RAM *plus* the swap grant
# before it dies — a bigger footprint than the 7.5 GiB that took the box down in the first place,
# thrashing a shared 4 GiB swapfile the whole way.
grep -q '^MemorySwapMax={{ dev_worker_tmux_memory_swap_max }}$' "$SLICE_TMPL" &&
	grep -q '^dev_worker_tmux_memory_swap_max: "0"$' "$DEFAULTS"
check $? "slice swap is denied (MemorySwapMax=0)"

# No MemoryHigh here, unlike dev_worker_job_memory_* — a deliberate asymmetry, so this pin is what
# stops it being "restored". MemoryHigh throttles by driving reclaim, and with swap denied there is
# nothing to reclaim from an anon-heavy runaway; it would only burn CPU across every pane in the
# slice on the way to MemoryMax.
# Anchored to line start: the template is EXPECTED to name MemoryHigh in the comment that explains
# its absence, and an unanchored grep would fail on that prose instead of on a real directive.
! grep -qE '^[[:space:]]*MemoryHigh' "$SLICE_TMPL"
check $? "slice has no MemoryHigh (futile reclaim with swap denied — see defaults/main.yml)"

grep -q 'import_tasks: oom_containment\.yml' "$MAIN"
check $? "main.yml imports oom_containment.yml"

grep -A2 'import_tasks: oom_containment\.yml' "$MAIN" | grep -q 'tags: \[oom\]'
check $? "carries its own --tags oom so the guard rolls out without touching docker/toolchains"

# ...and that tag is only usable if the file stands alone: users.yml is outside the `oom` tag set, so
# a tag-limited run reaches this file with dev_worker_users undefined and every per-user loop below
# it fails. Same fallback as claude_statusline.yml.
grep -q 'dev_worker_users is not defined' "$TASKS"
check $? "re-derives dev_worker_users so --tags oom works without users.yml"

# The read-back assert is the only thing between "the files are on disk" and "the guard is armed";
# a run that quietly lost it would look identical in the play recap.
grep -q 'DefaultOOMPolicy' "$TASKS" && grep -q 'ansible.builtin.assert' "$TASKS"
check $? "the play asserts the effective policy, not just the file content"

# ...but not under --check, where neither drop-in is written and the re-exec is skipped, so the
# read-back would report the pre-change value and fail on every not-yet-converged host — i.e. every
# host a pre-flight is aimed at. Without this guard `--check --diff` is permanently red.
[ "$(grep -c 'not ansible_check_mode' "$TASKS")" -ge 2 ]
check $? "the read-back and its assert are both skipped under --check"

echo
echo "passed=$PASS failed=$FAIL not-run=$SKIP"
[ "$FAIL" -eq 0 ]
