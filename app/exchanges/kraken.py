import asyncio
import logging
from functools import partial

import ccxt
import pandas as pd

from app.config import KRAKEN_API_KEY, KRAKEN_API_SECRET
from app.exchanges.base import BaseExchange, OrderResult, Side
from app.strategy.indicators import calc_indicators

logger = logging.getLogger(__name__)


def _run(fn, *args, **kwargs):
    """Run blocking CCXT call in thread pool so we don't block the event loop."""
    loop = asyncio.get_event_loop()
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

    def get_positions(self) -> dict[str, dict]:
        try:
            balance = self._ex.fetch_balance()
            positions = {}
            for currency, info in balance.get("total", {}).items():
                if currency in ("BTC", "EUR", "USD") or float(info or 0) <= 0:
                    continue
                symbol = f"{currency}/BTC"
                try:
                    ticker = self._ex.fetch_ticker(symbol)
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
            order  = await _run(self._ex.create_order,
                                 symbol, "market", side.value, base_amount, None, params)
            actual_price = float(order.get("price") or order.get("average") or price)
            return OrderResult(
                symbol=symbol, side=side, qty=base_amount,
                price=actual_price, order_id=str(order["id"]),
            )
        except Exception as e:
            logger.warning("Kraken order failed for %s: %s", symbol, e)
            return None

    async def close_position(self, symbol: str, side: str = "long", qty: float | None = None) -> bool:
        try:
            if side == "short":
                if qty is None:
                    logger.warning("Kraken close_position: qty required for short %s", symbol)
                    return False
                await _run(self._ex.create_order, symbol, "market", "buy", qty, None, {"leverage": 2})
                logger.info("Kraken short closed: %s qty=%.4f", symbol, qty)
                return True
            else:
                positions = self.get_positions()
                if symbol not in positions:
                    return False
                actual_qty = float(positions[symbol]["qty"])
                await _run(self._ex.create_order, symbol, "market", "sell", actual_qty)
                logger.info("Kraken position closed: %s", symbol)
                return True
        except Exception as e:
            logger.warning("Kraken close_position failed for %s: %s", symbol, e)
            return False

    def get_account_info(self) -> dict:
        try:
            balance = self._ex.fetch_balance()
            btc = float(balance.get("total", {}).get("BTC", 0))
            return {"currency": "BTC", "balance": btc}
        except Exception as e:
            logger.warning("Kraken account_info failed: %s", e)
            return {}
