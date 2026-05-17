"""
Circuit Breaker — kapselt Loss-Limit-Check + manuelles Override.

Wird neue Entries blockieren, wenn die Summe der letzten N Trade-P&Ls
einen konfigurierten Loss-Threshold unterschreitet. Manuelles Reset
über `reset(hours)` lässt den Breaker für den angegebenen Zeitraum
inaktiv (Stops + Exits laufen immer, der Breaker betrifft nur Entries).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

RecentPLProvider = Callable[[int], Awaitable[float]]


class CircuitBreaker:
    def __init__(
        self,
        max_loss: float,
        window: int,
        recent_pl_provider: RecentPLProvider,
    ) -> None:
        """
        :param max_loss: negativer Schwellwert (z.B. -0.003 BTC). Wenn Summe < max_loss → triggert.
        :param window:   Anzahl Trades, die in die Summe einfließen.
        :param recent_pl_provider: async Callable(window) → Summe der pl_abs der letzten N Trades.
        """
        self.max_loss      = max_loss
        self.window        = window
        self._provider     = recent_pl_provider
        self._override_until: datetime | None = None

    async def check(self) -> tuple[bool, str]:
        """Returns (active, reason). active=True heißt: Entries gesperrt."""
        now = datetime.now(timezone.utc)
        if self._override_until and now < self._override_until:
            return False, ""
        recent_pl = await self._provider(self.window)
        if recent_pl < self.max_loss:
            return True, (f"{recent_pl:.6f} BTC Verlust in letzten "
                          f"{self.window} Trades (Limit: {self.max_loss} BTC)")
        return False, ""

    def reset(self, hours: int = 1) -> None:
        """Override the breaker for `hours` hours. Idempotent — überschreibt vorhandenes Override."""
        self._override_until = datetime.now(timezone.utc) + timedelta(hours=hours)
        logger.warning("[CircuitBreaker] Manuell zurückgesetzt für %d Stunde(n)", hours)

    @property
    def override_until(self) -> datetime | None:
        return self._override_until
