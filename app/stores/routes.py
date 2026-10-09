from secrets import token_hex
from uuid import UUID
from urllib.parse import urlsplit
from zoneinfo import available_timezones

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import func, select

from app.auth import service
from app.auth.models import Membership, Organization
from app.auth.security import now
from app.stores.models import Register, Store

router = APIRouter(prefix="/api/v1/organizations/{organization_id}", tags=["Store setup"])
CURRENCIES = ["NPR", "INR", "USD", "EUR", "GBP", "AUD", "CAD", "JPY", "CNY", "SGD", "AED"]
TIMEZONES = sorted(available_timezones() - {"localtime"})


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*")
    @classmethod
    def no_null_characters(cls, value):
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("Null characters are not allowed")
        return value


class Contact(Input):
    name: str = Field(min_length=1, max_length=160)
    phone: str = Field(default="", max_length=40)
    contact_email: EmailStr | str = Field(default="", max_length=254)
    timezone: str = Field(max_length=80)
    receipt_footer: str = Field(default="", max_length=500)

    @field_validator("contact_email")
    @classmethod
    def email(cls, value):
        if not value:
            return ""
        from pydantic import TypeAdapter

        return str(TypeAdapter(EmailStr).validate_python(value))

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        if value not in TIMEZONES:
            raise ValueError("Use an IANA timezone")
        return value


class BusinessInput(Contact):
    currency: str
    expected_version: int = Field(ge=1)
    organization_type: str | None = None
    image_url: str | None = Field(default=None, max_length=1000)
    website_url: str | None = Field(default=None, max_length=300)
    location_label: str | None = Field(default=None, max_length=240)

    @field_validator("organization_type")
    @classmethod
    def valid_organization_type(cls, value):
        if value is not None and value not in {"retail", "wholesale", "other"}:
            raise ValueError("Choose a valid organization type")
        return value

    @field_validator("image_url", "website_url")
    @classmethod
    def safe_optional_url(cls, value):
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Use a valid http or https URL")
        return value

    @field_validator("currency")
    @classmethod
    def currency_code(cls, value):
        if value not in CURRENCIES:
            raise ValueError("Unsupported currency")
        return value


class StoreLimitRequest(Input):
    requested_limit: int = Field(ge=1, le=10_000)
    reason: str = Field(min_length=10, max_length=1000)


class StoreFields(Contact):
    address: str = Field(default="", max_length=500)
    opening_hours: str = Field(default="", max_length=500)
    receipt_name: str = Field(min_length=1, max_length=160)


