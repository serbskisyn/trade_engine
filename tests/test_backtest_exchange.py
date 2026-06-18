"""Tests for BacktestExchange — no-lookahead replay + simulated fills."""
import asyncio

import pandas as pd
import pytest

from app.exchanges.backtest import BacktestExchange
from app.exchanges.base import Side


def _df(closes):
    n = len(closes)
    return pd.DataFrame({
        "timestamp": list(range(n)),
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1.0] * n,
    })


def test_fetch_bars_has_no_lookahead():
    bx = BacktestExchange({"X/BTC": _df(list(range(100)))}, warmup=10)
    bars = asyncio.run(bx.fetch_bars("X/BTC", limit=5))
    # clock sits at index 10 → newest visible close is 10, never the future
    assert bars["close"].iloc[-1] == 10
    assert bars["close"].max() == 10
    assert len(bars) == 5            # indices 6..10


def test_clock_advances_and_finishes():
    bx = BacktestExchange({"X/BTC": _df(list(range(5)))}, warmup=0)
    steps = 0
    while bx.advance():
        steps += 1
    assert bx.finished
    assert steps == 4                # 0→1→2→3→4


def test_open_then_close_realises_pnl():
    bx = BacktestExchange({"X/BTC": _df([100, 100, 110, 110])},
                          warmup=0, fee=0.0, start_cash=0.0)

    async def run():
        await bx.place_order("X/BTC", Side.BUY, amount=1.0)  # fill @100 (i=0)
        bx.advance(); bx.advance()                            # i=2 → close 110
        pos = bx.get_positions()["X/BTC"]
        assert pos["unrealized_pl"] == pytest.approx(10.0)    # mark-to-market
        assert await bx.close_position("X/BTC")               # realise +10

    asyncio.run(run())
    assert bx.cash == pytest.approx(10.0)
    assert bx.get_account_info()["equity"] == pytest.approx(10.0)


def test_short_pnl_inverts():
    bx = BacktestExchange({"X/BTC": _df([100, 90])},
                          warmup=0, fee=0.0, start_cash=0.0)

    async def run():
        await bx.place_order("X/BTC", Side.SELL, amount=1.0, short=True)  # @100
        bx.advance()                                                      # → 90
        await bx.close_position("X/BTC", side="short")                    # +10

    asyncio.run(run())
    assert bx.cash == pytest.approx(10.0)
