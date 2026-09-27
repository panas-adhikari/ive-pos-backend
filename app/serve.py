"""Portable web entry point: python -m app.serve."""

import os

import uvicorn


def main():
    port = int(os.environ.get("PORT", "8000"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        access_log=False,  # URLs can contain personal information; avoid raw request logs.
        timeout_graceful_shutdown=30,
    )


if __name__ == "__main__":
    main()
