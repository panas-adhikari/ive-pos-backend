"""Operator-only provisioning. Passwords are never accepted on the command line."""

import asyncio
import getpass

from pydantic import BaseModel, EmailStr, Field, SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.models import Membership, Organization, User
from app.auth.permissions import OWNER_PERMISSIONS
from app.auth.security import PASSWORD_HASHER
from app.auth.service import audit
from app.config import Settings


class OwnerInput(BaseModel):
    email: EmailStr = Field(max_length=254)
    organization: str = Field(min_length=1, max_length=160)
    password: SecretStr = Field(min_length=15, max_length=128)


async def provision(settings: Settings, data: OwnerInput):
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        async with async_sessionmaker(engine)() as db, db.begin():
            user = User(
                email=str(data.email).strip().lower(),
                password_hash=PASSWORD_HASHER.hash(data.password.get_secret_value()),
            )
            organization = Organization(name=data.organization)
            db.add_all([user, organization])
            await db.flush()
            db.add(
                Membership(
                    user_id=user.id,
                    organization_id=organization.id,
                    permissions=list(OWNER_PERMISSIONS),
                    roles=["owner"],
                )
            )
            audit(db, "owner.provisioned", user.id, organization_id=organization.id)
    finally:
        await engine.dispose()


def main():
    settings = Settings.from_environment()
    email = input("Owner email: ").strip().lower()
    organization = input("Organization name: ").strip()
    password = getpass.getpass("Password (15–128 characters): ")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match")
    try:
        data = OwnerInput(email=email, organization=organization, password=password)
    except ValueError:
        raise SystemExit("Invalid email, organization, or password length") from None
    try:
        asyncio.run(provision(settings, data))
    except Exception:
        raise SystemExit(
            "Provisioning failed; check database access and email uniqueness"
        ) from None
    print("Owner and organization created. Sign in through the application.")


if __name__ == "__main__":
    main()
