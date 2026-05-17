import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from app.config import OPENROUTER_API_KEY, OPENROUTER_MODEL

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

SYSTEM_PROMPT = """Du bist ein entschlossener Crypto-Trader. Analysiere die Daten und triff eine klare Entscheidung.

Antworte AUSSCHLIESSLICH mit diesem JSON (kein anderer Text):
{"signal": "buy" | "sell" | "hold", "confidence": 0.0-1.0, "reason": "max 10 Wörter"}

KAUF-Signal wenn mind. 1 zutrifft:
- RSI unter 42 und dreht aufwärts (letzte 2-3 Kerzen)
- StochRSI %K unter 20, kreuzt %D aufwärts
- MACD-Histogramm wechselt negativ → positiv
- EMA20 kreuzt EMA50 von unten
- Preis schließt zurück über unterem Bollinger-Band

VERKAUF-Signal wenn mind. 1 zutrifft:
- RSI über 60 und dreht abwärts
- StochRSI %K über 80, kreuzt %D abwärts
- MACD-Histogramm wechselt positiv → negativ
- EMA20 kreuzt EMA50 von oben
- Preis schließt zurück unter oberem Bollinger-Band

HOLD nur wenn kein einziges Signal erkennbar ist.
SHORT-LOGIK: sell ohne offene Position = Short. buy bei offener Short = Schließen.
Confidence: 0.9+ für sehr klares Signal, 0.7-0.9 für klares Signal, 0.5-0.7 für schwächeres Signal."""


def build_prompt(symbol: str, df: pd.DataFrame, sentiment_block: str,
                 position: dict | None, market: str = "US") -> str:
    last   = df.tail(30)
    latest = last.iloc[-1]

    candles = [
        f"{row['timestamp'].strftime('%Y-%m-%d %H:%M')} | "
        f"O:{row['open']:.4f} H:{row['high']:.4f} L:{row['low']:.4f} C:{row['close']:.4f} "
        f"V:{row['volume']:.0f} | "
        f"RSI:{row['rsi']:.1f} SK:{row['stoch_k']:.1f} SD:{row['stoch_d']:.1f} "
        f"EMA20:{row['ema20']:.4f} EMA50:{row['ema50']:.4f} "
        f"BB_u:{row['bb_upper']:.4f} BB_l:{row['bb_lower']:.4f} "
        f"MACD:{row['macd']:.5f} Sig:{row['macd_sig']:.5f} Hist:{row['macd_hist']:.5f}"
        for _, row in last.iterrows()
    ]

    slope_20  = latest["close"] - last.iloc[-20]["close"]
    rsi_slope = latest["rsi"] - last.iloc[-3]["rsi"]
    ema_slope = latest["ema50"] - last.iloc[-6]["ema50"]
    macd_dir  = "steigend" if float(latest["macd_hist"]) > float(last.iloc[-2]["macd_hist"]) else "fallend"

    pos_ctx = ""
    if position:
        entry    = float(position.get("entry_price", position.get("avg_entry_price", 0)))
        pos_side = position.get("side", "long")
        if pos_side == "short":
            pl_pct = (entry - latest["close"]) / entry * 100
            sign   = "+" if pl_pct >= 0 else ""
            pos_ctx = (
                f"\nOFFENE SHORT-POSITION: Einstieg {entry:.4f} | "
                f"Aktuell {sign}{pl_pct:.2f}% | Qty: {position.get('qty', '?')} | "
                f"'buy'-Signal → Short schließen."
            )
        else:
            pl_pct = (latest["close"] - entry) / entry * 100
            sign   = "+" if pl_pct >= 0 else ""
            pos_ctx = (
                f"\nOFFENE LONG-POSITION: Einstieg {entry:.4f} | "
                f"Aktuell {sign}{pl_pct:.2f}% | Qty: {position.get('qty', '?')} | "
                f"Bewerte ob VERKAUFT werden soll."
            )

    tz_label = "ET" if market == "US" else "UTC"
    now_str  = datetime.now(ET if market == "US" else None).strftime("%Y-%m-%d %H:%M")

    stoch_k   = float(latest["stoch_k"]) if not pd.isna(latest["stoch_k"]) else 50.0
    stoch_d   = float(latest["stoch_d"]) if not pd.isna(latest["stoch_d"]) else 50.0
    stoch_zone = "oversold" if stoch_k < 20 else ("overbought" if stoch_k > 80 else "neutral")
    mom       = float(latest["mom"]) if not pd.isna(latest.get("mom", float("nan"))) else 0.0

    return (
        f"Symbol: {symbol} | Timeframe: 5m | {now_str} {tz_label}\n"
        f"Trend (20 Kerzen): {'aufwärts' if slope_20 > 0 else 'abwärts'} | "
        f"RSI: {'steigend' if rsi_slope > 0 else 'fallend'} ({latest['rsi']:.1f}) | "
        f"StochRSI: K={stoch_k:.1f} D={stoch_d:.1f} [{stoch_zone}] | "
        f"EMA20 {'>' if latest['ema20'] > latest['ema50'] else '<'} EMA50 | "
        f"MACD-Hist: {macd_dir} ({latest['macd_hist']:.5f}) | "
        f"MOM(4): {mom:+.2f}%"
        f"{pos_ctx}\n\n"
        f"{sentiment_block}\n\n"
        f"Letzte 30 Kerzen (älteste zuerst):\n"
        + "\n".join(candles)
        + "\n\nUmkehrsignal vorhanden? Deine Entscheidung:"
    )


async def call_llm(prompt: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": OPENROUTER_MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user",   "content": prompt},
                    ],
                    "max_tokens": 200,
                    "temperature": 0.1,
                },
            )
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"]["content"].strip()
            # strip optional markdown code fences
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            # extract first {...} block if LLM added surrounding text
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end != -1:
                raw = raw[start:end + 1]
            return json.loads(raw)
    except Exception as e:
        logger.warning("LLM call failed: %s", e)
        return {"signal": "hold", "confidence": 0.0, "reason": "LLM error"}
