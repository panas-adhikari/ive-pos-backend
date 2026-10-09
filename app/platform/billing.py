"""Platform billing records: commercial terms and received-payment entries, not charges."""

from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from app.auth import service
from app.auth.models import Organization, PlatformPayment
from app.auth.security import now
from app.platform.routes import require_step_up, require_super_admin
from app.stores.routes import CURRENCIES

router = APIRouter(prefix="/api/v1/platform/organizations", tags=["Platform billing"])


class CurrencyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def supported_currency(cls, value):
        value = value.upper()
        if value not in CURRENCIES:
            raise ValueError("Unsupported currency")
        return value


class BillingTerms(CurrencyInput):
    plan: str = Field(min_length=1, max_length=80)
    amount_minor: int = Field(ge=0, le=2_000_000_000, strict=True)
    interval: Literal["monthly", "yearly"]
    expected_version: int = Field(ge=1, strict=True)


class PaymentInput(CurrencyInput):
    amount_minor: int = Field(gt=0, le=2_000_000_000, strict=True)
    paid_on: date
    method: Literal["bank_transfer", "cash", "wallet", "other"]
    reference: str = Field(default="", max_length=120)
    client_key: UUID

    @field_validator("paid_on")
    @classmethod
    def received_date(cls, value):
        if value > now().date():
            raise ValueError("Received payments cannot be dated in the future")
        return value


class VoidInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=240)


def payment_record(payment):
    return {
        "id": payment.id,
        "amount_minor": payment.amount_minor,
        "currency": payment.currency,
        "paid_on": payment.paid_on,
        "method": payment.method,
        "reference": payment.reference,
        "created": payment.created,
        "voided_at": payment.voided_at,
        "void_reason": payment.void_reason,
    }


async def locked_organization(db, organization_id):
    organization = await db.scalar(
        select(Organization).where(Organization.id == organization_id).with_for_update()
    )
    if not organization:
        raise HTTPException(404, "Organization not found")
    return organization


@router.get("/{organization_id}/billing")
async def billing(organization_id: UUID, request: Request):
    await service.require_platform_role(request, "super_admin", "employee")
    async with request.app.state.db() as db:
        if not await db.get(Organization, organization_id):
            raise HTTPException(404, "Organization not found")
        payments = list(
            await db.scalars(
                select(PlatformPayment)
                .where(PlatformPayment.organization_id == organization_id)
                .order_by(
                    PlatformPayment.paid_on.desc(),
                    PlatformPayment.created.desc(),
                    PlatformPayment.id.desc(),
                )
                .limit(50)
            )
        )
        totals = (
            await db.execute(
                select(PlatformPayment.currency, func.sum(PlatformPayment.amount_minor))
                .where(
                    PlatformPayment.organization_id == organization_id,
                    PlatformPayment.voided_at.is_(None),
                )
                .group_by(PlatformPayment.currency)
                .order_by(PlatformPayment.currency)
            )
        ).all()
        return {
            "payments": [payment_record(payment) for payment in payments],
            "recorded_totals": [
                {"currency": currency, "amount_minor": total} for currency, total in totals
            ],
        }


@router.put("/{organization_id}/billing")
async def update_billing(body: BillingTerms, organization_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        actor, session = await service.locked_identity(request, db)
        require_super_admin(actor)
        require_step_up(actor, session)
        organization = await locked_organization(db, organization_id)
        if organization.deletion_scheduled_for:
            raise HTTPException(409, "Cancel scheduled deletion before changing billing")
        if organization.version != body.expected_version:
            raise HTTPException(409, "Organization changed. Refresh before saving billing terms.")
        before = {
            key: getattr(organization, f"billing_{key}")
            for key in ["plan", "amount_minor", "currency", "interval"]
        }
        organization.billing_plan = body.plan
        organization.billing_amount_minor = body.amount_minor
        organization.billing_currency = body.currency
        organization.billing_interval = body.interval
        organization.version += 1
        service.audit(
            db,
            "platform.billing_terms_updated",
            actor.id,
            session.id,
            organization_id=organization_id,
            target_type="organizations",
            target_id=organization_id,
            changes={"before": before, "after": body.model_dump(exclude={"expected_version"})},
        )
        return {"status": "saved", "version": organization.version}


@router.post("/{organization_id}/payments")
async def record_payment(body: PaymentInput, organization_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        actor, session = await service.locked_identity(request, db)
        require_super_admin(actor)
        require_step_up(actor, session)
        organization = await locked_organization(db, organization_id)
        existing = await db.scalar(
            select(PlatformPayment).where(
                PlatformPayment.organization_id == organization_id,
                PlatformPayment.client_key == body.client_key,
            )
        )
        if existing:
            if any(getattr(existing, key) != value for key, value in body.model_dump().items()):
                raise HTTPException(409, "Payment retry differs from the original entry")
            return payment_record(existing)
        if organization.deletion_scheduled_for:
            raise HTTPException(409, "Cancel scheduled deletion before recording payments")
        payment = PlatformPayment(
            organization_id=organization_id, actor_id=actor.id, created=now(), **body.model_dump()
        )
        db.add(payment)
        await db.flush()
        service.audit(
            db,
            "platform.payment_recorded",
            actor.id,
            session.id,
            organization_id=organization_id,
            target_type="platform_payments",
            target_id=payment.id,
            changes=body.model_dump(mode="json", exclude={"client_key"}),
        )
        return payment_record(payment)


@router.post("/{organization_id}/payments/{payment_id}/void")
async def void_payment(body: VoidInput, organization_id: UUID, payment_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        actor, session = await service.locked_identity(request, db)
        require_super_admin(actor)
        require_step_up(actor, session)
        await locked_organization(db, organization_id)
        payment = await db.scalar(
            select(PlatformPayment)
            .where(
                PlatformPayment.id == payment_id, PlatformPayment.organization_id == organization_id
            )
            .with_for_update()
        )
        if not payment:
            raise HTTPException(404, "Payment not found")
        if payment.voided_at is None:
            payment.voided_at = now()
            payment.void_reason = body.reason
            service.audit(
                db,
                "platform.payment_voided",
                actor.id,
                session.id,
                organization_id=organization_id,
                target_type="platform_payments",
                target_id=payment_id,
                changes={"reason": body.reason},
            )
        return payment_record(payment)
