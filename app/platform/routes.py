import asyncio
import json
import secrets
import time
from datetime import timedelta
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request as URLRequest
from urllib.request import urlopen
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from sqlalchemy import func, select, text, update

from app.auth import onboarding, service
from app.auth.mail import queue_mail
from app.auth.models import AuditEvent, EmailChallenge, Membership, Organization, Session, User
from app.auth.permissions import OWNER_PERMISSIONS
from app.auth.security import PASSWORD_HASHER, digest, now, password_work, verify_password
from app.platform.deletion import purge_organization
from app.stores.routes import CURRENCIES, TIMEZONES
from app.tenancy.service import (
    allocate_slug,
    organization_origin,
    subdomain_suggestions,
    validate_name_slug,
    validate_slug,
)

router = APIRouter(prefix="/api/v1/platform", tags=["Platform control"])
_geocoder_lock = asyncio.Lock()
_geocoder_cache: dict[str, tuple[float, list[dict[str, str | float]]]] = {}


class PlatformEmployee(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr = Field(max_length=254)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class OrganizationLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    store_limit: int = Field(ge=1, le=10_000)
    employee_limit: int = Field(ge=1, le=100_000)


class OrganizationSubdomain(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    slug: str | None = Field(default=None, max_length=63)
    expected_version: int = Field(ge=1)


class OwnerPasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class OrganizationInvite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str | None = Field(default=None, max_length=63)

    @field_validator("slug")
    @classmethod
    def valid_slug(cls, value):
        return validate_slug(value) if value else None

    @model_validator(mode="after")
    def name_based_subdomain(self):
        if self.slug:
            self.slug = validate_name_slug(self.name, self.slug)
        return self

    name: str = Field(min_length=1, max_length=160)
    owner_email: EmailStr = Field(max_length=254)
    owner_email_confirmed: bool = True
    owner_temporary_password: SecretStr = Field(min_length=15, max_length=128)
    owner_name: str = Field(default="Organization administrator", min_length=1, max_length=160)
    owner_phone: str = Field(default="", max_length=40)
    phone: str | None = Field(default=None, max_length=40)
    owner_title: str = Field(default="", max_length=100)
    organization_type: Literal["retail", "wholesale", "other"] = "retail"
    image_url: str = Field(default="", max_length=1000)
    website_url: str = Field(default="", max_length=300)
    location_label: str = Field(default="Not provided", min_length=1, max_length=240)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    currency: str = Field(default="NPR", min_length=3, max_length=3)
    timezone: str = Field(default="Asia/Kathmandu", min_length=1, max_length=80)
    store_limit: int = Field(default=1, ge=1, le=10_000)
    employee_limit: int = Field(default=5, ge=1, le=100_000)

    @field_validator("name", "owner_name", "location_label")
    @classmethod
    def normalize_required_text(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("This field is required")
        return value

    @field_validator("currency")
    @classmethod
    def valid_currency(cls, value):
        value = value.strip().upper()
        if value not in CURRENCIES:
            raise ValueError("Unsupported currency")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        value = value.strip()
        if value not in TIMEZONES:
            raise ValueError("Use an IANA timezone")
        return value

    @field_validator("image_url", "website_url")
    @classmethod
    def safe_optional_url(cls, value):
        value = value.strip()
        if value:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("Use a valid http or https URL")
        return value

    @field_validator("owner_phone", "owner_title")
    @classmethod
    def normalize_optional_text(cls, value):
        return value.strip()

    @model_validator(mode="after")
    def complete_map_coordinates(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Choose a complete map location")
        return self

    @field_validator("owner_email", mode="before")
    @classmethod
    def normalize_owner_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("owner_email_confirmed")
    @classmethod
    def require_owner_email_confirmation(cls, value):
        if not value:
            raise ValueError("Confirm the owner's email address before provisioning the account")
        return value


class OrganizationDeletion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    confirm_name: str = Field(min_length=1, max_length=160)


class OwnerCredentialsEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr = Field(min_length=15, max_length=128)


def require_super_admin(actor):
    if actor.platform_role != "super_admin":
        raise HTTPException(403, "Platform super administrator access required")


def require_step_up(actor: User, session: Session):
    if not service.action_verified(actor, session):
        raise HTTPException(403, "Verify your password and MFA before changing platform data")


@router.get("/me")
async def platform_me(request: Request):
    user = await service.require_platform_role(request, "super_admin", "employee")
    return {"email": user.email, "role": user.platform_role}


@router.get("/options")
async def platform_options(request: Request):
    await service.require_platform_role(request, "super_admin", "employee")
    return {"currencies": CURRENCIES, "timezones": TIMEZONES}


@router.get("/geocode")
async def geocode_city(request: Request, q: str = Query(min_length=2, max_length=100)):
    await service.require_platform_role(request, "super_admin")
    query = " ".join(q.split())
    if len(query) < 2:
        raise HTTPException(422, "Enter at least two characters")
    await service.throttle(request, "platform-geocode")
    cache_key = query.casefold()
    async with _geocoder_lock:
        cached = _geocoder_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return {"results": cached[1]}
        settings = request.app.state.settings
        url = f"{settings.geocoder_url}?{urlencode({'format': 'jsonv2', 'limit': 5, 'q': query})}"
        external = URLRequest(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": f"IvePOS/0.1 ({settings.public_origin})",
            },
        )
        try:
            async with request.app.state.db() as db, db.begin():
                await db.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": 904506683372797880}
                )
                started = time.monotonic()
                try:

                    def fetch_places():
                        with urlopen(external, timeout=8) as response:
                            return json.loads(response.read(64_000))

                    payload = await asyncio.to_thread(fetch_places)
                finally:
                    await asyncio.sleep(max(0, 1 - (time.monotonic() - started)))
        except (HTTPError, URLError, TimeoutError, ValueError):
            raise HTTPException(502, "City search is temporarily unavailable") from None
        results = []
        for place in payload if isinstance(payload, list) else []:
            try:
                results.append(
                    {
                        "name": str(place.get("display_name", ""))[:240],
                        "latitude": float(place["lat"]),
                        "longitude": float(place["lon"]),
                    }
                )
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
        _geocoder_cache[cache_key] = (time.monotonic() + 86_400, results)
        if len(_geocoder_cache) > 256:
            expired = [
                key for key, (expires, _) in _geocoder_cache.items() if expires <= time.monotonic()
            ]
            for key in expired:
                _geocoder_cache.pop(key, None)
            while len(_geocoder_cache) > 256:
                _geocoder_cache.pop(next(iter(_geocoder_cache)))
    return {"results": results}


@router.post("/organizations", status_code=201)
async def invite_organization(body: OrganizationInvite, request: Request):
    async with request.app.state.db() as db, db.begin():
        actor, session = await service.locked_identity(request, db)
        require_super_admin(actor)
        require_step_up(actor, session)
        owner_email = str(body.owner_email)
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": int(digest(owner_email)[:15], 16)},
        )
        if await db.scalar(select(User.id).where(User.email == owner_email).with_for_update()):
            raise HTTPException(
                409, "An account already uses this email. Choose another owner email."
            )
        organization = Organization(
            name=body.name,
            slug=await allocate_slug(db, body.name, body.slug)
            if body.slug
            else f"org-{uuid4().hex}",
            subdomain_enabled=bool(body.slug),
            contact_email=owner_email,
            phone=body.owner_phone or body.phone or "",
            organization_type=body.organization_type,
            image_url=body.image_url,
            website_url=body.website_url,
            location_label=body.location_label,
            latitude=body.latitude,
            longitude=body.longitude,
            currency=body.currency.upper(),
            timezone=body.timezone,
            store_limit=body.store_limit,
            employee_limit=body.employee_limit,
        )
        user = User(
            email=owner_email,
            full_name=body.owner_name,
            phone=body.owner_phone or body.phone or "",
            job_title=body.owner_title,
            password_hash=await password_work(
                request, PASSWORD_HASHER.hash, body.owner_temporary_password.get_secret_value()
            ),
            email_verified=True,
            must_change_password=True,
        )
        db.add_all([organization, user])
        await db.flush()
        db.add(
            Membership(
                user_id=user.id,
                organization_id=organization.id,
                permissions=list(OWNER_PERMISSIONS),
                roles=["owner"],
                all_stores=True,
                store_ids=[],
                active=True,
            )
        )
        service.audit(
            db,
            "platform.organization_provisioned",
            actor.id,
            session.id,
            organization_id=organization.id,
            target_type="organizations",
            target_id=organization.id,
            changes={
                "owner_email": owner_email,
                "organization_type": body.organization_type,
                "store_limit": body.store_limit,
                "employee_limit": body.employee_limit,
                "owner_access": "temporary_password",
            },
        )
        return {
            "id": organization.id,
            "name": organization.name,
            "configured": False,
            "slug": organization.slug if organization.subdomain_enabled else None,
            "subdomain_enabled": organization.subdomain_enabled,
            "login_url": organization_origin(
                request.app.state.settings, organization.slug, organization.subdomain_enabled
            )
            + "/login",
        }


@router.post("/organizations/{organization_id}/owner-credentials-email", status_code=202)
async def email_owner_credentials(
    body: OwnerCredentialsEmail, organization_id: UUID, request: Request
):
    onboarding.require_email(request)
    await service.throttle(request, "platform-owner-credentials", str(organization_id))
    async with request.app.state.db() as db, db.begin():
        actor, session = await service.locked_identity(request, db)
        require_super_admin(actor)
        require_step_up(actor, session)
        organization = await db.scalar(
            select(Organization).where(Organization.id == organization_id).with_for_update()
        )
        if not organization:
            raise HTTPException(404, "Organization not found")
        owner = await db.scalar(
            select(User).where(User.email == organization.contact_email).with_for_update()
        )
        password = body.password.get_secret_value()
        if (
            not owner
            or not owner.active
            or not owner.must_change_password
            or not await password_work(request, verify_password, owner.password_hash, password)
        ):
            raise HTTPException(400, "Temporary sign-in details are no longer valid")
        login_url = (
            organization_origin(
                request.app.state.settings, organization.slug, organization.subdomain_enabled
            )
            + "/login"
        )
        queue_mail(
            db,
            request.app.state.settings,
            owner.email,
            f"Your {organization.name} administrator account on Ive POS",
            f"Hello {owner.full_name},\n\n"
            f"Your administrator account for {organization.name} is ready.\n\n"
            f"Email: {owner.email}\nTemporary password: {password}\n\n"
            f"Sign in at {login_url}. "
            "You must choose a new password "
            "before continuing. Keep these sign-in details private "
            "and delete this email after use.",
            expires=now() + timedelta(hours=1),
            action_url=login_url,
            action_label="Sign in to Ive POS",
        )
        service.audit(
            db,
            "platform.owner_credentials_emailed",
            actor.id,
            session.id,
            organization_id=organization.id,
            target_type="users",
            target_id=owner.id,
            changes={"delivery": "email_outbox"},
        )
    return {"status": "queued"}


@router.post("/organizations/{organization_id}/owner-password-reset")
async def reset_owner_password(body: OwnerPasswordReset, organization_id: UUID, request: Request):
    await service.require_platform_role(request, "super_admin")
    await service.throttle(request, "platform-owner-password-reset", str(organization_id))
    async with request.app.state.db() as db, db.begin():
        actor, session = await service.locked_identity(request, db)
        require_super_admin(actor)
        require_step_up(actor, session)
        organization = await db.scalar(
            select(Organization).where(Organization.id == organization_id).with_for_update()
        )
        if not organization:
            raise HTTPException(404, "Organization not found")
        if organization.deletion_scheduled_for:
            raise HTTPException(409, "This organization is scheduled for deletion")
        if organization.version != body.expected_version:
            raise HTTPException(409, "Organization changed. Refresh before resetting the password.")
        owner = await db.scalar(
            select(User).where(User.email == organization.contact_email).with_for_update()
        )
        membership = (
            await db.scalar(
                select(Membership)
                .where(
                    Membership.organization_id == organization.id,
                    Membership.user_id == owner.id,
                    Membership.active.is_(True),
                )
                .with_for_update()
            )
            if owner
            else None
        )
        if (
            not owner
            or not owner.active
            or not membership
            or not (
                "owner" in membership.roles
                or membership.all_stores
                and set(OWNER_PERMISSIONS).issubset(membership.permissions)
            )
        ):
            raise HTTPException(409, "No active organization administrator matches this email.")
        if owner.id == actor.id or owner.platform_role != "none":
            raise HTTPException(409, "Platform accounts must use their own password recovery.")
        temporary = secrets.token_urlsafe(24)
        owner.password_hash = await password_work(request, PASSWORD_HASHER.hash, temporary)
        owner.must_change_password = True
        revoked = await db.execute(
            update(Session)
            .where(Session.user_id == owner.id, Session.revoked.is_(False))
            .values(revoked=True)
        )
        await db.execute(
            update(EmailChallenge)
            .where(EmailChallenge.user_id == owner.id, EmailChallenge.purpose == "reset")
            .values(used=True)
        )
        service.audit(
            db,
            "platform.owner_password_reset",
            actor.id,
            session.id,
            organization_id=organization.id,
            target_type="users",
            target_id=owner.id,
            changes={"must_change_password": True, "sessions_revoked": revoked.rowcount},
        )
        return {
            "id": organization.id,
            "organization": organization.name,
            "email": owner.email,
            "temporary_password": temporary,
            "login_url": organization_origin(
                request.app.state.settings, organization.slug, organization.subdomain_enabled
            )
            + "/login",
        }


@router.get("/organizations")
async def organizations(request: Request):
    await service.require_platform_role(request, "super_admin", "employee")
    async with request.app.state.db() as db:
        rows = await db.execute(
            select(
                Organization,
                func.count(Membership.id)
                .filter(Membership.active.is_(True))
                .label("active_employees"),
            )
            .outerjoin(Membership, Membership.organization_id == Organization.id)
            .group_by(Organization.id)
            .order_by(Organization.name)
        )
        return [
            {
                "id": organization.id,
                "name": organization.name,
                "organization_type": organization.organization_type,
                "image_url": organization.image_url,
                "slug": organization.slug if organization.subdomain_enabled else None,
                "subdomain_enabled": organization.subdomain_enabled,
                "login_url": organization_origin(
                    request.app.state.settings, organization.slug, organization.subdomain_enabled
                )
                + "/login",
                "location_label": organization.location_label,
                "configured": organization.configured,
                "billing_plan": organization.billing_plan,
                "billing_amount_minor": organization.billing_amount_minor,
                "billing_currency": organization.billing_currency,
                "billing_interval": organization.billing_interval,
                "store_limit": organization.store_limit,
                "employee_limit": organization.employee_limit,
                "active_employees": active_employees,
                "deletion_scheduled_for": organization.deletion_scheduled_for,
            }
            for organization, active_employees in rows
        ]


@router.get("/organizations/{organization_id}")
async def organization_detail(organization_id: UUID, request: Request):
    await service.require_platform_role(request, "super_admin", "employee")
    async with request.app.state.db() as db:
        organization = await db.get(Organization, organization_id)
        if not organization:
            raise HTTPException(404, "Organization not found")
        active_employees = await db.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.organization_id == organization.id, Membership.active.is_(True))
        )
        from app.stores.models import Store

        stores = await db.scalar(
            select(func.count()).select_from(Store).where(Store.organization_id == organization.id)
        )
        owner = await db.scalar(select(User).where(User.email == organization.contact_email))
        limit_requests = list(
            await db.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.organization_id == organization.id,
                    AuditEvent.action == "organization.store_limit_requested",
                )
                .order_by(AuditEvent.created.desc())
                .limit(5)
            )
        )
        return {
            "id": organization.id,
            "name": organization.name,
            "organization_type": organization.organization_type,
            "image_url": organization.image_url,
            "slug": organization.slug if organization.subdomain_enabled else None,
            "subdomain_enabled": organization.subdomain_enabled,
            "login_url": organization_origin(
                request.app.state.settings, organization.slug, organization.subdomain_enabled
            )
            + "/login",
            "website_url": organization.website_url,
            "location_label": organization.location_label,
            "latitude": organization.latitude,
            "longitude": organization.longitude,
            "contact_email": organization.contact_email,
            "phone": organization.phone,
            "owner_name": owner.full_name if owner else "",
            "owner_phone": owner.phone if owner else "",
            "owner_title": owner.job_title if owner else "",
            "currency": organization.currency,
            "timezone": organization.timezone,
            "configured": organization.configured,
            "store_limit": organization.store_limit,
            "employee_limit": organization.employee_limit,
            "active_employees": active_employees,
            "stores": stores,
            "billing_tier": organization.billing_plan,
            "billing_plan": organization.billing_plan,
            "billing_amount_minor": organization.billing_amount_minor,
            "billing_currency": organization.billing_currency,
            "billing_interval": organization.billing_interval,
            "version": organization.version,
            "deletion_scheduled_for": organization.deletion_scheduled_for,
            "store_limit_requests": [
                {"id": row.id, "created": row.created, **(row.changes or {})}
                for row in limit_requests
                if (row.changes or {}).get("requested_limit", 0) > organization.store_limit
            ],
        }


