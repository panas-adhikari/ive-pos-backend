"""Delivery adapters for providers behind the transactional mail outbox."""

import json
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.auth.email_template import html_message
from app.config import Settings


class EmailProviderAdapter(Protocol):
    def send(self, payload: dict[str, str]) -> None:
        ...


class SMTPEmailAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send(self, payload: dict[str, str]) -> None:
        sender_name, sender_email = self.settings.sender_for(payload.get("sender", "onboard"))
        message = EmailMessage()
        message["From"] = formataddr((sender_name or "", sender_email))
        message["To"] = payload["email"]
        message["Subject"] = payload["subject"]
        message.set_content(payload["body"])
        message.add_alternative(
            payload.get("html") or html_message(payload["subject"], payload["body"]),
            subtype="html",
        )
        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=10) as smtp:
            if self.settings.smtp_starttls:
                smtp.starttls(context=ssl.create_default_context())
            if self.settings.smtp_username:
                smtp.login(
                    self.settings.smtp_username,
                    self.settings.smtp_password.get_secret_value()
                    if self.settings.smtp_password
                    else "",
                )
            smtp.send_message(message)


class BrevoEmailAdapter:
    endpoint = "https://api.brevo.com/v3/smtp/email"

    def __init__(self, settings: Settings):
        self.api_key = settings.brevo_api_key.get_secret_value()
        self.settings = settings

    def send(self, payload: dict[str, str]) -> None:
        sender_name, sender_email = self.settings.sender_for(payload.get("sender", "onboard"))
        sender = {"email": sender_email}
        if sender_name:
            sender["name"] = sender_name
        body = json.dumps(
            {
                "sender": sender,
                "to": [{"email": payload["email"]}],
                "subject": payload["subject"],
                "textContent": payload["body"],
                "htmlContent": payload.get("html")
                or html_message(payload["subject"], payload["body"]),
            }
        ).encode()
        request = Request(
            self.endpoint,
            data=body,
            headers={
                "api-key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ive-pos/0.1",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"Brevo rejected HTTP {response.status}")
        except HTTPError as error:
            # Keep provider diagnostics useful without logging recipient, body, or API key.
            try:
                details = json.loads(error.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                details = {}
            code = details.get("code") if isinstance(details, dict) else None
            code = code or "unknown"
            raise RuntimeError(f"Brevo rejected HTTP {error.code} ({code})") from None
        except URLError as error:
            reason = error.reason.__class__.__name__
            raise RuntimeError(f"Brevo connection failed ({reason})") from None


def email_provider(settings: Settings) -> EmailProviderAdapter:
    if settings.email_provider == "brevo":
        return BrevoEmailAdapter(settings)
    return SMTPEmailAdapter(settings)
