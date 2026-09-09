#!/usr/bin/env bash
# Functional tests for the infra-pg nightly dump script embedded in
# kubernetes/apps/databases/infra-pg-dump-cronjob.yaml.
#
# WHY THIS EXISTS. `kubeconform` validates the CronJob object; nothing validates the 130 lines of
# bash inside it, and that bash decides whether a good backup is kept or deleted. Every case below
# is a defect the codex implementation review found in the first version:
#
#   #2 (blocker) an unchecked COMPLETE/SHA256SUMS/sync failure could still be followed by a
#                successful rename, a prune of older generations, and DUMP_OK
#   #3           abandoned staging was swept only AFTER publication, so once interrupted runs ate
#                the free space every later run died at the space check and never reached the sweep
#   #4           retention counted incomplete directories toward the 7-generation allowance
#   #5           `for db in $DBS` word-split a database named `sales archive` into two targets, and
#                a name containing / would have escaped the staging directory
#
# Run: bash scripts/tests/test-infra-pg-dump.sh
# It extracts the script from the manifest, so editing the YAML is enough to re-test it.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
MANIFEST="$REPO/kubernetes/apps/databases/infra-pg-dump-cronjob.yaml"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
# python3 on Linux/CI, `python` on the Windows workstation -- pick whichever exists rather
# than failing on the one that does not.
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
"$PY" - "$MANIFEST" "$WORK/dump.sh" <<'EXTRACT'
import sys, yaml, pathlib
docs = [d for d in yaml.safe_load_all(open(sys.argv[1], encoding="utf-8")) if d]
cj = [d for d in docs if d["kind"] == "CronJob"][0]
pathlib.Path(sys.argv[2]).write_bytes(
    cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"][0].encode())
EXTRACT
bash -n "$WORK/dump.sh" || { echo "extracted script is not valid bash"; exit 1; }
SCRIPT="$WORK/dump.sh"
ROOT="$(mktemp -d)"
export PATH="$ROOT/bin:$PATH"
mkdir -p "$ROOT/bin" "$ROOT/dumps"
PASS=0; FAIL=0
ok(){ PASS=$((PASS+1)); echo "  PASS $1"; }
bad(){ FAIL=$((FAIL+1)); echo "  FAIL $1 -- $2"; }

# ---- stubs. pg_dump parses -f POSITIONALLY, the way the real binary does; a regex over a
# ---- flattened argv truncates at a space and would fake the very bug under test.
cat > "$ROOT/bin/pg_dump" <<'EOF'
#!/usr/bin/env bash
OUT=""
while [ $# -gt 0 ]; do
  [ "$1" = "-f" ] && { OUT="$2"; shift; }
  shift
done
[ -n "${PGDUMP_FAIL:-}" ] && exit 1
: > "$OUT"
EOF
cat > "$ROOT/bin/pg_dumpall" <<'EOF'
#!/usr/bin/env bash
echo "-- globals"
EOF
cat > "$ROOT/bin/pg_restore" <<'EOF'
#!/usr/bin/env bash
echo "1; TABLE public t"
EOF
cat > "$ROOT/bin/psql" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' $PSQL_DBS
EOF
chmod +x "$ROOT/bin"/*

sed "s#/dumps#$ROOT/dumps#g" "$SCRIPT" > "$ROOT/run.sh"
run() { ( cd "$ROOT" && PGUSER=u PGPASS=p bash "$ROOT/run.sh" ) >"$ROOT/out.txt" 2>&1; echo $?; }
gens_complete() { for d in "$ROOT"/dumps/[0-9]*; do [ -d "$d" ] && [ -f "$d/COMPLETE" ] && echo "$d"; done; }
gens_incomplete() { for d in "$ROOT"/dumps/[0-9]*; do [ -d "$d" ] && [ ! -f "$d/COMPLETE" ] && echo "$d"; done; }

echo "== 1. happy path publishes a complete generation =="
rm -rf "${ROOT:?}/dumps"/*
export PSQL_DBS="alpha beta"; unset PGDUMP_FAIL
RC=$(run); GEN=$(gens_complete | head -1)
{ [ "$RC" = 0 ] && [ -n "$GEN" ]; } && ok "publishes with COMPLETE" || bad "publishes with COMPLETE" "rc=$RC"
[ -f "$GEN/SHA256SUMS" ] && ok "writes SHA256SUMS" || bad "writes SHA256SUMS" "missing"
[ -f "$GEN/globals.sql" ] && ok "captures globals" || bad "captures globals" "missing"
[ -f "$GEN/alpha.dump" ] && [ -f "$GEN/beta.dump" ] && ok "dumps every enumerated database" || bad "dumps every db" "$(ls "$GEN")"
ls "$GEN" | grep -q '\.err$' && bad "no .err files published" "leaked" || ok "no .err files published"

echo "== 2. codex #5: a database name containing a space is ONE target =="
rm -rf "${ROOT:?}/dumps"/*
export PSQL_DBS="'sales archive'"
# shellcheck disable=SC2086
RC=$(PSQL_DBS="sales archive" run)   # the stub prints it unquoted -> two lines; use a real one:
printf '%s\n' 'sales archive' > "$ROOT/one.txt"
cat > "$ROOT/bin/psql" <<EOF
#!/usr/bin/env bash
cat "$ROOT/one.txt"
EOF
chmod +x "$ROOT/bin/psql"
rm -rf "${ROOT:?}/dumps"/*
RC=$(run); GEN=$(gens_complete | head -1)
[ -f "$GEN/sales archive.dump" ] && ok "space in database name handled as one target" || bad "space in db name" "$(ls "$GEN" 2>/dev/null | tr '\n' ' ')"

echo "== 3. codex #5: a name containing / is REFUSED, not written outside staging =="
printf '%s\n' 'evil/../../escape' > "$ROOT/one.txt"
rm -rf "${ROOT:?}/dumps"/*
RC=$(run)
[ "$RC" != 0 ] && ok "unsafe name fails the run" || bad "unsafe name fails" "rc=$RC"
grep -q "REFUSING unsafe database name" "$ROOT/out.txt" && ok "unsafe name is refused explicitly" || bad "refusal message" "$(tail -3 "$ROOT/out.txt")"

echo "== 4. a failed pg_dump discards staging and keeps previous generations =="
printf '%s\n' alpha > "$ROOT/one.txt"
rm -rf "${ROOT:?}/dumps"/*
RC=$(run); PREV=$(gens_complete | head -1)
export PGDUMP_FAIL=1
RC=$(run); unset PGDUMP_FAIL
[ "$RC" != 0 ] && ok "failed dump exits non-zero" || bad "failed dump exits non-zero" "rc=$RC"
[ -d "$PREV" ] && ok "previous generation survives a failed run" || bad "previous survives" "deleted"
[ -z "$(ls -d "$ROOT"/dumps/.staging-* 2>/dev/null)" ] && ok "staging removed on failure (trap)" || bad "staging removed" "left behind"

echo "== 5. codex #2 BLOCKER: a failed COMPLETE marker must NOT publish and must NOT prune =="
rm -rf "${ROOT:?}/dumps"/*
RC=$(run); G1=$(gens_complete | head -1)
cat > "$ROOT/bin/date" <<'EOF'
#!/usr/bin/env bash
[ "$1" = "-u" ] && [ "$2" = "+%Y-%m-%dT%H:%M:%SZ" ] && exit 1
exec /usr/bin/date "$@"
EOF
chmod +x "$ROOT/bin/date"
RC=$(run)
rm -f "$ROOT/bin/date"
[ "$RC" != 0 ] && ok "marker failure exits non-zero" || bad "marker failure exits non-zero" "rc=$RC"
[ -d "$G1" ] && ok "marker failure does NOT prune the good generation" || bad "does not prune" "G1 gone"
[ "$(gens_complete | wc -l)" = 1 ] && ok "no half-generation published" || bad "no half-generation" "$(gens_complete | wc -l)"
grep -q "not publishing" "$ROOT/out.txt" && ok "marker failure says why" || bad "marker failure message" "$(tail -3 "$ROOT/out.txt")"

echo "== 6. codex #4: incomplete directories do not consume retention slots =="
rm -rf "${ROOT:?}/dumps"/*
for i in 1 2 3 4 5 6 7; do mkdir -p "$ROOT/dumps/2026010${i}T000000Z"; touch "$ROOT/dumps/2026010${i}T000000Z/COMPLETE"; done
for i in 1 2 3; do mkdir -p "$ROOT/dumps/2025010${i}T000000Z"; done
RC=$(run)
C=$(gens_complete | wc -l); I=$(gens_incomplete | wc -l)
[ "$C" = 7 ] && ok "retention keeps exactly 7 COMPLETE generations (got $C)" || bad "retention keeps 7" "got $C"
[ "$I" = 3 ] && ok "incomplete dirs left alone, not counted (got $I)" || bad "incomplete untouched" "got $I"

echo "== 7. codex #3: abandoned staging is swept BEFORE the space check =="
rm -rf "${ROOT:?}/dumps"/*
mkdir -p "$ROOT/dumps/.staging-OLD"
touch -d '2 days ago' "$ROOT/dumps/.staging-OLD"
RC=$(run)
[ ! -d "$ROOT/dumps/.staging-OLD" ] && ok "stale staging swept" || bad "stale staging swept" "still present"

echo
echo "  passed $PASS, failed $FAIL"
rm -rf "$ROOT"
[ "$FAIL" = 0 ]
