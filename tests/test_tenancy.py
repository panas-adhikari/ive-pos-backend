from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.auth.models import AuditEvent, Membership, Organization, Session, User
from app.tenancy.service import (
    allocate_slug,
    subdomain_suggestions,
    suggested_slug,
    tenant_slug,
    validate_name_slug,
    validate_slug,
)
from tests.test_auth import HEADERS, PASSWORD
from tests.test_auth import anyio_backend as anyio_backend
from tests.test_auth import setup as setup
from tests.test_stores import CODES
from tests.test_stores import owner as owner


@pytest.mark.parametrize("value", ["api", "www", "a.b", "-shop", "shop-", "a" * 64, "foo/bar"])
def test_invalid_subdomain(value):
    with pytest.raises(ValueError):
        validate_slug(value)


def test_slug_normalization():
    assert validate_slug(" My-Store ") == "my-store"
    assert suggested_slug("Café & Groceries") == "cafe-groceries"
    assert suggested_slug("API") == "api-store"
    assert suggested_slug("पसल") == "store"


@pytest.fixture
async def tenants(setup):
    app, client = setup
    app.state.settings.tenant_base_domain = "example.test"
    async with app.state.db() as db, db.begin():
        orgs = list(await db.scalars(select(Organization).order_by(Organization.name)))
        orgs[0].slug, orgs[1].slug = "alpha", "beta"
        orgs[0].subdomain_enabled = orgs[1].subdomain_enabled = True
        orgs[0].image_url = "https://images.example.test/alpha.png"
        ids = [str(org.id) for org in orgs]
    return app, client, ids


def tenant_client(app, name):
    origin = f"https://{name}.example.test"
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=origin,
        headers={**HEADERS, "Origin": origin},
    )


@pytest.mark.anyio
async def test_public_branding_and_unknown_tenants(tenants):
    app, client, ids = tenants
    assert (await client.get("/api/v1/public/site")).json()["organization"] is None
    async with tenant_client(app, "alpha") as alpha:
        response = await alpha.get("/api/v1/public/site")
        assert response.status_code == 200
        brand = response.json()["organization"]
        assert brand == {
            "id": ids[0],
            "name": "Store 0",
            "slug": "alpha",
            "image_url": "https://images.example.test/alpha.png",
            "login_url": "https://alpha.example.test/login",
        }
        assert "contact_email" not in response.text
        # Same-origin GETs can arrive without Origin; the actual Host resolves branding.
        response = await alpha.get("/api/v1/public/site", headers={"Origin": ""})
        assert response.json()["organization"]["slug"] == "alpha"
    async with tenant_client(app, "missing") as missing:
        assert (await missing.get("/api/v1/public/site")).status_code == 404
        assert (
            await missing.post(
                "/api/v1/auth/login",
                json={
                    "email": "owner0@example.com",
                    "password": PASSWORD,
                },
            )
        ).status_code == 404
    assert (
        await client.get("/api/v1/public/domain-check?domain=alpha.example.test")
    ).status_code == 204
    for domain in [
        "missing.example.test",
        "api.example.test",
        "alpha.example.test.evil.com",
        "alpha.beta.example.test",
        "alpha.example.test/path",
        "evil.com",
    ]:
        assert (
            await client.get("/api/v1/public/domain-check", params={"domain": domain})
        ).status_code == 403
    assert tenant_slug(app.state.settings, "http://alpha.example.test") is None
    assert tenant_slug(app.state.settings, "https://alpha.example.test:9999") is None


