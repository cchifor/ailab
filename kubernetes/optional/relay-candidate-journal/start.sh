#!/bin/sh
set -eu
umask 077
# Bypass docker-entrypoint.sh entirely: a new/lost/empty volume MUST NOT initialize.
if [ ! -f "$PGDATA/PG_VERSION" ] || [ "$(cat "$PGDATA/PG_VERSION")" != 17 ] ||
   [ ! -f "$PGDATA/relay-candidate-journal-schema-version" ] ||
   [ "$(cat "$PGDATA/relay-candidate-journal-schema-version")" != 2 ] ||
   [ -e "$PGDATA/standby.signal" ] || [ -e "$PGDATA/recovery.signal" ]; then
  echo 'Independent candidate journal database requires operator initialization or recovery review' >&2
  exit 78
fi
# Kubelet fsGroup repair must not silently make PGDATA group-writable.
# Refuse and preserve retained state; never chmod/chown it automatically.
owner=$(stat -c '%u' "$PGDATA")
mode=$(stat -c '%a' "$PGDATA")
case "$mode" in 700|750|2700|2750) mode_allowed=true;; *) mode_allowed=false;; esac
if [ "$owner" != 70 ] || [ "$mode_allowed" != true ]; then
  echo 'Independent candidate journal PGDATA requires UID 70 and mode 0700/0750 (optional inherited setgid); inspect fsGroup/root ownership without changing retained state' >&2
  exit 78
fi
exec postgres -D "$PGDATA" \
  -c listen_addresses='*' \
  -c unix_socket_directories=/var/run/postgresql \
  -c hba_file=/etc/relay-candidate-journal/pg_hba.conf \
  -c ssl=on \
  -c ssl_min_protocol_version=TLSv1.2 \
  -c ssl_cert_file=/run/tls/tls.crt \
  -c ssl_key_file=/run/tls/tls.key \
  -c password_encryption=scram-sha-256 \
  -c fsync=on -c full_page_writes=on -c synchronous_commit=on \
  -c max_wal_senders=0 -c max_replication_slots=0 -c archive_mode=off \
  -c max_connections=40 -c shared_buffers=256MB \
  -c log_statement=none -c log_min_error_statement=panic
