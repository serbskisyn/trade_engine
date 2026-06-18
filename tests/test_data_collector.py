"""Tests for the backtest data collector — round-trip + grow-over-time merge."""
import asyncio

import pandas as pd
import pytest

from app.backtest import data_collector as dc


def _df(start_ms, n, step_ms=300_000):  # 5m = 300_000 ms
    ts = pd.to_datetime([start_ms + i * step_ms for i in range(n)], unit="ms", utc=True)
    return pd.DataFrame({
        "timestamp": ts,
        "open": range(n), "high": range(n), "low": range(n),
        "close": [float(i) for i in range(n)], "volume": [1.0] * n,
    })


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "DB_PATH", tmp_path / "history.db")


def test_save_load_roundtrip_keeps_shape():
    dc.save_history("ETH/BTC", _df(0, 10))
    out = dc.load_history("ETH/BTC")
    assert list(out.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(out) == 10
    assert str(out["timestamp"].dt.tz) == "UTC"
    assert out["close"].iloc[-1] == 9.0


def test_merge_grows_history_and_dedupes():
    dc.save_history("ETH/BTC", _df(0, 10))               # bars 0..9
    dc.save_history("ETH/BTC", _df(5 * 300_000, 10))     # bars 5..14 (overlap 5..9)
    out = dc.load_history("ETH/BTC")
    assert len(out) == 15                                # grown to 15, no dupes
    # strictly increasing timestamps (sorted, unique)
    assert out["timestamp"].is_monotonic_increasing
    assert out["timestamp"].is_unique


def test_collect_and_load_all():
    class _FakeExchange:
        async def fetch_bars(self, symbol, limit=720):
            return _df(0, 20)

    counts = asyncio.run(dc.collect(_FakeExchange(), ["ETH/BTC", "SOL/BTC"]))
    assert counts == {"ETH/BTC": 20, "SOL/BTC": 20}
    loaded = dc.load_all(["ETH/BTC", "SOL/BTC"])
    assert set(loaded) == {"ETH/BTC", "SOL/BTC"}
    assert len(loaded["ETH/BTC"]) == 20
