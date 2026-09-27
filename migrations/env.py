import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth.models import Base
from app.stores import models  # noqa: F401
from app.operations import models as operations_models  # noqa: F401


def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.connect() as connection:
        await connection.run_sync(run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=os.environ["DATABASE_URL"], target_metadata=Base.metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
