"""Copy platform identities to a fresh Supabase database without tenant data.

Run with the backend environment exported. Preview is the default; --apply creates
the application schema and copies records. The native source is never modified.
"""

import argparse
import asyncio
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth.models import AuditEvent, Base, User
from app.database import check_database_environment, database_url
from app.operations import models as operations_models  # noqa: F401
from app.stores import models as stores_models  # noqa: F401

BACKEND = Path(__file__).resolve().parents[2]
SOURCE_QUERY = """
SELECT json_build_object(
    'revision', (SELECT version_num FROM alembic_version),
    'users', (SELECT coalesce(json_agg(u), '[]'::json) FROM users u
              WHERE platform_role IN ('super_admin', 'employee')),
    'audits', (SELECT coalesce(json_agg(a), '[]'::json) FROM auth_audit_events a
               WHERE organization_id IS NULL
                 AND user_id IN (SELECT id FROM users
                                 WHERE platform_role IN ('super_admin', 'employee'))
                 AND (target_type IS NULL OR
                      (target_type = 'users' AND target_id IN
                       (SELECT id FROM users WHERE platform_role IN ('super_admin', 'employee')))))
)
"""


def migration_config() -> Config:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "migrations"))
    return config


async def source_snapshot() -> dict:
    if os.environ.get("SOURCE_DATABASE_URL"):
        engine = create_async_engine(
            database_url(os.environ["SOURCE_DATABASE_URL"]),
            hide_parameters=True,
            connect_args={"timeout": 10},
        )
        try:
            async with engine.connect() as connection:
                return (await connection.execute(text(SOURCE_QUERY))).scalar_one()
        finally:
            await engine.dispose()
    # No published native port is needed. Keep credentials inside the db container;
    # the query result (password hashes and encrypted MFA) stays only in memory.
    env = dict(os.environ, DB_PROVIDER="postgres", COMPOSE_PROFILES="postgres")
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "docker",
            "compose",
            "--env-file",
            str(BACKEND / ".env"),
            "-f",
            str(BACKEND.parent / "docker-compose.yml"),
            "exec",
            "-T",
            "db",
            "sh",
            "-c",
            'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At',
        ],
        input=SOURCE_QUERY,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    if result.returncode:
        raise ValueError("Cannot read native database through Docker Compose")
    return json.loads(result.stdout)


def records(table, rows: list[dict]) -> list[dict]:
    result = []
    for row in rows:
        converted = {}
        for column in table.columns:
            value = row[column.name]
            if value is not None:
                if column.type.python_type is UUID:
                    value = UUID(value)
                elif column.type.python_type is datetime:
                    value = datetime.fromisoformat(value)
            converted[column.name] = value
        result.append(converted)
    return result


async def require_empty(connection) -> None:
    present = set(
        (
            await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        ).scalars()
    )
    for name in Base.metadata.tables:
        if name in present:
            if await connection.scalar(text(f'SELECT count(*) FROM public."{name}"')):
                raise ValueError(f"Destination app table {name} is not empty; refusing to merge")
    if "alembic_version" in present:
        revisions = list(
            (
                await connection.execute(text("SELECT version_num FROM public.alembic_version"))
            ).scalars()
        )
        if revisions and revisions != [
            ScriptDirectory.from_config(migration_config()).get_current_head()
        ]:
            raise ValueError("Destination schema revision differs from the application head")


async def transfer(apply: bool) -> None:
    raw = os.environ.get("SUPABASE_DATABASE_URL")
    if not raw:
        raise ValueError("SUPABASE_DATABASE_URL is required")
    target = database_url(raw)
    check_database_environment(target, "production", "supabase")
    if (
        os.environ.get("SOURCE_DATABASE_URL")
        and database_url(os.environ["SOURCE_DATABASE_URL"]) == target
    ):
        raise ValueError("Source and destination must differ")
    snapshot = await source_snapshot()
    head = ScriptDirectory.from_config(migration_config()).get_current_head()
    if snapshot["revision"] != head:
        raise ValueError("Native schema must match the application head before transferring")
    if not snapshot["users"]:
        raise ValueError("No platform identities found in the native database")
    for user in snapshot["users"]:
        for field in ("mfa_secret", "mfa_pending"):
            if user[field]:
                key = os.environ.get("IDENTITY_ENCRYPTION_KEY")
                if not key:
                    raise ValueError("Existing MFA requires the original IDENTITY_ENCRYPTION_KEY")
                Fernet(key.encode()).decrypt(user[field].encode())
    users = records(User.__table__, snapshot["users"])
    audits = records(AuditEvent.__table__, snapshot["audits"])
    # Session credentials are intentionally not transferred. Audit records remain
    # useful without their foreign key to old sessions; admins sign in afresh.
    for row in audits:
        row["session_id"] = None
    engine = create_async_engine(target, hide_parameters=True, connect_args={"timeout": 10})
    try:
        async with engine.connect() as connection:
            await require_empty(connection)
        print(f"Platform identities: {len(users)}; platform audit events: {len(audits)}")
        print("Supabase reachable; destination application data is empty.")
        if not apply:
            print("Preview only. Run with --apply to migrate schema and platform data.")
            return
        # env.py uses the same provider selector as the runtime.
        os.environ["DB_PROVIDER"] = "supabase"
        await asyncio.to_thread(command.upgrade, migration_config(), "head")
        async with engine.begin() as connection:
            await connection.execute(text("SELECT pg_advisory_xact_lock(71001983)"))
            await require_empty(connection)
            await connection.execute(insert(User), users)
            if audits:
                await connection.execute(insert(AuditEvent), audits)
            actual_users = [
                dict(row) for row in (await connection.execute(select(User.__table__))).mappings()
            ]
            actual_audits = [
                dict(row)
                for row in (await connection.execute(select(AuditEvent.__table__))).mappings()
            ]
            if sorted(actual_users, key=lambda row: row["id"]) != sorted(
                users, key=lambda row: row["id"]
            ):
                raise ValueError("Transferred identities failed exact verification")
            if sorted(actual_audits, key=lambda row: row["id"]) != sorted(
                audits, key=lambda row: row["id"]
            ):
                raise ValueError("Transferred audit history failed exact verification")
            for name, table in Base.metadata.tables.items():
                if name not in {"users", "auth_audit_events"}:
                    if await connection.scalar(select(table).limit(1)) is not None:
                        raise ValueError(f"Unexpected records in excluded table {name}")
            # New app tables must not expose password hashes or tenant data through
            # Supabase's anon/authenticated API roles. The backend uses its DB role.
            for name in Base.metadata.tables:
                await connection.execute(
                    text(f'REVOKE ALL ON TABLE public."{name}" FROM anon, authenticated')
                )
        print("Transfer committed and verified. All organization/business tables remain empty.")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(transfer(args.apply))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except Exception as error:
        # Connection/SQL errors may include credentials or record parameters.
        raise SystemExit(
            f"Platform transfer failed ({type(error).__name__}); no credentials printed"
        ) from None


if __name__ == "__main__":
    main()
