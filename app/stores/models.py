from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from uuid6 import uuid7

from app.auth.models import Base


class Store(Base):
    __tablename__ = "stores"
    __table_args__ = (
        UniqueConstraint("organization_id", "code", name="uq_store_org_code"),
        CheckConstraint("code ~ '^[A-Z0-9][A-Z0-9_-]{0,19}$'", name="ck_store_code"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_store_name"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(160))
    address: Mapped[str] = mapped_column(String(500), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    contact_email: Mapped[str] = mapped_column(String(254), default="")
    timezone: Mapped[str] = mapped_column(String(80))
    opening_hours: Mapped[str] = mapped_column(String(500), default="")
    receipt_name: Mapped[str] = mapped_column(String(160))
    receipt_footer: Mapped[str] = mapped_column(String(500), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    receipt_sequence: Mapped[int] = mapped_column(Integer, default=0)


class Register(Base):
    __tablename__ = "registers"
    __table_args__ = (
        UniqueConstraint("store_id", "code", name="uq_register_store_code"),
        CheckConstraint("code ~ '^[A-Z0-9][A-Z0-9_-]{0,19}$'", name="ck_register_code"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_register_name"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    store_id: Mapped[UUID] = mapped_column(ForeignKey("stores.id"), index=True)
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(160))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
