import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from app.auth import service
from app.auth.factors import cipher
from app.auth.models import AuditEvent, Membership, Organization, Session, User
from app.auth.permissions import OWNER_PERMISSIONS
from app.auth.security import digest, now
from app.stores.models import Register, Store
from tests.test_auth import PASSWORD
from tests.test_auth import anyio_backend as anyio_backend
from tests.test_auth import setup as setup

STORE = {
    "name": "Main store",
    "code": "MAIN",
    "timezone": "Asia/Kathmandu",
    "address": "Market Road",
    "phone": "01-5550000",
    "contact_email": "shop@example.com",
    "opening_hours": "Sun–Fri, 09:00–19:00",
    "receipt_name": "Main shop",
    "receipt_footer": "Thank you",
}
BUSINESS = {
    "name": "Retail business",
    "timezone": "Asia/Kathmandu",
    "currency": "NPR",
    "expected_version": 1,
}
CODES = [f"{n:020x}" for n in range(1, 11)]


@pytest.fixture
async def owner(setup):
    app, client = setup
    async with app.state.db() as db, db.begin():
        user = await db.scalar(select(User).where(User.email == "owner0@example.com"))
        user.email_verified = True
        # These tests cover the optional repeated-verification policy.
        user.require_action_verification = True
        user.mfa_secret = cipher(app.state.settings).encrypt(b"JBSWY3DPEHPK3PXP").decode()
        user.recovery_hashes = [digest(code) for code in CODES]
        membership = await db.scalar(select(Membership).where(Membership.user_id == user.id))
        membership.permissions = list(OWNER_PERMISSIONS)
        org_id = str(membership.organization_id)
        # These fixtures exercise multiple stores, independently of default plan limits.
        organization = await db.get(Organization, membership.organization_id)
        organization.store_limit = 3
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "owner0@example.com",
            "password": PASSWORD,
            "code": CODES[0],
        },
    )
    assert response.status_code == 204
    return app, client, f"/api/v1/organizations/{org_id}"


async def unlock(client):
    response = await client.post(
        "/api/v1/auth/step-up", json={"password": PASSWORD, "code": CODES[1]}
    )
    assert response.status_code == 200


@pytest.fixture
async def configured(owner):
    app, client, base = owner
    await unlock(client)
    assert (await client.put(base + "/settings", json=BUSINESS)).status_code == 200
    return app, client, base


async def store_and_register(client, base):
    store = await client.post(base + "/stores", json=STORE)
    assert store.status_code == 201
    store = store.json()
    register = await client.post(
        f"{base}/stores/{store['id']}/registers", json={"name": "Front counter", "code": "POS1"}
    )
    assert register.status_code == 201
    return store, register.json()


@pytest.mark.anyio
async def test_setup_requires_explicit_permission_and_recent_mfa(owner):
    app, client, base = owner
    assert (await client.get(base + "/setup")).status_code == 200
    assert (await client.put(base + "/settings", json=BUSINESS)).status_code == 403
    assert (
        await client.post("/api/v1/auth/step-up", json={"password": PASSWORD})
    ).status_code == 400
    await unlock(client)
    assert (await client.put(base + "/settings", json=BUSINESS)).status_code == 200
    async with app.state.db() as db, db.begin():
        await db.execute(update(Session).values(step_up_expires=now() - timedelta(seconds=1)))
    assert (await client.post(base + "/stores", json=STORE)).status_code == 403
    async with app.state.db() as db, db.begin():
        await db.execute(update(Membership).values(permissions=["organization.read"]))
    assert (await client.get(base + "/setup")).status_code == 403


@pytest.mark.anyio
async def test_business_settings_before_first_store_and_validation(owner):
    _, client, base = owner
    await unlock(client)
    assert (await client.post(base + "/stores", json=STORE)).status_code == 409
    for invalid in (
        {"currency": "XYZ"},
        {"timezone": "Not/AZone"},
        {"name": "   "},
        {"contact_email": "invalid"},
    ):
        assert (
            await client.put(base + "/settings", json={**BUSINESS, **invalid})
        ).status_code == 422
    response = await client.put(
        base + "/settings", json={**BUSINESS, "phone": "01-5550000", "receipt_footer": "Thanks"}
    )
    assert response.status_code == 200
    assert response.json()["configured"] and response.json()["version"] == 2
    assert (await client.put(base + "/settings", json=BUSINESS)).status_code == 409


