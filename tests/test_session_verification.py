from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.auth.models import Membership, Session, User
from app.auth.security import now
from tests.test_auth import PASSWORD
from tests.test_auth import setup as setup
from tests.test_stores import CODES
from tests.test_stores import owner as strict_owner  # noqa: F401


@pytest.fixture
async def owner(strict_owner):  # noqa: F811
    app, client, path = strict_owner
    async with app.state.db() as db, db.begin():
        user = await db.scalar(select(User).where(User.email == "owner0@example.com"))
        user.require_action_verification = False
    return app, client, path


@pytest.mark.anyio
async def test_mfa_login_authorizes_changes_and_preference_applies_to_session(owner):
    app, client, _ = owner
    me = (await client.get("/api/v1/auth/me")).json()
    assert me["require_action_verification"] is False
    assert me["step_up_expires"]
    # No password or second MFA code is required after MFA login.
    assert (await client.post("/api/v1/auth/mfa/recovery-codes", json={})).status_code == 200
    assert (
        await client.post(
            "/api/v1/auth/security-preferences",
            json={
                "require_action_verification": True,
            },
        )
    ).status_code == 200
    assert (await client.get("/api/v1/auth/me")).json()["step_up_expires"] is None
    assert (
        await client.post(
            "/api/v1/auth/security-preferences",
            json={
                "require_action_verification": False,
            },
        )
    ).status_code == 400
    async with app.state.db() as db, db.begin():
        session = await db.get(Session, UUID(me["session_id"]))
        session.expires = now() - timedelta(seconds=1)
    assert (await client.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.anyio
async def test_staff_onboarding_is_scoped_and_requires_password_change(owner):
    app, client, _ = owner
    payload = {
        "email": "staff@example.com",
        "full_name": "Platform Support",
        "temporary_password": "temporary staff passphrase",
        "job_title": "Support",
    }
    assert (await client.post("/api/v1/platform/staff", json=payload)).status_code == 403
    async with app.state.db() as db, db.begin():
        actor = await db.scalar(select(User).where(User.email == "owner0@example.com"))
        actor.platform_role = "super_admin"
    response = await client.post("/api/v1/platform/staff", json=payload)
    assert response.status_code == 201, response.text
    staff = response.json()
    assert staff["role"] == "employee"
    assert staff["must_change_password"] is True
    assert payload["temporary_password"] not in response.text
    assert "password_hash" not in staff
    assert (await client.post("/api/v1/platform/staff", json=payload)).status_code == 409
    assert len((await client.get("/api/v1/platform/staff")).json()) == 2
    async with app.state.db() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.user_id == UUID(staff["id"]))
            )
            == 0
        )
    assert (
        await client.post(
            "/api/v1/auth/login",
            json={
                "email": payload["email"],
                "password": payload["temporary_password"],
            },
        )
    ).status_code == 204
    assert (await client.get("/api/v1/platform/organizations")).status_code == 403
    assert (
        await client.post(
            "/api/v1/auth/password",
            json={
                "current_password": payload["temporary_password"],
                "new_password": PASSWORD,
            },
        )
    ).status_code == 204
    assert (
        await client.post(
            "/api/v1/auth/login",
            json={
                "email": payload["email"],
                "password": PASSWORD,
            },
        )
    ).status_code == 204
    assert (await client.get("/api/v1/platform/organizations")).status_code == 200
    assert (await client.get("/api/v1/platform/staff")).status_code == 403
    assert (await client.post("/api/v1/platform/staff", json=payload)).status_code == 403


@pytest.mark.anyio
async def test_legacy_session_must_verify_once_and_refresh_preserves_proof(owner):
    app, client, _ = owner
    me = (await client.get("/api/v1/auth/me")).json()
    async with app.state.db() as db, db.begin():
        session = await db.get(Session, UUID(me["session_id"]))
        session.mfa_verified = False
    assert (await client.post("/api/v1/auth/mfa/recovery-codes", json={})).status_code == 400
    assert (
        await client.post(
            "/api/v1/auth/step-up",
            json={
                "password": PASSWORD,
                "code": CODES[1],
            },
        )
    ).status_code == 200
    assert (await client.post("/api/v1/auth/refresh", json={})).status_code == 204
    assert (await client.post("/api/v1/auth/mfa/recovery-codes", json={})).status_code == 200
    assert (
        await client.post(
            "/api/v1/auth/password",
            json={
                "new_password": PASSWORD + " updated",
            },
        )
    ).status_code == 204
    assert (await client.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.anyio
async def test_stricter_preference_invalidates_other_session_grants(owner):
    import httpx

    from tests.test_auth import HEADERS, ORIGIN

    app, client, _ = owner
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=HEADERS
    ) as other:
        assert (
            await other.post(
                "/api/v1/auth/login",
                json={
                    "email": "owner0@example.com",
                    "password": PASSWORD,
                    "code": CODES[1],
                },
            )
        ).status_code == 204
        assert (await other.post("/api/v1/auth/step-up", json={})).status_code == 200
        assert (
            await client.post(
                "/api/v1/auth/security-preferences",
                json={
                    "require_action_verification": True,
                },
            )
        ).status_code == 200
        assert (await other.get("/api/v1/auth/me")).json()["step_up_expires"] is None
        assert (await other.post("/api/v1/auth/mfa/recovery-codes", json={})).status_code == 400
        assert (
            await other.post(
                "/api/v1/auth/security-preferences",
                json={
                    "require_action_verification": False,
                    "password": PASSWORD,
                    "code": CODES[2],
                },
            )
        ).status_code == 200
        assert (await client.post("/api/v1/auth/mfa/recovery-codes", json={})).status_code == 200


@pytest.mark.anyio
async def test_password_only_session_cannot_bypass_credentials(setup):
    _, client = setup
    assert (
        await client.post(
            "/api/v1/auth/login",
            json={
                "email": "owner0@example.com",
                "password": PASSWORD,
            },
        )
    ).status_code == 204
    assert (await client.get("/api/v1/auth/me")).json()["step_up_expires"] is None
    assert (
        await client.post(
            "/api/v1/auth/password",
            json={
                "new_password": PASSWORD + " changed",
            },
        )
    ).status_code == 400
    assert (
        await client.post(
            "/api/v1/auth/security-preferences",
            json={
                "require_action_verification": False,
            },
        )
    ).status_code == 400
