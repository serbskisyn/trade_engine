"""
Scanner — parallel scan cycle per exchange + symbol list.
Phase 1: fetch bars + trend bars for ALL symbols concurrently
Phase 2: LLM calls only for symbols that pass technical pre-filter (concurrent)
Phase 3: trade execution (sequential to prevent double-orders)
"""
import asyncio
import logging
import time
from typing import Callable, Awaitable

import pandas as pd

from app.config import (BUY_CONFIDENCE, SELL_CONFIDENCE, EXIT_CONFIDENCE,
                        MIN_HOLD_CANDLES, MAX_HOLD_CANDLES, KRAKEN_ALLOW_SHORTS, BB_VOL_SCALING)
from app.exchanges.base import BaseExchange, Side
from app.engine import trade_manager as tm
from app.strategy.llm import build_prompt, call_llm
from app.strategy.sentiment import build_sentiment_block, should_block_entry

logger = logging.getLogger(__name__)

Notifier = Callable[[str], Awaitable[None]]

_scan_locks: dict[str, asyncio.Lock] = {}


def _fmt_price(market: str, price: float) -> str:
    return f"`${price:.2f}`" if market == "stocks" else f"`{price:.8f} BTC`"


def _fmt_pl(market: str, sign: str, pl_abs: float) -> str:
    return f"{sign}${pl_abs:.2f}" if market == "stocks" else f"{sign}{pl_abs:.6f} BTC"


def _technical_signal(df: pd.DataFrame) -> tuple[bool, bool]:
    """
    Fast pre-filter before LLM. Returns (long_candidate, short_candidate).
    Three independent signal paths — any one is sufficient to reach the LLM:
      Path 1 — Momentum:       2-of-3 voting (RSI, Stoch-K, MACD-crossover) + volume
      Path 2 — Trend:          EMA20/50 crossover
      Path 3 — Mean reversion: price touches Bollinger Band
    """
    if len(df) < 3:
        return False, False
    lat  = df.iloc[-1]
    prev = df.iloc[-2]

    # ── Path 1: Momentum (2-of-3 + volume) ───────────────────────────────────
    rsi     = float(lat.get("rsi", 50))
    stoch_k = float(lat["stoch_k"]) if pd.notna(lat.get("stoch_k")) else 50.0
    hist    = float(lat.get("macd_hist", 0))
    p_hist  = float(prev.get("macd_hist", 0))
    vol      = float(lat.get("volume", 1))
    vol_sma6 = float(lat.get("vol_sma6", vol)) or vol
    vol_ok   = vol >= vol_sma6 * 0.8
    long_sigs  = [rsi < 45, stoch_k < 25, p_hist < 0 < hist]
    short_sigs = [rsi > 58, stoch_k > 75, p_hist > 0 > hist]
    momentum_long  = vol_ok and sum(long_sigs) >= 2
    momentum_short = vol_ok and sum(short_sigs) >= 2

    # ── Path 2: EMA crossover (trend-following) ───────────────────────────────
    ema20, ema50   = float(lat.get("ema20", 0)),  float(lat.get("ema50", 0))
    p_ema20, p_ema50 = float(prev.get("ema20", 0)), float(prev.get("ema50", 0))
    ema_cross_up   = p_ema20 < p_ema50 and ema20 >= ema50 and ema20 > 0
    ema_cross_down = p_ema20 > p_ema50 and ema20 <= ema50 and ema20 > 0

    # ── Path 3: Bollinger Band touch (mean reversion) ─────────────────────────
    close    = float(lat.get("close", 0))
    bb_upper = float(lat.get("bb_upper", 0))
    bb_lower = float(lat.get("bb_lower", 0))
    bb_touch_low  = bb_lower > 0 and close <= bb_lower
    bb_touch_high = bb_upper > 0 and close >= bb_upper

    long_ok  = momentum_long  or ema_cross_up   or bb_touch_low
    short_ok = momentum_short or ema_cross_down  or bb_touch_high
    return long_ok, short_ok


