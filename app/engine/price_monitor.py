"""
Price Monitor — prüft alle 30 Sekunden Stop-Loss und Trailing Stop für offene Positionen.
Kein LLM, kein Bar-Fetch — nur ein billiger Ticker-Call pro Position.
Läuft parallel zum Scanner-Loop.
"""
import asyncio
import logging

from app.config import KRAKEN_API_KEY, ALPACA_API_KEY
from app.engine import trade_manager as tm
from app.engine.scanner import Notifier, _fmt_pl_with_fee

logger = logging.getLogger(__name__)

_INTERVAL = 30  # Sekunden


async def _check_market(exchange, market: str, notify: Notifier | None) -> None:
    positions = await tm.get_open_positions(market)
    if not positions:
        return

    for pos in positions:
        symbol = pos["symbol"]
        price  = await exchange.get_current_price(symbol)
        if price is None:
            continue

        should_close, reason = await tm.check_stops(
            market, symbol, price, int(pos.get("candles_held", 0))
        )
        if should_close:
            ok = await exchange.close_position(symbol)
            if ok:
                result = await tm.close_position(market, symbol, price, reason)
                if result:
                    sign   = "+" if result["pl_pct"] >= 0 else ""
                    pl_str = _fmt_pl_with_fee(market, result)
                    name   = exchange.name.capitalize()
                    msg    = (f"🛑 *{name} Stop*\n"
                              f"`{symbol}` {sign}{result['pl_pct']:.2f}% ({pl_str})\n"
                              f"Grund: {reason}")
                    logger.info("[PriceMonitor] %s", msg.replace("*", "").replace("`", ""))
                    if notify:
                        await notify(msg)


async def price_monitor_loop(notify: Notifier | None = None) -> None:
    kraken_exchange  = None
    alpaca_exchange  = None

    if KRAKEN_API_KEY:
        from app.exchanges.kraken import get_kraken
        kraken_exchange = get_kraken()

    if ALPACA_API_KEY:
        from app.exchanges.alpaca import get_alpaca
        alpaca_exchange = get_alpaca()

    logger.info("Price Monitor gestartet (alle %ds, Stop-Loss + Trailing Stop)", _INTERVAL)

    while True:
        try:
            if kraken_exchange:
                await _check_market(kraken_exchange, "crypto", notify)
            if alpaca_exchange:
                await _check_market(alpaca_exchange, "stocks", notify)
        except Exception as e:
            logger.error("Price Monitor Fehler: %s", e)
        await asyncio.sleep(_INTERVAL)
