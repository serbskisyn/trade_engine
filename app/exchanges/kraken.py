import logging
from datetime import datetime, timezone

import ccxt
import pandas as pd

from app.config import KRAKEN_API_KEY, KRAKEN_API_SECRET
from app.exchanges.base import BaseExchange, OrderResult, Side
from app.strategy.indicators import calc_indicators

logger = logging.getLogger(__name__)


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
            ohlcv = self._ex.fetch_ohlcv(symbol, timeframe="15m", limit=limit)
            if len(ohlcv) < 60:
                return None
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            return calc_indicators(df)
        except Exception as e:
            logger.warning("Kraken bars failed for %s: %s", symbol, e)
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

    async def place_order(self, symbol: str, side: Side, amount: float) -> OrderResult | None:
        try:
            order = self._ex.create_order(
                symbol=symbol,
                type="market",
                side=side.value,
                amount=amount,
            )
            price = float(order.get("price") or order.get("average") or 0)
            return OrderResult(
                symbol=symbol, side=side, qty=amount,
                price=price, order_id=str(order["id"]),
            )
        except Exception as e:
            logger.warning("Kraken order failed for %s: %s", symbol, e)
            return None

    async def close_position(self, symbol: str) -> bool:
        try:
            positions = self.get_positions()
            if symbol not in positions:
                return False
            qty = float(positions[symbol]["qty"])
            self._ex.create_order(symbol=symbol, type="market", side="sell", amount=qty)
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
