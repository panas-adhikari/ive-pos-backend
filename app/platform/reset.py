"""Remove tenant data while preserving platform identities and their auth records.

Preview by default. --apply requires paused writers and saves encrypted recovery
snapshots under backups/ before committing. Only this application's tables change.
"""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth.models import Base
from app.database import check_database_environment, database_url
from app.platform.migrate import BACKEND

PLATFORM_ROLES = {"super_admin", "employee"}
TABLES = [*Base.metadata.tables, "alembic_version"]
SNAPSHOT_SQL = (
    "SELECT jsonb_build_object("
    + ", ".join(
        f"'{name}', (SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text), "
        f"'[]'::jsonb) FROM public.\"{name}\" t)"
        for name in TABLES
    )
    + ")::text"
)
LOCK_SQL = "LOCK TABLE " + ", ".join(f'public."{name}"' for name in TABLES) + " IN EXCLUSIVE MODE"


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def in_values(values) -> str:
    return "(" + ", ".join(literal(str(value)) for value in sorted(values)) + ")"


def expected_snapshot(snapshot: dict, cipher: Fernet) -> dict:
    users = [row for row in snapshot["users"] if row["platform_role"] in PLATFORM_ROLES]
    if not users or not any(row["platform_role"] == "super_admin" for row in users):
        raise ValueError("Refusing cleanup without a platform super administrator")
    user_ids = {row["id"] for row in users}
    emails = {row["email"].strip().lower() for row in users}
    sessions = [row for row in snapshot["auth_sessions"] if row["user_id"] in user_ids]
    session_ids = {row["id"] for row in sessions}
    outbox = []
    for row in snapshot["auth_mail_outbox"]:
        payload = json.loads(cipher.decrypt(row["payload"].encode()))
        if payload["email"].strip().lower() in emails:
            outbox.append(row)
    expected = {name: [] for name in TABLES}
    expected.update(
        users=users,
        auth_sessions=sessions,
        auth_refresh_tokens=[
            row for row in snapshot["auth_refresh_tokens"] if row["session_id"] in session_ids
        ],
        auth_audit_events=[
            row
            for row in snapshot["auth_audit_events"]
            if row["user_id"] in user_ids
            and row["organization_id"] is None
            and (row["session_id"] is None or row["session_id"] in session_ids)
            and (
                row["target_type"] is None
                or (row["target_type"] == "users" and row["target_id"] in user_ids)
            )
        ],
        auth_email_challenges=[
            row
            for row in snapshot["auth_email_challenges"]
            if row["user_id"] in user_ids
            or (row["user_id"] is None and row["email"].strip().lower() in emails)
        ],
        auth_mail_outbox=outbox,
        alembic_version=snapshot["alembic_version"],
    )
    return expected


def cleanup_statements(expected: dict) -> list[str]:
    keep = {
        "users": ("id", [row["id"] for row in expected["users"]]),
        "auth_sessions": ("id", [row["id"] for row in expected["auth_sessions"]]),
        "auth_refresh_tokens": (
            "token_hash",
            [row["token_hash"] for row in expected["auth_refresh_tokens"]],
        ),
        "auth_audit_events": ("id", [row["id"] for row in expected["auth_audit_events"]]),
        "auth_email_challenges": (
            "token_hash",
            [row["token_hash"] for row in expected["auth_email_challenges"]],
        ),
        "auth_mail_outbox": ("id", [row["id"] for row in expected["auth_mail_outbox"]]),
    }
    statements = []
    # Reverse FK order removes dependents before parent organizations/users.
    for table in reversed(Base.metadata.sorted_tables):
        query = f'DELETE FROM public."{table.name}"'
        if table.name in keep:
            column, values = keep[table.name]
            if values:
                query += f' WHERE "{column}" NOT IN {in_values(values)}'
        statements.append(query)
    return statements


def normalized(snapshot: dict) -> str:
    return json.dumps(
        {
            name: sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
            for name, rows in snapshot.items()
        },
        sort_keys=True,
    )


