"""
Sentiment-Modul für LLMStrategy — drei Datenquellen:
  1. Fear & Greed Index  (alternative.me)  — Cache 1h
  2. Polymarket Makro    (gamma API)        — Cache 30min
  3. Tavily News         (pro Coin)         — Cache 15min
"""
import json
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# In-Memory-Cache: {key: (timestamp, data)}
_CACHE: dict = {}


def _cached(key: str, ttl: int, fetch_fn):
    now = time.time()
    if key in _CACHE:
        ts, data = _CACHE[key]
        if now - ts < ttl:
            return data
    data = fetch_fn()
    _CACHE[key] = (now, data)
    return data


# ---------------------------------------------------------------------------
# 1. Fear & Greed
# ---------------------------------------------------------------------------

def get_fear_greed() -> dict:
    """{'value': 58, 'label': 'Greed', 'signal': 'neutral|fear|greed|extreme_fear|extreme_greed'}"""
    def _fetch():
        try:
            r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
            r.raise_for_status()
            d = r.json()["data"][0]
            value = int(d["value"])
            label = d["value_classification"]
            if value <= 20:
                signal = "extreme_fear"
            elif value <= 40:
                signal = "fear"
            elif value <= 60:
                signal = "neutral"
            elif value <= 80:
                signal = "greed"
            else:
                signal = "extreme_greed"
            return {"value": value, "label": label, "signal": signal}
        except Exception as e:
            logger.warning("Fear&Greed fetch failed: %s", e)
            return {"value": 50, "label": "Neutral", "signal": "neutral"}

    return _cached("fear_greed", 3600, _fetch)


# ---------------------------------------------------------------------------
# 2. Polymarket Makro-Risiko
# ---------------------------------------------------------------------------

_MACRO_KEYWORDS = [
    "federal reserve", "fed rate", "fed cut", "fed hike", "interest rate",
    "sec crypto", "bitcoin etf", "ethereum etf", "crypto regulation",
    "inflation cpi", "recession", "fomc",
]

_COIN_KEYWORDS = {
    "BTC":  ["bitcoin", "btc"],
    "ETH":  ["ethereum", "eth"],
    "SOL":  ["solana", "sol"],
    "XRP":  ["xrp", "ripple"],
    "ADA":  ["cardano", "ada"],
    "LINK": ["chainlink", "link"],
    "DOT":  ["polkadot", "dot"],
    "ATOM": ["cosmos", "atom"],
    "DOGE": ["dogecoin", "doge"],
    "LTC":  ["litecoin", "ltc"],
    "XLM":  ["stellar", "xlm"],
}


def get_polymarket_macro() -> dict:
    """
    Returns:
      {
        'risk':   'low' | 'medium' | 'high' | 'unknown',
        'events': ['question: XX% Yes', ...],   # bis zu 3 relevante Märkte
      }
    """
    def _fetch():
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active": "true", "closed": "false", "limit": 200},
                timeout=12,
            )
            r.raise_for_status()
            markets = r.json()
            if not isinstance(markets, list):
                markets = []

            relevant = []
            for m in markets:
                q = m.get("question", "").lower()
                if not any(kw in q for kw in _MACRO_KEYWORDS):
                    continue
                try:
                    prices   = json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) else (m.get("outcomePrices") or [])
                    outcomes = json.loads(m["outcomes"])      if isinstance(m.get("outcomes"), str)      else (m.get("outcomes") or [])
                except Exception:
                    continue
                yes_prob = next(
                    (float(p) for o, p in zip(outcomes, prices) if "yes" in str(o).lower()),
                    None,
                )
                if yes_prob is None:
                    continue
                relevant.append({
                    "question": m["question"],
                    "yes_prob": yes_prob,
                    "end_date": str(m.get("endDate", ""))[:10],
                })

            # Risiko-Heuristik
            risk = "low"
            for ev in relevant:
                q = ev["question"].lower()
                p = ev["yes_prob"]
                if any(kw in q for kw in ["hike", "recession", "ban", "crackdown"]) and p > 0.45:
                    risk = "high"
                    break
                if any(kw in q for kw in ["cut", "etf approved", "etf approval"]) and p > 0.65:
                    risk = "low"

            events = [
                f"{ev['question'][:65]}: {ev['yes_prob']*100:.0f}% Yes"
                for ev in relevant[:3]
            ]
            return {"risk": risk, "events": events}

        except Exception as e:
            logger.warning("Polymarket macro fetch failed: %s", e)
            return {"risk": "unknown", "events": []}

    return _cached("polymarket_macro", 1800, _fetch)


