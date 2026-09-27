import asyncio
import json
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, update

from app.auth.factors import cipher
from app.auth.models import Invitation, MailOutbox, Membership, Organization, User
from app.auth.security import digest, now, verify_password
from tests.test_auth import PASSWORD
from tests.test_auth import anyio_backend as anyio_backend
from tests.test_auth import setup as setup
from tests.test_employees import ACCESS
from tests.test_stores import configured as configured
from tests.test_stores import owner as owner


async def link(app, email):
    async with app.state.db() as db:
        rows = await db.scalars(select(MailOutbox).order_by(MailOutbox.created.desc()))
        for row in rows:
            payload = json.loads(cipher(app.state.settings).decrypt(row.payload.encode()))
            if payload["email"] == email:
                return payload["body"].split("token=")[1].split()[0]
    raise AssertionError("Invitation email missing")


async def accept(client, token, **changes):
    return await client.post(
        "/api/v1/auth/invitations/accept", json={"token": token, "password": PASSWORD, **changes}
    )


@pytest.mark.anyio
async def test_staff_new_account_and_single_use(configured):
    app, client, base = configured
    response = await client.post(
        base + "/invitations", json={"email": "staff@example.com", **ACCESS}
    )
    assert response.status_code == 201, response.text
    raw = await link(app, "staff@example.com")
    preview = await client.post("/api/v1/auth/invitations/inspect", json={"token": raw})
    assert preview.json()["existing_account"] is False
    results = await asyncio.gather(accept(client, raw), accept(client, raw))
    assert sorted(r.status_code for r in results) == [204, 400]
    async with app.state.db() as db:
        user = await db.scalar(select(User).where(User.email == "staff@example.com"))
        assert user.email_verified and user.platform_role == "none"
        memberships = list(
            await db.scalars(select(Membership).where(Membership.user_id == user.id))
        )
        assert len(memberships) == 1
        assert memberships[0].organization_id == UUID(base.rsplit("/", 1)[1])
        assert memberships[0].permissions == sorted(ACCESS["permissions"])


@pytest.mark.anyio
async def test_existing_account_requires_password_and_retains_credentials(configured):
    app, client, base = configured
    async with app.state.db() as db:
        original = (
            await db.scalar(select(User).where(User.email == "owner1@example.com"))
        ).password_hash
    assert (
        await client.post(base + "/invitations", json={"email": "owner1@example.com", **ACCESS})
    ).status_code == 201
    raw = await link(app, "owner1@example.com")
    assert (await accept(client, raw, password="incorrect password")).status_code == 400
    assert (await accept(client, raw)).status_code == 204
    async with app.state.db() as db:
        user = await db.scalar(select(User).where(User.email == "owner1@example.com"))
        assert user.password_hash == original


@pytest.mark.anyio
async def test_resend_revoke_expiry_and_tenant_isolation(configured):
    app, client, base = configured
    body = {"email": "staff@example.com", **ACCESS}
    response = await client.post(base + "/invitations", json=body)
    path = base + "/invitations/" + response.json()["id"]
    raw = await link(app, body["email"])
    assert (await client.post(base + "/invitations", json=body)).status_code == 409
    async with app.state.db() as db, db.begin():
        await db.execute(update(Invitation).values(expires=now() - timedelta(seconds=1)))
    assert (await accept(client, raw)).status_code == 400
    assert (await client.get(base + "/invitations")).json()[0]["status"] == "expired"
    assert (await client.post(path, json={"action": "resend"})).status_code == 200
    fresh = await link(app, body["email"])
    assert fresh != raw
    assert (await accept(client, raw)).status_code == 400
    assert (await client.post(path, json={"action": "revoke"})).status_code == 200
    assert (await accept(client, fresh)).status_code == 400
    assert (await client.post(base + "/invitations", json=body)).status_code == 201
    async with app.state.db() as db:
        other = await db.scalar(
            select(Organization).where(Organization.id != UUID(base.rsplit("/", 1)[1]))
        )
    assert (await client.get(f"/api/v1/organizations/{other.id}/invitations")).status_code == 403


@pytest.mark.anyio
async def test_acceptance_rechecks_limits_and_inviter(configured):
    app, client, base = configured
    assert (
        await client.post(base + "/invitations", json={"email": "staff@example.com", **ACCESS})
    ).status_code == 201
    raw = await link(app, "staff@example.com")
    org_id = UUID(base.rsplit("/", 1)[1])
    async with app.state.db() as db, db.begin():
        await db.execute(
            update(Organization).where(Organization.id == org_id).values(employee_limit=1)
        )
    assert (await accept(client, raw)).status_code == 409
    async with app.state.db() as db, db.begin():
        await db.execute(
            update(Organization).where(Organization.id == org_id).values(employee_limit=5)
        )
        await db.execute(
            update(Membership)
            .where(Membership.organization_id == org_id)
            .values(permissions=["organization.read"])
        )
    assert (await accept(client, raw)).status_code == 409


