"""organization deletion schedule"""

import sqlalchemy as sa
from alembic import op

revision = "7e34af1a9b02"
down_revision = "d892f61a2301"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "organizations",
        sa.Column("deletion_scheduled_for", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_organizations_deletion_scheduled_for",
        "organizations",
        ["deletion_scheduled_for"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_organizations_deletion_scheduled_for", table_name="organizations")
    op.drop_column("organizations", "deletion_scheduled_for")
