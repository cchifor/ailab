#!/usr/bin/env bash
# Self-contained behavioural test for the dev_worker `platform` helper and the openbao-agent wiring
# around it (plain bash, same shape as test-cred-helper.sh — no bats, no molecule, no vault, no
# cluster, so it runs everywhere including a developer's Windows box).
#
# WHY THIS EXISTS. `platform` is what an agent actually types, and every way it can break is quiet:
#   - the slot derivation. `dw<N>_platform_ro` comes from the hostname; a wrong or silently-defaulted
#     slot means authenticating as another worker's role (or a confusing "no password supplied").
#   - the argv it composes. A missing `--kubeconfig` would fall back to ~/.kube/config — a different,
#     possibly far more privileged cluster credential — and still look like it worked.
#   - the port-forward lifecycle. `exec psql` would orphan the forward for the life of the login
#     session; a missing readiness wait would race psql against the tunnel.
#   - --tenant. The platform's RLS policies read current_setting('app.tenant_id'); if the option does
#     not reach libpq, every RLS table reads EMPTY and an agent concludes "no data" instead of
#     "wrong session".
#   - the render wiring. The two .ctmpl templates, the agent.hcl stanzas and the KV field names have
#     to agree with what openbao-platform-pg-sync / openbao-k8stoken-sync publish.
# Sections [A]-[D] run the REAL helper against stub kubectl/psql/pg_isready/hostname; section [E]
# pins the wiring by reading the shipped files.
#
# Usage: bash ansible/roles/dev_worker/tests/test-platform-helper.sh   (exit 0 = pass)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROLE="$HERE/.."
HELPER="$ROLE/files/platform"
HCL="$ROLE/templates/openbao-agent.hcl.j2"
KCTMPL="$ROLE/templates/platform-kubeconfig.ctmpl.j2"
PGTMPL="$ROLE/templates/platform-pgpass.ctmpl.j2"
TASKS="$ROLE/tasks/openbao.yml"
PACKAGES="$ROLE/tasks/packages.yml"
for f in "$HELPER" "$HCL" "$KCTMPL" "$PGTMPL" "$TASKS" "$PACKAGES"; do
  [ -r "$f" ] || { echo "FATAL: cannot read $f"; exit 2; }
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"
HOME_DIR="$WORK/home"
mkdir -p "$BIN" "$HOME_DIR/.platform"
CALLS="$WORK/calls.log"
: >"$CALLS"

STUB_HOST="dev-worker-3"
# Synthetic; nothing here touches a real vault or cluster.
printf 'apiVersion: v1\nkind: Config\n' >"$HOME_DIR/.platform/kubeconfig"
printf '*:*:*:dw3_platform_ro:stub-password-aaaa\n' >"$HOME_DIR/.platform/pgpass"

