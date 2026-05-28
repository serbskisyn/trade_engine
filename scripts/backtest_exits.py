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
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0  # negative
    wr = len(wins) / len(trades)
    # Reward:Risk-Verhältnis — |avgWin| / |avgLoss|
    r = (avg_win / abs(avg_loss)) if avg_loss != 0 else float("inf")
    # Kelly f* = WR − (1 − WR) / R  (negativ = nicht setzen)
    kelly = wr - (1 - wr) / r if r > 0 else -1.0
    return {
        "n": len(trades),
        "win_rate": wr,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "r": r,
        "kelly": kelly,
        "half_kelly": kelly / 2 if kelly > 0 else 0.0,
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


_DEFAULT_SWEEP = (0.010, 0.015, 0.020, 0.025, 0.030, 0.040, 0.050, 0.060)


def _print_sweep_table(rows: list[tuple[str, dict]]) -> None:
    """Tabular sweep output with R + Kelly columns."""
    header = (f"  {'Param':<14} {'n':>3}  {'WR':>5}  {'avgW':>6}  {'avgL':>7}  "
              f"{'R':>5}  {'Exp':>7}  {'Net':>7}  {'Kelly f*':>9}  {'½-Kelly':>8}")
    print(header)
    print("  " + "─" * (len(header) - 2))
    for label, s in rows:
        if s.get("n", 0) == 0:
            print(f"  {label:<14} keine Trades")
            continue
        print(f"  {label:<14} {s['n']:>3}  {s['win_rate']*100:4.1f}%  "
              f"{s['avg_win']*100:+5.2f}%  {s['avg_loss']*100:+6.2f}%  "
              f"{s['r']:>5.2f}  {s['expectancy']*100:+6.3f}%  {s['net']*100:+6.2f}%  "
              f"{s['kelly']*100:+8.1f}%  {s['half_kelly']*100:+7.1f}%")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=720, help="5m-Candles pro Pair (Kraken-Max ~720)")
    parser.add_argument("--fee", type=float, default=0.0016, help="Round-trip-Fee (Maker 0.08%%×2)")
    parser.add_argument("--simple", action="store_true",
                        help="ALT-vs-NEU-Vergleich (2 Sets) statt Sweep")
    parser.add_argument(
        "--trail-grid", type=str, default=",".join(str(x) for x in _DEFAULT_SWEEP),
        help="Komma-separierte trail_pct-Werte für den Sweep (default: 1.0-6.0%%)",
    )
    args = parser.parse_args()

    pairs = config.KRAKEN_PAIRS
    stop = getattr(config, "STOP_LOSS_PCT_CRYPTO", 0.03)
    activate = getattr(config, "TRAILING_ACTIVATE_PCT_CRYPTO", 0.03)
    max_hold = config.MAX_HOLD_CANDLES

    if args.simple:
        param_sets = {
            "ALT trail1.5%": dict(stop_pct=stop, trail_activate_pct=activate, trail_pct=0.015, max_hold=max_hold),
            "NEU trail3.0%": dict(stop_pct=stop, trail_activate_pct=activate, trail_pct=0.030, max_hold=max_hold),
        }
    else:
        trail_values = [float(x.strip()) for x in args.trail_grid.split(",") if x.strip()]
        param_sets = {
            f"trail {t*100:>4.1f}%": dict(stop_pct=stop, trail_activate_pct=activate,
                                          trail_pct=t, max_hold=max_hold)
            for t in trail_values
        }

    mode = "SIMPLE" if args.simple else f"SWEEP ({len(param_sets)} trail-Werte)"
    print(f"Backtest {mode} — {len(pairs)} Pairs, {args.limit} Candles (5m), Fee {args.fee*100:.2f}% RT")
    print(f"Stop={stop*100:.1f}%  Trail-Activate={activate*100:.1f}%  Max-Hold={max_hold}\n")

    ex = ccxt.kraken({"enableRateLimit": True})
    agg: dict[str, list[dict]] = {k: [] for k in param_sets}

    skipped = 0
    for symbol in pairs:
        df = _fetch_history(ex, symbol, args.limit)
        if df is None:
            print(f"{symbol:<10} — übersprungen (zu wenig Daten)")
            skipped += 1
            continue
        for set_name, params in param_sets.items():
            agg[set_name].extend(simulate(df, fee=args.fee, **params))

    print(f"\n{'=' * 90}")
    print(f"AGGREGAT über {len(pairs) - skipped} Pairs:")
    print(f"{'=' * 90}")
    summaries = [(name, _summarize(agg[name])) for name in param_sets]
    _print_sweep_table(summaries)
    print("=" * 90)

    if not args.simple:
        # Optima identifizieren
        viable = [(n, s) for n, s in summaries if s.get("n", 0) > 0]
        if viable:
            best_exp = max(viable, key=lambda x: x[1]["expectancy"])
            best_kelly = max(viable, key=lambda x: x[1]["kelly"])
            print(f"\n🥇 Beste Expectancy: {best_exp[0]}  →  "
                  f"Exp {best_exp[1]['expectancy']*100:+.3f}%/Trade  "
                  f"(R={best_exp[1]['r']:.2f}, WR={best_exp[1]['win_rate']*100:.1f}%)")
            print(f"🥇 Bestes Kelly f*:  {best_kelly[0]}  →  "
                  f"f* {best_kelly[1]['kelly']*100:+.1f}%  "
                  f"(½-Kelly ≈ {best_kelly[1]['half_kelly']*100:+.1f}% Stake)")

    print("\nHinweis: Entry-Proxy = Vorfilter (kein LLM). Bei R≤1 zeigt Kelly negativ →\n"
          "nicht setzen. Kelly nur indikativ — in der Praxis ½-Kelly oder weniger.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
