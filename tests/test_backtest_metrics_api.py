"""Tests for backtest metrics (R/Kelly/MaxDD) + the POST /backtest endpoint."""
import pytest
from fastapi.testclient import TestClient

from app.backtest.runner import _metrics, BacktestParams
from app.api import routes
from app.config import API_SECRET


def test_metrics_expectancy_kelly_drawdown():
    trades = [{"return_pct": 0.06}, {"return_pct": -0.03},
              {"return_pct": 0.06}, {"return_pct": -0.03}]
    m = _metrics(trades, BacktestParams(stop_pct=0.03))
    assert m["win_rate"] == 0.5
    assert m["payoff_ratio"] == pytest.approx(2.0)      # avg_win 0.06 / |avg_loss| 0.03
    assert m["kelly"] == pytest.approx(0.25)            # 0.5 - 0.5/2
    assert m["expectancy_R"] == pytest.approx(0.5)      # (net/n)/stop = 0.015/0.03
    assert m["max_drawdown_pct"] == pytest.approx(-0.03)
    assert m["equity_curve"] == [0.06, 0.03, 0.09, 0.06]


def test_backtest_endpoint_requires_auth():
    client = TestClient(routes.app)
    assert client.post("/backtest", json={}).status_code == 401


def test_backtest_endpoint_returns_metrics(monkeypatch):
    import app.backtest.data_collector as dc
    import app.backtest.runner as rn

    monkeypatch.setattr(dc, "load_all", lambda syms, tf: {"X/BTC": "bars"})

    async def fake_run(bars, params):
        return {"n_trades": 3, "win_rate": 0.66, "net_pct": 0.05,
                "kelly": 0.2, "expectancy_R": 0.5, "trades": [1, 2, 3]}

    monkeypatch.setattr(rn, "run_backtest", fake_run)

    client = TestClient(routes.app)
    r = client.post("/backtest", headers={"X-API-Secret": API_SECRET},
                    json={"symbols": ["X/BTC"], "stop_pct": 0.03})
    assert r.status_code == 200
    data = r.json()
    assert data["n_trades"] == 3
    assert data["symbols"] == ["X/BTC"]
    assert "trades" not in data            # stripped to keep the response lean


def test_backtest_endpoint_400_without_history(monkeypatch):
    import app.backtest.data_collector as dc
    monkeypatch.setattr(dc, "load_all", lambda syms, tf: {})
    client = TestClient(routes.app)
    r = client.post("/backtest", headers={"X-API-Secret": API_SECRET}, json={})
    assert r.status_code == 400
