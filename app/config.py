import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID: int | None = int(x) if (x := os.getenv("ADMIN_CHAT_ID", "").strip()) else None

# ── LLM ───────────────────────────────────────────────────────────────────────
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL:   str = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")

# ── Kraken (Crypto) ───────────────────────────────────────────────────────────
KRAKEN_API_KEY:    str = os.getenv("KRAKEN_API_KEY", "")
KRAKEN_API_SECRET: str = os.getenv("KRAKEN_API_SECRET", "")
KRAKEN_STAKE_AMOUNT:  float = float(os.getenv("KRAKEN_STAKE_AMOUNT", "0.001"))
KRAKEN_MAX_POSITIONS: int   = int(os.getenv("KRAKEN_MAX_POSITIONS", "5"))
KRAKEN_PAIRS: list[str] = [
    p.strip() for p in os.getenv(
        "KRAKEN_PAIRS",
        "ETH/BTC,SOL/BTC,XRP/BTC,ADA/BTC,LTC/BTC,LINK/BTC,DOT/BTC,ATOM/BTC,DOGE/BTC,XLM/BTC,"
        "UNI/BTC,TRX/BTC"
    ).split(",") if p.strip()
]
KRAKEN_ALLOW_SHORTS:   bool = os.getenv("KRAKEN_ALLOW_SHORTS", "false").lower() == "true"
# Limit-Order-Strategie: warte TIMEOUT Sekunden auf Fill, dann Market-Fallback.
# Maker-Fee (0.08%) statt Taker-Fee (0.16%) spart bei jedem Round-Trip 0.16%.
KRAKEN_LIMIT_TIMEOUT: int   = int(os.getenv("KRAKEN_LIMIT_TIMEOUT", "15"))
KRAKEN_FEE_MAKER:     float = float(os.getenv("KRAKEN_FEE_MAKER", "0.0008"))  # 0.08% pro Leg

# ── Alpaca (US-Aktien) ────────────────────────────────────────────────────────
ALPACA_API_KEY:       str   = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY:    str   = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER:         bool  = os.getenv("ALPACA_PAPER", "false").lower() == "true"
ALPACA_STAKE_USD:     float = float(os.getenv("ALPACA_STAKE_USD", "10"))
ALPACA_MAX_POSITIONS: int   = int(os.getenv("ALPACA_MAX_POSITIONS", "3"))
ALPACA_SYMBOLS: list[str] = [
    s.strip() for s in os.getenv(
        "ALPACA_SYMBOLS",
        # Long: breite Diversifikation
        "SPY,QQQ,GLD,AAPL,MSFT,NVDA,TSLA,XLF,USO,AMZN,GOOGL,META,AMD,JPM,IWM,"
        # Inverse ETFs für Short-Exposure ohne Margin
        "SQQQ,SDS,SPXS"
    ).split(",") if s.strip()
]

# ── Strategie ─────────────────────────────────────────────────────────────────
BUY_CONFIDENCE:   float = float(os.getenv("BUY_CONFIDENCE",   "0.60"))
SELL_CONFIDENCE:  float = float(os.getenv("SELL_CONFIDENCE",  "0.60"))
EXIT_CONFIDENCE:  float = float(os.getenv("EXIT_CONFIDENCE",  "0.68"))  # höher als Entry

# Markt-spezifische Confidence-Schwellen — Stocks brauchen mehr Sicherheit als Crypto,
# weil Aktien-Reversals häufiger Falling-Knives sind als bei mean-reverting Crypto-Pairs.
BUY_CONFIDENCE_CRYPTO:  float = float(os.getenv("BUY_CONFIDENCE_CRYPTO",  str(BUY_CONFIDENCE)))
BUY_CONFIDENCE_STOCKS:  float = float(os.getenv("BUY_CONFIDENCE_STOCKS",  "0.75"))
SELL_CONFIDENCE_CRYPTO: float = float(os.getenv("SELL_CONFIDENCE_CRYPTO", str(SELL_CONFIDENCE)))
SELL_CONFIDENCE_STOCKS: float = float(os.getenv("SELL_CONFIDENCE_STOCKS", "0.75"))

# Entry-Debatte (Bull/Bear+Judge statt einzelnem LLM-Call) — nur für ENTRIES
# auf Symbole die den Technik-Vorfilter passieren. Adressiert den Conviction-Cap
# (einzelner Call cappt ~0.56). Nur crypto default-an; Exits nutzen weiter call_llm.
ENTRY_DEBATE_ENABLED: bool = os.getenv("ENTRY_DEBATE_ENABLED", "true").lower() == "true"
EXIT_CONFIDENCE_CRYPTO: float = float(os.getenv("EXIT_CONFIDENCE_CRYPTO", str(EXIT_CONFIDENCE)))
EXIT_CONFIDENCE_STOCKS: float = float(os.getenv("EXIT_CONFIDENCE_STOCKS", "0.75"))

