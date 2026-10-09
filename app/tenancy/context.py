from sqlalchemy import select
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from app.auth.models import Organization
from app.tenancy.service import tenant_slug


class SiteContextMiddleware:
    """Resolve only registered organization origins before CORS, CSRF and authentication."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        settings = scope["app"].state.settings
        headers = dict(scope["headers"])
        origin = headers.get(b"origin", b"").decode()
        host_origin = f"{scope['scheme']}://{headers.get(b'host', b'').decode()}"
        host_slug = tenant_slug(settings, host_origin)
        origin_slug = tenant_slug(settings, origin)
        # A tenant host cannot accept credentialed writes originating from a sibling host.
        if host_slug and origin and origin != host_origin:
            return await JSONResponse({"detail": "Request origin rejected"}, 403)(
                scope, receive, send
            )
        slug = origin_slug if origin else host_slug
        state = scope.setdefault("state", {})
        state.update(tenant_id=None, tenant=None, site_origin=settings.public_origin)
        allowed = [settings.public_origin]
        if slug:
            async with scope["app"].state.db() as db:
                organization = await db.scalar(
                    select(Organization).where(Organization.slug == slug)
                )
            if organization is None:
                return await JSONResponse(
                    {"detail": "Organization address not found"},
                    404,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
            state.update(
                tenant_id=organization.id, tenant=organization, site_origin=origin or host_origin
            )
            allowed.append(origin or host_origin)
        state["allowed_origins"] = allowed
        cors = CORSMiddleware(
            self.app,
            allow_origins=allowed,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "X-POS-CSRF"],
        )
        await cors(scope, receive, send)
