from datetime import timedelta

import pyotp
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator
from sqlalchemy import update

from app.auth import onboarding, service
from app.auth.factors import accept_factor, backup_codes, cipher
from app.auth.mail import queue_mail
from app.auth.models import Session
from app.auth.routes import cookies
from app.auth.security import now, password_work, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["Identity"])


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmailInput(Input):
    email: EmailStr = Field(max_length=254)

    @field_validator("email", mode="before")
    @classmethod
    def normalize(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class LinkInput(Input):
    token: SecretStr = Field(min_length=40, max_length=128)


class SignupInput(LinkInput):
    password: SecretStr = Field(min_length=15, max_length=128)
    organization: str = Field(min_length=1, max_length=160)

    @field_validator("organization")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Organization is required")
        return value.strip()


class ResetInput(LinkInput):
    password: SecretStr = Field(min_length=15, max_length=128)
    code: SecretStr = Field(default=SecretStr(""), max_length=64)


class Reauthenticate(Input):
    password: SecretStr = Field(min_length=1, max_length=128)
    code: SecretStr = Field(default=SecretStr(""), max_length=64)


class CodeInput(Input):
    code: SecretStr = Field(min_length=6, max_length=64)


@router.get("/capabilities")
async def capabilities(request: Request):
    settings = request.app.state.settings
    return {"email": settings.email_enabled, "mfa": bool(settings.identity_encryption_key)}


@router.post("/signup/request", status_code=202)
async def signup_request(body: EmailInput, request: Request):
    return await onboarding.request_link(request, str(body.email), "signup")


@router.post("/recovery/request", status_code=202)
async def recovery_request(body: EmailInput, request: Request):
    return await onboarding.request_link(request, str(body.email), "reset")


@router.post("/signup/complete", status_code=204)
async def signup_complete(body: SignupInput, request: Request):
    await onboarding.finish(
        request,
        body.token.get_secret_value(),
        "signup",
        body.password.get_secret_value(),
        body.organization,
    )


@router.post("/recovery/complete", status_code=204)
async def recovery_complete(body: ResetInput, request: Request, response: Response):
    await onboarding.finish(
        request,
        body.token.get_secret_value(),
        "reset",
        body.password.get_secret_value(),
        code=body.code.get_secret_value(),
    )
    cookies(response, request)


@router.post("/email/complete", status_code=204)
async def email_complete(body: LinkInput, request: Request):
    await onboarding.finish(request, body.token.get_secret_value(), "verify")


async def reauthenticate(request, db, body):
    user, session = await service.locked_identity(request, db)
    if not await password_work(
        request, verify_password, user.password_hash, body.password.get_secret_value()
    ):
        raise HTTPException(400, "Invalid password or authentication code")
    if not accept_factor(request.app.state.settings, user, body.code.get_secret_value()):
        raise HTTPException(400, "Invalid password or authentication code")
    return user, session


@router.post("/email/request", status_code=202)
async def verify_request(body: Reauthenticate, request: Request):
    onboarding.require_email(request)
    await service.throttle(request, "security", str(request.state.identity[0]))
    async with request.app.state.db() as db, db.begin():
        user, _ = await reauthenticate(request, db, body)
        if not user.email_verified:
            await service.throttle(request, "email", user.email)
            onboarding.enqueue_link(db, request.app.state.settings, user.email, "verify", user)
            service.audit(db, "email.verification_requested", user.id)
    return {"message": onboarding.GENERIC_MESSAGE}


@router.post("/mfa/enroll")
async def enroll(body: Reauthenticate, request: Request):
    await service.throttle(request, "security", str(request.state.identity[0]))
    async with request.app.state.db() as db, db.begin():
        user, session = await reauthenticate(request, db, body)
        if not user.email_verified:
            raise HTTPException(400, "Verify your email before enabling MFA")
        if user.mfa_secret:
            raise HTTPException(409, "MFA is already enabled")
        secret = pyotp.random_base32()
        user.mfa_pending = cipher(request.app.state.settings).encrypt(secret.encode()).decode()
        user.mfa_pending_expires = now() + timedelta(minutes=10)
        service.audit(db, "mfa.enrollment_started", user.id, session.id)
        return {
            "secret": secret,
            "uri": pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="Ive POS"),
        }


def notify_security_change(db, request, user, change):
    if request.app.state.settings.email_enabled:
        queue_mail(
            db,
            request.app.state.settings,
            user.email,
            "Your Ive POS account security changed",
            f"{change} on your Ive POS account. "
            "Contact your administrator if you did not make this change.",
            sender="operations",
        )


@router.post("/mfa/confirm")
async def confirm(body: CodeInput, request: Request, response: Response):
    await service.throttle(request, "security", str(request.state.identity[0]))
    async with request.app.state.db() as db, db.begin():
        user, session = await service.locked_identity(request, db)
        if (
            user.mfa_secret
            or not user.mfa_pending
            or not user.mfa_pending_expires
            or user.mfa_pending_expires <= now()
        ):
            raise HTTPException(400, "Start MFA enrollment again")
        user.mfa_secret = user.mfa_pending
        user.mfa_last_step = -1
        if not accept_factor(
            request.app.state.settings, user, body.code.get_secret_value(), recovery=False
        ):
            raise HTTPException(400, "Invalid authentication code")
        user.mfa_pending = None
        user.mfa_pending_expires = None
        codes, user.recovery_hashes = backup_codes()
        await db.execute(update(Session).where(Session.user_id == user.id).values(revoked=True))
        service.audit(db, "mfa.enabled", user.id, session.id)
        notify_security_change(db, request, user, "MFA was enabled")
    cookies(response, request)
    return {"recovery_codes": codes}


@router.post("/mfa/disable", status_code=204)
async def disable(body: Reauthenticate, request: Request, response: Response):
    await service.throttle(request, "security", str(request.state.identity[0]))
    async with request.app.state.db() as db, db.begin():
        user, session = await reauthenticate(request, db, body)
        user.mfa_secret = None
        user.mfa_pending = None
        user.mfa_pending_expires = None
        user.mfa_last_step = -1
        user.recovery_hashes = []
        await db.execute(update(Session).where(Session.user_id == user.id).values(revoked=True))
        service.audit(db, "mfa.disabled", user.id, session.id)
        notify_security_change(db, request, user, "MFA was disabled")
    cookies(response, request)


@router.post("/mfa/recovery-codes")
async def regenerate(body: Reauthenticate, request: Request):
    await service.throttle(request, "security", str(request.state.identity[0]))
    async with request.app.state.db() as db, db.begin():
        user, session = await reauthenticate(request, db, body)
        if not user.mfa_secret:
            raise HTTPException(400, "Enable MFA first")
        codes, user.recovery_hashes = backup_codes()
        service.audit(db, "mfa.recovery_codes_replaced", user.id, session.id)
        notify_security_change(db, request, user, "Your MFA backup codes were replaced")
    return {"recovery_codes": codes}


@router.post("/step-up")
async def step_up(body: Reauthenticate, request: Request):
    await service.throttle(request, "security", str(request.state.identity[0]))
    async with request.app.state.db() as db, db.begin():
        user, session = await reauthenticate(request, db, body)
        if not user.email_verified or not user.mfa_secret:
            raise HTTPException(
                403, "Verify your email and enable MFA before changing business settings"
            )
        session.step_up_expires = min(now() + timedelta(minutes=5), session.expires)
        service.audit(db, "session.step_up", user.id, session.id)
        return {"expires": session.step_up_expires}
