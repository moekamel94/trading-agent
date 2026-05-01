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

# Geopolitical keyword buckets for Finnhub news sweep
_GEO_KEYWORDS = {
    "active_war":        ["war", "invasion", "military strike", "conflict escalation",
                          "troops deployed", "bombing", "missile strike", "NATO article 5"],
    "supply_disruption": ["supply chain", "Red Sea", "Suez Canal", "Strait of Hormuz",
                          "blockade", "shipping disruption", "port closure", "tariff shock",
                          "export ban"],
    "energy_crisis":     ["energy crisis", "gas shortage", "OPEC cut", "oil embargo",
                          "energy sanctions", "LNG shortage", "pipeline attack"],
    "trade_war":         ["trade war", "tariff escalation", "chip export ban",
                          "trade restrictions", "sanctions escalation", "decoupling"],
}

# Sector weights by macro regime label
# Keys must match config.SECTOR_MAP values exactly.
# Weights: 0.85+ = concentrate here | 0.45-0.70 = acceptable | <0.40 = BUCKET only
_SECTOR_WEIGHTS_BY_REGIME: dict[str, dict[str, float]] = {
    "growth_driven": {
        "ai_software":        0.95,
        "semis":              0.90,
        "cyber":              0.85,
        "ai_infra":           0.85,
        "ecommerce":          0.75,
        "healthcare":         0.65,
        "biotech":            0.60,
        "defense":            0.60,
        "nuclear":            0.65,
        "robotics":           0.60,
        "fintech":            0.55,
        "space":              0.55,
        "voice_ai":           0.55,
        "quantum":            0.50,
        "mega_tech":          0.70,
        "energy_oil":         0.25,
        "commodities_metals": 0.20,
    },
    "inflationary": {
        "energy_oil":         0.95,
        "commodities_metals": 0.90,
        "defense":            0.80,
        "nuclear":            0.80,
        "fintech":            0.65,
        "healthcare":         0.55,
        "biotech":            0.45,
        "robotics":           0.40,
        "cyber":              0.40,
        "ai_software":        0.30,
        "semis":              0.25,
        "ai_infra":           0.30,
        "mega_tech":          0.30,
        "ecommerce":          0.20,
        "space":              0.20,
        "voice_ai":           0.20,
        "quantum":            0.15,
    },
    "recessionary": {
        "defense":            0.90,
        "healthcare":         0.88,
        "nuclear":            0.75,
        "biotech":            0.65,
        "energy_oil":         0.55,
        "cyber":              0.50,
        "fintech":            0.40,
        "commodities_metals": 0.40,
        "ai_software":        0.30,
        "ai_infra":           0.30,
        "semis":              0.20,
        "mega_tech":          0.35,
        "ecommerce":          0.20,
        "robotics":           0.25,
        "space":              0.15,
        "voice_ai":           0.15,
        "quantum":            0.10,
    },
    "geopolitically_stressed": {
        "defense":            0.95,
        "energy_oil":         0.90,
        "commodities_metals": 0.88,
        "cyber":              0.85,
        "nuclear":            0.80,
        "space":              0.65,
        "healthcare":         0.55,
        "biotech":            0.45,
        "fintech":            0.40,
        "ai_software":        0.35,
        "semis":              0.30,
        "ai_infra":           0.35,
        "robotics":           0.45,
        "mega_tech":          0.30,
        "ecommerce":          0.20,
        "voice_ai":           0.20,
        "quantum":            0.20,
    },
    "stagflation": {
        "energy_oil":         0.90,
        "commodities_metals": 0.88,
        "defense":            0.82,
        "nuclear":            0.78,
        "healthcare":         0.68,
        "biotech":            0.55,
        "fintech":            0.50,
        "cyber":              0.45,
        "ai_software":        0.25,
        "semis":              0.20,
        "ai_infra":           0.25,
        "mega_tech":          0.25,
        "ecommerce":          0.15,
        "robotics":           0.30,
        "space":              0.15,
        "voice_ai":           0.15,
        "quantum":            0.10,
    },
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


def _finnhub_geo_news() -> dict:
    """
    Sweep Finnhub general market news for active geopolitical themes.
    Returns {flags: {active_war, supply_disruption, energy_crisis, trade_war},
             headlines: [str], any_active: bool}.
    Cached within the daily macro regime cache — costs 1 API call/day.
    """
    try:
        import config as _cfg
        if not _cfg.FINNHUB_API_KEY:
            return {}
        r = requests.get(
            "https://finnhub.io/api/v1/news",
            params={"category": "general", "token": _cfg.FINNHUB_API_KEY},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return {}
        articles = r.json() if isinstance(r.json(), list) else []

        # Combine headline + summary for each article
        texts = []
        for a in articles[:60]:
            t = (a.get("headline", "") + " " + a.get("summary", "")).lower()
            texts.append(t)

        flags: dict[str, bool] = {}
        headlines: list[str] = []
        for flag_key, keywords in _GEO_KEYWORDS.items():
            # Require ≥2 keyword hits to avoid single-word false positives
            hit_count = sum(1 for kw in keywords if any(kw.lower() in t for t in texts))
            flags[flag_key] = hit_count >= 2
            if flags[flag_key]:
                for a in articles[:30]:
                    h = a.get("headline", "")
                    if any(kw.lower() in h.lower() for kw in keywords):
                        headlines.append(h[:120])
                        break

        return {
            "flags":      flags,
            "headlines":  headlines[:5],
            "any_active": any(flags.values()),
        }
    except Exception:
        return {}


def _derive_regime_label(regime: dict, geo: dict) -> str:
    """
    Classify the macro environment into one clear regime label.
    Geopolitical stress (active war + energy/supply crisis) can override fundamentals.
    Order of precedence: geo_stress > stagflation > recessionary > inflationary > growth_driven
    """
    inflation  = regime.get("inflation_trend", "stable")
    curve      = regime.get("yield_curve", "normal")
    labor      = regime.get("labor", "neutral")
    yield_10y  = regime.get("yield_10y") or 4.0
    consumer   = regime.get("consumer_mood", "cautious")
    geo_flags  = (geo or {}).get("flags", {})

    # Geopolitical override: active war + any supply/energy disruption = geo_stressed
    if geo_flags.get("active_war") and (
            geo_flags.get("supply_disruption") or geo_flags.get("energy_crisis")):
        return "geopolitically_stressed"

    # Stagflation: rising inflation + weakening labor
    if inflation == "rising" and labor == "weakening":
        return "stagflation"

    # Recessionary: inverted curve + weakening labor, OR consumer pessimistic + claims rising
    claims_trend = regime.get("claims_trend", "stable")
    if ((curve == "inverted" and labor == "weakening") or
            (consumer == "pessimistic" and claims_trend == "rising")):
        return "recessionary"

    # Inflationary: rising inflation + high yields (4.5%+)
    if inflation == "rising" and yield_10y > 4.5:
        return "inflationary"

    # Geopolitically stressed (without active war): supply/energy disruption OR trade war
    if geo_flags.get("supply_disruption") or geo_flags.get("energy_crisis") or geo_flags.get("trade_war"):
        return "geopolitically_stressed"

    # Default: growth-driven
    return "growth_driven"


def _derive_sector_weights(regime_label: str) -> dict[str, float]:
    """
    Return the sector weight vector for the given regime label.
    Weights range 0.0–1.0; top 2-3 sectors ≥ 0.85, bottom sectors ≤ 0.25.
    """
    return dict(_SECTOR_WEIGHTS_BY_REGIME.get(regime_label,
                _SECTOR_WEIGHTS_BY_REGIME["growth_driven"]))


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
                # Back-fill keys added after the cache was first written
                cached.setdefault("regime_shift", False)
                cached.setdefault("prev_regime_label", None)
                return cached
        except Exception:
            pass

    print("  [MacroRegime] Fetching macro data (FRED + yfinance)...")
    data: dict = {}
    _fred_miss = 0

    # FRED series (monthly/weekly data — authoritative but lagged)
    for series_id, key in _FRED_SERIES.items():
        result = _fred_series(series_id)
        if result:
            data[key] = result
        else:
            _fred_miss += 1

    if _fred_miss > 3:
        try:
            from monitoring import health
            health.record_signal_degraded("MACRO", "macro_regime.fred",
                                          f"{_fred_miss}/{len(_FRED_SERIES)} FRED series failed")
        except Exception:
            pass

    # yfinance (daily/live — DXY, gold, oil, VIX, treasury live)
    data.update(_yf_prices())

    # FMP economic calendar (upcoming events next 14 days)
    calendar = _fmp_economic_calendar()

    regime = _derive_regime(data)

    # Geopolitical news sweep (Finnhub — cached within daily macro cache)
    geo = _finnhub_geo_news()

    # Derive higher-level regime label and sector weights
    regime_label    = _derive_regime_label(regime, geo)
    sector_weights  = _derive_sector_weights(regime_label)

    # Detect regime shift: compare to previous day's cached label
    regime_shift       = False
    prev_regime_label  = regime_label
    try:
        if os.path.exists(_CACHE_PATH):
            with open(_CACHE_PATH) as _pf:
                _prev = json.load(_pf)
            if _prev.get("date") != today:          # it's from a different day
                prev_regime_label = _prev.get("regime_label", regime_label)
                regime_shift = prev_regime_label != regime_label
    except Exception:
        pass

    output = {
        "date":              today,
        "regime":            regime,
        "regime_label":      regime_label,
        "sector_weights":    sector_weights,
        "geo_flags":         geo,
        "raw":               data,
        "upcoming_events":   calendar,
        "regime_shift":      regime_shift,
        "prev_regime_label": prev_regime_label if regime_shift else None,
    }

    # Cache to disk
    try:
        with open(_CACHE_PATH, "w") as f:
            json.dump(output, f, indent=2)
    except Exception:
        pass

    # Summary line
    r = regime
    # Top sectors by weight
    top_sectors = sorted(sector_weights.items(), key=lambda x: x[1], reverse=True)[:3]
    top_str = " | ".join(f"{s}={w:.2f}" for s, w in top_sectors)
    print(
        f"  [MacroRegime] REGIME={regime_label.upper()} | "
        f"inflation={r.get('inflation_trend')} | "
        f"curve={r.get('yield_curve')} ({r.get('yield_spread_bps')}bps) | "
        f"labor={r.get('labor')} (U={r.get('unemployment_rate')}%) | "
        f"10Y={r.get('yield_10y')} | DXY={r.get('dxy')}"
    )
    print(f"  [MacroRegime] TOP SECTORS: {top_str}")
    if geo.get("any_active"):
        print(f"  [MacroRegime] GEO FLAGS: {geo.get('flags')} | {geo.get('headlines', [])[:2]}")
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

    # Sector weights — the most actionable output
    sw = macro.get("sector_weights", {})
    if sw:
        lines.append("=== SECTOR ROUTING (regime-driven) ===")
        lines.append(f"Regime: {macro.get('regime_label', '?').upper().replace('_', ' ')}")
        # Sort by weight descending
        sorted_sw = sorted(sw.items(), key=lambda x: x[1], reverse=True)
        winning   = [(s, w) for s, w in sorted_sw if w >= 0.70]
        neutral   = [(s, w) for s, w in sorted_sw if 0.40 <= w < 0.70]
        avoid     = [(s, w) for s, w in sorted_sw if w < 0.40]
        if winning:
            lines.append("CONCENTRATE HERE (weight ≥ 0.70) — new BUYs only from these sectors:")
            for s, w in winning:
                lines.append(f"  ✓ {s}: {w:.2f}")
        if neutral:
            lines.append("NEUTRAL (weight 0.40–0.69) — existing holds OK, new entries need high conviction:")
            for s, w in neutral:
                lines.append(f"  ~ {s}: {w:.2f}")
        if avoid:
            lines.append("AVOID (weight < 0.40) — BUCKET only, no new entries:")
            for s, w in avoid:
                lines.append(f"  ✗ {s}: {w:.2f}")

    # Geopolitical alert
    geo = macro.get("geo_flags", {})
    if geo.get("any_active"):
        lines.append("=== GEOPOLITICAL ALERT ===")
        active = [k for k, v in (geo.get("flags") or {}).items() if v]
        lines.append(f"Active signals: {', '.join(active).replace('_', ' ').upper()}")
        for h in (geo.get("headlines") or [])[:3]:
            lines.append(f"  • {h}")

    return "\n".join(lines)
