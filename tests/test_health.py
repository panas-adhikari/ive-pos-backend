from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_liveness_and_database_readiness_are_separate():
    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost/test",
        auth_secret="test-only-secret-that-is-at-least-32-characters",
    )
    with TestClient(create_app(settings)) as client:

        async def unavailable():
            raise ConnectionError("private database connection details")

        client.app.state.check_database = unavailable
        assert client.get("/api/v1/health").json() == {"status": "ok"}
        response = client.get("/api/v1/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
        assert "private" not in response.text

        async def available():
            pass

        client.app.state.check_database = available
        response = client.get("/api/v1/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}
