"""organization onboarding profile and owner details"""

import sqlalchemy as sa
from alembic import op

revision = "c13f4a82d6b1"
down_revision = "93c2d0e5a117"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("full_name", sa.String(length=160), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "users",
        sa.Column("phone", sa.String(length=40), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "users",
        sa.Column("job_title", sa.String(length=100), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "organization_type",
            sa.String(length=30),
            nullable=False,
            server_default=sa.text("'retail'"),
        ),
    )
    op.add_column(
        "organizations",
        sa.Column("image_url", sa.String(length=1000), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "organizations",
        sa.Column("website_url", sa.String(length=300), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "organizations",
        sa.Column("location_label", sa.String(length=240), nullable=False, server_default=sa.text("''")),
    )
    op.add_column("organizations", sa.Column("latitude", sa.Float(), nullable=True))
    op.add_column("organizations", sa.Column("longitude", sa.Float(), nullable=True))
    op.create_check_constraint(
        "ck_organizations_type",
        "organizations",
        "organization_type IN ('retail', 'wholesale', 'other')",
    )
    op.create_check_constraint(
        "ck_organizations_location_coordinates",
        "organizations",
        "(latitude IS NULL AND longitude IS NULL) OR "
        "(latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180)",
    )
    for table, column in (
        ("users", "full_name"), ("users", "phone"), ("users", "job_title"),
        ("organizations", "organization_type"), ("organizations", "image_url"),
        ("organizations", "website_url"), ("organizations", "location_label"),
    ):
        op.alter_column(table, column, server_default=None)


def downgrade():
    op.drop_constraint("ck_organizations_location_coordinates", "organizations", type_="check")
    op.drop_constraint("ck_organizations_type", "organizations", type_="check")
    for column in ("latitude", "longitude", "location_label", "website_url", "image_url", "organization_type"):
        op.drop_column("organizations", column)
    for column in ("job_title", "phone", "full_name"):
        op.drop_column("users", column)
