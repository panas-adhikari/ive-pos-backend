"""Disposable database smoke check for the first retail transaction path."""

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import service
from app.auth.models import Membership, Organization, User
from app.auth.permissions import OWNER_PERMISSIONS
from app.database import require_test_database
from app.operations.models import Sale, StockBalance, StockMovement
from app.operations.routes import (
    BillEditInput,
    BillEditLine,
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
    delete_product,
    edit_sale,
    sale_receipt,
    update_product,
)
from app.operations.routes import (
    products as list_products,
)
from app.operations.routes import (
    sales as list_sales,
)
from app.operations.routes import (
    stock as stock_list,
)
from app.stores.models import Register, Store


async def main():
    database = os.environ["DATABASE_URL"]
    database = require_test_database(database)
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
    updated = await update_product(
        org_id,
        product["id"],
        ProductInput(name="Tea", sku="TEA-1", category="Drinks", price_minor=500, cost_minor=300),
        request,
    )
    assert updated["category"] == "Drinks"
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
    found = await list_sales(org_id, store_id, request, query="MAIN-000001")
    assert [row["id"] for row in found] == [bill["id"]]
    corrected = await edit_sale(
        org_id,
        store_id,
        bill["id"],
        BillEditInput(
            customer_type="daily",
            customer_id=customer["id"],
            lines=[
                BillEditLine(
                    line_id=bill["lines"][0]["id"],
                    product_id=product["id"],
                    quantity=1,
                    unit_price_minor=450,
                )
            ],
            cash_received_minor=500,
        ),
        request,
    )
    assert corrected["receipt_number"] == bill["receipt_number"]
    assert corrected["customer"] == "Daily shopper"
    assert corrected["total_minor"] == 450 and corrected["change_minor"] == 50
    report = await daily_report(org_id, store_id, request, day=None)
    assert report["sale_count"] == 2
    assert report["gross_sales_minor"] == 950
    assert report["units_sold"] == 2
    assert report["estimated_gross_profit_minor"] == 350
    await delete_product(org_id, product["id"], request)
    assert await stock_list(org_id, store_id, request) == []
    assert await list_products(org_id, store_id, request, query="Tea") == []
    saved_bill = await sale_receipt(org_id, store_id, bill["id"], request)
    assert saved_bill["receipt_number"] == "MAIN-000001"
    replacement = await create_product(
        org_id,
        ProductInput(name="Replacement tea", sku="TEA-1", price_minor=600),
        request,
    )
    assert replacement["sku"] == "TEA-1"
    await add_stock(org_id, store_id, StockInput(product_id=replacement["id"], quantity=1), request)
    try:
        await edit_sale(
            org_id,
            store_id,
            bill["id"],
            BillEditInput(
                customer_type="daily",
                customer_id=customer["id"],
                lines=[BillEditLine(product_id=replacement["id"], quantity=2)],
                cash_received_minor=1200,
            ),
            request,
        )
        raise AssertionError("Editing must reject insufficient stock")
    except HTTPException as error:
        assert error.status_code == 409
    replaced = await edit_sale(
        org_id,
        store_id,
        bill["id"],
        BillEditInput(
            customer_type="daily",
            customer_id=customer["id"],
            lines=[BillEditLine(product_id=replacement["id"], quantity=1)],
            cash_received_minor=600,
        ),
        request,
    )
    assert replaced["receipt_number"] == bill["receipt_number"]
    assert [line["name"] for line in replaced["lines"]] == ["Replacement tea"]
    final_report = await daily_report(org_id, store_id, request, day=None)
    assert final_report["gross_sales_minor"] == 1100
    async with factory() as db:
        quantities = dict(
            (
                await db.execute(
                    select(StockBalance.product_id, StockBalance.quantity).where(
                        StockBalance.store_id == store_id
                    )
                )
            ).all()
        )
        sale_count = await db.scalar(select(func.count()).select_from(Sale))
        movement_count = await db.scalar(select(func.count()).select_from(StockMovement))
        assert quantities[product["id"]] == 4 and quantities[replacement["id"]] == 0
        assert sale_count == 2 and movement_count == 7
    await engine.dispose()
    print("operations smoke passed: checkout, bill correction, stock, report, idempotency")


asyncio.run(main())
