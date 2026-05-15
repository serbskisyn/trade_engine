<h1 align="center">Trade Engine</h1>

<p align="center">
  <strong>Autonomous LLM-driven Trading Service — Crypto + US Stocks</strong><br>
  <sub>Runs independently of the Telegram bot · REST API · 24/7 on Raspberry Pi</sub>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-blue?style=flat-square&logo=python" />
  <img src="https://img.shields.io/badge/Exchanges-Kraken%20%2B%20Alpaca-green?style=flat-square" />
  <img src="https://img.shields.io/badge/API-FastAPI%208081-teal?style=flat-square" />
  <img src="https://img.shields.io/badge/Candles-5m-orange?style=flat-square" />
  <img src="https://img.shields.io/badge/Stops-30s%20monitor-red?style=flat-square" />
</p>

---

Trade Engine is a standalone Python service that autonomously scans 30 symbols across two exchanges, generates LLM trading signals, and executes orders — entirely independent of the Telegram frontend. The bot is just a display layer; trades happen even if the bot is down.

```
Every 5 minutes:   Fetch 5m candles → Indicators → Sentiment → LLM → Order
Every 30 seconds:  Price check → Stop-loss / Trailing stop → Close if triggered
```

---

## How It Works

### Signal Pipeline

```
Symbol list (15 crypto + 15 stocks)
        │
        ▼
Fetch 100× 5m candles (Kraken ccxt / Alpaca SDK)
        │
        ▼
Calculate indicators
  RSI (EWM, com=13)
  EMA20 / EMA50
  Bollinger Bands (20-period)
        │
        ▼
EMA50 slope filter ──► negative slope → SKIP (no entry)
        │
        ▼
Sentiment block
  Fear & Greed Index (alternative.me, 1h cache)
  Polymarket Gamma API (macro events, 30min cache)
  Tavily news headlines (15min cache)
  ──► extreme fear / extreme greed / high macro risk → SKIP
        │
        ▼
LLM decision (OpenRouter / GPT-4o-mini)
  System prompt: trend reversal specialist
  Input: 50 candles + indicators + sentiment block + open position context
  Output: {"signal": "buy|sell|hold", "confidence": 0.0–1.0, "reason": "..."}
        │
        ▼
confidence ≥ 0.65 → Execute order
confidence < 0.65 → hold
```

### Stop Monitor (30s, no LLM)

```
Every 30 seconds per open position:
  Fetch current price (cheap ticker call)
        │
        ├── loss > 2%         → STOP-LOSS → close immediately
        │
        └── profit > 2% ever reached (trailing active)?
              └── price dropped > 1% from peak → TRAILING STOP → close
```

---

## Watchlists

**Crypto — Kraken (15 BTC pairs, 24/7)**

| | | | | |
|---|---|---|---|---|
| ETH/BTC | SOL/BTC | XRP/BTC | ADA/BTC | LTC/BTC |
| LINK/BTC | DOT/BTC | ATOM/BTC | DOGE/BTC | XLM/BTC |
| UNI/BTC | AAVE/BTC | ETC/BTC | TRX/BTC | XMR/BTC |

**US Stocks — Alpaca (15 symbols, Mo–Fr 10:00–15:45 ET)**

| | | | | |
|---|---|---|---|---|
| SPY | QQQ | GLD | AAPL | MSFT |
| NVDA | TSLA | XLF | USO | AMZN |
| GOOGL | META | AMD | JPM | IWM |

---

## REST API

All endpoints require `X-API-Secret` header.

```bash
BASE=http://127.0.0.1:8081
SECRET=your_secret

# Health
curl $BASE/health

# Full status (positions + account + stats)
curl -H "X-API-Secret: $SECRET" $BASE/status

# Open positions
curl -H "X-API-Secret: $SECRET" "$BASE/positions?market=crypto"
curl -H "X-API-Secret: $SECRET" "$BASE/positions?market=stocks"

# Trade statistics
curl -H "X-API-Secret: $SECRET" $BASE/stats

# Trigger manual scan
curl -X POST -H "X-API-Secret: $SECRET" "$BASE/scan?market=all"
curl -X POST -H "X-API-Secret: $SECRET" "$BASE/scan?market=crypto"
curl -X POST -H "X-API-Secret: $SECRET" "$BASE/scan?market=stocks"
```

### Response: `/status`

