"""
Basket manager — builds the watchlist from 3 sources:
  1. Full S&P 500 (all sectors, all 500 stocks)
  2. Nasdaq-100 / QQQ components
  3. Stocks congress members bought in the last 60 days (auto-added)
All three are merged and deduplicated. Refreshed weekly.
"""
import json
import os
import requests
from datetime import datetime

import pandas as pd
import yfinance as yf
from bs4 import BeautifulSoup

BASKET_FILE = os.path.join(os.path.dirname(__file__), "basket.json")
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _fetch_sp500() -> list[str]:
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        df = tables[0][["Symbol"]].copy()
        tickers = df["Symbol"].str.replace(".", "-", regex=False).tolist()
        print(f"  [Basket] S&P 500: {len(tickers)} tickers")
        return tickers
    except Exception as e:
        print(f"  [Basket] S&P 500 fetch failed: {e}")
        return []


def _fetch_qqq() -> list[str]:
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
        for t in tables:
            cols = [c.lower() for c in t.columns]
            if any("ticker" in c or "symbol" in c for c in cols):
                col = next(c for c in t.columns if "ticker" in c.lower() or "symbol" in c.lower())
                tickers = t[col].str.replace(".", "-", regex=False).dropna().tolist()
                print(f"  [Basket] Nasdaq-100 (QQQ): {len(tickers)} tickers")
                return tickers
        return []
    except Exception as e:
        print(f"  [Basket] QQQ fetch failed: {e}")
        return []


def _fetch_congress_buys() -> list[str]:
    """Scrape Capitol Trades for recent congress purchase transactions."""
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
                # Ticker is typically in col 3 or 4 — look for short uppercase strings
                for col in cols:
                    cleaned = col.strip().upper()
                    if 1 <= len(cleaned) <= 5 and cleaned.isalpha() and cleaned not in ("BUY", "SELL", "USD"):
                        tickers.add(cleaned)
        result = list(tickers)
        if result:
            print(f"  [Basket] Congress buys: {len(result)} tickers → {result[:10]}")
        return result
    except Exception as e:
        print(f"  [Basket] Congress buys fetch failed: {e}")
        return []


def refresh() -> list[str]:
    print("  [Basket] Refreshing watchlist from S&P 500 + QQQ + Congress buys...")

    sp500   = _fetch_sp500()
    qqq     = _fetch_qqq()
    cong    = _fetch_congress_buys()

    # Merge all three, deduplicate, keep order: S&P500 first, then QQQ-only, then congress-only
    seen   = set()
    merged = []
    for sym in sp500 + qqq + cong:
        s = sym.strip().upper()
        if s and s not in seen:
            seen.add(s)
            merged.append(s)

    if not merged:
        merged = _fallback()

    data = {
        "updated":  datetime.utcnow().isoformat(),
        "tickers":  merged,
        "sources":  {
            "sp500":    len(sp500),
            "qqq":      len(qqq),
            "congress": len(cong),
            "total":    len(merged),
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
        return data.get("tickers", [])
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
        "CRM","BAC","NFLX","AMD","KO","PEP","TMO","ORCL","CSCO","ACN",
        "MCD","WMT","DIS","INTU","IBM","GE","HON","QCOM","RTX","ADBE",
    ]
