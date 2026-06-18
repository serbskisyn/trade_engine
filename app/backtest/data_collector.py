"""
data_collector.py — persist Kraken OHLCV history for backtests (harness inc. 2).

Kraken's public OHLC endpoint only returns the last ~720 candles, so a backtest
over a longer window needs accumulated history. This collector appends each
fetch into a dedup'd SQLite store (PRIMARY KEY (symbol, timeframe, ts_ms) +
INSERT OR REPLACE), so running it periodically GROWS the history beyond 720.

load_all() returns exactly the dict[symbol, OHLCV-DataFrame] that
BacktestExchange consumes — same columns/timestamp dtype as live fetch_bars.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "backtest" / "history.db"
_COLS = ["timestamp", "open", "high", "low", "close", "volume"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol    TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts_ms     INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, timeframe, ts_ms)
);
"""


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    return conn


def save_history(symbol: str, df: pd.DataFrame, timeframe: str = "5m") -> int:
    """Merge a freshly-fetched OHLCV frame into the store. Returns rows written."""
    if df is None or df.empty:
        return 0
    # Epoch milliseconds, independent of the datetime64 resolution (pandas 3
    # may use ms-resolution, where astype("int64") is already ms, not ns).
    _epoch = pd.Timestamp("1970-01-01", tz="UTC")
    ts = ((pd.to_datetime(df["timestamp"], utc=True) - _epoch)
          // pd.Timedelta(milliseconds=1)).astype("int64")
    rows = [
        (symbol, timeframe, int(t), float(o), float(h), float(lo), float(c), float(v))
        for t, o, h, lo, c, v in zip(ts, df["open"], df["high"], df["low"], df["close"], df["volume"])
    ]
    conn = _conn()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO bars "
            "(symbol, timeframe, ts_ms, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def load_history(symbol: str, timeframe: str = "5m") -> pd.DataFrame | None:
    """Load stored history as an OHLCV frame (same shape as live fetch_bars)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT ts_ms, open, high, low, close, volume FROM bars "
            "WHERE symbol = ? AND timeframe = ? ORDER BY ts_ms",
            (symbol, timeframe),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts_ms", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    return df[_COLS]


def count(symbol: str, timeframe: str = "5m") -> int:
    conn = _conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM bars WHERE symbol = ? AND timeframe = ?",
            (symbol, timeframe),
        ).fetchone()[0]
    finally:
        conn.close()


async def collect(exchange, symbols: list[str], timeframe: str = "5m",
                  limit: int = 720) -> dict[str, int]:
    """Fetch the latest bars for each symbol and append to the store.

    Run periodically (cron) — each run extends the persisted history. Returns
    {symbol: total_bars_in_store_after_save}.
    """
    out: dict[str, int] = {}
    for sym in symbols:
        try:
            df = await exchange.fetch_bars(sym, limit=limit)
            written = save_history(sym, df, timeframe)
            total = count(sym, timeframe)
            out[sym] = total
            logger.info("collect: %s +%d bars → %d total", sym, written, total)
        except Exception as exc:
            logger.warning("collect: %s failed: %s", sym, exc)
            out[sym] = count(sym, timeframe)
    return out


def load_all(symbols: list[str], timeframe: str = "5m") -> dict[str, pd.DataFrame]:
    """Load every symbol's stored history → ready for BacktestExchange(bars=...)."""
    result = {}
    for sym in symbols:
        df = load_history(sym, timeframe)
        if df is not None and not df.empty:
            result[sym] = df
    return result
