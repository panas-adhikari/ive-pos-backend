"""Create the first platform super administrator from an interactive terminal."""

import asyncio
import getpass

from pydantic import BaseModel, EmailStr, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.models import User
from app.auth.security import PASSWORD_HASHER, now
from app.auth.service import audit
from app.config import Settings


class PlatformAdminInput(BaseModel):
    email: EmailStr = Field(max_length=254)
    # Initial local bootstrap may use a temporary 12-character password. Normal password
    # changes continue to enforce the application's 15-character minimum.
    password: SecretStr = Field(min_length=12, max_length=128)


async def provision(settings: Settings, data: PlatformAdminInput):
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        async with async_sessionmaker(engine)() as db, db.begin():
            if await db.scalar(select(User.id).where(User.email == str(data.email))):
                raise ValueError("An account already uses this email")
            user = User(
                email=str(data.email).strip().lower(),
                password_hash=PASSWORD_HASHER.hash(data.password.get_secret_value()),
                # A terminal operator has verified this address during initial provisioning.
                email_verified=True,
                platform_role="super_admin",
            )
            db.add(user)
            await db.flush()
            audit(
                db,
                "platform.super_admin_provisioned",
                user.id,
                changes={"created": now().isoformat()},
            )
    finally:
        await engine.dispose()


def main():
    settings = Settings.from_environment()
    email = input("Platform admin email: ").strip().lower()
    password = getpass.getpass("Temporary password (12–128 characters): ")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match")
    try:
        data = PlatformAdminInput(email=email, password=password)
        asyncio.run(provision(settings, data))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print("Platform super administrator created. Sign in through the application and enroll MFA.")


if __name__ == "__main__":
    main()
