"""
Future Growth Evaluator — identifies companies positioned to beat the market.

Goal: find stocks where growth is ACCELERATING faster than the market expects,
margins are expanding, and analysts keep raising targets. These are the stocks
that compound wealth over months, not days.

Scoring: 0-100 across 4 categories:
  - Growth Momentum   (30 pts): revenue/earnings growth rate and trend
  - Growth Quality    (25 pts): margins, FCF, efficiency
  - Analyst Conviction(25 pts): consensus, target upside, coverage
  - Earnings Execution(20 pts): beat rate, surprise trend

A score >= 70 = high-growth stock -> wider P/E tolerance, higher position sizing
A score >= 50 = steady compounder -> standard criteria
A score < 35  = avoid or flag for exit if already held
"""
import requests
import yfinance as yf
import config
from concurrent.futures import ThreadPoolExecutor, as_completed

_TIMEOUT  = 10
_HEADERS  = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Industry tailwind map — stocks in these categories get an automatic tailwind flag
_TAILWIND_SECTORS = {
    "ai_robotics":    {"NVDA","AMD","INTC","AVGO","QCOM","AMAT","LRCX","MU","COHR","TER","PDYN","TSLA"},
    "quantum":        {"IBM","IONQ","RGTI","QUBT","QBTS","HON","MSFT","GOOGL","MKSI"},
    "clean_energy":   {"ENPH","FSLR","NEE","PLUG","BE","SEDG","RUN","CEG"},
    "biotech":        {"MRNA","BNTX","REGN","VRTX","BIIB","GILD","AMGN","ABBV","LLY","NVO"},
    "cybersecurity":  {"CRWD","PANW","FTNT","ZS","S","OKTA","CYBR"},
    "space_defense":  {"LMT","RTX","NOC","GD","BA","RKLB","ASTS","MNTS"},
    "fintech":        {"V","MA","SQ","PYPL","AFRM","SOFI","NU"},
}


def _get_yf_metrics(symbol: str) -> dict:
    """Pull forward-looking metrics from yfinance."""
    clean = symbol.split("/")[0] if "/" in symbol else symbol
    try:
        info = yf.Ticker(clean).info
        target = info.get("targetMeanPrice")
        price  = info.get("currentPrice") or info.get("regularMarketPrice") or 1

        return {
            "peg_ratio":         info.get("pegRatio"),
            "forward_pe":        info.get("forwardPE"),
            "trailing_pe":       info.get("trailingPE"),
            "revenue_growth":    info.get("revenueGrowth"),       # TTM YoY
            "earnings_growth":   info.get("earningsGrowth"),      # TTM YoY
            "gross_margin":      info.get("grossMargins"),
            "operating_margin":  info.get("operatingMargins"),
            "fcf":               info.get("freeCashflow"),
            "roe":               info.get("returnOnEquity"),
            "debt_to_equity":    info.get("debtToEquity"),
            "rec_mean":          info.get("recommendationMean"),   # 1=strong buy 5=sell
            "analyst_count":     info.get("numberOfAnalystOpinions"),
            "target_mean":       target,
            "target_high":       info.get("targetHighPrice"),
            "target_upside":     round((target - price) / price * 100, 1) if target and price else None,
            "sector":            info.get("sector"),
            "industry":          info.get("industry"),
        }
    except Exception:
        return {}


