#!/usr/bin/env bash
# AgentForge platform DB bootstrap + migration — scripts activation-runbook steps 3-4
# (docs/runbooks/agentforge-platform-activation.md). Both subcommands are idempotent/re-runnable and
# resolve the infra-pg PRIMARY fresh on every invocation (never assume infra-pg-1).
#
# Usage:
#   scripts/af-db.sh init      # create agentforge_platform DB + afp_admin/afp_app roles (idempotent)
#   scripts/af-db.sh migrate   # (re-)run the schema/RLS migration Job + verify alembic head + RLS
#
# Env:
#   AF_KUBE_CONTEXT   kubectl --context override (default: empty = current context; the estate
#                     convention is admin@ai — the sibling scripts/verify-sandbox-boundary.sh defaults
#                     its own KUBECTL_CONTEXT to admin@ai internally, but this script keeps an
#                     empty=current-context contract and lets callers (the justfile recipes) pin the
#                     default at the call site — see `just af-db-init` / `just af-db-migrate`)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

DB_NS="databases"
AF_NS="agentforge"
CLUSTER="infra-pg"
DB_NAME="agentforge_platform"
MIGRATE_MANIFEST="kubernetes/apps/apps/agentforge/db-migrate.yaml"
DEPLOY_FILE="kubernetes/apps/apps/agentforge/deployment.yaml"

K=(kubectl)
if [ -n "${AF_KUBE_CONTEXT:-}" ]; then
  K=(kubectl --context "$AF_KUBE_CONTEXT")
fi

