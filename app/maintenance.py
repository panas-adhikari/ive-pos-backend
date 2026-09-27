"""Separate scheduled-deletion process for hosted deployments."""

import asyncio
import logging
import signal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.platform.deletion import deletion_worker


async def main():
    settings = Settings.from_environment()
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 60},
    )
    task = asyncio.create_task(deletion_worker(async_sessionmaker(engine, expire_on_commit=False)))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        await engine.dispose()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