def get_polymarket_coin(coin: str) -> dict | None:
    """
    Sucht nach aktiven Polymarket-Märkten für einen spezifischen Coin.
    Returns {'question': ..., 'yes_prob': 0.62, 'end_date': '2026-05-31'} or None.
    """
    base = coin.split("/")[0].upper()
    keywords = _COIN_KEYWORDS.get(base, [base.lower()])

    def _fetch():
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active": "true", "closed": "false", "limit": 200},
                timeout=12,
            )
            r.raise_for_status()
            markets = r.json()
            if not isinstance(markets, list):
                return None

            price_kw = ["above", "below", "reach", "hit", "exceed", "price"]
            for m in markets:
                q = m.get("question", "").lower()
                if not any(kw in q for kw in keywords):
                    continue
                if not any(kw in q for kw in price_kw):
                    continue
                try:
                    prices   = json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) else (m.get("outcomePrices") or [])
                    outcomes = json.loads(m["outcomes"])      if isinstance(m.get("outcomes"), str)      else (m.get("outcomes") or [])
                except Exception:
                    continue
                yes_prob = next(
                    (float(p) for o, p in zip(outcomes, prices) if "yes" in str(o).lower()),
                    None,
                )
                if yes_prob is not None:
                    return {
                        "question": m["question"],
                        "yes_prob": yes_prob,
                        "end_date": str(m.get("endDate", ""))[:10],
                    }
            return None
        except Exception as e:
            logger.warning("Polymarket coin fetch failed for %s: %s", coin, e)
            return None

    return _cached(f"pm_coin_{base}", 1800, _fetch)


# ---------------------------------------------------------------------------
# 3. Tavily News
# ---------------------------------------------------------------------------

_COIN_SEARCH = {
    "BTC":  "Bitcoin price",
    "ETH":  "Ethereum price",
    "SOL":  "Solana SOL price",
    "XRP":  "XRP Ripple price",
    "ADA":  "Cardano ADA price",
    "LTC":  "Litecoin LTC price",
    "LINK": "Chainlink LINK price",
    "DOT":  "Polkadot DOT price",
    "ATOM": "Cosmos ATOM price",
    "DOGE": "Dogecoin DOGE price",
    "XLM":  "Stellar XLM price",
}


def get_coin_news(pair: str) -> list[str]:
    """Returns up to 3 recent headlines (letzten 24h) für den Coin."""
    if not TAVILY_API_KEY:
        return []
    base  = pair.split("/")[0].upper()
    query = _COIN_SEARCH.get(base, f"{base} crypto price news")

    def _fetch():
        try:
            r = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": TAVILY_API_KEY,
                    "query":        query,
                    "search_depth": "basic",
                    "max_results":  3,
                    "days":         1,
                },
                timeout=12,
            )
            r.raise_for_status()
            return [f"• {res['title']}" for res in r.json().get("results", [])[:3]]
        except Exception as e:
            logger.warning("Tavily news fetch failed for %s: %s", pair, e)
            return []

    return _cached(f"news_{base}", 900, _fetch)


# ---------------------------------------------------------------------------
# Kompakt-Zusammenfassung für LLM-Prompt
# ---------------------------------------------------------------------------

def build_sentiment_block(pair: str) -> str:
    """Gibt einen formatierten Sentiment-Block für den LLM-Prompt zurück."""
    fg   = get_fear_greed()
    macro = get_polymarket_macro()
    coin_market = get_polymarket_coin(pair)
    news = get_coin_news(pair)

    lines = ["=== MARKT-KONTEXT ==="]

    # Fear & Greed
    lines.append(f"Fear & Greed: {fg['value']}/100 — {fg['label']} [{fg['signal']}]")

    # Polymarket Makro
    lines.append(f"Polymarket Makro-Risiko: {macro['risk'].upper()}")
    for ev in macro["events"]:
        lines.append(f"  · {ev}")

    # Polymarket Coin-spezifisch
    if coin_market:
        lines.append(
            f"Polymarket {pair.split('/')[0]}-Markt: \"{coin_market['question'][:60]}\" "
            f"→ {coin_market['yes_prob']*100:.0f}% Yes (endet {coin_market['end_date']})"
        )

    # News
    if news:
        lines.append(f"Aktuelle News ({pair.split('/')[0]}, 24h):")
        lines.extend(news)

    lines.append("====================")
    return "\n".join(lines)


def should_block_entry(pair: str) -> tuple[bool, str]:
    """
    Harter Kauf-Filter basierend auf Sentiment.
    Returns (blocked: bool, reason: str).
    """
    fg = get_fear_greed()
    macro = get_polymarket_macro()

    if fg["signal"] == "extreme_fear":
        return True, f"Fear&Greed {fg['value']} (Extreme Fear) — Kauf blockiert"
    if fg["signal"] == "extreme_greed":
        return True, f"Fear&Greed {fg['value']} (Extreme Greed) — Kauf blockiert"
    if macro["risk"] == "high":
        return True, f"Polymarket Makro-Risiko HIGH — Kauf blockiert"

    return False, ""