@pytest.mark.anyio
async def test_tenant_login_membership_session_binding_and_api_scope(tenants):
    app, root, ids = tenants
    async with app.state.db() as db, db.begin():
        user = await db.scalar(select(User).where(User.email == "owner0@example.com"))
        user.platform_role = "super_admin"
        # Multiple memberships must still yield only the current hostname's organization.
        db.add(Membership(user_id=user.id, organization_id=UUID(ids[1]), active=True))
    async with tenant_client(app, "alpha") as alpha, tenant_client(app, "beta") as beta:
        denied = await alpha.post(
            "/api/v1/auth/login",
            json={
                "email": "owner1@example.com",
                "password": PASSWORD,
            },
        )
        assert denied.status_code == 401
        assert (
            await alpha.post(
                "/api/v1/auth/login",
                json={
                    "email": "owner0@example.com",
                    "password": PASSWORD,
                },
            )
        ).status_code == 204
        me = (await alpha.get("/api/v1/auth/me")).json()
        assert me["platform_role"] == "none"
        assert [m["organization_id"] for m in me["memberships"]] == [ids[0]]
        assert (await alpha.get("/api/v1/platform/organizations")).status_code == 403
        assert (await alpha.get(f"/api/v1/organizations/{ids[1]}/stores")).status_code == 403
        access = alpha.cookies.get("__Host-pos_access")
        refresh = alpha.cookies.get("__Host-pos_refresh")
        cookies = {"Cookie": f"__Host-pos_access={access}; __Host-pos_refresh={refresh}"}
        assert (await beta.get("/api/v1/auth/me", headers=cookies)).status_code == 401
        assert (await root.get("/api/v1/auth/me", headers=cookies)).status_code == 401
        assert (
            await beta.post("/api/v1/auth/refresh", json={}, headers=cookies)
        ).status_code == 401
        assert (await alpha.post("/api/v1/auth/refresh", json={})).status_code == 204
        # Reject sibling-origin CSRF even though both organizations are registered.
        assert (
            await alpha.post(
                "/api/v1/auth/logout",
                json={},
                headers={
                    "Origin": "https://beta.example.test",
                },
            )
        ).status_code == 403
        async with app.state.db() as db:
            session = await db.get(Session, UUID(me["session_id"]))
            assert str(session.tenant_id) == ids[0]
        assert (await alpha.post("/api/v1/auth/logout", json={})).status_code == 204
        assert (await alpha.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.anyio
async def test_tenant_recovery_links_and_signup_restriction(tenants):
    from tests.test_auth import latest_link

    app, _, _ = tenants
    async with app.state.db() as db, db.begin():
        user = await db.scalar(select(User).where(User.email == "owner0@example.com"))
        user.email_verified = True
    async with tenant_client(app, "alpha") as alpha:
        assert (
            await alpha.post(
                "/api/v1/auth/signup/request",
                json={
                    "email": "new@example.com",
                },
            )
        ).status_code == 403
        assert (
            await alpha.post(
                "/api/v1/auth/recovery/request",
                json={
                    "email": "owner0@example.com",
                },
            )
        ).status_code == 202
        # Inspect the encrypted outbox with the existing test helper.
        from app.auth.factors import cipher
        from app.auth.models import MailOutbox

        async with app.state.db() as db:
            outbox = await db.scalar(select(MailOutbox).order_by(MailOutbox.created.desc()))
            body = cipher(app.state.settings).decrypt(outbox.payload.encode()).decode()
            assert "https://alpha.example.test/login#identity=reset" in body
        assert await latest_link(
            app, "owner0@example.com", "reset", expected_origin="https://alpha.example.test"
        )


@pytest.mark.anyio
async def test_slug_allocation_and_platform_onboarding(owner):
    app, client, _ = owner
    app.state.settings.tenant_base_domain = "example.test"
    async with app.state.db() as db, db.begin():
        user = await db.scalar(select(User).where(User.email == "owner0@example.com"))
        user.platform_role = "super_admin"
        assert await allocate_slug(db, "Store 0", "fresh-store") == "fresh-store"
    assert (
        await client.post(
            "/api/v1/auth/step-up",
            json={
                "password": PASSWORD,
                "code": CODES[1],
            },
        )
    ).status_code == 200
    payload = {
        "name": "Mountain Shop",
        "slug": "mountain",
        "owner_email": "mountain@example.com",
        "owner_name": "Shop Owner",
        "owner_temporary_password": PASSWORD,
        "location_label": "Kathmandu",
    }
    response = await client.post("/api/v1/platform/organizations", json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["login_url"] == "https://mountain.example.test/login"
    response = await client.post(
        "/api/v1/platform/organizations",
        json={
            **payload,
            "owner_email": "another@example.com",
        },
    )
    assert response.status_code == 409
    response = await client.post(
        "/api/v1/platform/organizations",
        json={
            **payload,
            "owner_email": "reserved@example.com",
            "slug": "api",
        },
    )
    assert response.status_code == 422
    response = await client.post(
        "/api/v1/platform/organizations",
        json={
            **payload,
            "owner_email": "auto@example.com",
            "slug": "",
        },
    )
    assert response.status_code == 201
    assert response.json()["slug"] is None
    assert response.json()["subdomain_enabled"] is False
    assert response.json()["login_url"] == app.state.settings.public_origin + "/login"


@pytest.mark.anyio
async def test_localhost_subdomain_branding_login_and_isolation(tenants):
    app, _, ids = tenants
    settings = app.state.settings
    settings.environment = "development"
    settings.public_origin = "http://app.localhost:5173"
    settings.tenant_base_domain = "localhost"
    origin = "http://alpha.localhost:5173"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=origin,
        headers={"Origin": origin, "X-POS-CSRF": "1"},
    ) as alpha:
        brand = (await alpha.get("/api/v1/public/site")).json()["organization"]
        assert brand["id"] == ids[0]
        assert brand["login_url"] == origin + "/login"
        response = await alpha.post(
            "/api/v1/auth/login",
            json={"email": "owner0@example.com", "password": PASSWORD},
        )
        assert response.status_code == 204
        for cookie in response.headers.get_list("set-cookie"):
            assert "Domain=" not in cookie
            assert "pos_dev_" in cookie
        me = (await alpha.get("/api/v1/auth/me")).json()
        assert [m["organization_id"] for m in me["memberships"]] == [ids[0]]
        assert (
            await alpha.post(
                "/api/v1/auth/logout",
                json={},
                headers={"Origin": "http://beta.localhost:5173"},
            )
        ).status_code == 403
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://beta.localhost:5173",
        ) as beta:
            cookies = "; ".join(f"{key}={value}" for key, value in alpha.cookies.items())
            assert (
                await beta.get("/api/v1/auth/me", headers={"Cookie": cookies})
            ).status_code == 401


