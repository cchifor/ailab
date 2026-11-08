#!/usr/bin/env bash
# gitea-owner-op.sh OPS_FILE [--var NAME=VALUE]... [--execute | --dry-run] - run a fixed list of owner-only Gitea
# API calls with ONE one-shot owner token. The procedure around it: docs/runbooks/owner-credentials.md.
#
# A DRY RUN BY DEFAULT. Without --execute it validates the ops file, prints the plan and exits: it never calls
# kubectl or curl and mints nothing.
#
# For the owner operations that gitea-owner-merge.sh does not cover (repository settings, collaborators, topics,
# branch protection). Same rules (auth-hardening plan, ruling R13): no standing owner credential exists, so with
# --execute it mints a token for the site-admin owner `chifor` inside the Gitea pod (cluster-admin `kubectl exec`)
# with the scopes the ops file declares, makes the calls, and deletes the token from the database at once.
#
# OPS_FILE: one operation per line, run in order; blank lines and `#` comment lines are ignored, except
#   # scopes: write:repository[,write:organization...]
# which is REQUIRED before the first operation and sets the token's scopes: Gitea's scope names, the smallest set
# (no `all`, no duplicate, no read:X next to write:X). An operation line is
#   METHOD PATH BODY_FILE|- EXPECTED_CODES
# METHOD is GET, POST, PUT, PATCH or DELETE. PATH is an API path under the fixed origin, held to gitea-api.sh's
# character set (so no host) and refused if it contains // or a "..". BODY_FILE is a JSON file relative to the ops
# file's directory (not absolute, no ".."), or - for none. EXPECTED_CODES is a comma list of HTTP statuses (204,
# or 200,201).
# Every ${NAME} in PATH and in a body file is replaced by --var NAME=VALUE (VALUE matches ^[A-Za-z0-9._-]+$). An
# unreplaced placeholder, a --var that nothing uses, or a body that is not valid JSON after the substitution is
# refused (exit 2) before anything else happens. Presets: scripts/owner-ops/*.ops.
#
# With --execute:
#   - the token is named `owner-op-<ops file name>-<--var values, by NAME>-<utc>-<8 random hex>`. The random part
#     keeps two runs in the same second apart, so a run never deletes another run's token. The name is set
#     BEFORE the mint, so the trap deletes the token by name even if the mint is interrupted or answers something
#     that is not a token. The value lives in a shell variable and reaches the fixed origin only on curl's STDIN
#     (`-q ... -H @-`): never an argument, never printed;
#   - every call is bounded (10 s to connect, 60 s in all), so a server that stops answering cannot hold the
#     token past the deadline: the call counts as failed (HTTP 000) and the token is deleted;
#   - the calls run in order and STOP at the first status outside EXPECTED_CODES: its status and the first 300
#     bytes of its body are printed, the remaining calls are not made;
#   - responses go to a private temp dir that is removed on exit (nothing is written to the current directory);
#   - a trap on EXIT/INT/TERM/HUP deletes the token by name on the infra-pg primary, proves it dead (gone from
#     the database, and its next use answers 401) and lists the owner's remaining tokens by name.
# Exit: 0 every call answered as expected (dry run: the ops file is valid); 2 bad input, nothing called;
# 4 the mint or an operation failed (the token was deleted); 5 the deletion was NOT confirmed: run the printed
# psql delete now.
#   scripts/gitea-owner-op.sh scripts/owner-ops/renovate-onboard.ops --var REPO=relay             # dry run
#   scripts/gitea-owner-op.sh scripts/owner-ops/renovate-onboard.ops --var REPO=relay --execute
#   scripts/gitea-owner-op.sh scripts/owner-ops/repository-template-settings.ops --execute
set -uo pipefail
set +x # never trace: a traced assignment would print the token

ORIGIN="https://git.chifor.me/api/v1"
OWNER="chifor"
KCTX="admin@ai"

ts() { date -u +%H:%M:%SZ; }
log() { echo "[$(ts)] $*"; }
stop() { local code=$1; shift; echo "[$(ts)] STOP: $*" >&2; exit "$code"; }

