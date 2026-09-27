"""PostgreSQL configuration shared by runtime, migrations and operator tools."""

from sqlalchemy.engine import make_url


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


def check_database_environment(value: str, environment: str) -> None:
    url = make_url(database_url(value))
    if environment == "development":
        if url.host not in {"localhost", "127.0.0.1", "::1", "db", "postgres"}:
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