@pytest.mark.anyio
async def test_platform_owner_onboarding(configured):
    app, client, _ = configured
    body = {
        "name": "Invited business",
        "owner_email": "newowner@example.com",
        "phone": "123",
        "owner_temporary_password": "temporary owner password for test",
    }
    assert (await client.post("/api/v1/platform/organizations", json=body)).status_code == 403
    async with app.state.db() as db, db.begin():
        await db.execute(
            update(User)
            .where(User.email == "owner0@example.com")
            .values(platform_role="super_admin")
        )
    response = await client.post("/api/v1/platform/organizations", json=body)
    assert response.status_code == 201, response.text
    org_id = response.json()["id"]
    async with app.state.db() as db:
        member = await db.scalar(
            select(Membership).where(Membership.organization_id == UUID(org_id))
        )
        assert member.roles == ["owner"] and "employees.manage" in member.permissions
        user = await db.get(User, member.user_id)
        assert user.platform_role == "none" and user.must_change_password
        assert verify_password(user.password_hash, body["owner_temporary_password"])
    listing = await client.get(f"/api/v1/platform/organizations/{org_id}/invitations")
    assert listing.json() == []


@pytest.mark.anyio
async def test_unlocked_and_configured_email_required(owner):
    app, client, base = owner
    assert (
        await client.post(base + "/invitations", json={"email": "staff@example.com", **ACCESS})
    ).status_code == 403


@pytest.mark.anyio
async def test_existing_mfa_is_required(configured):
    app, client, base = configured
    async with app.state.db() as db, db.begin():
        user = await db.scalar(select(User).where(User.email == "owner1@example.com"))
        user.mfa_secret = cipher(app.state.settings).encrypt(b"JBSWY3DPEHPK3PXP").decode()
        user.recovery_hashes = [digest("12345678901234567890")]
    assert (
        await client.post(base + "/invitations", json={"email": "owner1@example.com", **ACCESS})
    ).status_code == 201
    raw = await link(app, "owner1@example.com")
    assert (await accept(client, raw)).status_code == 400
    assert (await accept(client, raw, code="12345678901234567890")).status_code == 204


@pytest.mark.anyio
async def test_parallel_acceptances_enforce_last_seat(configured):
    app, client, base = configured
    org_id = UUID(base.rsplit("/", 1)[1])
    async with app.state.db() as db, db.begin():
        await db.execute(
            update(Organization).where(Organization.id == org_id).values(employee_limit=2)
        )
    tokens = []
    for email in ("first@example.com", "second@example.com"):
        assert (
            await client.post(base + "/invitations", json={"email": email, **ACCESS})
        ).status_code == 201
        tokens.append(await link(app, email))
    results = await asyncio.gather(*(accept(client, raw) for raw in tokens))
    assert sorted(r.status_code for r in results) == [204, 409]


@pytest.mark.anyio
async def test_legacy_unclaimed_owner_and_email_rollback(configured, monkeypatch):
    from app.auth import invitations

    app, client, _ = configured
    async with app.state.db() as db, db.begin():
        await db.execute(
            update(User)
            .where(User.email == "owner0@example.com")
            .values(platform_role="super_admin")
        )
        org = Organization(name="Legacy unclaimed", contact_email="legacy@example.com")
        db.add(org)
        await db.flush()
        org_id = org.id
    path = f"/api/v1/platform/organizations/{org_id}/invitations"
    assert (await client.post(path, json={})).status_code == 201
    raw = await link(app, "legacy@example.com")
    assert (await accept(client, raw)).status_code == 204
    assert (await client.post(path, json={})).status_code == 409

    def unavailable(*args, **kwargs):
        raise RuntimeError("outbox failure")

    async with app.state.db() as db, db.begin():
        pending = Organization(name="Rollback invitation", contact_email="rollback@example.com")
        db.add(pending)
        await db.flush()
        pending_id = pending.id
    monkeypatch.setattr(invitations, "queue_mail", unavailable)
    with pytest.raises(RuntimeError, match="outbox failure"):
        await client.post(f"/api/v1/platform/organizations/{pending_id}/invitations", json={})
    async with app.state.db() as db:
        assert (
            await db.scalar(select(Invitation.id).where(Invitation.organization_id == pending_id))
            is None
        )
