from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.auth.factors import cipher
from app.auth.models import AuditEvent, EmailChallenge, Membership, Organization, Session, User
from app.auth.security import PASSWORD_HASHER, digest, now, verify_password
from tests.test_auth import HEADERS, ORIGIN, PASSWORD
from tests.test_auth import anyio_backend as anyio_backend
from tests.test_auth import setup as setup
from tests.test_platform import make_platform_admin
from tests.test_stores import CODES
from tests.test_stores import configured as configured
from tests.test_stores import owner as owner


async def target(app):
    async with app.state.db() as db, db.begin():
        user = await db.scalar(select(User).where(User.email == "owner1@example.com"))
        membership = await db.scalar(select(Membership).where(Membership.user_id == user.id))
        organization = await db.get(Organization, membership.organization_id)
        organization.contact_email = user.email
        membership.roles = ["owner"]
        return user.id, organization.id, organization.version


@pytest.mark.anyio
async def test_reset_revokes_sessions_preserves_mfa_and_forces_password_change(configured):
    app, client, _ = configured
    user_id, org_id, version = await target(app)
    await make_platform_admin(app)
    path = f"/api/v1/platform/organizations/{org_id}/owner-password-reset"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=HEADERS
    ) as account:
        assert (
            await account.post(
                "/api/v1/auth/login", json={"email": "owner1@example.com", "password": PASSWORD}
            )
        ).status_code == 204
        async with app.state.db() as db, db.begin():
            user = await db.get(User, user_id)
            mfa = cipher(app.state.settings).encrypt(b"JBSWY3DPEHPK3PXP").decode()
            user.mfa_secret = mfa
            user.recovery_hashes = [digest(code) for code in CODES]
            db.add(
                EmailChallenge(
                    token_hash=digest("old-reset"),
                    email=user.email,
                    purpose="reset",
                    user_id=user.id,
                    credential_hash=digest(user.password_hash),
                    expires=now() + timedelta(minutes=30),
                )
            )
        response = await client.post(path, json={"expected_version": version})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        result = response.json()
        temporary = result["temporary_password"]
        assert len(temporary) >= 24 and result["email"] == "owner1@example.com"
        assert result["login_url"] == ORIGIN + "/login"
        async with app.state.db() as db:
            user = await db.get(User, user_id)
            assert PASSWORD_HASHER.verify(user.password_hash, temporary)
            assert not verify_password(user.password_hash, PASSWORD)
            assert user.must_change_password and user.mfa_secret == mfa
            assert user.recovery_hashes == [digest(code) for code in CODES]
            assert all(
                row.revoked
                for row in await db.scalars(select(Session).where(Session.user_id == user_id))
            )
            assert (await db.get(EmailChallenge, digest("old-reset"))).used
            audit = await db.scalar(
                select(AuditEvent).where(AuditEvent.action == "platform.owner_password_reset")
            )
            assert audit.organization_id == org_id and audit.target_id == user_id
            assert temporary not in str(audit.changes)
        assert (await account.get("/api/v1/auth/me")).status_code == 401
        assert (
            await account.post(
                "/api/v1/auth/login", json={"email": result["email"], "password": temporary}
            )
        ).status_code == 401
        assert (
            await account.post(
                "/api/v1/auth/login",
                json={"email": result["email"], "password": temporary, "code": CODES[0]},
            )
        ).status_code == 204
        assert (await account.get("/api/v1/auth/me")).json()["must_change_password"]
        changed = await account.post(
            "/api/v1/auth/password",
            json={
                "current_password": temporary,
                "new_password": "new permanent owner password",
                "code": CODES[1],
            },
        )
        assert changed.status_code == 204
        async with app.state.db() as db:
            user = await db.get(User, user_id)
            assert not user.must_change_password
            assert PASSWORD_HASHER.verify(user.password_hash, "new permanent owner password")


@pytest.mark.anyio
async def test_reset_requires_super_admin_and_recent_verification(configured):
    app, client, _ = configured
    user_id, org_id, version = await target(app)
    path = f"/api/v1/platform/organizations/{org_id}/owner-password-reset"
    body = {"expected_version": version}
    assert (await client.post(path, json=body)).status_code == 403
    await make_platform_admin(app)
    async with app.state.db() as db, db.begin():
        actor = await db.scalar(select(User).where(User.email == "owner0@example.com"))
        actor.platform_role = "employee"
    assert (await client.post(path, json=body)).status_code == 403
    await make_platform_admin(app)
    async with app.state.db() as db, db.begin():
        session = await db.scalar(select(Session).where(Session.user_id == actor.id))
        session.step_up_expires = None
    assert (await client.post(path, json=body)).status_code == 403
    async with app.state.db() as db:
        assert verify_password((await db.get(User, user_id)).password_hash, PASSWORD)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reason", ["stale", "deletion", "inactive", "unrelated", "not_owner", "platform"]
)
async def test_reset_rejects_invalid_targets_without_changing_credentials(configured, reason):
    app, client, _ = configured
    user_id, org_id, version = await target(app)
    await make_platform_admin(app)
    async with app.state.db() as db, db.begin():
        organization = await db.get(Organization, org_id)
        user = await db.get(User, user_id)
        if reason == "deletion":
            organization.deletion_scheduled_for = now()
        if reason == "inactive":
            user.active = False
        if reason == "unrelated":
            organization.contact_email = "owner0@example.com"
        if reason == "not_owner":
            member = await db.scalar(select(Membership).where(Membership.user_id == user_id))
            member.roles = ["custom"]
        if reason == "platform":
            user.platform_role = "employee"
    response = await client.post(
        f"/api/v1/platform/organizations/{org_id}/owner-password-reset",
        json={"expected_version": version + (1 if reason == "stale" else 0)},
    )
    assert response.status_code == 409 and "temporary_password" not in response.text
    async with app.state.db() as db:
        assert verify_password((await db.get(User, user_id)).password_hash, PASSWORD)
        assert not await db.scalar(
            select(AuditEvent.id).where(AuditEvent.action == "platform.owner_password_reset")
        )
