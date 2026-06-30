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
  <img src="https://img.shields.io/badge/Mode-Paper%20(dry__run)-yellow?style=flat-square" />
  <img src="https://img.shields.io/badge/LLM-gpt--5.4--nano-orange?style=flat-square" />
</p>

---

Trade Engine is a standalone Python service that autonomously scans **12 crypto pairs + 18 US stocks** across two exchanges, generates LLM trading signals, and executes orders — entirely independent of the Telegram frontend. The bot is just a display layer; trades happen even if the bot is down.

> **Currently in PAPER mode** (`TRADING_DRY_RUN=true`) — orders are simulated, logged with `mode='dry_run'`. An **`EXPLORE_MODE`** learning toggle (one reversible flag, paper only) loosens the gates to generate many trades as training data — see [Explore / Learning mode](#explore--learning-mode).

```
Every 5 minutes:   Fetch 5m candles → Indicators → Trend+Technical pre-filter
                   → Sentiment → LLM (Bull/Bear/Judge debate) → Order
Every 30 seconds:  Price check → Stop-loss / Trailing stop → Close if triggered
```

---

## How It Works

### Signal Pipeline

```
Symbol list (12 crypto pairs + 18 US stocks)
        │
        ▼
Fetch 5m candles (Kraken ccxt / Alpaca SDK) + 1h trend + 1D daily bars
        │
        ▼
Calculate indicators  (RSI · EMA20/50 · Bollinger Bands · Stoch · MACD · vol-SMA)
        │
        ▼
Trend gate
  1h EMA50 slope down            → no longs
  stocks: daily close < EMA50    → no longs (STOCKS_DAILY_TREND_GATE)
  crypto downtrend + shorts on   → allow short  (KRAKEN_ALLOW_SHORTS)
        │
        ▼
Technical pre-filter (_technical_signal) — any ONE path reaches the LLM:
  1) Momentum  2-of-3 (RSI · Stoch-K · MACD-cross) + volume
  2) EMA20/50 crossover
  3) Bollinger-Band touch
  + re-entry cooldown · stocks entry-cutoff before close
        │
        ▼
Sentiment block (should_block_entry)
  Fear & Greed (alternative.me, 1h) · Polymarket macro (30min) · Tavily news (15min)
  ──► extreme fear / extreme greed / high macro risk → SKIP buy
        │
        ▼
LLM decision  (OpenRouter · openai/gpt-5.4-nano)
  ENTRIES: Bull ‖ Bear ‖ Judge debate (strategy/debate.py) — beats the single-call
           conviction cap (~0.56 → decisive 0.62–0.67). EXITS: single call.
  Output: {"signal": "buy|sell|hold", "confidence": 0.0–1.0, "reason": "..."}
        │
        ▼
Per-market confidence gate
  crypto: buy ≥ 0.55 · sell ≥ 0.70 · exit ≥ 0.68
  stocks: buy ≥ 0.75 · sell/exit ≥ 0.75
  (EXPLORE_MODE caps buy/sell at 0.50)
        │
        ▼
Execute (volatility-adjusted stake: wider BB → smaller position)
```

### Stop Monitor (30s, no LLM)

```
Every 30 seconds per open position (per-market stops; crypto is more volatile):
  Fetch current price (cheap ticker call)
        │
        ├── loss > stop %          → STOP-LOSS → close immediately
        │     crypto 3% · stocks 1.5%
        │
        └── profit > activate % ever reached (trailing active)?
              crypto +3% · stocks +2%
              └── price dropped > trail % from peak → TRAILING STOP → close
                    crypto 3% · stocks 1%
  LLM exits are gated by MIN_PROFIT_PCT (1.2%) so winners aren't cut at +0.3%.
  MIN_HOLD 2 candles · MAX_HOLD 48 candles (4h force-close).
```

---

## Explore / Learning mode

`EXPLORE_MODE=true` (**paper only**) is one reversible flag that loosens every entry gate to
generate many trades as learning data:
- `_technical_signal` → 1-of-3 + relaxed thresholds (volume ignored)
- crypto shorts on · stocks daily-trend gate off · 1h-slope blocks longs only on *steep* downtrends
- re-entry cooldown 0 · buy/sell confidence capped at 0.50
- Fear & Greed / macro sentiment buy-block bypassed

Set `EXPLORE_MODE=false` to restore the strict production gates. With strict gates in choppy
or bearish markets, very few symbols pass the pre-filter — that's by design.

> **Ops notes.** Trades only happen when the LLM call succeeds — a depleted OpenRouter key
> returns `403 "Key limit exceeded"` and every signal falls back to `hold` (raise the key
> limit / top up, or point at a working LLM proxy). The crypto/stocks scan loops are
> self-healing — an exchange error no longer kills the task (it re-inits next cycle).

---

## Watchlists

**Crypto — Kraken (12 BTC pairs, 24/7)** — `config.KRAKEN_PAIRS`

| | | | |
|---|---|---|---|
| ETH/BTC | SOL/BTC | XRP/BTC | ADA/BTC |
| LTC/BTC | LINK/BTC | DOT/BTC | ATOM/BTC |
| DOGE/BTC | XLM/BTC | UNI/BTC | TRX/BTC |

**US Stocks — Alpaca (18 symbols, Mo–Fr 10:00–15:45 ET)** — `config.ALPACA_SYMBOLS`
(incl. inverse ETFs SQQQ/SDS/SPXS for short-side exposure)

| | | | | | |
|---|---|---|---|---|---|
| SPY | QQQ | GLD | AAPL | MSFT | NVDA |
| TSLA | XLF | USO | AMZN | GOOGL | META |
| AMD | JPM | IWM | SQQQ | SDS | SPXS |

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
│   │   ├── indicators.py      RSI · EMA20/50 · Bollinger · Stoch · MACD
│   │   ├── llm.py             OpenRouter prompt builder + async call (exits)
│   │   ├── debate.py          Bull ‖ Bear ‖ Judge entry debate
│   │   └── sentiment.py       Fear&Greed · Polymarket · Tavily (EXPLORE_MODE bypass)
│   │
│   ├── engine/
│   │   ├── trade_manager.py   SQLite positions + per-market stop/trailing
│   │   ├── scanner.py         Trend gate + _technical_signal + LLM + EXPLORE_MODE
│   │   ├── scheduler.py       self-healing loops: crypto 24/7, stocks Mo–Fr, price monitor
│   │   └── price_monitor.py   30s cheap price check — no LLM, no bars
│   │
│   ├── backtest/             SHIPPED — BacktestExchange, runner, data collector, metrics
│   │
│   └── api/
│       └── routes.py          FastAPI: /health /status /positions /stats /scan /backtest
│
├── data/                      SQLite DBs (git-ignored)
│   ├── trades.db              positions + trade_log (dry_run rows in paper mode)
│   └── backtest/history.db    historical OHLCV archive
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
| `OPENROUTER_MODEL` | `openai/gpt-5.4-nano` | Model for signals |
| `TRADING_DRY_RUN` | `true` | **Paper mode** — simulate orders (`mode='dry_run'`) |
| `EXPLORE_MODE` | `false` | Learning mode — loosen all gates (paper only), see below |
| `ENTRY_DEBATE_ENABLED` | `true` | Bull/Bear/Judge debate for entries |
| `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` | — | Kraken exchange |
| `KRAKEN_STAKE_AMOUNT` | `0.001` | BTC per trade |
| `KRAKEN_MAX_POSITIONS` | `5` | Max open crypto positions |
| `KRAKEN_ALLOW_SHORTS` | `false` | Allow crypto shorts in downtrends |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | — | Alpaca Markets |
| `ALPACA_STAKE_USD` | `10` | USD per stock trade |
| `ALPACA_MAX_POSITIONS` | `3` | Max open stock positions |
| `STOCKS_DAILY_TREND_GATE` | `true` | Block stock longs below daily EMA50 |
| `BUY_CONFIDENCE_CRYPTO` / `_STOCKS` | `0.55` / `0.75` | Min confidence to buy (per market) |
| `SELL_CONFIDENCE_CRYPTO` / `_STOCKS` | `0.70` / `0.75` | Min confidence to short |
| `EXIT_CONFIDENCE_CRYPTO` / `_STOCKS` | `0.68` / `0.75` | Min confidence for LLM exit |
| `MIN_PROFIT_PCT` | `0.012` | LLM exit only above +1.2% |
| `STOP_LOSS_PCT_CRYPTO` | `0.03` | Hard stop crypto −3% (stocks −1.5%) |
| `TRAILING_ACTIVATE_PCT_CRYPTO` | `0.03` | Trailing activates at +3% (crypto) |
| `TRAILING_TRAIL_PCT_CRYPTO` | `0.03` | Trail distance 3% (crypto) |
| `REENTRY_COOLDOWN_MIN_STOCKS` | `60` | Cooldown after a stop before re-entry |
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

## Backtest Mode (shipped)

Lives in **`app/backtest/`** — a `BacktestExchange(BaseExchange)` drop-in, a tick-based runner
that reuses the *real* `scanner.run_scan()` + `_technical_signal` (zero scanner changes), a
historical OHLCV collector (`data/backtest/history.db`, hourly cron), an LLM-response cache for
determinism, and a metrics endpoint. `scripts/backtest_exits.py` sweeps exit-param sets against
Kraken 5m history (validated Trail 1.5%→3.0%: Net +21%→+22.5%). It's the foundation for
autonomous strategy optimization (overnight agent that sweeps `config.py` params — see
`karpathy/autoresearch` for the pattern).

**Design (as built):**

1. **`BacktestExchange(BaseExchange)`** — drop-in replacement that
   - Holds historical OHLCV data in memory (per symbol)
   - Implements `fetch_bars()` by slicing to the current simulated time
   - Implements `place_order()` / `close_position()` against an in-memory portfolio with fee simulation (uses `KRAKEN_FEE_MAKER` / Alpaca fee model)
   - Returns deterministic results — no real API calls

2. **`Backtest Runner`** — tick-based loop over historical candles
   - Advances a central simulated clock candle-by-candle
   - Calls existing `scanner.run_scan()` at each tick — zero changes to scanner code
   - Writes trades to a separate `backtest_trades.db` (via existing `DB_PATH` env var)

3. **Historical Data Collector** — separate cron-like job
   - Polls Kraken `fetch_ohlcv` continuously (Kraken only exposes ~720 bars at a time)
   - Appends to a SQLite/parquet archive in `data/historical/`
   - Alpaca already exposes years of history, no collector needed there

4. **Sentiment stub** — `sentiment.py` is not historically reconstructable (Fear&Greed, Polymarket, Tavily are live-only)
   - In backtest mode: return a neutral "no block" sentiment block
   - Optional: snapshot live sentiment values to a TSV during prod runs for partial historical replay

5. **LLM handling** — two backtest modes
   - **Technical-only**: bypass `call_llm()`, let `_technical_signal()` alone decide → fast, deterministic, free
   - **LLM-cached**: hash the prompt, cache responses in SQLite → deterministic on second run, ~$5-10 per fresh full run

6. **Metrics output** — extend existing `get_fee_stats()`
   - Add Sharpe ratio, max drawdown, time-in-market
   - Return as JSON from the new endpoint

7. **API endpoint** — `POST /backtest`
   - Body: `{params: {...}, market: "crypto"|"stocks", from: ISO, to: ISO, llm_mode: "off"|"cached"}`
   - Response: full metrics dict + trade log
   - Allows external automation (e.g. parameter sweep scripts, autoresearch-style agents)

**Seams that made it clean:** `BaseExchange` dependency-injection, pure `calc_indicators()` /
`_technical_signal()`, parameterized `DB_PATH`, and `get_fee_stats()` (gross/net P&L, payoff
ratio, breakeven win-rate). Covered by `tests/test_backtest_*`.

---

## License

Private project — not licensed for public use.
