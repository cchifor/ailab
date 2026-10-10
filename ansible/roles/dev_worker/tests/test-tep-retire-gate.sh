#!/usr/bin/env bash
# Tests for files/tep-retire-gate.sh (tasks/tep.yml, ADR 0037): when may the role delete ~/.tep and
# the agent's tep template? Hermetic: a stub systemctl and temp files, no root, no real agent.
set -euo pipefail

role=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
gate="$role/files/tep-retire-gate.sh"
t=$(mktemp -d)
trap 'rm -rf "$t"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

# Stub systemctl: is-active answers $STUB_ACTIVE; `show ... ExecMainStartTimestamp` answers
# $STUB_STARTED in the --timestamp=us+utc format.
mkdir -p "$t/bin"
cat >"$t/bin/systemctl" <<'EOF'
#!/bin/bash
case "$1" in
  is-active) echo "${STUB_ACTIVE:-active}" ;;
  show) echo "${STUB_STARTED:-}" ;;
esac
EOF
chmod +x "$t/bin/systemctl"
export PATH="$t/bin:$PATH"

hcl="$t/agent.hcl"
clean_hcl() { printf 'template {\n  source = "/etc/openbao-agent/helmtest-kubeconfig.ctmpl"\n}\n' >"$hcl"; }
expect() { # expect <want> <label>
  got=$(bash "$gate" "$hcl")
  [ "$got" = "$1" ] || fail "$2: expected $1, got $got"
  echo "  ok  $2 -> $1"
}

# 1. No agent config at all (a host without the bao agent): nothing renders into ~/.tep.
rm -f "$hcl"
expect safe "no agent.hcl"

# 2. The config still has the tep stanza: never delete, whatever the process times.
printf 'template {\n  source = "/etc/openbao-agent/tep-kubeconfig.ctmpl"\n}\n' >"$hcl"
STUB_ACTIVE=active STUB_STARTED="Fri 2026-10-10 12:00:05.000000 UTC" expect unsafe-config "tep stanza on disk"

# 3. Clean config, agent not running: nothing can render.
clean_hcl
STUB_ACTIVE=inactive expect safe "agent inactive"

# 4. Clean config written 12:00:00.200, agent started a second later: it runs the new config.
clean_hcl; touch -d "2026-10-10 12:00:00.200000000 UTC" "$hcl"
STUB_ACTIVE=active STUB_STARTED="Fri 2026-10-10 12:00:01.000000 UTC" expect safe "started 0.8 s after the write"

# 5. REGRESSION (reviewer-codex, ailab#1217): agent started at 12:00:00.100, config written at
#    12:00:00.900 — the same whole second. Whole-second -ge called this safe; it is stale.
clean_hcl; touch -d "2026-10-10 12:00:00.900000000 UTC" "$hcl"
STUB_ACTIVE=active STUB_STARTED="Fri 2026-10-10 12:00:00.100000 UTC" expect unsafe-stale-process "started 0.8 s before the write, same second"

# 6. The normal handler path: written at .100, restarted at .650 in the same second -> safe.
clean_hcl; touch -d "2026-10-10 12:00:00.100000000 UTC" "$hcl"
STUB_ACTIVE=active STUB_STARTED="Fri 2026-10-10 12:00:00.650000 UTC" expect safe "restarted 0.55 s after the write, same second"

# 7. Exactly equal instants are not proof of ordering: fail closed.
clean_hcl; touch -d "2026-10-10 12:00:00.500000000 UTC" "$hcl"
STUB_ACTIVE=active STUB_STARTED="Fri 2026-10-10 12:00:00.500000 UTC" expect unsafe-stale-process "equal timestamps"

# 8. An unreadable start time (empty / garbage) fails closed.
STUB_ACTIVE=active STUB_STARTED="" expect unsafe-unknown "empty start time"
STUB_ACTIVE=active STUB_STARTED="n/a" expect unsafe-unknown "garbage start time"

echo "test-tep-retire-gate: OK"