USAGE="usage: gitea-owner-op.sh OPS_FILE [--var NAME=VALUE]... [--execute | --dry-run]"
OPS=""; NOPS=0; VARS=(); EXECUTE=""; DRY=""
while [ $# -gt 0 ]; do
  case $1 in
    --execute) EXECUTE=1 ;;
    --dry-run) DRY=1 ;;
    --var) { [ $# -ge 2 ] && [ "${2#-}" = "$2" ]; } || stop 2 "--var needs NAME=VALUE; $USAGE"; VARS+=("$2"); shift ;;
    --var=*) VARS+=("${1#--var=}") ;;
    -*) stop 2 "unknown option $1; $USAGE" ;;
    *) OPS=$1; NOPS=$((NOPS + 1)) ;;
  esac
  shift
done
[ "$NOPS" -eq 1 ] && [ -n "$OPS" ] || stop 2 "$USAGE"
[ -z "$EXECUTE" ] || [ -z "$DRY" ] || stop 2 "--execute and --dry-run exclude each other"
[ -f "$OPS" ] || stop 2 "no ops file $OPS"

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import json' >/dev/null 2>&1; then PY=$c; break; fi
done
[ -n "$PY" ] || stop 2 "no working python3 or python"
py() { "$PY" "$@" | tr -d '\r'; } # Windows Python ends lines with CRLF

kube() { kubectl --context "$KCTX" "$@"; }

# owner_call METHOD PATH BODY_FILE|"" OUT: the HTTP status on stdout ("000" on a transport failure), the body in
# OUT. Same transport rules as gitea-api.sh: -q FIRST (no ~/.curlrc can add tracing or URLs), https only, no
# redirects, the header on stdin. Bounded: a stalled server ends the call (curl exit 28 -> 000) instead of
# holding the live token, and the revocation probe below uses the same bound.
owner_call() {
  local code args=(-q --proto '=https' --max-redirs 0 --connect-timeout 10 --max-time 60 -sS -o "$4"
    -w '%{http_code}' -X "$1" -H @-)
  [ -n "$EXECUTE" ] || stop 4 "internal error: owner call without --execute"
  [ -n "$3" ] && args+=(-H 'Content-Type: application/json' --data-binary "@$3")
  : > "$4"
  code=$(printf 'Authorization: token %s\n' "$TOKEN" | curl "${args[@]}" "$ORIGIN$2") || code=000
  echo "$code"
}

psql_gitea() { kube -n databases exec -i "$DB_POD" -c postgres -- psql -U postgres -d gitea -tA -v ON_ERROR_STOP=1 -f -; }

# The pods are looked up by label, not by name, and only Running ones count (a failover moves the primary).
gitea_pod() {
  kube -n gitea get pod -l app.kubernetes.io/name=gitea,app.kubernetes.io/instance=gitea \
    --field-selector=status.phase=Running -o name 2>/dev/null | head -1 | sed 's|^pod/||'
}
db_primary() {
  kube -n databases get pod -l cnpg.io/cluster=infra-pg,cnpg.io/instanceRole=primary \
    --field-selector=status.phase=Running -o name 2>/dev/null | head -1 | sed 's|^pod/||'
}

# revoke: delete the live owner token by NAME, then prove it dead: the database no longer lists it and (when the
# value is known) its next use answers 401. Returns 1, loudly, with the delete to run, when either proof fails.
# The primary is looked up again first: a failover since the mint must not send the delete to a replica.
revoke() {
  [ -n "$NAME" ] || return 0
  local name=$NAME code="(no token value)" left bad="" sql primary
  primary=$(db_primary)
  [ -z "$primary" ] || DB_POD=$primary
  sql="delete from access_token where uid=(select id from \"user\" where name='$OWNER') and name='$name';"
  printf '%s\n' "$sql" | psql_gitea >/dev/null 2>&1
  left=$(printf "select coalesce(string_agg(name, ',' order by id), '') from access_token where uid=(select id from \"user\" where name='%s');\n" \
    "$OWNER" | psql_gitea 2>/dev/null | tr -d '\r') || left="<unreadable>"
  if [ -n "$TOKEN" ]; then
    code=$(owner_call GET /user "" "$WORK/revoked")
    [ "$code" = 401 ] || bad="its next use answered HTTP $code"
  fi
  NAME=""; TOKEN=""
  case ",$left," in *",$name,"*|",<unreadable>,") bad="${bad:+$bad; }the database still lists it (or could not be read)" ;; esac
  log "owner token $name deleted: next use -> HTTP $code; $OWNER tokens now: ${left:-none}"
  if [ -n "$bad" ]; then
    {
      echo "[$(ts)] REVOCATION NOT CONFIRMED for owner token $name: $bad."
      echo "Delete it NOW on the infra-pg primary (database gitea), then list the $OWNER tokens again:"
      echo "  $sql"
      echo "  e.g. kubectl --context $KCTX -n databases exec -it ${DB_POD:-<the infra-pg primary>} -c postgres -- psql -U postgres -d gitea"
      echo "GiteaOwnerTokenStanding fires if a $OWNER token survives 4 h."
    } >&2
    return 1
  fi
}