# ---- stubs -----------------------------------------------------------------------------------------
cat >"$BIN/hostname" <<EOF
#!/usr/bin/env bash
printf '%s\n' "$STUB_HOST"
EOF
# kubectl: logs argv. A port-forward call must BLOCK (the helper backgrounds it and kills it), so it
# sleeps; anything else returns at once.
cat >"$BIN/kubectl" <<EOF
#!/usr/bin/env bash
printf 'kubectl %s\n' "\$*" >>"$CALLS"
for a in "\$@"; do [ "\$a" = port-forward ] && { sleep 30; exit 0; }; done
exit 0
EOF
# python3: the helper uses it only to pick a free loopback port. A stub keeps the port deterministic
# so the suite can prove the forward, the readiness probe and psql all use the SAME one.
cat >"$BIN/python3" <<EOF
#!/usr/bin/env bash
printf '15432
'
EOF
cat >"$BIN/pg_isready" <<EOF
#!/usr/bin/env bash
printf 'pg_isready %s\n' "\$*" >>"$CALLS"
exit 0
EOF
# psql: records argv AND the environment the helper handed it (PGPASSFILE/PGOPTIONS/PGSSLMODE), then
# exits 7 so the suite can prove the helper propagates psql's own status.
cat >"$BIN/psql" <<EOF
#!/usr/bin/env bash
printf 'psql %s\n' "\$*" >>"$CALLS"
printf 'env PGPASSFILE=%s PGOPTIONS=%s PGSSLMODE=%s\n' "\${PGPASSFILE:-}" "\${PGOPTIONS:-}" "\${PGSSLMODE:-}" >>"$CALLS"
exit 7
EOF
chmod +x "$BIN"/*

# Output goes to a FILE and the status to $STATUS; the helper is never run inside $( ), because a
# command substitution is a subshell and a $STATUS set there would never reach the caller (it would
# read whatever the previous direct call left behind — a test that passes while proving nothing).
OUT="$WORK/out"
run() { # run <args...>; stdout+stderr -> $OUT, exit status -> $STATUS, and echoed for convenience
  (cd "$WORK" && env -i PATH="$BIN:/usr/bin:/bin" HOME="$HOME_DIR" sh "$HELPER" "$@") >"$OUT" 2>&1
  STATUS=$?
  return 0
}
out() { cat "$OUT"; }
pass() { printf '  ok  %s\n' "$1"; }
fail() {
  printf 'FAIL: %s\n' "$1" >&2
  [ -s "$CALLS" ] && sed 's/^/  | /' "$CALLS" >&2
  exit 1
}
saw() { grep -qF -- "$1" "$CALLS" || fail "$2"; }
not_saw() { grep -qF -- "$1" "$CALLS" && fail "$2"; return 0; }

echo "[A] usage and argument validation"
run >/dev/null 2>&1
[ "$STATUS" = 2 ] || fail "no subcommand must exit 2 (got $STATUS)"
run nosuch
out="$(out)"
[ "$STATUS" = 2 ] || fail "an unknown subcommand must exit 2 (got $STATUS)"
case "$out" in *"usage: platform"*) : ;; *) fail "usage text not printed" ;; esac
run pf onlyone
out="$(out)"
[ "$STATUS" = 2 ] || fail "pf with one argument must exit 2"
run psql --tenant 'bad;id'
out="$(out)"
[ "$STATUS" -ne 0 ] || fail "an invalid --tenant must not be accepted"
case "$out" in *"--tenant must match"*) : ;; *) fail "invalid --tenant not rejected with the right message: $out" ;; esac
pass "usage, pf arity and --tenant validation"

echo "[B] env reports the slot-derived role and file state"
run env
out="$(out)"
[ "$STATUS" = 0 ] || fail "env exited $STATUS"
case "$out" in *"slot=3"*) : ;; *) fail "slot not derived from the hostname: $out" ;; esac
case "$out" in *"pg_role=dw3_platform_ro"*) : ;; *) fail "wrong role name: $out" ;; esac
case "$out" in *"kubeconfig=$HOME_DIR/.platform/kubeconfig (present)"*) : ;; *) fail "kubeconfig state wrong: $out" ;; esac
mv "$BIN/hostname" "$BIN/hostname.off"
cat >"$BIN/hostname" <<'EOF'
#!/usr/bin/env bash
printf 'some-other-box\n'
EOF
chmod +x "$BIN/hostname"
run env
out="$(out)"
[ "$STATUS" -ne 0 ] || fail "a non dev-worker-N hostname must be a hard error, not a guess"
case "$out" in *"is not dev-worker-N"*) : ;; *) fail "wrong message for a foreign hostname: $out" ;; esac
rm -f "$BIN/hostname"
mv "$BIN/hostname.off" "$BIN/hostname"
pass "slot derivation, role name, and the fail-closed foreign hostname"

echo "[C] kubectl and pf compose the right argv"
: >"$CALLS"
run kubectl get pods >/dev/null
saw "kubectl --kubeconfig=$HOME_DIR/.platform/kubeconfig -n strive-ailab get pods" "kubectl argv is wrong"
: >"$CALLS"
run pf valkey-master 16379:6379 >/dev/null
saw "kubectl --kubeconfig=$HOME_DIR/.platform/kubeconfig -n strive-ailab port-forward svc/valkey-master 16379:6379" "pf argv is wrong"
mv "$HOME_DIR/.platform/kubeconfig" "$WORK/kubeconfig.away"
run kubectl get pods
out="$(out)"
[ "$STATUS" -ne 0 ] || fail "a missing kubeconfig must fail"
case "$out" in *"no kubeconfig at"*) : ;; *) fail "wrong missing-kubeconfig message: $out" ;; esac
mv "$WORK/kubeconfig.away" "$HOME_DIR/.platform/kubeconfig"
pass "kubectl/pf argv and the missing-kubeconfig error"

echo "[D] psql: private forward, replica default, --rw, --tenant, status, no orphan"
: >"$CALLS"
run psql -d airlock -c 'select 1'
out="$(out)"
[ "$STATUS" = 7 ] || fail "psql's exit status must propagate (got $STATUS, expected 7)"
saw "port-forward svc/strive-pg-ro 127.0.0.1:" "psql must default to the READ-ONLY replica service"
saw "-U dw3_platform_ro -d airlock -c select 1" "psql argv is wrong"
saw "PGPASSFILE=$HOME_DIR/.platform/pgpass" "PGPASSFILE not handed to psql"
saw "PGSSLMODE=require" "PGSSLMODE not defaulted to require"
grep -q "psql -h 127.0.0.1 -p [0-9]" "$CALLS" || fail "psql must connect over the loopback forward"
fwd_port="$(sed -n 's/.*127\.0\.0\.1:\([0-9]*\):5432.*/\1/p' "$CALLS" | head -1)"
ready_port="$(sed -n 's/.*pg_isready .*-p \([0-9]*\).*/\1/p' "$CALLS" | head -1)"
psql_port="$(sed -n 's/.*psql -h 127\.0\.0\.1 -p \([0-9]*\) .*/\1/p' "$CALLS" | head -1)"
[ -n "$fwd_port" ] && [ "$fwd_port" = "$ready_port" ] && [ "$fwd_port" = "$psql_port" ] \
  || fail "the forward, the readiness probe and psql must use the SAME port ($fwd_port/$ready_port/$psql_port)"
