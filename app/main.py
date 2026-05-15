import asyncio
import logging
import os

import httpx
import uvicorn

from app.config import validate, API_HOST, API_PORT, TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID
from app.api.routes import app as fastapi_app, set_notify
from app.engine import scheduler

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _telegram_notify(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not ADMIN_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            )
    except Exception as e:
        logger.warning("Telegram notify failed: %s", e)


async def main():
    validate()
    logger.info("Trade Engine startet — API auf %s:%d", API_HOST, API_PORT)

    set_notify(_telegram_notify)

    # Start scan loops
    tasks = scheduler.start(notify=_telegram_notify)

    # Start FastAPI
    config = uvicorn.Config(fastapi_app, host=API_HOST, port=API_PORT,
                            log_level="warning", loop="asyncio")
    server = uvicorn.Server(config)

    try:
        await server.serve()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Trade Engine gestoppt.")


if __name__ == "__main__":
    asyncio.run(main())
