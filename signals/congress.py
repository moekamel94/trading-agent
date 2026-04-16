import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta


_BASE = "https://www.capitoltrades.com/trades"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def compute(symbol: str, days: int = 60) -> dict:
    try:
        resp = requests.get(f"{_BASE}?asset={symbol}", headers=_HEADERS, timeout=10)
        if resp.status_code != 200:
            return {"trades": [], "net_signal": "neutral"}

        soup = BeautifulSoup(resp.text, "lxml")
        rows = soup.select("table tbody tr")
        cutoff = datetime.utcnow() - timedelta(days=days)

        trades = []
        for row in rows[:20]:
            cols = [c.get_text(strip=True) for c in row.find_all("td")]
            if len(cols) < 6:
                continue
            try:
                trade_date = datetime.strptime(cols[2], "%Y-%m-%d")
            except ValueError:
                continue
            if trade_date < cutoff:
                continue
            trades.append({
                "politician": cols[0],
                "party":      cols[1],
                "date":       cols[2],
                "action":     cols[4],
                "amount":     cols[5],
            })

        buys  = sum(1 for t in trades if "buy"      in t["action"].lower())
        sells = sum(1 for t in trades if "sell"     in t["action"].lower())

        if buys > sells * 1.5:
            net = "bullish"
        elif sells > buys * 1.5:
            net = "bearish"
        else:
            net = "neutral"

        return {"trades": trades, "buys": buys, "sells": sells, "net_signal": net}

    except Exception:
        return {"trades": [], "net_signal": "neutral"}
