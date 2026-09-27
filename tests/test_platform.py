from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.auth.models import AuditEvent, Invitation, Membership, Organization, Session, User
from app.auth.security import now
from app.stores.models import Register, Store
from tests.test_auth import setup as setup
from tests.test_stores import configured as configured
from tests.test_stores import owner as owner


async def make_platform_admin(app):
    async with app.state.db() as db, db.begin():
        actor = await db.scalar(select(User).where(User.email == "owner0@example.com"))
        actor.platform_role = "super_admin"
        actor.mfa_secret = None
        await db.execute(
            update(Session).where(Session.user_id == actor.id).values(step_up_expires=None)
        )


@pytest.mark.anyio
async def test_platform_options_use_validated_timezones(configured):
    app, client, _ = configured
    path = "/api/v1/platform/options"
    assert (await client.get(path)).status_code == 403
    await make_platform_admin(app)
    response = await client.get(path)
    assert response.status_code == 200
    assert "Asia/Kathmandu" in response.json()["timezones"]
    assert "NPR" in response.json()["currencies"]


@pytest.mark.anyio
async def test_production_deletion_is_scheduled_and_can_be_cancelled(configured):
    app, client, _ = configured
    async with app.state.db() as db:
        target = await db.scalar(select(Organization).where(Organization.name == "Store 1"))

    path = f"/api/v1/platform/organizations/{target.id}"
    assert (
        await client.request("DELETE", path, json={"confirm_name": target.name})
    ).status_code == 403
    await make_platform_admin(app)
    wrong = await client.request("DELETE", path, json={"confirm_name": "wrong"})
    assert wrong.status_code == 422

    response = await client.request("DELETE", path, json={"confirm_name": target.name})
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "scheduled"
    scheduled = await client.get(path)
    assert scheduled.status_code == 200
    assert scheduled.json()["deletion_scheduled_for"] == result["deletion_scheduled_for"]
    async with app.state.db() as db:
        target = await db.get(Organization, target.id)
        assert timedelta(days=29) < target.deletion_scheduled_for - now() <= timedelta(days=30)
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.organization_id == target.id)
            )
            == 1
        )

    cancelled = await client.post(path + "/deletion/cancel", json={})
    assert cancelled.status_code == 200
    async with app.state.db() as db:
        assert (await db.get(Organization, target.id)).deletion_scheduled_for is None


@pytest.mark.anyio
async def test_development_deletion_removes_tenant_children(configured):
    app, client, _ = configured
    await make_platform_admin(app)
    app.state.settings.environment = "development"
    async with app.state.db() as db, db.begin():
        actor = await db.scalar(select(User).where(User.email == "owner0@example.com"))
        target = await db.scalar(select(Organization).where(Organization.name == "Store 1"))
        employee = await db.scalar(select(User).where(User.email == "owner1@example.com"))
        store = Store(
            organization_id=target.id,
            code="CHILD",
            name="Child store",
            timezone="UTC",
            receipt_name="Child store",
        )
        db.add(store)
        await db.flush()
        register = Register(store_id=store.id, code="POS1", name="Counter")
        invitation = Invitation(
            organization_id=target.id,
            invited_by=actor.id,
            email="future@example.com",
            kind="staff",
            access={},
            token_hash="deletion-test-token-hash",
            expires=now() + timedelta(days=1),
            status="pending",
        )
        audit = AuditEvent(
            organization_id=target.id,
            user_id=employee.id,
            action="tenant.test",
            created=now(),
        )
        db.add_all([register, invitation, audit])
        await db.flush()
        ids = target.id, employee.id, store.id, register.id, invitation.id, audit.id

    response = await client.request(
        "DELETE",
        f"/api/v1/platform/organizations/{ids[0]}",
        json={"confirm_name": "Store 1"},
    )
    assert response.status_code == 200 and response.json()["status"] == "deleted"
    async with app.state.db() as db:
        assert await db.get(Organization, ids[0]) is None
        assert not await db.scalar(
            select(Membership.id).where(Membership.organization_id == ids[0])
        )
        assert await db.get(Store, ids[2]) is None
        assert await db.get(Register, ids[3]) is None
        assert await db.get(Invitation, ids[4]) is None
        assert await db.get(AuditEvent, ids[5]) is None
        assert await db.get(User, ids[1]) is not None
        event = await db.scalar(
            select(AuditEvent).where(AuditEvent.action == "platform.organization_deleted")
        )
        assert event and event.organization_id is None and event.target_id == ids[0]
