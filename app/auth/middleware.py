from starlette.responses import JSONResponse


class SecurityMiddleware:
    """Exact-origin CSRF protection and actual (not just declared) body size limits."""

    def __init__(self, app, settings_getter):
        self.app = app
        self.settings_getter = settings_getter

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope["headers"]}
        settings = self.settings_getter()

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] += [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                ]
                if settings.secure_cookies:
                    message["headers"].append((b"strict-transport-security", b"max-age=31536000"))
            await send(message)

        if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            if (
                headers.get(b"origin", b"").decode() not in scope.get("state", {}).get(
                    "allowed_origins", [settings.public_origin]
                )
                or headers.get(b"x-pos-csrf") != b"1"
                or headers.get(b"sec-fetch-site") == b"cross-site"
            ):
                return await JSONResponse({"detail": "Request origin rejected"}, 403)(
                    scope, receive, secure_send
                )
            # Only PUBLIC_ORIGIN may pass credentialed CORS and this CSRF check.
            if headers.get(b"content-type", b"").split(b";")[0].strip() != b"application/json":
                return await JSONResponse({"detail": "JSON required"}, 415)(
                    scope, receive, secure_send
                )
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 16384:
                    return await JSONResponse({"detail": "Request too large"}, 413)(
                        scope, receive, secure_send
                    )
                if not message.get("more_body", False):
                    break

            async def buffered_receive():
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            return await self.app(scope, buffered_receive, secure_send)
        await self.app(scope, receive, secure_send)
