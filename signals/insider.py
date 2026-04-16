import requests
from datetime import datetime, timedelta


_HEADERS = {"User-Agent": "trading-agent mohammed.a.kamil@gmail.com"}


def _get_cik(symbol: str) -> str | None:
    url = f"https://efts.sec.gov/LATEST/search-index?q=%22{symbol}%22&dateRange=custom&startdt=2020-01-01&forms=4"
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


def compute(symbol: str, days: int = 30) -> dict:
    cik = _get_cik(symbol)
    if not cik:
        return {"filings": [], "net_signal": "neutral"}

    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        r = requests.get(url, headers=_HEADERS, timeout=10)
        if r.status_code != 200:
            return {"filings": [], "net_signal": "neutral"}

        data = r.json()
        recent = data.get("filings", {}).get("recent", {})
        forms   = recent.get("form", [])
        dates   = recent.get("filingDate", [])
        cutoff  = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

        filings = []
        for form, date in zip(forms, dates):
            if form == "4" and date >= cutoff:
                filings.append({"form": form, "date": date})

        # Simple heuristic: more Form 4s recently = insider activity
        count = len(filings)
        net = "bullish" if count >= 3 else "neutral"

        return {"filings": filings, "count": count, "net_signal": net}

    except Exception:
        return {"filings": [], "net_signal": "neutral"}
