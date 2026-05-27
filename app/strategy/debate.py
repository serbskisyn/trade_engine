"""
debate.py — Bull/Bear+Judge entry decision (lightweight TradingAgents pattern).

Replaces the single LLM confidence call for ENTRY decisions on the few
symbols that pass the technical pre-filter. Instead of one conservative
model that caps around 0.56, three roles run:

  1. Bull  — strongest case FOR a long entry (concurrent with Bear)
  2. Bear  — strongest case AGAINST / risk advocate (concurrent with Bull)
  3. Judge — weighs both, outputs the final {signal, confidence, reason}

Pi/cost-conscious: only used for entries on the 1-4 pre-filter survivors,
not for exits and not for the 8-12 symbols the pre-filter rejects. Bull and
Bear run in parallel (asyncio.gather), so latency is two LLM rounds, not three.

Output shape matches call_llm() exactly so the scanner integration is a
drop-in: {"signal", "confidence", "reason"} (+ "debate" for transparency).
Degrades to a safe hold on any failure.
"""
from __future__ import annotations

import json
import logging

import httpx

from app.config import OPENROUTER_API_KEY, OPENROUTER_MODEL

logger = logging.getLogger(__name__)

_URL = "https://openrouter.ai/api/v1/chat/completions"

_BULL_SYSTEM = (
    "Du bist der BULL-Analyst in einem Trading-Komitee. Argumentiere den STÄRKSTEN "
    "Fall FÜR einen Long-Entry — basierend ausschließlich auf den gelieferten Daten. "
    "Nenne konkrete Indikatoren (RSI, MACD, EMA, StochRSI, BB, Volumen). Sei ehrlich: "
    "ist das Setup schwach, gib niedrige conviction. Keine Erfindungen.\n"
    'Antworte NUR mit JSON: {"thesis": "max 25 Wörter", "conviction": 0.0-1.0}'
)

_BEAR_SYSTEM = (
    "Du bist der BEAR-Analyst / Risk-Advocate in einem Trading-Komitee. Argumentiere den "
    "STÄRKSTEN Fall GEGEN den Long-Entry — Gegensignale, Trendschwäche, Überkauft-Risiko, "
    "fehlende Volumenstütze, mögliches Falling-Knife. Basierend nur auf den Daten.\n"
    'Antworte NUR mit JSON: {"thesis": "max 25 Wörter", "conviction": 0.0-1.0} '
    "(conviction = wie stark der Bear-Fall ist)"
)

_JUDGE_SYSTEM = (
    "Du bist der Portfolio-Manager. Du bekommst Bull- und Bear-These mit deren conviction. "
    "Entscheide, ob ein Long-Entry jetzt sinnvoll ist. Überwiegt der Bull klar → 'buy' mit "
    "entsprechend hoher confidence. Patt oder Bear stärker → 'hold'. Sei entschlossen — "
    "wäge die beiden Seiten ab, hedge nicht grundlos.\n"
    'Antworte NUR mit JSON: {"signal": "buy"|"hold", "confidence": 0.0-1.0, "reason": "max 12 Wörter"}'
)


async def _call_role(client: httpx.AsyncClient, system: str, user: str, max_tokens: int) -> dict:
    r = await client.post(
        _URL,
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        },
    )
    r.raise_for_status()
    raw = r.json()["choices"][0]["message"]["content"].strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]
    return json.loads(raw)


async def call_llm_debate(prompt: str, market: str = "crypto") -> dict:
    """Run Bull‖Bear → Judge. Returns {signal, confidence, reason, debate}.

    On any failure returns a safe hold so the scanner simply skips the entry.
    """
    import asyncio

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            bull, bear = await asyncio.gather(
                _call_role(client, _BULL_SYSTEM, prompt, 160),
                _call_role(client, _BEAR_SYSTEM, prompt, 160),
                return_exceptions=True,
            )
            if isinstance(bull, Exception) or isinstance(bear, Exception):
                logger.warning("debate: bull/bear failed bull=%s bear=%s", bull, bear)
                return {"signal": "hold", "confidence": 0.0, "reason": "debate error", "debate": {}}

            bull_thesis = str(bull.get("thesis", ""))[:120]
            bull_conv = float(bull.get("conviction", 0.0))
            bear_thesis = str(bear.get("thesis", ""))[:120]
            bear_conv = float(bear.get("conviction", 0.0))

            judge_user = (
                f"MARKTDATEN:\n{prompt}\n\n"
                f"BULL (conviction {bull_conv:.2f}): {bull_thesis}\n"
                f"BEAR (conviction {bear_conv:.2f}): {bear_thesis}\n\n"
                f"Deine Entscheidung:"
            )
            judge = await _call_role(client, _JUDGE_SYSTEM, judge_user, 120)
    except Exception as e:
        logger.warning("debate: judge/call failed: %s", e)
        return {"signal": "hold", "confidence": 0.0, "reason": "debate error", "debate": {}}

    signal = str(judge.get("signal", "hold")).lower()
    conf = float(judge.get("confidence", 0.0))
    reason = str(judge.get("reason", ""))[:80]

    logger.info(
        "debate: bull=%.2f bear=%.2f → %s conf=%.2f | %s",
        bull_conv, bear_conv, signal, conf, reason,
    )
    return {
        "signal": signal,
        "confidence": conf,
        "reason": reason,
        "debate": {
            "bull_conviction": bull_conv, "bull_thesis": bull_thesis,
            "bear_conviction": bear_conv, "bear_thesis": bear_thesis,
        },
    }
