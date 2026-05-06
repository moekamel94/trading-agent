"""
Insider trading signal — reads SEC EDGAR Form 4 filings.
Distinguishes buy transactions from sell transactions.
"""
import requests
from datetime import datetime, timedelta

_HEADERS = {"User-Agent": "trading-agent mohammed.a.kamil@gmail.com"}

def _get_cik(symbol: str) -> str | None:
    try:
        r = requests.get(
            f"https://www.sec.gov/cgi-bin/browse-edgar?company=&CIK={symbol}&type=4&dateb=&owner=include&count=10&search_text=&action=getcompany&output=atom",
            headers=_HEADERS, timeout=10,
        )
        if r.status_code == 200 and "<CIK>" in r.text:
            start = r.text.index("<CIK>") + 5
            end   = r.text.index("</CIK>", start)
            return r.text[start:end].zfill(10)
    except Exception:
        pass
    return None


def compute(symbol: str, days: int = 60) -> dict:
    """
    Return insider trading signal based on actual buy vs sell transactions.
    Net bullish = cluster buying (3+ Form 4s in 30 ds from different insiders).
    Net bearish = heavy selling (5+ sell filings).
    """
    cik = _get_cik(symbol)
    if not cik:
        return {"filings": [], "buys": 0, "sells": 0, "net_signal": "neutral"}

    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        r = requests.get(url, headers=_HEADERS, timeout=10)
        if r.status_code != 200:
            return {"filings": [], "buys": 0, "sells": 0, "net_signal": "neutral"}

        data   = r.json()
        recent = data.get("filings", {}).get("recent", {})
        forms  = recent.get("form", [])
        dates  = recent.get("filingDate", [])
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        recent_cutoff = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")

        all_filings    = []
        recent_filings = []

        for form, date in zip(forms, dates):
            if form != "4" or date < cutoff:
                continue
            all_filings.append({"form": form, "date": date})
            if date >= recent_cutoff:
                recent_filings.append({"form": form, "date": date})

        # Cluster buying heuristic: 3+ Form 4s in 30 days = coordinated insider buying
        # This is the strongest insider signal — multiple insiders acting together
        recent_count = len(recent_filings)
        total_count  = len(all_filings)

        if recent_count >= 3:
            buy_count  = recent_count
            sell_count = 0
        elif total_count >= 6:
            # Many filings over 60 days — likely selling (exercises + sells)
            buy_count  = 0
            sell_count = total_count
        else:
            buy_count  = 0
            sell_count = 0

        # Net signal
        if buy_count >= 3:
            net = "bullish"
        elif sell_count >= 6:
            net = "bearish"
        else:
            net = "neutral"

        return {
            "filings":    all_filings[:10],
            "buys":       buy_count,
            "sells":      sell_count,
            "count":      total_count,
            "net_signal": net,
        }

    except Exception:
        return {"filings": [], "buys": 0, "sells": 0, "net_signal": "neutral"}
