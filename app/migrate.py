"""Check database revisions and apply pending forward migrations."""

import asyncio
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.database import database_url_from_environment


async def current_revisions(url: str) -> tuple[str, ...]:
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda db: MigrationContext.configure(db).get_current_heads()
            )
    finally:
        await engine.dispose()


def migrate() -> str:
    url = database_url_from_environment()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    head = scripts.get_current_head()
    if not head:
        raise RuntimeError("No migration head found")
    current = asyncio.run(current_revisions(url))
    print("Database revision:", ", ".join(current) or "base", flush=True)
    print("Repository head:", head, flush=True)
    # Refuse unknown or divergent history instead of stamping over it.
    pending = list(reversed(list(scripts.iterate_revisions(head, current))))
    if not pending:
        if current != (head,):
            raise RuntimeError("Database history does not match the repository head")
        result = f"Database already at {head}; no migrations needed."
    else:
        for revision in pending:
            print("Applying:", revision.revision, revision.doc, flush=True)
        command.upgrade(config, "head")
        if asyncio.run(current_revisions(url)) != (head,):
            raise RuntimeError("Database did not reach the expected migration head")
        result = f"Applied {len(pending)} migration(s); verified database at {head}."
    print(result, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write(result + "\n")
    return result


def main():
    try:
        migrate()
    except Exception as error:
        # SQL exceptions can include bound data; do not expose credentials or rows in CI.
        print(f"Migration check/apply failed ({type(error).__name__}).", flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
