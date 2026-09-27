import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, update

from app.auth import service
from app.auth.models import AuditEvent, Membership, User
from tests.test_auth import HEADERS, ORIGIN, PASSWORD
from tests.test_auth import anyio_backend as anyio_backend
from tests.test_auth import setup as setup
from tests.test_stores import STORE, store_and_register, unlock
from tests.test_stores import configured as configured
from tests.test_stores import owner as owner

ACCESS = {
    "roles": ["cashier"],
    "permissions": ["organization.read", "store.read"],
    "all_stores": False,
    "store_ids": [],
    "active": True,
}


async def add_employee(app, client, base, **changes):
    async with app.state.db() as db, db.begin():
        await db.execute(
            update(User).where(User.email == "owner1@example.com").values(email_verified=True)
        )
    response = await client.post(
        base + "/employees", json={"email": " OWNER1@example.com ", **ACCESS, **changes}
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.anyio
async def test_employee_permission_step_up_and_input_validation(owner):
    app, client, base = owner
    assert (await client.get(base + "/employees")).status_code == 200
    body = {"email": "owner1@example.com", **ACCESS}
    assert (await client.post(base + "/employees", json=body)).status_code == 403
    await unlock(client)
    for invalid in (
        {"roles": ["bogus"]},
        {"permissions": ["sales.admin"]},
        {"permissions": ["organization.setup"]},
        {"store_ids": [str(uuid4())]},
        {"all_stores": "false"},
    ):
        assert (await client.post(base + "/employees", json={**body, **invalid})).status_code == 422
    assert (await client.post(base + "/employees", json=body)).status_code == 409
    await add_employee(app, client, base)
    assert (await client.post(base + "/employees", json=body)).status_code == 409
    async with app.state.db() as db, db.begin():
        await db.execute(
            update(Membership)
            .where(Membership.organization_id == UUID(base.rsplit("/", 1)[1]))
            .values(permissions=["organization.read"])
        )
    assert (await client.get(base + "/employees")).status_code == 403


@pytest.mark.anyio
async def test_scope_isolation_suspension_and_audit(configured):
    app, client, base = configured
    store, register = await store_and_register(client, base)
    await client.post(base + "/stores", json={**STORE, "code": "SECOND"})
    employee = await add_employee(app, client, base, store_ids=[store["id"]])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=HEADERS
    ) as worker:
        assert (
            await worker.post(
                "/api/v1/auth/login", json={"email": "owner1@example.com", "password": PASSWORD}
            )
        ).status_code == 204
        rows = (await worker.get(base + "/stores")).json()
        assert [r["id"] for r in rows] == [store["id"]]
        assert rows[0]["registers"][0]["id"] == register["id"]
        assert (await worker.get(base + "/employees")).status_code == 403
        assert (await worker.get(base + "/setup")).status_code == 403
        path = base + "/employees/" + employee["id"]
        response = await client.put(path, json={**ACCESS, "active": False, "expected_version": 1})
        assert response.status_code == 200
        assert (await worker.get(base + "/stores")).status_code == 403
        # Suspension is tenant-local: the other membership and session survive.
        assert (await worker.get("/api/v1/auth/me")).status_code == 200
        response = await client.put(path, json={**ACCESS, "expected_version": 2})
        assert response.status_code == 200
        assert (await worker.get(base + "/stores")).json() == []
    async with app.state.db() as db:
        event = await db.scalar(select(AuditEvent).where(AuditEvent.action == "membership.created"))
        assert event.changes["after"]["store_ids"] == [store["id"]]
        assert event.target_id == UUID(employee["id"])
        assert "password" not in str(event.changes)


@pytest.mark.anyio
async def test_cross_tenant_self_edit_and_escalation(configured):
    app, client, base = configured
    rows = (await client.get(base + "/employees")).json()["memberships"]
    assert (
        await client.put(
            base + "/employees/" + rows[0]["id"], json={**ACCESS, "expected_version": 1}
        )
    ).status_code == 409
    async with app.state.db() as db:
        other = await db.scalar(
            select(Membership).where(Membership.organization_id != UUID(base.rsplit("/", 1)[1]))
        )
        other_id, other_org = str(other.id), str(other.organization_id)
    assert (
        await client.put(base + "/employees/" + other_id, json={**ACCESS, "expected_version": 1})
    ).status_code == 404
    assert (await client.get(f"/api/v1/organizations/{other_org}/employees")).status_code == 403
    employee = await add_employee(app, client, base)
    async with app.state.db() as db, db.begin():
        await db.execute(
            update(Membership)
            .where(Membership.id == UUID(rows[0]["id"]))
            .values(permissions=["employees.manage"])
        )
    assert (
        await client.put(
            base + "/employees/" + employee["id"], json={**ACCESS, "expected_version": 1}
        )
    ).status_code == 403


