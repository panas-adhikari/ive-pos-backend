#!/usr/bin/env bash
set -Eeuo pipefail
# Runs only on a NEW, empty volume. Never changes an existing cluster.
[[ "$POSTGRES_DB" == *_production ]] || { echo 'Production database must end in _production' >&2; exit 1; }
[[ "$APP_DATABASE_USER" != "$POSTGRES_USER" ]] || { echo 'Use a separate application role' >&2; exit 1; }
for credential in "$POSTGRES_PASSWORD" "$APP_DATABASE_PASSWORD"; do
  [[ "$credential" != REPLACE_* && ${#credential} -ge 16 ]] || {
    echo 'Generate independent database passwords of at least 16 characters' >&2; exit 1;
  }
done
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv app_user APP_DATABASE_USER
\getenv app_password APP_DATABASE_PASSWORD
\getenv db_name POSTGRES_DB
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'app_user', :'app_password') \gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'db_name', :'app_user') \gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
