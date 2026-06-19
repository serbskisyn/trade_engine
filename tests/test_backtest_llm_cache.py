"""Tests for the backtest LLM verdict cache (harness inc. 5)."""
import asyncio

from app.backtest import llm_cache


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_cache, "DB_PATH", tmp_path / "llm_cache.db")


def test_get_miss_returns_none(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert llm_cache.get("model-x", "prompt") is None


def test_put_then_get_roundtrip(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    llm_cache.put("model-x", "prompt", {"signal": "buy", "confidence": 0.7, "reason": "r"})
    hit = llm_cache.get("model-x", "prompt")
    assert hit == {"signal": "buy", "confidence": 0.7, "reason": "r"}


def test_key_separates_models(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    llm_cache.put("model-a", "prompt", {"signal": "buy", "confidence": 0.7})
    assert llm_cache.get("model-b", "prompt") is None


def test_cached_verdict_calls_once(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def fake_call(prompt, market):
        calls["n"] += 1
        return {"signal": "buy", "confidence": 0.8, "reason": "x"}

    async def run():
        a = await llm_cache.cached_verdict("p", "crypto", "m", fake_call)
        b = await llm_cache.cached_verdict("p", "crypto", "m", fake_call)
        return a, b

    a, b = asyncio.run(run())
    assert a["signal"] == "buy" and a == b
    assert calls["n"] == 1  # second call served from cache
