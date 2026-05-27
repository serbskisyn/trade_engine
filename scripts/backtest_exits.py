"""
backtest_exits.py — Offline-Backtest des mechanischen Exit-Edges.

Adressiert den backtest_roadmap-Gap: Strategie-/Param-Änderungen offline
gegen echte Kraken-Historie validieren, statt live mit Kapital zu testen.

WAS es macht:
  • Holt 5m-OHLCV-Historie pro KRAKEN_PAIR (public ccxt, kein API-Key)
  • Berechnet Indikatoren mit DERSELBEN calc_indicators() wie die Engine
  • Long-Entry-Proxy: der Technik-Vorfilter _technical_signal() (long_candidate)
    — approximiert "wir wären eingestiegen". Der LLM-Call wird NICHT simuliert
    (nicht-deterministisch + teuer), daher testet das den MECHANISCHEN Edge:
    Vorfilter-Entry + Stop/Trailing/Max-Hold-Exit. Für VERGLEICHE zwischen
    Param-Sets (gleiche Entries, andere Exits) ist das exakt das richtige Werkzeug.
  • Exit-Checks close-basiert — spiegelt das Live-Verhalten (check_stops prüft
    bei jedem 5m-Close), inkl. Trailing-Peak = max(Close).

LIMITS (ehrlich):
  • Kraken-OHLC-API liefert max. ~720 Candles (~2.5 Tage @ 5m) pro Pair.
    Kurzes Fenster — gut für relativen Old-vs-New-Vergleich, nicht für
    statistisch robuste Absolutwerte.
  • Kein LLM-Entry-Filter (überschätzt Trade-Zahl) und kein LLM-Exit
    (testet nur mechanische Exits). MIN_PROFIT_PCT/LLM-Exit-Tuning wird
    hier NICHT abgebildet — der dominante Hebel (Trail-Weite) aber schon.

Usage:
    python -m scripts.backtest_exits
    python -m scripts.backtest_exits --limit 720 --fee 0.0016
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import ccxt
import pandas as pd

from app import config
from app.strategy.indicators import calc_indicators
from app.engine.scanner import _technical_signal

_WARMUP = 60  # candles needed before indicators are stable


def _fetch_history(ex: ccxt.kraken, symbol: str, limit: int) -> pd.DataFrame | None:
    try:
        ohlcv = ex.fetch_ohlcv(symbol, "5m", None, limit)
    except Exception as exc:
        print(f"  ⚠️  {symbol}: fetch failed: {exc}")
        return None
    if not ohlcv or len(ohlcv) < _WARMUP + 20:
        return None
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    return calc_indicators(df)


def simulate(df: pd.DataFrame, *, stop_pct: float, trail_activate_pct: float,
             trail_pct: float, max_hold: int, fee: float) -> list[dict]:
    """Walk the candle series; enter long on pre-filter signal, exit mechanically.

    Exit checks are close-based to mirror the live engine (check_stops on each
    5m close, peak = running max of closes).
    """
    trades: list[dict] = []
    n = len(df)
    i = _WARMUP
    while i < n - 1:
        sub = df.iloc[: i + 1]
        long_sig, _short = _technical_signal(sub)
        if not long_sig:
            i += 1
            continue

        entry = float(df.iloc[i]["close"])
        if entry <= 0:
            i += 1
            continue
        peak = entry
        trailing_active = False
        exit_price = None
        reason = "eod"
        held = 0

        j = i + 1
        while j < n:
            held += 1
            price = float(df.iloc[j]["close"])
            peak = max(peak, price)
            if (peak - entry) / entry >= trail_activate_pct:
                trailing_active = True

            if (entry - price) / entry >= stop_pct:
                exit_price, reason = price, "stop"
                break
            if trailing_active and peak > 0 and (peak - price) / peak >= trail_pct:
                exit_price, reason = price, "trail"
                break
            if held >= max_hold:
                exit_price, reason = price, "max_hold"
                break
            j += 1

        if exit_price is None:
            exit_price = float(df.iloc[-1]["close"])
            j = n - 1

        pl = (exit_price - entry) / entry - fee
        trades.append({"pl": pl, "reason": reason, "held": held})
        i = j + 1  # resume after exit (no overlapping positions)

    return trades


def _summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    pls = [t["pl"] for t in trades]
    wins = [p for p in pls if p > 0]
    losses = [p for p in pls if p <= 0]
    return {
        "n": len(trades),
        "win_rate": len(wins) / len(trades),
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "net": sum(pls),
        "expectancy": sum(pls) / len(trades),
        "best": max(pls),
        "worst": min(pls),
    }


def _print_summary(label: str, s: dict) -> None:
    if s.get("n", 0) == 0:
        print(f"  {label:<14} keine Trades")
        return
    print(f"  {label:<14} n={s['n']:>3}  WR={s['win_rate']*100:4.1f}%  "
          f"avgWin={s['avg_win']*100:+5.2f}%  avgLoss={s['avg_loss']*100:+5.2f}%  "
          f"Net={s['net']*100:+6.2f}%  Exp/Trade={s['expectancy']*100:+5.3f}%  "
          f"best={s['best']*100:+5.2f}%")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=720, help="5m-Candles pro Pair (Kraken-Max ~720)")
    parser.add_argument("--fee", type=float, default=0.0016, help="Round-trip-Fee (Maker 0.08%%×2)")
    args = parser.parse_args()

    pairs = config.KRAKEN_PAIRS
    stop = getattr(config, "STOP_LOSS_PCT_CRYPTO", 0.03)
    activate = getattr(config, "TRAILING_ACTIVATE_PCT_CRYPTO", 0.03)
    max_hold = config.MAX_HOLD_CANDLES

    # Param-Sets: ALT (vor 27.05.) vs NEU (Gold-Tuning)
    PARAM_SETS = {
        "ALT trail1.5%": dict(stop_pct=stop, trail_activate_pct=activate, trail_pct=0.015, max_hold=max_hold),
        "NEU trail3.0%": dict(stop_pct=stop, trail_activate_pct=activate, trail_pct=0.030, max_hold=max_hold),
    }

    print(f"Backtest — {len(pairs)} Pairs, {args.limit} Candles (5m), Fee {args.fee*100:.2f}% RT")
    print(f"Stop={stop*100:.1f}%  Trail-Activate={activate*100:.1f}%  Max-Hold={max_hold}\n")

    ex = ccxt.kraken({"enableRateLimit": True})
    agg: dict[str, list[dict]] = {k: [] for k in PARAM_SETS}

    for symbol in pairs:
        df = _fetch_history(ex, symbol, args.limit)
        if df is None:
            print(f"{symbol:<10} — übersprungen (zu wenig Daten)")
            continue
        line = f"{symbol:<10}"
        for set_name, params in PARAM_SETS.items():
            trades = simulate(df, fee=args.fee, **params)
            agg[set_name].extend(trades)
            s = _summarize(trades)
            line += f"  [{set_name}: n={s.get('n',0)} net={s.get('net',0)*100:+.2f}%]"
        print(line)

    print("\n" + "=" * 70)
    print("AGGREGAT über alle Pairs:")
    for set_name in PARAM_SETS:
        _print_summary(set_name, _summarize(agg[set_name]))
    print("=" * 70)
    print("\nHinweis: Entry-Proxy = Technik-Vorfilter (kein LLM). Vergleich ALT vs NEU\n"
          "ist aussagekräftig (gleiche Entries), Absolutwerte nur indikativ (kurzes Fenster).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
