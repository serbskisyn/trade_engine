"""
autoresearch.py — autonomous parameter search over the backtest (harness inc. 6).

Coordinate-descent over BacktestParams: starts from a baseline and, one param at
a time, tries the candidate values in the search space, adopting the best one;
loops until a full pass yields no improvement (or the eval budget is hit).
Evaluations are memoised, so revisited configs are free — and the LLM verdict
cache (inc. 5) makes llm_mode searches cheap across runs too.

READ-ONLY: this reports the best config, it NEVER writes the live trading config.
Backtests overfit; a human applies a recommendation manually (money-adjacent).

The objective deliberately penalises drawdown and floors low-trade configs, so a
single lucky trade can't win the search.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, replace
from pathlib import Path

from app.backtest.runner import BacktestParams, run_backtest

logger = logging.getLogger(__name__)

RESULT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "backtest" / "autoresearch_result.json"

# Technical-only by default — add "llm_mode"/"buy_conf" here to search the gate
# too (each new config then costs LLM calls, cached after the first run).
DEFAULT_SPACE: dict[str, list] = {
    "stop_pct":           [0.02, 0.03, 0.04, 0.05],
    "trail_activate_pct": [0.02, 0.03, 0.05],
    "trail_pct":          [0.02, 0.03, 0.05],
    "max_hold":           [24, 48, 72, 96],
}


def default_objective(metrics: dict, min_trades: int = 10, dd_weight: float = 0.5) -> float:
    """Net return penalised by max drawdown; configs with too few trades are
    floored out so the search can't reward a lucky one-off. max_drawdown_pct ≤ 0,
    so adding dd_weight*mdd subtracts the drawdown."""
    if metrics["n_trades"] < min_trades:
        return float("-inf")
    return metrics["net_pct"] + dd_weight * metrics["max_drawdown_pct"]


async def search(space: dict[str, list], baseline: BacktestParams, eval_fn,
                 max_evals: int = 200) -> dict:
    """Coordinate-descent search. eval_fn is async (BacktestParams) -> (score, metrics).
    Returns best_params/best_score/best_metrics/n_evals/history."""
    memo: dict[tuple, tuple[float, dict]] = {}

    def _key(p: BacktestParams) -> tuple:
        return tuple(getattr(p, k) for k in space)

    async def _ev(p: BacktestParams) -> tuple[float, dict]:
        k = _key(p)
        if k not in memo:
            memo[k] = await eval_fn(p)
        return memo[k]

    current = baseline
    best_score, best_metrics = await _ev(current)
    history: list[dict] = [{"params": {k: getattr(current, k) for k in space},
                            "score": best_score}]

    improved = True
    while improved and len(memo) < max_evals:
        improved = False
        for param, candidates in space.items():
            cur_val = getattr(current, param)
            best_val, best_s, best_m = cur_val, best_score, best_metrics
            for val in candidates:
                if val == cur_val or len(memo) >= max_evals:
                    continue
                trial = replace(current, **{param: val})
                score, metrics = await _ev(trial)
                history.append({"params": {k: getattr(trial, k) for k in space},
                                "score": score})
                if score > best_s:
                    best_val, best_s, best_m = val, score, metrics
            if best_val != cur_val:
                current = replace(current, **{param: best_val})
                best_score, best_metrics = best_s, best_m
                improved = True

    return {
        "best_params": asdict(current),
        "best_score": best_score,
        "best_metrics": {k: v for k, v in best_metrics.items() if k != "equity_curve"},
        "n_evals": len(memo),
        "history": history,
    }


async def run(bars: dict, space: dict[str, list] | None = None,
              baseline: BacktestParams | None = None, max_evals: int = 200) -> dict:
    """Wire run_backtest into the search and return the result dict."""
    space = space or DEFAULT_SPACE
    baseline = baseline or BacktestParams()

    async def eval_fn(p: BacktestParams):
        metrics = await run_backtest(bars, p)
        return default_objective(metrics), metrics

    return await search(space, baseline, eval_fn, max_evals=max_evals)


def _cli() -> None:
    """Overnight optimiser: load the collected history, search, write a report.
    Run via:  python -m app.backtest.autoresearch"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from app.config import KRAKEN_PAIRS
    from app.backtest.data_collector import load_all

    bars = load_all(KRAKEN_PAIRS)
    if not bars:
        logger.error("Keine Historie im Store — erst data_collector.collect() laufen lassen.")
        return

    result = asyncio.run(run(bars))
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(result, indent=2))

    bp, bm = result["best_params"], result["best_metrics"]
    logger.info("autoresearch done: %d configs evaluated", result["n_evals"])
    logger.info("best score %.4f → net %.4f, WR %.2f, R %.3f, MaxDD %.4f, n=%d",
                result["best_score"], bm.get("net_pct", 0), bm.get("win_rate", 0),
                bm.get("expectancy_R", 0), bm.get("max_drawdown_pct", 0), bm.get("n_trades", 0))
    logger.info("best params: stop=%.3f trail_act=%.3f trail=%.3f max_hold=%d",
                bp["stop_pct"], bp["trail_activate_pct"], bp["trail_pct"], bp["max_hold"])
    logger.info("→ READ-ONLY recommendation written to %s — apply to live config manually.", RESULT_PATH)


if __name__ == "__main__":
    _cli()