# Re-Entry-Cooldown nach Stop-Loss (Minuten — 0 = deaktiviert).
# Stocks: 60 Min verhindert "Falling-Knife"-Doppel-Entries innerhalb 1h.
REENTRY_COOLDOWN_MIN_CRYPTO: int = int(os.getenv("REENTRY_COOLDOWN_MIN_CRYPTO", "0"))
REENTRY_COOLDOWN_MIN_STOCKS: int = int(os.getenv("REENTRY_COOLDOWN_MIN_STOCKS", "60"))

# Stocks: Keine neuen Entries nach diesem ET-Zeitpunkt (Late-Day Mean-Reversion-Fail-Schutz).
# Existing positions werden trotzdem weiter verwaltet (Stops/Exits).
STOCKS_ENTRY_CUTOFF_HOUR:   int = int(os.getenv("STOCKS_ENTRY_CUTOFF_HOUR",   "14"))
STOCKS_ENTRY_CUTOFF_MINUTE: int = int(os.getenv("STOCKS_ENTRY_CUTOFF_MINUTE", "45"))

# Stocks: Daily-Trend-Gate — Long-Entries nur wenn Tageskerze > EMA50(1D).
# Blockt Falling-Knife-Käufe an Down-Days, selbst wenn 5m-Reversal-Signale stark wirken.
STOCKS_DAILY_TREND_GATE: bool = os.getenv("STOCKS_DAILY_TREND_GATE", "true").lower() == "true"

# Pro-Markt Stops — Crypto volatiler (3%), Stocks enger (1.5%).
# STOP_LOSS_PCT bleibt als globaler Fallback für ältere Tests / Legacy.
STOP_LOSS_PCT:         float = float(os.getenv("STOP_LOSS_PCT",         "0.02"))
STOP_LOSS_PCT_CRYPTO:  float = float(os.getenv("STOP_LOSS_PCT_CRYPTO",  "0.03"))
STOP_LOSS_PCT_STOCKS:  float = float(os.getenv("STOP_LOSS_PCT_STOCKS",  "0.015"))
TRAILING_ACTIVATE_PCT:        float = float(os.getenv("TRAILING_ACTIVATE_PCT",        "0.02"))
TRAILING_ACTIVATE_PCT_CRYPTO: float = float(os.getenv("TRAILING_ACTIVATE_PCT_CRYPTO", "0.03"))
TRAILING_ACTIVATE_PCT_STOCKS: float = float(os.getenv("TRAILING_ACTIVATE_PCT_STOCKS", "0.015"))
TRAILING_TRAIL_PCT:           float = float(os.getenv("TRAILING_TRAIL_PCT",           "0.01"))
TRAILING_TRAIL_PCT_CRYPTO:    float = float(os.getenv("TRAILING_TRAIL_PCT_CRYPTO",    "0.015"))
TRAILING_TRAIL_PCT_STOCKS:    float = float(os.getenv("TRAILING_TRAIL_PCT_STOCKS",    "0.008"))
MIN_HOLD_CANDLES:  int   = int(os.getenv("MIN_HOLD_CANDLES",  "6"))    # 30 Min Mindesthaltedauer
MAX_HOLD_CANDLES:  int   = int(os.getenv("MAX_HOLD_CANDLES",  "96"))   # 8h Force-Close
# LLM-Exit nur wenn P&L >= dieses Minimum (Gebühren sind 0.16% RT — Mindestgewinn 0.30%)
MIN_PROFIT_PCT:    float = float(os.getenv("MIN_PROFIT_PCT", "0.003"))  # 0.30%

# ── Circuit Breaker ────────────────────────────────────────────────────────────
CIRCUIT_BREAKER_MAX_LOSS_BTC: float = float(os.getenv("CIRCUIT_BREAKER_MAX_LOSS_BTC", "-0.003"))
CIRCUIT_BREAKER_WINDOW:       int   = int(os.getenv("CIRCUIT_BREAKER_WINDOW", "10"))

# ── Volatilitäts-basierte Positionsgröße ──────────────────────────────────────
BB_VOL_SCALING: float = float(os.getenv("BB_VOL_SCALING", "2.0"))

# ── Sentiment ─────────────────────────────────────────────────────────────────
TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")

# ── API ───────────────────────────────────────────────────────────────────────
API_HOST:   str = os.getenv("API_HOST", "127.0.0.1")
API_PORT:   int = int(os.getenv("API_PORT", "8081"))
API_SECRET: str = os.getenv("API_SECRET", "change_me")

# ── Datenbank ─────────────────────────────────────────────────────────────────
DB_PATH: str = os.getenv("DB_PATH", "/home/pi/trade_engine/data/trades.db")


def validate():
    missing = [k for k, v in {"OPENROUTER_API_KEY": OPENROUTER_API_KEY}.items() if not v]
    if missing:
        raise ValueError(f"Fehlende Umgebungsvariablen: {', '.join(missing)}")
    if not API_SECRET or API_SECRET == "change_me":
        raise ValueError(
            "API_SECRET fehlt oder steht auf 'change_me' — "
            "ohne starkes Secret ist die Trade Engine offen erreichbar."
        )
