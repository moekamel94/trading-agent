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

# Always scanned regardless of anything else (empty — no legacy ETF pins needed)
_PINNED = []

# Focused sector watchlist — ~80 high-conviction tickers aligned with the thesis
SECTOR_LIST = [
    # AI / Software / Cloud
    "MSFT", "GOOGL", "META", "AMZN", "ORCL", "PLTR", "CRM", "NOW", "AI",
    # Semiconductors (removed INTC, QCOM — declining thesis alignment)
    "NVDA", "AMD", "AVGO", "AMAT", "LRCX", "KLAC", "MU", "ARM", "MRVL", "TSM",
    "SNPS", "KEYS",
    # Quantum Computing (removed QUBT — too early, no revenue path)
    "IONQ", "RGTI", "IBM",
    # Cybersecurity (removed OKTA — growth concerns, CRWD/PANW dominate)
    "CRWD", "PANW", "ZS", "FTNT", "S",
    # Space Tech
    "RKLB", "ASTS",
    # Nuclear Energy
    "CCJ", "OKLO", "SMR", "CEG",
    # Defense Tech
    "LMT", "RTX", "NOC", "GD", "KTOS", "AXON", "BWXT", "GE", "ATI", "CACI",
    # Clean Energy (removed NEE, RUN, SEDG, ENPH — low growth / broken thesis)
    "FSLR",
    # Robotics / Automation
    "ETN", "ISRG", "SYM", "TRMB",
    # AI Networking / Infrastructure + Data
    "ANET", "VRT", "PWR", "PSTG", "SNOW", "APH",
    # Fintech & financial infrastructure
    "HOOD", "COIN", "MELI", "NU", "MA", "MSCI",
    # Consumer tech & e-commerce
    "DUOL", "RDDT", "SHOP", "UBER",
    "CELH", "CAVA",
    # Healthcare / Biotech AI
    "RXRX", "LLY", "DXCM", "VEEV",
    # International high-growth
    "SE", "GRAB",
    # eVTOL / air mobility
    "JOBY",
    # Energy — Oil & Gas
    "FANG", "COP",
    # Energy — Midstream
    "WMB",
    # Commodities & Metals (AI/EV-linked)
    "FCX", "RGLD", "MP",
    # Anchor mega-caps
    "AAPL", "TSLA",
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