@pytest.mark.anyio
async def test_membership_races_and_rollback(configured, monkeypatch):
    app, client, base = configured
    employee = await add_employee(app, client, base)
    path = base + "/employees/" + employee["id"]
    responses = await asyncio.gather(
        *[
            client.put(path, json={**ACCESS, "active": active, "expected_version": 1})
            for active in (True, False)
        ]
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    original = service.audit

    def fail(db, action, *args, **kwargs):
        if action == "membership.updated":
            raise RuntimeError("audit failed")
        return original(db, action, *args, **kwargs)

    monkeypatch.setattr(service, "audit", fail)
    with pytest.raises(RuntimeError, match="audit failed"):
        await client.put(path, json={**ACCESS, "expected_version": 2})
    async with app.state.db() as db:
        assert (await db.get(Membership, UUID(employee["id"]))).version == 2


@pytest.mark.anyio
async def test_foreign_store_scope_and_inactive_store(configured):
    from app.stores.models import Store

    app, client, base = configured
    store, _ = await store_and_register(client, base)
    async with app.state.db() as db, db.begin():
        other = await db.scalar(
            select(Membership).where(Membership.organization_id != UUID(base.rsplit("/", 1)[1]))
        )
        foreign = Store(
            organization_id=other.organization_id,
            code="OTHER",
            name="Other",
            timezone="UTC",
            receipt_name="Other",
        )
        db.add(foreign)
        await db.flush()
        foreign_id = str(foreign.id)
    assert (
        await client.post(
            base + "/employees",
            json={"email": "owner1@example.com", **ACCESS, "store_ids": [foreign_id]},
        )
    ).status_code == 422
    await add_employee(app, client, base, store_ids=[store["id"]])
    assert (
        await client.post(
            base + "/stores/" + store["id"] + "/status",
            json={"active": False, "expected_version": 1},
        )
    ).status_code == 200
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=HEADERS
    ) as worker:
        await worker.post(
            "/api/v1/auth/login", json={"email": "owner1@example.com", "password": PASSWORD}
        )
        assert (await worker.get(base + "/stores")).json() == []


@pytest.mark.anyio
async def test_csrf_expired_unlock_and_duplicate_creation(configured):
    from datetime import timedelta

    from app.auth.models import Session
    from app.auth.security import now

    app, client, base = configured
    body = {"email": "owner1@example.com", **ACCESS}
    assert (
        await client.post(base + "/employees", json=body, headers={"Origin": "https://evil.test"})
    ).status_code == 403
    async with app.state.db() as db, db.begin():
        await db.execute(
            update(User).where(User.email == "owner1@example.com").values(email_verified=True)
        )
    results = await asyncio.gather(*[client.post(base + "/employees", json=body) for _ in range(2)])
    assert sorted(r.status_code for r in results) == [201, 409]
    employee = next(r.json() for r in results if r.status_code == 201)
    async with app.state.db() as db, db.begin():
        await db.execute(update(Session).values(step_up_expires=now() - timedelta(seconds=1)))
    assert (
        await client.put(
            base + "/employees/" + employee["id"], json={**ACCESS, "expected_version": 1}
        )
    ).status_code == 403


@pytest.mark.anyio
async def test_concurrent_administrators_cannot_suspend_each_other(configured):
    from app.auth.factors import cipher
    from app.auth.permissions import OWNER_PERMISSIONS
    from app.auth.security import digest

    app, client, base = configured
    admin_access = {
        **ACCESS,
        "roles": ["administrator"],
        "permissions": list(OWNER_PERMISSIONS),
        "all_stores": True,
    }
    employee = await add_employee(app, client, base, **admin_access)
    owner_row = next(
        m
        for m in (await client.get(base + "/employees")).json()["memberships"]
        if m["email"] == "owner0@example.com"
    )
    async with app.state.db() as db, db.begin():
        user = await db.scalar(select(User).where(User.email == "owner1@example.com"))
        user.mfa_secret = cipher(app.state.settings).encrypt(b"JBSWY3DPEHPK3PXP").decode()
        user.recovery_hashes = [digest("11111111111111111111"), digest("22222222222222222222")]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=HEADERS
    ) as second:
        assert (
            await second.post(
                "/api/v1/auth/login",
                json={
                    "email": "owner1@example.com",
                    "password": PASSWORD,
                    "code": "11111111111111111111",
                },
            )
        ).status_code == 204
        assert (
            await second.post(
                "/api/v1/auth/step-up", json={"password": PASSWORD, "code": "22222222222222222222"}
            )
        ).status_code == 200
        results = await asyncio.gather(
            client.put(
                base + "/employees/" + employee["id"],
                json={**admin_access, "active": False, "expected_version": 1},
            ),
            second.put(
                base + "/employees/" + owner_row["id"],
                json={**admin_access, "active": False, "expected_version": 1},
            ),
        )
        assert sorted(r.status_code for r in results) in ([200, 403], [200, 401])
    async with app.state.db() as db:
        members = list(
            await db.scalars(
                select(Membership).where(Membership.organization_id == UUID(base.rsplit("/", 1)[1]))
            )
        )
        assert sum(m.active for m in members) == 1
