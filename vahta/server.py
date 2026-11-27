"""Headless, single-process server: python -m vahta.server."""
from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
from contextlib import asynccontextmanager
from pathlib import Path


def main():
    os.environ["VAHTA_SERVER"] = "1"
    os.umask(0o077)
    import uvicorn
    from . import web
    from .paths import DATA_DIR
    from .remote import Access
    from .service import Service

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(mode=0o700, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), RotatingFileHandler(
            log_dir / "vahta.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Fail closed before starting Twitch or accepting requests.
    web.remote_access = Access(os.environ["VAHTA_PUBLIC_URL"],
                               Path(os.environ["VAHTA_PASSWORD_FILE"]))

    @asynccontextmanager
    async def lifespan(app):
        service = Service()
        await asyncio.to_thread(service.start)
        web.attach(service)
        try:
            yield
        finally:
            await asyncio.to_thread(service.stop)
            web._service = None

    web.api.router.lifespan_context = lifespan
    uvicorn.run(web.api, host="0.0.0.0", port=8420, workers=1,
                proxy_headers=True, forwarded_allow_ips=os.environ["VAHTA_TRUSTED_PROXY"],
                timeout_graceful_shutdown=20, access_log=False)


if __name__ == "__main__":
    main()
