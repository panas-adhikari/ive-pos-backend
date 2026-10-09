"""Platform staff onboarding, independent of organization membership."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, SecretStr, field_validator
from sqlalchemy import select, text

from app.auth import service
from app.auth.models import User
from app.auth.security import PASSWORD_HASHER, digest, password_work
from app.platform.routes import PlatformEmployee, require_step_up, require_super_admin

router = APIRouter(prefix="/api/v1/platform/staff", tags=["Platform staff"])


class StaffOnboarding(PlatformEmployee):
    full_name: str = Field(min_length=1, max_length=160)
    job_title: str = Field(default="", max_length=100)
    temporary_password: SecretStr = Field(min_length=15, max_length=128)

    @field_validator("full_name", "job_title")
    @classmethod
    def clean_text(cls, value, info):
        value = value.strip()
        if "\x00" in value or (info.field_name == "full_name" and not value):
            raise ValueError("Enter valid staff details")
        return value


def staff_details(user):
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "job_title": user.job_title,
        "role": user.platform_role,
        "active": user.active,
        "must_change_password": user.must_change_password,
        "email_verified": user.email_verified,
        "mfa_enabled": bool(user.mfa_secret),
    }


@router.get("")
async def list_staff(request: Request):
    await service.require_platform_role(request, "super_admin")
    async with request.app.state.db() as db:
        users = await db.scalars(
            select(User).where(User.platform_role != "none").order_by(User.full_name, User.email)
        )
        return [staff_details(user) for user in users]


@router.post("", status_code=201)
async def onboard_staff(body: StaffOnboarding, request: Request):
    async with request.app.state.db() as db, db.begin():
        actor, session = await service.locked_identity(request, db)
        require_super_admin(actor)
        require_step_up(actor, session)
        email = str(body.email)
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(digest(email)[:15], 16)}
        )
        if await db.scalar(select(User.id).where(User.email == email)):
            raise HTTPException(
                409, "An account already uses this email. Use Add existing account."
            )
        user = User(
            email=email,
            full_name=body.full_name,
            job_title=body.job_title,
            platform_role="employee",
            must_change_password=True,
            password_hash=await password_work(
                request, PASSWORD_HASHER.hash, body.temporary_password.get_secret_value()
            ),
        )
        db.add(user)
        await db.flush()
        service.audit(
            db,
            "platform.staff_onboarded",
            actor.id,
            session.id,
            target_type="users",
            target_id=user.id,
            changes={"platform_role": "employee"},
        )
        return staff_details(user)
