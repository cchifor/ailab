#!/usr/bin/env bash
# Self-contained behavioural test for the dev_worker `cred` helper and the openbao-agent wiring
# around it (plain bash — no bats, no molecule, following tests/test-tmux-persistence.sh and
# ansible/roles/gitea_runner/tests/test-cleanup.sh).
#
# WHY THIS EXISTS. `cred` is the only thing standing between an interactive agent and a printed
# credential, and every way it can break is quiet:
#   - the path fallback. `cred get common gitea_pat` has to reach af/dev-workers/common, while a
#     per-worker name has to win over the shared one. Get the order wrong and a worker silently
#     reads someone else's value; get the fallback wrong and the shared PAT is simply unreachable.
#   - `cred exec`. Its whole purpose is that the value never appears on stdout. A refactor that
#     echoed it would still "work" — and would put the secret in an agent transcript.
#   - the sink coupling. The helper's token path, the sink path in agent.hcl, the unit's
#     RuntimeDirectory and the group the unit runs as have to agree. Any single mismatch produces a
#     permission error at 3am on a live worker, and nothing catches it before then.
# Sections [A]-[C] run the REAL helper against a stub `bao` (and a stub `hostname`, so path
# assertions are deterministic); section [D] pins the couplings above by reading the shipped files.
#
# No soft skips and no REQUIRE_* switch: unlike the tmux suite this needs no service to talk to — no
# tmux server, no vault — so every section runs everywhere, including on a developer's Windows box.
#
# Usage: bash ansible/roles/dev_worker/tests/test-cred-helper.sh   (exit 0 = pass)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROLE="$HERE/.."
CRED="$ROLE/files/cred"
HCL="$ROLE/templates/openbao-agent.hcl.j2"
UNIT="$ROLE/templates/openbao-agent.service.j2"
CTMPL="$ROLE/templates/git-credentials.ctmpl.j2"
TASKS="$ROLE/tasks/openbao.yml"
for f in "$CRED" "$HCL" "$UNIT" "$CTMPL" "$TASKS"; do
  [ -r "$f" ] || { echo "FATAL: cannot read $f"; exit 2; }
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"
mkdir -p "$BIN"
CALLS="$WORK/bao-calls.log"
: >"$CALLS"

# Synthetic values. They are the only "secrets" in this suite, so printing them on a failure is
# safe — nothing here ever touches a real vault.
STUB_HOST="dev-worker-test"
TOKEN="stub-sink-token-000111222"
SHARED_VALUE="stub-shared-gitea-pat-abcdef"
HOST_VALUE="stub-host-only-value"
SINK="$WORK/token"
printf '%s' "$TOKEN" >"$SINK"

# ---- stubs ---------------------------------------------------------------------------------------
# bao: logs argv, asserts the helper handed it the sink token + an address, and answers from a canned
# two-entry KV. Exit 2 with bao's own "No value found" wording for anything else, because that is the
# ambiguous failure the helper's fallback has to interpret.
cat >"$BIN/bao" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >>"$CALLS"
[ "\${BAO_TOKEN:-}" = "$TOKEN" ] || { echo "stub bao: BAO_TOKEN is not the sink token" >&2; exit 3; }
[ -n "\${BAO_ADDR:-}" ] || { echo "stub bao: BAO_ADDR unset" >&2; exit 3; }
sub="\$1 \$2"
shift 2
field=""; mount=""; path=""
for a in "\$@"; do
  case "\$a" in
    -field=*) field="\${a#-field=}" ;;
    -mount=*) mount="\${a#-mount=}" ;;
    -*) ;;
    *) path="\$a" ;;
  esac
done
[ "\$mount" = af ] || { echo "stub bao: expected -mount=af, got '\$mount'" >&2; exit 3; }
case "\$sub" in
  "kv get")
    case "\$path|\$field" in
      "dev-workers/common|gitea_pat") printf '%s' "$SHARED_VALUE"; exit 0 ;;
      "dev-workers/$STUB_HOST/personal|token") printf '%s' "$HOST_VALUE"; exit 0 ;;
    esac
    echo "No value found at af/data/\$path" >&2; exit 2 ;;
  "kv list")
    [ "\$path" = "dev-workers" ] && { printf '%s\n' common personal; exit 0; }
    echo "No value found at af/metadata/\$path" >&2; exit 2 ;;
esac
echo "stub bao: unexpected subcommand '\$sub'" >&2; exit 3
EOF
chmod +x "$BIN/bao"

cat >"$BIN/hostname" <<EOF
#!/usr/bin/env bash
printf '%s\n' "$STUB_HOST"
EOF
chmod +x "$BIN/hostname"

