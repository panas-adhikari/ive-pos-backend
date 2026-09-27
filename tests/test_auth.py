import asyncio
import os
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text, update

from app.auth.models import (
    AuditEvent,
    Membership,
    Organization,
    RateBucket,
    RefreshToken,
    Session,
    User,
)
from app.auth.security import PASSWORD_HASHER, digest, now, rate_key
from app.config import Settings
from app.main import create_app

PASSWORD = "correct horse local retail battery"
HASH = PASSWORD_HASHER.hash(PASSWORD)
ORIGIN = "https://pos.example.test"
HEADERS = {"Origin": ORIGIN, "X-POS-CSRF": "1"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def setup():
    url = os.environ.get("AUTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set AUTH_TEST_DATABASE_URL to a migrated dedicated PostgreSQL test database")
    from sqlalchemy.engine import make_url

    if not make_url(url).database.endswith("_test"):
        raise RuntimeError("Refusing to truncate a database without the _test suffix")
    settings = Settings(
        database_url=url,
        auth_secret="integration-test-secret-at-least-32-characters",
        public_origin=ORIGIN,
        identity_encryption_key=Fernet.generate_key().decode(),
        smtp_host="localhost",
    )
    app = create_app(settings)

    @app.get("/api/v1/new-protected-route")
    async def future_route():
        return {"ok": True}

    async with app.router.lifespan_context(app):
        async with app.state.db() as db, db.begin():
            await db.execute(
                text(
                    "TRUNCATE stock_movements, sale_lines, sales, stock_balances, products, "
                    "customers, invitations, registers, stores, auth_email_challenges, "
                    "auth_mail_outbox, "
                    "auth_audit_events, auth_refresh_tokens, "
                    "auth_sessions, memberships, users, organizations, auth_rate_buckets"
                )
            )
            for index in range(2):
                user = User(email=f"owner{index}@example.com", password_hash=HASH)
                org = Organization(name=f"Store {index}")
                db.add_all([user, org])
                await db.flush()
                db.add(
                    Membership(
                        user_id=user.id, organization_id=org.id, permissions=["organization.read"]
                    )
                )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=HEADERS
        ) as client:
            yield app, client


async def login(client, index=0, password=PASSWORD):
    return await client.post(
        "/api/v1/auth/login",
        json={
            "email": f"owner{index}@example.com",
            "password": password,
        },
    )


async def me(client):
    return await client.get("/api/v1/auth/me")


@pytest.mark.anyio
async def test_login_cookies_and_no_raw_secrets_in_database(setup):
    app, client = setup
    assert (await me(client)).status_code == 401
    response = await login(client)
    assert response.status_code == 204
    for cookie in response.headers.get_list("set-cookie"):
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
        assert "Path=/" in cookie and "Domain=" not in cookie and "__Host-" in cookie
    info = (await me(client)).json()
    assert info["email"] == "owner0@example.com"
    assert "password" not in str(info)
    async with app.state.db() as db:
        session = await db.get(Session, UUID(info["session_id"]))
        assert session.access_hash == digest(client.cookies["__Host-pos_access"])
        assert session.access_hash != client.cookies["__Host-pos_access"]
        assert await db.get(RefreshToken, digest(client.cookies["__Host-pos_refresh"]))
        user = await db.get(User, session.user_id)
        assert user.password_hash.startswith("$argon2id$")
    assert response.headers["cache-control"] == "no-store"
    assert (await client.get("/api/v1/new-protected-route")).status_code == 200


@pytest.mark.anyio
async def test_bad_credentials_generic_and_default_deny(setup):
    _, client = setup
    bad = await login(client, password="wrong")
    missing = await login(client, index=9, password="wrong")
    assert bad.status_code == missing.status_code == 401
    assert bad.json() == missing.json() == {"detail": "Invalid email or password"}
    assert (await client.get("/api/v1/new-protected-route")).status_code == 401
    assert (await client.get("/docs")).status_code == 404


