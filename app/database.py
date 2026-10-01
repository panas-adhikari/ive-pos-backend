"""PostgreSQL configuration shared by runtime, migrations and operator tools."""

import os
from collections.abc import Mapping

from sqlalchemy.engine import make_url


def database_provider(value: str) -> str:
    if value not in {"postgres", "supabase"}:
        raise ValueError("DB_PROVIDER must be postgres or supabase")
    return value


def database_url_from_environment(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    provider = database_provider(env.get("DB_PROVIDER", "postgres"))
    key = "SUPABASE_DATABASE_URL" if provider == "supabase" else "DATABASE_URL"
    if not env.get(key):
        raise ValueError(f"{key} is required for DB_PROVIDER={provider}")
    url = database_url(env[key])
    check_database_environment(url, env.get("APP_ENV", "production"), provider)
    return url


def database_url(value: str) -> str:
    try:
        url = make_url(value)
        if url.drivername not in {"postgres", "postgresql", "postgresql+asyncpg"}:
            raise ValueError
        if not url.database:
            raise ValueError
        query = dict(url.query)
        if "sslmode" in query:
            query["ssl"] = query.pop("sslmode")
        return url.set(drivername="postgresql+asyncpg", query=query).render_as_string(
            hide_password=False
        )
    except Exception:
        raise ValueError("DATABASE_URL must be a PostgreSQL URL with a database name") from None


def check_database_environment(value: str, environment: str, provider: str = "postgres") -> None:
    database_provider(provider)
    url = make_url(database_url(value))
    if provider == "supabase":
        # Both direct connections and the session pooler work with asyncpg.
        # Transaction pooling needs driver changes; do not silently select it.
        if url.port == 6543:
            raise ValueError("Supabase requires a direct connection or session pooler on port 5432")
        if url.query.get("ssl") not in {"require", "verify-ca", "verify-full"}:
            raise ValueError("Supabase requires TLS: add sslmode=require to the database URI")
    if environment == "development":
        if provider == "postgres" and url.host not in {
            "localhost",
            "127.0.0.1",
            "::1",
            "db",
            "postgres",
        }:
            raise ValueError("Development must use local PostgreSQL, never a remote database")
        if url.database.endswith(("_production", "_prod")):
            raise ValueError("Development cannot use a production database")


def require_test_database(value: str) -> str:
    import os

    url = make_url(database_url(value))
    if (
        os.environ.get("APP_ENV") == "production"
        or not url.database.endswith("_test")
        or url.host not in {"localhost", "127.0.0.1", "::1", "db", "postgres"}
    ):
        raise RuntimeError("Tests require a local disposable _test database outside production")
    return database_url(value)
