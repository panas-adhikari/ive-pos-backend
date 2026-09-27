"""require temporary owner passwords to be changed"""

import sqlalchemy as sa
from alembic import op

revision = "93c2d0e5a117"
down_revision = "7e34af1a9b02"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("users", "must_change_password", server_default=None)


def downgrade():
    op.drop_column("users", "must_change_password")
