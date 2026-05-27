"""Tests for the Bull/Bear+Judge entry debate (HTTP mocked)."""
import json

import pytest

from app.strategy import debate


class _FakeResponse:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _make_fake_client(role_outputs: dict[str, str]):
    """Returns a fake AsyncClient whose .post() picks a reply by system-prompt role."""
    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            system = json["messages"][0]["content"]
            if "BULL-Analyst" in system:
                return _FakeResponse(role_outputs["bull"])
            if "BEAR-Analyst" in system:
                return _FakeResponse(role_outputs["bear"])
            return _FakeResponse(role_outputs["judge"])

    return _FakeClient()


@pytest.mark.anyio
async def test_debate_strong_bull_yields_buy(monkeypatch):
    outputs = {
        "bull":  json.dumps({"thesis": "RSI oversold turning up, MACD crossing positive", "conviction": 0.85}),
        "bear":  json.dumps({"thesis": "minor resistance overhead", "conviction": 0.25}),
        "judge": json.dumps({"signal": "buy", "confidence": 0.78, "reason": "bull clearly outweighs"}),
    }
    monkeypatch.setattr(debate.httpx, "AsyncClient", lambda *a, **k: _make_fake_client(outputs))

    res = await debate.call_llm_debate("market data", market="crypto")
    assert res["signal"] == "buy"
    assert res["confidence"] == 0.78
    assert res["debate"]["bull_conviction"] == 0.85
    assert res["debate"]["bear_conviction"] == 0.25


@pytest.mark.anyio
async def test_debate_strong_bear_yields_hold(monkeypatch):
    outputs = {
        "bull":  json.dumps({"thesis": "weak bounce attempt", "conviction": 0.30}),
        "bear":  json.dumps({"thesis": "downtrend intact, falling knife risk, low volume", "conviction": 0.80}),
        "judge": json.dumps({"signal": "hold", "confidence": 0.4, "reason": "bear case stronger"}),
    }
    monkeypatch.setattr(debate.httpx, "AsyncClient", lambda *a, **k: _make_fake_client(outputs))

    res = await debate.call_llm_debate("market data", market="crypto")
    assert res["signal"] == "hold"


@pytest.mark.anyio
async def test_debate_handles_markdown_fences(monkeypatch):
    outputs = {
        "bull":  "```json\n" + json.dumps({"thesis": "x", "conviction": 0.6}) + "\n```",
        "bear":  "```json\n" + json.dumps({"thesis": "y", "conviction": 0.4}) + "\n```",
        "judge": "```json\n" + json.dumps({"signal": "buy", "confidence": 0.66, "reason": "ok"}) + "\n```",
    }
    monkeypatch.setattr(debate.httpx, "AsyncClient", lambda *a, **k: _make_fake_client(outputs))

    res = await debate.call_llm_debate("market data", market="crypto")
    assert res["signal"] == "buy"
    assert res["confidence"] == 0.66


@pytest.mark.anyio
async def test_debate_degrades_to_hold_on_bull_failure(monkeypatch):
    class _BoomClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(debate.httpx, "AsyncClient", lambda *a, **k: _BoomClient())
    res = await debate.call_llm_debate("market data", market="crypto")
    assert res["signal"] == "hold"
    assert res["confidence"] == 0.0