WORK=$(mktemp -d) || stop 2 "mktemp failed"
chmod 700 "$WORK"
NAME=""   # the live owner token's name, set BEFORE the mint so the trap can always delete it
TOKEN=""  # its value: never printed, never an argument
DB_POD=""
on_exit() {
  local rc=$?
  # A second INT/TERM/HUP must not abort the revocation (its kubectl children inherit the ignore).
  trap '' INT TERM HUP
  revoke || rc=5
  rm -rf "$WORK"
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# The validator: reads the ops file and the --var values, writes each substituted body to WORK/body.N.json and
# prints the plan as tab-separated lines (NAME, SCOPES, then one OP line per operation). On any error it prints
# every problem as an ERROR line and exits 2. It never touches the network.
cat > "$WORK/plan.py" <<'PY'
import json, os, re, sys

# Must stay equal to PATH_RE in scripts/gitea-api.sh (scripts/tests/test_gitea_owner_op.py checks it).
PATH_RE = r'^/[A-Za-z0-9/_.,-]+(\?[A-Za-z0-9=&_.,-]*)?$'
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
# Gitea's token scopes. `all` is refused: an ops file names the smallest set its calls need.
CATEGORIES = ("activitypub", "admin", "issue", "misc", "notification", "organization", "package", "repository",
              "user")
SCOPES = {f"{a}:{c}" for a in ("read", "write") for c in CATEGORIES}
VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
VAR_VALUE = re.compile(r"[A-Za-z0-9._-]+")
PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
CODES = re.compile(r"[1-5][0-9]{2}(,[1-5][0-9]{2})*")
BODY_NAME = re.compile(r"[A-Za-z0-9._/-]+")
SCOPES_LINE = re.compile(r"#\s*scopes:\s*(.*?)\s*")
OPS_NAME = re.compile(r"[A-Za-z0-9._-]+")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

work, ops = sys.argv[1], sys.argv[2]
errors = []


def err(msg):
    errors.append(msg)


def finish():
    if errors:
        for e in errors:
            print("ERROR\t" + e)
        sys.exit(2)


values = {}
rejected = set()  # given, but refused above: its placeholders are not reported again as missing
for a in sys.argv[3:]:
    name, sep, value = a.partition("=")
    if not sep or not VAR_NAME.fullmatch(name):
        err(f"--var {a!r}: give NAME=VALUE (NAME: letters, digits and _)")
    elif not VAR_VALUE.fullmatch(value):
        err(f"--var {name}: the value must match ^[A-Za-z0-9._-]+$")
        rejected.add(name)
    elif name in values:
        err(f"--var {name} is given twice")
    else:
        values[name] = value

base = os.path.basename(ops)
opsname = base[:-4] if base.endswith(".ops") else base
if not OPS_NAME.fullmatch(opsname):
    err(f"the ops file name {base!r} must match ^[A-Za-z0-9._-]+$ (it names the token)")
here = os.path.dirname(os.path.abspath(ops))
try:
    with open(ops, "rb") as f:
        text = f.read().decode("utf-8")
except (OSError, UnicodeDecodeError) as e:
    err(f"cannot read {ops}: {e}")
    finish()

used = set()


def subst(s, where):
    """Replace every ${NAME} from --var; report a missing value or a malformed placeholder."""
    def rep(m):
        n = m.group(1)
        if n not in values:
            if n not in rejected:
                err(f"{where}: ${{{n}}} has no --var {n}=VALUE")
            return m.group(0)
        used.add(n)
        return values[n]
    out = PLACEHOLDER.sub(rep, s)
    if "${" in PLACEHOLDER.sub("", out):
        err(f"{where}: a malformed placeholder (only ${{NAME}}, NAME of letters, digits and _)")
    return out


def no_constants(c):
    raise ValueError(f"{c} is not JSON")


def check_scopes(spec, where):
    out = []
    for s in (x.strip() for x in spec.split(",")):
        if s == "all":
            err(f"{where}: scope 'all' is refused: name the smallest set the operations need")
        elif s not in SCOPES:
            err(f"{where}: unknown scope {s!r} (known: read:/write: + {', '.join(CATEGORIES)})")
        elif s in out:
            err(f"{where}: scope {s} is listed twice")
        else:
            out.append(s)
    for s in out:
        if s.startswith("read:") and "write:" + s[5:] in out:
            err(f"{where}: {s} is redundant next to write:{s[5:]}")
    return out


scopes = None
plan = []
for n, line in enumerate(text.split("\n"), 1):
    line = line.strip()
    where = f"{base}:{n}"
    if not line:
        continue
    if line.startswith("#"):
        m = SCOPES_LINE.fullmatch(line)
        if m:
            if scopes is not None:
                err(f"{where}: a second scopes line")
            elif plan:
                err(f"{where}: the scopes line must come before the first operation")
            scopes = check_scopes(m.group(1), where)
        continue
    fields = line.split()
    if len(fields) != 4:
        err(f"{where}: want METHOD PATH BODY_FILE|- EXPECTED_CODES, got {len(fields)} field(s)")
        continue
    method, path, body, expect = fields
    i = len(plan) + 1
    if method not in METHODS:
        err(f"{where}: method {method!r} is not GET, POST, PUT, PATCH or DELETE")
    path = subst(path, where)
    if "${" not in path and (not re.fullmatch(PATH_RE, path) or "//" in path or ".." in path):
        err(f"{where}: {path!r} is not an API path (gitea-api.sh's character set, no host, no //, no ..)")
    if not CODES.fullmatch(expect):
        err(f"{where}: expected codes {expect!r} must be a comma list of HTTP statuses, such as 204 or 200,201")
    desc = "no body"
    has_body = "-"
    if body != "-":
        parts = body.split("/")
        if not BODY_NAME.fullmatch(body) or body.startswith("/") or ".." in parts:
            err(f"{where}: body file {body!r} must be a path relative to the ops file's directory, without ..")
        else:
            src = os.path.join(here, *parts)
            try:
                with open(src, "rb") as f:
                    raw = f.read().decode("utf-8")
            except (OSError, UnicodeDecodeError) as e:
                err(f"{where}: cannot read body file {body}: {e}")
                raw = None
            if raw is not None:
                nerr = len(errors)
                filled = subst(raw, f"{where} ({body})")
                if len(errors) == nerr:
                    try:
                        doc = json.loads(filled, parse_constant=no_constants)
                    except ValueError as e:
                        err(f"{where}: body file {body} is not valid JSON after the substitution: {e}")
                    else:
                        with open(os.path.join(work, f"body.{i}.json"), "wb") as f:
                            f.write(filled.encode("utf-8"))
                        has_body = "1"
                        if isinstance(doc, dict):
                            desc = f"body {body} {{{', '.join(doc)}}}"
                        else:
                            desc = f"body {body} ({type(doc).__name__})"
    plan.append((i, method, path, has_body, expect, re.sub(r"[\x00-\x1f]", " ", desc)))

if scopes is None:
    err(f"{base}: no '# scopes: ...' line: declare the token's scopes (the smallest set) before the first operation")
if not plan:
    err(f"{base}: no operations")
if not errors:
    for n in sorted(set(values) - used):
        err(f"--var {n} is not used by {base}")
prefix = "-".join(["owner-op", opsname] + [values[k] for k in sorted(values)])
if len(prefix) > 200:
    err(f"the token name {prefix[:40]}... would be longer than 200 characters")
finish()
print(f"NAME\t{prefix}")
print(f"SCOPES\t{','.join(scopes)}")
for op in plan:
    print("OP\t" + "\t".join(str(x) for x in op))
PY

PLAN=$(py "$WORK/plan.py" "$WORK" "$OPS" ${VARS[@]+"${VARS[@]}"})
rc=$?
if [ "$rc" -ne 0 ]; then
  tab=$'\t' # not sed's \t: BSD sed reads it as a plain t and would drop every line
  while IFS= read -r line; do
    case $line in "ERROR$tab"*) printf '  %s\n' "${line#"ERROR$tab"}" >&2 ;; esac
  done <<< "$PLAN"
  stop 2 "$OPS: invalid ops file or --var values (validator exit $rc); nothing called"
