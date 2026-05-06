"""
Simple backtesting module — validates signal combinations against 2 years of historical data.

Usage:
    from learning.backtest import run_backtest
    results = run_backtest(
        tickers=["NVDA", "MSFT"],
        rsi_min=40, rsi_max=70,
        growth_score_min=50,
        sentiment_required="bullish"
    )

Runs automatically after monthly deep dive and sends results to Discord.
"""
import os
from datetime import date, timedelta
from notifications import discord_bot as discord


def run_backtest(
    tickers: list[str] = None,
    rsi_min: float = 40,
    rsi_max: float = 70,
    growth_score_min: float = 50,
    sentiment_required: str = None,  # "bullish", "neutral", "bearish", or None
    macd_required: str = None,        # "bullish", "bearish", or None
    lookback_years: int = 2,
    forward_days: int = 30,
) -> dict:
    """
    Backtest a signal combination against historical data.

    Returns:
        {win_rate, avg_return, max_drawdown, sharpe, alpha_vs_spy, signals_found,
         results: [{symbol, date, entry_price, exit_price, return_pct}]}
    """
    try:
        import yfinance as yf
        import numpy as np
    except ImportError:
        return {"error": "yfinance or numpy not installed"}

    if tickers is None:
        try:
            import config
            tickers = list(config.TICKER_TIERS.keys())
        except Exception:
            tickers = ["NVDA", "MSFT", "GOOGL"]

    start_date = (date.today() - timedelta(days=lookback_years * 365)).isoformat()
    end_date = (date.today() - timedelta(days=forward_days)).isoformat()

    all_results = []
    spy_returns = []

    # Get SPY history for alpha calculation
    try:
        spy_hist = yf.Ticker("SPY").history(start=start_date)
        spy_prices = spy_hist["Close"] if not spy_hist.empty else None
    except Exception:
        spy_prices = None

    for sym in tickers:
        try:
            hist = yf.Ticker(sym).history(start=start_date)
            if hist.empty or len(hist) < 50:
                continue

            closes = hist["Close"]

            # Calculate rolling RSI (14-period)
            delta = closes.diff()
            gain = delta.clip(lower=0)
            loss = (-delta).clip(lower=0)
            avg_gain = gain.rolling(14).mean()
            avg_loss = loss.rolling(14).mean()
            rs = avg_gain / avg_loss.replace(0, float('nan'))
            rsi_series = 100 - (100 / (1 + rs))

            # Calculate SMA20
            sma20 = closes.rolling(20).mean()

            # MACD (12, 26, 9)
            ema12 = closes.ewm(span=12).mean()
            ema26 = closes.ewm(span=26).mean()
            macd_line = ema12 - ema26
            signal_line = macd_line.ewm(span=9).mean()

            # Scan for entry signals
            for i in range(50, len(closes) - forward_days):
                dt = closes.index[i]

                # Skip if date too recent for forward return
                if dt.date() > date.fromisoformat(end_date):
                    break

                rsi_val = float(rsi_series.iloc[i])
                price = float(closes.iloc[i])
                sma20_val = float(sma20.iloc[i])
                macd_val = float(macd_line.iloc[i])
                sig_val = float(signal_line.iloc[i])

                # Apply filters
                if not (rsi_min <= rsi_val <= rsi_max):
                    continue
                if price < sma20_val * 0.97:  # must be near/above SMA20
                    continue
                if macd_required == "bullish" and macd_val <= sig_val:
                    continue
                if macd_required == "bearish" and macd_val >= sig_val:
                    continue

                # Calculate forward return
                exit_price = float(closes.iloc[i + forward_days])
                ret_pct = (exit_price / price - 1) * 100

                # SPY return over same period
                spy_ret = None
                if spy_prices is not None:
                    try:
                        spy_entry = float(spy_prices[spy_prices.index >= dt].iloc[0])
                        spy_exit_idx = spy_prices[spy_prices.index >= dt].index[forward_days] if len(spy_prices[spy_prices.index >= dt]) > forward_days else None
                        if spy_exit_idx is not None:
                            spy_exit = float(spy_prices[spy_exit_idx])
                            spy_ret = (spy_exit / spy_entry - 1) * 100
                            spy_returns.append(spy_ret)
                    except Exception:
                        pass

                all_results.append({
                    "symbol": sym,
                    "date": dt.strftime("%Y-%m-%d"),
                    "entry_price": round(price, 2),
                    "exit_price": round(exit_price, 2),
                    "return_pct": round(ret_pct, 2),
                    "rsi_at_entry": round(rsi_val, 1),
                    "spy_return": round(spy_ret, 2) if spy_ret else None,
                })
        except Exception as e:
            print(f"[Backtest] {sym}: {e}")
            continue

    if not all_results:
        return {"error": "No signals found matching criteria", "signals_found": 0}

    returns_arr = [r["return_pct"] for r in all_results]
    wins = sum(1 for r in returns_arr if r > 0)
    avg_ret = sum(returns_arr) / len(returns_arr)

    # Sharpe ratio (annualized, assuming daily returns)
    import math
    if len(returns_arr) > 1:
        std = (sum((r - avg_ret) ** 2 for r in returns_arr) / len(returns_arr)) ** 0.5
        sharpe = (avg_ret / std * math.sqrt(252 / forward_days)) if std > 0 else 0
    else:
        sharpe = 0

    # Max drawdown (on the list of returns in sequence)
    peak = returns_arr[0]
    max_dd = 0
    for r in returns_arr:
        peak = max(peak, r)
        dd = peak - r
        max_dd = max(max_dd, dd)

    avg_spy = sum(spy_returns) / len(spy_returns) if spy_returns else None
    alpha = round(avg_ret - avg_spy, 2) if avg_spy else None

    return {
        "win_rate":       round(wins / len(returns_arr) * 100, 1),
        "avg_return":     round(avg_ret, 2),
        "max_drawdown":   round(max_dd, 2),
        "sharpe":         round(sharpe, 2),
        "alpha_vs_spy":   alpha,
        "avg_spy_return": round(avg_spy, 2) if avg_spy else None,
        "signals_found":  len(all_results),
        "results":        all_results[:20],  # top 20 for Discord
    }


def run_and_report(label: str = "default", **kwargs) -> None:
    """Run backtest and send Discord report."""
    print(f"[Backtest] Running: {label}")
    results = run_backtest(**kwargs)

    if "error" in results:
        discord.send(f"📈 BACKTEST {label}: {results['error']}")
        return

    spy_str = f" vs SPY {results['avg_spy_return']:+.1f}%" if results.get("avg_spy_return") else ""
    alpha_str = f" | Alpha: {results['alpha_vs_spy']:+.1f}%" if results.get("alpha_vs_spy") else ""

    msg = (
        f"📈 BACKTEST RESULTS: Signal [{label}]\n"
        f"  Win rate: {results['win_rate']:.0f}% | "
        f"Avg return: {results['avg_return']:+.1f}%{spy_str}{alpha_str}\n"
        f"  Sharpe: {results['sharpe']:.2f} | "
        f"Max drawdown: {results['max_drawdown']:.1f}% | "
        f"Signals found: {results['signals_found']}"
    )
    discord.send(msg)
    print(f"[Backtest] Done: {msg}")
