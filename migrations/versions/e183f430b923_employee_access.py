"""Employee permissions, role templates and store scope."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e183f430b923"
down_revision = "c491465f4162"
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        sa.Column(
            "roles",
            postgresql.ARRAY(sa.String(80)),
            nullable=False,
            server_default=sa.text("ARRAY['custom']::varchar[]"),
        ),
        sa.Column("all_stores", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "store_ids",
            postgresql.ARRAY(sa.UUID()),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    ):
        op.add_column("memberships", column)
        op.alter_column("memberships", column.name, server_default=None)
    # Promote only unchanged, active owners with provisioning evidence.
    op.execute("""
        UPDATE memberships AS m
        SET permissions = ARRAY['organization.read', 'organization.setup',
                                'employees.manage', 'store.read']::varchar[],
            roles = ARRAY['owner']::varchar[]
        WHERE m.active
          AND m.permissions = ARRAY['organization.read', 'organization.setup']::varchar[]
          AND EXISTS (SELECT 1 FROM auth_audit_events a
            WHERE a.user_id = m.user_id AND a.organization_id = m.organization_id
              AND a.action IN ('owner.provisioned', 'owner.signup'))
    """)


def downgrade():
    op.execute("""UPDATE memberships SET permissions =
        array_remove(array_remove(permissions, 'employees.manage'), 'store.read')""")
    for name in ("version", "store_ids", "all_stores", "roles"):
        op.drop_column("memberships", name)
