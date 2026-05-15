import pandas as pd


def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add RSI, EMA20/50, Bollinger Bands, MACD to a OHLCV DataFrame."""
    # RSI
    delta = df["close"].diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    df["rsi"]      = 100 - (100 / (1 + gain.ewm(com=13, adjust=False).mean()
                                       / loss.ewm(com=13, adjust=False).mean()))
    # EMAs
    df["ema20"]    = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"]    = df["close"].ewm(span=50, adjust=False).mean()

    # Bollinger Bands
    roll           = df["close"].rolling(20)
    df["bb_mid"]   = roll.mean()
    df["bb_upper"] = df["bb_mid"] + 2 * roll.std()
    df["bb_lower"] = df["bb_mid"] - 2 * roll.std()

    # MACD (12/26/9)
    ema12           = df["close"].ewm(span=12, adjust=False).mean()
    ema26           = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]      = ema12 - ema26
    df["macd_sig"]  = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_sig"]

    return df
