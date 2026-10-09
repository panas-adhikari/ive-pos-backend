from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, SecretStr
from sqlalchemy import select, text

from app.auth import service
from app.auth.factors import accept_factor
from app.auth.identity_routes import Input, LinkInput
from app.auth.mail import queue_mail
from app.auth.models import Invitation, Membership, Organization, User
from app.auth.onboarding import require_email
from app.auth.permissions import OWNER_PERMISSIONS
from app.auth.security import PASSWORD_HASHER, digest, now, password_work, token, verify_password
from app.employees.routes import Access, Create, authorize, check_employee_limit, validate_grant
from app.tenancy.service import organization_origin

router = APIRouter(tags=["Onboarding"])


def record(invite):
    return {
        "id": invite.id,
        "email": invite.email,
        "kind": invite.kind,
        "status": "expired"
        if invite.status == "pending" and invite.expires <= now()
        else invite.status,
        "expires": invite.expires,
        "access": invite.access,
    }


def deliver(db, request, invite, organization):
    require_email(request)
    raw = token()
    invite.token_hash = digest(raw)
    invite.expires = now() + timedelta(minutes=30)
    invite.status = "pending"
    origin = organization_origin(request.app.state.settings, organization.slug)
    link = f"{origin}/login#identity=invite&token={raw}"
    queue_mail(
        db,
        request.app.state.settings,
        invite.email,
        f"Join {organization.name} on Ive POS",
        f"You are invited to join {organization.name} on Ive POS.\n\n{link}\n\n"
        "This link expires in 30 minutes. If you did not expect it, ignore it.",
        expires=invite.expires,
        action_url=link,
        action_label="Accept invitation",
    )


async def issue(db, request, org, actor_id, email, kind, access):
    invite = await db.scalar(
        select(Invitation).where(Invitation.organization_id == org.id, Invitation.email == email)
    )
    if invite and invite.status != "revoked":
        raise HTTPException(409, "An invitation already exists; resend or revoke it first")
    if not invite:
        invite = Invitation(organization_id=org.id, email=email)
        db.add(invite)
    invite.invited_by, invite.kind, invite.access = actor_id, kind, access
    deliver(db, request, invite, org)
    await db.flush()
    service.audit(
        db,
        "invitation.sent",
        actor_id,
        organization_id=org.id,
        target_type="invitations",
        target_id=invite.id,
    )
    return invite


@router.post("/api/v1/organizations/{organization_id}/invitations", status_code=201)
async def invite_staff(organization_id: UUID, body: Create, request: Request):
    async with request.app.state.db() as db, db.begin():
        actor = await authorize(db, request, organization_id, write=True)
        await validate_grant(db, organization_id, actor, body)
        if not body.active:
            raise HTTPException(422, "Invite employees with active access")
        existing = await db.scalar(
            select(Membership)
            .join(User)
            .where(Membership.organization_id == organization_id, User.email == str(body.email))
        )
        if existing:
            raise HTTPException(409, "This account already has a membership; edit its access")
        await check_employee_limit(db, organization_id, adding_active=True)
        org = await db.get(Organization, organization_id)
        return record(
            await issue(
                db,
                request,
                org,
                actor.user_id,
                str(body.email),
                "staff",
                body.model_dump(mode="json", exclude={"email"}),
            )
        )


async def manage(request, organization_id, db, platform, write=False):
    if not platform:
        return await authorize(db, request, organization_id, write=write)
    org = await db.scalar(
        select(Organization).where(Organization.id == organization_id).with_for_update()
    )
    actor, session = await service.locked_identity(request, db)
    if actor.platform_role != "super_admin":
        raise HTTPException(403, "Platform super administrator access required")
    if write and (
        not actor.email_verified
        or not actor.mfa_secret
        or not service.action_verified(actor, session)
    ):
        raise HTTPException(403, "Unlock changes with your password and MFA code first")
    if not org:
        raise HTTPException(404, "Organization not found")


@router.get("/api/v1/platform/organizations/{organization_id}/invitations")
@router.get("/api/v1/organizations/{organization_id}/invitations")
async def listing(organization_id: UUID, request: Request):
    platform = request.url.path.startswith("/api/v1/platform/")
    async with request.app.state.db() as db, db.begin():
        await manage(request, organization_id, db, platform)
        rows = await db.scalars(
            select(Invitation)
            .where(
                Invitation.organization_id == organization_id,
                Invitation.kind == ("owner" if platform else "staff"),
            )
            .order_by(Invitation.email)
        )
        return [record(row) for row in rows]


class Action(Input):
    action: str = Field(pattern="^(resend|revoke)$")


