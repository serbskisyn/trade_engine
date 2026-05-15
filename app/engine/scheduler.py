"""
Scheduler — runs scan loops for crypto (24/7) and stocks (Mo–Fr market hours).
Uses asyncio tasks; call start() once from main.
"""
import asyncio
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.config import (
    KRAKEN_API_KEY, KRAKEN_PAIRS, KRAKEN_STAKE_AMOUNT, KRAKEN_MAX_POSITIONS,
    ALPACA_API_KEY, ALPACA_SYMBOLS, ALPACA_STAKE_USD, ALPACA_MAX_POSITIONS,
)
from app.engine.scanner import run_scan, Notifier
from app.engine.price_monitor import price_monitor_loop

logger = logging.getLogger(__name__)

ET  = ZoneInfo("America/New_York")
_SCAN_INTERVAL = 5 * 60  # 5 minutes in seconds

_STOCK_OPEN  = time(10, 0)
_STOCK_CLOSE = time(15, 45)
_STOCK_DAYS  = {0, 1, 2, 3, 4}  # Mo–Fr


def _stocks_window_open() -> bool:
    now = datetime.now(ET)
    return now.weekday() in _STOCK_DAYS and _STOCK_OPEN <= now.time() <= _STOCK_CLOSE


async def _crypto_loop(notify: Notifier | None) -> None:
    if not KRAKEN_API_KEY:
        logger.info("Kraken nicht konfiguriert — Crypto-Loop deaktiviert.")
        return
    from app.exchanges.kraken import KrakenExchange
    exchange = KrakenExchange()
    logger.info("Crypto-Loop gestartet (alle 5 Min, 24/7)")
    while True:
        try:
            actions = await run_scan(exchange, KRAKEN_PAIRS, KRAKEN_STAKE_AMOUNT,
                                     KRAKEN_MAX_POSITIONS, notify)
            if actions:
                logger.info("Crypto-Scan: %d Aktionen", len(actions))
        except Exception as e:
            logger.error("Crypto-Loop Fehler: %s", e)
        await asyncio.sleep(_SCAN_INTERVAL)


async def _stocks_loop(notify: Notifier | None) -> None:
    if not ALPACA_API_KEY:
        logger.info("Alpaca nicht konfiguriert — Stocks-Loop deaktiviert.")
        return
    from app.exchanges.alpaca import AlpacaExchange
    exchange = AlpacaExchange()
    logger.info("Stocks-Loop gestartet (Mo–Fr 10:00–15:45 ET, alle 5 Min)")
    while True:
        if _stocks_window_open():
            try:
                actions = await run_scan(exchange, ALPACA_SYMBOLS, ALPACA_STAKE_USD,
                                         ALPACA_MAX_POSITIONS, notify)
                if actions:
                    logger.info("Stocks-Scan: %d Aktionen", len(actions))
            except Exception as e:
                logger.error("Stocks-Loop Fehler: %s", e)
        await asyncio.sleep(_SCAN_INTERVAL)


def start(notify: Notifier | None = None) -> list[asyncio.Task]:
    """Start scan loops + price monitor as asyncio tasks."""
    tasks = [
        asyncio.create_task(_crypto_loop(notify),       name="crypto_loop"),
        asyncio.create_task(_stocks_loop(notify),        name="stocks_loop"),
        asyncio.create_task(price_monitor_loop(notify),  name="price_monitor"),
    ]
    return tasks
