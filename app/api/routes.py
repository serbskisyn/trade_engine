"""
FastAPI REST-API — consumed by Serbo_bot for /tradebot and /stocks commands.
All endpoints require X-API-Secret header matching API_SECRET from config.
"""
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from fastapi.responses import JSONResponse

from app.config import API_SECRET, ALPACA_API_KEY, KRAKEN_API_KEY
from app.engine import trade_manager as tm

logger = logging.getLogger(__name__)
app    = FastAPI(title="Trade Engine API", version="1.0.0")

# shared notify_fn injected by main.py
_notify_fn = None


def set_notify(fn):
    global _notify_fn
    _notify_fn = fn


def _auth(secret: str):
    if API_SECRET and secret != API_SECRET:
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
    stats      = await tm.get_trade_stats()
    result = {
        "crypto": {
            "enabled":   bool(KRAKEN_API_KEY),
            "positions": crypto_pos,
        },
        "stocks": {
            "enabled":   bool(ALPACA_API_KEY),
            "positions": stocks_pos,
        },
        "stats": stats,
    }
    if KRAKEN_API_KEY:
        try:
            from app.exchanges.kraken import KrakenExchange
            result["crypto"]["account"] = KrakenExchange().get_account_info()
        except Exception:
            pass
    if ALPACA_API_KEY:
        try:
            from app.exchanges.alpaca import AlpacaExchange
            acc = AlpacaExchange().get_account_info()
            result["stocks"]["account"] = acc
            result["stocks"]["market_open"] = AlpacaExchange().is_market_open()
        except Exception:
            pass
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
            from app.exchanges.kraken import KrakenExchange
            actions += await run_scan(KrakenExchange(), KRAKEN_PAIRS,
                                      KRAKEN_STAKE_AMOUNT, KRAKEN_MAX_POSITIONS, _notify_fn)
        if market in ("all", "stocks") and ALPACA_API_KEY:
            from app.exchanges.alpaca import AlpacaExchange
            actions += await run_scan(AlpacaExchange(), ALPACA_SYMBOLS,
                                      ALPACA_STAKE_USD, ALPACA_MAX_POSITIONS, _notify_fn)
        logger.info("Manual scan complete: %d actions", len(actions))

    background_tasks.add_task(_do_scan)
    return {"status": "scan gestartet", "market": market}
