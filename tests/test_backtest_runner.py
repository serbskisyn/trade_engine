"""Tests for the clock-driven backtest runner — entry/exit control flow."""
import asyncio

import pandas as pd
import pytest

from app.backtest import runner
from app.backtest.runner import run_backtest, BacktestParams


def _bars(closes):
    n = len(closes)
    return pd.DataFrame({
        "timestamp": pd.to_datetime(range(n), unit="m", utc=True),
        "open": [float(c) for c in closes], "high": [float(c) for c in closes],
        "low": [float(c) for c in closes], "close": [float(c) for c in closes],
        "volume": [1.0] * n,
    })


def _enter_at_first_clock_bar(monkeypatch, warmup):
    # _technical_signal fires exactly once, at the first clock bar
    # (df has warmup+1 rows there). Long-only.
    monkeypatch.setattr(runner, "_technical_signal",
                        lambda df: (len(df) == warmup + 1, False))


def test_stop_exit(monkeypatch):
    _enter_at_first_clock_bar(monkeypatch, warmup=60)
    closes = [100.0] * 61 + [90.0]          # enter @100 (i=60), crash to 90 (i=61)
    res = asyncio.run(run_backtest(
        {"X": _bars(closes)},
        BacktestParams(warmup=60, stop_pct=0.05, trail_activate_pct=0.5, max_hold=999, fee=0.0),
    ))
    assert res["n_trades"] == 1
    t = res["trades"][0]
    assert t["reason"] == "stop"
    assert t["return_pct"] == pytest.approx(-0.10)


def test_trailing_exit(monkeypatch):
    _enter_at_first_clock_bar(monkeypatch, warmup=60)
    closes = [100.0] * 61 + [110.0, 104.0]  # peak 110 → trail, exit @104
    res = asyncio.run(run_backtest(
        {"X": _bars(closes)},
        BacktestParams(warmup=60, stop_pct=0.5, trail_activate_pct=0.05, trail_pct=0.05,
                       max_hold=999, fee=0.0),
    ))
    assert res["n_trades"] == 1
    t = res["trades"][0]
    assert t["reason"] == "trail"
    assert t["return_pct"] == pytest.approx(0.04)


def test_short_profit_on_price_drop(monkeypatch):
    # short signal fires at first clock bar; price falls → profit
    monkeypatch.setattr(runner, "_technical_signal",
                        lambda df: (False, len(df) == 61))
    closes = [100.0] * 61 + [90.0]          # short @100, drop to 90
    res = asyncio.run(run_backtest(
        {"X": _bars(closes)},
        BacktestParams(warmup=60, allow_long=False, allow_short=True,
                       stop_pct=0.5, trail_activate_pct=0.5, max_hold=1, fee=0.0),
    ))
    assert res["n_trades"] == 1
    assert res["trades"][0]["side"] == "short"
    assert res["trades"][0]["return_pct"] == pytest.approx(0.10)


def test_short_stop_on_price_rise(monkeypatch):
    monkeypatch.setattr(runner, "_technical_signal",
                        lambda df: (False, len(df) == 61))
    closes = [100.0] * 61 + [110.0]         # short @100, adverse rise to 110
    res = asyncio.run(run_backtest(
        {"X": _bars(closes)},
        BacktestParams(warmup=60, allow_long=False, allow_short=True,
                       stop_pct=0.05, max_hold=999, fee=0.0),
    ))
    assert res["trades"][0]["reason"] == "stop"
    assert res["trades"][0]["return_pct"] == pytest.approx(-0.10)


def test_no_signal_no_trades(monkeypatch):
    monkeypatch.setattr(runner, "_technical_signal", lambda df: (False, False))
    res = asyncio.run(run_backtest({"X": _bars([100.0] * 70)}, BacktestParams(warmup=60)))
    assert res["n_trades"] == 0
    assert res["net_pct"] == 0.0


def test_llm_mode_blocks_entry_below_conf(monkeypatch):
    # technical signal fires, but the LLM verdict is too weak → no trade
    _enter_at_first_clock_bar(monkeypatch, warmup=60)

    async def weak(prompt):
        return {"signal": "buy", "confidence": 0.40, "reason": "meh"}

    res = asyncio.run(run_backtest(
        {"X": _bars([100.0] * 61 + [110.0])},
        BacktestParams(warmup=60, llm_mode=True, buy_conf=0.55, max_hold=999, fee=0.0),
        verdict_fn=weak,
    ))
    assert res["n_trades"] == 0


def test_llm_mode_allows_entry_above_conf(monkeypatch):
    # confident matching verdict → the technical signal is taken
    _enter_at_first_clock_bar(monkeypatch, warmup=60)

    async def strong(prompt):
        return {"signal": "buy", "confidence": 0.80, "reason": "go"}

    res = asyncio.run(run_backtest(
        {"X": _bars([100.0] * 61 + [110.0, 104.0])},
        BacktestParams(warmup=60, llm_mode=True, buy_conf=0.55,
                       stop_pct=0.5, trail_activate_pct=0.05, trail_pct=0.05,
                       max_hold=999, fee=0.0),
        verdict_fn=strong,
    ))
    assert res["n_trades"] == 1


def test_llm_mode_blocks_on_signal_mismatch(monkeypatch):
    # LLM says "sell" but the technical signal is long → gate rejects
    _enter_at_first_clock_bar(monkeypatch, warmup=60)

    async def opposite(prompt):
        return {"signal": "sell", "confidence": 0.90, "reason": "down"}

    res = asyncio.run(run_backtest(
        {"X": _bars([100.0] * 61 + [110.0])},
        BacktestParams(warmup=60, llm_mode=True, buy_conf=0.55, max_hold=999, fee=0.0),
        verdict_fn=opposite,
    ))
    assert res["n_trades"] == 0


def test_fee_reduces_return(monkeypatch):
    _enter_at_first_clock_bar(monkeypatch, warmup=60)
    closes = [100.0] * 61 + [110.0, 104.0]
    res = asyncio.run(run_backtest(
        {"X": _bars(closes)},
        BacktestParams(warmup=60, stop_pct=0.5, trail_activate_pct=0.05, trail_pct=0.05,
                       max_hold=999, fee=0.001),
    ))
    # 0.04 gross minus 2 legs * 0.001 fee
    assert res["trades"][0]["return_pct"] == pytest.approx(0.04 - 0.002)
