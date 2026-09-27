"""platform managed organization limits"""

import sqlalchemy as sa
from alembic import op

revision = "a4f1bcd78923"
down_revision = "f17d4c0a9e12"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "organizations",
        sa.Column("store_limit", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "organizations",
        sa.Column("employee_limit", sa.Integer(), nullable=False, server_default="5"),
    )
    op.create_check_constraint("ck_organizations_store_limit", "organizations", "store_limit >= 1")
    op.create_check_constraint(
        "ck_organizations_employee_limit", "organizations", "employee_limit >= 1"
    )
    op.alter_column("organizations", "store_limit", server_default=None)
    op.alter_column("organizations", "employee_limit", server_default=None)


def downgrade():
    op.drop_constraint("ck_organizations_employee_limit", "organizations", type_="check")
    op.drop_constraint("ck_organizations_store_limit", "organizations", type_="check")
    op.drop_column("organizations", "employee_limit")
    op.drop_column("organizations", "store_limit")
