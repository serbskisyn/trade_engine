import logging

import pandas as pd
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from app.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER, ALPACA_STAKE_USD
from app.exchanges.base import BaseExchange, OrderResult, Side
from app.strategy.indicators import calc_indicators

logger = logging.getLogger(__name__)


class AlpacaExchange(BaseExchange):
    name = "alpaca"

    def __init__(self):
        self._trading = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
        self._data    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    def is_market_open(self) -> bool:
        try:
            return self._trading.get_clock().is_open
        except Exception:
            return False

    async def fetch_bars(self, symbol: str, limit: int = 100) -> pd.DataFrame | None:
        try:
            req  = StockBarsRequest(symbol_or_symbols=symbol,
                                    timeframe=TimeFrame(5, TimeFrameUnit.Minute), limit=limit)
            bars = self._data.get_stock_bars(req)
            df   = bars.df
            if hasattr(df.index, "levels"):
                df = df.xs(symbol, level=0) if symbol in df.index.get_level_values(0) else df
            df = df.reset_index()
            if "timestamp" not in df.columns:
                df = df.rename(columns={df.columns[0]: "timestamp"})
            if len(df) < 60:
                return None
            return calc_indicators(df)
        except Exception as e:
            logger.warning("Alpaca bars failed for %s: %s", symbol, e)
            return None

    def get_positions(self) -> dict[str, dict]:
        try:
            return {
                p.symbol: {
                    "qty": float(p.qty),
                    "avg_entry_price": float(p.avg_entry_price),
                    "market_value":    float(p.market_value),
                    "unrealized_pl":   float(p.unrealized_pl),
                    "unrealized_plpc": float(p.unrealized_plpc),
                }
                for p in self._trading.get_all_positions()
            }
        except Exception:
            return {}

    async def place_order(self, symbol: str, side: Side, amount: float,
                          short: bool = False) -> OrderResult | None:
        try:
            req  = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, limit=1)
            bars = self._data.get_stock_bars(req)
            df   = bars.df
            if hasattr(df.index, "levels"):
                df = df.xs(symbol, level=0)
            price = float(df["close"].iloc[-1])
            qty   = max(1, int(amount / price))

            alpaca_side = OrderSide.BUY if side == Side.BUY else OrderSide.SELL
            order = self._trading.submit_order(MarketOrderRequest(
                symbol=symbol, qty=qty, side=alpaca_side, time_in_force=TimeInForce.DAY,
            ))
            return OrderResult(symbol=symbol, side=side, qty=qty, price=price, order_id=str(order.id))
        except Exception as e:
            logger.warning("Alpaca order failed for %s: %s", symbol, e)
            return None

    async def close_position(self, symbol: str, side: str = "long", qty: float | None = None) -> bool:
        try:
            self._trading.close_position(symbol)
            logger.info("Alpaca position closed: %s", symbol)
            return True
        except Exception as e:
            logger.warning("Alpaca close_position failed for %s: %s", symbol, e)
            return False

    async def fetch_trend_bars(self, symbol: str, limit: int = 50) -> pd.DataFrame | None:
        try:
            req  = StockBarsRequest(symbol_or_symbols=symbol,
                                    timeframe=TimeFrame(1, TimeFrameUnit.Hour), limit=limit)
            bars = self._data.get_stock_bars(req)
            df   = bars.df
            if hasattr(df.index, "levels"):
                df = df.xs(symbol, level=0) if symbol in df.index.get_level_values(0) else df
            df = df.reset_index()
            if "timestamp" not in df.columns:
                df = df.rename(columns={df.columns[0]: "timestamp"})
            if len(df) < 20:
                return None
            df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
            return df
        except Exception as e:
            logger.warning("Alpaca trend_bars failed for %s: %s", symbol, e)
            return None

    async def get_current_price(self, symbol: str) -> float | None:
        try:
            from alpaca.data.requests import StockLatestBarRequest
            req  = StockLatestBarRequest(symbol_or_symbols=symbol)
            bars = self._data.get_stock_latest_bar(req)
            return float(bars[symbol].close)
        except Exception as e:
            logger.warning("Alpaca get_current_price failed for %s: %s", symbol, e)
            return None

    def get_account_info(self) -> dict:
        try:
            acc = self._trading.get_account()
            return {
                "equity":     float(acc.equity),
                "cash":       float(acc.cash),
                "last_equity": float(acc.last_equity),
                "mode":       "paper" if ALPACA_PAPER else "live",
            }
        except Exception as e:
            logger.warning("Alpaca account_info failed: %s", e)
            return {}


# ── Singleton ─────────────────────────────────────────────────────────────────
_instance: AlpacaExchange | None = None


def get_alpaca() -> AlpacaExchange:
    """Lazy singleton — vermeidet wiederholten Alpaca-Client-Init pro Call."""
    global _instance
    if _instance is None:
        _instance = AlpacaExchange()
    return _instance