@pytest.mark.anyio
async def test_csrf_content_type_body_limit_and_validation_redaction(setup):
    _, client = setup
    for headers in (
        {"Origin": "https://evil.test"},
        {"X-POS-CSRF": ""},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        response = await client.post("/api/v1/auth/login", json={}, headers=headers)
        assert response.status_code == 403
    response = await client.post(
        "/api/v1/auth/login", content="a" * 17000, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert (await client.post("/api/v1/auth/login", data={"email": "x"})).status_code == 415
    secret = "sensitive-password-" * 20
    response = await client.post("/api/v1/auth/login", json={"email": "bad", "password": secret})
    assert response.status_code == 422
    assert secret not in response.text and "password" not in response.text
    preflight = await client.options(
        "/api/v1/auth/login",
        headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in preflight.headers


@pytest.mark.anyio
async def test_tenant_permission_and_membership_checks(setup):
    app, client = setup
    await login(client)
    info = (await me(client)).json()
    own = info["memberships"][0]["organization_id"]
    async with app.state.db() as db:
        other = await db.scalar(select(Organization).where(Organization.name == "Store 1"))
        other_id = other.id
    assert (await client.get(f"/api/v1/organizations/{own}")).status_code == 200
    assert (await client.get(f"/api/v1/organizations/{other_id}")).status_code == 403
    async with app.state.db() as db, db.begin():
        await db.execute(
            update(Membership).where(Membership.user_id == UUID(info["id"])).values(permissions=[])
        )
    assert (await client.get(f"/api/v1/organizations/{own}")).status_code == 403
    async with app.state.db() as db, db.begin():
        await db.execute(
            update(Membership).where(Membership.user_id == UUID(info["id"])).values(active=False)
        )
    assert (await me(client)).status_code == 401
    assert (await client.post("/api/v1/auth/refresh", json={})).status_code == 401
    assert (await login(client)).status_code == 401


@pytest.mark.anyio
async def test_rotation_and_replay_revoke_entire_session(setup):
    app, client = setup
    await login(client)
    old_refresh = client.cookies["__Host-pos_refresh"]
    old_access = client.cookies["__Host-pos_access"]
    assert (await client.post("/api/v1/auth/refresh", json={})).status_code == 204
    assert client.cookies["__Host-pos_refresh"] != old_refresh
    assert (await me(client)).status_code == 200
    assert (
        await client.get("/api/v1/auth/me", headers={"Cookie": f"__Host-pos_access={old_access}"})
    ).status_code == 401
    response = await client.post(
        "/api/v1/auth/refresh", json={}, headers={"Cookie": f"__Host-pos_refresh={old_refresh}"}
    )
    assert response.status_code == 401
    assert (await me(client)).status_code == 401
    async with app.state.db() as db:
        assert await db.scalar(select(AuditEvent.id).where(AuditEvent.action == "refresh.replay"))


@pytest.mark.anyio
async def test_concurrent_refresh_detects_replay_without_duplicate_active_session(setup):
    app, client = setup
    await login(client)
    cookie = f"__Host-pos_refresh={client.cookies['__Host-pos_refresh']}"

    async def rotate():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=HEADERS
        ) as other:
            return await other.post("/api/v1/auth/refresh", json={}, headers={"Cookie": cookie})

    responses = await asyncio.gather(rotate(), rotate())
    assert sorted(r.status_code for r in responses) == [204, 401]
    async with app.state.db() as db:
        assert not list(await db.scalars(select(Session).where(Session.revoked.is_(False))))


@pytest.mark.anyio
async def test_access_and_session_expiration(setup):
    app, client = setup
    await login(client)
    async with app.state.db() as db, db.begin():
        await db.execute(update(Session).values(access_expires=now() - timedelta(seconds=1)))
    assert (await me(client)).status_code == 401
    assert (await client.post("/api/v1/auth/refresh", json={})).status_code == 204
    assert (await me(client)).status_code == 200
    async with app.state.db() as db, db.begin():
        await db.execute(update(Session).values(expires=now() - timedelta(seconds=1)))
    assert (await me(client)).status_code == 401
    assert (await client.post("/api/v1/auth/refresh", json={})).status_code == 401


@pytest.mark.anyio
async def test_account_disabled_invalidates_existing_tokens(setup):
    app, client = setup
    await login(client)
    async with app.state.db() as db, db.begin():
        await db.execute(update(User).values(active=False))
    assert (await me(client)).status_code == 401
    assert (await client.post("/api/v1/auth/refresh", json={})).status_code == 401
    assert (await login(client)).status_code == 401


@pytest.mark.anyio
async def test_password_change_requires_current_password_and_revokes_all_sessions(setup):
    app, client = setup
    await login(client)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=HEADERS
    ) as other:
        await login(other)
        response = await client.post(
            "/api/v1/auth/password",
            json={"current_password": "wrong", "new_password": PASSWORD + " changed"},
        )
        assert response.status_code == 400
        assert (await me(client)).status_code == 200
        response = await client.post(
            "/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": "short"}
        )
        assert response.status_code == 422
        response = await client.post(
            "/api/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": PASSWORD + " changed"},
        )
        assert response.status_code == 204
        assert (await me(other)).status_code == 401
        assert (await other.post("/api/v1/auth/refresh", json={})).status_code == 401
        assert (await me(client)).status_code == 401
        assert (await login(client)).status_code == 401
        assert (await login(client, password=PASSWORD + " changed")).status_code == 204


