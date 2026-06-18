"""
FastAPI REST-API — consumed by Serbo_bot for /tradebot and /stocks commands.
All endpoints require X-API-Secret header matching API_SECRET from config.
"""
import logging
from datetime import datetime, timezone

import asyncio
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import API_SECRET, ALPACA_API_KEY, KRAKEN_API_KEY, TRADING_DRY_RUN
from app.engine import trade_manager as tm

logger = logging.getLogger(__name__)
app    = FastAPI(title="Trade Engine API", version="1.0.0")

# shared notify_fn injected by main.py
_notify_fn = None


def set_notify(fn):
    global _notify_fn
    _notify_fn = fn


def _auth(secret: str):
    if secret != API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


# ── Status ────────────────────────────────────────────────────────────────────

@app.get("/status")
async def status(x_api_secret: str = Header(default="")):
    _auth(x_api_secret)
    crypto_pos = await tm.get_open_positions("crypto")
    stocks_pos = await tm.get_open_positions("stocks")
    stats, fee_stats = await asyncio.gather(tm.get_trade_stats(), tm.get_fee_stats())
    result = {
        "crypto": {
            "enabled":   bool(KRAKEN_API_KEY),
            "positions": crypto_pos,
        },
        "stocks": {
            "enabled":   bool(ALPACA_API_KEY),
            "positions": stocks_pos,
        },
        "stats":     stats,
        "fee_stats": fee_stats,
    }
    if KRAKEN_API_KEY:
        try:
            from app.exchanges.kraken import get_kraken
            result["crypto"]["account"] = await get_kraken().get_account_info()
        except Exception:
            pass
    if ALPACA_API_KEY:
        try:
            from app.exchanges.alpaca import get_alpaca
            alpaca = get_alpaca()
            result["stocks"]["account"] = alpaca.get_account_info()
            result["stocks"]["market_open"] = alpaca.is_market_open()
        except Exception:
            pass
    cb_broken, cb_reason = await tm.check_circuit_breaker()
    result["circuit_breaker"] = {"active": cb_broken, "reason": cb_reason}
    result["dry_run"] = TRADING_DRY_RUN
    return result


# ── Positions ─────────────────────────────────────────────────────────────────

@app.get("/positions")
async def positions(market: str = "all", x_api_secret: str = Header(default="")):
    _auth(x_api_secret)
    if market == "crypto":
        return await tm.get_open_positions("crypto")
    if market == "stocks":
        return await tm.get_open_positions("stocks")
    return {
        "crypto": await tm.get_open_positions("crypto"),
        "stocks": await tm.get_open_positions("stocks"),
    }


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/stats")
async def stats(x_api_secret: str = Header(default="")):
    _auth(x_api_secret)
    return await tm.get_trade_stats()


@app.get("/trades/recent")
async def recent_trades(limit: int = 3, x_api_secret: str = Header(default="")):
    _auth(x_api_secret)
    return await tm.get_recent_trades(limit)


# ── Circuit Breaker ───────────────────────────────────────────────────────────

@app.get("/circuit-breaker")
async def circuit_breaker_status(x_api_secret: str = Header(default="")):
    _auth(x_api_secret)
    broken, reason = await tm.check_circuit_breaker()
    return {"active": broken, "reason": reason}


@app.post("/circuit-breaker/reset")
async def circuit_breaker_reset(hours: int = 1, x_api_secret: str = Header(default="")):
    _auth(x_api_secret)
    tm.reset_circuit_breaker(hours=hours)
    return {"status": "reset", "override_hours": hours}


# ── Crypto Pause / Resume ─────────────────────────────────────────────────────

@app.post("/crypto/pause")
async def crypto_pause(x_api_secret: str = Header(default="")):
    _auth(x_api_secret)
    from app.engine.trade_manager import pause_market
    pause_market("crypto")
    return {"status": "paused", "market": "crypto"}


@app.post("/crypto/resume")
async def crypto_resume(x_api_secret: str = Header(default="")):
    _auth(x_api_secret)
    from app.engine.trade_manager import resume_market
    resume_market("crypto")
    return {"status": "active", "market": "crypto"}


# ── Manual Scan ───────────────────────────────────────────────────────────────

@app.post("/scan")
async def manual_scan(
    background_tasks: BackgroundTasks,
    market: str = "all",
    x_api_secret: str = Header(default=""),
):
    _auth(x_api_secret)

    async def _do_scan():
        from app.config import (
            KRAKEN_PAIRS, KRAKEN_STAKE_AMOUNT, KRAKEN_MAX_POSITIONS,
            ALPACA_SYMBOLS, ALPACA_STAKE_USD, ALPACA_MAX_POSITIONS,
        )
        from app.engine.scanner import run_scan
        actions = []
        if market in ("all", "crypto") and KRAKEN_API_KEY:
            from app.exchanges.kraken import get_kraken
            actions += await run_scan(get_kraken(), KRAKEN_PAIRS,
                                      KRAKEN_STAKE_AMOUNT, KRAKEN_MAX_POSITIONS, _notify_fn)
        if market in ("all", "stocks") and ALPACA_API_KEY:
            from app.exchanges.alpaca import get_alpaca
            actions += await run_scan(get_alpaca(), ALPACA_SYMBOLS,
                                      ALPACA_STAKE_USD, ALPACA_MAX_POSITIONS, _notify_fn)
        logger.info("Manual scan complete: %d actions", len(actions))

    background_tasks.add_task(_do_scan)
    return {"status": "scan gestartet", "market": market}


# ── Backtest (harness increment 4) ─────────────────────────────────────────────
# Technical-only backtest over the collected OHLCV history. Lets a caller (or the
# autoresearch harness) measure a parameter set BEFORE touching the live config.

class BacktestRequest(BaseModel):
    symbols:            list[str] | None = None   # default: KRAKEN_PAIRS
    timeframe:          str   = "5m"
    stop_pct:           float = 0.03
    trail_activate_pct: float = 0.03
    trail_pct:          float = 0.03
    max_hold:           int   = 48
    fee:                float = 0.0008
    warmup:             int   = 50


@app.post("/backtest")
async def backtest(req: BacktestRequest, x_api_secret: str = Header(default="")):
    _auth(x_api_secret)
    from app.config import KRAKEN_PAIRS
    from app.backtest.data_collector import load_all
    from app.backtest.runner import run_backtest, BacktestParams

    symbols = req.symbols or KRAKEN_PAIRS
    bars = load_all(symbols, req.timeframe)
    if not bars:
        raise HTTPException(
            status_code=400,
            detail="Keine Historie im Store — erst data_collector.collect() laufen lassen.",
        )
    params = BacktestParams(
        stop_pct=req.stop_pct, trail_activate_pct=req.trail_activate_pct,
        trail_pct=req.trail_pct, max_hold=req.max_hold, fee=req.fee, warmup=req.warmup,
    )
    result = await run_backtest(bars, params)
    result.pop("trades", None)          # keep the response lean for harness loops
    result["symbols"] = list(bars.keys())
    return result
