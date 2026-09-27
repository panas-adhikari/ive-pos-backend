"""Encrypted TOTP enrollment and one-use backup codes. Call with the user row locked."""

import hmac
import secrets

import pyotp
from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.auth.security import digest, now


def cipher(settings):
    if not settings.identity_encryption_key:
        raise HTTPException(503, "Account security is not configured")
    return Fernet(settings.identity_encryption_key.get_secret_value().encode())


def accept_factor(settings, user, code: str, *, recovery=True) -> bool:
    if not user.mfa_secret:
        return True
    if not code.isascii():
        return False
    if recovery:
        hashed = digest(code.replace("-", "").strip().lower())
        for stored in user.recovery_hashes:
            if hmac.compare_digest(hashed, stored):
                user.recovery_hashes = [item for item in user.recovery_hashes if item != stored]
                return True
    secret = cipher(settings).decrypt(user.mfa_secret.encode()).decode()
    step = int(now().timestamp()) // 30
    generator = pyotp.TOTP(secret)
    for candidate in (step - 1, step, step + 1):
        if candidate > user.mfa_last_step and hmac.compare_digest(
            generator.at(candidate * 30), code
        ):
            user.mfa_last_step = candidate
            return True
    return False


def backup_codes():
    codes = [secrets.token_hex(10) for _ in range(10)]
    return codes, [digest(code) for code in codes]
