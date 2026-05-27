"""
Scanner — parallel scan cycle per exchange + symbol list.
Phase 1: fetch bars + trend bars for ALL symbols concurrently
Phase 2: LLM calls only for symbols that pass technical pre-filter (concurrent)
Phase 3: trade execution (sequential to prevent double-orders)
"""
import asyncio
import logging
import time
from datetime import datetime, time as dtime
from typing import Callable, Awaitable
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import (BUY_CONFIDENCE_CRYPTO, BUY_CONFIDENCE_STOCKS,
                        SELL_CONFIDENCE_CRYPTO, SELL_CONFIDENCE_STOCKS,
                        EXIT_CONFIDENCE_CRYPTO, EXIT_CONFIDENCE_STOCKS,
                        REENTRY_COOLDOWN_MIN_CRYPTO, REENTRY_COOLDOWN_MIN_STOCKS,
                        STOCKS_ENTRY_CUTOFF_HOUR, STOCKS_ENTRY_CUTOFF_MINUTE,
                        STOCKS_DAILY_TREND_GATE,
                        MIN_HOLD_CANDLES, MAX_HOLD_CANDLES, KRAKEN_ALLOW_SHORTS, BB_VOL_SCALING,
                        KRAKEN_FEE_MAKER, MIN_PROFIT_PCT, ENTRY_DEBATE_ENABLED)
from app.exchanges.base import BaseExchange, Side
from app.engine import trade_manager as tm
from app.strategy.llm import build_prompt, call_llm
from app.strategy.debate import call_llm_debate
from app.strategy.sentiment import build_sentiment_block, should_block_entry

logger = logging.getLogger(__name__)

Notifier = Callable[[str], Awaitable[None]]

_ET = ZoneInfo("America/New_York")

_scan_locks: dict[str, asyncio.Lock] = {}
_cb_last_notified: dict[str, float] = {}  # exchange_name → monotonic timestamp
_CB_NOTIFY_COOLDOWN = 3600  # max. 1× pro Stunde


def _stocks_entry_allowed_now() -> bool:
    """False nach STOCKS_ENTRY_CUTOFF (default 14:45 ET) — schützt vor Late-Day-Reversal-Fails."""
    now_et = datetime.now(_ET).time()
    cutoff = dtime(STOCKS_ENTRY_CUTOFF_HOUR, STOCKS_ENTRY_CUTOFF_MINUTE)
    return now_et < cutoff


def _fmt_price(market: str, price: float) -> str:
    return f"`${price:.2f}`" if market == "stocks" else f"`{price:.8f} BTC`"


def _fmt_pl(market: str, sign: str, pl_abs: float) -> str:
    return f"{sign}${pl_abs:.2f}" if market == "stocks" else f"{sign}{pl_abs:.6f} BTC"


def _fmt_pl_with_fee(market: str, result: dict) -> str:
    """Brutto-P&L + Netto-P&L nach Gebühren in Klammern (nur Crypto)."""
    sign   = "+" if result["pl_abs"] >= 0 else ""
    gross  = _fmt_pl(market, sign, result["pl_abs"])
    if market != "crypto":
        return gross
    # Round-Trip-Gebühr: beide Legs × Maker-Fee
    fee    = (result["entry_price"] + result["exit_price"]) * result["qty"] * KRAKEN_FEE_MAKER
    net    = result["pl_abs"] - fee
    nsign  = "+" if net >= 0 else ""
    return f"{gross} (nach Geb. {nsign}{net:.6f} BTC)"


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
        df, trend_df, daily_df = await asyncio.gather(
            exchange.fetch_bars(symbol),
            exchange.fetch_trend_bars(symbol),
            exchange.fetch_daily_bars(symbol),
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

        # 1D macro trend gate (nur Stocks, opt-in) — blockt Long-Entries an Down-Days
        daily_trend_down = False
        if market == "stocks" and STOCKS_DAILY_TREND_GATE and daily_df is not None and len(daily_df) >= 50:
            daily_close = float(daily_df.iloc[-1]["close"])
            daily_ema50 = float(daily_df.iloc[-1]["ema50"])
            daily_trend_down = daily_close < daily_ema50

        allow_long  = ((not trend_clearly_down) or trend_df is None) and not daily_trend_down
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
            # Entry: stocks time-window gate (kein Entry in der letzten Stunde vor Marktschluss)
            if market == "stocks" and not _stocks_entry_allowed_now():
                return {"symbol": symbol, "skip": True, "reason": "stocks_entry_cutoff"}

            # Entry: Re-Entry-Cooldown nach Stop-Loss
            cooldown_min = REENTRY_COOLDOWN_MIN_STOCKS if market == "stocks" else REENTRY_COOLDOWN_MIN_CRYPTO
            if await tm.is_in_reentry_cooldown(market, symbol, cooldown_min):
                return {"symbol": symbol, "skip": True, "reason": "reentry_cooldown"}

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
        # Entries (keine offene Position) gehen durch die Bull/Bear+Judge-Debatte,
        # Exits bleiben beim schnellen Einzel-Call.
        if position is None and ENTRY_DEBATE_ENABLED:
            llm_result = await call_llm_debate(prompt, market=market)
        else:
            llm_result = await call_llm(prompt, market=market)

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

    # Markt-spezifische LLM-Confidence-Schwellen
    if market == "stocks":
        buy_conf  = BUY_CONFIDENCE_STOCKS
        sell_conf = SELL_CONFIDENCE_STOCKS
        exit_conf = EXIT_CONFIDENCE_STOCKS
    else:
        buy_conf  = BUY_CONFIDENCE_CRYPTO
        sell_conf = SELL_CONFIDENCE_CRYPTO
        exit_conf = EXIT_CONFIDENCE_CRYPTO

    cb_broken, cb_reason = await tm.check_circuit_breaker()
    if cb_broken:
        logger.warning("[Scanner/%s] ⚡ Circuit Breaker aktiv: %s", exchange.name, cb_reason)
        last = _cb_last_notified.get(exchange.name, 0)
        if notify and (time.monotonic() - last) > _CB_NOTIFY_COOLDOWN:
            _cb_last_notified[exchange.name] = time.monotonic()
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
                    pl_str = _fmt_pl_with_fee(market, result)
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

        # Exit offener Position — EXIT_CONFIDENCE + Profit-Gate gegen Gebührenerosion
        if position:
            entry_p = float(position.get("entry_price", price))
            if pos_side == "short":
                current_pl_pct = (entry_p - price) / entry_p
            else:
                current_pl_pct = (price - entry_p) / entry_p

            # LLM-Exit nur wenn Position ausreichend im Plus (Gebühren gedeckt + Gewinn)
            profit_ok = current_pl_pct >= MIN_PROFIT_PCT
            if not profit_ok:
                logger.debug("[Scanner/%s] %s Profit-Gate: %.3f%% < %.3f%% — kein LLM-Exit",
                             exchange.name, symbol, current_pl_pct * 100, MIN_PROFIT_PCT * 100)

            exit_triggered = profit_ok and (
                (pos_side == "short" and signal == "buy"  and conf >= exit_conf) or
                (pos_side != "short" and signal == "sell" and conf >= exit_conf)
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
                        pl_str = _fmt_pl_with_fee(market, result)
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

            if signal == "buy" and conf >= buy_conf and allow_long:
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

            elif signal == "sell" and conf >= sell_conf and allow_short:
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
