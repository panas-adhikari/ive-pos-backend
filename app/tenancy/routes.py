from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import select

from app.auth.models import Organization
from app.tenancy.service import organization_origin, tenant_slug

router = APIRouter(prefix="/api/v1/public", tags=["Public site"])


@router.get("/site")
async def site(request: Request):
    settings = request.app.state.settings
    organization = getattr(request.state, "tenant", None)
    return {
        "organization": {
            "id": organization.id,
            "name": organization.name,
            "slug": organization.slug,
            "image_url": organization.image_url,
            "login_url": organization_origin(settings, organization.slug) + "/login",
        }
        if organization
        else None,
        "platform_url": settings.public_origin,
        "tenant_base_domain": settings.tenant_base_domain,
    }


@router.get("/domain-check", status_code=204)
async def domain_check(request: Request, domain: str = Query(max_length=253)):
    # Caddy's on-demand TLS allowlist: never approve arbitrary/custom domains.
    settings = request.app.state.settings
    slug = tenant_slug(settings, f"https://{domain}")
    if not slug:
        raise HTTPException(403, "Domain not registered")
    async with request.app.state.db() as db:
        if not await db.scalar(
            select(Organization.id).where(
                Organization.slug == slug, Organization.subdomain_enabled.is_(True)
            )
        ):
            raise HTTPException(403, "Domain not registered")
    return Response(status_code=204)
