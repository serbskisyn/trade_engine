"""
llm_cache.py — persistent cache for LLM verdicts in backtests (harness inc. 5).

A backtest that models the live confidence gate must call the LLM per technical
signal — expensive, and the autoresearch harness re-runs the backtest many times
with different params. This caches each verdict keyed by (model, prompt) so it's
computed ONCE and reused across runs.

CAVEAT: this is an approximation. The LLM judges a historical bar's indicators
"as if now" (sentiment is neutral-stubbed, not point-in-time). Good for the
relative question "does the LLM gate filter the bad technical signals?" — not a
faithful point-in-time backtest.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "backtest" / "llm_cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    key        TEXT PRIMARY KEY,
    signal     TEXT,
    confidence REAL,
    reason     TEXT
);
"""


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    return conn


def _key(model: str, prompt: str) -> str:
    return hashlib.sha256(f"{model}\x00{prompt}".encode("utf-8")).hexdigest()


def get(model: str, prompt: str) -> dict | None:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT signal, confidence, reason FROM verdicts WHERE key = ?",
            (_key(model, prompt),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"signal": row[0], "confidence": row[1], "reason": row[2]}


def put(model: str, prompt: str, verdict: dict) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO verdicts (key, signal, confidence, reason) VALUES (?, ?, ?, ?)",
            (_key(model, prompt), verdict.get("signal", "hold"),
             float(verdict.get("confidence", 0.0)), str(verdict.get("reason", ""))[:500]),
        )
        conn.commit()
    finally:
        conn.close()


async def cached_verdict(prompt: str, market: str, model: str, call_fn) -> dict:
    """Return the cached verdict for (model, prompt), else call_fn(prompt, market),
    cache it and return. call_fn is async → {signal, confidence, reason}."""
    hit = get(model, prompt)
    if hit is not None:
        return hit
    verdict = await call_fn(prompt, market)
    put(model, prompt, verdict)
    return verdict
