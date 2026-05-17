"""
Trade Manager — persists open positions in SQLite, enforces stop-loss and trailing stop.
Stop-loss:      close if unrealized loss exceeds market-specific threshold
                (crypto default 3%, stocks default 1.5%).
Trailing stop:  activate when profit exceeds market-specific threshold,
                then close when price drops > trail-pct from peak.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Literal

import aiosqlite

from app import config
from app.config import (DB_PATH, STOP_LOSS_PCT, TRAILING_ACTIVATE_PCT, TRAILING_TRAIL_PCT,
                        CIRCUIT_BREAKER_MAX_LOSS_BTC, CIRCUIT_BREAKER_WINDOW)

logger = logging.getLogger(__name__)

Market = Literal["crypto", "stocks"]


# ── Pro-Markt Stop-Parameter ──────────────────────────────────────────────────

def _stop_loss_pct(market: Market) -> float:
    """Look up via module so test-monkeypatch auf trade_manager-Konstanten greift."""
    if market == "crypto":
        return getattr(config, "STOP_LOSS_PCT_CRYPTO", STOP_LOSS_PCT)
    if market == "stocks":
        return getattr(config, "STOP_LOSS_PCT_STOCKS", STOP_LOSS_PCT)
    return STOP_LOSS_PCT


def _trailing_activate_pct(market: Market) -> float:
    if market == "crypto":
        return getattr(config, "TRAILING_ACTIVATE_PCT_CRYPTO", TRAILING_ACTIVATE_PCT)
    if market == "stocks":
        return getattr(config, "TRAILING_ACTIVATE_PCT_STOCKS", TRAILING_ACTIVATE_PCT)
    return TRAILING_ACTIVATE_PCT


def _trailing_trail_pct(market: Market) -> float:
    if market == "crypto":
        return getattr(config, "TRAILING_TRAIL_PCT_CRYPTO", TRAILING_TRAIL_PCT)
    if market == "stocks":
        return getattr(config, "TRAILING_TRAIL_PCT_STOCKS", TRAILING_TRAIL_PCT)
    return TRAILING_TRAIL_PCT


# ── Singleton DB Connection ───────────────────────────────────────────────────
_conn: aiosqlite.Connection | None = None
_init_lock = asyncio.Lock()


async def _init_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            market        TEXT NOT NULL,
            symbol        TEXT NOT NULL,
            side          TEXT NOT NULL DEFAULT 'buy',
            entry_price   REAL NOT NULL,
            qty           REAL NOT NULL,
            peak_price    REAL NOT NULL,
            trailing_active INTEGER NOT NULL DEFAULT 0,
            opened_at     TEXT NOT NULL,
            candles_held  INTEGER NOT NULL DEFAULT 0,
            UNIQUE(market, symbol)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            market      TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            side        TEXT NOT NULL,
            entry_price REAL,
            exit_price  REAL,
            qty         REAL,
            pl_pct      REAL,
            pl_abs      REAL,
            reason      TEXT,
            closed_at   TEXT NOT NULL
        )
    """)
    await conn.commit()


async def _get_db() -> aiosqlite.Connection:
    """Returns the singleton aiosqlite connection. Lazy-initialized + thread-safe."""
    global _conn
    if _conn is not None:
        return _conn
    async with _init_lock:
        if _conn is not None:
            return _conn
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = await aiosqlite.connect(DB_PATH)
        await _init_schema(conn)
        _conn = conn
        return conn


async def close_db() -> None:
    """Closes the singleton connection. Idempotent. Useful for tests + clean shutdown."""
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


# ── Position CRUD ─────────────────────────────────────────────────────────────

async def open_position(market: Market, symbol: str, entry_price: float, qty: float,
                        side: str = "long") -> None:
    conn = await _get_db()
    await conn.execute("""
        INSERT OR REPLACE INTO positions
          (market, symbol, side, entry_price, qty, peak_price, trailing_active, opened_at, candles_held)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, 0)
    """, (market, symbol, side, entry_price, qty, entry_price,
           datetime.now(timezone.utc).isoformat()))
    await conn.commit()
    logger.info("[TradeManager] Opened %s %s %s @ %.8f qty=%.4f",
                side.upper(), market, symbol, entry_price, qty)


