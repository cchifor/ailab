#!/bin/sh
# Sourced by the two offline operator Jobs, before touching retained storage.
verify_schema() {
  printf '%s\n' "${RELAY_BUDGET_SCHEMA_MANIFEST_SHA256:-}" | grep -Eq '^[0-9a-f]{64}$' || return 78
  [ -s /schema/SHA256SUMS ] || return 78
  actual=$(sha256sum /schema/SHA256SUMS) || return 78
  [ "${actual%% *}" = "$RELAY_BUDGET_SCHEMA_MANIFEST_SHA256" ] || return 78
  [ "$(wc -c < /schema/source-commit | tr -d ' ')" = 41 ] || return 78
  grep -Eq '^[0-9a-f]{40}$' /schema/source-commit || return 78
  # Generate the exact allowed inventory ourselves. Never execute paths supplied
  # by a manifest, and reject missing, extra, reordered or changed entries.
  inventory=$(mktemp /tmp/relay-budget-schema.XXXXXX) || return 78
  (cd /schema && sha256sum source-commit 001-authority.sql 002-preflight.sql \
    003-scoped-requests.sql 004-preflight-errors.sql) > "$inventory" || {
    rm -f "$inventory"; return 78;
  }
  cmp -s "$inventory" /schema/SHA256SUMS || { rm -f "$inventory"; return 78; }
  rm -f "$inventory"
}

record_schema() {
  # File marker 4 describes a completely applied bundle, not a recovery witness.
  cp /schema/SHA256SUMS "$PGDATA/relay-budget-schema-SHA256SUMS"
  cp /schema/source-commit "$PGDATA/relay-budget-schema-source-commit"
  sync
  printf '4\n' > "$PGDATA/relay-budget-schema-version.new"
  mv "$PGDATA/relay-budget-schema-version.new" "$PGDATA/relay-budget-schema-version"
  sync
}
