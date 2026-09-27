from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import EmailStr, Field, field_validator, model_validator
from sqlalchemy import func, select

from app.auth import service
from app.auth.models import Membership, Organization, User
from app.auth.permissions import ORGANIZATION_PERMISSIONS, OWNER_PERMISSIONS, ROLE_DEFAULT_ALL_STORES, ROLE_PERMISSIONS
from app.auth.security import now
from app.stores.models import Register, Store
from app.stores.routes import Input, check_version, log, record

router = APIRouter(prefix="/api/v1/organizations/{organization_id}", tags=["Employees"])


class Access(Input):
    roles: list[str] = Field(min_length=1, max_length=7)
    permissions: list[str] = Field(max_length=20)
    all_stores: bool = Field(strict=True)
    store_ids: list[UUID] = Field(default_factory=list, max_length=200)
    active: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def valid_access(self):
        if not set(self.roles) <= set(ROLE_PERMISSIONS) | {"custom"}:
            raise ValueError("Unknown role")
        if not set(self.permissions) <= set(OWNER_PERMISSIONS):
            raise ValueError("Unknown permission")
        if self.all_stores and self.store_ids:
            raise ValueError("Choose all stores or specific stores")
        if not self.all_stores and set(self.permissions) & ORGANIZATION_PERMISSIONS:
            raise ValueError("Administration requires organization-wide access")
        self.roles = sorted(set(self.roles))
        self.permissions = sorted(set(self.permissions))
        self.store_ids = sorted(set(self.store_ids))
        return self


class Create(Access):
    email: EmailStr = Field(max_length=254)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class Edit(Access):
    expected_version: int = Field(ge=1)


async def authorize(db, request, organization_id, *, write=False):
    # All membership/store mutations lock the organization before any membership.
    # This serializes revocation with authorization without cross-admin deadlocks.
    org = await db.scalar(
        select(Organization).where(Organization.id == organization_id).with_for_update()
    )
    user, session = await service.locked_identity(request, db)
    member = await db.scalar(
        select(Membership)
        .where(Membership.user_id == user.id, Membership.organization_id == organization_id)
        .with_for_update()
    )
    if (
        not org
        or not member
        or not member.active
        or not member.all_stores
        or "employees.manage" not in member.permissions
    ):
        raise HTTPException(403, "Employee management access denied")
    if write and (
        not user.email_verified
        or not user.mfa_secret
        or not session.step_up_expires
        or session.step_up_expires <= now()
    ):
        raise HTTPException(403, "Unlock changes with your password and MFA code first")
    return member


async def validate_grant(db, organization_id, actor, body, target=None):
    if target and target.user_id == actor.user_id:
        raise HTTPException(409, "Another administrator must change your own access")
    if not set(body.permissions) <= set(actor.permissions) or (
        target and not set(target.permissions) <= set(actor.permissions)
    ):
        raise HTTPException(403, "You cannot grant or change access beyond your own permissions")
    found = set(
        await db.scalars(
            select(Store.id).where(
                Store.organization_id == organization_id, Store.id.in_(body.store_ids)
            )
        )
    )
    if found != set(body.store_ids):
        raise HTTPException(422, "Every assigned store must belong to this organization")


async def check_employee_limit(db, organization_id, *, adding_active, target_active=False):
    if not adding_active or target_active:
        return
    organization = await db.scalar(
        select(Organization).where(Organization.id == organization_id).with_for_update()
    )
    active_count = await db.scalar(
        select(func.count())
        .select_from(Membership)
        .where(Membership.organization_id == organization_id, Membership.active.is_(True))
    )
    if active_count >= organization.employee_limit:
        raise HTTPException(409, "This organization has reached its active employee limit")


@router.get("/employees")
async def employees(organization_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        actor = await authorize(db, request, organization_id)
        rows = (
            await db.execute(
                select(Membership, User.email)
                .join(User)
                .where(Membership.organization_id == organization_id)
                .order_by(User.email)
            )
        ).all()
        stores = await db.scalars(
            select(Store).where(Store.organization_id == organization_id).order_by(Store.code)
        )
        return {
            "memberships": [{**record(m), "email": email} for m, email in rows],
            "stores": [{"id": s.id, "name": s.name, "active": s.active} for s in stores],
            "roles": ROLE_PERMISSIONS,
            "role_all_stores": sorted(ROLE_DEFAULT_ALL_STORES),
            "grantable_permissions": sorted(set(actor.permissions) & set(OWNER_PERMISSIONS)),
        }


@router.post("/employees", status_code=201)
async def create(body: Create, organization_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        actor = await authorize(db, request, organization_id, write=True)
        await validate_grant(db, organization_id, actor, body)
        user = await db.scalar(
            select(User).where(
                User.email == str(body.email), User.active.is_(True), User.email_verified.is_(True)
            )
        )
        if not user:
            raise HTTPException(409, "Use an existing account with a verified email address")
        if await db.scalar(
            select(Membership.id).where(
                Membership.user_id == user.id, Membership.organization_id == organization_id
            )
        ):
            raise HTTPException(409, "This account already has a membership; edit its access")
        await check_employee_limit(db, organization_id, adding_active=body.active)
        member = Membership(
            user_id=user.id, organization_id=organization_id, **body.model_dump(exclude={"email"})
        )
        db.add(member)
        await db.flush()
        log(db, request, organization_id, "membership.created", member)
        return {**record(member), "email": user.email}


@router.put("/employees/{membership_id}")
async def edit(body: Edit, organization_id: UUID, membership_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        actor = await authorize(db, request, organization_id, write=True)
        member = await db.scalar(
            select(Membership)
            .where(Membership.id == membership_id, Membership.organization_id == organization_id)
            .with_for_update()
        )
        if not member:
            raise HTTPException(404, "Membership not found")
        check_version(member, body.expected_version)
        await validate_grant(db, organization_id, actor, body, member)
        await check_employee_limit(
            db, organization_id, adding_active=body.active, target_active=member.active
        )
        before = record(member)
        for key, value in body.model_dump(exclude={"expected_version"}).items():
            setattr(member, key, value)
        member.version += 1
        log(db, request, organization_id, "membership.updated", member, before)
        return record(member)


@router.get("/stores")
async def accessible_stores(organization_id: UUID, request: Request):
    async with request.app.state.db() as db, db.begin():
        await db.scalar(
            select(Organization).where(Organization.id == organization_id).with_for_update()
        )
        await service.locked_identity(request, db)
        member = await service.scoped_permission(db, request, organization_id, "store.read")
        query = select(Store).where(
            Store.organization_id == organization_id, Store.active.is_(True)
        )
        if not member.all_stores:
            query = query.where(Store.id.in_(member.store_ids))
        stores = list(await db.scalars(query.order_by(Store.code)))
        registers = list(
            await db.scalars(
                select(Register)
                .where(Register.store_id.in_([s.id for s in stores]), Register.active.is_(True))
                .order_by(Register.code)
            )
        )
        return [
            {**record(s), "registers": [record(r) for r in registers if r.store_id == s.id]}
            for s in stores
        ]
