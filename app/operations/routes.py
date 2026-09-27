"""First retail operations: catalog, stock intake, cash checkout, and daily visibility."""

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field, field_validator, model_validator
from sqlalchemy import or_, select

from app.auth import service
from app.auth.models import Organization
from app.auth.security import now
from app.operations.models import Customer, Product, Sale, SaleLine, StockBalance, StockMovement
from app.stores.models import Register, Store
from app.stores.routes import Input, record

router = APIRouter(prefix="/api/v1/organizations/{organization_id}/operations", tags=["Operations"])


class ProductInput(Input):
    name: str = Field(min_length=1, max_length=160)
    sku: str = Field(min_length=1, max_length=60)
    barcode: str | None = Field(default=None, max_length=80)
    price_minor: int = Field(ge=0, le=1_000_000_000)
    cost_minor: int = Field(default=0, ge=0, le=1_000_000_000)
    low_stock_threshold: int = Field(default=5, ge=0, le=1_000_000)

    @field_validator("sku", mode="before")
    @classmethod
    def normalize_sku(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


class StockInput(Input):
    product_id: UUID
    quantity: int = Field(ge=1, le=1_000_000)
    kind: Literal["opening", "receipt"] = "receipt"
    note: str = Field(default="", max_length=300)


class CustomerInput(Input):
    name: str = Field(min_length=1, max_length=160)
    phone: str | None = Field(default=None, max_length=40)


class CartLine(Input):
    product_id: UUID
    quantity: int = Field(ge=1, le=10_000)


class CheckoutInput(Input):
    client_key: UUID
    register_id: UUID
    customer_type: Literal["walkin", "daily"]
    customer_id: UUID | None = None
    lines: list[CartLine] = Field(min_length=1, max_length=100)
    cash_received_minor: int = Field(ge=0, le=2_000_000_000)

    @model_validator(mode="after")
    def valid_cart(self):
        if (self.customer_type == "daily") != (self.customer_id is not None):
            raise ValueError("Daily sales require a customer; walk-in sales do not")
        if len({line.product_id for line in self.lines}) != len(self.lines):
            raise ValueError("Combine duplicate products into one cart line")
        return self


async def authorize(db, request, organization_id, permission, *, store_id=None, write=False):
    query = select(Organization).where(Organization.id == organization_id)
    if write:
        query = query.with_for_update()
    organization = await db.scalar(query)
    if not organization:
        raise HTTPException(404, "Organization not found")
    if write:
        await service.locked_identity(request, db)
    membership = await service.scoped_permission(db, request, organization_id, permission, store_id)
    return organization, membership


async def store_row(db, organization_id, store_id, *, lock=False):
    query = select(Store).where(Store.id == store_id, Store.organization_id == organization_id)
    if lock:
        query = query.with_for_update()
    store = await db.scalar(query)
    if not store or not store.active:
        raise HTTPException(409, "Choose an active store")
    return store


async def receipt(db, sale):
    store = await db.get(Store, sale.store_id)
    organization = await db.get(Organization, sale.organization_id)
    customer = await db.get(Customer, sale.customer_id) if sale.customer_id else None
    lines = list(await db.scalars(select(SaleLine).where(SaleLine.sale_id == sale.id)))
    return {
        "id": sale.id,
        "receipt_number": sale.receipt_number,
        "created": sale.created,
        "organization": organization.name,
        "store": store.receipt_name,
        "store_address": store.address,
        "receipt_footer": store.receipt_footer,
        "currency": sale.currency,
        "customer_type": sale.customer_type,
        "customer": customer.name if customer else None,
        "payment_method": sale.payment_method,
        "status": sale.status,
        "total_minor": sale.total_minor,
        "cash_received_minor": sale.cash_received_minor,
        "change_minor": sale.cash_received_minor - sale.total_minor,
        "lines": [
            {
                "product_id": line.product_id,
                "name": line.name,
                "sku": line.sku,
                "quantity": line.quantity,
                "unit_price_minor": line.unit_price_minor,
                "line_total_minor": line.quantity * line.unit_price_minor,
            }
            for line in lines
        ],
    }


@router.get("/context")
async def context(organization_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        organization, membership = await authorize(db, request, organization_id, "store.read")
        statement = (
            select(Store)
            .where(Store.organization_id == organization_id, Store.active.is_(True))
            .order_by(Store.code)
        )
        if not membership.all_stores:
            statement = statement.where(Store.id.in_(membership.store_ids))
        stores = list(await db.scalars(statement))
        registers = list(
            await db.scalars(
                select(Register)
                .where(
                    Register.store_id.in_([store.id for store in stores]), Register.active.is_(True)
                )
                .order_by(Register.code)
            )
        )
        return {
            "organization": organization.name,
            "currency": organization.currency,
            "stores": [
                {
                    "id": store.id,
                    "name": store.name,
                    "code": store.code,
                    "registers": [
                        {"id": register.id, "name": register.name, "code": register.code}
                        for register in registers
                        if register.store_id == store.id
                    ],
                }
                for store in stores
            ],
        }


@router.get("/stores/{store_id}/products")
async def products(organization_id: UUID, store_id: UUID, request: Request, query: str = ""):
    async with request.app.state.db() as db, db.begin():
        await authorize(db, request, organization_id, "store.read", store_id=store_id)
        statement = (
            select(Product, StockBalance.quantity)
            .outerjoin(
                StockBalance,
                (StockBalance.product_id == Product.id) & (StockBalance.store_id == store_id),
            )
            .where(Product.organization_id == organization_id)
            .order_by(Product.name)
            .limit(100)
        )
        term = query.strip()[:100]
        if term:
            statement = statement.where(
                or_(
                    Product.name.ilike(f"%{term}%"),
                    Product.sku.ilike(f"%{term}%"),
                    Product.barcode.ilike(f"%{term}%"),
                )
            )
        return [
            {**record(product), "quantity": quantity or 0}
            for product, quantity in (await db.execute(statement)).all()
        ]


@router.post("/products", status_code=201)
async def create_product(organization_id: UUID, body: ProductInput, request: Request):
    async with request.app.state.db() as db, db.begin():
        _, member = await authorize(db, request, organization_id, "catalog.manage", write=True)
        if not member.all_stores:
            raise HTTPException(403, "Organization-wide catalog access required")
        if await db.scalar(
            select(Product.id).where(
                Product.organization_id == organization_id,
                or_(
                    Product.sku == body.sku,
                    Product.barcode == body.barcode if body.barcode else False,
                ),
            )
        ):
            raise HTTPException(409, "SKU or barcode already exists")
        product = Product(organization_id=organization_id, **body.model_dump())
        db.add(product)
        await db.flush()
        service.audit(
            db,
            "product.created",
            *request.state.identity,
            organization_id=organization_id,
            target_type="products",
            target_id=product.id,
        )
        return record(product)


@router.post("/stores/{store_id}/stock", status_code=201)
async def add_stock(organization_id: UUID, store_id: UUID, body: StockInput, request: Request):
    async with request.app.state.db() as db, db.begin():
        await authorize(
            db, request, organization_id, "inventory.manage", store_id=store_id, write=True
        )
        await store_row(db, organization_id, store_id, lock=True)
        product = await db.scalar(
            select(Product).where(
                Product.id == body.product_id,
                Product.organization_id == organization_id,
            )
        )
        if not product or not product.active:
            raise HTTPException(404, "Active product not found")
        balance = await db.scalar(
            select(StockBalance)
            .where(
                StockBalance.store_id == store_id,
                StockBalance.product_id == product.id,
            )
            .with_for_update()
        )
        if not balance:
            balance = StockBalance(
                organization_id=organization_id,
                store_id=store_id,
                product_id=product.id,
                quantity=0,
            )
            db.add(balance)
        balance.quantity += body.quantity
        movement = StockMovement(
            organization_id=organization_id,
            store_id=store_id,
            product_id=product.id,
            actor_id=request.state.identity[0],
            kind=body.kind,
            delta=body.quantity,
            note=body.note,
            created=now(),
        )
        db.add(movement)
        await db.flush()
        service.audit(
            db,
            "stock.received",
            *request.state.identity,
            organization_id=organization_id,
            target_type="stock_movements",
            target_id=movement.id,
        )
        return {"product_id": product.id, "quantity": balance.quantity, "movement_id": movement.id}


@router.get("/stores/{store_id}/stock")
async def stock(organization_id: UUID, store_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        await authorize(db, request, organization_id, "store.read", store_id=store_id)
        rows = (
            await db.execute(
                select(Product, StockBalance.quantity)
                .outerjoin(
                    StockBalance,
                    (StockBalance.product_id == Product.id) & (StockBalance.store_id == store_id),
                )
                .where(Product.organization_id == organization_id)
                .order_by(Product.name)
            )
        ).all()
        return [
            {
                **record(product),
                "quantity": quantity or 0,
                "low_stock": (quantity or 0) <= product.low_stock_threshold,
            }
            for product, quantity in rows
        ]


@router.get("/stores/{store_id}/movements")
async def movements(organization_id: UUID, store_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        await authorize(db, request, organization_id, "reports.read", store_id=store_id)
        rows = (
            await db.execute(
                select(StockMovement, Product.name)
                .join(Product, Product.id == StockMovement.product_id)
                .where(
                    StockMovement.organization_id == organization_id,
                    StockMovement.store_id == store_id,
                )
                .order_by(StockMovement.created.desc())
                .limit(100)
            )
        ).all()
        return [{**record(movement), "product_name": name} for movement, name in rows]


@router.get("/customers")
async def customers(organization_id: UUID, request: Request, query: str = ""):
    async with request.app.state.db() as db, db.begin():
        await authorize(db, request, organization_id, "sales.create")
        statement = select(Customer).where(Customer.organization_id == organization_id)
        term = query.strip()[:100]
        if term:
            statement = statement.where(
                or_(Customer.name.ilike(f"%{term}%"), Customer.phone.ilike(f"%{term}%"))
            )
        rows = await db.scalars(statement.order_by(Customer.name).limit(100))
        return [record(row) for row in rows]


@router.post("/customers", status_code=201)
async def create_customer(organization_id: UUID, body: CustomerInput, request: Request):
    async with request.app.state.db() as db, db.begin():
        await authorize(db, request, organization_id, "sales.create", write=True)
        if body.phone and await db.scalar(
            select(Customer.id).where(
                Customer.organization_id == organization_id,
                Customer.phone == body.phone,
            )
        ):
            raise HTTPException(409, "A customer already uses this phone")
        customer = Customer(
            organization_id=organization_id, name=body.name, phone=body.phone or None, created=now()
        )
        db.add(customer)
        await db.flush()
        service.audit(
            db,
            "customer.created",
            *request.state.identity,
            organization_id=organization_id,
            target_type="customers",
            target_id=customer.id,
        )
        return record(customer)


@router.post("/stores/{store_id}/checkout", status_code=201)
async def checkout(organization_id: UUID, store_id: UUID, body: CheckoutInput, request: Request):
    fingerprint = hashlib.sha256(
        json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    async with request.app.state.db() as db, db.begin():
        organization, _ = await authorize(
            db, request, organization_id, "sales.create", store_id=store_id, write=True
        )
        existing = await db.scalar(
            select(Sale).where(
                Sale.organization_id == organization_id,
                Sale.client_key == body.client_key,
            )
        )
        if existing:
            if existing.store_id != store_id or existing.request_hash != fingerprint:
                raise HTTPException(409, "Checkout key was used for another cart")
            return await receipt(db, existing)
        store = await store_row(db, organization_id, store_id, lock=True)
        register = await db.scalar(
            select(Register).where(
                Register.id == body.register_id,
                Register.store_id == store_id,
                Register.active.is_(True),
            )
        )
        if not register:
            raise HTTPException(409, "Choose an active register")
        if body.customer_id and not await db.scalar(
            select(Customer.id).where(
                Customer.id == body.customer_id,
                Customer.organization_id == organization_id,
            )
        ):
            raise HTTPException(404, "Customer not found")
        products = {
            product.id: product
            for product in await db.scalars(
                select(Product).where(
                    Product.organization_id == organization_id,
                    Product.id.in_([line.product_id for line in body.lines]),
                    Product.active.is_(True),
                )
            )
        }
        if len(products) != len(body.lines):
            raise HTTPException(409, "One or more products are unavailable")
        balances = {
            balance.product_id: balance
            for balance in await db.scalars(
                select(StockBalance)
                .where(
                    StockBalance.store_id == store_id,
                    StockBalance.product_id.in_(products),
                )
                .order_by(StockBalance.product_id)
                .with_for_update()
            )
        }
        total = 0
        for line in body.lines:
            balance = balances.get(line.product_id)
            if not balance or balance.quantity < line.quantity:
                raise HTTPException(409, f"Insufficient stock for {products[line.product_id].name}")
            total += products[line.product_id].price_minor * line.quantity
        if total > 2_000_000_000:
            raise HTTPException(422, "Cart total is too large")
        if body.cash_received_minor < total:
            raise HTTPException(
                422, "Cash received must cover the full sale; credit is not available yet"
            )
        store.receipt_sequence += 1
        sale = Sale(
            organization_id=organization_id,
            store_id=store_id,
            register_id=register.id,
            customer_id=body.customer_id,
            customer_type=body.customer_type,
            client_key=body.client_key,
            request_hash=fingerprint,
            receipt_number=f"{store.code}-{store.receipt_sequence:06d}",
            currency=organization.currency,
            total_minor=total,
            cash_received_minor=body.cash_received_minor,
            payment_method="cash",
            status="paid",
            actor_id=request.state.identity[0],
            created=now(),
        )
        db.add(sale)
        await db.flush()
        for line in body.lines:
            product = products[line.product_id]
            balances[line.product_id].quantity -= line.quantity
            db.add(
                SaleLine(
                    sale_id=sale.id,
                    product_id=product.id,
                    name=product.name,
                    sku=product.sku,
                    quantity=line.quantity,
                    unit_price_minor=product.price_minor,
                    unit_cost_minor=product.cost_minor,
                )
            )
            db.add(
                StockMovement(
                    organization_id=organization_id,
                    store_id=store_id,
                    product_id=product.id,
                    actor_id=request.state.identity[0],
                    kind="sale",
                    delta=-line.quantity,
                    note="",
                    sale_id=sale.id,
                    created=sale.created,
                )
            )
        await db.flush()
        service.audit(
            db,
            "sale.completed",
            *request.state.identity,
            organization_id=organization_id,
            target_type="sales",
            target_id=sale.id,
        )
        return await receipt(db, sale)


@router.get("/stores/{store_id}/sales")
async def sales(organization_id: UUID, store_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        await authorize(db, request, organization_id, "sales.create", store_id=store_id)
        rows = await db.scalars(
            select(Sale)
            .where(
                Sale.organization_id == organization_id,
                Sale.store_id == store_id,
            )
            .order_by(Sale.created.desc())
            .limit(50)
        )
        return [record(row) for row in rows]


@router.get("/stores/{store_id}/sales/{sale_id}")
async def sale_receipt(organization_id: UUID, store_id: UUID, sale_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        await authorize(db, request, organization_id, "sales.create", store_id=store_id)
        sale = await db.scalar(
            select(Sale).where(
                Sale.id == sale_id,
                Sale.organization_id == organization_id,
                Sale.store_id == store_id,
            )
        )
        if not sale:
            raise HTTPException(404, "Sale not found")
        return await receipt(db, sale)


@router.get("/stores/{store_id}/reports")
async def daily_report(
    organization_id: UUID, store_id: UUID, request: Request, day: date | None = Query(default=None)
):
    async with request.app.state.db() as db, db.begin():
        organization, _ = await authorize(
            db, request, organization_id, "reports.read", store_id=store_id
        )
        store = await store_row(db, organization_id, store_id)
        tz = ZoneInfo(store.timezone or organization.timezone)
        business_day = day or datetime.now(tz).date()
        start = datetime.combine(business_day, time.min, tz).astimezone(timezone.utc)
        end = datetime.combine(business_day + timedelta(days=1), time.min, tz).astimezone(
            timezone.utc
        )
        sales_rows = list(
            await db.scalars(
                select(Sale)
                .where(
                    Sale.organization_id == organization_id,
                    Sale.store_id == store_id,
                    Sale.created >= start,
                    Sale.created < end,
                )
                .order_by(Sale.created.desc())
            )
        )
        ids = [sale.id for sale in sales_rows]
        cost = 0
        units = 0
        if ids:
            line_rows = await db.execute(
                select(SaleLine.quantity, SaleLine.unit_cost_minor).where(
                    SaleLine.sale_id.in_(ids),
                )
            )
            for quantity, unit_cost in line_rows:
                units += quantity
                cost += quantity * unit_cost
        stock_rows = (
            await db.execute(
                select(Product, StockBalance.quantity)
                .outerjoin(
                    StockBalance,
                    (StockBalance.product_id == Product.id) & (StockBalance.store_id == store_id),
                )
                .where(Product.organization_id == organization_id, Product.active.is_(True))
                .order_by(Product.name)
            )
        ).all()
        total = sum(sale.total_minor for sale in sales_rows)
        return {
            "date": business_day,
            "timezone": str(tz),
            "currency": organization.currency,
            "sale_count": len(sales_rows),
            "units_sold": units,
            "gross_sales_minor": total,
            "cash_collected_minor": total,
            "estimated_cost_minor": cost,
            "estimated_gross_profit_minor": total - cost,
            "low_stock": [
                {
                    "product_id": product.id,
                    "name": product.name,
                    "sku": product.sku,
                    "quantity": quantity or 0,
                    "threshold": product.low_stock_threshold,
                }
                for product, quantity in stock_rows
                if (quantity or 0) <= product.low_stock_threshold
            ],
            "sales": [
                {
                    "id": sale.id,
                    "receipt_number": sale.receipt_number,
                    "created": sale.created,
                    "total_minor": sale.total_minor,
                    "customer_type": sale.customer_type,
                }
                for sale in sales_rows[:50]
            ],
        }
