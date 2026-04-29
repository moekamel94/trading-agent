"""
Macro regime signal — runs once per day, cached to .macro_regime.json.
Sources: FRED (free, no key) + yfinance (free) + FMP economic calendar.

Outputs a compact regime dict the committee uses to pre-position ahead of
macro catalysts rather than reacting after the fact.
"""

import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone

import requests

_CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", ".macro_regime.json")
_CACHE_PATH = os.path.normpath(_CACHE_PATH)
_TIMEOUT = 12

# FRED series (free CSV, no API key required)
_FRED_SERIES = {
    "CPIAUCSL": "cpi",
    "PCEPI":    "pce",
    "FEDFUNDS": "fed_rate",
    "DGS10":    "yield_10y",
    "DGS2":     "yield_2y",
    "T10Y2Y":   "yield_spread",      # 10Y-2Y (positive = normal, negative = inverted)
    "UNRATE":   "unemployment",
    "PAYEMS":   "nfp_thousands",
    "ICSA":     "jobless_claims",
    "UMCSENT":  "consumer_sentiment",
    "INDPRO":   "industrial_production",
    "MANEMP":   "mfg_employment_thousands",
}

# yfinance symbols for real-time market regime signals (not in FRED at daily freq)
_YF_SYMBOLS = {
    "^TNX":    "yield_10y_live",    # 10Y treasury yield (live)
    "^IRX":    "yield_3m_live",     # 3-month T-bill (live)
    "DX-Y.NYB":"dxy",               # US dollar index
    "GC=F":    "gold_price",        # Gold futures
    "CL=F":    "oil_price",         # WTI crude
    "^VIX":    "vix",               # VIX
}


