import pandas as pd
import pandas_ta as ta


def compute(df: pd.DataFrame) -> dict:
    if df is None or len(df) < 20:
        return {}

    close = df["close"]

    df.ta.rsi(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.sma(length=50, append=True)
    df.ta.sma(length=200, append=True)
    df.ta.bbands(length=20, append=True)

    latest = df.iloc[-1]
    prev   = df.iloc[-2] if len(df) > 1 else latest

    rsi = latest.get("RSI_14")
    macd_line = latest.get("MACD_12_26_9")
    macd_signal = latest.get("MACDs_12_26_9")
    sma50  = latest.get("SMA_50")
    sma200 = latest.get("SMA_200")
    bb_upper = latest.get("BBU_20_2.0")
    bb_lower = latest.get("BBL_20_2.0")
    price = float(close.iloc[-1])

    prev_macd  = prev.get("MACD_12_26_9")
    prev_macd_sig = prev.get("MACDs_12_26_9")

    # MACD crossover direction
    macd_cross = None
    if macd_line is not None and macd_signal is not None:
        if prev_macd is not None and prev_macd_sig is not None:
            if prev_macd < prev_macd_sig and macd_line > macd_signal:
                macd_cross = "bullish"
            elif prev_macd > prev_macd_sig and macd_line < macd_signal:
                macd_cross = "bearish"

    golden_cross = (sma50 and sma200 and sma50 > sma200)
    death_cross  = (sma50 and sma200 and sma50 < sma200)

    bb_position = None
    if bb_upper and bb_lower:
        bb_position = "above_upper" if price > bb_upper else ("below_lower" if price < bb_lower else "inside")

    return {
        "rsi":          round(rsi, 2) if rsi is not None else None,
        "macd_cross":   macd_cross,
        "golden_cross": golden_cross,
        "death_cross":  death_cross,
        "bb_position":  bb_position,
        "price":        price,
        "sma50":        round(sma50, 2) if sma50 else None,
        "sma200":       round(sma200, 2) if sma200 else None,
    }
