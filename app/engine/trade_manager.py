"""
Trade Manager — persists open positions in SQLite, enforces stop-loss and trailing stop.
Stop-loss:      close if unrealized loss > STOP_LOSS_PCT (default 2%)
Trailing stop:  activate when profit > TRAILING_ACTIVATE_PCT (default 2%),
                then close when price drops > TRAILING_TRAIL_PCT (default 1%) from peak.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Literal

import aiosqlite

from app.config import DB_PATH, STOP_LOSS_PCT, TRAILING_ACTIVATE_PCT, TRAILING_TRAIL_PCT

logger = logging.getLogger(__name__)

Market = Literal["crypto", "stocks"]


async def _db() -> aiosqlite.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH)
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
    return conn


# ── Position CRUD ─────────────────────────────────────────────────────────────

async def open_position(market: Market, symbol: str, entry_price: float, qty: float,
                        side: str = "long") -> None:
    conn = await _db()
    try:
        await conn.execute("""
            INSERT OR REPLACE INTO positions
              (market, symbol, side, entry_price, qty, peak_price, trailing_active, opened_at, candles_held)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, 0)
        """, (market, symbol, side, entry_price, qty, entry_price,
               datetime.now(timezone.utc).isoformat()))
        await conn.commit()
        logger.info("[TradeManager] Opened %s %s %s @ %.8f qty=%.4f",
                    side.upper(), market, symbol, entry_price, qty)
    finally:
        await conn.close()


async def close_position(market: Market, symbol: str, exit_price: float, reason: str) -> dict | None:
    conn = await _db()
    try:
        async with conn.execute(
            "SELECT entry_price, qty, side FROM positions WHERE market=? AND symbol=?",
            (market, symbol)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        entry_price, qty, pos_side = row

        # P&L depends on direction
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
                "reason": reason, "side": pos_side}
    finally:
        await conn.close()


async def get_open_positions(market: Market | None = None) -> list[dict]:
    conn = await _db()
    try:
        query  = "SELECT * FROM positions"
        params: tuple = ()
        if market:
            query  += " WHERE market=?"
            params  = (market,)
        async with conn.execute(query, params) as cur:
            cols = [d[0] for d in cur.description]
            rows = await cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        await conn.close()


async def increment_candles(market: Market, symbol: str) -> None:
    conn = await _db()
    try:
        await conn.execute(
            "UPDATE positions SET candles_held = candles_held + 1 WHERE market=? AND symbol=?",
            (market, symbol)
        )
        await conn.commit()
    finally:
        await conn.close()


async def update_peak(market: Market, symbol: str, current_price: float) -> None:
    conn = await _db()
    try:
        async with conn.execute(
            "SELECT peak_price, entry_price FROM positions WHERE market=? AND symbol=?",
            (market, symbol)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return
        peak_price, entry_price = row
        trailing_active = int((current_price - entry_price) / entry_price >= TRAILING_ACTIVATE_PCT)
        new_peak        = max(peak_price, current_price)
        await conn.execute(
            "UPDATE positions SET peak_price=?, trailing_active=? WHERE market=? AND symbol=?",
            (new_peak, trailing_active, market, symbol)
        )
        await conn.commit()
    finally:
        await conn.close()


# ── Stop-Loss / Trailing-Stop Check ──────────────────────────────────────────

async def check_stops(market: Market, symbol: str, current_price: float,
                      candles_held: int) -> tuple[bool, str]:
    """Returns (should_close, reason). Side-aware: long and short handled separately."""
    conn = await _db()
    try:
        async with conn.execute(
            "SELECT entry_price, peak_price, trailing_active, candles_held, side FROM positions WHERE market=? AND symbol=?",
            (market, symbol)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False, ""
        entry_price, peak_price, trailing_active, db_candles, pos_side = row

        await update_peak(market, symbol, current_price)

        if pos_side == "short":
            # Short: loss = price went UP from entry
            loss_pct = (current_price - entry_price) / entry_price
            if loss_pct >= STOP_LOSS_PCT:
                return True, f"stop_loss_short ({loss_pct*100:.2f}%)"
            # Trailing for short: peak_price = lowest price seen
            if trailing_active and peak_price > 0:
                rise_from_trough = (current_price - peak_price) / peak_price
                if rise_from_trough >= TRAILING_TRAIL_PCT:
                    return True, f"trailing_stop_short (trough={peak_price:.4f}, rise={rise_from_trough*100:.2f}%)"
        else:
            # Long: loss = price went DOWN from entry
            loss_pct = (entry_price - current_price) / entry_price
            if loss_pct >= STOP_LOSS_PCT:
                return True, f"stop_loss ({loss_pct*100:.2f}%)"
            if trailing_active and peak_price > 0:
                drop_from_peak = (peak_price - current_price) / peak_price
                if drop_from_peak >= TRAILING_TRAIL_PCT:
                    return True, f"trailing_stop (peak={peak_price:.4f}, drop={drop_from_peak*100:.2f}%)"

        return False, ""
    finally:
        await conn.close()


async def get_recent_trades(limit: int = 3) -> list[dict]:
    conn = await _db()
    try:
        async with conn.execute("""
            SELECT symbol, side, entry_price, exit_price, qty, pl_pct, pl_abs, reason, closed_at
            FROM trade_log ORDER BY closed_at DESC LIMIT ?
        """, (limit,)) as cur:
            cols = [d[0] for d in cur.description]
            rows = await cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        await conn.close()


async def get_trade_stats() -> dict:
    conn = await _db()
    try:
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
    finally:
        await conn.close()
