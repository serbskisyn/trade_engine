"""Tests für KrakenExchange._place_limit_with_fallback — Limit-Order-Logik."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.exchanges.kraken import KrakenExchange
from app import config


def _make_exchange() -> KrakenExchange:
    ex = KrakenExchange.__new__(KrakenExchange)
    ex._ex = MagicMock()
    return ex


# ── _mid_price ────────────────────────────────────────────────────────────────

async def test_mid_price_uses_bid_ask_average():
    ex = _make_exchange()
    ex._ex.fetch_ticker = MagicMock(return_value={"bid": 1.00, "ask": 1.02, "last": 1.01})
    with patch("app.exchanges.kraken._run", side_effect=lambda fn, *a, **kw: asyncio.coroutine(lambda: fn(*a, **kw))()):
        pass
    # Direkter Aufruf mit gemocktem _run
    async def fake_run(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    with patch("app.exchanges.kraken._run", side_effect=fake_run):
        mid = await ex._mid_price("ETH/BTC")
    assert mid == pytest.approx(1.01)


async def test_mid_price_falls_back_to_last_when_no_bid_ask():
    ex = _make_exchange()
    ex._ex.fetch_ticker = MagicMock(return_value={"bid": None, "ask": None, "last": 1.05})
    async def fake_run(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    with patch("app.exchanges.kraken._run", side_effect=fake_run):
        mid = await ex._mid_price("ETH/BTC")
    assert mid == pytest.approx(1.05)


# ── _place_limit_with_fallback: Limit wird sofort gefüllt ─────────────────────

async def test_limit_filled_within_timeout_returns_filled_order(monkeypatch):
    monkeypatch.setattr(config, "KRAKEN_LIMIT_TIMEOUT", 10)
    ex = _make_exchange()

    limit_order = {"id": "123", "status": "open", "average": None, "price": 1.01}
    filled_order = {"id": "123", "status": "closed", "average": 1.009, "price": 1.01}

    call_seq = iter([limit_order, filled_order])

    async def fake_run(fn, *args, **kwargs):
        if fn == ex._ex.fetch_order:
            return next(call_seq)
        return next(call_seq)

    with patch("app.exchanges.kraken._run", side_effect=fake_run):
        with patch("app.exchanges.kraken.KrakenExchange._mid_price",
                   new=AsyncMock(return_value=1.01)):
            with patch("asyncio.sleep", new=AsyncMock()):
                order = await ex._place_limit_with_fallback("ETH/BTC", "buy", 1.0, {})

    assert order is not None
    assert order["status"] == "closed"


# ── _place_limit_with_fallback: Timeout → Market-Fallback ────────────────────

async def test_limit_timeout_triggers_cancel_and_market_fallback(monkeypatch):
    monkeypatch.setattr(config, "KRAKEN_LIMIT_TIMEOUT", 0)  # sofortiger Timeout
    ex = _make_exchange()

    market_order = {"id": "999", "status": "closed", "average": 1.02, "price": 1.02}

    create_calls = []

    async def fake_run(fn, *args, **kwargs):
        if fn == ex._ex.create_order:
            create_calls.append(args)
            if "market" in args:
                return market_order
            return {"id": "123", "status": "open"}
        if fn == ex._ex.cancel_order:
            return {}
        return {}

    with patch("app.exchanges.kraken._run", side_effect=fake_run):
        with patch("app.exchanges.kraken.KrakenExchange._mid_price",
                   new=AsyncMock(return_value=1.01)):
            with patch("asyncio.sleep", new=AsyncMock()):
                order = await ex._place_limit_with_fallback("ETH/BTC", "buy", 1.0, {})

    assert order == market_order
    order_types = [a[1] for a in create_calls]  # index 1 = order type arg
    assert "limit" in order_types
    assert "market" in order_types


# ── _place_limit_with_fallback: mid_price None → direkt Market ───────────────

async def test_no_mid_price_goes_directly_to_market(monkeypatch):
    ex = _make_exchange()
    market_order = {"id": "1", "status": "closed", "average": 1.0}

    async def fake_run(fn, *args, **kwargs):
        return market_order

    with patch("app.exchanges.kraken._run", side_effect=fake_run):
        with patch("app.exchanges.kraken.KrakenExchange._mid_price",
                   new=AsyncMock(return_value=None)):
            order = await ex._place_limit_with_fallback("ETH/BTC", "buy", 1.0, {})

    assert order == market_order
