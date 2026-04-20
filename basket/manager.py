"""
Basket manager — focused ~80-ticker list covering Kimmy's thesis sectors,
plus congress buys auto-added dynamically.

Sectors: AI/Software, Semiconductors, Quantum, Cybersecurity, Biotech,
         Defense Tech, Clean Energy, Robotics/Automation, Mega-caps
"""
import json
import os
import re
import requests
from datetime import datetime

from bs4 import BeautifulSoup

BASKET_FILE = os.path.join(os.path.dirname(__file__), "basket.json")
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Always scanned regardless of anything else
_PINNED = ["QTUM", "BOTT", "SPWO"]

# Focused sector watchlist — ~80 high-conviction tickers aligned with the thesis
SECTOR_LIST = [
    # AI / Software / Cloud
    "MSFT", "GOOGL", "META", "AMZN", "ORCL", "PLTR", "SNOW", "CRM", "NOW", "AI",
    # Semiconductors
    "NVDA", "AMD", "AVGO", "QCOM", "AMAT", "LRCX", "KLAC", "MU", "ARM", "MRVL", "TSM", "INTC",
    # Quantum Computing
    "IONQ", "RGTI", "QUBT", "IBM",
    # Cybersecurity
    "CRWD", "PANW", "ZS", "FTNT", "S", "OKTA", "CYBR",
    # Space Tech (early-innings, massive 10-year TAM)
    "RKLB", "ASTS", "LUNR", "RDW",
    # Nuclear Energy (AI power demand + SMR deployment this decade)
    "CCJ", "OKLO", "SMR", "CEG",
    # Defense Tech
    "LMT", "RTX", "NOC", "GD", "KTOS", "AXON", "BWXT",
    # Clean Energy
    "ENPH", "FSLR", "NEE", "RUN", "SEDG",
    # Robotics / Automation (includes surgical robotics)
    "ROK", "EMR", "ABB", "ETN", "ISRG",
    # Mega-caps / always relevant
    "AAPL", "TSLA", "JPM", "V", "MA", "BRK-B",
]


def _is_us_ticker(symbol: str) -> bool:
    return bool(re.match(r"^[A-Z]{1,6}$", symbol))


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
    print("  [Basket] Building focused sector basket + congress buys...")

    cong = _fetch_congress_buys()

    seen   = set()
    merged = []
    for sym in _PINNED + SECTOR_LIST + cong:
        s = sym.strip().upper()
        if s and s not in seen:
            seen.add(s)
            merged.append(s)

    data = {
        "updated": datetime.utcnow().isoformat(),
        "tickers": merged,
        "sources": {
            "sector_list": len(SECTOR_LIST),
            "congress":    len(cong),
            "pinned":      _PINNED,
            "total":       len(merged),
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
    return days_old >= 30


def _fallback() -> list[str]:
    return _PINNED + SECTOR_LIST
