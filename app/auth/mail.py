"""Transactional encrypted email outbox. Run `python -m app.auth.mail` as a worker."""

import asyncio
import json
import logging
import signal
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.email_providers import email_provider
from app.auth.email_template import html_message
from app.auth.factors import cipher
from app.auth.models import MailOutbox
from app.auth.security import now
from app.config import Settings

logger = logging.getLogger(__name__)


def queue_mail(
    db,
    settings,
    email,
    subject,
    body,
    *,
    expires=None,
    sender="onboard",
    action_url=None,
    action_label=None,
):
    timestamp = now()
    payload = json.dumps(
        {
            "email": email,
            "subject": subject,
            "body": body,
            "sender": sender,
            "html": html_message(subject, body, action_url=action_url, action_label=action_label),
        }
    )
    db.add(
        MailOutbox(
            payload=cipher(settings).encrypt(payload.encode()).decode(),
            created=timestamp,
            expires=expires or timestamp + timedelta(hours=24),
            next_attempt=timestamp,
        )
    )


def send(settings, payload):
    email_provider(settings).send(payload)


async def deliver_one(db_factory, settings, sender=send):
    # Hold SKIP LOCKED through delivery: workers cannot send the same row concurrently.
    # A crash after provider acceptance may resend a message; redemption is one-use.
    async with db_factory() as db, db.begin():
        await db.execute(delete(MailOutbox).where(MailOutbox.expires <= now()))
        row = await db.scalar(
            select(MailOutbox)
            .where(MailOutbox.next_attempt <= now(), MailOutbox.expires > now())
            .order_by(MailOutbox.created)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return False
        try:
            payload = json.loads(cipher(settings).decrypt(row.payload.encode()))
            await asyncio.to_thread(sender, settings, payload)
        except Exception as error:
            # Provider adapters sanitize diagnostics; never log recipients or message bodies.
            logger.warning(
                "Identity email delivery failed (%s); retry scheduled", type(error).__name__
            )
            row.attempts += 1
            row.next_attempt = now() + timedelta(seconds=min(3600, 30 * 2 ** min(row.attempts, 7)))
        else:
            await db.delete(row)
        return True


async def main():
    settings = Settings.from_environment()
    if not settings.email_enabled:
        logger.info("Email delivery is not configured; mail worker exiting")
        return
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        hide_parameters=True,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=1,
        max_overflow=0,
        connect_args={"timeout": 5, "command_timeout": 60},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        while not stop.is_set():
            try:
                delivered = await deliver_one(factory, settings)
            except Exception:
                logger.warning("Mail database unavailable; retry scheduled")
                delivered = False
            if not delivered:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2)
                except TimeoutError:
                    pass
    finally:
        await engine.dispose()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
