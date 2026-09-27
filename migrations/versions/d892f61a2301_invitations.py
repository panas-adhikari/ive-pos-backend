"""Organization owner and staff invitations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d892f61a2301"
down_revision = "a4f1bcd78923"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "invitations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("invited_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("access", postgresql.JSONB(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.UniqueConstraint("organization_id", "email"),
    )
    op.create_index("ix_invitations_organization_id", "invitations", ["organization_id"])


def downgrade():
    op.drop_table("invitations")
