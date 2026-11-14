#!/bin/sh
# Sourced by the two offline operator Jobs, before touching retained storage.
# The reviewed source pin ships beside this script in the same script ConfigMap.
pin_file="$(dirname "$0")/relay-source.env"

pinned_source_commit() {
  [ -f "$pin_file" ] || return 78
  # Only comments, blank lines and exactly one assignment of a full SHA (or empty).
  if grep -Evq '^(#.*|[[:space:]]*|RELAY_BUDGET_SOURCE_COMMIT=([0-9a-f]{40})?)$' "$pin_file"; then
    return 78
  fi
  [ "$(grep -c '^RELAY_BUDGET_SOURCE_COMMIT=' "$pin_file")" = 1 ] || return 78
  sed -n 's/^RELAY_BUDGET_SOURCE_COMMIT=//p' "$pin_file"
}

verify_schema() {
  printf '%s\n' "${RELAY_BUDGET_SCHEMA_MANIFEST_SHA256:-}" | grep -Eq '^[0-9a-f]{64}$' || return 78
  [ -s /schema/SHA256SUMS ] || return 78
  actual=$(sha256sum /schema/SHA256SUMS) || return 78
  [ "${actual%% *}" = "$RELAY_BUDGET_SCHEMA_MANIFEST_SHA256" ] || return 78
  [ "$(wc -c < /schema/source-commit | tr -d ' ')" = 41 ] || return 78
  grep -Eq '^[0-9a-f]{40}$' /schema/source-commit || return 78
  # The bundle must come from the reviewed pin, not merely be self-consistent.
  pin=$(pinned_source_commit) || return 78
  printf '%s\n' "$pin" | grep -Eq '^[0-9a-f]{40}$' || return 78
  [ "$(cat /schema/source-commit)" = "$pin" ] || return 78
  # Generate the exact allowed inventory ourselves. Never execute paths supplied
  # by a manifest, and reject missing, extra, reordered or changed entries.
  inventory=$(mktemp /tmp/relay-budget-schema.XXXXXX) || return 78
  (cd /schema && sha256sum source-commit 001-authority.sql 002-preflight.sql \
    003-scoped-requests.sql 004-preflight-errors.sql 005-provenance-purpose.sql) > "$inventory" || {
    rm -f "$inventory"; return 78;
  }
  cmp -s "$inventory" /schema/SHA256SUMS || { rm -f "$inventory"; return 78; }
  rm -f "$inventory"
}

record_schema() {
  # File marker 5 describes a completely applied 001-005 bundle, not a recovery
  # witness. The server wrapper refuses every other marker, including the retired 4.
  cp /schema/SHA256SUMS "$PGDATA/relay-budget-schema-SHA256SUMS"
  cp /schema/source-commit "$PGDATA/relay-budget-schema-source-commit"
  sync
  printf '5\n' > "$PGDATA/relay-budget-schema-version.new"
  mv "$PGDATA/relay-budget-schema-version.new" "$PGDATA/relay-budget-schema-version"
  sync
}
