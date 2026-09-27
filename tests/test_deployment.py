import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import check_database_environment, database_url, require_test_database
from app.main import create_app


def test_postgres_url_formats_and_tls():
    assert database_url("postgres://user:pass@db/ive?sslmode=require") == (
        "postgresql+asyncpg://user:pass@db/ive?ssl=require"
    )
    with pytest.raises(ValueError, match="PostgreSQL URL"):
        database_url("sqlite:///database")


def test_development_and_test_database_guards(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    with pytest.raises(ValueError, match="remote"):
        check_database_environment("postgresql://u:p@real-rds.example.com/live", "development")
    with pytest.raises(ValueError, match="production"):
        check_database_environment("postgresql://u:p@db/ive_production", "development")
    for url in ("postgresql://u:p@db/live", "postgresql://u:p@remote.example/live_test"):
        with pytest.raises(RuntimeError):
            require_test_database(url)
    assert require_test_database("postgresql://u:p@localhost/ive_test")
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError):
        require_test_database("postgresql://u:p@localhost/ive_test")


def test_cross_subdomain_cors_and_csrf():
    origin = "https://app.example.com"
    settings = Settings(
        database_url="postgresql://test:test@localhost/unused_test",
        auth_secret="test-only-secret-that-is-at-least-32-characters",
        public_origin=origin,
        run_deletion_worker=False,
    )
    with TestClient(create_app(settings), base_url="https://api.example.com") as client:
        preflight = client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type,X-POS-CSRF",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == origin
        assert preflight.headers["access-control-allow-credentials"] == "true"
        response = client.get("/api/v1/health", headers={"Origin": origin})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        rejected = client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert rejected.status_code == 400
        assert "access-control-allow-origin" not in rejected.headers
        assert (
            client.post(
                "/api/v1/auth/login",
                json={},
                headers={
                    "Origin": origin,
                },
            ).status_code
            == 403
        )
        # Successful CSRF reaches field validation without opening a DB connection.
        valid = client.post(
            "/api/v1/auth/login",
            json={},
            headers={
                "Origin": origin,
                "X-POS-CSRF": "1",
                "Sec-Fetch-Site": "same-site",
            },
        )
        assert valid.status_code == 422
        assert valid.headers["access-control-allow-origin"] == origin
        cross_site = client.post(
            "/api/v1/auth/login",
            json={},
            headers={
                "Origin": origin,
                "X-POS-CSRF": "1",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert cross_site.status_code == 403


@pytest.mark.parametrize("command", ["downgrade", "stamp"])
def test_production_destructive_migration_commands_fail_before_connect(monkeypatch, command):
    import runpy
    from types import SimpleNamespace

    from alembic import context
    from sqlalchemy import ext

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/disposable_test")
    monkeypatch.setattr(
        context,
        "config",
        SimpleNamespace(
            cmd_opts=SimpleNamespace(cmd=(SimpleNamespace(__name__=command),)),
        ),
        raising=False,
    )

    def must_not_connect(*args, **kwargs):
        raise AssertionError("The migration guard must run before creating an engine")

    monkeypatch.setattr(ext.asyncio, "create_async_engine", must_not_connect)
    with pytest.raises(RuntimeError, match="Production downgrade/stamp is disabled"):
        runpy.run_path("migrations/env.py")


def test_example_auth_secret_is_not_accepted():
    with pytest.raises(ValueError, match="Replace the example AUTH_SECRET"):
        Settings(
            database_url="postgresql://test:test@localhost/test",
            auth_secret="REPLACE_WITH_AT_LEAST_48_RANDOM_CHARACTERS",
        )