@pytest.mark.anyio
async def test_session_revoke_ownership_and_logout(setup):
    app, client = setup
    await login(client)
    first_session = (await me(client)).json()["session_id"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=HEADERS
    ) as other:
        await login(other, index=1)
        response = await other.post(f"/api/v1/auth/sessions/{first_session}/revoke", json={})
        assert response.status_code == 404
        assert len((await other.get("/api/v1/auth/sessions")).json()) == 1
        await login(other)
        assert len((await other.get("/api/v1/auth/sessions")).json()) == 2
        assert (
            await other.post(f"/api/v1/auth/sessions/{first_session}/revoke", json={})
        ).status_code == 204
        assert (await me(client)).status_code == 401
        saved = other.cookies["__Host-pos_refresh"]
        assert (await other.post("/api/v1/auth/logout", json={})).status_code == 204
        assert (await me(other)).status_code == 401
        assert (
            await other.post(
                "/api/v1/auth/refresh", json={}, headers={"Cookie": f"__Host-pos_refresh={saved}"}
            )
        ).status_code == 401


@pytest.mark.anyio
async def test_database_rate_limit_survives_requests_and_ignores_forwarded_headers(setup):
    app, client = setup
    await login(client, password="wrong")
    async with app.state.db() as db, db.begin():
        await db.execute(update(RateBucket).values(count=120))
    response = await client.post(
        "/api/v1/auth/login",
        headers={"X-Forwarded-For": "1.2.3.4"},
        json={"email": "owner0@example.com", "password": PASSWORD},
    )
    assert response.status_code == 429
    assert 0 < int(response.headers["retry-after"]) <= 900
    key = rate_key(
        app.state.settings.auth_secret.get_secret_value(), "login:account:owner0@example.com"
    )
    async with app.state.db() as db:
        assert await db.scalar(select(RateBucket.count).where(RateBucket.key == key)) == 121


def test_production_configuration_fails_closed():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(database_url="postgresql+asyncpg://localhost/db", auth_secret="short")
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+asyncpg://localhost/db",
            auth_secret="a" * 32,
            public_origin="http://localhost:5173",
        )


@pytest.mark.anyio
async def test_idle_expiry_and_logout_without_refresh_cookie(setup):
    app, client = setup
    await login(client)
    saved_access = client.cookies["__Host-pos_access"]
    response = await client.post(
        "/api/v1/auth/logout", json={}, headers={"Cookie": f"__Host-pos_access={saved_access}"}
    )
    assert response.status_code == 204
    assert (
        await client.get("/api/v1/auth/me", headers={"Cookie": f"__Host-pos_access={saved_access}"})
    ).status_code == 401
    await login(client)
    async with app.state.db() as db, db.begin():
        await db.execute(update(Session).values(idle_expires=now() - timedelta(seconds=1)))
    assert (await client.post("/api/v1/auth/refresh", json={})).status_code == 401


@pytest.mark.anyio
async def test_concurrent_throttle_increment_is_atomic(setup):
    app, client = setup
    await login(client)
    key = rate_key(
        app.state.settings.auth_secret.get_secret_value(), "login:account:owner0@example.com"
    )
    async with app.state.db() as db, db.begin():
        await db.execute(update(RateBucket).where(RateBucket.key == key).values(count=9))

    async def attempt():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=HEADERS
        ) as other:
            return await login(other)

    responses = await asyncio.gather(attempt(), attempt())
    assert sorted(r.status_code for r in responses) == [204, 429]


