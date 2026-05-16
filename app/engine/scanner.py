"""
Scanner — runs one full scan cycle for a given exchange + symbol list.
Called by the scheduler every 5 minutes.
"""
import logging
from typing import Callable, Awaitable

from app.config import BUY_CONFIDENCE, SELL_CONFIDENCE, MIN_HOLD_CANDLES, KRAKEN_ALLOW_SHORTS
from app.exchanges.base import BaseExchange, Side
from app.engine import trade_manager as tm
from app.strategy.llm import build_prompt, call_llm
from app.strategy.sentiment import build_sentiment_block, should_block_entry

logger = logging.getLogger(__name__)

Notifier = Callable[[str], Awaitable[None]]


async def run_scan(
    exchange: BaseExchange,
    symbols: list[str],
    stake_amount: float,
    max_positions: int,
    notify: Notifier | None = None,
) -> list[str]:
    """
    Scans all symbols, executes trades.
    Returns list of action strings (for Telegram summary).
    """
    market  = "stocks" if exchange.name == "alpaca" else "crypto"
    mkt_str = "US" if market == "stocks" else "crypto"

    if not exchange.is_market_open():
        logger.info("[Scanner/%s] Markt geschlossen — übersprungen", exchange.name)
        return []

    open_positions = {p["symbol"]: p for p in await tm.get_open_positions(market)}
    open_count     = len(open_positions)
    actions        = []

    for symbol in symbols:
        df = await exchange.fetch_bars(symbol)
        if df is None:
            continue

        await tm.increment_candles(market, symbol)

        latest   = df.iloc[-1]
        position = open_positions.get(symbol)
        price    = float(latest["close"])
        pos_side = position.get("side", "long") if position else None

        # ── Multi-Timeframe: 1h trend filter ─────────────────────────────────
        trend_df           = await exchange.fetch_trend_bars(symbol)
        trend_clearly_down = False
        if trend_df is not None and len(trend_df) >= 6:
            ema50_val          = float(trend_df.iloc[-1]["ema50"])
            h1_slope           = ema50_val - float(trend_df.iloc[-6]["ema50"])
            slope_pct          = h1_slope / ema50_val if ema50_val > 0 else 0
            trend_clearly_down = slope_pct < -0.001   # < -0.1% über 6h = klar abwärts

        # ── Exit path ─────────────────────────────────────────────────────────
        if position:
            candles_held = int(position.get("candles_held", 0))

            # Hardware stops first (no LLM cost)
            stop_hit, stop_reason = await tm.check_stops(market, symbol, price, candles_held)
            if stop_hit:
                ok = await exchange.close_position(symbol)
                if ok:
                    result = await tm.close_position(market, symbol, price, stop_reason)
                    if result:
                        sign = "+" if result["pl_pct"] >= 0 else ""
                        msg  = (f"🛑 *{exchange.name.capitalize()} Exit*\n"
                                f"`{symbol}` {sign}{result['pl_pct']:.2f}% | {stop_reason}")
                        actions.append(msg)
                        if notify:
                            await notify(msg)
                continue

            # LLM exit signal
            if candles_held >= MIN_HOLD_CANDLES:
                sentiment_block = build_sentiment_block(symbol)
                prompt  = build_prompt(symbol, df, sentiment_block, position, mkt_str)
                result  = await call_llm(prompt)
                signal  = result.get("signal", "hold")
                conf    = float(result.get("confidence", 0.0))
                reason  = result.get("reason", "")
                logger.info("[Scanner/%s] %s (open %s) → %s conf=%.2f",
                            exchange.name, symbol, pos_side, signal, conf)

                # Short: "buy" closes; Long: "sell" closes
                exit_triggered = (
                    (pos_side == "short" and signal == "buy"  and conf >= BUY_CONFIDENCE) or
                    (pos_side != "short" and signal == "sell" and conf >= SELL_CONFIDENCE)
                )
                if exit_triggered:
                    ok = await exchange.close_position(symbol)
                    if ok:
                        close_reason = f"llm_{'buy' if pos_side == 'short' else 'sell'}: {reason}"
                        res = await tm.close_position(market, symbol, price, close_reason)
                        if res:
                            sign  = "+" if res["pl_pct"] >= 0 else ""
                            label = "Short-Exit" if pos_side == "short" else "Verkauf"
                            msg   = (f"📤 *{exchange.name.capitalize()} {label}*\n"
                                     f"`{symbol}` {sign}{res['pl_pct']:.2f}% ({sign}${res['pl_abs']:.2f})\n"
                                     f"Grund: {reason}")
                            actions.append(msg)
                            if notify:
                                await notify(msg)
            continue

        # ── Entry path ────────────────────────────────────────────────────────
        if open_count >= max_positions:
            continue

        blocked, block_reason = should_block_entry(symbol)
        if blocked:
            logger.info("[Scanner/%s] %s — Entry blockiert: %s", exchange.name, symbol, block_reason)
            continue

        # Longs: erlaubt außer bei klar negativem 1h-Slope (< -0.1%)
        # Shorts: nur bei klar negativem 1h-Slope + KRAKEN_ALLOW_SHORTS
        allow_long  = (not trend_clearly_down) or trend_df is None
        allow_short = (trend_clearly_down or trend_df is None) and market == "crypto" and KRAKEN_ALLOW_SHORTS

        if not allow_long and not allow_short:
            logger.info("[Scanner/%s] %s — Entry blockiert (1h EMA50 klar abwärts, kein Short erlaubt)",
                        exchange.name, symbol)
            continue

        sentiment_block = build_sentiment_block(symbol)
        prompt  = build_prompt(symbol, df, sentiment_block, None, mkt_str)
        result  = await call_llm(prompt)
        signal  = result.get("signal", "hold")
        conf    = float(result.get("confidence", 0.0))
        reason  = result.get("reason", "")
        logger.info("[Scanner/%s] %s → %s conf=%.2f", exchange.name, symbol, signal, conf)

        if signal == "buy" and conf >= BUY_CONFIDENCE and allow_long:
            order = await exchange.place_order(symbol, Side.BUY, stake_amount)
            if order:
                open_count += 1
                await tm.open_position(market, symbol, order.price, order.qty, side="long")
                msg = (f"🟢 *{exchange.name.capitalize()} Long*\n"
                       f"`{symbol}` @ ~{order.price:.4f}\n"
                       f"Einsatz: {stake_amount} | Conf: {conf:.2f}\nGrund: {reason}")
                actions.append(msg)
                if notify:
                    await notify(msg)

        elif signal == "sell" and conf >= SELL_CONFIDENCE and allow_short:
            order = await exchange.place_order(symbol, Side.SELL, stake_amount, short=True)
            if order:
                open_count += 1
                await tm.open_position(market, symbol, order.price, order.qty, side="short")
                msg = (f"🔴 *{exchange.name.capitalize()} Short*\n"
                       f"`{symbol}` @ ~{order.price:.4f}\n"
                       f"Einsatz: {stake_amount} | Conf: {conf:.2f}\nGrund: {reason}")
                actions.append(msg)
                if notify:
                    await notify(msg)

    return actions
