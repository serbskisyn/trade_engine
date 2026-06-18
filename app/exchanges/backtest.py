"""
backtest.py — BacktestExchange: replays historical OHLCV bars as a BaseExchange.

Foundation for the autoresearch backtest harness (see backtest roadmap). A
BacktestExchange is constructed with the full history per symbol and a central
clock cursor (`_i`); the engine's scanner/trade-manager can drive it exactly
like the live Kraken/Alpaca exchanges, but every read is bounded by the clock so
there is NO LOOKAHEAD — the single most important correctness property of a
backtest.

Increment 1 of the runner: this class only. The data collector, the clock-driven
run loop, the /backtest API, the sentiment stub and the LLM cache come next.
"""
from __future__ import annotations

import pandas as pd

from app.exchanges.base import BaseExchange, Side, OrderResult


class BacktestExchange(BaseExchange):
    name = "backtest"

    def __init__(
        self,
        bars: dict[str, pd.DataFrame],
        *,
        start_cash: float = 1.0,
        fee: float = 0.0008,         # Kraken maker fee per leg
        warmup: int = 50,            # bars available before the clock starts (indicators)
        trend_bars: dict[str, pd.DataFrame] | None = None,
    ):
        # Each value: OHLCV DataFrame [timestamp, open, high, low, close, volume],
        # ascending by time. We index positionally via the clock.
        self._bars = {s: df.reset_index(drop=True) for s, df in bars.items()}
        self._trend = trend_bars or {}
        self.fee = fee
        self.cash = start_cash
        self.start_cash = start_cash
        self._i = warmup                       # central clock: current bar index
        self._max = min((len(df) for df in self._bars.values()), default=0)
        # symbol -> {qty, avg_entry_price, side}
        self._positions: dict[str, dict] = {}
        self.trade_log: list[dict] = []

    # ── central clock ────────────────────────────────────────────────────────
    @property
    def finished(self) -> bool:
        return self._i >= self._max - 1

    def advance(self) -> bool:
        """Step the clock one bar forward. Returns False when history is exhausted."""
        if self.finished:
            return False
        self._i += 1
        return True

    def _price(self, symbol: str) -> float:
        return float(self._bars[symbol].iloc[self._i]["close"])

    # ── BaseExchange interface ────────────────────────────────────────────────
    def is_market_open(self) -> bool:
        return True  # crypto-style 24/7; stock calendars handled by the runner

    async def fetch_bars(self, symbol: str, limit: int = 100) -> pd.DataFrame | None:
        df = self._bars.get(symbol)
        if df is None:
            return None
        # Bars UP TO AND INCLUDING the current clock bar — never the future.
        end = self._i + 1
        return df.iloc[max(0, end - limit):end].reset_index(drop=True)

    async def fetch_trend_bars(self, symbol: str, limit: int = 50) -> pd.DataFrame | None:
        df = self._trend.get(symbol)
        if df is None:
            return None
        out = df.iloc[max(0, len(df) - limit):].reset_index(drop=True).copy()
        if "ema50" not in out.columns and "close" in out.columns:
            out["ema50"] = out["close"].ewm(span=50, adjust=False).mean()
        return out

    async def get_current_price(self, symbol: str) -> float | None:
        if symbol not in self._bars:
            return None
        return self._price(symbol)

    def get_positions(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for sym, pos in self._positions.items():
            price = self._price(sym)
            qty = pos["qty"]
            entry = pos["avg_entry_price"]
            direction = 1 if pos["side"] == "long" else -1
            pl = (price - entry) * qty * direction
            cost = entry * qty
            out[sym] = {
                "qty": qty,
                "avg_entry_price": entry,
                "market_value": price * qty,
                "unrealized_pl": round(pl, 8),
                "unrealized_plpc": round(pl / cost, 6) if cost else 0.0,
                "side": pos["side"],
            }
        return out

    async def place_order(self, symbol: str, side: Side, amount: float,
                          short: bool = False) -> OrderResult | None:
        price = self._price(symbol)
        qty = amount  # crypto: base-currency units (matches live KRAKEN_STAKE_AMOUNT)
        self.cash -= price * qty * self.fee
        self._positions[symbol] = {
            "qty": qty,
            "avg_entry_price": price,
            "side": "short" if short else "long",
        }
        self.trade_log.append({"i": self._i, "symbol": symbol, "action": "open",
                               "side": "short" if short else "long",
                               "price": price, "qty": qty})
        return OrderResult(symbol=symbol, side=side, qty=qty, price=price,
                           order_id=f"bt-{self._i}")

    async def close_position(self, symbol: str, side: str = "long",
                             qty: float | None = None) -> bool:
        pos = self._positions.pop(symbol, None)
        if not pos:
            return False
        price = self._price(symbol)
        q = qty or pos["qty"]
        direction = 1 if pos["side"] == "long" else -1
        pnl = (price - pos["avg_entry_price"]) * q * direction
        self.cash += pnl - price * q * self.fee
        self.trade_log.append({"i": self._i, "symbol": symbol, "action": "close",
                               "side": pos["side"], "price": price, "qty": q,
                               "pnl": round(pnl, 8)})
        return True

    def get_account_info(self) -> dict:
        equity = self.cash + sum(
            self._price(s) * p["qty"] * (1 if p["side"] == "long" else -1)
            for s, p in self._positions.items()
        )
        return {"cash": round(self.cash, 8), "equity": round(equity, 8),
                "start_cash": self.start_cash, "clock": self._i}