# The backgrounded port-forward must not outlive the helper (the EXIT trap, i.e. no `exec psql`).
sleep 0.5
pgrep -f "port-forward svc/strive-pg-ro" >/dev/null 2>&1 && fail "the port-forward outlived the helper — is psql exec'd?"
: >"$CALLS"
run psql --rw -d workflow -c 'select 1' >/dev/null
saw "port-forward svc/strive-pg-rw " "--rw must target the primary service"
: >"$CALLS"
run psql --tenant 00000000000000000000000000000001 -d airlock >/dev/null
saw "PGOPTIONS=-c app.tenant_id=00000000000000000000000000000001" "--tenant must reach libpq as app.tenant_id"
: >"$CALLS"
mv "$HOME_DIR/.platform/pgpass" "$WORK/pgpass.away"
run psql -d airlock
out="$(out)"
[ "$STATUS" -ne 0 ] || fail "a missing pgpass must fail"
case "$out" in *"no pgpass at"*) : ;; *) fail "wrong missing-pgpass message: $out" ;; esac
not_saw "port-forward" "a missing pgpass must fail BEFORE opening a port-forward"
mv "$WORK/pgpass.away" "$HOME_DIR/.platform/pgpass"
pass "forward lifecycle, replica default, --rw, --tenant, status propagation, fail-before-forward"

echo "[E] wiring: templates, agent stanzas, tasks, package"
grep -q 'platform_kubeconfig' "$KCTMPL" || fail "the kubeconfig ctmpl must read the platform_kubeconfig field"
grep -q 'platform_pg_user' "$PGTMPL" && grep -q 'platform_pg_password' "$PGTMPL" \
  || fail "the pgpass ctmpl must read platform_pg_user and platform_pg_password"
# libpq format: hostname:port:database:username:password, and the first three MUST be wildcards —
# `platform psql` forwards to a fresh loopback port each time and may target any platform database,
# so a pinned host/port/db would silently stop matching and psql would prompt (or fail) instead.
tail -1 "$PGTMPL" | grep -qF '*:*:*:' \
  || fail "the pgpass credential line must wildcard host:port:database (libpq format)"
tail -1 "$PGTMPL" | grep -qF '{{ .Data.data.platform_pg_user }}:{{ .Data.data.platform_pg_password }}' \
  || fail "the pgpass line must end with the published user:password pair"
grep -q 'platform-kubeconfig.ctmpl' "$HCL" || fail "agent.hcl must render the platform kubeconfig"
grep -q 'platform-pgpass.ctmpl' "$HCL" || fail "agent.hcl must render the platform pgpass"
grep -q 'dev_worker_platform_fields_present' "$HCL" \
  || fail "the platform stanzas must be gated on the pre-flight fact (a stanza for an absent field EXITS the agent)"
grep -q 'error_on_missing_key = true' "$HCL" || fail "agent.hcl must fail closed on a missing key"
grep -q 'dev_worker_platform_fields_present' "$TASKS" || fail "openbao.yml must set the pre-flight fact"
# A DIRECTIVE, not the word: the file explains at length why the fact is deliberately not cached, so
# grepping for "cacheable" would fail on its own documentation.
grep -qE '^\s*cacheable:\s*(true|yes)' "$TASKS" && fail "the pre-flight fact must NOT be cacheable (it must be re-derived every run)"
grep -q 'src: platform$' "$TASKS" || fail "openbao.yml must install the platform helper"
grep -q 'postgresql-client' "$PACKAGES" || fail "packages.yml must install postgresql-client (platform psql needs it)"
pass "ctmpl fields, gated agent stanzas, pre-flight fact, helper install, package"

echo "test-platform-helper: OK"