usage() {
  cat <<EOF
Usage: $(basename "$0") <init|migrate>

  init     Resolve the infra-pg primary, pipe bootstrap.sql into 'psql -U postgres' inside the primary
           pod (idempotent CREATE ROLE/DATABASE \\gexec), then verify (read-only): ${DB_NAME} exists,
           afp_admin has BYPASSRLS, afp_app has NOBYPASSRLS. bootstrap.sql is extracted from the repo
           copy of $MIGRATE_MANIFEST (source of truth; that manifest is deliberately un-kustomized, so
           Flux never applies it), falling back to the live agentforge-db-bootstrap ConfigMap only if
           the repo extraction fails (e.g. a hand-applied variant an operator wants honored).

  migrate  Verify db-migrate.yaml and $DEPLOY_FILE pin the SAME agentforge-platform image digest (FAIL
           if not — the manifests' own header comments require lockstep), then delete + re-apply
           job/agentforge-db-migrate, wait up to 300s for completion, then print alembic_version
           (informational only — see AF_EXPECTED_ALEMBIC_HEAD below) and verify
           pg_class.relforcerowsecurity on every RLS table. On failure, dumps the last 40 lines of the
           Job log and exits non-zero.

Env:
  AF_KUBE_CONTEXT           kubectl --context override (default: empty = current context; see the Env
                            comment at the top of this file for the admin@ai convention)
  AF_EXPECTED_ALEMBIC_HEAD  optional, 'migrate' only: if set, assert the post-migration
                            alembic_version equals this value and FAIL on mismatch (instead of only
                            printing it informationally, which proves the Job ran but NOT which
                            revision it landed on).
EOF
}

die() {
  echo "FAIL: $*" >&2
  exit 1
}

resolve_primary() {
  local primary
  primary="$("${K[@]}" -n "$DB_NS" get cluster "$CLUSTER" -o jsonpath='{.status.currentPrimary}')"
  [ -n "$primary" ] || die "could not resolve $CLUSTER currentPrimary in ns $DB_NS"
  echo "$primary"
}

psql_primary() {  # <primary-pod> [psql-args...] — statement/query goes on stdin from the caller
  local primary="$1"
  shift
  "${K[@]}" -n "$DB_NS" exec -i "$primary" -- psql "$@"
}

bootstrap_sql() {
  # Repo-first (source of truth): extract bootstrap.sql straight out of the repo copy of
  # $MIGRATE_MANIFEST — works with zero cluster state and can never drift from what `migrate` applies.
  # $MIGRATE_MANIFEST is deliberately excluded from kustomization.yaml (an operator-run one-shot — see
  # its header), so Flux never creates the ConfigMap; that live ConfigMap is used only as a FALLBACK,
  # in case an operator hand-`kubectl apply -f`'d a variant they want honored over the repo copy.
  #
  # Extraction is stdlib-only (no PyYAML — mirrors scripts/check-inline-hashes.py's literal-block
  # technique on the sibling feat/iac-drift-bundle branch): locate the `bootstrap.sql: |` marker line
  # by regex, collect the following more-indented lines as the raw literal block, then dedent by the
  # block's own content indentation (YAML `|` clip-chomping semantics) to recover the exact string
  # value the ConfigMap embeds.
  local repo_sql
  repo_sql="$(python -c '
import re, sys

path = sys.argv[1]
text = open(path, encoding="utf-8").read()
marker_re = re.compile(r"^[ ]*bootstrap\.sql:\s*\|[-+]?\s*$")
lines = text.splitlines(keepends=True)
for i, line in enumerate(lines):
    if not marker_re.match(line):
        continue
    marker_indent = len(line) - len(line.lstrip(" "))
    raw = []
    content_indent = None
    for l in lines[i + 1:]:
        if l.strip() == "":
            raw.append(l)
            continue
        indent = len(l) - len(l.lstrip(" "))
        if indent <= marker_indent:
            break
        if content_indent is None:
            content_indent = indent
        raw.append(l)
    while raw and raw[-1].strip() == "":
        raw.pop()
    if content_indent is None:
        content_indent = marker_indent + 2
    dedented = "".join("\n" if l.strip() == "" else l[content_indent:] for l in raw)
    sys.stdout.write(dedented)
    sys.exit(0)
sys.stderr.write("bootstrap.sql literal block not found in " + path + "\n")
sys.exit(1)
' "$MIGRATE_MANIFEST" 2>/dev/null || true)"
  if [ -n "$repo_sql" ]; then
    printf '%s' "$repo_sql"
    return 0
  fi
  echo "-- could not extract bootstrap.sql from the repo copy of $MIGRATE_MANIFEST -- falling back to" >&2
  echo "   the live ConfigMap agentforge-db-bootstrap in ns $AF_NS --" >&2
  local cm_sql
  cm_sql="$("${K[@]}" -n "$AF_NS" get cm agentforge-db-bootstrap -o jsonpath='{.data.bootstrap\.sql}' 2>/dev/null || true)"
  if [ -n "$cm_sql" ]; then
    printf '%s' "$cm_sql"
    return 0
  fi
  die "could not get bootstrap.sql from the repo file OR the live ConfigMap. Apply it by hand first: kubectl -n $AF_NS apply -f $MIGRATE_MANIFEST (creates the CM; also applies the migrate Job, which is idempotent/harmless to re-run)"
}

cmd_init() {
  echo "== af-db init =="
  local primary
  primary="$(resolve_primary)"
  echo "primary=$primary"

  echo "-- applying bootstrap.sql (idempotent \\gexec; repo-file source of truth, live-ConfigMap fallback) --"
  bootstrap_sql | psql_primary "$primary" -U postgres -v ON_ERROR_STOP=1

  echo "-- verifying --"
  local dbrow
  dbrow="$("${K[@]}" -n "$DB_NS" exec "$primary" -- psql -U postgres -tAc \
    "select datname, pg_catalog.pg_get_userbyid(datdba) from pg_database where datname='${DB_NAME}'")"
  case "$dbrow" in
    *"${DB_NAME}"*"afp_admin"*) echo "PASS: ${DB_NAME} exists, owned by afp_admin" ;;
    *) die "${DB_NAME} not found or not owned by afp_admin (got: '${dbrow:-<empty>}')" ;;
  esac

  local admin_bypass app_bypass
  admin_bypass="$("${K[@]}" -n "$DB_NS" exec "$primary" -- psql -U postgres -tAc \
    "select rolbypassrls from pg_roles where rolname='afp_admin'" | tr -d '[:space:]')"
  [ "$admin_bypass" = "t" ] || die "afp_admin rolbypassrls='$admin_bypass' (want t / BYPASSRLS)"
  echo "PASS: afp_admin has BYPASSRLS"

  app_bypass="$("${K[@]}" -n "$DB_NS" exec "$primary" -- psql -U postgres -tAc \
    "select rolbypassrls from pg_roles where rolname='afp_app'" | tr -d '[:space:]')"
  [ "$app_bypass" = "f" ] || die "afp_app rolbypassrls='$app_bypass' (want f / NOBYPASSRLS)"
  echo "PASS: afp_app has NOBYPASSRLS"

  echo "== af-db init: OK =="
}

