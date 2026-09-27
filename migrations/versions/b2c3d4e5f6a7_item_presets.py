"""Item categories, stock units, presets, and sale line snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("products", sa.Column("category", sa.String(80)))
    op.add_column(
        "products", sa.Column("stock_unit", sa.String(10), nullable=False, server_default="piece")
    )
    op.add_column("products", sa.Column("presets", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column(
        "sale_lines", sa.Column("stock_quantity", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column(
        "sale_lines", sa.Column("unit_label", sa.String(80), nullable=False, server_default="each")
    )
    op.add_column(
        "sale_lines",
        sa.Column("line_total_minor", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "sale_lines", sa.Column("line_cost_minor", sa.Integer(), nullable=False, server_default="0")
    )
    op.execute(
        "UPDATE sale_lines SET stock_quantity = quantity, "
        "line_total_minor = quantity * unit_price_minor, "
        "line_cost_minor = quantity * unit_cost_minor"
    )
    for column in ("stock_unit", "presets"):
        op.alter_column("products", column, server_default=None)
    for column in ("stock_quantity", "unit_label", "line_total_minor", "line_cost_minor"):
        op.alter_column("sale_lines", column, server_default=None)


def downgrade():
    for column in ("line_cost_minor", "line_total_minor", "unit_label", "stock_quantity"):
        op.drop_column("sale_lines", column)
    for column in ("presets", "stock_unit", "category"):
        op.drop_column("products", column)
