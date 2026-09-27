"""Disposable database smoke check for the first retail transaction path."""

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import service
from app.auth.models import Membership, Organization, User
from app.auth.permissions import OWNER_PERMISSIONS
from app.operations.models import Sale, StockBalance, StockMovement
from app.operations.routes import (
    CartLine,
    CheckoutInput,
    CustomerInput,
    ProductInput,
    StockInput,
    add_stock,
    checkout,
    create_customer,
    create_product,
    daily_report,
)
from app.stores.models import Register, Store


async def main():
    database = os.environ["DATABASE_URL"]
    assert database.rsplit("/", 1)[-1].endswith("_test")
    engine = create_async_engine(database)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        user = User(email="smoke@example.test", password_hash="not-used")
        organization = Organization(name="Smoke shop", currency="NPR")
        db.add_all([user, organization])
        await db.flush()
        membership = Membership(
            user_id=user.id,
            organization_id=organization.id,
            permissions=list(OWNER_PERMISSIONS),
            roles=["owner"],
        )
        store = Store(
            organization_id=organization.id,
            code="MAIN",
            name="Main",
            timezone="Asia/Kathmandu",
            receipt_name="Smoke shop",
        )
        db.add_all([membership, store])
        await db.flush()
        register = Register(store_id=store.id, code="POS1", name="Counter")
        db.add(register)
        await db.flush()
        org_id, store_id, register_id, user_id = organization.id, store.id, register.id, user.id

    async def identity(_request, _db):
        return None, None

    service.locked_identity = identity
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=factory)),
        state=SimpleNamespace(identity=(user_id, None)),
    )
    product = await create_product(
        org_id,
        ProductInput(name="Tea", sku="TEA-1", price_minor=500, cost_minor=300),
        request,
    )
    await add_stock(org_id, store_id, StockInput(product_id=product["id"], quantity=5), request)
    customer = await create_customer(org_id, CustomerInput(name="Daily shopper"), request)
    first = CheckoutInput(
        client_key=uuid4(),
        register_id=register_id,
        customer_type="walkin",
        lines=[CartLine(product_id=product["id"], quantity=2)],
        cash_received_minor=1200,
    )
    bill = await checkout(org_id, store_id, first, request)
    repeated = await checkout(org_id, store_id, first, request)
    assert bill["receipt_number"] == repeated["receipt_number"] == "MAIN-000001"
    assert bill["total_minor"] == 1000 and bill["change_minor"] == 200
    second = CheckoutInput(
        client_key=uuid4(),
        register_id=register_id,
        customer_type="daily",
        customer_id=customer["id"],
        lines=[CartLine(product_id=product["id"], quantity=1)],
        cash_received_minor=500,
    )
    second_bill = await checkout(org_id, store_id, second, request)
    assert second_bill["receipt_number"] == "MAIN-000002"
    assert second_bill["customer"] == "Daily shopper"
    report = await daily_report(org_id, store_id, request, day=None)
    assert report["sale_count"] == 2
    assert report["gross_sales_minor"] == 1500
    assert report["units_sold"] == 3
    assert report["estimated_gross_profit_minor"] == 600
    async with factory() as db:
        quantity = await db.scalar(
            select(StockBalance.quantity).where(StockBalance.store_id == store_id)
        )
        sale_count = await db.scalar(select(func.count()).select_from(Sale))
        movement_count = await db.scalar(select(func.count()).select_from(StockMovement))
        assert quantity == 2 and sale_count == 2 and movement_count == 3
    await engine.dispose()
    print("operations smoke passed: stock, cash, daily customer, receipt, report, idempotency")


asyncio.run(main())
