"""Session MFA proof and account verification preference."""

import sqlalchemy as sa
from alembic import op

revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column(
            "require_action_verification", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    # Existing sessions have no recorded login proof: they must verify once.
    op.add_column(
        "auth_sessions",
        sa.Column("mfa_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("auth_sessions", "mfa_verified")
    op.drop_column("users", "require_action_verification")
