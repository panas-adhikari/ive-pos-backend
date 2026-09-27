"""platform user roles"""

import sqlalchemy as sa
from alembic import op

revision = "f17d4c0a9e12"
down_revision = "e183f430b923"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("platform_role", sa.String(length=32), nullable=False, server_default="none"),
    )
    op.create_check_constraint(
        "ck_users_platform_role", "users", "platform_role IN ('none', 'super_admin', 'employee')"
    )
    op.alter_column("users", "platform_role", server_default=None)


def downgrade():
    op.drop_constraint("ck_users_platform_role", "users", type_="check")
    op.drop_column("users", "platform_role")