@pytest.mark.anyio
async def test_store_register_create_read_edit_and_audit(configured):
    app, client, base = configured
    store, register = await store_and_register(client, base)
    info = (await client.get(base + "/setup")).json()
    assert info["stores"][0]["registers"][0]["id"] == register["id"]
    assert info["stores"][0]["opening_hours"] == STORE["opening_hours"]
    body = {k: v for k, v in STORE.items() if k != "code"}
    body.update(name="Updated main", expected_version=store["version"])
    response = await client.put(f"{base}/stores/{store['id']}", json=body)
    assert response.status_code == 200 and response.json()["version"] == 2
    response = await client.put(
        f"{base}/stores/{store['id']}/registers/{register['id']}",
        json={"name": "Counter 2", "expected_version": 1},
    )
    assert response.status_code == 200 and response.json()["name"] == "Counter 2"
    async with app.state.db() as db:
        event = await db.scalar(select(AuditEvent).where(AuditEvent.action == "store.updated"))
        assert str(event.target_id) == store["id"] and event.target_type == "stores"
        assert event.changes["before"]["name"] == "Main store"
        assert event.changes["after"]["name"] == "Updated main"
        assert event.organization_id == UUID(base.rsplit("/", 1)[1])
        assert "password" not in str(event.changes)


@pytest.mark.anyio
async def test_cross_tenant_and_parent_resource_checks(configured):
    app, client, base = configured
    store, register = await store_and_register(client, base)
    async with app.state.db() as db, db.begin():
        other = await db.scalar(
            select(Organization).where(Organization.id != UUID(base.rsplit("/", 1)[1]))
        )
        other_id = str(other.id)
        foreign_store = Store(
            organization_id=other.id,
            code="MAIN",
            name="Other",
            timezone="UTC",
            receipt_name="Other",
        )
        db.add(foreign_store)
        await db.flush()
        foreign_id = str(foreign_store.id)
    other_base = f"/api/v1/organizations/{other_id}"
    assert (await client.get(other_base + "/setup")).status_code == 403
    assert (await client.post(other_base + "/stores", json=STORE)).status_code == 403
    status = {"active": False, "expected_version": 1}
    assert (await client.post(f"{base}/stores/{foreign_id}/status", json=status)).status_code == 404
    assert (
        await client.post(
            f"{base}/stores/{foreign_id}/registers", json={"name": "Bad", "code": "BAD"}
        )
    ).status_code == 404
    second = (await client.post(base + "/stores", json={**STORE, "code": "SECOND"})).json()
    assert (
        await client.post(
            f"{base}/stores/{second['id']}/registers/{register['id']}/status", json=status
        )
    ).status_code == 404
    assert len((await client.get(base + "/setup")).json()["stores"]) == 2
    assert (await client.post(f"{base}/stores/{uuid4()}/status", json=status)).status_code == 404


@pytest.mark.anyio
async def test_deactivation_preserves_history_and_registers_stay_inactive(configured):
    app, client, base = configured
    store, register = await store_and_register(client, base)
    path = f"{base}/stores/{store['id']}"
    response = await client.post(path + "/status", json={"active": False, "expected_version": 1})
    assert response.status_code == 200
    assert (
        await client.post(path + "/registers", json={"name": "New", "code": "NEW"})
    ).status_code == 409
    reg_path = path + "/registers/" + register["id"]
    assert (
        await client.post(reg_path + "/status", json={"active": True, "expected_version": 2})
    ).status_code == 409
    response = await client.post(path + "/status", json={"active": True, "expected_version": 2})
    assert response.status_code == 200
    info = (await client.get(base + "/setup")).json()["stores"][0]
    assert info["active"] is True and info["registers"][0]["active"] is False
    assert (
        await client.post(reg_path + "/status", json={"active": True, "expected_version": 2})
    ).status_code == 200
    async with app.state.db() as db:
        assert await db.get(Store, UUID(store["id"]))
        assert await db.get(Register, UUID(register["id"]))
        actions = list(await db.scalars(select(AuditEvent.action)))
        assert "store.deactivated" in actions and "register.deactivated" in actions


