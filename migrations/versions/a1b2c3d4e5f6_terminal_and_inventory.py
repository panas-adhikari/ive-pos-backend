"""Catalog, stock ledger, customers, cash sales, and receipts."""

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "c13f4a82d6b1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "stores", sa.Column("receipt_sequence", sa.Integer(), nullable=False, server_default="0")
    )
    op.alter_column("stores", "receipt_sequence", server_default=None)
    op.create_table(
        "products",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("sku", sa.String(60), nullable=False),
        sa.Column("barcode", sa.String(80)),
        sa.Column("price_minor", sa.Integer(), nullable=False),
        sa.Column("cost_minor", sa.Integer(), nullable=False),
        sa.Column("low_stock_threshold", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("organization_id", "sku"),
        sa.UniqueConstraint("organization_id", "barcode"),
        sa.CheckConstraint("price_minor >= 0 AND cost_minor >= 0 AND low_stock_threshold >= 0"),
    )
    op.create_index("ix_products_organization_id", "products", ["organization_id"])
    op.create_table(
        "customers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("phone", sa.String(40)),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "phone"),
    )
    op.create_index("ix_customers_organization_id", "customers", ["organization_id"])
    op.create_table(
        "stock_balances",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("store_id", sa.Uuid(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.UniqueConstraint("store_id", "product_id"),
        sa.CheckConstraint("quantity >= 0"),
    )
    for name in ("organization_id", "store_id", "product_id"):
        op.create_index(f"ix_stock_balances_{name}", "stock_balances", [name])
    op.create_table(
        "sales",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("store_id", sa.Uuid(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("register_id", sa.Uuid(), sa.ForeignKey("registers.id"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("customers.id")),
        sa.Column("customer_type", sa.String(20), nullable=False),
        sa.Column("client_key", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_number", sa.String(50), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("total_minor", sa.Integer(), nullable=False),
        sa.Column("cash_received_minor", sa.Integer(), nullable=False),
        sa.Column("payment_method", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "client_key"),
        sa.UniqueConstraint("store_id", "receipt_number"),
        sa.CheckConstraint("total_minor >= 0 AND cash_received_minor >= total_minor"),
    )
    for name in ("organization_id", "store_id"):
        op.create_index(f"ix_sales_{name}", "sales", [name])
    op.create_index("ix_sales_store_created", "sales", ["store_id", "created"])
    op.create_table(
        "sale_lines",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("sale_id", sa.Uuid(), sa.ForeignKey("sales.id"), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("sku", sa.String(60), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_minor", sa.Integer(), nullable=False),
        sa.Column("unit_cost_minor", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity > 0 AND unit_price_minor >= 0"),
    )
    op.create_index("ix_sale_lines_sale_id", "sale_lines", ["sale_id"])
    op.create_table(
        "stock_movements",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("store_id", sa.Uuid(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(300), nullable=False),
        sa.Column("sale_id", sa.Uuid(), sa.ForeignKey("sales.id")),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("delta <> 0"),
    )
    for name in ("organization_id", "store_id", "product_id"):
        op.create_index(f"ix_stock_movements_{name}", "stock_movements", [name])
    op.execute("""
        UPDATE memberships SET permissions = array_cat(
            permissions,
            ARRAY['catalog.manage','inventory.manage','sales.create','reports.read']::varchar[]
        ) WHERE roles && ARRAY['owner','organization_admin','administrator']::varchar[]
    """)


def downgrade():
    op.execute("""
        UPDATE memberships SET permissions = array_remove(array_remove(array_remove(
            array_remove(permissions, 'catalog.manage'), 'inventory.manage'),
            'sales.create'), 'reports.read')
    """)
    for table in (
        "stock_movements",
        "sale_lines",
        "sales",
        "stock_balances",
        "customers",
        "products",
    ):
        op.drop_table(table)
    op.drop_column("stores", "receipt_sequence")
