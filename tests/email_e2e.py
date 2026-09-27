"""Read a captured outbox link ONLY from the dedicated browser-test database."""

import asyncio
import json
import os
import sys

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.factors import cipher
from app.auth.models import MailOutbox
from app.config import Settings


async def main():
    settings = Settings.from_environment()
    url = settings.database_url.get_secret_value()
    if not make_url(url).database.endswith("_test") or url != os.environ["AUTH_TEST_DATABASE_URL"]:
        raise RuntimeError("Only a dedicated test database may be inspected")
    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine)() as db:
            rows = await db.scalars(select(MailOutbox).order_by(MailOutbox.created.desc()))
            for row in rows:
                payload = json.loads(cipher(settings).decrypt(row.payload.encode()))
                if (
                    payload["email"] == sys.argv[1]
                    and "#identity=" + sys.argv[2] in payload["body"]
                ):
                    print(payload["body"].split("\n\n")[1])
                    return
        raise RuntimeError("No captured link")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
