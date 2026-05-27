"""Tests for the global TRADING_DRY_RUN mode (no real orders placed)."""
import pytest

from app.exchanges import kraken as kraken_mod
from app.exchanges.base import Side


@pytest.fixture
def kx(monkeypatch):
    ex = kraken_mod.KrakenExchange()
    # public ticker stub — _run executes it in an executor
    monkeypatch.setattr(ex._ex, "fetch_ticker", lambda s: {"last": 100.0})
    # track whether the REAL placement path is reached
    calls = {"placed": False}
    async def _record(*a, **k):
        calls["placed"] = True
        return {"id": "X", "average": 100.0}
    monkeypatch.setattr(ex, "_place_limit_with_fallback", _record)
    ex._calls = calls
    return ex


@pytest.mark.anyio
async def test_dry_run_place_order_simulates(monkeypatch, kx):
    monkeypatch.setattr(kraken_mod, "TRADING_DRY_RUN", True)
    res = await kx.place_order("ETH/BTC", Side.BUY, 0.001)
    assert res is not None
    assert res.order_id.startswith("DRYRUN-")
    assert res.price == 100.0
    assert res.symbol == "ETH/BTC"
    assert kx._calls["placed"] is False  # real placement NEVER reached


@pytest.mark.anyio
async def test_dry_run_close_position_simulates(monkeypatch, kx):
    monkeypatch.setattr(kraken_mod, "TRADING_DRY_RUN", True)
    ok = await kx.close_position("ETH/BTC", side="long", qty=0.5)
    assert ok is True
    assert kx._calls["placed"] is False


@pytest.mark.anyio
async def test_live_mode_reaches_real_placement(monkeypatch, kx):
    """With dry-run OFF, place_order DOES reach the real placement path."""
    monkeypatch.setattr(kraken_mod, "TRADING_DRY_RUN", False)
    res = await kx.place_order("ETH/BTC", Side.BUY, 0.001)
    assert kx._calls["placed"] is True
    assert res is not None and not res.order_id.startswith("DRYRUN-")
