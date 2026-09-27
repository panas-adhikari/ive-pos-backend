from datetime import timedelta
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.auth.factors import accept_factor
from app.auth.models import AuditEvent, Membership, RateBucket, RefreshToken, Session, User
from app.auth.permissions import PLATFORM_ROLES
from app.auth.security import (
    ACCESS_SECONDS,
    DUMMY_HASH,
    IDLE_SECONDS,
    PASSWORD_HASHER,
    SESSION_SECONDS,
    digest,
    now,
    password_work,
    rate_key,
    token,
    verify_password,
)


def audit(
    db,
    action,
    user_id=None,
    session_id=None,
    organization_id=None,
    *,
    target_type=None,
    target_id=None,
    changes=None,
):
    db.add(
        AuditEvent(
            action=action,
            target_type=target_type,
            target_id=target_id,
            changes=changes,
            user_id=user_id,
            session_id=session_id,
            organization_id=organization_id,
            created=now(),
        )
    )


async def throttle(request: Request, category: str, account: str | None = None):
    settings = request.app.state.settings
    # Uvicorn accepts forwarding headers only from explicitly configured proxy peers.
    peer = request.client.host if request.client else "unknown"
    window = int(now().timestamp()) // 900
    limits = [(f"{category}:ip:{peer}", 30 if category in {"login", "email"} else 120)]
    if account is not None:
        limits.append((f"{category}:account:{account}", 10))
    blocked = False
    async with request.app.state.db() as db, db.begin():
        for identifier, limit in limits:
            key = rate_key(settings.auth_secret.get_secret_value(), identifier)
            statement = insert(RateBucket).values(key=key, window=window, count=1)
            count = await db.scalar(
                statement.on_conflict_do_update(
                    index_elements=["key", "window"], set_={"count": RateBucket.count + 1}
                ).returning(RateBucket.count)
            )
            blocked |= count > limit
    if blocked:
        raise HTTPException(
            429,
            "Too many attempts; try again later",
            headers={"Retry-After": str(900 - int(now().timestamp()) % 900)},
        )


def session_valid(session, user):
    timestamp = now()
    return (
        session is not None
        and user is not None
        and user.active
        and not session.revoked
        and session.expires > timestamp
        and session.idle_expires > timestamp
    )


async def has_console_access(db, user_id):
    user = await db.get(User, user_id)
    if user and user.platform_role in PLATFORM_ROLES - {"none"}:
        return True
    return bool(
        await db.scalar(
            select(Membership.id)
            .where(Membership.user_id == user_id, Membership.active.is_(True))
            .limit(1)
        )
    )


async def access_identity(request: Request):
    raw = request.cookies.get(request.app.state.settings.cookie_name("access"), "")
    if not raw or len(raw) > 128:
        raise HTTPException(401, "Authentication required")
    async with request.app.state.db() as db:
        result = (
            await db.execute(
                select(Session, User).join(User).where(Session.access_hash == digest(raw))
            )
        ).first()
        if result is None or not session_valid(*result) or result.Session.access_expires <= now():
            raise HTTPException(401, "Authentication required")
        if not await has_console_access(db, result.User.id):
            raise HTTPException(401, "Authentication required")
        if result.User.must_change_password and (request.method, request.url.path) not in {
            ("GET", "/api/v1/auth/me"),
            ("POST", "/api/v1/auth/password"),
        }:
            raise HTTPException(403, "Change your temporary password to continue")
        return result.User.id, result.Session.id


# FastAPI's global dependency makes newly added endpoints protected by default.
PUBLIC_ENDPOINTS = {
    ("POST", "/api/v1/auth/invitations/inspect"),
    ("POST", "/api/v1/auth/invitations/accept"),
    ("GET", "/api/v1/auth/capabilities"),
    ("POST", "/api/v1/auth/signup/request"),
    ("POST", "/api/v1/auth/signup/complete"),
    ("POST", "/api/v1/auth/recovery/request"),
    ("POST", "/api/v1/auth/recovery/complete"),
    ("POST", "/api/v1/auth/email/complete"),
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/ready"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/refresh"),
    ("POST", "/api/v1/auth/logout"),
}


async def require_auth(request: Request):
    if (request.method, request.url.path) not in PUBLIC_ENDPOINTS:
        request.state.identity = await access_identity(request)


async def scoped_permission(db, request, organization_id, permission, store_id=None):
    from app.auth.permissions import ORGANIZATION_PERMISSIONS
    from app.stores.models import Store

    membership = await db.scalar(
        select(Membership).where(
            Membership.user_id == request.state.identity[0],
            Membership.organization_id == organization_id,
            Membership.active.is_(True),
        )
    )
    if membership is None or permission not in membership.permissions:
        raise HTTPException(403, "Access denied")
    if permission in ORGANIZATION_PERMISSIONS and not membership.all_stores:
        raise HTTPException(403, "Organization-wide access required")
    if store_id is not None:
        if not membership.all_stores and store_id not in membership.store_ids:
            raise HTTPException(403, "Store access denied")
        store = await db.scalar(
            select(Store).where(
                Store.id == store_id,
                Store.organization_id == organization_id,
                Store.active.is_(True),
            )
        )
        if not store:
            raise HTTPException(403, "Active store access required")
    return membership


async def require_permission(request: Request, organization_id: UUID, permission: str):
    async with request.app.state.db() as db:
        return await scoped_permission(db, request, organization_id, permission)