class StoreCreate(StoreFields):
    code: str | None = Field(default=None, pattern=r"^[A-Z0-9][A-Z0-9_-]{0,19}$")

    @field_validator("code", mode="before")
    @classmethod
    def normalize_code(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


class StoreEdit(StoreFields):
    expected_version: int = Field(ge=1)


class StatusInput(Input):
    active: bool = Field(strict=True)
    expected_version: int = Field(ge=1)


class RegisterCreate(Input):
    code: str = Field(pattern=r"^[A-Z0-9][A-Z0-9_-]{0,19}$")
    name: str = Field(min_length=1, max_length=160)

    @field_validator("code", mode="before")
    @classmethod
    def normalize_code(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


class RegisterEdit(Input):
    name: str = Field(min_length=1, max_length=160)
    expected_version: int = Field(ge=1)


def record(row):
    # Used only for business records and memberships; never pass users or sessions.
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


async def authorize(db, request, organization_id, *, write=False):
    # Keep authorization and mutation in one transaction; recheck revocation after locking.
    org = await db.scalar(
        select(Organization).where(Organization.id == organization_id).with_for_update()
    )
    user, session = await service.locked_identity(request, db)
    membership = await db.scalar(
        select(Membership)
        .where(Membership.user_id == user.id, Membership.organization_id == organization_id)
        .with_for_update()
    )
    if (
        not membership
        or not membership.active
        or not membership.all_stores
        or "organization.setup" not in membership.permissions
    ):
        raise HTTPException(403, "Store setup access denied")
    if write and (
        not user.email_verified
        or not user.mfa_secret
        or not service.action_verified(user, session)
    ):
        raise HTTPException(
            403, "Unlock setup with your password and MFA code before making changes"
        )
    if not org:
        raise HTTPException(404, "Organization not found")
    return org


async def get_store(db, organization_id, store_id, *, require_active=False):
    store = await db.scalar(
        select(Store)
        .where(Store.id == store_id, Store.organization_id == organization_id)
        .with_for_update()
    )
    if not store:
        raise HTTPException(404, "Store not found")
    if require_active and not store.active:
        raise HTTPException(409, "Activate this store before changing its registers")
    return store


async def get_register(db, store_id, register_id):
    register = await db.scalar(
        select(Register)
        .where(Register.id == register_id, Register.store_id == store_id)
        .with_for_update()
    )
    if not register:
        raise HTTPException(404, "Register not found")
    return register


def check_version(row, version):
    if row.version != version:
        raise HTTPException(409, "This record changed. Reload its latest details before saving")


def log(db, request, org_id, action, row, before=None):
    after = record(row)

    # UUID objects must be JSON serializable. Only business fields enter this audit payload.
    def snapshot(data):
        if isinstance(data, UUID):
            return str(data)
        if isinstance(data, list):
            return [snapshot(value) for value in data]
        if isinstance(data, dict):
            return {key: snapshot(value) for key, value in data.items()}
        return data

    service.audit(
        db,
        action,
        *request.state.identity,
        organization_id=org_id,
        target_type=row.__tablename__,
        target_id=row.id,
        changes={"before": snapshot(before) if before else None, "after": snapshot(after)},
    )


@router.get("/setup")
async def setup(organization_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        org = await authorize(db, request, organization_id)
        stores = list(
            await db.scalars(
                select(Store).where(Store.organization_id == org.id).order_by(Store.code)
            )
        )
        registers = list(
            await db.scalars(
                select(Register)
                .join(Store)
                .where(Store.organization_id == org.id)
                .order_by(Register.code)
            )
        )
        return {
            "organization": record(org),
            "stores": [
                {
                    **record(store),
                    "registers": [record(r) for r in registers if r.store_id == store.id],
                }
                for store in stores
            ],
            "currencies": CURRENCIES,
            "timezones": TIMEZONES,
        }


@router.put("/settings")
async def settings(body: BusinessInput, organization_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        org = await authorize(db, request, organization_id, write=True)
        check_version(org, body.expected_version)
        before = record(org)
        for key, value in body.model_dump(exclude={"expected_version"}, exclude_none=True).items():
            setattr(org, key, value)
        org.configured = True
        org.version += 1
        log(db, request, org.id, "organization.settings_changed", org, before)
        return record(org)


@router.post("/store-limit-requests", status_code=201)
async def request_store_limit(body: StoreLimitRequest, organization_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        org = await authorize(db, request, organization_id)
        if body.requested_limit <= org.store_limit:
            raise HTTPException(422, "Request a limit higher than your current allowance")
        service.audit(
            db, "organization.store_limit_requested", *request.state.identity,
            organization_id=org.id, target_type="organizations", target_id=org.id,
            changes={"current_limit": org.store_limit, "requested_limit": body.requested_limit, "reason": body.reason},
        )
        return {"status": "submitted"}


@router.post("/stores", status_code=201)
async def create_store(body: StoreCreate, organization_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        org = await authorize(db, request, organization_id, write=True)
        if not org.configured:
            raise HTTPException(409, "Save business settings before creating a store")
        store_count = await db.scalar(
            select(func.count()).select_from(Store).where(Store.organization_id == org.id)
        )
        if store_count >= org.store_limit:
            raise HTTPException(409, "This organization has reached its store limit")
        code = body.code
        if code is None:
            # Generated codes fit the existing 20-character limit and are scoped to the organization.
            while True:
                code = f"STR-{token_hex(8).upper()}"
                if not await db.scalar(select(Store.id).where(Store.organization_id == org.id, Store.code == code)):
                    break
        elif await db.scalar(select(Store.id).where(Store.organization_id == org.id, Store.code == code)):
            raise HTTPException(409, "A store with this code already exists, including inactive stores")
        store = Store(organization_id=org.id, code=code, **body.model_dump(exclude={"code"}))
        db.add(store)
        await db.flush()
        log(db, request, org.id, "store.created", store)
        return record(store)


@router.put("/stores/{store_id}")
async def edit_store(body: StoreEdit, organization_id: UUID, store_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        await authorize(db, request, organization_id, write=True)
        store = await get_store(db, organization_id, store_id)
        check_version(store, body.expected_version)
        before = record(store)
        for key, value in body.model_dump(exclude={"expected_version"}).items():
            setattr(store, key, value)
        store.version += 1
        log(db, request, organization_id, "store.updated", store, before)
        return record(store)


@router.post("/stores/{store_id}/status")
async def store_status(body: StatusInput, organization_id: UUID, store_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        await authorize(db, request, organization_id, write=True)
        store = await get_store(db, organization_id, store_id)
        check_version(store, body.expected_version)
        before = record(store)
        if store.active != body.active:
            store.active = body.active
            store.version += 1
            if not body.active:
                registers = await db.scalars(
                    select(Register)
                    .where(Register.store_id == store.id, Register.active.is_(True))
                    .order_by(Register.id)
                    .with_for_update()
                )
                for register in registers:
                    old = record(register)
                    register.active = False
                    register.version += 1
                    log(db, request, organization_id, "register.deactivated", register, old)
            log(
                db,
                request,
                organization_id,
                "store.activated" if body.active else "store.deactivated",
                store,
                before,
            )
        return record(store)


@router.post("/stores/{store_id}/registers", status_code=201)
async def create_register(
    body: RegisterCreate, organization_id: UUID, store_id: UUID, request: Request
):
    async with request.app.state.db() as db, db.begin():
        await authorize(db, request, organization_id, write=True)
        store = await get_store(db, organization_id, store_id, require_active=True)
        if await db.scalar(
            select(Register.id).where(Register.store_id == store.id, Register.code == body.code)
        ):
            raise HTTPException(
                409, "A register with this code already exists, including inactive registers"
            )
        register = Register(store_id=store.id, **body.model_dump())
        db.add(register)
        await db.flush()
        log(db, request, organization_id, "register.created", register)
        return record(register)


@router.put("/stores/{store_id}/registers/{register_id}")
async def edit_register(
    body: RegisterEdit, organization_id: UUID, store_id: UUID, register_id: UUID, request: Request
):
    async with request.app.state.db() as db, db.begin():
        await authorize(db, request, organization_id, write=True)
        await get_store(db, organization_id, store_id, require_active=True)
        register = await get_register(db, store_id, register_id)
        check_version(register, body.expected_version)
        before = record(register)
        register.name = body.name
        register.version += 1
        log(db, request, organization_id, "register.updated", register, before)
        return record(register)


@router.post("/stores/{store_id}/registers/{register_id}/status")
async def register_status(
    body: StatusInput, organization_id: UUID, store_id: UUID, register_id: UUID, request: Request
):
    async with request.app.state.db() as db, db.begin():
        await authorize(db, request, organization_id, write=True)
        await get_store(db, organization_id, store_id, require_active=body.active)
        register = await get_register(db, store_id, register_id)
        check_version(register, body.expected_version)
        before = record(register)
        if register.active != body.active:
            register.active = body.active
            register.version += 1
            log(
                db,
                request,
                organization_id,
                "register.activated" if body.active else "register.deactivated",
                register,
                before,
            )
        return record(register)