def native_cleanup_sql(raw: str, expected: dict, commit: bool = True) -> str:
    fingerprint = hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()
    sql = "BEGIN; SET LOCAL lock_timeout = '10s'; " + LOCK_SQL + ";\n"
    sql += (
        "DO $guard$ BEGIN IF (SELECT md5(s.snapshot) FROM ("
        + SNAPSHOT_SQL
        + f") AS s(snapshot)) <> {literal(fingerprint)} THEN "
        "RAISE EXCEPTION 'Source changed since backup'; END IF; END $guard$;\n"
    )
    sql += ";\n".join(cleanup_statements(expected)) + ";\n"
    for name, rows in expected.items():
        expected_rows = literal(json.dumps(rows))
        sql += (
            f'DO $verify$ BEGIN IF EXISTS ((SELECT to_jsonb(t) FROM public."{name}" t '
            f"EXCEPT SELECT value FROM jsonb_array_elements({expected_rows}::jsonb)) "
            f"UNION ALL (SELECT value FROM jsonb_array_elements({expected_rows}::jsonb) "
            f'EXCEPT SELECT to_jsonb(t) FROM public."{name}" t)) THEN '
            "RAISE EXCEPTION 'Preservation check failed'; END IF; END $verify$;\n"
        )
    return sql + ("COMMIT;\n" if commit else "ROLLBACK;\n") + SNAPSHOT_SQL + ";"


def save_backup(directory: Path, name: str, raw: str, cipher: Fernet) -> None:
    path = directory / f"{name}.json.fernet"
    encrypted = cipher.encrypt(raw.encode())
    # Runtime backup artifacts, generated with owner-only permissions.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encrypted)
        stream.flush()
        os.fsync(stream.fileno())
    if cipher.decrypt(path.read_bytes()).decode() != raw:
        raise ValueError("Recovery snapshot verification failed")
    print(f"Encrypted {name} recovery snapshot: {path}", flush=True)


async def native_query(sql: str) -> str:
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
            'exec psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At',
        ],
        input=sql,
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
    )
    if result.returncode:
        raise ValueError("Native database command failed; its transaction was not committed")
    return result.stdout.strip()


def report(name: str, before: dict, expected: dict) -> None:
    print(
        f"{name}: preserve {len(expected['users'])} platform accounts; "
        f"remove {len(before['organizations'])} organizations and "
        f"{len(before['users']) - len(expected['users'])} tenant accounts.",
        flush=True,
    )


async def reset(apply: bool, backup_directory: Path) -> None:
    cipher = Fernet(os.environ["IDENTITY_ENCRYPTION_KEY"].encode())
    target = database_url(os.environ["SUPABASE_DATABASE_URL"])
    check_database_environment(target, "production", "supabase")
    native_raw = await native_query(SNAPSHOT_SQL + ";")
    native = json.loads(native_raw)
    native_expected = expected_snapshot(native, cipher)
    engine = create_async_engine(
        target, hide_parameters=True, connect_args={"timeout": 10, "command_timeout": 60}
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL lock_timeout = '10s'"))
            await connection.execute(text(LOCK_SQL))
            supabase_raw = await connection.scalar(text(SNAPSHOT_SQL))
            supabase = json.loads(supabase_raw)
            supabase_expected = expected_snapshot(supabase, cipher)
            report("native", native, native_expected)
            report("supabase", supabase, supabase_expected)
            if not apply:
                print("Preview only; no records changed.", flush=True)
                return
            backup_directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            save_backup(backup_directory, "native", native_raw, cipher)
            save_backup(backup_directory, "supabase", supabase_raw, cipher)
            for statement in cleanup_statements(supabase_expected):
                await connection.execute(text(statement))
            after = json.loads(await connection.scalar(text(SNAPSHOT_SQL)))
            if normalized(after) != normalized(supabase_expected):
                raise ValueError("Supabase verification failed; transaction will roll back")
        print("Supabase cleanup committed; platform records verified unchanged.", flush=True)
        # The fingerprint ensures no changes happened after the native backup.
        native_after = json.loads(
            await native_query(native_cleanup_sql(native_raw, native_expected))
        )
        if normalized(native_after) != normalized(native_expected):
            raise ValueError(
                "Native post-commit verification failed; use the saved recovery snapshot"
            )
        print("Native cleanup committed; platform records verified unchanged.", flush=True)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--backup-directory",
        type=Path,
        default=BACKEND.parent
        / "backups"
        / ("platform-reset-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")),
    )
    args = parser.parse_args()
    try:
        asyncio.run(reset(args.apply, args.backup_directory))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except Exception as error:
        raise SystemExit(
            f"Platform reset failed ({type(error).__name__}); credentials not printed"
        ) from None


if __name__ == "__main__":
    main()