fi

PREFIX=""; SCOPES=""; M=(); P=(); B=(); X=(); D=()
while IFS=$'\t' read -r kind a b c d e f; do
  case $kind in
    NAME) PREFIX=$a ;;
    SCOPES) SCOPES=$a ;;
    OP) M+=("$b"); P+=("$c"); B+=("$d"); X+=("$e"); D+=("$f") ;;
  esac
done <<< "$PLAN"
N=${#M[@]}
{ [ -n "$PREFIX" ] && [ -n "$SCOPES" ] && [ "$N" -gt 0 ]; } || stop 2 "internal error: the validator printed no plan"

if [ -n "$EXECUTE" ]; then RUNKIND="EXECUTE"; else RUNKIND="DRY RUN"; fi
log "$RUNKIND: $OPS${VARS[*]+ (${VARS[*]})}: $N call(s) to $ORIGIN with ONE owner token" \
  "$PREFIX-<utc>-<random>, scopes $SCOPES"
for ((i = 0; i < N; i++)); do
  log "  $((i + 1)). ${M[$i]} ${P[$i]}  ${D[$i]}  expect ${X[$i]}"
done
if [ -z "$EXECUTE" ]; then
  log "DRY RUN: nothing minted or called. Re-run with --execute to do it."
  exit 0
fi

GITEA_POD=$(gitea_pod)
DB_POD=$(db_primary)
[ -n "$GITEA_POD" ] && [ -n "$DB_POD" ] || stop 4 "cannot find the Gitea pod or the infra-pg primary; nothing minted"

# 8 random hex characters: unique per run (two runs in the same second must not share a name, or one run's
# delete-by-name would revoke the other's token) and safe inside the SQL string literal of the delete.
RAND=$(od -An -N4 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')
[[ "$RAND" =~ ^[0-9a-f]{8}$ ]] || stop 4 "cannot read 4 random bytes for the token name; nothing minted"
NAME="$PREFIX-$(date -u +%Y%m%d%H%M%S)-$RAND"
TOKEN=$(kube -n gitea exec "$GITEA_POD" -c gitea -- gitea admin user generate-access-token --username "$OWNER" \
  --token-name "$NAME" --scopes "$SCOPES" --raw 2>/dev/null | tr -d '\r\n')
if ! [[ "$TOKEN" =~ ^[0-9a-f]{40}$ ]]; then
  TOKEN=""
  stop 4 "owner token $NAME: the mint did not return a token"
fi
log "owner token $NAME minted (scopes $SCOPES)"

for ((i = 0; i < N; i++)); do
  body=""
  [ "${B[$i]}" = - ] || body="$WORK/body.$((i + 1)).json"
  out="$WORK/resp.$((i + 1))"
  code=$(owner_call "${M[$i]}" "${P[$i]}" "$body" "$out")
  case ",${X[$i]}," in
    *",$code,"*) log "$((i + 1))/$N ${M[$i]} ${P[$i]} -> HTTP $code" ;;
    *)
      snippet=$(head -c 300 "$out" | tr '\r\n\t' '   ')
      snippet=${snippet//"$TOKEN"/<owner token>}
      left=""
      [ $((i + 1)) -lt "$N" ] && left="; calls $((i + 2))..$N not made"
      stop 4 "$((i + 1))/$N ${M[$i]} ${P[$i]} -> HTTP $code, expected ${X[$i]}: $snippet$left" ;;
  esac
done
revoke || exit 5
log "DONE: all $N call(s) answered as expected; the owner token is deleted"
