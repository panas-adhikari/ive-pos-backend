import asyncio
import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type
from fastapi import HTTPException, Request

# Explicit Argon2id parameters, above OWASP's minimum recommendation.
PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1, type=Type.ID)
DUMMY_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))
ACCESS_SECONDS = 300
IDLE_SECONDS = 86400
SESSION_SECONDS = 7 * 86400


def now():
    return datetime.now(UTC)


def token():
    return secrets.token_urlsafe(32)


def digest(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


def rate_key(secret: str, value: str):
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def verify_password(encoded: str, password: str):
    try:
        return PASSWORD_HASHER.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


async def password_work(request: Request, function, *args):
    # Bound concurrent Argon2 memory use within each worker; fail closed under load.
    gate = request.app.state.password_gate
    try:
        await asyncio.wait_for(gate.acquire(), timeout=0.2)
    except TimeoutError:
        raise HTTPException(503, "Authentication busy; retry shortly") from None
    work = asyncio.create_task(asyncio.to_thread(function, *args))
    # A disconnected request must not release capacity while its thread still hashes.
    work.add_done_callback(lambda _: gate.release())
    return await asyncio.shield(work)
