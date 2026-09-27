import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.identity_routes import router as identity_router
from app.auth.invitations import router as invitations_router
from app.auth.middleware import SecurityMiddleware
from app.auth.routes import router
from app.auth.service import require_auth
from app.config import Settings
from app.employees.routes import router as employees_router
from app.operations.routes import router as operations_router
from app.platform.deletion import deletion_worker
from app.platform.routes import router as platform_router
from app.stores.routes import router as stores_router

logger = logging.getLogger(__name__)


class Health(BaseModel):
    status: Literal["ok", "ready", "unavailable"]


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings or Settings.from_environment()
        app.state.settings = config
        app.state.password_gate = asyncio.Semaphore(4)
        engine = create_async_engine(
            config.database_url.get_secret_value(),
            pool_pre_ping=True,
            pool_timeout=3,
            hide_parameters=True,
            connect_args={
                "timeout": 3,
                "command_timeout": 10,
                "server_settings": {"statement_timeout": "10000", "lock_timeout": "5000"},
            },
        )
        app.state.db = async_sessionmaker(engine, expire_on_commit=False)
        app.state.deletion_worker = asyncio.create_task(deletion_worker(app.state.db))

        async def check_database() -> None:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))

        app.state.check_database = check_database
        try:
            yield
        finally:
            app.state.deletion_worker.cancel()
            try:
                await app.state.deletion_worker
            except asyncio.CancelledError:
                pass
            await engine.dispose()

    app = FastAPI(
        title="Ive POS API",
        version="0.1.0",
        lifespan=lifespan,
        dependencies=[Depends(require_auth)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(SecurityMiddleware, settings_getter=lambda: app.state.settings)
    app.include_router(router)
    app.include_router(identity_router)
    app.include_router(invitations_router)
    app.include_router(stores_router)
    app.include_router(employees_router)
    app.include_router(operations_router)
    app.include_router(platform_router)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError):
        # FastAPI's default errors may echo raw password inputs. Never return them.
        return JSONResponse(status_code=422, content={"detail": "Invalid request fields"})

    @app.get("/api/v1/health", response_model=Health, tags=["Operations"])
    async def health() -> Health:
        """Liveness only; does not imply the database is available."""
        return Health(status="ok")

    @app.get(
        "/api/v1/ready",
        response_model=Health,
        responses={503: {"model": Health}},
        tags=["Operations"],
    )
    async def ready():
        try:
            await asyncio.wait_for(app.state.check_database(), timeout=4)
        except Exception:
            # Avoid exposing connection strings or database exceptions publicly.
            logger.warning("Database readiness check failed")
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return Health(status="ready")

    return app


app = create_app()