@pytest.mark.parametrize(
    "name, slug",
    [
        ("Atharva Organization", "atharva"),
        ("Atharva Organization", "org"),
        ("Atharva Organization", "ao"),
        ("Margret Crimson", "mc"),
        ("Café & Groceries", "cafe"),
        ("A Store", "a"),
    ],
)
def test_subdomain_uses_name_words_and_short_forms(name, slug):
    assert validate_name_slug(name, slug.upper()) == slug


@pytest.mark.parametrize("slug", ["unrelated", "atharva-organization", "a", "org-2", "app"])
def test_unrelated_or_multiword_subdomains_are_rejected(slug):
    with pytest.raises(ValueError):
        validate_name_slug("Atharva Organization", slug)


def test_name_subdomain_suggestions():
    assert subdomain_suggestions("Atharva Organization") == [
        "atharva",
        "organization",
        "ath",
        "org",
        "ao",
    ]
    assert "api" not in subdomain_suggestions("API Store")


@pytest.mark.anyio
async def test_optional_subdomain_setup_validation_and_disabling(owner):
    app, client, base = owner
    org_id = UUID(base.rsplit("/", 1)[1])
    app.state.settings.tenant_base_domain = "example.test"
    async with app.state.db() as db, db.begin():
        actor = await db.scalar(select(User).where(User.email == "owner0@example.com"))
        actor.platform_role = "super_admin"
        org = await db.get(Organization, org_id)
        org.name = "Atharva Organization"
        other = await db.scalar(select(Organization).where(Organization.id != org_id))
        other.slug = "organization"
        other.subdomain_enabled = True
    path = f"/api/v1/platform/organizations/{org_id}"
    detail = (await client.get(path)).json()
    assert detail["subdomain_enabled"] is False
    assert detail["slug"] is None
    assert detail["login_url"] == app.state.settings.public_origin + "/login"
    options = (await client.get(path + "/subdomain-options")).json()
    assert options["domain"] == "example.test"
    assert {item["slug"]: item["available"] for item in options["suggestions"]}[
        "organization"
    ] is False
    body = {"enabled": True, "slug": "ath", "expected_version": detail["version"]}
    assert (await client.put(path + "/subdomain", json=body)).status_code == 403
    assert (
        await client.post("/api/v1/auth/step-up", json={"password": PASSWORD, "code": CODES[1]})
    ).status_code == 200
    for invalid in ("atharva-organization", "unrelated", "a", "app"):
        assert (
            await client.put(path + "/subdomain", json={**body, "slug": invalid})
        ).status_code == 422
    assert (
        await client.put(path + "/subdomain", json={**body, "slug": "organization"})
    ).status_code == 409
    response = await client.put(path + "/subdomain", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["login_url"] == "https://ath.example.test/login"
    assert result["subdomain_enabled"] is True
    assert (await client.put(path + "/subdomain", json=body)).status_code == 409
    async with tenant_client(app, "ath") as tenant:
        assert (await tenant.get("/api/v1/public/site")).json()["organization"]["id"] == str(org_id)
    # Re-saving an organization's own slug must not be treated as a collision.
    response = await client.put(
        path + "/subdomain", json={**body, "expected_version": result["version"]}
    )
    assert response.status_code == 200
    response = await client.put(
        path + "/subdomain", json={"enabled": False, "expected_version": response.json()["version"]}
    )
    assert response.status_code == 200
    assert response.json()["slug"] is None
    assert response.json()["login_url"] == app.state.settings.public_origin + "/login"
    async with tenant_client(app, "ath") as tenant:
        assert (await tenant.get("/api/v1/public/site")).status_code == 404
    assert (
        await client.get("/api/v1/public/domain-check", params={"domain": "ath.example.test"})
    ).status_code == 403
    async with app.state.db() as db:
        events = list(
            await db.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "platform.organization_subdomain_updated"
                )
            )
        )
        assert len(events) == 3
