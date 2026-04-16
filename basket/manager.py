"""
Maintains a curated basket of S&P 500 stocks from Technology, Energy, and Materials sectors.
Refreshed every Monday at 8:00 AM ET. Stored in basket/basket.json for persistence.
Top 25 by market cap per sector run — keeps API costs low and focuses on liquid, large-cap names.
"""
import json
import os
from datetime import datetime

import pandas as pd
import yfinance as yf

BASKET_FILE = os.path.join(os.path.dirname(__file__), "basket.json")
TARGET_SECTORS = {"Information Technology", "Energy", "Materials"}
TOP_N = 25  # total tickers across all three sectors


def _fetch_sp500_by_sector() -> list[str]:
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        df = tables[0][["Symbol", "GICS Sector"]].copy()
        df["Symbol"] = df["Symbol"].str.replace(".", "-", regex=False)
        df = df[df["GICS Sector"].isin(TARGET_SECTORS)]
        tickers = df["Symbol"].tolist()
        return tickers
    except Exception as e:
        print(f"  [Basket] Wikipedia fetch failed: {e}")
        return _fallback_tickers()


def _fallback_tickers() -> list[str]:
    return [
        # Technology
        "AAPL", "MSFT", "NVDA", "AVGO", "AMD", "ORCL", "CRM", "ADBE", "QCOM",
        "TXN", "INTC", "INTU", "CSCO", "IBM", "NOW", "AMAT", "MU", "LRCX",
        # Energy
        "XOM", "CVX", "COP", "EOG", "SLB", "MPC", "PSX", "VLO", "OXY", "PXD",
        # Materials
        "LIN", "APD", "ECL", "SHW", "FCX", "NEM", "DOW", "DD",
    ]


def _rank_by_market_cap(tickers: list[str], top_n: int) -> list[str]:
    caps = {}
    for sym in tickers:
        try:
            info = yf.Ticker(sym).fast_info
            mc = getattr(info, "market_cap", None)
            if mc:
                caps[sym] = mc
        except Exception:
            pass
    ranked = sorted(caps, key=lambda s: caps[s], reverse=True)
    return ranked[:top_n]


def refresh() -> list[str]:
    print("  [Basket] Refreshing watchlist from S&P 500 Tech/Energy/Materials...")
    candidates = _fetch_sp500_by_sector()
    basket = _rank_by_market_cap(candidates, TOP_N)

    # Always include BTC for crypto
    if not basket:
        basket = _fallback_tickers()[:TOP_N]

    data = {
        "updated": datetime.utcnow().isoformat(),
        "tickers": basket,
    }
    with open(BASKET_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"  [Basket] Updated: {len(basket)} stocks -> {basket}")
    return basket


def load() -> list[str]:
    if os.path.exists(BASKET_FILE):
        with open(BASKET_FILE) as f:
            data = json.load(f)
        return data.get("tickers", [])
    # No basket file yet — build it now
    return refresh()


def needs_refresh() -> bool:
    if not os.path.exists(BASKET_FILE):
        return True
    with open(BASKET_FILE) as f:
        data = json.load(f)
    updated = datetime.fromisoformat(data.get("updated", "2000-01-01"))
    days_old = (datetime.utcnow() - updated).days
    return days_old >= 7
