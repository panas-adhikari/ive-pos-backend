import io
import json
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from app.auth.email_providers import BrevoEmailAdapter, SMTPEmailAdapter, email_provider
from app.auth.email_template import html_message
from app.auth.factors import cipher
from app.auth.mail import queue_mail
from app.config import Settings


def settings():
    return Settings(
        database_url="postgresql+asyncpg://test:test@localhost/test",
        auth_secret="test-only-secret-that-is-at-least-32-characters",
        identity_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        email_provider="brevo",
        brevo_api_key="private-test-api-key",
        email_from="onboarding@ivepos.me",
        email_from_name="Onboard",
        email_operations_from="operator@ivepos.me",
        email_operations_from_name="Operations",
    )


def test_brevo_sends_transactional_email():
    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    with patch("app.auth.email_providers.urlopen", return_value=Response()) as open_url:
        email_provider(settings()).send(
            {"email": "employee@example.com", "subject": "Invitation", "body": "Join us"}
        )

    request = open_url.call_args.args[0]
    assert request.full_url == "https://api.brevo.com/v3/smtp/email"
    assert request.get_method() == "POST"
    assert request.get_header("Api-key") == "private-test-api-key"
    payload = json.loads(request.data)
    assert {key: value for key, value in payload.items() if key != "htmlContent"} == {
        "sender": {"email": "onboarding@ivepos.me", "name": "Onboard"},
        "to": [{"email": "employee@example.com"}],
        "subject": "Invitation",
        "textContent": "Join us",
    }
    assert "Ive POS" in payload["htmlContent"]
    assert "Join us" in payload["htmlContent"]


def test_html_template_escapes_untrusted_content_and_keeps_action_link():
    html = html_message(
        "Join <North & South>",
        "Hello <script>alert(1)</script>\n\nhttps://ivepos.me/?a=1&b=2\n\nExpires soon.",
        action_url="https://ivepos.me/?a=1&b=2",
        action_label="Accept <invitation>",
    )
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Join &lt;North &amp; South&gt;" in html
    assert 'href="https://ivepos.me/?a=1&amp;b=2"' in html
    assert "Accept &lt;invitation&gt;" in html


def test_smtp_includes_text_and_html_versions():
    class SMTP:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def starttls(self, **_):
            pass

        def send_message(self, message):
            assert message["From"] == "Onboard <onboarding@ivepos.me>"
            assert message.get_body(preferencelist=("plain",)).get_content() == "Join us\n"
            assert "Ive POS" in message.get_body(preferencelist=("html",)).get_content()

    smtp_settings = settings().model_copy(update={"smtp_host": "localhost"})
    with patch("app.auth.email_providers.smtplib.SMTP", return_value=SMTP()):
        SMTPEmailAdapter(smtp_settings).send(
            {"email": "employee@example.com", "subject": "Invitation", "body": "Join us"}
        )


def test_queued_action_email_stores_branded_html_encrypted():
    class DB:
        row = None

        def add(self, row):
            self.row = row

    db = DB()
    config = settings()
    queue_mail(
        db,
        config,
        "employee@example.com",
        "Join Ive POS",
        "Open your invitation.\n\nhttps://ivepos.me/invite",
        action_url="https://ivepos.me/invite",
        action_label="Accept invitation",
    )
    assert "Accept invitation" not in db.row.payload
    payload = json.loads(cipher(config).decrypt(db.row.payload.encode()))
    assert payload["body"].startswith("Open your invitation")
    assert 'href="https://ivepos.me/invite"' in payload["html"]


def test_brevo_uses_operations_sender_for_security_mail():
    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    with patch("app.auth.email_providers.urlopen", return_value=Response()) as open_url:
        email_provider(settings()).send(
            {
                "email": "employee@example.com",
                "subject": "Security alert",
                "body": "Your password changed",
                "sender": "operations",
            }
        )

    request = open_url.call_args.args[0]
    assert json.loads(request.data)["sender"] == {
        "email": "operator@ivepos.me",
        "name": "Operations",
    }


def test_brevo_error_does_not_expose_message_or_key():
    error = HTTPError(
        BrevoEmailAdapter.endpoint,
        401,
        "Unauthorized",
        {},
        io.BytesIO(b'{"code":"unauthorized","message":"private provider detail"}'),
    )
    with patch("app.auth.email_providers.urlopen", side_effect=error):
        with pytest.raises(
            RuntimeError, match=r"Brevo rejected HTTP 401 \(unauthorized\)"
        ) as caught:
            email_provider(settings()).send(
                {"email": "employee@example.com", "subject": "Invitation", "body": "Join us"}
            )
    assert "private provider detail" not in str(caught.value)
    assert "private-test-api-key" not in str(caught.value)
