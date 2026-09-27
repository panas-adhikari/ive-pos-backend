"""Prepare one dedicated browser-test account, never a production database."""

import asyncio
import os

from sqlalchemy import delete, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.factors import cipher
from app.auth.models import Invitation, Membership, Organization, RateBucket, Session, User
from app.auth.permissions import OWNER_PERMISSIONS
from app.auth.security import PASSWORD_HASHER, digest
from app.config import Settings
from app.stores.models import Register, Store


async def main():
    url = os.environ["AUTH_TEST_DATABASE_URL"]
    if not make_url(url).database.endswith("_test"):
        raise RuntimeError("A dedicated _test database is required")
    engine = create_async_engine(url)
    async with async_sessionmaker(engine)() as db, db.begin():
        user = await db.scalar(select(User).where(User.email == "browser@example.com"))
        if not user:
            user = User(email="browser@example.com", password_hash="pending")
            org = Organization(name="Browser test store")
            db.add_all([user, org])
            await db.flush()
            db.add(
                Membership(
                    user_id=user.id, organization_id=org.id, permissions=["organization.read"]
                )
            )
        user.password_hash = PASSWORD_HASHER.hash("browser testing passphrase only")
        user.active = True
        user.email_verified = False
        user.mfa_secret = None
        user.mfa_pending = None
        user.mfa_pending_expires = None
        user.mfa_last_step = -1
        user.recovery_hashes = []
        await db.execute(update(Session).where(Session.user_id == user.id).values(revoked=True))
        await db.execute(delete(RateBucket))
        platform_user = await db.scalar(
            select(User).where(User.email == "platform-browser@example.com")
        )
        if not platform_user:
            platform_user = User(email="platform-browser@example.com", password_hash="pending")
            db.add(platform_user)
            await db.flush()
        platform_user.password_hash = PASSWORD_HASHER.hash("browser platform passphrase only")
        platform_user.active = True
        platform_user.email_verified = True
        platform_user.platform_role = "super_admin"
        platform_user.mfa_secret = (
            cipher(Settings.from_environment()).encrypt(b"JBSWY3DPEHPK3PXP").decode()
        )
        platform_user.recovery_hashes = [digest(f"{n:020x}") for n in range(1, 11)]
        await db.execute(
            update(Session).where(Session.user_id == platform_user.id).values(revoked=True)
        )
        # Separate MFA-enabled owner for store setup; never alter a non-test database.
        setup_user = await db.scalar(select(User).where(User.email == "setup-browser@example.com"))
        if not setup_user:
            setup_user = User(email="setup-browser@example.com", password_hash="pending")
            db.add(setup_user)
            await db.flush()
            for name in ("Setup first business", "Setup second business"):
                org = Organization(name=name)
                db.add(org)
                await db.flush()
                db.add(
                    Membership(
                        user_id=setup_user.id,
                        organization_id=org.id,
                        permissions=list(OWNER_PERMISSIONS),
                    )
                )
            await db.flush()
        setup_user.password_hash = PASSWORD_HASHER.hash("browser setup passphrase only")
        setup_user.email_verified = True
        setup_user.active = True
        setup_user.mfa_secret = (
            cipher(Settings.from_environment()).encrypt(b"JBSWY3DPEHPK3PXP").decode()
        )
        setup_user.mfa_last_step = -1
        setup_user.recovery_hashes = [digest(f"{n:020x}") for n in range(1, 11)]
        await db.execute(
            update(Session).where(Session.user_id == setup_user.id).values(revoked=True)
        )
        await db.execute(
            update(Membership)
            .where(Membership.user_id == setup_user.id)
            .values(permissions=list(OWNER_PERMISSIONS), roles=["owner"], all_stores=True)
        )
        employee = await db.scalar(select(User).where(User.email == "employee-browser@example.com"))
        if not employee:
            employee = User(email="employee-browser@example.com", password_hash="pending")
            db.add(employee)
            await db.flush()
        employee.password_hash = PASSWORD_HASHER.hash("browser employee passphrase only")
        employee.active = True
        employee.email_verified = True
        await db.execute(delete(Membership).where(Membership.user_id == employee.id))
        await db.execute(update(Session).where(Session.user_id == employee.id).values(revoked=True))
        org_ids = list(
            await db.scalars(
                select(Membership.organization_id).where(Membership.user_id == setup_user.id)
            )
        )
        await db.execute(
            delete(Register).where(
                Register.store_id.in_(select(Store.id).where(Store.organization_id.in_(org_ids)))
            )
        )
        await db.execute(delete(Store).where(Store.organization_id.in_(org_ids)))
        await db.execute(delete(Invitation).where(Invitation.organization_id.in_(org_ids)))
        await db.execute(
            update(Organization)
            .where(Organization.id.in_(org_ids))
            .values(configured=False, version=1)
        )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
