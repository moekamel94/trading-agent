"""
Market-wide context signals — computed ONCE per cycle, not per ticker.
- CNN Fear & Greed Index
- CBOE Total Put/Call Ratio
- Finnhub upcoming earnings (per ticker, called separately)
- Finnhub economic calendar (macro events next 7 days)
"""
import requests
import yfinance as yf
import config
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed",
}
_TIMEOUT = 10


def _fear_and_greed() -> dict:
    try:
        r = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return {}
        fg = r.json().get("fear_and_greed", {})
        score = fg.get("score")
        return {
            "score":          round(score, 1) if score else None,
            "label":          fg.get("rating", ""),
            "previous_close": round(fg.get("previous_close", 0), 1),
            "previous_1_week": round(fg.get("previous_1_week", 0), 1),
        }
    except Exception:
        return {}


def _vix() -> dict:
    """VIX index via yfinance — reliable market fear proxy."""
    try:
        info = yf.Ticker("^VIX").fast_info
        vix = round(info.last_price, 2)
        label = "extreme_fear" if vix > 35 else (
                "fear"         if vix > 25 else (
                "elevated"     if vix > 18 else (
                "low"          if vix < 12 else "normal")))
        return {"vix": vix, "label": label}
    except Exception:
        return {}


def _economic_calendar() -> list[dict]:
    if not config.FINNHUB_API_KEY:
        return []
    today     = date.today().isoformat()
    next_week = (date.today() + timedelta(days=7)).isoformat()
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/economic",
            params={"from": today, "to": next_week, "token": config.FINNHUB_API_KEY},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return []
        events = r.json().get("economicCalendar", [])
        return [
            {"event": e.get("event"), "date": (e.get("time") or "")[:10]}
            for e in events
            if str(e.get("impact")) in ("high", "1") and e.get("country") == "US"
        ][:5]
    except Exception:
        return []


def earnings_soon(symbol: str) -> dict:
    """Check if earnings are within the next 14 days for this ticker."""
    if not config.FINNHUB_API_KEY:
        return {}
    today    = date.today().isoformat()
    in_2wks  = (date.today() + timedelta(days=14)).isoformat()
    clean    = symbol.split("/")[0] if "/" in symbol else symbol
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": today, "to": in_2wks, "symbol": clean, "token": config.FINNHUB_API_KEY},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return {}
        items = r.json().get("earningsCalendar", [])
        if not items:
            return {"earnings_soon": False}
        e = items[0]
        earnings_date = e.get("date")
        try:
            from datetime import date as _date
            days_to = (_date.fromisoformat(earnings_date) - _date.today()).days
        except Exception:
            days_to = None
        return {
            "earnings_soon":    True,
            "earnings_date":    earnings_date,
            "days_to_earnings": days_to,
            "eps_estimate":     e.get("epsEstimate"),
            "revenue_estimate": e.get("revenueEstimate"),
        }
    except Exception:
        return {}


def compute() -> dict:
    """Run all market-wide signals in parallel. Call once at cycle start."""
    with ThreadPoolExecutor(max_workers=3) as ex:
        ft_fg   = ex.submit(_fear_and_greed)
        ft_vix  = ex.submit(_vix)
        ft_econ = ex.submit(_economic_calendar)

    fg   = ft_fg.result()
    vix  = ft_vix.result()
    econ = ft_econ.result()

    score = fg.get("score")
    if score is not None:
        if score < 25:   risk = "extreme_fear"
        elif score < 45: risk = "fear"
        elif score > 75: risk = "extreme_greed"
        elif score > 55: risk = "greed"
        else:            risk = "neutral"
    elif vix.get("vix", 0) > 30:
        risk = "fear"
    else:
        risk = "unknown"

    return {
        "fear_and_greed":        fg,
        "vix":                   vix,
        "upcoming_macro_events": econ,
        "market_risk":           risk,
    }
