"""Tests für CircuitBreaker — Loss-Limit Check + Reset-Override."""
import pytest

from app.engine.circuit_breaker import CircuitBreaker


def _provider_returning(value: float):
    async def _provider(window: int) -> float:
        return value
    return _provider


async def test_circuit_breaker_inactive_when_loss_below_limit():
    """Recent P&L > max_loss → Breaker bleibt offen, Entries erlaubt."""
    cb = CircuitBreaker(max_loss=-0.003, window=10,
                        recent_pl_provider=_provider_returning(0.001))
    active, reason = await cb.check()
    assert active is False
    assert reason == ""


async def test_circuit_breaker_active_when_loss_exceeds_limit():
    """Summe < max_loss → Breaker triggert, Entries blockiert."""
    cb = CircuitBreaker(max_loss=-0.003, window=10,
                        recent_pl_provider=_provider_returning(-0.005))
    active, reason = await cb.check()
    assert active is True
    assert "-0.005" in reason
    assert "10 Trades" in reason


async def test_circuit_breaker_reset_overrides_active_state():
    """Nach reset(hours=1) bleibt Breaker für 1h inaktiv, auch wenn Loss-Limit verletzt."""
    cb = CircuitBreaker(max_loss=-0.003, window=10,
                        recent_pl_provider=_provider_returning(-0.005))
    # Pre-Reset: aktiv
    active, _ = await cb.check()
    assert active is True
    # Override
    cb.reset(hours=1)
    assert cb.override_until is not None
    # Post-Reset: inaktiv trotz unverändertem Loss
    active, reason = await cb.check()
    assert active is False
    assert reason == ""


async def test_circuit_breaker_reset_with_zero_hours_still_creates_window():
    """Reset(hours=0) setzt override_until auf jetzt → läuft sofort ab, Breaker wieder aktiv."""
    cb = CircuitBreaker(max_loss=-0.003, window=10,
                        recent_pl_provider=_provider_returning(-0.005))
    cb.reset(hours=0)
    # `now < override_until` ist False, Loss-Check greift wieder
    active, _ = await cb.check()
    assert active is True