async def _fetch_and_analyse(
    symbol: str,
    exchange: BaseExchange,
    market: str,
    mkt_str: str,
    open_positions: dict,
) -> dict:
    """
    Fetch all data for one symbol and run LLM if technically warranted.
    Returns a result dict consumed by the execution phase.
    """
    try:
        df, trend_df = await asyncio.gather(
            exchange.fetch_bars(symbol),
            exchange.fetch_trend_bars(symbol),
        )
        if df is None:
            return {"symbol": symbol, "skip": True}

        price    = float(df.iloc[-1]["close"])
        position = open_positions.get(symbol)
        pos_side = position.get("side", "long") if position else None

        # 1h trend direction
        trend_clearly_down = False
        if trend_df is not None and len(trend_df) >= 6:
            ema50     = float(trend_df.iloc[-1]["ema50"])
            slope_pct = (ema50 - float(trend_df.iloc[-6]["ema50"])) / ema50 if ema50 else 0
            trend_clearly_down = slope_pct < -0.001

        allow_long  = (not trend_clearly_down) or trend_df is None
        allow_short = (trend_clearly_down or trend_df is None) and market == "crypto" and KRAKEN_ALLOW_SHORTS

        # ── Open position: check hardware stops first ─────────────────────────
        if position:
            candles_held = int(position.get("candles_held", 0))
            stop_hit, stop_reason = await tm.check_stops(market, symbol, price, candles_held)
            if stop_hit:
                return {"symbol": symbol, "action": "stop", "price": price,
                        "stop_reason": stop_reason, "position": position, "pos_side": pos_side}

            # LLM exit check after MIN_HOLD_CANDLES
            if candles_held < MIN_HOLD_CANDLES:
                return {"symbol": symbol, "skip": True}

            # Force-close after MAX_HOLD_CANDLES (capital unlock)
            if candles_held >= MAX_HOLD_CANDLES:
                return {"symbol": symbol, "action": "stop", "price": price,
                        "stop_reason": f"max_hold ({candles_held} candles)",
                        "position": position, "pos_side": pos_side}

        else:
            # Entry: trend gate
            if not allow_long and not allow_short:
                return {"symbol": symbol, "skip": True, "reason": "trend_blocked"}

            # Entry: technical pre-filter
            long_sig, short_sig = _technical_signal(df)
            if not ((allow_long and long_sig) or (allow_short and short_sig)):
                logger.debug("[Scanner] %s — kein techn. Signal, LLM skip", symbol)
                return {"symbol": symbol, "skip": True, "reason": "no_signal"}

            blocked, block_reason = should_block_entry(symbol)
            if blocked:
                return {"symbol": symbol, "skip": True, "reason": block_reason}

        # ── LLM call ──────────────────────────────────────────────────────────
        loop            = asyncio.get_event_loop()
        sentiment_block = await loop.run_in_executor(None, build_sentiment_block, symbol)
        prompt     = build_prompt(symbol, df, sentiment_block, position, mkt_str)
        llm_result = await call_llm(prompt)

        bb_upper = float(df.iloc[-1].get("bb_upper", 0))
        bb_lower = float(df.iloc[-1].get("bb_lower", 0))
        bb_mid   = float(df.iloc[-1].get("bb_mid", 0))
        bb_width = (bb_upper - bb_lower) / bb_mid if bb_mid > 0 else 0.0

        return {
            "symbol":      symbol,
            "action":      "llm",
            "price":       price,
            "position":    position,
            "pos_side":    pos_side,
            "signal":      llm_result.get("signal", "hold"),
            "conf":        float(llm_result.get("confidence", 0.0)),
            "reason":      llm_result.get("reason", ""),
            "allow_long":  allow_long,
            "allow_short": allow_short,
            "candles_held": int(position.get("candles_held", 0)) if position else 0,
            "bb_width":    bb_width,
        }

    except Exception as e:
        logger.warning("[Scanner] %s Fehler: %s", symbol, e)
        return {"symbol": symbol, "skip": True}


async def run_scan(
    exchange: BaseExchange,
    symbols: list[str],
    stake_amount: float,
    max_positions: int,
    notify: Notifier | None = None,
) -> list[str]:
    market  = "stocks" if exchange.name == "alpaca" else "crypto"
    mkt_str = "US" if market == "stocks" else "crypto"

    if market not in _scan_locks:
        _scan_locks[market] = asyncio.Lock()
    if _scan_locks[market].locked():
        logger.info("[Scanner/%s] Scan läuft bereits — übersprungen", exchange.name)
        return []

    async with _scan_locks[market]:
        return await _execute_scan(exchange, symbols, stake_amount, max_positions,
                                   notify, market, mkt_str)