```json
{
  "crypto": {
    "enabled": true,
    "positions": [
      {
        "symbol": "ETH/BTC",
        "entry_price": 0.02541,
        "qty": 0.012,
        "peak_price": 0.02589,
        "trailing_active": 1,
        "candles_held": 14
      }
    ]
  },
  "stocks": {
    "enabled": true,
    "market_open": false,
    "account": { "equity": 52.40, "cash": 42.40, "mode": "live" },
    "positions": []
  },
  "stats": {
    "total_trades": 7,
    "total_pl": 1.24,
    "avg_pl_pct": 0.82,
    "wins": 5,
    "losses": 2,
    "win_rate": 71.4
  }
}
```

---

## Project Structure

```
trade_engine/
├── app/
│   ├── config.py              All env vars — never call os.getenv() elsewhere
│   ├── main.py                asyncio entry: scan tasks + uvicorn
│   │
│   ├── exchanges/
│   │   ├── base.py            Abstract BaseExchange interface
│   │   ├── kraken.py          Kraken via ccxt (5m candles, 24/7)
│   │   └── alpaca.py          Alpaca via alpaca-py (5m candles, market hours)
│   │
│   ├── strategy/
│   │   ├── indicators.py      RSI · EMA20/50 · Bollinger Bands
│   │   ├── llm.py             OpenRouter prompt builder + async call
│   │   └── sentiment.py       Fear&Greed · Polymarket · Tavily
│   │
│   ├── engine/
│   │   ├── trade_manager.py   SQLite positions + stop-loss + trailing stop
│   │   ├── scanner.py         Per-symbol scan logic (exit + entry path)
│   │   ├── scheduler.py       asyncio loops: crypto 24/7, stocks Mo–Fr, price monitor
│   │   └── price_monitor.py   30s cheap price check — no LLM, no bars
│   │
│   └── api/
│       └── routes.py          FastAPI: /health /status /positions /stats /scan
│
├── data/                      SQLite DB (git-ignored)
│   └── trades.db              positions table + trade_log table
│
├── .env.example
├── requirements.txt
└── trade_engine.service       systemd unit file
```

---

## Setup

```bash
cd /home/pi/trade_engine
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in OPENROUTER_API_KEY, KRAKEN_API_KEY, ALPACA_API_KEY, ...
```

### systemd

```bash
sudo cp trade_engine.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trade_engine

# Logs
sudo journalctl -u trade_engine -f
```

### Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `OPENROUTER_API_KEY` | required | LLM API key |
| `OPENROUTER_MODEL` | `openai/gpt-4o-mini` | Model for signals |
| `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` | — | Kraken exchange |
| `KRAKEN_STAKE_AMOUNT` | `0.0003` | BTC per trade |
| `KRAKEN_MAX_POSITIONS` | `5` | Max open crypto positions |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | — | Alpaca Markets |
| `ALPACA_PAPER` | `false` | Paper trading mode |
| `ALPACA_STAKE_USD` | `10` | USD per stock trade |
| `ALPACA_MAX_POSITIONS` | `3` | Max open stock positions |
| `BUY_CONFIDENCE` | `0.65` | Min confidence to buy |
| `SELL_CONFIDENCE` | `0.65` | Min confidence to sell |
| `STOP_LOSS_PCT` | `0.02` | Hard stop at −2% |
| `TRAILING_ACTIVATE_PCT` | `0.02` | Trailing activates at +2% |
| `TRAILING_TRAIL_PCT` | `0.01` | Trail distance 1% |
| `TAVILY_API_KEY` | — | Sentiment news search |
| `TELEGRAM_BOT_TOKEN` | — | For trade push alerts |
| `ADMIN_CHAT_ID` | — | Telegram chat to notify |
| `API_HOST` | `127.0.0.1` | FastAPI bind address |
| `API_PORT` | `8081` | FastAPI port |
| `API_SECRET` | required | Header auth secret |
| `DB_PATH` | `/home/pi/trade_engine/data/trades.db` | SQLite path |

---

## Adding an Exchange

1. Subclass `BaseExchange` in `app/exchanges/your_exchange.py`
2. Implement: `is_market_open`, `fetch_bars`, `get_positions`, `place_order`, `close_position`, `get_current_price`, `get_account_info`
3. Add to `scheduler.py` with its own loop and market-hours logic

---

## License

Private project — not licensed for public use.