def _get_earnings_history(symbol: str) -> dict:
    """Pull last 4 quarters of earnings surprises from Finnhub."""
    if not config.FINNHUB_API_KEY:
        return {}
    clean = symbol.split("/")[0] if "/" in symbol else symbol
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/stock/earnings",
            params={"symbol": clean, "limit": 4, "token": config.FINNHUB_API_KEY},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200 or not r.json():
            return {}
        quarters = r.json()
        beats = sum(1 for q in quarters if (q.get("actual") or 0) >= (q.get("estimate") or 0))
        surprises = [q.get("surprisePercent", 0) for q in quarters if q.get("surprisePercent") is not None]
        avg_surprise = round(sum(surprises) / len(surprises), 2) if surprises else 0

        # Trend: is surprise % improving each quarter? (latest vs earliest)
        surprise_trend = "improving" if len(surprises) >= 2 and surprises[0] > surprises[-1] else (
                         "declining" if len(surprises) >= 2 and surprises[0] < surprises[-1] else "flat")

        return {
            "quarters_checked": len(quarters),
            "beat_count":       beats,
            "beat_rate":        round(beats / len(quarters) * 100) if quarters else 0,
            "avg_surprise_pct": avg_surprise,
            "surprise_trend":   surprise_trend,
            "recent_quarters":  [{"period": q.get("period"), "actual": q.get("actual"),
                                   "estimate": q.get("estimate"), "surprise_pct": q.get("surprisePercent")}
                                  for q in quarters[:4]],
        }
    except Exception:
        return {}


def _detect_tailwinds(symbol: str, sector: str, industry: str) -> list[str]:
    """Identify which structural growth tailwinds apply to this stock."""
    tailwinds = []
    sym_upper = symbol.upper().split("/")[0]
    sec_lower = (sector or "").lower()
    ind_lower = (industry or "").lower()

    for theme, tickers in _TAILWIND_SECTORS.items():
        if sym_upper in tickers:
            tailwinds.append(theme)

    # Sector-based detection
    if "technology" in sec_lower or "semiconductor" in ind_lower:
        if "ai_robotics" not in tailwinds:
            tailwinds.append("ai_robotics")
    if "health" in sec_lower or "biotech" in ind_lower or "pharma" in ind_lower:
        if "biotech" not in tailwinds:
            tailwinds.append("biotech")
    if "energy" in sec_lower and ("renewable" in ind_lower or "solar" in ind_lower):
        tailwinds.append("clean_energy")

    return tailwinds


