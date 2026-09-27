import os
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    database_url: SecretStr = Field(min_length=1)
    auth_secret: SecretStr = Field(min_length=32)
    environment: Literal["development", "production"] = "production"
    public_origin: str = "https://localhost"
    geocoder_url: str = "https://nominatim.openstreetmap.org/search"

    identity_encryption_key: SecretStr | None = None
    email_provider: Literal["smtp", "brevo"] = "smtp"
    brevo_api_key: SecretStr | None = None
    email_from: str | None = None
    email_from_name: str | None = None
    email_operations_from: str | None = None
    email_operations_from_name: str | None = None
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str = "noreply@localhost"
    smtp_starttls: bool = True

    @property
    def email_enabled(self) -> bool:
        provider_configured = (
            bool(self.brevo_api_key) if self.email_provider == "brevo" else bool(self.smtp_host)
        )
        return bool(provider_configured and self.identity_encryption_key)

    @property
    def sender_address(self) -> str:
        return self.email_from or self.smtp_from

    def sender_for(self, kind: str) -> tuple[str | None, str]:
        if kind == "operations":
            return (
                self.email_operations_from_name or self.email_from_name,
                self.email_operations_from or self.sender_address,
            )
        return self.email_from_name, self.sender_address

    @model_validator(mode="after")
    def validate_security(self):
        origin = urlsplit(self.public_origin)
        if (
            not origin.hostname
            or origin.path
            or origin.query
            or origin.fragment
            or origin.username
            or origin.password
        ):
            raise ValueError("PUBLIC_ORIGIN must be an exact origin without a trailing slash")
        geocoder = urlsplit(self.geocoder_url)
        if (
            geocoder.scheme != "https"
            or not geocoder.hostname
            or geocoder.username
            or geocoder.password
            or geocoder.fragment
            or geocoder.query
        ):
            raise ValueError("GEOCODER_URL must be an HTTPS endpoint")
        if origin.scheme != "https":
            if not (
                self.environment == "development"
                and origin.scheme == "http"
                and origin.hostname in {"localhost", "127.0.0.1", "[::1]", "::1"}
            ):
                raise ValueError("HTTPS is required except for explicit localhost development")
        if not self.database_url.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("An async PostgreSQL DATABASE_URL is required")
        if self.identity_encryption_key:
            from cryptography.fernet import Fernet

            try:
                Fernet(self.identity_encryption_key.get_secret_value().encode())
            except (ValueError, TypeError):
                raise ValueError("IDENTITY_ENCRYPTION_KEY must be a Fernet key") from None
        if (self.smtp_host or self.brevo_api_key) and not self.identity_encryption_key:
            raise ValueError("Email delivery requires IDENTITY_ENCRYPTION_KEY")
        if (
            self.email_provider == "smtp"
            and self.smtp_host
            and self.environment == "production"
            and not self.smtp_starttls
        ):
            raise ValueError("SMTP STARTTLS is required in production")
        addresses = (self.sender_address, self.email_operations_from or "")
        if any(char in "\r\n" for address in addresses for char in address):
            raise ValueError("Invalid email sender address")
        names = (self.email_from_name or "", self.email_operations_from_name or "")
        if any(char in "\r\n" for name in names for char in name):
            raise ValueError("Invalid email sender name")
        return self

    @property
    def secure_cookies(self) -> bool:
        return self.environment == "production" or self.public_origin.startswith("https://")

    def cookie_name(self, kind: str) -> str:
        return f"__Host-pos_{kind}" if self.secure_cookies else f"pos_dev_{kind}"

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            database_url=os.environ["DATABASE_URL"],
            auth_secret=os.environ["AUTH_SECRET"],
            environment=os.environ.get("APP_ENV", "production"),
            public_origin=os.environ["PUBLIC_ORIGIN"],
            geocoder_url=os.environ.get(
                "GEOCODER_URL", "https://nominatim.openstreetmap.org/search"
            ),
            identity_encryption_key=os.environ.get("IDENTITY_ENCRYPTION_KEY") or None,
            email_provider=os.environ.get("EMAIL_PROVIDER")
            or ("brevo" if os.environ.get("BREVO_API_KEY") else "smtp"),
            brevo_api_key=os.environ.get("BREVO_API_KEY") or None,
            email_from=os.environ.get("EMAIL_FROM") or None,
            email_from_name=os.environ.get("EMAIL_FROM_NAME") or None,
            email_operations_from=os.environ.get("EMAIL_OPERATIONS_FROM") or None,
            email_operations_from_name=os.environ.get("EMAIL_OPERATIONS_FROM_NAME") or None,
            smtp_host=os.environ.get("SMTP_HOST") or None,
            smtp_port=int(os.environ.get("SMTP_PORT", "587")),
            smtp_username=os.environ.get("SMTP_USERNAME") or None,
            smtp_password=os.environ.get("SMTP_PASSWORD") or None,
            smtp_from=os.environ.get("SMTP_FROM", "noreply@localhost"),
            smtp_starttls=os.environ.get("SMTP_STARTTLS", "true").lower() == "true",
        )