cmd_migrate() {
  echo "== af-db migrate =="

  echo "-- verifying image digest lockstep between $MIGRATE_MANIFEST and $DEPLOY_FILE --"
  # Anchored to the actual YAML key (not just anywhere the string "image:" appears, e.g. in a
  # comment), matching the same regex af-cp-smoke.sh/verify-image-digest.sh use against $DEPLOY_FILE.
  local deploy_digest migrate_digest
  deploy_digest="$(grep -m1 -E '^[[:space:]]*image:[[:space:]]*registry\.chifor\.me/agentforge/agentforge-platform@sha256:[0-9a-f]{64}' "$DEPLOY_FILE" | grep -oE 'sha256:[0-9a-f]{64}' || true)"
  migrate_digest="$(grep -m1 -E '^[[:space:]]*image:[[:space:]]*registry\.chifor\.me/agentforge/agentforge-platform@sha256:[0-9a-f]{64}' "$MIGRATE_MANIFEST" | grep -oE 'sha256:[0-9a-f]{64}' || true)"
  [ -n "$deploy_digest" ] || die "could not find the pinned agentforge-platform image digest in $DEPLOY_FILE"
  [ -n "$migrate_digest" ] || die "could not find the pinned agentforge-platform image digest in $MIGRATE_MANIFEST"
  if [ "$deploy_digest" != "$migrate_digest" ]; then
    die "image digest lockstep broken: $DEPLOY_FILE pins $deploy_digest but $MIGRATE_MANIFEST pins $migrate_digest -- both manifests' own header comments require them to stay pinned to the SAME digest; re-pin both together before migrating"
  fi
  echo "PASS: $MIGRATE_MANIFEST and $DEPLOY_FILE pin the same digest ($deploy_digest)"

  echo "-- (re-)running job/agentforge-db-migrate --"
  "${K[@]}" -n "$AF_NS" delete job agentforge-db-migrate --ignore-not-found
  "${K[@]}" apply -f "$MIGRATE_MANIFEST"

  # NB: `kubectl wait --for=condition=complete` only returns early on condition=complete=True. If the
  # Job instead reaches condition=Failed=True (e.g. backoffLimit exhausted on a bad migration), kubectl
  # does NOT notice and return early -- it blocks for the FULL --timeout=300s before giving up. So a
  # fast, deterministic failure still takes ~5 minutes to report here; that's expected kubectl
  # behavior; the log dump below is exact and immediate regardless.
  if ! "${K[@]}" -n "$AF_NS" wait --for=condition=complete --timeout=300s job/agentforge-db-migrate; then
    echo "FAIL: job/agentforge-db-migrate did not complete; last 40 log lines:" >&2
    "${K[@]}" -n "$AF_NS" logs job/agentforge-db-migrate --tail=40 >&2 || true
    exit 1
  fi
  echo "PASS: job/agentforge-db-migrate complete"

  local primary
  primary="$(resolve_primary)"

  local version
  version="$("${K[@]}" -n "$DB_NS" exec "$primary" -- psql -U postgres -d "$DB_NAME" -tAc \
    "select version_num from alembic_version" | tr -d '[:space:]')"
  [ -n "$version" ] || die "alembic_version is empty/unreadable"
  echo "alembic_version (informational; not independently verified unless AF_EXPECTED_ALEMBIC_HEAD is set): $version"
  if [ -n "${AF_EXPECTED_ALEMBIC_HEAD:-}" ]; then
    if [ "$version" != "$AF_EXPECTED_ALEMBIC_HEAD" ]; then
      die "alembic_version '$version' != AF_EXPECTED_ALEMBIC_HEAD '$AF_EXPECTED_ALEMBIC_HEAD'"
    fi
    echo "PASS: alembic_version matches AF_EXPECTED_ALEMBIC_HEAD"
  fi

  echo "-- RLS-forced tables (relname | relrowsecurity | relforcerowsecurity) --"
  local rls_rows
  rls_rows="$("${K[@]}" -n "$DB_NS" exec "$primary" -- psql -U postgres -d "$DB_NAME" -tAc \
    "select relname, relrowsecurity, relforcerowsecurity from pg_class where relrowsecurity and relnamespace='public'::regnamespace order by 1")"
  [ -n "$rls_rows" ] || die "no RLS-enabled tables found in $DB_NAME"
  printf '%s\n' "$rls_rows" | sed 's/^/  /'
  if printf '%s\n' "$rls_rows" | grep -qE '\|f *$'; then
    die "at least one RLS table has relforcerowsecurity=f (not forced)"
  fi
  echo "PASS: every RLS table has relforcerowsecurity=t"

  if [ -n "${AF_EXPECTED_ALEMBIC_HEAD:-}" ]; then
    echo "== af-db migrate: OK (alembic_version=$version, verified == AF_EXPECTED_ALEMBIC_HEAD) =="
  else
    echo "== af-db migrate: done (alembic_version=$version; UNVERIFIED against any expected revision -- set AF_EXPECTED_ALEMBIC_HEAD to assert it) =="
  fi
}

if [ $# -eq 0 ]; then
  usage >&2
  exit 2
fi

case "$1" in
  -h|--help)
    usage
    exit 0
    ;;
  init)
    shift
    if [ $# -ne 0 ]; then
      echo "error: 'init' takes no arguments (got: '$1')" >&2
      usage >&2
      exit 2
    fi
    cmd_init
    ;;
  migrate)
    shift
    if [ $# -ne 0 ]; then
      echo "error: 'migrate' takes no arguments (got: '$1')" >&2
      usage >&2
      exit 2
    fi
    cmd_migrate
    ;;
  *)
    echo "error: unknown subcommand '$1'" >&2
    usage >&2
    exit 2
    ;;
esac
