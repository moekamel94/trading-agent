import pandas as pd
import ta


def compute(df: pd.DataFrame) -> dict:
    if df is None or len(df) < 20:
        return {}

    close  = df["close"]
    volume = df["volume"] if "volume" in df.columns else None

    # ── Trend indicators ─────────────────────────────────────────────────────
    rsi       = ta.momentum.RSIIndicator(close, window=14).rsi()
    macd_obj  = ta.trend.MACD(close, window_fast=12, window_slow=26, window_sign=9)
    macd_line = macd_obj.macd()
    macd_sig  = macd_obj.macd_signal()
    sma50     = ta.trend.SMAIndicator(close, window=50).sma_indicator()
    sma200    = ta.trend.SMAIndicator(close, window=200).sma_indicator()
    bb        = ta.volatility.BollingerBands(close, window=20, window_dev=2)

    price         = float(close.iloc[-1])
    rsi_val       = rsi.iloc[-1]
    macd_now      = macd_line.iloc[-1]
    macd_sig_now  = macd_sig.iloc[-1]
    macd_prev     = macd_line.iloc[-2] if len(macd_line) > 1 else macd_now
    macd_sig_prev = macd_sig.iloc[-2]  if len(macd_sig)  > 1 else macd_sig_now
    sma50_val     = sma50.iloc[-1]
    sma200_val    = sma200.iloc[-1]
    bbu           = bb.bollinger_hband().iloc[-1]
    bbl           = bb.bollinger_lband().iloc[-1]

    macd_cross = None
    if pd.notna(macd_prev) and pd.notna(macd_sig_prev):
        if macd_prev < macd_sig_prev and macd_now > macd_sig_now:
            macd_cross = "bullish"
        elif macd_prev > macd_sig_prev and macd_now < macd_sig_now:
            macd_cross = "bearish"

    # MACD histogram acceleration: histogram growing above zero = bullish momentum building
    macd_hist_now  = (macd_now  - macd_sig_now)  if pd.notna(macd_now)  and pd.notna(macd_sig_now)  else None
    macd_hist_prev = (macd_prev - macd_sig_prev) if pd.notna(macd_prev) and pd.notna(macd_sig_prev) else None
    macd_hist_accel = bool(
        macd_hist_now is not None and macd_hist_prev is not None
        and macd_hist_now > 0 and macd_hist_now > macd_hist_prev
    )

    golden_cross = bool(pd.notna(sma50_val) and pd.notna(sma200_val) and sma50_val > sma200_val)
    death_cross  = bool(pd.notna(sma50_val) and pd.notna(sma200_val) and sma50_val < sma200_val)

    # How many trading days ago did the death cross form?
    # Walk backwards to find the last bar where SMA50 was still >= SMA200.
    # Returns None if no death cross is active. Capped at 252 (treat anything older as >1yr).
    death_cross_days = None
    if death_cross:
        lookback = min(len(sma50), len(sma200), 252)
        for i in range(1, lookback):
            s50 = sma50.iloc[-(i + 1)]
            s200 = sma200.iloc[-(i + 1)]
            if pd.notna(s50) and pd.notna(s200) and s50 >= s200:
                death_cross_days = i
                break
        if death_cross_days is None:
            death_cross_days = 252  # cross formed more than a year ago

    bb_position = None
    if pd.notna(bbu) and pd.notna(bbl):
        bb_position = "above_upper" if price > bbu else ("below_lower" if price < bbl else "inside")

    # ── Momentum signals ──────────────────────────────────────────────────────
    n = len(close)

    return_1m = None
    if n >= 21:
        p_21 = float(close.iloc[-21])
        if p_21 > 0:
            return_1m = round((price - p_21) / p_21 * 100, 2)

    return_3m = None
    if n >= 63:
        p_63 = float(close.iloc[-63])
        if p_63 > 0:
            return_3m = round((price - p_63) / p_63 * 100, 2)

    # Volume ratio: avg last 5 days vs avg last 20 days
    volume_ratio = None
    if volume is not None and n >= 20:
        avg_5  = float(volume.iloc[-5:].mean())
        avg_20 = float(volume.iloc[-20:].mean())
        if avg_20 > 0:
            volume_ratio = round(avg_5 / avg_20, 2)

    # Rate of change (14-day)
    roc_14 = None
    if n >= 14:
        p_14 = float(close.iloc[-14])
        if p_14 > 0:
            roc_14 = round((price - p_14) / p_14 * 100, 2)

    # 30-day average daily dollar volume (ADV) — used for liquidity / position-size cap
    adv_30d = None
    if volume is not None and n >= 30:
        avg_vol_30 = float(volume.iloc[-30:].mean())
        adv_30d = round(avg_vol_30 * price, 0)

    # ATR(20) — Average True Range, used for volatility-adjusted stop placement
    atr = None
    if "high" in df.columns and "low" in df.columns and n >= 20:
        try:
            atr_series = ta.volatility.AverageTrueRange(
                df["high"], df["low"], close, window=20
            ).average_true_range()
            atr_val = atr_series.iloc[-1]
            atr = round(float(atr_val), 4) if pd.notna(atr_val) else None
        except Exception:
            atr = None

    return {
        "rsi":              round(float(rsi_val), 2) if pd.notna(rsi_val) else None,
        "macd_cross":       macd_cross,
        "macd_hist_accel":   macd_hist_accel,
        "golden_cross":      golden_cross,
        "death_cross":       death_cross,
        "death_cross_days":  death_cross_days,
        "bb_position":  bb_position,
        "price":        price,
        "sma50":        round(float(sma50_val),  2) if pd.notna(sma50_val)  else None,
        "sma200":       round(float(sma200_val), 2) if pd.notna(sma200_val) else None,
        # momentum
        "return_1m":    return_1m,
        "return_3m":    return_3m,
        "volume_ratio": volume_ratio,
        "roc_14":       roc_14,
        "adv_30d":      adv_30d,
        "atr":          atr,
    }