def _score(metrics: dict, earnings: dict) -> dict:
    """Compute 0-100 future growth score across 4 categories."""
    score = 0
    breakdown = {}

    # ── Category 1: Growth Momentum (30 pts) ─────────────────────────────────
    growth_pts = 0
    rev_g = metrics.get("revenue_growth") or 0
    earn_g = metrics.get("earnings_growth") or 0

    if rev_g > 0.50:   growth_pts += 12
    elif rev_g > 0.25: growth_pts += 9
    elif rev_g > 0.10: growth_pts += 5
    elif rev_g > 0:    growth_pts += 2

    if earn_g > 0.75:  growth_pts += 10
    elif earn_g > 0.30:growth_pts += 7
    elif earn_g > 0.10:growth_pts += 4
    elif earn_g > 0:   growth_pts += 2

    fpe = metrics.get("forward_pe")
    tpe = metrics.get("trailing_pe")
    if fpe and tpe and fpe < tpe:
        growth_pts += 4  # market expects earnings to grow into valuation

    peg = metrics.get("peg_ratio")
    if peg and 0 < peg < 1.0:   growth_pts += 4
    elif peg and peg < 1.5:     growth_pts += 2

    score += min(growth_pts, 30)
    breakdown["growth_momentum"] = min(growth_pts, 30)

    # ── Category 2: Growth Quality (25 pts) ──────────────────────────────────
    quality_pts = 0
    gm = metrics.get("gross_margin") or 0
    om = metrics.get("operating_margin") or 0
    roe = metrics.get("roe") or 0
    fcf = metrics.get("fcf") or 0
    de = metrics.get("debt_to_equity") or 0

    if gm > 0.60:     quality_pts += 8
    elif gm > 0.40:   quality_pts += 6
    elif gm > 0.25:   quality_pts += 3

    if om > 0.25:     quality_pts += 5
    elif om > 0.10:   quality_pts += 3

    # Rule of 40: revenue_growth% + operating_margin% >= 40 (healthy tech co)
    rule40 = (rev_g * 100) + (om * 100)
    if rule40 >= 60:  quality_pts += 5
    elif rule40 >= 40:quality_pts += 3

    if fcf > 0:       quality_pts += 4
    if roe > 0.20:    quality_pts += 3
    elif roe > 0.10:  quality_pts += 1

    score += min(quality_pts, 25)
    breakdown["growth_quality"] = min(quality_pts, 25)

    # ── Category 3: Analyst Conviction (25 pts) ──────────────────────────────
    conviction_pts = 0
    rec = metrics.get("rec_mean")       # 1=strong buy, 5=sell
    upside = metrics.get("target_upside")
    n_analysts = metrics.get("analyst_count") or 0

    if rec and rec <= 1.5:    conviction_pts += 10
    elif rec and rec <= 2.0:  conviction_pts += 7
    elif rec and rec <= 2.5:  conviction_pts += 4

    if upside and upside > 40:   conviction_pts += 10
    elif upside and upside > 25: conviction_pts += 7
    elif upside and upside > 15: conviction_pts += 4

    if n_analysts > 20:    conviction_pts += 5
    elif n_analysts > 10:  conviction_pts += 3

    score += min(conviction_pts, 25)
    breakdown["analyst_conviction"] = min(conviction_pts, 25)

    # ── Category 4: Earnings Execution (20 pts) ──────────────────────────────
    execution_pts = 0
    beat_rate = earnings.get("beat_rate", 0)
    avg_surp  = earnings.get("avg_surprise_pct", 0)
    surp_trend = earnings.get("surprise_trend", "flat")

    if beat_rate == 100:   execution_pts += 10
    elif beat_rate >= 75:  execution_pts += 7
    elif beat_rate >= 50:  execution_pts += 4

    if avg_surp > 10:      execution_pts += 6
    elif avg_surp > 5:     execution_pts += 4
    elif avg_surp > 2:     execution_pts += 2

    if surp_trend == "improving":
        execution_pts += 4

    score += min(execution_pts, 20)
    breakdown["earnings_execution"] = min(execution_pts, 20)

    # ── Classification ────────────────────────────────────────────────────────
    if score >= 70:   classification = "high_growth"
    elif score >= 50: classification = "steady_compounder"
    elif score >= 35: classification = "value_play"
    else:             classification = "declining"

    return {
        "score":          score,
        "classification": classification,
        "breakdown":      breakdown,
        "peg_ratio":      peg,
        "forward_pe":     fpe,
        "revenue_growth": round(rev_g * 100, 1) if rev_g else None,
        "earnings_growth":round(earn_g * 100, 1) if earn_g else None,
        "rule_of_40":     round(rule40, 1),
        "target_upside":  upside,
        "analyst_count":  n_analysts,
        "beat_rate_pct":  beat_rate,
        "avg_eps_surprise": earnings.get("avg_surprise_pct"),
        "surprise_trend": earnings.get("surprise_trend"),
        "recent_quarters":earnings.get("recent_quarters", []),
    }


def compute(symbol: str) -> dict:
    """
    Returns a future growth assessment for use by Claude.
    High-growth stocks (score >= 70) are treated with wider P/E tolerance
    and higher position sizing by the risk manager.
    """
    with ThreadPoolExecutor(max_workers=2) as ex:
        ft_yf   = ex.submit(_get_yf_metrics, symbol)
        ft_earn = ex.submit(_get_earnings_history, symbol)

    metrics = ft_yf.result()
    earnings = ft_earn.result()

    if not metrics:
        return {"score": 0, "classification": "unknown", "tailwinds": []}

    scored = _score(metrics, earnings)

    tailwinds = _detect_tailwinds(
        symbol,
        metrics.get("sector", ""),
        metrics.get("industry", ""),
    )
    scored["tailwinds"]   = tailwinds
    scored["sector"]      = metrics.get("sector")
    scored["industry"]    = metrics.get("industry")
    scored["gross_margin"]= round((metrics.get("gross_margin") or 0) * 100, 1)
    scored["rec_mean"]    = metrics.get("rec_mean")

    return scored