@pytest.mark.anyio
async def test_owner_provisioning_rolls_back_on_duplicate_email(setup):
    from sqlalchemy.exc import IntegrityError

    from app.auth.bootstrap import OwnerInput, provision

    app, _ = setup
    with pytest.raises(IntegrityError):
        await provision(
            app.state.settings,
            OwnerInput(
                email="owner0@example.com", organization="Must roll back", password=PASSWORD
            ),
        )
    async with app.state.db() as db:
        assert (
            await db.scalar(select(Organization.id).where(Organization.name == "Must roll back"))
            is None
        )


@pytest.mark.anyio
async def test_password_change_racing_another_session_refresh_revokes_both(setup):
    app, client = setup
    await login(client)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=HEADERS
    ) as other:
        await login(other)
        changed, rotated = await asyncio.gather(
            client.post(
                "/api/v1/auth/password",
                json={"current_password": PASSWORD, "new_password": PASSWORD + " changed"},
            ),
            other.post("/api/v1/auth/refresh", json={}),
        )
        assert changed.status_code == 204
        assert rotated.status_code in {204, 401}
        assert (await me(other)).status_code == 401
        assert (await other.post("/api/v1/auth/refresh", json={})).status_code == 401


async def latest_link(app, email, purpose):
    import json
    from urllib.parse import parse_qs, urlsplit

    from app.auth.factors import cipher
    from app.auth.models import MailOutbox

    async with app.state.db() as db:
        rows = await db.scalars(select(MailOutbox).order_by(MailOutbox.created.desc()))
        for row in rows:
            payload = json.loads(cipher(app.state.settings).decrypt(row.payload.encode()))
            if payload["email"] == email and "#identity=" + purpose in payload["body"]:
                link = payload["body"].split("\n\n")[1]
                assert link.startswith(ORIGIN + "/#")
                return parse_qs(urlsplit(link).fragment)["token"][0]
    raise AssertionError("No matching email")


async def verify_owner(app, client):
    await login(client)
    assert (
        await client.post("/api/v1/auth/email/request", json={"password": PASSWORD})
    ).status_code == 202
    raw = await latest_link(app, "owner0@example.com", "verify")
    assert (
        await client.post("/api/v1/auth/email/complete", json={"token": raw})
    ).status_code == 204


async def enable_mfa(app, client):
    import pyotp

    await verify_owner(app, client)
    response = await client.post("/api/v1/auth/mfa/enroll", json={"password": PASSWORD})
    assert response.status_code == 200
    secret = response.json()["secret"]
    code = pyotp.TOTP(secret).now()
    response = await client.post("/api/v1/auth/mfa/confirm", json={"code": code})
    assert response.status_code == 200
    return secret, response.json()["recovery_codes"], code


@pytest.mark.anyio
async def test_verified_signup_atomic_and_single_use(setup):
    from sqlalchemy import func

    from app.auth.models import EmailChallenge, MailOutbox

    app, client = setup
    email = "new@example.com"
    response = await client.post("/api/v1/auth/signup/request", json={"email": " NEW@example.com "})
    assert response.status_code == 202
    assert "token" not in response.text
    raw = await latest_link(app, email, "signup")
    async with app.state.db() as db:
        assert await db.scalar(select(User).where(User.email == email)) is None
        challenge = await db.get(EmailChallenge, digest(raw))
        assert challenge and challenge.user_id is None
        assert raw not in str((await db.scalars(select(MailOutbox.payload))).all())
    payload = {"token": raw, "password": PASSWORD, "organization": "New shop"}
    responses = await asyncio.gather(
        *[client.post("/api/v1/auth/signup/complete", json=payload) for _ in range(2)]
    )
    assert sorted(r.status_code for r in responses) == [204, 400]
    assert (await me(client)).status_code == 401  # No automatic login.
    async with app.state.db() as db:
        user = await db.scalar(select(User).where(User.email == email))
        assert user.email_verified
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Organization)
                .where(Organization.name == "New shop")
            )
            == 1
        )
        assert await db.scalar(select(Membership).where(Membership.user_id == user.id))
    assert (
        await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    ).status_code == 204


