"""Tests für trade_manager — Stop-Loss + Trailing-Stop für Long und Short."""
import pytest

from app.engine import trade_manager as tm


# ── Long: Stop-Loss ───────────────────────────────────────────────────────────

async def test_long_stop_loss_triggered_when_loss_exceeds_pct(isolated_db, monkeypatch):
    """Long mit 3% Verlust > STOP_LOSS_PCT=2% → Stop muss greifen."""
    monkeypatch.setattr(tm, "STOP_LOSS_PCT", 0.02)
    await tm.open_position("crypto", "BTC/USD", entry_price=100.0, qty=1.0, side="long")
    stop, reason = await tm.check_stops("crypto", "BTC/USD", current_price=97.0, candles_held=5)
    assert stop is True
    assert "stop_loss" in reason


async def test_long_stop_loss_not_triggered_when_loss_below_pct(isolated_db, monkeypatch):
    """Long mit 1% Verlust < STOP_LOSS_PCT=2% → kein Stop."""
    monkeypatch.setattr(tm, "STOP_LOSS_PCT", 0.02)
    await tm.open_position("crypto", "BTC/USD", entry_price=100.0, qty=1.0, side="long")
    stop, reason = await tm.check_stops("crypto", "BTC/USD", current_price=99.0, candles_held=5)
    assert stop is False


# ── Long: Trailing Stop ───────────────────────────────────────────────────────

async def test_long_trailing_stop_activates_and_triggers(isolated_db, monkeypatch):
    """Long: nach >2% Profit aktiviert sich Trailing; bei >1% Drop vom Peak → Exit."""
    monkeypatch.setattr(tm, "STOP_LOSS_PCT", 0.10)  # weit weg
    monkeypatch.setattr(tm, "TRAILING_ACTIVATE_PCT", 0.02)
    monkeypatch.setattr(tm, "TRAILING_TRAIL_PCT", 0.01)
    await tm.open_position("crypto", "BTC/USD", entry_price=100.0, qty=1.0, side="long")

    # Preis steigt auf 105 — Trailing aktiviert sich, Peak = 105
    stop, _ = await tm.check_stops("crypto", "BTC/USD", current_price=105.0, candles_held=5)
    assert stop is False

    # Preis fällt auf 103.5 → 1.43% Drop vom Peak (105) > 1% → Exit
    stop, reason = await tm.check_stops("crypto", "BTC/USD", current_price=103.5, candles_held=6)
    assert stop is True
    assert "trailing_stop" in reason and "short" not in reason


# ── Short: Stop-Loss ──────────────────────────────────────────────────────────

async def test_short_stop_loss_triggered_when_price_rises(isolated_db, monkeypatch):
    """Short: Preis 3% über Entry → STOP_LOSS_PCT=2% greift."""
    monkeypatch.setattr(tm, "STOP_LOSS_PCT", 0.02)
    await tm.open_position("crypto", "BTC/USD", entry_price=100.0, qty=1.0, side="short")
    stop, reason = await tm.check_stops("crypto", "BTC/USD", current_price=103.0, candles_held=5)
    assert stop is True
    assert "stop_loss_short" in reason


async def test_short_stop_loss_not_triggered_when_price_falls(isolated_db, monkeypatch):
    """Short profitabel (Preis fällt) → kein Stop."""
    monkeypatch.setattr(tm, "STOP_LOSS_PCT", 0.02)
    await tm.open_position("crypto", "BTC/USD", entry_price=100.0, qty=1.0, side="short")
    stop, _ = await tm.check_stops("crypto", "BTC/USD", current_price=98.0, candles_held=5)
    assert stop is False


# ── Short: Trailing Stop (Trough-Tracking) ────────────────────────────────────

async def test_short_trailing_stop_tracks_trough_and_triggers_on_rise(isolated_db, monkeypatch):
    """Short: nach >2% Profit (Preis sinkt) aktiviert sich Trailing am Tief;
       wenn Preis vom Tief um >1% steigt → Exit."""
    monkeypatch.setattr(tm, "STOP_LOSS_PCT", 0.10)
    monkeypatch.setattr(tm, "TRAILING_ACTIVATE_PCT", 0.02)
    monkeypatch.setattr(tm, "TRAILING_TRAIL_PCT", 0.01)
    await tm.open_position("crypto", "BTC/USD", entry_price=100.0, qty=1.0, side="short")

    # Preis sinkt auf 95 → 5% Profit, Trailing aktiviert, Trough = 95
    stop, _ = await tm.check_stops("crypto", "BTC/USD", current_price=95.0, candles_held=5)
    assert stop is False

    # Preis steigt auf 96 → 1.05% Anstieg vom Trough → Exit
    stop, reason = await tm.check_stops("crypto", "BTC/USD", current_price=96.0, candles_held=6)
    assert stop is True
    assert "trailing_stop_short" in reason


# ── Bonus: update_peak korrekt für long und short ─────────────────────────────

async def test_update_peak_long_only_grows(isolated_db, monkeypatch):
    """Long: peak_price ist Maximum-Tracker, sinkende Preise verändern es nicht."""
    monkeypatch.setattr(tm, "TRAILING_ACTIVATE_PCT", 0.02)
    await tm.open_position("crypto", "ETH/USD", entry_price=100.0, qty=1.0, side="long")
    await tm.update_peak("crypto", "ETH/USD", 105.0)
    await tm.update_peak("crypto", "ETH/USD", 103.0)
    positions = await tm.get_open_positions("crypto")
    assert positions[0]["peak_price"] == 105.0


async def test_update_peak_short_only_shrinks(isolated_db, monkeypatch):
    """Short: peak_price ist Minimum-Tracker (Trough), steigende Preise verändern es nicht."""
    monkeypatch.setattr(tm, "TRAILING_ACTIVATE_PCT", 0.02)
    await tm.open_position("crypto", "ETH/USD", entry_price=100.0, qty=1.0, side="short")
    await tm.update_peak("crypto", "ETH/USD", 95.0)
    await tm.update_peak("crypto", "ETH/USD", 97.0)
    positions = await tm.get_open_positions("crypto")
    assert positions[0]["peak_price"] == 95.0