# ---- harness -------------------------------------------------------------------------------------
fails=0
ok() { echo "  PASS: $1"; }
bad() {
  echo "  FAIL: $1"
  fails=$((fails + 1))
}
assert_eq() { # <got> <want> <label>
  if [ "$1" = "$2" ]; then ok "$3"; else
    bad "$3"
    echo "    got:  '$1'"
    echo "    want: '$2'"
  fi
}
assert_contains() { # <haystack> <needle> <label>
  case "$1" in
  *"$2"*) ok "$3" ;;
  *)
    bad "$3"
    echo "    looked for: '$2'"
    echo "    in:         '$1'"
    ;;
  esac
}

TOKEN_FILE_CUR="$SINK"
OUT=""; ERR=""; RC=0
run_cred() { # <args...>; sets OUT/ERR/RC. Runs the REAL file through sh(1) rather than bash — on a
  # Debian/Ubuntu runner that is dash, which is what makes the helper's POSIX-sh claim mean something.
  : >"$CALLS"
  PATH="$BIN:$PATH" CRED_TOKEN_FILE="$TOKEN_FILE_CUR" BAO_ADDR="https://openbao.invalid:30820" \
    sh "$CRED" "$@" >"$WORK/out" 2>"$WORK/err"
  RC=$?
  OUT="$(cat "$WORK/out")"
  ERR="$(cat "$WORK/err")"
}

# ---- [A] name resolution ---------------------------------------------------------------------------
echo "[A] name resolution (host subtree first, shared second)"
run_cred get common gitea_pat
assert_eq "$RC" "0" "A1a: 'cred get common gitea_pat' succeeds"
assert_eq "$OUT" "$SHARED_VALUE" "A1b: ...and prints the shared value"
# Non-vacuity for A1: it must have TRIED the host path first. Without this, a helper that only ever
# reads the shared path passes A1 while silently ignoring per-worker secrets.
assert_contains "$(sed -n 1p "$CALLS")" "dev-workers/$STUB_HOST/common" "A1c: the host path is attempted first"
assert_contains "$(sed -n 2p "$CALLS")" "dev-workers/common" "A1d: ...then the shared path"

run_cred get personal token
assert_eq "$OUT" "$HOST_VALUE" "A2a: a name that exists per-host resolves to the host value"
assert_eq "$(grep -c 'kv get' "$CALLS")" "1" "A2b: ...and the shared path is not consulted after a hit"

run_cred get nope missing
assert_eq "$RC" "1" "A3a: an unresolvable name exits 1"
assert_eq "$OUT" "" "A3b: ...prints nothing on stdout"
assert_contains "$ERR" "af/dev-workers/nope" "A3c: ...and names the shared candidate it tried"
assert_contains "$ERR" "af/dev-workers/$STUB_HOST/nope" "A3d: ...and the host candidate"

run_cred list
assert_eq "$RC" "0" "A4a: 'cred list' succeeds"
assert_contains "$OUT" "common" "A4b: ...and lists the shared subtree"

# ---- [B] exec injection ----------------------------------------------------------------------------
# The reason this helper exists: the value reaches a child process's environment, never the terminal.
echo "[B] exec injection"
# shellcheck disable=SC2016 # the CHILD shell must expand it — that is the property under test
run_cred exec common gitea_pat GITEA_PAT -- sh -c 'printf %s "${#GITEA_PAT}"'
assert_eq "$RC" "0" "B1a: 'cred exec' runs the command"
assert_eq "$OUT" "${#SHARED_VALUE}" "B1b: ...with the secret in the named env var (length echoed)"
case "$OUT$ERR" in
*"$SHARED_VALUE"*) bad "B2: the value leaked into cred's own output" ;;
*) ok "B2: the value never appears in cred's stdout/stderr" ;;
esac

run_cred exec common gitea_pat GITEA_PAT -- sh -c 'exit 7'
assert_eq "$RC" "7" "B3: the child's exit status is cred's (exec, not a wrapper)"

run_cred exec common gitea_pat GITEA_PAT sh -c 'true'
assert_eq "$RC" "2" "B4: a missing '--' separator is a usage error"

# ---- [C] failure modes -----------------------------------------------------------------------------
echo "[C] failure modes"
TOKEN_FILE_CUR="$WORK/no-such-token"
run_cred list
assert_eq "$RC" "1" "C1a: an unreadable sink token exits 1"
assert_contains "$ERR" "openbao-agent group" "C1b: ...and points at the group membership (the usual cause)"
assert_contains "$ERR" "openbao-agent.service" "C1c: ...and at the service"
TOKEN_FILE_CUR="$SINK"

