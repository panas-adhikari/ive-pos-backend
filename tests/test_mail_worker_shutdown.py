import asyncio
import signal
from types import SimpleNamespace

from app.auth import mail


def test_worker_finishes_current_delivery_after_sigterm(monkeypatch):
    settings = SimpleNamespace(
        email_enabled=True,
        database_url=SimpleNamespace(get_secret_value=lambda: "postgresql+asyncpg://test"),
    )
    monkeypatch.setattr(mail.Settings, "from_environment", lambda: settings)

    class Engine:
        disposed = False

        async def dispose(self):
            self.disposed = True

    engine = Engine()
    monkeypatch.setattr(mail, "create_async_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(mail, "async_sessionmaker", lambda *args, **kwargs: object())

    async def run():
        handlers = {}
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop, "add_signal_handler", lambda sig, handler: handlers.update({sig: handler})
        )
        monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: handlers.pop(sig))
        deliveries = 0

        async def deliver_one(*args):
            nonlocal deliveries
            deliveries += 1
            handlers[signal.SIGTERM]()
            await asyncio.sleep(0)
            return True

        monkeypatch.setattr(mail, "deliver_one", deliver_one)
        await mail.main()
        assert deliveries == 1
        assert not handlers

    asyncio.run(run())
    assert engine.disposed
