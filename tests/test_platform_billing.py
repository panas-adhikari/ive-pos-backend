from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.auth.models import AuditEvent, Organization, PlatformPayment, Session, User
from app.auth.security import now
from tests.test_auth import anyio_backend as anyio_backend
from tests.test_auth import setup as setup
from tests.test_platform import make_platform_admin
from tests.test_stores import configured as configured
from tests.test_stores import owner as owner


def payment(**changes):
    return {
        "amount_minor": 250000,
        "currency": "NPR",
        "paid_on": now().date().isoformat(),
        "method": "bank_transfer",
        "reference": "Transfer 123",
        "client_key": str(uuid4()),
        **changes,
    }


async def target(app):
    async with app.state.db() as db:
        org = await db.scalar(select(Organization).where(Organization.name == "Store 1"))
        return str(org.id), org.version


@pytest.mark.anyio
async def test_billing_read_and_write_role_boundaries(configured):
    app, client, _ = configured
    org_id, version = await target(app)
    path = f"/api/v1/platform/organizations/{org_id}"
    body = {
        "plan": "Retail",
        "currency": "NPR",
        "amount_minor": 250000,
        "interval": "monthly",
        "expected_version": version,
    }
    assert (await client.get(path + "/billing")).status_code == 403
    assert (await client.put(path + "/billing", json=body)).status_code == 403
    await make_platform_admin(app)
    assert (await client.put(path + "/billing", json=body)).status_code == 200
    async with app.state.db() as db, db.begin():
        user = await db.scalar(select(User).where(User.email == "owner0@example.com"))
        user.platform_role = "employee"
    assert (await client.get(path + "/billing")).status_code == 200
    assert (await client.get(path)).json()["billing_amount_minor"] == 250000
    assert (await client.put(path + "/billing", json=body)).status_code == 403
    assert (await client.post(path + "/payments", json=payment())).status_code == 403


@pytest.mark.anyio
async def test_billing_version_validation_and_audit(configured):
    app, client, _ = configured
    await make_platform_admin(app)
    org_id, version = await target(app)
    path = f"/api/v1/platform/organizations/{org_id}/billing"
    body = {
        "plan": "Retail",
        "currency": "npr",
        "amount_minor": 250000,
        "interval": "monthly",
        "expected_version": version,
    }
    saved = await client.put(path, json=body)
    assert saved.status_code == 200 and saved.json()["version"] == version + 1
    assert (await client.put(path, json=body)).status_code == 409
    for invalid in [
        {"currency": "BAD"},
        {"amount_minor": -1},
        {"amount_minor": 1.5},
        {"plan": "   "},
        {"interval": "weekly"},
    ]:
        assert (await client.put(path, json={**body, **invalid})).status_code == 422
    async with app.state.db() as db:
        audit = await db.scalar(
            select(AuditEvent).where(AuditEvent.action == "platform.billing_terms_updated")
        )
        assert audit.changes["after"]["currency"] == "NPR"
        assert audit.changes["after"]["amount_minor"] == 250000


@pytest.mark.anyio
async def test_received_payments_retry_void_and_currency_totals(configured):
    app, client, _ = configured
    await make_platform_admin(app)
    org_id, _ = await target(app)
    path = f"/api/v1/platform/organizations/{org_id}"
    body = payment()
    response = await client.post(path + "/payments", json=body)
    assert response.status_code == 200
    payment_id = response.json()["id"]
    retry = await client.post(path + "/payments", json=body)
    assert retry.json()["id"] == payment_id
    conflict = await client.post(path + "/payments", json={**body, "amount_minor": 100})
    assert conflict.status_code == 409
    assert (
        await client.post(path + "/payments", json=payment(currency="USD", amount_minor=2000))
    ).status_code == 200
    totals = (await client.get(path + "/billing")).json()["recorded_totals"]
    assert totals == [
        {"currency": "NPR", "amount_minor": 250000},
        {"currency": "USD", "amount_minor": 2000},
    ]
    void = await client.post(
        path + f"/payments/{payment_id}/void", json={"reason": "Duplicate receipt"}
    )
    assert void.status_code == 200 and void.json()["voided_at"]
    assert (
        await client.post(path + f"/payments/{payment_id}/void", json={"reason": "Retry"})
    ).status_code == 200
    result = (await client.get(path + "/billing")).json()
    assert len(result["payments"]) == 2
    assert result["recorded_totals"] == [{"currency": "USD", "amount_minor": 2000}]
    async with app.state.db() as db:
        audits = list(
            await db.scalars(
                select(AuditEvent).where(AuditEvent.action == "platform.payment_voided")
            )
        )
        assert len(audits) == 1
        assert (await db.get(PlatformPayment, UUID(payment_id))).void_reason == "Duplicate receipt"


@pytest.mark.anyio
async def test_billing_requires_verified_session_and_valid_received_payment(configured):
    app, client, _ = configured
    await make_platform_admin(app)
    org_id, _ = await target(app)
    path = f"/api/v1/platform/organizations/{org_id}/payments"
    for invalid in [
        {"amount_minor": 0},
        {"amount_minor": -1},
        {"amount_minor": True},
        {"paid_on": (now().date() + timedelta(days=1)).isoformat()},
        {"method": "credit"},
        {"currency": "BAD"},
    ]:
        assert (await client.post(path, json=payment(**invalid))).status_code == 422
    async with app.state.db() as db, db.begin():
        user = await db.scalar(select(User).where(User.email == "owner0@example.com"))
        sessions = list(await db.scalars(select(Session).where(Session.user_id == user.id)))
        for session in sessions:
            session.step_up_expires = now() - timedelta(seconds=1)
    assert (await client.post(path, json=payment())).status_code == 403


@pytest.mark.anyio
async def test_payment_scope_and_deletion_block(configured):
    app, client, own_path = configured
    await make_platform_admin(app)
    org_id, version = await target(app)
    path = f"/api/v1/platform/organizations/{org_id}"
    response = await client.post(path + "/payments", json=payment())
    payment_id = response.json()["id"]
    other = own_path.split("/")[-1]
    assert (
        await client.post(
            f"/api/v1/platform/organizations/{other}/payments/{payment_id}/void",
            json={"reason": "Wrong organization"},
        )
    ).status_code == 404
    await client.request("DELETE", path, json={"confirm_name": "Store 1"})
    assert (await client.post(path + "/payments", json=payment())).status_code == 409
    body = {
        "plan": "Retail",
        "currency": "NPR",
        "amount_minor": 250000,
        "interval": "monthly",
        "expected_version": version,
    }
    assert (await client.put(path + "/billing", json=body)).status_code == 409
    app.state.settings.environment = "development"
    assert (
        await client.request("DELETE", path, json={"confirm_name": "Store 1"})
    ).status_code == 200
    async with app.state.db() as db:
        assert await db.get(PlatformPayment, UUID(payment_id)) is None
