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
#   AF_KUBE_CONTEXT   kubectl --context override (default: empty = current context)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

DB_NS="databases"
AF_NS="agentforge"
CLUSTER="infra-pg"
DB_NAME="agentforge_platform"
MIGRATE_MANIFEST="kubernetes/apps/apps/agentforge/db-migrate.yaml"

K=(kubectl)
if [ -n "${AF_KUBE_CONTEXT:-}" ]; then
  K=(kubectl --context "$AF_KUBE_CONTEXT")
fi

usage() {
  cat <<EOF
Usage: $(basename "$0") <init|migrate>

  init     Resolve the infra-pg primary, pipe the agentforge-db-bootstrap ConfigMap's bootstrap.sql
           into 'psql -U postgres' inside the primary pod (idempotent CREATE ROLE/DATABASE \\gexec),
           then verify (read-only): ${DB_NAME} exists, afp_admin has BYPASSRLS, afp_app has
           NOBYPASSRLS.

  migrate  Delete + re-apply job/agentforge-db-migrate, wait up to 300s for completion, then verify
           alembic_version (printed) and pg_class.relforcerowsecurity on every RLS table. On failure,
           dumps the last 40 lines of the Job log and exits non-zero.

Env:
  AF_KUBE_CONTEXT   kubectl --context override (default: empty = current context)
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

cmd_init() {
  echo "== af-db init =="
  local primary
  primary="$(resolve_primary)"
  echo "primary=$primary"

  echo "-- applying agentforge-db-bootstrap ConfigMap (idempotent \\gexec) --"
  "${K[@]}" -n "$AF_NS" get cm agentforge-db-bootstrap -o jsonpath='{.data.bootstrap\.sql}' \
    | psql_primary "$primary" -U postgres -v ON_ERROR_STOP=1

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
  echo "-- (re-)running job/agentforge-db-migrate --"
  "${K[@]}" -n "$AF_NS" delete job agentforge-db-migrate --ignore-not-found
  "${K[@]}" apply -f "$MIGRATE_MANIFEST"

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
  echo "alembic_version=$version"

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

  echo "== af-db migrate: OK (alembic_version=$version) =="
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
    cmd_init "$@"
    ;;
  migrate)
    shift
    cmd_migrate "$@"
    ;;
  *)
    echo "error: unknown subcommand '$1'" >&2
    usage >&2
    exit 2
    ;;
esac