@pytest.mark.anyio
async def test_duplicate_codes_retries_and_concurrency(configured):
    _, client, base = configured
    responses = await asyncio.gather(
        *[client.post(base + "/stores", json={**STORE, "code": " main "}) for _ in range(2)]
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    store = next(r.json() for r in responses if r.status_code == 201)
    path = f"{base}/stores/{store['id']}/registers"
    responses = await asyncio.gather(
        *[client.post(path, json={"code": " pos1 ", "name": "Till"}) for _ in range(2)]
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    await client.post(
        f"{base}/stores/{store['id']}/status", json={"active": False, "expected_version": 1}
    )
    assert (await client.post(base + "/stores", json=STORE)).status_code == 409


@pytest.mark.anyio
async def test_concurrent_edits_reject_stale_versions(configured):
    _, client, base = configured
    store, _ = await store_and_register(client, base)
    body = {k: v for k, v in STORE.items() if k != "code"}
    responses = await asyncio.gather(
        *[
            client.put(
                f"{base}/stores/{store['id']}", json={**body, "name": name, "expected_version": 1}
            )
            for name in ["First edit", "Second edit"]
        ]
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    info = (await client.get(base + "/setup")).json()["stores"][0]
    assert info["version"] == 2


@pytest.mark.anyio
async def test_create_register_racing_store_deactivation(configured):
    _, client, base = configured
    store = (await client.post(base + "/stores", json=STORE)).json()
    path = f"{base}/stores/{store['id']}"
    created, disabled = await asyncio.gather(
        client.post(path + "/registers", json={"code": "POS1", "name": "Till"}),
        client.post(path + "/status", json={"active": False, "expected_version": 1}),
    )
    assert created.status_code in {201, 409} and disabled.status_code == 200
    info = (await client.get(base + "/setup")).json()["stores"][0]
    assert info["active"] is False
    assert all(not row["active"] for row in info["registers"])


@pytest.mark.anyio
async def test_audit_failure_rolls_back_store_creation(configured, monkeypatch):
    app, client, base = configured
    original = service.audit

    def reject(db, action, *args, **kwargs):
        if action == "store.created":
            raise RuntimeError("Injected audit failure")
        return original(db, action, *args, **kwargs)

    monkeypatch.setattr(service, "audit", reject)
    with pytest.raises(RuntimeError, match="Injected audit failure"):
        await client.post(base + "/stores", json=STORE)
    async with app.state.db() as db:
        assert not list(await db.scalars(select(Store)))


@pytest.mark.anyio
async def test_revoked_membership_and_session_prevent_setup(configured):
    app, client, base = configured
    async with app.state.db() as db, db.begin():
        await db.execute(update(Session).values(revoked=True))
    assert (await client.post(base + "/stores", json=STORE)).status_code == 401
    assert (await client.get(base + "/setup")).status_code == 401


@pytest.mark.anyio
async def test_setup_csrf_and_invalid_fields(configured):
    _, client, base = configured
    assert (
        await client.post(base + "/stores", json=STORE, headers={"Origin": "https://evil.test"})
    ).status_code == 403
    for change in [
        {"code": "bad/code"},
        {"name": "bad\x00name"},
        {"name": " "},
        {"timezone": "bogus"},
        {"organization_id": str(uuid4())},
    ]:
        assert (await client.post(base + "/stores", json={**STORE, **change})).status_code == 422
    store, register = await store_and_register(client, base)
    assert (
        await client.post(
            f"{base}/stores/{store['id']}/registers/{register['id']}/status",
            json={"active": "false", "expected_version": 1},
        )
    ).status_code == 422