def _fred_series(series_id: str) -> dict:
    """Fetch latest value + prior value for a FRED series via free CSV."""
    try:
        r = requests.get(
            f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return {}
        rows = list(csv.reader(io.StringIO(r.text)))[1:]  # skip header
        rows = [(d, v) for d, v in rows if v and v != "."]
        if not rows:
            return {}
        latest_date, latest_val = rows[-1]
        prev_val = rows[-2][1] if len(rows) >= 2 else latest_val
        return {
            "value":      float(latest_val),
            "prev":       float(prev_val),
            "date":       latest_date,
            "change":     float(latest_val) - float(prev_val),
            "pct_change": round((float(latest_val) - float(prev_val)) / abs(float(prev_val)) * 100, 3)
                          if float(prev_val) != 0 else 0,
        }
    except Exception:
        return {}


def _yf_prices() -> dict:
    """Fetch live prices for market regime proxies via yfinance."""
    result = {}
    try:
        import yfinance as yf
        for sym, key in _YF_SYMBOLS.items():
            try:
                info = yf.Ticker(sym).fast_info
                price = getattr(info, "last_price", None)
                if price:
                    result[key] = round(float(price), 3)
            except Exception:
                pass
    except ImportError:
        pass
    return result


def _fmp_economic_calendar() -> list[dict]:
    """
    Fetch upcoming high-impact economic events (next 14 days) via FMP.
    Returns list of {date, event, impact, country} sorted by date.
    """
    try:
        import config
        if not config.FMP_API_KEY:
            return []
        today = datetime.now(timezone.utc).date()
        end   = today + timedelta(days=14)
        r = requests.get(
            f"https://financialmodelingprep.com/api/v3/economic_calendar"
            f"?from={today}&to={end}&apikey={config.FMP_API_KEY}",
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return []
        events = r.json() if isinstance(r.json(), list) else []
        # Filter to high-impact US events
        high_impact = [
            {
                "date":    e.get("date", "")[:10],
                "event":   e.get("event", ""),
                "impact":  e.get("impact", ""),
                "actual":  e.get("actual"),
                "estimate":e.get("estimate"),
            }
            for e in events
            if e.get("country") == "US" and e.get("impact") in ("High", "Medium")
        ]
        return sorted(high_impact, key=lambda x: x["date"])[:20]
    except Exception:
        return []


def _derive_regime(data: dict) -> dict:
    """
    Derive a compact qualitative regime from the raw macro data.
    Returns signals the committee can act on immediately.
    """
    regime = {}

    # Inflation trend
    cpi = data.get("cpi", {})
    pce = data.get("pce", {})
    cpi_chg = cpi.get("pct_change", 0)
    regime["inflation_trend"] = (
        "rising" if cpi_chg > 0.1 else
        "falling" if cpi_chg < -0.1 else
        "stable"
    )
    regime["cpi_yoy_est"] = round(cpi.get("value", 0) / 100 * 3.0, 1) if cpi.get("value") else None  # rough proxy

    # Yield curve
    spread = data.get("yield_spread", {}).get("value")
    yield_10y = data.get("yield_10y_live") or data.get("yield_10y", {}).get("value")
    yield_2y  = data.get("yield_2y", {}).get("value")
    if spread is not None:
        regime["yield_curve"] = "inverted" if spread < 0 else "normal" if spread > 0.3 else "flat"
        regime["yield_spread_bps"] = round(spread * 100, 0)
    if yield_10y:
        regime["yield_10y"] = round(yield_10y, 2)
    if yield_2y:
        regime["yield_2y"] = round(yield_2y, 2)

    # Labor market
    unrate = data.get("unemployment", {}).get("value")
    claims = data.get("jobless_claims", {})
    claims_val  = claims.get("value", 0)
    claims_prev = claims.get("prev", claims_val)
    if unrate:
        regime["labor"] = "strong" if unrate < 4.5 else "weakening" if unrate > 5.0 else "neutral"
        regime["unemployment_rate"] = round(unrate, 1)
    if claims_val:
        regime["jobless_claims"] = int(claims_val)
        regime["claims_trend"] = "rising" if claims_val > claims_prev * 1.03 else \
                                  "falling" if claims_val < claims_prev * 0.97 else "stable"

    # Dollar strength
    dxy = data.get("dxy")
    if dxy:
        regime["dxy"] = round(dxy, 2)
        regime["dollar"] = "strong" if dxy > 103 else "weak" if dxy < 97 else "neutral"

    # Commodities (inflation proxy + risk sentiment)
    gold = data.get("gold_price")
    oil  = data.get("oil_price")
    if gold: regime["gold"]     = round(gold, 0)
    if oil:  regime["oil_wti"]  = round(oil, 2)

    # Fed rate
    fed = data.get("fed_rate", {}).get("value")
    if fed: regime["fed_funds_rate"] = round(fed, 2)

    # Consumer sentiment
    sent = data.get("consumer_sentiment", {}).get("value")
    if sent:
        regime["consumer_sentiment"] = round(sent, 1)
        regime["consumer_mood"] = "confident" if sent > 80 else "pessimistic" if sent < 60 else "cautious"

    # Industrial production
    indpro = data.get("industrial_production", {})
    if indpro.get("change") is not None:
        regime["industrial_production_trend"] = (
            "expanding" if indpro["change"] > 0 else "contracting"
        )

    # Sector rotation implication (derived)
    # Rising yields + strong labor = value/financials favoured; falling yields = growth/tech favoured
    if yield_10y and yield_2y and unrate:
        if yield_10y > 4.5 and unrate < 4.5:
            regime["rotation_bias"] = "value_financials"
        elif yield_10y < 3.5 or (yield_10y and spread is not None and spread < 0):
            regime["rotation_bias"] = "growth_tech"
        else:
            regime["rotation_bias"] = "balanced"

    return regime


def compute(force_refresh: bool = False) -> dict:
    """
    Return today's macro regime. Cached daily — only one FRED fetch per day.
    force_refresh=True bypasses the cache (use sparingly).
    """
    today = datetime.now(timezone.utc).date().isoformat()

    # Load cache
    if not force_refresh and os.path.exists(_CACHE_PATH):
        try:
            with open(_CACHE_PATH) as f:
                cached = json.load(f)
            if cached.get("date") == today:
                return cached
        except Exception:
            pass

    print("  [MacroRegime] Fetching macro data (FRED + yfinance)...")
    data: dict = {}

    # FRED series (monthly/weekly data — authoritative but lagged)
    for series_id, key in _FRED_SERIES.items():
        result = _fred_series(series_id)
        if result:
            data[key] = result

    # yfinance (daily/live — DXY, gold, oil, VIX, treasury live)
    data.update(_yf_prices())

    # FMP economic calendar (upcoming events next 14 days)
    calendar = _fmp_economic_calendar()

    regime = _derive_regime(data)

    output = {
        "date":            today,
        "regime":          regime,
        "raw":             data,
        "upcoming_events": calendar,
    }

    # Cache to disk
    try:
        with open(_CACHE_PATH, "w") as f:
            json.dump(output, f, indent=2)
    except Exception:
        pass

    # Summary line
    r = regime
    print(
        f"  [MacroRegime] inflation={r.get('inflation_trend')} | "
        f"curve={r.get('yield_curve')} ({r.get('yield_spread_bps')}bps) | "
        f"labor={r.get('labor')} (U={r.get('unemployment_rate')}%) | "
        f"10Y={r.get('yield_10y')} | DXY={r.get('dxy')} | "
        f"rotation_bias={r.get('rotation_bias')}"
    )
    if calendar:
        near = [e for e in calendar[:5] if e.get("impact") == "High"]
        if near:
            print(f"  [MacroRegime] Upcoming high-impact: " +
                  " | ".join(f"{e['date']} {e['event']}" for e in near[:3]))

    return output


def format_for_prompt(macro: dict) -> str:
    """
    Compact text block the committee prompt can embed.
    Keeps token cost low while giving the committee actionable regime signals.
    """
    if not macro:
        return ""

    r = macro.get("regime", {})
    lines = ["=== MACRO REGIME ==="]

    lines.append(
        f"Inflation: {r.get('inflation_trend','?')} | "
        f"CPI index {(macro.get('raw') or {}).get('cpi', {}).get('value','?')} "
        f"(MoM {r.get('inflation_trend','')})"
    )
    lines.append(
        f"Rates: Fed={r.get('fed_funds_rate','?')}% | "
        f"10Y={r.get('yield_10y','?')}% | 2Y={r.get('yield_2y','?')}% | "
        f"Curve={r.get('yield_curve','?')} ({r.get('yield_spread_bps','?')}bps)"
    )
    lines.append(
        f"Labor: U-rate={r.get('unemployment_rate','?')}% ({r.get('labor','?')}) | "
        f"Claims={r.get('jobless_claims','?')} ({r.get('claims_trend','?')})"
    )
    lines.append(
        f"Markets: DXY={r.get('dxy','?')} ({r.get('dollar','?')}) | "
        f"Gold=${r.get('gold','?')} | Oil=${r.get('oil_wti','?')} | "
        f"IndProd={r.get('industrial_production_trend','?')}"
    )
    lines.append(
        f"Rotation bias: {r.get('rotation_bias','?').upper().replace('_',' ')} | "
        f"Consumer sentiment: {r.get('consumer_sentiment','?')} ({r.get('consumer_mood','?')})"
    )

    # Upcoming high-impact events
    events = macro.get("upcoming_events", [])
    high = [e for e in events if e.get("impact") == "High"]
    if high:
        lines.append("Upcoming catalysts (HIGH impact):")
        for e in high[:5]:
            est = f" est={e['estimate']}" if e.get("estimate") else ""
            lines.append(f"  {e['date']}: {e['event']}{est}")

    lines.append("=== PRE-POSITION IMPLICATION ===")
    bias = r.get("rotation_bias", "")
    inflation = r.get("inflation_trend", "")
    curve = r.get("yield_curve", "")
    labor = r.get("labor", "")

    if curve == "inverted":
        lines.append("• Yield curve INVERTED — recession risk elevated. Favour defensive, reduce cyclicals.")
    elif curve == "flat":
        lines.append("• Yield curve flat — late-cycle. Monitor for rotation to defensives.")
    else:
        lines.append("• Yield curve normal — growth-friendly environment.")

    if inflation == "rising":
        lines.append("• Inflation rising — favour commodities, energy, financials over long-duration growth.")
    elif inflation == "falling":
        lines.append("• Inflation falling — tailwind for growth/tech (lower discount rate).")

    if labor == "strong":
        lines.append("• Labor market strong — consumer spending resilient, supports discretionary & financials.")
    elif labor == "weakening":
        lines.append("• Labor market weakening — defensive rotation signal.")

    return "\n".join(lines)
