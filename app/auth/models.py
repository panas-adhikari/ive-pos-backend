from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from uuid6 import uuid7


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    full_name: Mapped[str] = mapped_column(String(160), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    job_title: Mapped[str] = mapped_column(String(100), default="")
    password_hash: Mapped[str] = mapped_column(String(512))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Platform access is deliberately separate from a tenant membership.
    # A platform user may support many organizations without belonging to any of them.
    platform_role: Mapped[str] = mapped_column(String(32), default="none")
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    require_action_verification: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_secret: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mfa_pending: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mfa_pending_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mfa_last_step: Mapped[int] = mapped_column(Integer, default=-1)
    recovery_hashes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    slug: Mapped[str] = mapped_column(String(63), unique=True, default=lambda: f"org-{uuid7().hex}")
    name: Mapped[str] = mapped_column(String(160))
    contact_email: Mapped[str] = mapped_column(String(254), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    organization_type: Mapped[str] = mapped_column(String(30), default="retail")
    image_url: Mapped[str] = mapped_column(String(1000), default="")
    website_url: Mapped[str] = mapped_column(String(300), default="")
    location_label: Mapped[str] = mapped_column(String(240), default="")
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="NPR")
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Kathmandu")
    receipt_footer: Mapped[str] = mapped_column(String(500), default="")
    configured: Mapped[bool] = mapped_column(Boolean, default=False)
    billing_plan: Mapped[str | None] = mapped_column(String(80), nullable=True)
    billing_amount_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    billing_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    billing_interval: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Platform-managed commercial limits until package plans are introduced.
    store_limit: Mapped[int] = mapped_column(Integer, default=1)
    employee_limit: Mapped[int] = mapped_column(Integer, default=5)
    deletion_scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "organization_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list)
    roles: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=lambda: ["custom"])
    all_stores: Mapped[bool] = mapped_column(Boolean, default=True)
    store_ids: Mapped[list[UUID]] = mapped_column(ARRAY(PG_UUID(as_uuid=True)), default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Session(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    # Historical scope survives organization deletion; it must never become a root session.
    tenant_id: Mapped[UUID | None] = mapped_column(nullable=True)
    access_hash: Mapped[str] = mapped_column(String(64), unique=True)
    access_expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idle_expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    step_up_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RefreshToken(Base):
    __tablename__ = "auth_refresh_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("auth_sessions.id"), index=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class RateBucket(Base):
    __tablename__ = "auth_rate_buckets"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window: Mapped[int] = mapped_column(Integer, primary_key=True)
    count: Mapped[int] = mapped_column(Integer)


class AuditEvent(Base):
    __tablename__ = "auth_audit_events"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    organization_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True
    )
    session_id: Mapped[UUID | None] = mapped_column(ForeignKey("auth_sessions.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    target_type: Mapped[str | None] = mapped_column(String(30))
    target_id: Mapped[UUID | None] = mapped_column(nullable=True)
    changes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EmailChallenge(Base):
    __tablename__ = "auth_email_challenges"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), index=True)
    purpose: Mapped[str] = mapped_column(String(20))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    credential_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class MailOutbox(Base):
    __tablename__ = "auth_mail_outbox"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    payload: Mapped[str] = mapped_column(String(8192))
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (UniqueConstraint("organization_id", "email"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    invited_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    email: Mapped[str] = mapped_column(String(254))
    kind: Mapped[str] = mapped_column(String(20))
    access: Mapped[dict] = mapped_column(JSONB)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="pending")


class PlatformPayment(Base):
    __tablename__ = "platform_payments"
    __table_args__ = (
        UniqueConstraint("organization_id", "client_key", name="uq_platform_payment_client"),
        CheckConstraint("amount_minor > 0", name="ck_platform_payment_amount"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    client_key: Mapped[UUID] = mapped_column()
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    paid_on: Mapped[date] = mapped_column(Date)
    method: Mapped[str] = mapped_column(String(20))
    reference: Mapped[str] = mapped_column(String(120), default="")
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(String(240), nullable=True)