async def require_platform_role(request: Request, *roles: str):
    async with request.app.state.db() as db:
        user = await db.get(User, request.state.identity[0])
        if not user or user.platform_role not in roles:
            raise HTTPException(403, "Platform access denied")
        return user


async def issue_tokens(db, session):
    access, refresh = token(), token()
    timestamp = now()
    session.access_hash = digest(access)
    session.access_expires = min(timestamp + timedelta(seconds=ACCESS_SECONDS), session.expires)
    session.idle_expires = min(timestamp + timedelta(seconds=IDLE_SECONDS), session.expires)
    session.last_used = timestamp
    await db.flush()
    db.add(RefreshToken(token_hash=digest(refresh), session_id=session.id))
    return access, refresh


async def login(request, email, password, code=""):
    await throttle(request, "login", email)
    async with request.app.state.db() as db, db.begin():
        user = await db.scalar(select(User).where(User.email == email).with_for_update())
        valid = await password_work(
            request, verify_password, user.password_hash if user else DUMMY_HASH, password
        )
        if not valid or not user or not user.active:
            audit(db, "login.failed")
            result = None
        elif not await has_console_access(db, user.id):
            audit(db, "login.failed")
            result = None
        elif not accept_factor(request.app.state.settings, user, code):
            audit(db, "login.factor_rejected", user.id)
            result = None
        else:
            if PASSWORD_HASHER.check_needs_rehash(user.password_hash):
                user.password_hash = await password_work(request, PASSWORD_HASHER.hash, password)
            active = list(
                await db.scalars(
                    select(Session)
                    .where(
                        Session.user_id == user.id,
                        Session.revoked.is_(False),
                        Session.expires > now(),
                        Session.idle_expires > now(),
                    )
                    .order_by(Session.created.desc())
                )
            )
            for older in active[9:]:
                older.revoked = True
                audit(db, "session.limit_revoked", user.id, older.id)
            session = Session(
                user_id=user.id, created=now(), expires=now() + timedelta(seconds=SESSION_SECONDS)
            )
            db.add(session)
            result = await issue_tokens(db, session)
            audit(db, "login.succeeded", user.id, session.id)
    if result is None:
        raise HTTPException(401, "Invalid email or password")
    return result


async def refresh(request):
    await throttle(request, "refresh")
    raw = request.cookies.get(request.app.state.settings.cookie_name("refresh"), "")
    if not raw or len(raw) > 128:
        raise HTTPException(401, "Authentication required")
    result = None
    async with request.app.state.db() as db, db.begin():
        record = await db.get(RefreshToken, digest(raw))
        if record:
            # Always lock user before session, including password changes/revocations.
            user_id = await db.scalar(
                select(Session.user_id).where(Session.id == record.session_id)
            )
            user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
            session = await db.scalar(
                select(Session).where(Session.id == record.session_id).with_for_update()
            )
            await db.refresh(record)  # Observe concurrent rotation after the lock was acquired.
            if record.used:
                session.revoked = True
                audit(db, "refresh.replay", user_id, session.id)
            elif session_valid(session, user) and await has_console_access(db, user.id):
                record.used = True
                result = await issue_tokens(db, session)
                audit(db, "refresh.rotated", user_id, session.id)
    # Raise only after the transaction commits: replay revocation must survive the 401.
    if result is None:
        raise HTTPException(401, "Authentication required")
    return result


async def logout(request):
    raw = request.cookies.get(request.app.state.settings.cookie_name("refresh"), "")
    access = request.cookies.get(request.app.state.settings.cookie_name("access"), "")
    async with request.app.state.db() as db, db.begin():
        record = await db.get(RefreshToken, digest(raw)) if raw and len(raw) <= 128 else None
        session = await db.get(Session, record.session_id) if record else None
        if session is None and access and len(access) <= 128:
            session = await db.scalar(select(Session).where(Session.access_hash == digest(access)))
        if session:
            await db.scalar(select(User).where(User.id == session.user_id).with_for_update())
            await db.execute(update(Session).where(Session.id == session.id).values(revoked=True))
            audit(db, "logout", session.user_id, session.id)


async def locked_identity(request, db):
    user_id, session_id = request.state.identity
    user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    session = await db.scalar(select(Session).where(Session.id == session_id).with_for_update())
    raw = request.cookies.get(request.app.state.settings.cookie_name("access"), "")
    if (
        not session_valid(session, user)
        or session.access_expires <= now()
        or session.access_hash != digest(raw)
    ):
        raise HTTPException(401, "Authentication required")
    return user, session


async def change_password(request, current_password, new_password, code=""):
    await throttle(request, "password", str(request.state.identity[0]))
    accepted = False
    async with request.app.state.db() as db, db.begin():
        user, session = await locked_identity(request, db)
        if await password_work(
            request, verify_password, user.password_hash, current_password
        ) and accept_factor(request.app.state.settings, user, code) and (
            not user.must_change_password or current_password != new_password
        ):
            user.password_hash = await password_work(request, PASSWORD_HASHER.hash, new_password)
            user.must_change_password = False
            user.mfa_pending = None
            user.mfa_pending_expires = None
            await db.execute(update(Session).where(Session.user_id == user.id).values(revoked=True))
            audit(db, "password.changed", user.id, session.id)
            accepted = True
        else:
            audit(db, "password.rejected", user.id, session.id)
    if not accepted:
        raise HTTPException(400, "Current password or authentication code is incorrect")