@pytest.mark.anyio
async def test_links_expiry_purpose_and_no_account_enumeration(setup):
    from app.auth.models import EmailChallenge

    app, client = setup
    known = await client.post("/api/v1/auth/signup/request", json={"email": "owner0@example.com"})
    unknown = await client.post("/api/v1/auth/signup/request", json={"email": "new@example.com"})
    assert known.status_code == unknown.status_code == 202 and known.json() == unknown.json()
    raw = await latest_link(app, "new@example.com", "signup")
    assert (
        await client.post(
            "/api/v1/auth/recovery/complete", json={"token": raw, "password": PASSWORD}
        )
    ).status_code == 400
    async with app.state.db() as db, db.begin():
        await db.execute(update(EmailChallenge).values(expires=now() - timedelta(seconds=1)))
    assert (
        await client.post(
            "/api/v1/auth/signup/complete",
            json={"token": raw, "password": PASSWORD, "organization": "Expired"},
        )
    ).status_code == 400
    async with app.state.db() as db:
        assert await db.scalar(select(Organization).where(Organization.name == "Expired")) is None


@pytest.mark.anyio
async def test_unverified_accounts_cannot_recover_and_verification_requires_password(setup):
    from app.auth.models import MailOutbox

    app, client = setup
    response = await client.post(
        "/api/v1/auth/recovery/request", json={"email": "owner0@example.com"}
    )
    unknown = await client.post(
        "/api/v1/auth/recovery/request", json={"email": "missing@example.com"}
    )
    assert response.json() == unknown.json()
    async with app.state.db() as db:
        assert not list(await db.scalars(select(MailOutbox)))
    await login(client)
    assert (
        await client.post("/api/v1/auth/email/request", json={"password": "wrong"})
    ).status_code == 400
    assert (
        await client.post("/api/v1/auth/mfa/enroll", json={"password": PASSWORD})
    ).status_code == 400
    await verify_owner(app, client)
    assert (await me(client)).json()["email_verified"] is True


@pytest.mark.anyio
async def test_recovery_revokes_all_sessions_and_all_outstanding_links(setup):
    app, client = setup
    await verify_owner(app, client)
    old_cookie = client.cookies["__Host-pos_refresh"]
    for _ in range(2):
        await client.post("/api/v1/auth/recovery/request", json={"email": "owner0@example.com"})
    raw = await latest_link(app, "owner0@example.com", "reset")
    payload = {"token": raw, "password": PASSWORD + " reset"}
    responses = await asyncio.gather(
        *[client.post("/api/v1/auth/recovery/complete", json=payload) for _ in range(2)]
    )
    assert sorted(r.status_code for r in responses) == [204, 400]
    assert (await me(client)).status_code == 401
    assert (
        await client.post(
            "/api/v1/auth/refresh", json={}, headers={"Cookie": f"__Host-pos_refresh={old_cookie}"}
        )
    ).status_code == 401
    assert (await login(client)).status_code == 401
    assert (await login(client, password=PASSWORD + " reset")).status_code == 204


@pytest.mark.anyio
async def test_password_change_invalidates_issued_recovery_and_verification_links(setup):
    app, client = setup
    await verify_owner(app, client)
    await client.post("/api/v1/auth/recovery/request", json={"email": "owner0@example.com"})
    raw = await latest_link(app, "owner0@example.com", "reset")
    assert (
        await client.post(
            "/api/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": PASSWORD + " changed"},
        )
    ).status_code == 204
    assert (
        await client.post(
            "/api/v1/auth/recovery/complete", json={"token": raw, "password": PASSWORD + " stolen"}
        )
    ).status_code == 400


@pytest.mark.anyio
async def test_mfa_enrollment_login_replay_and_encrypted_storage(setup):
    app, client = setup
    secret, backups, used_code = await enable_mfa(app, client)
    assert len(backups) == len(set(backups)) == 10
    assert (await me(client)).status_code == 401
    assert (await login(client)).status_code == 401
    async with app.state.db() as db:
        user = await db.scalar(select(User).where(User.email == "owner0@example.com"))
        assert user.mfa_secret != secret and secret not in user.mfa_secret
        assert user.mfa_pending is None
        assert backups[0] not in user.recovery_hashes
        assert digest(backups[0]) in user.recovery_hashes
    for code in [used_code, "invalid"]:
        assert (
            await client.post(
                "/api/v1/auth/login",
                json={"email": "owner0@example.com", "password": PASSWORD, "code": code},
            )
        ).status_code == 401
    payload = {"email": "owner0@example.com", "password": PASSWORD, "code": backups[0]}
    results = await asyncio.gather(
        *[client.post("/api/v1/auth/login", json=payload) for _ in range(2)]
    )
    assert sorted(r.status_code for r in results) == [204, 401]
    assert (await me(client)).json()["mfa_enabled"] is True


