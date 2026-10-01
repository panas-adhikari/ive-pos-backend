"""Product price history, checkout price overrides, and discounts."""

import sqlalchemy as sa
from alembic import op

revision = "d3e4f5a6b7c8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "product_price_history",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("old_price_minor", sa.Integer()),
        sa.Column("new_price_minor", sa.Integer(), nullable=False),
        sa.Column("old_cost_minor", sa.Integer()),
        sa.Column("new_cost_minor", sa.Integer(), nullable=False),
        sa.Column("old_presets", sa.JSON()),
        sa.Column("new_presets", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(30), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_product_price_history_organization_id", "product_price_history", ["organization_id"]
    )
    op.create_index(
        "ix_product_price_history_product_id", "product_price_history", ["product_id"]
    )
    op.execute(
        "INSERT INTO product_price_history "
        "(id, organization_id, product_id, actor_id, old_price_minor, new_price_minor, "
        "old_cost_minor, new_cost_minor, old_presets, new_presets, reason, created) "
        "SELECT gen_random_uuid(), organization_id, id, NULL, NULL, price_minor, NULL, "
        "cost_minor, NULL, presets, 'migration_baseline', CURRENT_TIMESTAMP FROM products"
    )

    op.add_column(
        "sales", sa.Column("subtotal_minor", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("sales", sa.Column("discount_type", sa.String(20)))
    op.add_column(
        "sales", sa.Column("discount_value", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "sales", sa.Column("discount_minor", sa.Integer(), nullable=False, server_default="0")
    )
    op.execute("UPDATE sales SET subtotal_minor = total_minor")
    for column in ("subtotal_minor", "discount_value", "discount_minor"):
        op.alter_column("sales", column, server_default=None)
    op.create_check_constraint(
        "ck_sales_discount_totals",
        "sales",
        "subtotal_minor >= 0 AND discount_minor >= 0 AND total_minor >= 0 "
        "AND discount_minor <= subtotal_minor AND cash_received_minor >= total_minor",
    )

    op.add_column(
        "sale_lines",
        sa.Column("catalog_unit_price_minor", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "sale_lines",
        sa.Column("line_subtotal_minor", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("sale_lines", sa.Column("discount_type", sa.String(20)))
    op.add_column(
        "sale_lines", sa.Column("discount_value", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "sale_lines", sa.Column("discount_minor", sa.Integer(), nullable=False, server_default="0")
    )
    op.execute(
        "UPDATE sale_lines SET catalog_unit_price_minor = unit_price_minor, "
        "line_subtotal_minor = line_total_minor"
    )
    for column in (
        "catalog_unit_price_minor",
        "line_subtotal_minor",
        "discount_value",
        "discount_minor",
    ):
        op.alter_column("sale_lines", column, server_default=None)
    op.create_check_constraint(
        "ck_sale_lines_discount_totals",
        "sale_lines",
        "catalog_unit_price_minor >= 0 AND line_subtotal_minor >= 0 "
        "AND discount_minor >= 0 AND discount_minor <= line_subtotal_minor "
        "AND line_total_minor >= 0",
    )


def downgrade():
    op.drop_constraint("ck_sale_lines_discount_totals", "sale_lines", type_="check")
    for column in (
        "discount_minor",
        "discount_value",
        "discount_type",
        "line_subtotal_minor",
        "catalog_unit_price_minor",
    ):
        op.drop_column("sale_lines", column)
    op.drop_constraint("ck_sales_discount_totals", "sales", type_="check")
    for column in ("discount_minor", "discount_value", "discount_type", "subtotal_minor"):
        op.drop_column("sales", column)
    op.drop_table("product_price_history")
