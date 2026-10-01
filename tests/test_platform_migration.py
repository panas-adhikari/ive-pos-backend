import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory

from app.auth.models import AuditEvent, User
from app.platform.migrate import migration_config, records, require_empty


class Destination:
    def __init__(self, tables, counts=None, revision=None):
        self.tables = tables
        self.counts = counts or {}
        self.revision = revision
        self.queries = []

    async def execute(self, query):
        sql = str(query)
        self.queries.append(sql)
        rows = self.tables if "pg_tables" in sql else [self.revision]
        return SimpleNamespace(scalars=lambda: rows)

    async def scalar(self, query):
        sql = str(query)
        self.queries.append(sql)
        return next((count for table, count in self.counts.items() if f'"{table}"' in sql), 0)


@pytest.mark.parametrize("table", ["users", "organizations", "sales"])
def test_platform_transfer_refuses_destination_data(table):
    destination = Destination([table], {table: 1})
    with pytest.raises(ValueError, match="not empty"):
        asyncio.run(require_empty(destination))


def test_platform_transfer_refuses_incompatible_schema():
    destination = Destination(["alembic_version"], revision="older_revision")
    with pytest.raises(ValueError, match="schema revision"):
        asyncio.run(require_empty(destination))


def test_platform_transfer_accepts_empty_app_without_touching_managed_tables():
    head = ScriptDirectory.from_config(migration_config()).get_current_head()
    destination = Destination(
        ["users", "organizations", "managed_table", "alembic_version"], revision=head
    )
    asyncio.run(require_empty(destination))
    assert not any("managed_table" in query for query in destination.queries)
    assert all(query.startswith("SELECT") for query in destination.queries)


def test_platform_record_conversion_preserves_credentials_and_audit_dates():
    user_id = uuid4()
    row = {column.name: None for column in User.__table__.columns}
    row.update(
        id=str(user_id),
        password_hash="existing-password-hash",
        mfa_secret="existing-encrypted-mfa",
        recovery_hashes=["existing-recovery-hash"],
        platform_role="super_admin",
    )
    converted = records(User.__table__, [row])[0]
    assert converted["id"] == user_id
    assert {key: value for key, value in converted.items() if key != "id"} == {
        key: value for key, value in row.items() if key != "id"
    }
    created = datetime(2026, 10, 1, tzinfo=timezone.utc)
    audit = {column.name: None for column in AuditEvent.__table__.columns}
    audit.update(id=str(uuid4()), user_id=str(user_id), created=created.isoformat())
    restored = records(AuditEvent.__table__, [audit])[0]
    assert restored["created"] == created
    assert restored["user_id"] == user_id
    assert restored["organization_id"] is None