@router.post("/api/v1/platform/organizations/{organization_id}/invitations", status_code=201)
async def invite_unclaimed_owner(organization_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        await manage(request, organization_id, db, True, write=True)
        if await db.scalar(
            select(Membership.id).where(Membership.organization_id == organization_id)
        ):
            raise HTTPException(409, "This organization already has members")
        org = await db.get(Organization, organization_id)
        from pydantic import EmailStr, TypeAdapter, ValidationError

        try:
            email = str(TypeAdapter(EmailStr).validate_python(org.contact_email)).lower()
        except ValidationError:
            raise HTTPException(422, "Organization needs a valid owner contact email") from None
        return record(
            await issue(
                db,
                request,
                org,
                request.state.identity[0],
                email,
                "owner",
                {
                    "roles": ["owner"],
                    "permissions": list(OWNER_PERMISSIONS),
                    "all_stores": True,
                    "store_ids": [],
                    "active": True,
                },
            )
        )


@router.post("/api/v1/platform/organizations/{organization_id}/invitations/{invitation_id}")
@router.post("/api/v1/organizations/{organization_id}/invitations/{invitation_id}")
async def change(organization_id: UUID, invitation_id: UUID, body: Action, request: Request):
    platform = request.url.path.startswith("/api/v1/platform/")
    async with request.app.state.db() as db, db.begin():
        actor = await manage(request, organization_id, db, platform, write=True)
        invite = await db.scalar(
            select(Invitation)
            .where(
                Invitation.id == invitation_id,
                Invitation.organization_id == organization_id,
                Invitation.kind == ("owner" if platform else "staff"),
            )
            .with_for_update()
        )
        if not invite:
            raise HTTPException(404, "Invitation not found")
        if invite.status != "pending":
            raise HTTPException(409, "Invitation is already accepted or revoked")
        if actor:
            await validate_grant(db, organization_id, actor, Access(**invite.access))
        if body.action == "resend":
            await service.throttle(request, "invite-resend", str(invite.id))
            invite.invited_by = request.state.identity[0]
            deliver(db, request, invite, await db.get(Organization, organization_id))
        else:
            invite.status = "revoked"
        service.audit(
            db,
            f"invitation.{body.action}",
            request.state.identity[0],
            organization_id=organization_id,
            target_type="invitations",
            target_id=invite.id,
        )
        return record(invite)


class Accept(LinkInput):
    password: SecretStr = Field(min_length=1, max_length=128)
    code: SecretStr = Field(default=SecretStr(""), max_length=64)


@router.post("/api/v1/auth/invitations/inspect")
async def inspect(body: LinkInput, request: Request):
    await service.throttle(request, "invite-inspect", digest(body.token.get_secret_value()))
    async with request.app.state.db() as db:
        invite = await db.scalar(
            select(Invitation).where(Invitation.token_hash == digest(body.token.get_secret_value()))
        )
        if not invite or invite.status != "pending" or invite.expires <= now():
            raise HTTPException(400, "Invitation is invalid or expired")
        org = await db.get(Organization, invite.organization_id)
        existing = await db.scalar(select(User.id).where(User.email == invite.email))
        return {
            "organization": org.name,
            "email": invite.email,
            "existing_account": bool(existing),
            "roles": invite.access["roles"],
        }


@router.post("/api/v1/auth/invitations/accept", status_code=204)
async def accept(body: Accept, request: Request):
    raw = body.token.get_secret_value()
    await service.throttle(request, "invite-accept", digest(raw))
    async with request.app.state.db() as db, db.begin():
        invite = await db.scalar(select(Invitation).where(Invitation.token_hash == digest(raw)))
        if not invite:
            raise HTTPException(400, "Invitation is invalid or expired")
        await db.scalar(
            select(Organization).where(Organization.id == invite.organization_id).with_for_update()
        )
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(digest(invite.email)[:15], 16)}
        )
        await db.refresh(invite, with_for_update=True)
        if (
            invite.token_hash != digest(raw)
            or invite.status != "pending"
            or invite.expires <= now()
        ):
            raise HTTPException(400, "Invitation is invalid or expired")
        issuer = await db.get(User, invite.invited_by)
        if not issuer or not issuer.active:
            raise HTTPException(409, "Ask an administrator to resend this invitation")
        access = Access(**invite.access)
        if invite.kind == "owner":
            if issuer.platform_role != "super_admin":
                raise HTTPException(409, "Ask a platform administrator to resend this invitation")
        else:
            member = await db.scalar(
                select(Membership).where(
                    Membership.user_id == issuer.id,
                    Membership.organization_id == invite.organization_id,
                )
            )
            if (
                not member
                or not member.active
                or not member.all_stores
                or "employees.manage" not in member.permissions
            ):
                raise HTTPException(
                    409, "Ask an organization administrator to resend this invitation"
                )
            await validate_grant(db, invite.organization_id, member, access)
        await check_employee_limit(db, invite.organization_id, adding_active=True)
        await service.throttle(request, "invite-account", invite.email)
        user = await db.scalar(select(User).where(User.email == invite.email).with_for_update())
        password = body.password.get_secret_value()
        if user:
            if (
                not user.active
                or not await password_work(request, verify_password, user.password_hash, password)
                or not accept_factor(request.app.state.settings, user, body.code.get_secret_value())
            ):
                raise HTTPException(400, "Invalid password or authentication code")
            if await db.scalar(
                select(Membership.id).where(
                    Membership.user_id == user.id,
                    Membership.organization_id == invite.organization_id,
                )
            ):
                raise HTTPException(409, "You already belong to this organization")
        else:
            if len(password) < 15:
                raise HTTPException(422, "Use a password of 15–128 characters")
            user = User(
                email=invite.email,
                password_hash=await password_work(request, PASSWORD_HASHER.hash, password),
            )
            db.add(user)
            await db.flush()
        user.email_verified = True
        db.add(
            Membership(
                user_id=user.id, organization_id=invite.organization_id, **access.model_dump()
            )
        )
        invite.status = "accepted"
        service.audit(
            db,
            "invitation.accepted",
            user.id,
            organization_id=invite.organization_id,
            target_type="invitations",
            target_id=invite.id,
        )
