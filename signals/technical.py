import pandas as pd
import ta


def compute(df: pd.DataFrame) -> dict:
    if df is None or len(df) < 20:
        return {}

    close = df["close"]

    rsi        = ta.momentum.RSIIndicator(close, window=14).rsi()
    macd_obj   = ta.trend.MACD(close, window_fast=12, window_slow=26, window_sign=9)
    macd_line  = macd_obj.macd()
    macd_sig   = macd_obj.macd_signal()
    sma50      = ta.trend.SMAIndicator(close, window=50).sma_indicator()
    sma200     = ta.trend.SMAIndicator(close, window=200).sma_indicator()
    bb         = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    bb_upper   = bb.bollinger_hband()
    bb_lower   = bb.bollinger_lband()

    price       = float(close.iloc[-1])
    rsi_val     = rsi.iloc[-1]
    macd_now    = macd_line.iloc[-1]
    macd_sig_now= macd_sig.iloc[-1]
    macd_prev   = macd_line.iloc[-2] if len(macd_line) > 1 else macd_now
    macd_sig_prev = macd_sig.iloc[-2] if len(macd_sig) > 1 else macd_sig_now
    sma50_val   = sma50.iloc[-1]
    sma200_val  = sma200.iloc[-1]
    bbu         = bb_upper.iloc[-1]
    bbl         = bb_lower.iloc[-1]

    macd_cross = None
    if pd.notna(macd_prev) and pd.notna(macd_sig_prev):
        if macd_prev < macd_sig_prev and macd_now > macd_sig_now:
            macd_cross = "bullish"
        elif macd_prev > macd_sig_prev and macd_now < macd_sig_now:
            macd_cross = "bearish"

    golden_cross = bool(pd.notna(sma50_val) and pd.notna(sma200_val) and sma50_val > sma200_val)
    death_cross  = bool(pd.notna(sma50_val) and pd.notna(sma200_val) and sma50_val < sma200_val)

    bb_position = None
    if pd.notna(bbu) and pd.notna(bbl):
        bb_position = "above_upper" if price > bbu else ("below_lower" if price < bbl else "inside")

    return {
        "rsi":          round(float(rsi_val), 2) if pd.notna(rsi_val) else None,
        "macd_cross":   macd_cross,
        "golden_cross": golden_cross,
        "death_cross":  death_cross,
        "bb_position":  bb_position,
        "price":        price,
        "sma50":        round(float(sma50_val), 2) if pd.notna(sma50_val) else None,
        "sma200":       round(float(sma200_val), 2) if pd.notna(sma200_val) else None,
    }
