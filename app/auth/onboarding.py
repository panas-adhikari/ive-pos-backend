from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select, text, update

from app.auth import service
from app.auth.factors import accept_factor
from app.auth.mail import queue_mail
from app.auth.models import EmailChallenge, Membership, Organization, Session, User
from app.auth.permissions import OWNER_PERMISSIONS
from app.auth.security import PASSWORD_HASHER, digest, now, password_work, token

GENERIC_MESSAGE = "If this address is eligible, an email will arrive shortly."


def require_email(request):
    if not request.app.state.settings.email_enabled:
        raise HTTPException(503, "Email delivery is not configured")


async def request_link(request, email, purpose):
    require_email(request)
    await service.throttle(request, "email", email)
    async with request.app.state.db() as db, db.begin():
        user = await db.scalar(select(User).where(User.email == email))
        eligible = (purpose == "signup" and user is None) or (
            purpose == "reset" and user and user.active and user.email_verified
        )
        if eligible:
            enqueue_link(db, request.app.state.settings, email, purpose, user)
        service.audit(db, f"email.{purpose}_requested")
    return {"message": GENERIC_MESSAGE}


def enqueue_link(db, settings, email, purpose, user=None):
    raw = token()
    expires = now() + timedelta(minutes=30)
    db.add(
        EmailChallenge(
            token_hash=digest(raw),
            email=email,
            purpose=purpose,
            user_id=user.id if user else None,
            credential_hash=digest(user.password_hash) if user else None,
            expires=expires,
        )
    )
    link = f"{settings.public_origin}/#identity={purpose}&token={raw}"
    subject, introduction = {
        "signup": (
            "Create your Ive POS account",
            "Create your Ive POS account using the link below.",
        ),
        "reset": (
            "Reset your Ive POS password",
            "We received a request to reset your Ive POS password.",
        ),
        "verify": (
            "Verify your Ive POS email",
            "Verify your email address for your Ive POS account.",
        ),
    }[purpose]
    queue_mail(
        db,
        settings,
        email,
        subject,
        f"{introduction}\n\n{link}\n\n"
        "This link expires in 30 minutes and can be used once. "
        "If you did not request it, ignore this email.",
        expires=expires,
        sender="operations" if purpose == "reset" else "onboard",
        action_url=link,
        action_label={
            "signup": "Create account",
            "reset": "Reset password",
            "verify": "Verify email",
        }[purpose],
    )


async def finish(request, raw, purpose, password=None, organization=None, code=""):
    require_email(request)
    await service.throttle(request, "email-complete", digest(raw))
    success = False
    async with request.app.state.db() as db, db.begin():
        initial = await db.get(EmailChallenge, digest(raw))
        if initial is None:
            raise HTTPException(400, "Link is invalid or expired")
        # Serialize all completions for an email, including concurrent signup tokens.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(digest(initial.email)[:15], 16)}
        )
        user = await db.scalar(select(User).where(User.email == initial.email).with_for_update())
        await db.refresh(initial, with_for_update=True)
        valid = not initial.used and initial.expires > now() and initial.purpose == purpose
        if purpose == "signup":
            valid = valid and user is None
        else:
            valid = (
                valid
                and user is not None
                and user.active
                and initial.user_id == user.id
                and initial.credential_hash == digest(user.password_hash)
            )
        if valid and purpose == "reset":
            await service.throttle(request, "reset-factor", str(user.id))
            valid = user.email_verified and accept_factor(request.app.state.settings, user, code)
        if valid:
            if purpose == "signup":
                user = User(
                    email=initial.email,
                    email_verified=True,
                    password_hash=await password_work(request, PASSWORD_HASHER.hash, password),
                )
                org = Organization(name=organization)
                db.add_all([user, org])
                await db.flush()
                db.add(
                    Membership(
                        user_id=user.id,
                        organization_id=org.id,
                        permissions=list(OWNER_PERMISSIONS),
                        roles=["owner"],
                    )
                )
                service.audit(db, "owner.signup", user.id, organization_id=org.id)
            elif purpose == "reset":
                user.password_hash = await password_work(request, PASSWORD_HASHER.hash, password)
                user.mfa_pending = None
                user.mfa_pending_expires = None
                await db.execute(
                    update(Session).where(Session.user_id == user.id).values(revoked=True)
                )
                queue_mail(
                    db,
                    request.app.state.settings,
                    user.email,
                    "Your Ive POS password was reset",
                    "Your Ive POS password was reset. All sessions were signed out. "
                    "Contact your administrator if you did not make this change.",
                    sender="operations",
                )
                service.audit(db, "password.reset", user.id)
            else:
                user.email_verified = True
                service.audit(db, "email.verified", user.id)
            await db.execute(
                update(EmailChallenge)
                .where(EmailChallenge.email == initial.email, EmailChallenge.purpose == purpose)
                .values(used=True)
            )
            success = True
        else:
            service.audit(db, "email.completion_rejected")
    if not success:
        raise HTTPException(400, "Link or authentication code is invalid or expired")
