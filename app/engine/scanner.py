"""
Scanner — runs one full scan cycle for a given exchange + symbol list.
Called by the scheduler every 15 minutes.
"""
import logging
from typing import Callable, Awaitable

from app.config import BUY_CONFIDENCE, SELL_CONFIDENCE, MIN_HOLD_CANDLES
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

        latest    = df.iloc[-1]
        ema_slope = latest["ema50"] - df.iloc[-6]["ema50"]
        position  = open_positions.get(symbol)
        price     = float(latest["close"])

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

            # LLM sell signal
            if candles_held >= MIN_HOLD_CANDLES:
                sentiment_block = build_sentiment_block(symbol)
                prompt  = build_prompt(symbol, df, sentiment_block, position, mkt_str)
                result  = await call_llm(prompt)
                signal  = result.get("signal", "hold")
                conf    = float(result.get("confidence", 0.0))
                reason  = result.get("reason", "")
                logger.info("[Scanner/%s] %s (open) → %s conf=%.2f", exchange.name, symbol, signal, conf)

                if signal == "sell" and conf >= SELL_CONFIDENCE:
                    ok = await exchange.close_position(symbol)
                    if ok:
                        res = await tm.close_position(market, symbol, price, f"llm_sell: {reason}")
                        if res:
                            sign = "+" if res["pl_pct"] >= 0 else ""
                            pl   = float(position.get("unrealized_pl", res["pl_abs"]))
                            msg  = (f"📤 *{exchange.name.capitalize()} Verkauf*\n"
                                    f"`{symbol}` {sign}{res['pl_pct']:.2f}% ({sign}${pl:.2f})\n"
                                    f"Grund: {reason}")
                            actions.append(msg)
                            if notify:
                                await notify(msg)
            continue

        # ── Entry path ────────────────────────────────────────────────────────
        if open_count >= max_positions:
            continue
        if ema_slope < 0:
            logger.info("[Scanner/%s] %s — Entry blockiert (EMA50 abwärts)", exchange.name, symbol)
            continue
        blocked, block_reason = should_block_entry(symbol)
        if blocked:
            logger.info("[Scanner/%s] %s — Entry blockiert: %s", exchange.name, symbol, block_reason)
            continue

        sentiment_block = build_sentiment_block(symbol)
        prompt  = build_prompt(symbol, df, sentiment_block, None, mkt_str)
        result  = await call_llm(prompt)
        signal  = result.get("signal", "hold")
        conf    = float(result.get("confidence", 0.0))
        reason  = result.get("reason", "")
        logger.info("[Scanner/%s] %s → %s conf=%.2f", exchange.name, symbol, signal, conf)

        if signal == "buy" and conf >= BUY_CONFIDENCE:
            order = await exchange.place_order(symbol, Side.BUY, stake_amount)
            if order:
                open_count += 1
                await tm.open_position(market, symbol, order.price, order.qty)
                msg = (f"🟢 *{exchange.name.capitalize()} Kauf*\n"
                       f"`{symbol}` @ ~{order.price:.4f}\n"
                       f"Einsatz: {stake_amount} | Conf: {conf:.2f}\nGrund: {reason}")
                actions.append(msg)
                if notify:
                    await notify(msg)

    return actions