async def _execute_scan(
    exchange: BaseExchange,
    symbols: list[str],
    stake_amount: float,
    max_positions: int,
    notify: Notifier | None,
    market: str,
    mkt_str: str,
) -> list[str]:
    if not exchange.is_market_open():
        logger.info("[Scanner/%s] Markt geschlossen", exchange.name)
        return []

    t0             = time.monotonic()
    open_positions = {p["symbol"]: p for p in await tm.get_open_positions(market)}
    open_count     = len(open_positions)
    actions        = []

    cb_broken, cb_reason = await tm.check_circuit_breaker()
    if cb_broken:
        logger.warning("[Scanner/%s] ⚡ Circuit Breaker aktiv: %s", exchange.name, cb_reason)
        if notify:
            await notify(f"⚡ *Circuit Breaker aktiv* ({exchange.name})\n`{cb_reason}`\nNeue Entries gesperrt.")

    # Candle counters für offene Positionen parallel hochzählen
    if open_positions:
        await asyncio.gather(*[
            tm.increment_candles(market, sym) for sym in open_positions
        ])

    # ── Phase 1+2: Alle Symbole parallel fetchen + LLM ───────────────────────
    results = await asyncio.gather(*[
        _fetch_and_analyse(sym, exchange, market, mkt_str, open_positions)
        for sym in symbols
    ])

    llm_count = sum(1 for r in results if r.get("action") in ("llm", "stop"))
    logger.info("[Scanner/%s] %d/%d Symbole mit LLM/Stop analysiert",
                exchange.name, llm_count, len(symbols))

    # ── Phase 3: Trades ausführen (sequentiell) ───────────────────────────────
    for res in results:
        if res.get("skip"):
            continue

        symbol   = res["symbol"]
        price    = res.get("price", 0.0)
        position = res.get("position")
        pos_side = res.get("pos_side")

        # Stop-Loss / Trailing ausführen
        if res.get("action") == "stop":
            ok = await exchange.close_position(symbol, side=pos_side or "long",
                                               qty=float(position.get("qty", 0)) if position else None)
            if ok:
                result = await tm.close_position(market, symbol, price, res["stop_reason"])
                if result:
                    sign   = "+" if result["pl_pct"] >= 0 else ""
                    pl_str = _fmt_pl(market, sign, result["pl_abs"])
                    msg    = (f"🛑 *{exchange.name.capitalize()} Stop*\n"
                              f"`{symbol}` {sign}{result['pl_pct']:.2f}% ({pl_str})\n"
                              f"Grund: {res['stop_reason']}")
                    actions.append(msg)
                    if notify:
                        await notify(msg)
            continue

        signal = res.get("signal", "hold")
        conf   = res.get("conf", 0.0)
        reason = res.get("reason", "")

        logger.info("[Scanner/%s] %s%s → %s conf=%.2f",
                    exchange.name, symbol,
                    f" ({pos_side})" if position else "",
                    signal, conf)

        # Exit offener Position — EXIT_CONFIDENCE verhindert frühzeitigen Exit
        if position:
            exit_triggered = (
                (pos_side == "short" and signal == "buy"  and conf >= EXIT_CONFIDENCE) or
                (pos_side != "short" and signal == "sell" and conf >= EXIT_CONFIDENCE)
            )
            if exit_triggered:
                ok = await exchange.close_position(symbol, side=pos_side,
                                                   qty=float(position.get("qty", 0)))
                if ok:
                    tag    = "buy" if pos_side == "short" else "sell"
                    result = await tm.close_position(market, symbol, price, f"llm_{tag}: {reason}")
                    if result:
                        sign   = "+" if result["pl_pct"] >= 0 else ""
                        label  = "Short-Exit" if pos_side == "short" else "Verkauf"
                        pl_str = _fmt_pl(market, sign, result["pl_abs"])
                        msg    = (f"📤 *{exchange.name.capitalize()} {label}*\n"
                                  f"`{symbol}` {sign}{result['pl_pct']:.2f}% ({pl_str})\n"
                                  f"Grund: {reason}")
                        actions.append(msg)
                        if notify:
                            await notify(msg)

        # Neue Position eröffnen
        elif open_count < max_positions and not cb_broken:
            allow_long  = res.get("allow_long", True)
            allow_short = res.get("allow_short", False)

            # Volatility-adjusted stake: high BB width → smaller position
            bb_width       = res.get("bb_width", 0.0)
            vol_factor     = 1.0 / (1.0 + bb_width * BB_VOL_SCALING)
            adjusted_stake = stake_amount * vol_factor

            if signal == "buy" and conf >= BUY_CONFIDENCE and allow_long:
                order = await exchange.place_order(symbol, Side.BUY, adjusted_stake)
                if order:
                    open_count += 1
                    await tm.open_position(market, symbol, order.price, order.qty, side="long")
                    vol_info  = f" | Vol-Faktor: {vol_factor:.2f}" if vol_factor < 0.95 else ""
                    price_str = _fmt_price(market, order.price)
                    msg = (f"🟢 *{exchange.name.capitalize()} Long*\n"
                           f"`{symbol}` @ {price_str}\n"
                           f"Conf: {conf:.2f} | {reason}{vol_info}")
                    actions.append(msg)
                    if notify:
                        await notify(msg)

            elif signal == "sell" and conf >= SELL_CONFIDENCE and allow_short:
                order = await exchange.place_order(symbol, Side.SELL, adjusted_stake, short=True)
                if order:
                    open_count += 1
                    await tm.open_position(market, symbol, order.price, order.qty, side="short")
                    vol_info  = f" | Vol-Faktor: {vol_factor:.2f}" if vol_factor < 0.95 else ""
                    price_str = _fmt_price(market, order.price)
                    msg = (f"🔴 *{exchange.name.capitalize()} Short*\n"
                           f"`{symbol}` @ {price_str}\n"
                           f"Conf: {conf:.2f} | {reason}{vol_info}")
                    actions.append(msg)
                    if notify:
                        await notify(msg)

    elapsed = time.monotonic() - t0
    logger.info("[Scanner/%s] ✓ Scan in %.1fs abgeschlossen — %d Aktionen",
                exchange.name, elapsed, len(actions))
    return actions
