"""Organization addresses and tenant-bound sessions."""

import re
import unicodedata

import sqlalchemy as sa
from alembic import op

revision = "f5a6b7c8d9e0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("organizations", sa.Column("slug", sa.String(63), nullable=True))
    db = op.get_bind()
    reserved = {
        "www",
        "app",
        "api",
        "admin",
        "platform",
        "auth",
        "login",
        "signup",
        "mail",
        "smtp",
        "support",
        "status",
        "static",
        "assets",
        "cdn",
        "docs",
        "help",
        "billing",
    }
    used = set(reserved)
    rows = db.execute(sa.text("SELECT id, name FROM organizations ORDER BY id")).all()
    for org_id, name in rows:
        plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
        base = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:63].rstrip("-") or "store"
        if base in reserved:
            base += "-store"
        candidate, suffix = base, 1
        while candidate in used:
            suffix += 1
            ending = f"-{suffix}"
            candidate = base[: 63 - len(ending)].rstrip("-") + ending
        db.execute(
            sa.text("UPDATE organizations SET slug = :slug WHERE id = :id"),
            {"slug": candidate, "id": org_id},
        )
        used.add(candidate)
    op.alter_column("organizations", "slug", nullable=False)
    op.create_unique_constraint("uq_organizations_slug", "organizations", ["slug"])
    op.add_column("auth_sessions", sa.Column("tenant_id", sa.Uuid(), nullable=True))


def downgrade():
    op.drop_column("auth_sessions", "tenant_id")
    op.drop_constraint("uq_organizations_slug", "organizations", type_="unique")
    op.drop_column("organizations", "slug")
