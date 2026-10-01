import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.auth.models import Base
from app.database import database_url_from_environment
from app.operations import models as operations_models  # noqa: F401
from app.stores import models  # noqa: F401

url = database_url_from_environment()
# Downgrades in the existing history drop tables/columns. Production rollback uses
# a reviewed forward migration or a restored backup, never an implicit downgrade.
if os.environ.get("APP_ENV", "production") == "production":
    command = getattr(context.config.cmd_opts, "cmd", ())
    if command and getattr(command[0], "__name__", "") in {"downgrade", "stamp"}:
        raise RuntimeError("Production downgrade/stamp is disabled; use the recovery runbook")


def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
