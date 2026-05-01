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


def _credit_spreads() -> dict:
    """
    Credit spread proxy using HYG (high-yield) and LQD (investment-grade) vs TLT.
    Widening spreads (HYG/TLT falling, LQD/TLT falling) precede equity stress by 1-2 weeks.
    Returns spread level and 5-day direction as a leading risk-off indicator.
    """
    try:
        tickers = yf.download(["HYG", "LQD", "TLT"], period="30d", progress=False, auto_adjust=True)
        closes  = tickers["Close"]
        if closes.empty or len(closes) < 6:
            return {}

        def _ratio_and_trend(num: str, den: str):
            if num not in closes.columns or den not in closes.columns:
                return None, None
            ratio = closes[num] / closes[den]
            ratio = ratio.dropna()
            if len(ratio) < 6:
                return None, None
            current = float(ratio.iloc[-1])
            prior5  = float(ratio.iloc[-6])
            pct_chg = round((current / prior5 - 1) * 100, 2)
            direction = "widening" if pct_chg < -0.5 else ("tightening" if pct_chg > 0.5 else "stable")
            return round(current, 4), direction

        hyg_ratio, hyg_dir = _ratio_and_trend("HYG", "TLT")
        lqd_ratio, lqd_dir = _ratio_and_trend("LQD", "TLT")

        risk_signal = "neutral"
        if hyg_dir == "widening" and lqd_dir == "widening":
            risk_signal = "stress"
        elif hyg_dir == "widening":
            risk_signal = "elevated"
        elif hyg_dir == "tightening" and lqd_dir == "tightening":
            risk_signal = "risk_on"

        return {
            "hyg_tlt_ratio":   hyg_ratio,
            "hyg_tlt_5d":      hyg_dir,
            "lqd_tlt_ratio":   lqd_ratio,
            "lqd_tlt_5d":      lqd_dir,
            "credit_signal":   risk_signal,
        }
    except Exception:
        return {}


def compute() -> dict:
    """Run all market-wide signals in parallel. Call once at cycle start."""
    from signals.momentum_news import global_macro_momentum

    with ThreadPoolExecutor(max_workers=5) as ex:
        ft_fg      = ex.submit(_fear_and_greed)
        ft_vix     = ex.submit(_vix)
        ft_econ    = ex.submit(_economic_calendar)
        ft_macro   = ex.submit(global_macro_momentum)
        ft_credit  = ex.submit(_credit_spreads)

    fg     = ft_fg.result()
    vix    = ft_vix.result()
    econ   = ft_econ.result()
    macro  = ft_macro.result()
    credit = ft_credit.result()

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

    if macro.get("available"):
        print(f"  Geopolitical: {macro.get('label','?')} (score={macro.get('score','?')}) | themes={macro.get('themes',[])} | headline={str(macro.get('top_headlines', ['']))[:80]}")

    if credit.get("credit_signal") == "stress":
        print(f"  ⚠️ Credit spreads WIDENING — HYG/TLT {credit.get('hyg_tlt_5d')} | "
              f"LQD/TLT {credit.get('lqd_tlt_5d')} — leading risk-off signal")

    return {
        "fear_and_greed":        fg,
        "vix":                   vix,
        "upcoming_macro_events": econ,
        "market_risk":           risk,
        "macro_momentum":        macro,
        "credit_spreads":        credit,
    }