@router.get("/organizations/{organization_id}/subdomain-options")
async def subdomain_options(organization_id: UUID, request: Request):
    await service.require_platform_role(request, "super_admin", "employee")
    async with request.app.state.db() as db:
        organization = await db.get(Organization, organization_id)
        if not organization:
            raise HTTPException(404, "Organization not found")
        suggestions = subdomain_suggestions(organization.name)
        taken = set(
            await db.scalars(
                select(Organization.slug).where(
                    Organization.slug.in_(suggestions), Organization.id != organization.id
                )
            )
        )
        return {
            "domain": request.app.state.settings.tenant_base_domain,
            "suggestions": [{"slug": slug, "available": slug not in taken} for slug in suggestions],
        }


@router.put("/organizations/{organization_id}/subdomain")
async def configure_subdomain(body: OrganizationSubdomain, organization_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        actor, session = await service.locked_identity(request, db)
        require_super_admin(actor)
        require_step_up(actor, session)
        # Match the allocator's lock order before locking an organization row.
        await db.execute(text("SELECT pg_advisory_xact_lock(736192801)"))
        organization = await db.scalar(
            select(Organization).where(Organization.id == organization_id).with_for_update()
        )
        if not organization:
            raise HTTPException(404, "Organization not found")
        if organization.deletion_scheduled_for:
            raise HTTPException(409, "This organization is scheduled for deletion")
        if organization.version != body.expected_version:
            raise HTTPException(409, "Organization changed. Refresh before saving the subdomain.")
        before = {"enabled": organization.subdomain_enabled, "slug": organization.slug}
        if body.enabled:
            if not request.app.state.settings.tenant_base_domain:
                raise HTTPException(
                    503, "Organization subdomain sign-in is not configured on this platform."
                )
            try:
                slug = validate_name_slug(organization.name, body.slug or "")
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            organization.slug = await allocate_slug(
                db, organization.name, slug, exclude_id=organization.id
            )
        organization.subdomain_enabled = body.enabled
        organization.version += 1
        service.audit(
            db,
            "platform.organization_subdomain_updated",
            actor.id,
            session.id,
            organization_id=organization.id,
            target_type="organizations",
            target_id=organization.id,
            changes={
                "before": before,
                "after": {"enabled": body.enabled, "slug": organization.slug},
            },
        )
        return {
            "subdomain_enabled": organization.subdomain_enabled,
            "slug": organization.slug if organization.subdomain_enabled else None,
            "version": organization.version,
            "login_url": organization_origin(
                request.app.state.settings, organization.slug, organization.subdomain_enabled
            )
            + "/login",
        }


@router.put("/organizations/{organization_id}/limits")
async def limits(body: OrganizationLimits, organization_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        actor, session = await service.locked_identity(request, db)
        require_super_admin(actor)
        require_step_up(actor, session)
        organization = await db.scalar(
            select(Organization).where(Organization.id == organization_id).with_for_update()
        )
        if not organization:
            raise HTTPException(404, "Organization not found")
        before = {
            "store_limit": organization.store_limit,
            "employee_limit": organization.employee_limit,
        }
        organization.store_limit = body.store_limit
        organization.employee_limit = body.employee_limit
        service.audit(
            db,
            "platform.organization_limits_updated",
            actor.id,
            session.id,
            organization_id=organization.id,
            target_type="organizations",
            target_id=organization.id,
            changes={"before": before, "after": body.model_dump()},
        )
        return {"id": organization.id, **body.model_dump()}


@router.delete("/organizations/{organization_id}")
async def delete_organization(body: OrganizationDeletion, organization_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        actor, session = await service.locked_identity(request, db)
        require_super_admin(actor)
        require_step_up(actor, session)
        organization = await db.scalar(
            select(Organization).where(Organization.id == organization_id).with_for_update()
        )
        if not organization:
            raise HTTPException(404, "Organization not found")
        if body.confirm_name != organization.name:
            raise HTTPException(422, "Enter the exact organization name to confirm deletion")

        organization_name = organization.name
        if request.app.state.settings.environment == "development":
            await purge_organization(db, organization.id)
            service.audit(
                db,
                "platform.organization_deleted",
                actor.id,
                session.id,
                target_type="organizations",
                target_id=organization_id,
                changes={"name": organization_name, "mode": "immediate"},
            )
            return {"status": "deleted", "deletion_scheduled_for": None}

        if organization.deletion_scheduled_for is None:
            organization.deletion_scheduled_for = now() + timedelta(days=30)
            service.audit(
                db,
                "platform.organization_deletion_scheduled",
                actor.id,
                session.id,
                target_type="organizations",
                target_id=organization.id,
                changes={
                    "name": organization.name,
                    "deletion_scheduled_for": organization.deletion_scheduled_for.isoformat(),
                },
            )
        return {
            "status": "scheduled",
            "deletion_scheduled_for": organization.deletion_scheduled_for,
        }


@router.post("/organizations/{organization_id}/deletion/cancel")
async def cancel_organization_deletion(organization_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        actor, session = await service.locked_identity(request, db)
        require_super_admin(actor)
        require_step_up(actor, session)
        organization = await db.scalar(
            select(Organization).where(Organization.id == organization_id).with_for_update()
        )
        if not organization:
            raise HTTPException(404, "Organization not found")
        if organization.deletion_scheduled_for is None:
            raise HTTPException(409, "Organization deletion is not scheduled")
        organization.deletion_scheduled_for = None
        service.audit(
            db,
            "platform.organization_deletion_cancelled",
            actor.id,
            session.id,
            target_type="organizations",
            target_id=organization.id,
            changes={"name": organization.name},
        )
        return {"status": "active", "deletion_scheduled_for": None}


@router.post("/employees", status_code=201)
async def add_employee(body: PlatformEmployee, request: Request):
    async with request.app.state.db() as db, db.begin():
        actor, session = await service.locked_identity(request, db)
        require_super_admin(actor)
        require_step_up(actor, session)
        user = await db.scalar(select(User).where(User.email == str(body.email)).with_for_update())
        if not user or not user.active or not user.email_verified:
            raise HTTPException(409, "Use an existing active account with a verified email address")
        if user.platform_role != "none":
            raise HTTPException(409, "This account already has a platform role")
        user.platform_role = "employee"
        service.audit(
            db,
            "platform.employee_added",
            actor.id,
            session.id,
            target_type="users",
            target_id=user.id,
            changes={"platform_role": "employee"},
        )
        return {"id": user.id, "email": user.email, "role": user.platform_role}