run_cred
assert_eq "$RC" "2" "C2: no subcommand is a usage error"
run_cred get onlyone
assert_eq "$RC" "2" "C3: 'get' with a missing field argument is a usage error"
run_cred frobnicate
assert_eq "$RC" "2" "C4: an unknown subcommand is a usage error"

run_cred list
case "$OUT$ERR" in
*"$TOKEN"*) bad "C5: the sink token leaked into cred's output" ;;
*) ok "C5: the sink token never appears in cred's output" ;;
esac

# ---- [D] wiring (each of these breaks silently — nothing goes red until an agent needs a secret) ----
echo "[D] wiring"
sink_path="$(sed -n 's/^[[:space:]]*path[[:space:]]*=[[:space:]]*"\(.*\)"[[:space:]]*$/\1/p' "$HCL" | head -1)"
# shellcheck disable=SC2016 # a sed program, not a shell expansion
helper_path="$(sed -n 's/^TOKEN_FILE="${CRED_TOKEN_FILE:-\(.*\)}"$/\1/p' "$CRED" | head -1)"
if [ -z "$sink_path" ]; then
  bad "D1: no sink path found in openbao-agent.hcl.j2 — the agent would write no token at all"
else
  assert_eq "$helper_path" "$sink_path" "D1: cred's default token path is the agent's sink path"
fi

unit_runtime_dir="$(sed -n 's/^RuntimeDirectory=\(.*\)$/\1/p' "$UNIT" | head -1)"
assert_eq "/run/$unit_runtime_dir" "$(dirname "$sink_path")" "D2: systemd creates the directory the sink writes into"

unit_group="$(sed -n 's/^Group=\(.*\)$/\1/p' "$UNIT" | head -1)"
if [ -z "$unit_group" ]; then
  bad "D3: openbao-agent.service.j2 has no Group= — the sink would be root:root and cred unusable"
else
  # The group is the entire access mechanism, so the unit, the ansible group creation, the
  # membership loop and the helper's error message all have to name the same one.
  if grep -q "name: $unit_group" "$TASKS" && grep -q "groups: $unit_group" "$TASKS"; then
    ok "D3a: openbao.yml creates '$unit_group' and adds the dev-worker users to it"
  else
    bad "D3a: openbao.yml does not both create '$unit_group' and add the users to it"
  fi
  if grep -q "$unit_group group" "$CRED"; then
    ok "D3b: cred's permission error names the '$unit_group' group"
  else
    bad "D3b: cred's permission error does not name the '$unit_group' group"
  fi
fi

unit_bin="$(sed -n 's/^ExecStart=\([^ ]*\).*/\1/p' "$UNIT" | head -1)"
task_bin_dir="$(grep -A6 'Install the bao CLI' "$TASKS" | sed -n 's/^[[:space:]]*dest:[[:space:]]*\(.*\)$/\1/p' | head -1)"
if [ -z "$task_bin_dir" ]; then
  bad "D4: openbao.yml has no 'Install the bao CLI' task with a dest — the unit's binary is never installed"
else
  assert_eq "$(dirname "$unit_bin")" "$task_bin_dir" "D4: the unit runs the bao the role actually installs"
fi

cred_dest="$(grep -A6 'src: cred' "$TASKS" | sed -n 's/^[[:space:]]*dest:[[:space:]]*\(.*\)$/\1/p' | head -1)"
cred_mode="$(grep -A6 'src: cred' "$TASKS" | sed -n 's/^[[:space:]]*mode:[[:space:]]*"\(.*\)"$/\1/p' | head -1)"
assert_eq "$cred_dest" "/usr/local/bin/cred" "D5a: openbao.yml installs the helper on PATH"
case "$cred_mode" in
*[1357]) ok "D5b: ...executable ($cred_mode)" ;;
*) bad "D5b: the helper is installed mode '$cred_mode' — users invoke it directly, so it must be executable" ;;
esac

# KV-v2 reads go through the data/ segment. `af/dev-workers/common` is a valid-looking path that
# simply resolves to nothing, so a template missing the segment renders an EMPTY .git-credentials
# and git falls back to prompting — with no error anywhere.
if grep -q 'secret "af/data/dev-workers/' "$CTMPL"; then
  ok "D6: the git-credentials template reads the KV-v2 data/ path"
else
  bad "D6: git-credentials.ctmpl.j2 does not read a 'af/data/dev-workers/...' path"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "ALL PASS"
  exit 0
else
  echo "$fails CHECK(S) FAILED"
  exit 1
fi
