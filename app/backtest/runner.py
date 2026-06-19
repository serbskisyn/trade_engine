"""
runner.py — clock-driven backtest run loop (harness increment 3).

Builds a BacktestExchange from collected history, advances the central clock
bar by bar, and drives the REAL entry pre-filter (_technical_signal) + the
mechanical exit model (stop / trailing / max-hold, close-based) — mirroring the
already-validated scripts/backtest_exits.py, but through the BaseExchange
abstraction so the same loop can later layer in cached-LLM entries.

Technical-only (no LLM, no network). Long-only, no overlapping positions —
matches the live engine's per-symbol single-position behaviour.

Indicators are computed once per symbol on the full series; every indicator in
calc_indicators is causal (depends only on past bars), and the runner never
reads a row beyond the clock cursor, so there is no lookahead.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field

import pandas as pd

from app.strategy.indicators import calc_indicators
from app.engine.scanner import _technical_signal
from app.exchanges.backtest import BacktestExchange
from app.exchanges.base import Side


@dataclass
class BacktestParams:
    stop_pct:           float = 0.03
    trail_activate_pct: float = 0.03
    trail_pct:          float = 0.03
    max_hold:           int   = 48
    fee:                float = 0.0008   # per leg
    warmup:             int   = 50       # bars before the clock starts (indicator warmup)
    allow_long:         bool  = True
    allow_short:        bool  = False    # toggle to test "shorts on"
    # llm_mode: gate technical signals through the (cached) LLM confidence vote,
    # like live. buy_conf = entry threshold; use_debate = Bull/Bear+Judge vs single.
    llm_mode:           bool  = False
    buy_conf:           float = 0.55
    use_debate:         bool  = True


# Backtests can't reconstruct historical Fear&Greed/Polymarket/Tavily sentiment.
_NEUTRAL_SENTIMENT = "Marktstimmung: neutral (Backtest — keine historischen Sentiment-Daten)."


@dataclass
class _Pos:
    entry: float
    entry_i: int
    side: str            # "long" | "short"
    extreme: float       # favourable extreme: peak (long) / trough (short)
    trailing: bool = False


async def run_backtest(bars: dict[str, pd.DataFrame], params: BacktestParams | None = None,
                       verdict_fn=None) -> dict:
    """Run a backtest over the given per-symbol OHLCV history.

    Long and/or short per params.allow_long / allow_short. Short exits mirror
    long (stop on adverse rise, trail off the trough). With params.llm_mode, a
    technical signal is additionally gated through the LLM confidence vote
    (cached). verdict_fn(prompt)->dict can be injected (tests); default uses the
    real cached LLM."""
    p = params or BacktestParams()
    bx = BacktestExchange(bars, fee=p.fee, warmup=p.warmup)
    ind = {s: calc_indicators(df.copy()) for s, df in bx._bars.items()}
    open_pos: dict[str, _Pos] = {}
    trades: list[dict] = []

    async def _get_verdict(prompt: str) -> dict:
        if verdict_fn is not None:
            return await verdict_fn(prompt)
        from app.config import OPENROUTER_MODEL
        from app.backtest.llm_cache import cached_verdict
        from app.strategy.debate import call_llm_debate
        from app.strategy.llm import call_llm
        call = call_llm_debate if p.use_debate else call_llm
        return await cached_verdict(prompt, "crypto", OPENROUTER_MODEL, call)

    async def _entry_ok(symbol: str, df_slice, side: str) -> bool:
        if not p.llm_mode:
            return True
        from app.strategy.llm import build_prompt
        prompt = build_prompt(symbol, df_slice, _NEUTRAL_SENTIMENT, None, "crypto")
        v = await _get_verdict(prompt)
        want = "buy" if side == "long" else "sell"
        return v.get("signal") == want and float(v.get("confidence", 0.0)) >= p.buy_conf

    async def _close(symbol: str, price: float, reason: str, i: int) -> None:
        pos = open_pos.pop(symbol)
        await bx.close_position(symbol, side=pos.side)
        direction = 1 if pos.side == "long" else -1
        gross = direction * (price - pos.entry) / pos.entry
        trades.append({
            "symbol": symbol, "side": pos.side, "entry": pos.entry, "exit": price,
            "reason": reason, "held": i - pos.entry_i,
            "return_pct": round(gross - 2 * p.fee, 6),
        })

    while True:
        i = bx._i
        for symbol, idf in ind.items():
            if i >= len(idf):
                continue
            price = float(idf.iloc[i]["close"])

            if symbol in open_pos:
                pos = open_pos[symbol]
                direction = 1 if pos.side == "long" else -1
                pos.extreme = max(pos.extreme, price) if pos.side == "long" else min(pos.extreme, price)
                favourable = direction * (pos.extreme - pos.entry) / pos.entry   # ≥0 in-the-money move
                if favourable >= p.trail_activate_pct:
                    pos.trailing = True
                pnl = direction * (price - pos.entry) / pos.entry
                giveback = direction * (pos.extreme - price) / pos.extreme       # retrace from extreme
                held = i - pos.entry_i
                if pnl <= -p.stop_pct:
                    await _close(symbol, price, "stop", i)
                elif pos.trailing and pos.extreme > 0 and giveback >= p.trail_pct:
                    await _close(symbol, price, "trail", i)
                elif held >= p.max_hold:
                    await _close(symbol, price, "max_hold", i)
            else:
                long_sig, short_sig = _technical_signal(idf.iloc[: i + 1])
                side = ("long" if (p.allow_long and long_sig)
                        else "short" if (p.allow_short and short_sig) else None)
                if side and await _entry_ok(symbol, idf.iloc[: i + 1], side):
                    await bx.place_order(symbol, Side.BUY if side == "long" else Side.SELL,
                                         amount=1.0, short=(side == "short"))
                    open_pos[symbol] = _Pos(entry=price, entry_i=i, side=side, extreme=price)

        if not bx.advance():
            break

    # Force-close anything still open at the last bar.
    last_i = bx._i
    for symbol in list(open_pos):
        await _close(symbol, float(ind[symbol].iloc[last_i]["close"]), "eod", last_i)

    return _metrics(trades, p)


def _max_drawdown(equity: list[float]) -> float:
    """Largest peak-to-trough drop on the cumulative-return equity curve."""
    peak = 0.0
    mdd = 0.0
    for e in equity:
        peak = max(peak, e)
        mdd = min(mdd, e - peak)
    return round(mdd, 6)


def _metrics(trades: list[dict], p: BacktestParams) -> dict:
    n = len(trades)
    rets = [t["return_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    net = sum(rets)
    win_rate = len(wins) / n if n else 0.0

    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0   # ≤ 0
    payoff = (avg_win / abs(avg_loss)) if avg_loss else 0.0      # avg_win / avg_loss
    # Kelly fraction: W - (1-W)/payoff. 0 if no payoff (degenerate).
    kelly = (win_rate - (1 - win_rate) / payoff) if payoff else 0.0
    # Expectancy in R-multiples: per-trade return measured in units of risk (stop).
    risk = p.stop_pct or 1.0
    avg_R = (net / n) / risk if n else 0.0

    equity, cum = [], 0.0
    for r in rets:
        cum += r
        equity.append(cum)

    return {
        "n_trades": n,
        "wins": len(wins),
        "win_rate": round(win_rate, 4),
        "net_pct": round(net, 6),
        "avg_return_pct": round(net / n, 6) if n else 0.0,
        "avg_win_pct": round(avg_win, 6),
        "avg_loss_pct": round(avg_loss, 6),
        "payoff_ratio": round(payoff, 4),
        "expectancy_R": round(avg_R, 4),
        "kelly": round(kelly, 4),
        "max_drawdown_pct": _max_drawdown(equity),
        "equity_curve": [round(e, 6) for e in equity],
        "params": asdict(p),
        "trades": trades,
    }
