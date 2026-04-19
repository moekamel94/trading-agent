"""
Basket manager — builds the watchlist from 5 sources:
  1. Full S&P 500 (all sectors, all 500 stocks)
  2. Nasdaq-100 / QQQ components
  3. Custom ETF holdings: QTUM, BOTT, SPWO (US-tradeable components only)
  4. Stocks congress members bought in the last 60 days (auto-added)
  5. Pinned tickers — always present (the ETFs themselves + any manual additions)
All sources are merged and deduplicated. Refreshed weekly.
"""
import json
import os
import re
import requests
from datetime import datetime

import pandas as pd
import yfinance as yf
from bs4 import BeautifulSoup

BASKET_FILE = os.path.join(os.path.dirname(__file__), "basket.json")
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# ETFs whose component stocks are added to the scan
_CUSTOM_ETFS = ["QTUM", "BOTT", "SPWO"]

# Always scanned regardless of index membership (includes the ETFs themselves)
_PINNED = ["QTUM", "BOTT", "SPWO"]


def _is_us_ticker(symbol: str) -> bool:
    """Filter out foreign-exchange tickers — keep only US-tradeable symbols."""
    return bool(re.match(r"^[A-Z]{1,6}$", symbol))


def _fetch_etf_holdings(etf: str) -> list[str]:
    """Fetch component stocks of an ETF via yfinance, keep US-tradeable only."""
    try:
        fd = yf.Ticker(etf).funds_data
        if fd is None:
            return []
        holdings_df = fd.top_holdings
        if holdings_df is None or holdings_df.empty:
            return []
        raw = holdings_df.index.tolist()
        us_only = [s for s in raw if _is_us_ticker(str(s))]
        print(f"  [Basket] {etf} holdings: {len(raw)} total, {len(us_only)} US-tradeable -> {us_only[:8]}")
        return us_only
    except Exception as e:
        print(f"  [Basket] {etf} holdings fetch failed: {e}")
        return []


def _fetch_sp500() -> list[str]:
    _wiki_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    for url in [
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "https://en.m.wikipedia.org/wiki/List_of_S%26P_500_companies",
    ]:
        try:
            tables = pd.read_html(url, storage_options={"User-Agent": _wiki_headers["User-Agent"]})
            df = tables[0][["Symbol"]].copy()
            tickers = df["Symbol"].str.replace(".", "-", regex=False).tolist()
            print(f"  [Basket] S&P 500: {len(tickers)} tickers")
            return tickers
        except Exception:
            continue
    print("  [Basket] S&P 500 fetch failed - using fallback")
    return []


def _fetch_qqq() -> list[str]:
    _wiki_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            storage_options={"User-Agent": _wiki_headers["User-Agent"]},
        )
        for t in tables:
            cols = [str(c).lower() for c in t.columns]
            if any("ticker" in c or "symbol" in c for c in cols):
                col = next(c for c in t.columns if "ticker" in str(c).lower() or "symbol" in str(c).lower())
                tickers = t[col].str.replace(".", "-", regex=False).dropna().tolist()
                print(f"  [Basket] Nasdaq-100 (QQQ): {len(tickers)} tickers")
                return tickers
        return []
    except Exception as e:
        print(f"  [Basket] QQQ fetch failed: {e}")
        return []


def _fetch_congress_buys() -> list[str]:
    try:
        resp = requests.get(
            "https://www.capitoltrades.com/trades?txType=purchase",
            headers=_HEADERS, timeout=15,
        )
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "lxml")
        tickers = set()
        for row in soup.select("table tbody tr")[:50]:
            cols = [c.get_text(strip=True) for c in row.find_all("td")]
            if len(cols) >= 4:
                for col in cols:
                    cleaned = col.strip().upper()
                    if _is_us_ticker(cleaned) and cleaned not in ("BUY", "SELL", "USD", "ETF"):
                        tickers.add(cleaned)
        result = list(tickers)
        if result:
            print(f"  [Basket] Congress buys: {len(result)} tickers -> {result[:10]}")
        return result
    except Exception as e:
        print(f"  [Basket] Congress buys fetch failed: {e}")
        return []


def refresh() -> list[str]:
    print("  [Basket] Refreshing: S&P 500 + QQQ + QTUM/BOTT/SPWO holdings + Congress buys...")

    sp500   = _fetch_sp500()
    qqq     = _fetch_qqq()
    cong    = _fetch_congress_buys()
    etf_holdings = []
    for etf in _CUSTOM_ETFS:
        etf_holdings += _fetch_etf_holdings(etf)

    # Merge all sources, pinned first, then deduplicate
    seen   = set()
    merged = []
    for sym in _PINNED + sp500 + qqq + etf_holdings + cong:
        s = sym.strip().upper()
        if s and s not in seen:
            seen.add(s)
            merged.append(s)

    if not merged:
        merged = _fallback()

    data = {
        "updated": datetime.utcnow().isoformat(),
        "tickers": merged,
        "sources": {
            "sp500":        len(sp500),
            "qqq":          len(qqq),
            "etf_holdings": len(etf_holdings),
            "congress":     len(cong),
            "pinned":       _PINNED,
            "total":        len(merged),
        },
    }
    with open(BASKET_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"  [Basket] Total watchlist: {len(merged)} tickers")
    return merged


def load() -> list[str]:
    if os.path.exists(BASKET_FILE):
        with open(BASKET_FILE) as f:
            data = json.load(f)
        tickers = data.get("tickers", [])
        for sym in _PINNED:
            if sym not in tickers:
                tickers.insert(0, sym)
        return tickers
    return refresh()


def needs_refresh() -> bool:
    if not os.path.exists(BASKET_FILE):
        return True
    with open(BASKET_FILE) as f:
        data = json.load(f)
    updated  = datetime.fromisoformat(data.get("updated", "2000-01-01"))
    days_old = (datetime.utcnow() - updated).days
    return days_old >= 7


def _fallback() -> list[str]:
    return [
        "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","JPM","V","UNH",
        "XOM","LLY","AVGO","MA","PG","JNJ","HD","MRK","ABBV","CVX",
        "QTUM","BOTT","SPWO",
    ]
