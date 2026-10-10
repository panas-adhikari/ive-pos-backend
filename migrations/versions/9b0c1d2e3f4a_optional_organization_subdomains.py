"""Make organization subdomains opt-in while preserving existing addresses."""

import sqlalchemy as sa
from alembic import op

revision = "9b0c1d2e3f4a"
down_revision = "8a9b0c1d2e3f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "organizations",
        sa.Column("subdomain_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # Existing organizations retain their URLs; future ones choose a subdomain separately.
    op.alter_column("organizations", "subdomain_enabled", server_default=sa.false())


def downgrade():
    op.drop_column("organizations", "subdomain_enabled")
