#!/usr/bin/env bash
set -Eeuo pipefail

BACKEND_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$BACKEND_DIR/.." && pwd)"
LOCAL_PGDATA="${PGDATA:-$BACKEND_DIR/.postgres_data}"
PG_BIN_DIR=""

if command -v initdb >/dev/null 2>&1 && command -v pg_ctl >/dev/null 2>&1; then
  PG_BIN_DIR="$(dirname -- "$(command -v initdb)")"
elif [[ -x /usr/lib/postgresql/16/bin/initdb && -x /usr/lib/postgresql/16/bin/pg_ctl ]]; then
  PG_BIN_DIR=/usr/lib/postgresql/16/bin
fi

if [[ -f "$BACKEND_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$BACKEND_DIR/.env"
  set +a
fi

if [[ "${APP_ENV:-development}" != development ]]; then
  printf 'This launcher is for local development only. Use deploy/compose.production.yml.\n' >&2
  exit 1
fi
if [[ -n "${DATABASE_URL:-}" && "${1:-}" != --container ]]; then
  "$BACKEND_DIR/venv/bin/python" -c 'import sys; sys.path.insert(0, sys.argv[1]); from app.database import check_database_environment; import os; check_database_environment(os.environ["DATABASE_URL"], "development")' "$BACKEND_DIR"
fi

POSTGRES_DB="${POSTGRES_DB:-retail_pos}"
POSTGRES_USER="${POSTGRES_USER:-retail_dev}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-local_development_only}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
API_PORT="${API_PORT:-8000}"
AUTH_SECRET="${AUTH_SECRET:-}"
DATABASE_URL_WAS_SET="${DATABASE_URL+x}"

if [[ "${1:-}" == "--container" ]]; then
  if [[ ! -f "$BACKEND_DIR/.env" ]]; then
    printf 'Missing %s/.env. Copy backend/.env.example to backend/.env and set AUTH_SECRET first.\n' "$BACKEND_DIR" >&2
    exit 1
  fi
  if [[ -z "$AUTH_SECRET" || ${#AUTH_SECRET} -lt 32 ]]; then
    printf 'AUTH_SECRET must contain at least 32 characters in .env.\n' >&2
    exit 1
  fi

  cd "$PROJECT_ROOT"
  compose_services=(db api)
  email_provider="${EMAIL_PROVIDER:-}"
  if [[ -z "$email_provider" ]]; then
    if [[ -n "${BREVO_API_KEY:-}" ]]; then
      email_provider=brevo
    else
      email_provider=smtp
    fi
  fi
  if [[ -n "${IDENTITY_ENCRYPTION_KEY:-}" ]] &&
    { [[ "$email_provider" == brevo && -n "${BREVO_API_KEY:-}" ]] ||
      [[ "$email_provider" == smtp && -n "${SMTP_HOST:-}" ]]; }; then
    compose_services+=(mail-worker)
  fi
  docker compose --env-file "$BACKEND_DIR/.env" -f "$PROJECT_ROOT/docker-compose.yml" \
    up --build -d "${compose_services[@]}"
  api_endpoint="$(docker compose --env-file "$BACKEND_DIR/.env" -f "$PROJECT_ROOT/docker-compose.yml" port api 8000)"
  api_port="${api_endpoint##*:}"
  if [[ ! "$api_port" =~ ^[0-9]+$ ]]; then
    printf 'Could not determine the published API port: %s\n' "$api_endpoint" >&2
    exit 1
  fi
  printf '%s\n' "$api_port" > "$BACKEND_DIR/.api-port"
  printf 'Backend is running at http://127.0.0.1:%s\n' "$api_port"
  exit 0
fi

if [[ "${1:-}" != "" ]]; then
  printf 'Usage: %s [--container]\n' "$(basename "$0")" >&2
  exit 2
fi

if [[ -z "$AUTH_SECRET" || ${#AUTH_SECRET} -lt 32 ]]; then
  printf 'Set AUTH_SECRET to a value with at least 32 characters (in backend/.env or the environment).\n' >&2
  exit 1
fi

if ! command -v pg_isready >/dev/null 2>&1; then
  printf 'PostgreSQL client tools are required for local mode (pg_isready was not found).\n' >&2
  exit 1
fi

if [[ -z "$DATABASE_URL_WAS_SET" ]] && ! pg_isready -q -h 127.0.0.1 -p "$POSTGRES_PORT"; then
  if [[ -z "$PG_BIN_DIR" ]]; then
    printf 'No PostgreSQL server is listening on 127.0.0.1:5432, and initdb/pg_ctl are unavailable.\n' >&2
    exit 1
  fi

  if [[ ! -f "$LOCAL_PGDATA/PG_VERSION" ]]; then
    mkdir -p "$LOCAL_PGDATA"
    password_file="$(mktemp)"
    trap 'rm -f "$password_file"' EXIT
    printf '%s\n' "$POSTGRES_PASSWORD" >"$password_file"
    "$PG_BIN_DIR/initdb" -D "$LOCAL_PGDATA" --encoding=UTF8 --username="$POSTGRES_USER" \
      --pwfile="$password_file" --auth-host=scram-sha-256 --auth-local=trust
    rm -f "$password_file"
    trap - EXIT
  fi

  "$PG_BIN_DIR/pg_ctl" -D "$LOCAL_PGDATA" -l "$LOCAL_PGDATA/server.log" \
    -o "-h 127.0.0.1 -p $POSTGRES_PORT -k /tmp" start
  for _ in {1..30}; do
    pg_isready -q -h 127.0.0.1 -p "$POSTGRES_PORT" && break
    sleep 1
  done
fi

export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PORT}/${POSTGRES_DB}}"
export APP_ENV="${APP_ENV:-development}"
export PUBLIC_ORIGIN="${PUBLIC_ORIGIN:-http://localhost:5173}"

if [[ -z "$DATABASE_URL_WAS_SET" ]] && ! command -v psql >/dev/null 2>&1; then
  printf 'PostgreSQL client tools are required for local mode (psql was not found).\n' >&2
  exit 1
fi
if [[ -z "$DATABASE_URL_WAS_SET" ]] && ! PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres \
  -tAc 'SELECT 1' >/dev/null 2>&1; then
  printf 'Could not connect to local PostgreSQL as %s. Check POSTGRES_USER and POSTGRES_PASSWORD.\n' "$POSTGRES_USER" >&2
  exit 1
fi
if [[ -z "$DATABASE_URL_WAS_SET" ]] && ! PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres \
  -tAc "SELECT 1 FROM pg_database WHERE datname = '$POSTGRES_DB'" | rg -q '^1$'; then
  PGPASSWORD="$POSTGRES_PASSWORD" createdb -h 127.0.0.1 -p "$POSTGRES_PORT" -U "$POSTGRES_USER" "$POSTGRES_DB"
fi

if [[ ! -x "$BACKEND_DIR/venv/bin/python" ]]; then
  printf 'Missing backend virtual environment. Create it with: python3 -m venv backend/venv\n' >&2
  exit 1
fi

cd "$BACKEND_DIR"
venv/bin/alembic upgrade head
printf '%s\n' "$API_PORT" > "$BACKEND_DIR/.api-port"
exec venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" --reload --no-proxy-headers