async def close_position(market: Market, symbol: str, exit_price: float, reason: str) -> dict | None:
    conn = await _get_db()
    async with conn.execute(
        "SELECT entry_price, qty, side FROM positions WHERE market=? AND symbol=?",
        (market, symbol)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    entry_price, qty, pos_side = row

    if pos_side == "short":
        pl_abs = (entry_price - exit_price) * qty
        pl_pct = (entry_price - exit_price) / entry_price * 100
    else:
        pl_abs = (exit_price - entry_price) * qty
        pl_pct = (exit_price - entry_price) / entry_price * 100

    await conn.execute("""
        INSERT INTO trade_log (market, symbol, side, entry_price, exit_price, qty, pl_pct, pl_abs, reason, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (market, symbol, pos_side, entry_price, exit_price, qty, pl_pct, pl_abs, reason,
           datetime.now(timezone.utc).isoformat()))
    await conn.execute("DELETE FROM positions WHERE market=? AND symbol=?", (market, symbol))
    await conn.commit()
    logger.info("[TradeManager] Closed %s %s %s @ %.8f | P&L: %+.2f%% reason=%s",
                pos_side.upper(), market, symbol, exit_price, pl_pct, reason)
    return {"symbol": symbol, "pl_pct": pl_pct, "pl_abs": pl_abs,
            "reason": reason, "side": pos_side,
            "entry_price": entry_price, "exit_price": exit_price, "qty": qty}


async def get_open_positions(market: Market | None = None) -> list[dict]:
    conn = await _get_db()
    query  = "SELECT * FROM positions"
    params: tuple = ()
    if market:
        query  += " WHERE market=?"
        params  = (market,)
    async with conn.execute(query, params) as cur:
        cols = [d[0] for d in cur.description]
        rows = await cur.fetchall()
    return [dict(zip(cols, r)) for r in rows]


async def increment_candles(market: Market, symbol: str) -> None:
    conn = await _get_db()
    await conn.execute(
        "UPDATE positions SET candles_held = candles_held + 1 WHERE market=? AND symbol=?",
        (market, symbol)
    )
    await conn.commit()


async def update_peak(market: Market, symbol: str, current_price: float) -> None:
    conn = await _get_db()
    async with conn.execute(
        "SELECT peak_price, entry_price, side FROM positions WHERE market=? AND symbol=?",
        (market, symbol)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return
    peak_price, entry_price, pos_side = row
    activate_pct = _trailing_activate_pct(market)
    if pos_side == "short":
        # For shorts: peak_price tracks the trough (minimum seen)
        trailing_active = int((entry_price - current_price) / entry_price >= activate_pct)
        new_peak        = min(peak_price, current_price)
    else:
        trailing_active = int((current_price - entry_price) / entry_price >= activate_pct)
        new_peak        = max(peak_price, current_price)
    await conn.execute(
        "UPDATE positions SET peak_price=?, trailing_active=? WHERE market=? AND symbol=?",
        (new_peak, trailing_active, market, symbol)
    )
    await conn.commit()


# ── Stop-Loss / Trailing-Stop Check ──────────────────────────────────────────

async def check_stops(market: Market, symbol: str, current_price: float,
                      candles_held: int) -> tuple[bool, str]:
    """Returns (should_close, reason). Side-aware: long and short handled separately."""
    conn = await _get_db()
    async with conn.execute(
        "SELECT entry_price, peak_price, trailing_active, candles_held, side FROM positions WHERE market=? AND symbol=?",
        (market, symbol)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return False, ""
    entry_price, peak_price, trailing_active, db_candles, pos_side = row

    await update_peak(market, symbol, current_price)

    stop_pct  = _stop_loss_pct(market)
    trail_pct = _trailing_trail_pct(market)

    if pos_side == "short":
        loss_pct = (current_price - entry_price) / entry_price
        if loss_pct >= stop_pct:
            return True, f"stop_loss_short ({loss_pct*100:.2f}%)"
        if trailing_active and peak_price > 0:
            rise_from_trough = (current_price - peak_price) / peak_price
            if rise_from_trough >= trail_pct:
                return True, f"trailing_stop_short (trough={peak_price:.4f}, rise={rise_from_trough*100:.2f}%)"
    else:
        loss_pct = (entry_price - current_price) / entry_price
        if loss_pct >= stop_pct:
            return True, f"stop_loss ({loss_pct*100:.2f}%)"
        if trailing_active and peak_price > 0:
            drop_from_peak = (peak_price - current_price) / peak_price
            if drop_from_peak >= trail_pct:
                return True, f"trailing_stop (peak={peak_price:.4f}, drop={drop_from_peak*100:.2f}%)"

    return False, ""


async def get_recent_trades(limit: int = 3) -> list[dict]:
    conn = await _get_db()
    async with conn.execute("""
        SELECT symbol, side, entry_price, exit_price, qty, pl_pct, pl_abs, reason, closed_at
        FROM trade_log ORDER BY closed_at DESC LIMIT ?
    """, (limit,)) as cur:
        cols = [d[0] for d in cur.description]
        rows = await cur.fetchall()
    return [dict(zip(cols, r)) for r in rows]


async def get_trade_stats() -> dict:
    conn = await _get_db()
    async with conn.execute("""
        SELECT COUNT(*), SUM(pl_abs), AVG(pl_pct),
               SUM(CASE WHEN pl_abs > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN pl_abs <= 0 THEN 1 ELSE 0 END)
        FROM trade_log
    """) as cur:
        row = await cur.fetchone()
    total, total_pl, avg_pct, wins, losses = row
    return {
        "total_trades": total or 0,
        "total_pl":     round(total_pl or 0, 8),
        "avg_pl_pct":   round(avg_pct or 0, 2),
        "wins":         wins or 0,
        "losses":       losses or 0,
        "win_rate":     round((wins or 0) / max(total or 1, 1) * 100, 1),
    }


# ── Circuit Breaker ───────────────────────────────────────────────────────────

async def _recent_pl_sum(window: int) -> float:
    """Summe der pl_abs der letzten N Trades aus trade_log."""
    conn = await _get_db()
    async with conn.execute("""
        SELECT SUM(pl_abs) FROM (
            SELECT pl_abs FROM trade_log ORDER BY closed_at DESC LIMIT ?
        )
    """, (window,)) as cur:
        row = await cur.fetchone()
    return float(row[0] or 0)


# Singleton — wird beim Import gebaut, P&L kommt aus dieser Modul-Funktion.
from app.engine.circuit_breaker import CircuitBreaker  # noqa: E402

circuit_breaker = CircuitBreaker(
    max_loss=CIRCUIT_BREAKER_MAX_LOSS_BTC,
    window=CIRCUIT_BREAKER_WINDOW,
    recent_pl_provider=_recent_pl_sum,
)


async def check_circuit_breaker() -> tuple[bool, str]:
    """Backward-compat-Wrapper für routes.py / scanner.py."""
    return await circuit_breaker.check()


def reset_circuit_breaker(hours: int = 1) -> None:
    """Backward-compat-Wrapper für routes.py."""
    circuit_breaker.reset(hours=hours)