@pytest.mark.anyio
async def test_mfa_recovery_requires_factor_and_preserves_mfa(setup):
    app, client = setup
    _, backups, _ = await enable_mfa(app, client)
    await client.post("/api/v1/auth/recovery/request", json={"email": "owner0@example.com"})
    raw = await latest_link(app, "owner0@example.com", "reset")
    payload = {"token": raw, "password": PASSWORD + " reset"}
    assert (await client.post("/api/v1/auth/recovery/complete", json=payload)).status_code == 400
    assert (
        await client.post("/api/v1/auth/recovery/complete", json={**payload, "code": backups[0]})
    ).status_code == 204
    assert (await login(client, password=PASSWORD + " reset")).status_code == 401
    assert (
        await client.post(
            "/api/v1/auth/login",
            json={
                "email": "owner0@example.com",
                "password": PASSWORD + " reset",
                "code": backups[1],
            },
        )
    ).status_code == 204
    assert (await me(client)).json()["mfa_enabled"] is True


@pytest.mark.anyio
async def test_mfa_disable_and_code_replacement_require_reauthentication(setup):
    app, client = setup
    _, backups, _ = await enable_mfa(app, client)
    await client.post(
        "/api/v1/auth/login",
        json={"email": "owner0@example.com", "password": PASSWORD, "code": backups[0]},
    )
    for path in ["mfa/disable", "mfa/recovery-codes", "password"]:
        body = (
            {"password": PASSWORD}
            if path != "password"
            else {"current_password": PASSWORD, "new_password": PASSWORD + " new"}
        )
        assert (await client.post("/api/v1/auth/" + path, json=body)).status_code == 400
    response = await client.post(
        "/api/v1/auth/mfa/recovery-codes", json={"password": PASSWORD, "code": backups[1]}
    )
    assert response.status_code == 200
    replacement = response.json()["recovery_codes"]
    assert (
        await client.post(
            "/api/v1/auth/mfa/disable", json={"password": PASSWORD, "code": backups[2]}
        )
    ).status_code == 400
    assert (
        await client.post(
            "/api/v1/auth/mfa/disable", json={"password": PASSWORD, "code": replacement[0]}
        )
    ).status_code == 204
    assert (await me(client)).status_code == 401
    assert (await login(client)).status_code == 204
    assert (await me(client)).json()["mfa_enabled"] is False


@pytest.mark.anyio
async def test_expired_enrollment_and_incorrect_code_do_not_enable_mfa(setup):
    import pyotp

    app, client = setup
    await verify_owner(app, client)
    secret = (await client.post("/api/v1/auth/mfa/enroll", json={"password": PASSWORD})).json()[
        "secret"
    ]
    assert (
        await client.post("/api/v1/auth/mfa/confirm", json={"code": "invalid"})
    ).status_code == 400
    assert (await me(client)).json()["mfa_enabled"] is False
    async with app.state.db() as db, db.begin():
        await db.execute(update(User).values(mfa_pending_expires=now() - timedelta(seconds=1)))
    assert (
        await client.post("/api/v1/auth/mfa/confirm", json={"code": pyotp.TOTP(secret).now()})
    ).status_code == 400


@pytest.mark.anyio
async def test_email_and_factor_throttles_csrf_and_input_redaction(setup):
    app, client = setup
    response = await client.post(
        "/api/v1/auth/signup/request",
        json={"email": "test@example.com"},
        headers={"Origin": "https://evil.test"},
    )
    assert response.status_code == 403
    raw = "sensitive-token" * 30
    response = await client.post(
        "/api/v1/auth/recovery/complete", json={"token": raw, "password": PASSWORD}
    )
    assert response.status_code == 422 and raw not in response.text
    await client.post("/api/v1/auth/signup/request", json={"email": "test@example.com"})
    async with app.state.db() as db, db.begin():
        await db.execute(update(RateBucket).values(count=120))
    assert (
        await client.post("/api/v1/auth/signup/request", json={"email": "test@example.com"})
    ).status_code == 429


