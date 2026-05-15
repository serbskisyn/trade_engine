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
KRAKEN_STAKE_AMOUNT:  float = float(os.getenv("KRAKEN_STAKE_AMOUNT", "0.0003"))
KRAKEN_MAX_POSITIONS: int   = int(os.getenv("KRAKEN_MAX_POSITIONS", "5"))
KRAKEN_PAIRS: list[str] = [
    p.strip() for p in os.getenv(
        "KRAKEN_PAIRS",
        "ETH/BTC,SOL/BTC,XRP/BTC,ADA/BTC,LTC/BTC,LINK/BTC,DOT/BTC,ATOM/BTC,DOGE/BTC,XLM/BTC,"
        "UNI/BTC,AAVE/BTC,ETC/BTC,TRX/BTC,XMR/BTC"
    ).split(",") if p.strip()
]

# ── Alpaca (US-Aktien) ────────────────────────────────────────────────────────
ALPACA_API_KEY:       str   = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY:    str   = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER:         bool  = os.getenv("ALPACA_PAPER", "false").lower() == "true"
ALPACA_STAKE_USD:     float = float(os.getenv("ALPACA_STAKE_USD", "10"))
ALPACA_MAX_POSITIONS: int   = int(os.getenv("ALPACA_MAX_POSITIONS", "3"))
ALPACA_SYMBOLS: list[str] = [
    s.strip() for s in os.getenv(
        "ALPACA_SYMBOLS",
        "SPY,QQQ,GLD,AAPL,MSFT,NVDA,TSLA,XLF,USO,AMZN,GOOGL,META,AMD,JPM,IWM"
    ).split(",") if s.strip()
]

# ── Strategie ─────────────────────────────────────────────────────────────────
BUY_CONFIDENCE:  float = float(os.getenv("BUY_CONFIDENCE",  "0.72"))
SELL_CONFIDENCE: float = float(os.getenv("SELL_CONFIDENCE", "0.72"))
STOP_LOSS_PCT:   float = float(os.getenv("STOP_LOSS_PCT",   "0.02"))
TRAILING_ACTIVATE_PCT: float = float(os.getenv("TRAILING_ACTIVATE_PCT", "0.02"))
TRAILING_TRAIL_PCT:    float = float(os.getenv("TRAILING_TRAIL_PCT",    "0.01"))
MIN_HOLD_CANDLES: int = int(os.getenv("MIN_HOLD_CANDLES", "6"))

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
