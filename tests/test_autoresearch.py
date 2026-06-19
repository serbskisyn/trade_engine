"""Tests for the autoresearch coordinate-descent search (harness inc. 6)."""
import asyncio

from app.backtest.autoresearch import search, default_objective
from app.backtest.runner import BacktestParams


def test_default_objective_floors_low_trade_counts():
    assert default_objective({"n_trades": 3, "net_pct": 9.9, "max_drawdown_pct": 0.0}) == float("-inf")
    assert default_objective(
        {"n_trades": 20, "net_pct": 0.10, "max_drawdown_pct": -0.04}
    ) == 0.10 + 0.5 * -0.04


def test_search_finds_known_optimum():
    space = {"stop_pct": [0.02, 0.03, 0.04, 0.05], "max_hold": [24, 48, 72]}
    # quadratic bowl peaking at stop_pct=0.04, max_hold=48
    async def eval_fn(p):
        score = -((p.stop_pct - 0.04) ** 2 * 1000 + (p.max_hold - 48) ** 2 / 100)
        return score, {"net_pct": score}

    res = asyncio.run(search(space, BacktestParams(stop_pct=0.02, max_hold=24), eval_fn))
    assert res["best_params"]["stop_pct"] == 0.04
    assert res["best_params"]["max_hold"] == 48


def test_search_memoizes_repeated_configs():
    space = {"stop_pct": [0.02, 0.03], "max_hold": [24, 48]}
    calls = {"n": 0}

    async def eval_fn(p):
        calls["n"] += 1
        return 1.0, {"net_pct": 1.0}  # flat → no improvement, full passes

    res = asyncio.run(search(space, BacktestParams(stop_pct=0.02, max_hold=24), eval_fn))
    # only the 2x2 = 4 distinct configs are ever evaluated, never recomputed
    assert calls["n"] == res["n_evals"] <= 4


def test_search_respects_max_evals():
    space = {"stop_pct": [0.01, 0.02, 0.03, 0.04, 0.05], "max_hold": [12, 24, 36, 48, 60]}
    calls = {"n": 0}

    async def eval_fn(p):
        calls["n"] += 1
        return float(calls["n"]), {"net_pct": 0.0}  # always "improving" → would loop

    res = asyncio.run(search(space, BacktestParams(), eval_fn, max_evals=6))
    assert res["n_evals"] <= 6