@pytest.mark.anyio
async def test_mail_outbox_delivery_retry_and_deletion(setup):
    from app.auth.mail import deliver_one
    from app.auth.models import MailOutbox

    app, client = setup
    await client.post("/api/v1/auth/signup/request", json={"email": "test@example.com"})
    delivered = []

    def failing(settings, payload):
        raise OSError("Private SMTP failure")

    assert await deliver_one(app.state.db, app.state.settings, failing)
    async with app.state.db() as db, db.begin():
        row = await db.scalar(select(MailOutbox))
        assert row.attempts == 1 and row.next_attempt > now()
        row.next_attempt = now() - timedelta(seconds=1)
    assert await deliver_one(
        app.state.db, app.state.settings, lambda settings, payload: delivered.append(payload)
    )
    assert delivered[0]["email"] == "test@example.com"
    async with app.state.db() as db:
        assert not list(await db.scalars(select(MailOutbox)))


@pytest.mark.anyio
async def test_concurrent_totp_and_non_ascii_input(setup):
    import pyotp

    app, client = setup
    secret, _, _ = await enable_mfa(app, client)
    payload = {"email": "owner0@example.com", "password": PASSWORD, "code": "१२३४५६"}
    assert (await client.post("/api/v1/auth/login", json=payload)).status_code == 401
    # The adjacent future step is allowed for clock drift, but it too is one-use.
    payload["code"] = pyotp.TOTP(secret).at(now() + timedelta(seconds=30))
    responses = await asyncio.gather(
        *[client.post("/api/v1/auth/login", json=payload) for _ in range(2)]
    )
    assert sorted(r.status_code for r in responses) == [204, 401]


@pytest.mark.anyio
async def test_recovery_racing_refresh_cannot_leave_valid_session(setup):
    app, client = setup
    await verify_owner(app, client)
    saved = client.cookies["__Host-pos_refresh"]
    await client.post("/api/v1/auth/recovery/request", json={"email": "owner0@example.com"})
    raw = await latest_link(app, "owner0@example.com", "reset")
    reset, refresh = await asyncio.gather(
        client.post(
            "/api/v1/auth/recovery/complete", json={"token": raw, "password": PASSWORD + " reset"}
        ),
        client.post(
            "/api/v1/auth/refresh", json={}, headers={"Cookie": f"__Host-pos_refresh={saved}"}
        ),
    )
    assert reset.status_code == 204
    assert refresh.status_code in {204, 401}
    async with app.state.db() as db:
        assert not list(await db.scalars(select(Session).where(Session.revoked.is_(False))))


@pytest.mark.anyio
async def test_email_and_mfa_capabilities_fail_closed_without_configuration(setup):
    app, client = setup
    app.state.settings = app.state.settings.model_copy(
        update={
            "smtp_host": None,
            "identity_encryption_key": None,
        }
    )
    assert (await client.get("/api/v1/auth/capabilities")).json() == {"email": False, "mfa": False}
    assert (
        await client.post("/api/v1/auth/signup/request", json={"email": "new@example.com"})
    ).status_code == 503
    assert (
        await client.post("/api/v1/auth/recovery/request", json={"email": "owner0@example.com"})
    ).status_code == 503


@pytest.mark.anyio
async def test_outbox_sends_to_local_smtp_capture(setup):
    import socketserver
    import threading

    from app.auth.mail import deliver_one

    app, client = setup
    await client.post("/api/v1/auth/signup/request", json={"email": "smtp@example.com"})
    messages = []

    class Capture(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b"220 local-test\r\n")
            while line := self.rfile.readline():
                command = line.split(b" ", 1)[0].strip().upper()
                if command == b"DATA":
                    self.wfile.write(b"354 continue\r\n")
                    body = []
                    while (part := self.rfile.readline()) not in (b".\r\n", b""):
                        body.append(part)
                    messages.append(b"".join(body))
                    self.wfile.write(b"250 captured\r\n")
                elif command == b"QUIT":
                    self.wfile.write(b"221 bye\r\n")
                    return
                else:
                    self.wfile.write(b"250 OK\r\n")

    with socketserver.TCPServer(("127.0.0.1", 0), Capture) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            settings = app.state.settings.model_copy(
                update={
                    "environment": "development",
                    "smtp_host": "127.0.0.1",
                    "smtp_port": server.server_address[1],
                    "smtp_starttls": False,
                }
            )
            assert await deliver_one(app.state.db, settings)
        finally:
            server.shutdown()
            thread.join(timeout=2)
    assert len(messages) == 1
    assert b"To: smtp@example.com" in messages[0]
    assert b"#identity=3Dsignup" in messages[0] or b"#identity=signup" in messages[0]
