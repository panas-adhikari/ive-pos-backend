import re
import unicodedata
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select, text

from app.auth.models import Membership, Organization

RESERVED_SLUGS = frozenset(
    {
        "www",
        "app",
        "api",
        "admin",
        "platform",
        "auth",
        "login",
        "signup",
        "mail",
        "smtp",
        "support",
        "status",
        "static",
        "assets",
        "cdn",
        "docs",
        "help",
        "billing",
    }
)
SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def validate_slug(value):
    value = value.strip().lower()
    if not SLUG_PATTERN.fullmatch(value) or value in RESERVED_SLUGS:
        raise ValueError(
            "Use 1–63 lowercase letters, numbers or hyphens; this address is reserved or invalid"
        )
    return value


def suggested_slug(name):
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:63].rstrip("-") or "store"
    return f"{base}-store" if base in RESERVED_SLUGS else base


def name_words(name):
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", plain)


def validate_name_slug(name, value):
    value = validate_slug(value)
    words = name_words(name)
    initials = "".join(word[0] for word in words)
    if value in words or (
        len(value) >= 2 and (value == initials or any(word.startswith(value) for word in words))
    ):
        return value
    raise ValueError(
        "Choose a word from the organization name, "
        "a prefix of at least two letters, or its initials."
    )


def subdomain_suggestions(name):
    words = name_words(name)
    candidates = words + [word[:3] for word in words if len(word) > 3]
    if len(words) > 1:
        candidates.append("".join(word[0] for word in words))
    return list(
        dict.fromkeys(
            value
            for value in candidates
            if SLUG_PATTERN.fullmatch(value) and value not in RESERVED_SLUGS
        )
    )


async def allocate_slug(db, name, requested=None, exclude_id=None):
    # Serialize allocation, including collisions between a generated and a requested slug.
    await db.execute(text("SELECT pg_advisory_xact_lock(736192801)"))
    base = validate_slug(requested) if requested else suggested_slug(name)
    candidate, suffix = base, 1
    query = select(Organization.id).where(Organization.slug == candidate)
    if exclude_id is not None:
        query = query.where(Organization.id != exclude_id)
    while await db.scalar(query):
        if requested:
            raise HTTPException(409, "This sign-in address is taken. Choose another.")
        suffix += 1
        ending = f"-{suffix}"
        candidate = f"{base[: 63 - len(ending)].rstrip('-')}{ending}"
        query = select(Organization.id).where(Organization.slug == candidate)
        if exclude_id is not None:
            query = query.where(Organization.id != exclude_id)
    return candidate


def tenant_slug(settings, origin):
    """Parse exactly one tenant label. Never trust forwarded-host headers here."""
    if not settings.tenant_base_domain or origin == settings.public_origin:
        return None
    try:
        url = urlsplit(origin)
        public = urlsplit(settings.public_origin)
        if (
            url.scheme != public.scheme
            or url.port != public.port
            or url.path
            or url.query
            or url.fragment
            or url.username
            or url.password
        ):
            return None
        host = url.hostname or ""
        suffix = "." + settings.tenant_base_domain
        if not host.endswith(suffix):
            return None
        slug = host[: -len(suffix)]
        if slug in RESERVED_SLUGS:
            return None
        return validate_slug(slug)
    except ValueError:
        return None


def organization_origin(settings, slug, enabled=True):
    if not enabled or not settings.tenant_base_domain:
        return settings.public_origin
    public = urlsplit(settings.public_origin)
    port = f":{public.port}" if public.port else ""
    return f"{public.scheme}://{slug}.{settings.tenant_base_domain}{port}"


async def tenant_membership(db, user_id, tenant_id):
    return bool(
        await db.scalar(
            select(Membership.id).where(
                Membership.user_id == user_id,
                Membership.organization_id == tenant_id,
                Membership.active.is_(True),
            )
        )
    )


def request_tenant_id(request):
    return getattr(request.state, "tenant_id", None)


def session_matches_tenant(request, session):
    return session.tenant_id == request_tenant_id(request)
