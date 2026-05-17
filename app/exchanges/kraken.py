import asyncio
import logging
from functools import partial

import ccxt
import pandas as pd

from app.config import KRAKEN_API_KEY, KRAKEN_API_SECRET, KRAKEN_LIMIT_TIMEOUT
from app.exchanges.base import BaseExchange, OrderResult, Side
from app.strategy.indicators import calc_indicators

logger = logging.getLogger(__name__)


def _run(fn, *args, **kwargs):
    """Run blocking CCXT call in thread pool so we don't block the event loop."""
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(None, partial(fn, *args, **kwargs))


class KrakenExchange(BaseExchange):
    name = "kraken"

    def __init__(self):
        self._ex = ccxt.kraken({
            "apiKey":  KRAKEN_API_KEY,
            "secret":  KRAKEN_API_SECRET,
            "enableRateLimit": True,
        })

    def is_market_open(self) -> bool:
        return True  # Crypto trades 24/7

    async def fetch_bars(self, symbol: str, limit: int = 100) -> pd.DataFrame | None:
        try:
            ohlcv = await _run(self._ex.fetch_ohlcv, symbol, "5m", None, limit)
            if len(ohlcv) < 60:
                return None
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            return calc_indicators(df)
        except Exception as e:
            logger.warning("Kraken bars failed for %s: %s", symbol, e)
            return None

    async def fetch_trend_bars(self, symbol: str, limit: int = 50) -> pd.DataFrame | None:
        try:
            ohlcv = await _run(self._ex.fetch_ohlcv, symbol, "1h", None, limit)
            if len(ohlcv) < 20:
                return None
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
            return df
        except Exception as e:
            logger.warning("Kraken trend_bars failed for %s: %s", symbol, e)
            return None

    async def get_current_price(self, symbol: str) -> float | None:
        try:
            ticker = await _run(self._ex.fetch_ticker, symbol)
            return float(ticker["last"])
        except Exception as e:
            logger.warning("Kraken get_current_price failed for %s: %s", symbol, e)
            return None

    async def get_positions(self) -> dict[str, dict]:
        try:
            balance = await _run(self._ex.fetch_balance)
            positions = {}
            for currency, info in balance.get("total", {}).items():
                if currency in ("BTC", "EUR", "USD") or float(info or 0) <= 0:
                    continue
                symbol = f"{currency}/BTC"
                try:
                    ticker = await _run(self._ex.fetch_ticker, symbol)
                    qty    = float(info)
                    price  = float(ticker["last"])
                    positions[symbol] = {
                        "qty": qty,
                        "avg_entry_price": price,
                        "market_value": qty * price,
                        "unrealized_pl": 0.0,
                        "unrealized_plpc": 0.0,
                    }
                except Exception:
                    continue
            return positions
        except Exception as e:
            logger.warning("Kraken positions failed: %s", e)
            return {}

    # ── Limit-Order mit Market-Fallback ───────────────────────────────────────

    async def _mid_price(self, symbol: str) -> float | None:
        """Bid/Ask-Mitte aus dem Ticker — Maker-freundlicher Limit-Preis."""
        try:
            ticker = await _run(self._ex.fetch_ticker, symbol)
            bid = ticker.get("bid")
            ask = ticker.get("ask")
            if bid and ask:
                return (float(bid) + float(ask)) / 2
            return float(ticker["last"])
        except Exception as e:
            logger.warning("Kraken mid_price failed for %s: %s", symbol, e)
            return None

    async def _await_fill(self, order_id: str, symbol: str,
                          timeout: int) -> dict | None:
        """Pollt den Order-Status alle 3s bis timeout. None = nicht gefüllt."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3)
            try:
                order = await _run(self._ex.fetch_order, order_id, symbol)
                status = order.get("status", "")
                if status == "closed":
                    return order
                if status in ("canceled", "expired", "rejected"):
                    return None
            except Exception as e:
                logger.warning("Kraken fetch_order failed %s: %s", order_id, e)
        return None

    async def _place_limit_with_fallback(
        self,
        symbol: str,
        side: str,
        amount: float,
        params: dict,
    ) -> dict | None:
        """
        Platziert Limit-Order am Mid-Price. Wartet KRAKEN_LIMIT_TIMEOUT Sekunden.
        Bei Timeout: Order canceln → Market-Fallback.
        Gibt das finale CCXT-Order-Dict zurück oder None bei Fehler.
        """
        mid = await self._mid_price(symbol)
        if mid is None:
            logger.warning("Kraken mid_price unavailable for %s — using market", symbol)
            return await _run(self._ex.create_order, symbol, "market", side, amount, None, params)

        try:
            limit_order = await _run(
                self._ex.create_order, symbol, "limit", side, amount, mid, params
            )
            order_id = str(limit_order["id"])
            logger.info("Kraken limit order placed: %s %s %s @ %.8f id=%s",
                        side, symbol, amount, mid, order_id)
        except Exception as e:
            logger.warning("Kraken limit order failed for %s: %s — market fallback", symbol, e)
            return await _run(self._ex.create_order, symbol, "market", side, amount, None, params)

        filled = await self._await_fill(order_id, symbol, KRAKEN_LIMIT_TIMEOUT)
        if filled:
            fill_price = float(filled.get("average") or filled.get("price") or mid)
            logger.info("Kraken limit filled: %s %s @ %.8f (maker)", symbol, side, fill_price)
            return filled

        # Timeout — cancel und market fallback
        logger.info("Kraken limit timeout (%ds) for %s — cancelling, market fallback",
                    KRAKEN_LIMIT_TIMEOUT, symbol)
        try:
            await _run(self._ex.cancel_order, order_id, symbol)
        except Exception as e:
            # Schon gefüllt während cancel? Nochmal prüfen.
            try:
                order = await _run(self._ex.fetch_order, order_id, symbol)
                if order.get("status") == "closed":
                    logger.info("Kraken order filled during cancel attempt: %s", order_id)
                    return order
            except Exception:
                pass
            logger.warning("Kraken cancel failed for %s: %s", order_id, e)

        return await _run(self._ex.create_order, symbol, "market", side, amount, None, params)

    # ── place_order ───────────────────────────────────────────────────────────

    async def place_order(self, symbol: str, side: Side, amount: float,
                          short: bool = False) -> OrderResult | None:
        try:
            ticker = await _run(self._ex.fetch_ticker, symbol)
            price  = float(ticker["last"])
            if symbol.startswith("BTC/"):
                base_amount = amount
            else:
                base_amount = round(amount / price, 8)

            params = {"leverage": 2} if short else {}
            order  = await self._place_limit_with_fallback(
                symbol, side.value, base_amount, params
            )
            if order is None:
                return None
            actual_price = float(order.get("average") or order.get("price") or price)
            return OrderResult(
                symbol=symbol, side=side, qty=base_amount,
                price=actual_price, order_id=str(order["id"]),
            )
        except Exception as e:
            logger.warning("Kraken order failed for %s: %s", symbol, e)
            return None

    # ── close_position ────────────────────────────────────────────────────────

    async def close_position(self, symbol: str, side: str = "long", qty: float | None = None) -> bool:
        try:
            if side == "short":
                if qty is None:
                    logger.warning("Kraken close_position: qty required for short %s", symbol)
                    return False
                order = await self._place_limit_with_fallback(
                    symbol, "buy", qty, {"leverage": 2}
                )
                if order is None:
                    return False
                logger.info("Kraken short closed: %s qty=%.4f", symbol, qty)
                return True
            else:
                if qty is not None:
                    actual_qty = qty
                else:
                    positions = await self.get_positions()
                    if symbol not in positions:
                        return False
                    actual_qty = float(positions[symbol]["qty"])
                order = await self._place_limit_with_fallback(
                    symbol, "sell", actual_qty, {}
                )
                if order is None:
                    return False
                logger.info("Kraken position closed: %s qty=%.4f", symbol, actual_qty)
                return True
        except Exception as e:
            logger.warning("Kraken close_position failed for %s: %s", symbol, e)
            return False

    async def get_account_info(self) -> dict:
        try:
            balance = await _run(self._ex.fetch_balance)
            btc = float(balance.get("total", {}).get("BTC", 0))
            return {"currency": "BTC", "balance": btc}
        except Exception as e:
            logger.warning("Kraken account_info failed: %s", e)
            return {}


# ── Singleton ─────────────────────────────────────────────────────────────────
_instance: KrakenExchange | None = None


def get_kraken() -> KrakenExchange:
    """Lazy singleton — vermeidet wiederholtes ccxt.load_markets pro /status-Call."""
    global _instance
    if _instance is None:
        _instance = KrakenExchange()
    return _instance
