#!/usr/bin/env bash
set -Eeuo pipefail
: "${PGDATABASE:?Set PGDATABASE to a NEW empty destination}"
: "${RESTORE_TARGET_CONFIRM:?Set RESTORE_TARGET_CONFIRM to the destination database name}"
[[ "$RESTORE_TARGET_CONFIRM" == "$PGDATABASE" ]] || { echo 'Restore target confirmation mismatch' >&2; exit 1; }
[[ $# == 1 && -f "$1" ]] || { echo 'Usage: restore.sh /path/backup.dump' >&2; exit 2; }
# Refuse an occupied destination; never --clean/drop an existing production schema.
objects="$(psql -X -v ON_ERROR_STOP=1 -Atc "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f')")"
[[ "$objects" == 0 ]] || { echo 'Destination is not empty; refusing restore' >&2; exit 1; }
pg_restore --exit-on-error --single-transaction --no-owner --no-privileges \
  --dbname="$PGDATABASE" "$1"
echo 'Restore finished. Verify schema version, counts and financial totals before reopening writes.'
