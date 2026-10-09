"""Platform billing terms and manually recorded payments."""

import sqlalchemy as sa
from alembic import op

revision = "8a9b0c1d2e3f"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade():
    for name, kind in [
        ("billing_plan", sa.String(80)),
        ("billing_amount_minor", sa.Integer()),
        ("billing_currency", sa.String(3)),
        ("billing_interval", sa.String(10)),
    ]:
        op.add_column("organizations", sa.Column(name, kind, nullable=True))
    op.create_table(
        "platform_payments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("client_key", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("reference", sa.String(120), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("voided_at", sa.DateTime(timezone=True)),
        sa.Column("void_reason", sa.String(240)),
        sa.UniqueConstraint("organization_id", "client_key", name="uq_platform_payment_client"),
        sa.CheckConstraint("amount_minor > 0", name="ck_platform_payment_amount"),
    )
    op.create_index(
        "ix_platform_payments_organization_id", "platform_payments", ["organization_id"]
    )


def downgrade():
    op.drop_table("platform_payments")
    for name in ["billing_interval", "billing_currency", "billing_amount_minor", "billing_plan"]:
        op.drop_column("organizations", name)
