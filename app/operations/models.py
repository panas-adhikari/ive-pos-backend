from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from uuid6 import uuid7

from app.auth.models import Base


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("organization_id", "sku"),
        UniqueConstraint("organization_id", "barcode"),
        CheckConstraint("price_minor >= 0 AND cost_minor >= 0 AND low_stock_threshold >= 0"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    sku: Mapped[str] = mapped_column(String(60))
    barcode: Mapped[str | None] = mapped_column(String(80), nullable=True)
    category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    stock_unit: Mapped[str] = mapped_column(String(10), default="piece")
    presets: Mapped[list] = mapped_column(JSON, default=list)
    price_minor: Mapped[int] = mapped_column(Integer)
    cost_minor: Mapped[int] = mapped_column(Integer, default=0)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=5)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProductPriceHistory(Base):
    __tablename__ = "product_price_history"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), index=True)
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    old_price_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_price_minor: Mapped[int] = mapped_column(Integer)
    old_cost_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_cost_minor: Mapped[int] = mapped_column(Integer)
    old_presets: Mapped[list | None] = mapped_column(JSON, nullable=True)
    new_presets: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(String(30), default="catalog_update")
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StockBalance(Base):
    __tablename__ = "stock_balances"
    __table_args__ = (
        UniqueConstraint("store_id", "product_id"),
        CheckConstraint("quantity >= 0"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    store_id: Mapped[UUID] = mapped_column(ForeignKey("stores.id"), index=True)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)


class StockMovement(Base):
    __tablename__ = "stock_movements"
    __table_args__ = (CheckConstraint("delta <> 0"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    store_id: Mapped[UUID] = mapped_column(ForeignKey("stores.id"), index=True)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), index=True)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(30))
    delta: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(300), default="")
    sale_id: Mapped[UUID | None] = mapped_column(ForeignKey("sales.id"), nullable=True)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("organization_id", "phone"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Sale(Base):
    __tablename__ = "sales"
    __table_args__ = (
        UniqueConstraint("organization_id", "client_key"),
        UniqueConstraint("store_id", "receipt_number"),
        CheckConstraint(
            "subtotal_minor >= 0 AND discount_minor >= 0 AND total_minor >= 0 "
            "AND discount_minor <= subtotal_minor AND cash_received_minor >= total_minor"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    store_id: Mapped[UUID] = mapped_column(ForeignKey("stores.id"), index=True)
    register_id: Mapped[UUID] = mapped_column(ForeignKey("registers.id"))
    customer_id: Mapped[UUID | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    customer_type: Mapped[str] = mapped_column(String(20))
    client_key: Mapped[UUID] = mapped_column()
    request_hash: Mapped[str] = mapped_column(String(64))
    receipt_number: Mapped[str] = mapped_column(String(50))
    currency: Mapped[str] = mapped_column(String(3))
    subtotal_minor: Mapped[int] = mapped_column(Integer, default=0)
    discount_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    discount_value: Mapped[int] = mapped_column(Integer, default=0)
    discount_minor: Mapped[int] = mapped_column(Integer, default=0)
    total_minor: Mapped[int] = mapped_column(Integer)
    cash_received_minor: Mapped[int] = mapped_column(Integer)
    payment_method: Mapped[str] = mapped_column(String(20), default="cash")
    status: Mapped[str] = mapped_column(String(20), default="paid")
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SaleLine(Base):
    __tablename__ = "sale_lines"
    __table_args__ = (
        CheckConstraint(
            "quantity > 0 AND catalog_unit_price_minor >= 0 AND unit_price_minor >= 0 "
            "AND line_subtotal_minor >= 0 AND discount_minor >= 0 "
            "AND discount_minor <= line_subtotal_minor AND line_total_minor >= 0"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    sale_id: Mapped[UUID] = mapped_column(ForeignKey("sales.id"), index=True)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"))
    name: Mapped[str] = mapped_column(String(160))
    sku: Mapped[str] = mapped_column(String(60))
    quantity: Mapped[int] = mapped_column(Integer)
    stock_quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_label: Mapped[str] = mapped_column(String(80), default="each")
    catalog_unit_price_minor: Mapped[int] = mapped_column(Integer, default=0)
    line_subtotal_minor: Mapped[int] = mapped_column(Integer, default=0)
    discount_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    discount_value: Mapped[int] = mapped_column(Integer, default=0)
    discount_minor: Mapped[int] = mapped_column(Integer, default=0)
    line_total_minor: Mapped[int] = mapped_column(Integer, default=0)
    line_cost_minor: Mapped[int] = mapped_column(Integer, default=0)
    unit_price_minor: Mapped[int] = mapped_column(Integer)
    unit_cost_minor: Mapped[int] = mapped_column(Integer)
