from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator
from sqlalchemy import select, update

from app.auth import service
from app.auth.models import Membership, Organization, Session, User
from app.auth.security import ACCESS_SECONDS, IDLE_SECONDS, now

router = APIRouter(prefix="/api/v1", tags=["Identity"])


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr = Field(max_length=254)
    password: SecretStr = Field(min_length=1, max_length=128)
    code: SecretStr = Field(default=SecretStr(""), max_length=64)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: SecretStr = Field(default=SecretStr(""), max_length=128)
    new_password: SecretStr = Field(min_length=15, max_length=128)
    code: SecretStr = Field(default=SecretStr(""), max_length=64)


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    full_name: str = Field(min_length=1, max_length=160)
    phone: str = Field(default="", max_length=40)
    job_title: str = Field(default="", max_length=100)

    @field_validator("full_name", "phone", "job_title")
    @classmethod
    def clean_text(cls, value, info):
        if "\x00" in value:
            raise ValueError("Invalid profile text")
        if info.field_name == "full_name" and not value:
            raise ValueError("Name is required")
        return value


def cookies(response, request, tokens=None):
    settings = request.app.state.settings
    for kind, value, lifetime in [
        ("access", tokens[0] if tokens else "", ACCESS_SECONDS),
        ("refresh", tokens[1] if tokens else "", IDLE_SECONDS),
    ]:
        response.set_cookie(
            settings.cookie_name(kind),
            value,
            max_age=lifetime if tokens else 0,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="strict",
            path="/",
        )


@router.post("/auth/login", status_code=204)
async def login(body: Credentials, request: Request, response: Response):
    tokens = await service.login(
        request, str(body.email), body.password.get_secret_value(), body.code.get_secret_value()
    )
    cookies(response, request, tokens)


@router.post("/auth/refresh", status_code=204)
async def refresh(request: Request, response: Response):
    cookies(response, request, await service.refresh(request))


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response):
    await service.logout(request)
    cookies(response, request)


@router.get("/auth/me")
async def me(request: Request):
    user_id, session_id = request.state.identity
    async with request.app.state.db() as db:
        user = await db.get(User, user_id)
        session = await db.get(Session, session_id)
        memberships = (
            await db.execute(
                select(Membership, Organization)
                .join(Organization)
                .where(Membership.user_id == user_id, Membership.active.is_(True))
            )
        ).all()
        return {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "phone": user.phone,
            "job_title": user.job_title,
            "platform_role": "none"
            if getattr(request.state, "tenant_id", None)
            else user.platform_role,
            "email_verified": user.email_verified,
            "must_change_password": user.must_change_password,
            "mfa_enabled": bool(user.mfa_secret),
            "session_id": session_id,
            "step_up_expires": service.verification_expires(user, session),
            "require_action_verification": user.require_action_verification,
            "memberships": [
                {
                    "organization_id": org.id,
                    "name": org.name,
                    "slug": org.slug,
                    "image_url": org.image_url,
                    "organization_type": org.organization_type,
                    "permissions": membership.permissions,
                    "roles": membership.roles,
                    "all_stores": membership.all_stores,
                    "store_ids": membership.store_ids,
                }
                for membership, org in memberships
                if not getattr(request.state, "tenant_id", None)
                or org.id == request.state.tenant_id
            ],
        }


@router.post("/auth/profile")
async def update_profile(body: ProfileUpdate, request: Request):
    async with request.app.state.db() as db, db.begin():
        user, session = await service.locked_identity(request, db)
        before = {"full_name": user.full_name, "phone": user.phone, "job_title": user.job_title}
        user.full_name = body.full_name
        user.phone = body.phone
        user.job_title = body.job_title
        service.audit(
            db,
            "account.profile_updated",
            user.id,
            session.id,
            target_type="users",
            target_id=user.id,
            changes={"before": before, "after": body.model_dump()},
        )
        return {"full_name": user.full_name, "phone": user.phone, "job_title": user.job_title}


@router.post("/auth/password", status_code=204)
async def change_password(body: PasswordChange, request: Request, response: Response):
    await service.change_password(
        request,
        body.current_password.get_secret_value(),
        body.new_password.get_secret_value(),
        body.code.get_secret_value(),
    )
    cookies(response, request)


@router.get("/auth/sessions")
async def sessions(request: Request):
    async with request.app.state.db() as db:
        rows = await db.scalars(
            select(Session)
            .where(
                Session.user_id == request.state.identity[0],
                Session.revoked.is_(False),
                Session.expires > now(),
                Session.idle_expires > now(),
            )
            .order_by(Session.created.desc())
        )
        return [
            {
                "id": row.id,
                "created": row.created,
                "last_used": row.last_used,
                "expires": row.expires,
                "current": row.id == request.state.identity[1],
            }
            for row in rows
        ]


@router.post("/auth/sessions/{session_id}/revoke", status_code=204)
async def revoke(session_id: UUID, request: Request, response: Response):
    async with request.app.state.db() as db, db.begin():
        user, _ = await service.locked_identity(request, db)
        target = await db.scalar(
            select(Session).where(Session.id == session_id, Session.user_id == user.id)
        )
        if not target:
            raise HTTPException(404, "Session not found")
        await db.execute(update(Session).where(Session.id == target.id).values(revoked=True))
        service.audit(db, "session.revoked", user.id, target.id)
    if session_id == request.state.identity[1]:
        cookies(response, request)


@router.get("/organizations/{organization_id}")
async def organization(organization_id: UUID, request: Request):
    await service.require_permission(request, organization_id, "organization.read")
    async with request.app.state.db() as db:
        organization = await db.get(Organization, organization_id)
        return {"id": organization.id, "name": organization.name}
