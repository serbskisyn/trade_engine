from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import pandas as pd


class Side(str, Enum):
    BUY  = "buy"
    SELL = "sell"


@dataclass
class OrderResult:
    symbol:   str
    side:     Side
    qty:      float
    price:    float
    order_id: str


class BaseExchange(ABC):
    name: str = "base"

    @abstractmethod
    def is_market_open(self) -> bool: ...

    @abstractmethod
    async def fetch_bars(self, symbol: str, limit: int = 100) -> pd.DataFrame | None:
        """Return OHLCV DataFrame with columns: timestamp, open, high, low, close, volume."""

    @abstractmethod
    def get_positions(self) -> dict[str, dict]:
        """Return {symbol: {qty, avg_entry_price, market_value, unrealized_pl, unrealized_plpc}}."""

    @abstractmethod
    async def place_order(self, symbol: str, side: Side, amount: float,
                          short: bool = False) -> OrderResult | None:
        """amount = USD for stocks, base-currency units for crypto. short=True for margin shorts."""

    @abstractmethod
    async def close_position(self, symbol: str) -> bool: ...

    @abstractmethod
    async def fetch_trend_bars(self, symbol: str, limit: int = 50) -> pd.DataFrame | None:
        """Fetch 1h candles for trend direction. Returns DataFrame with ema50 column."""

    @abstractmethod
    async def get_current_price(self, symbol: str) -> float | None:
        """Cheap single-price fetch — no bars, no indicators. Used by price monitor."""

    @abstractmethod
    def get_account_info(self) -> dict: ...
