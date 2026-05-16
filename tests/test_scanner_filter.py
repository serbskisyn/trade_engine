"""Tests für scanner._technical_signal — 3 Pfade: Momentum, EMA-Crossover, BB-Touch."""
import pandas as pd

from app.engine.scanner import _technical_signal


def _make_df(rsi: float = 50, stoch_k: float = 50, macd_hist: float = 0,
             prev_macd_hist: float = 0, close: float = 100,
             bb_upper: float = 110, bb_lower: float = 90, bb_mid: float = 100,
             ema20: float = 100, ema50: float = 100,
             prev_ema20: float = 100, prev_ema50: float = 100,
             volume: float = 1000, vol_sma6: float = 1000) -> pd.DataFrame:
    """Baut einen 3-Zeilen-DataFrame, in dem nur die letzten 2 Rows die getesteten Werte haben."""
    base = {
        "rsi": [50, prev_macd_hist, rsi],  # nur lat (-1) wird gelesen
        "stoch_k": [50, 50, stoch_k],
        "macd_hist": [0, prev_macd_hist, macd_hist],
        "close": [100, 100, close],
        "bb_upper": [110, 110, bb_upper],
        "bb_lower": [90, 90, bb_lower],
        "bb_mid": [100, 100, bb_mid],
        "ema20": [100, prev_ema20, ema20],
        "ema50": [100, prev_ema50, ema50],
        "volume": [1000, 1000, volume],
        "vol_sma6": [1000, 1000, vol_sma6],
    }
    return pd.DataFrame(base)


# ── Hold-Setup: nichts triggert ───────────────────────────────────────────────

def test_neutral_setup_returns_no_signal():
    df = _make_df()
    long_ok, short_ok = _technical_signal(df)
    assert long_ok is False
    assert short_ok is False


# ── Momentum-Pfad: 2-of-3 ─────────────────────────────────────────────────────

def test_momentum_long_when_two_of_three_indicators_agree():
    """RSI<45 + Stoch<25 (2 von 3) + Volume ok → Long-Signal."""
    df = _make_df(rsi=40, stoch_k=20, volume=1200)
    long_ok, short_ok = _technical_signal(df)
    assert long_ok is True
    assert short_ok is False


def test_momentum_short_when_two_of_three_agree():
    """RSI>58 + Stoch>75 → Short."""
    df = _make_df(rsi=65, stoch_k=80, volume=1200)
    long_ok, short_ok = _technical_signal(df)
    assert short_ok is True
    assert long_ok is False


def test_momentum_blocked_without_volume():
    """Auch 2-of-3 muss am Volume-Filter scheitern wenn kein Pfad ohne Volume greift."""
    df = _make_df(rsi=40, stoch_k=20, volume=500, vol_sma6=1000,
                  ema20=100, ema50=100, prev_ema20=100, prev_ema50=100,
                  close=100, bb_upper=110, bb_lower=90)
    # Volume 500 < 80% von vol_sma6 1000 → momentum nicht erlaubt
    # Aber: EMA-Crossover und BB-Touch sind getrennte Pfade — hier auch deaktiviert
    long_ok, short_ok = _technical_signal(df)
    assert long_ok is False
    assert short_ok is False


# ── EMA-Crossover-Pfad ────────────────────────────────────────────────────────

def test_ema_cross_up_triggers_long_without_momentum():
    """EMA20 kreuzt EMA50 von unten — auch ohne Momentum-Signal Long."""
    df = _make_df(rsi=50, stoch_k=50, volume=500,
                  prev_ema20=99, prev_ema50=100,
                  ema20=101, ema50=100)
    long_ok, short_ok = _technical_signal(df)
    assert long_ok is True
    assert short_ok is False


def test_ema_cross_down_triggers_short():
    """EMA20 kreuzt EMA50 von oben → Short."""
    df = _make_df(rsi=50, stoch_k=50, volume=500,
                  prev_ema20=101, prev_ema50=100,
                  ema20=99, ema50=100)
    long_ok, short_ok = _technical_signal(df)
    assert short_ok is True
    assert long_ok is False


# ── Bollinger-Band-Pfad ───────────────────────────────────────────────────────

def test_bb_touch_low_triggers_long():
    """Close <= bb_lower → Mean-Reversion-Long."""
    df = _make_df(rsi=50, stoch_k=50, volume=500,
                  close=89, bb_lower=90, bb_upper=110)
    long_ok, short_ok = _technical_signal(df)
    assert long_ok is True
    assert short_ok is False


def test_bb_touch_high_triggers_short():
    """Close >= bb_upper → Mean-Reversion-Short."""
    df = _make_df(rsi=50, stoch_k=50, volume=500,
                  close=111, bb_lower=90, bb_upper=110)
    long_ok, short_ok = _technical_signal(df)
    assert short_ok is True
    assert long_ok is False


# ── Edge: zu wenig Daten ──────────────────────────────────────────────────────

def test_short_df_returns_no_signal():
    df = pd.DataFrame({"close": [100, 101]})
    long_ok, short_ok = _technical_signal(df)
    assert long_ok is False
    assert short_ok is False
