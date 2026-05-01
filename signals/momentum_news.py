"""
Momentum News — two layers of event-driven momentum signals:

1. EARNINGS MOMENTUM  (per-ticker)
   • Fetches most recent actual vs estimated EPS & revenue (Finnhub)
   • Labels result: beat / miss / in-line
   • Score: +1.0 (strong beat) → -1.0 (strong miss)

2. GLOBAL MACRO / GEOPOLITICAL NEWS  (market-wide, computed once per cycle)
   • Searches for high-impact global events: wars, sanctions, tariffs,
     Fed shocks, oil supply disruptions, trade-war escalations, etc.
   • Labels: risk_off (sell pressure) | risk_on (buy pressure) | neutral
   • Score: -1.0 → +1.0
"""

import re
import requests
import config
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor

_TIMEOUT = 10
_HEADERS = {"User-Agent": "trading-agent mohammed.a.kamil@gmail.com"}

# Session-level skip set shared with research.py
def _get_skip_set() -> set:
    try:
        from signals import research as _r
        return _r._SKIPPED
    except Exception:
        return set()


def _quota_hit(name: str, status: int, body: str = "") -> bool:
    skip = _get_skip_set()
    lower = body.lower()
    hit = status in (402, 429) or any(
        kw in lower for kw in ("quota", "limit exceeded", "trial", "out of credits",
                               "subscription required", "rate limit", "exceeded your")
    )
    if hit and name not in skip:
        skip.add(name)
        print(f"  [API_SKIP] {name}: quota/trial exceeded — skipping for this session")
    return hit

# ─────────────────────────────────────────────────────────────
# GEOPOLITICAL / MACRO KEYWORDS
# ─────────────────────────────────────────────────────────────
_GEO_RISK_OFF = {
    "war", "invasion", "airstrike", "missile", "sanction", "embargo",
    "tariff", "trade war", "escalation", "conflict", "blockade",
    "recession", "default", "bank run", "systemic risk", "rate hike",
    "inflation surge", "supply shock", "oil shock", "energy crisis",
    "geopolitical", "nuclear", "terrorist", "coup", "humanitarian",
}
_GEO_RISK_ON = {
    "ceasefire", "peace deal", "trade deal", "rate cut", "stimulus",
    "bailout", "supply recovery", "de-escalation", "agreement",
    "resolution", "recovery", "reopening", "alliance",
}

# Earnings positive / negative language
_EARN_POS = {"beat", "beats", "exceeded", "surpassed", "record", "above", "raised guidance", "outperform"}
_EARN_NEG = {"miss", "missed", "below", "fell short", "lowered guidance", "cut guidance", "disappoints", "warning"}


def _keyword_score(text: str, pos_set: set, neg_set: set) -> float:
    words = set(re.findall(r"[a-z]+", text.lower()))
    # Also check multi-word phrases
    lower_text = text.lower()
    pos = len(words & pos_set) + sum(1 for p in pos_set if " " in p and p in lower_text)
    neg = len(words & neg_set) + sum(1 for n in neg_set if " " in n and n in lower_text)
    total = pos + neg
    return round((pos - neg) / total, 3) if total else 0.0


# ─────────────────────────────────────────────────────────────
# EARNINGS MOMENTUM (per-ticker)
# ─────────────────────────────────────────────────────────────

def _finnhub_earnings_surprise(symbol: str) -> dict:
    """Fetch last 4 quarters of EPS surprise from Finnhub."""
    if not config.FINNHUB_API_KEY:
        return {}
    clean = symbol.split("/")[0] if "/" in symbol else symbol
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/stock/earnings",
            params={"symbol": clean, "token": config.FINNHUB_API_KEY},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        if not data:
            return {}
        # Most recent quarter
        latest = data[0]
        actual   = latest.get("actual")
        estimate = latest.get("estimate")
        surprise_pct = latest.get("surprisePercent")
        period   = latest.get("period", "")

        if actual is None or estimate is None:
            return {"period": period, "label": "no_data"}

        if surprise_pct is None and estimate != 0:
            surprise_pct = round((actual - estimate) / abs(estimate) * 100, 2)

        label = "in_line"
        if surprise_pct is not None:
            if surprise_pct >= 5:
                label = "strong_beat"
            elif surprise_pct > 0:
                label = "beat"
            elif surprise_pct <= -5:
                label = "strong_miss"
            elif surprise_pct < 0:
                label = "miss"

        # Score: strong_beat=+1, beat=+0.5, in_line=0, miss=-0.5, strong_miss=-1
        score_map = {"strong_beat": 1.0, "beat": 0.5, "in_line": 0.0, "miss": -0.5, "strong_miss": -1.0}

        # Also look at trend: are last 3 quarters mostly beats?
        trend_labels = []
        for q in data[1:4]:
            sp = q.get("surprisePercent")
            if sp is not None:
                trend_labels.append("beat" if sp > 0 else "miss")
        trend = "consistent_beats" if trend_labels.count("beat") >= 2 else (
                "consistent_misses" if trend_labels.count("miss") >= 2 else "mixed")

        return {
            "period":        period,
            "actual_eps":    actual,
            "estimate_eps":  estimate,
            "surprise_pct":  round(surprise_pct, 2) if surprise_pct is not None else None,
            "label":         label,
            "score":         score_map.get(label, 0.0),
            "trend":         trend,
            "quarters_data": len(data),
        }
    except Exception:
        return {}


def _earnings_news_search(symbol: str) -> dict:
    """Search for recent earnings headlines to catch qualitative signals."""
    clean = symbol.split("/")[0] if "/" in symbol else symbol
    headlines = []

    skip = _get_skip_set()

    # Try Serper
    if config.SERPER_API_KEY and "serper" not in skip:
        try:
            r = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": config.SERPER_API_KEY, "Content-Type": "application/json"},
                json={"q": f"{clean} earnings results quarterly report beat miss", "num": 5, "tbs": "qdr:m"},
                timeout=_TIMEOUT,
            )
            if r.status_code == 200:
                for x in r.json().get("organic", [])[:5]:
                    headlines.append(x.get("title", "") + " " + x.get("snippet", ""))
            else:
                _quota_hit("serper", r.status_code, r.text)
        except Exception:
            pass

    # Try Tavily
    if config.TAVILY_API_KEY and "tavily" not in skip and len(headlines) < 3:
        try:
            r = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": config.TAVILY_API_KEY,
                    "query": f"{clean} quarterly earnings report results",
                    "max_results": 5,
                    "search_depth": "basic",
                },
                timeout=_TIMEOUT,
            )
            if r.status_code == 200:
                for x in r.json().get("results", [])[:5]:
                    headlines.append(x.get("title", "") + " " + x.get("content", "")[:200])
        except Exception:
            pass

    if not headlines:
        return {}

    combined = " ".join(headlines)
    score = _keyword_score(combined, _EARN_POS, _EARN_NEG)
    label = "positive" if score > 0.1 else ("negative" if score < -0.1 else "neutral")

    return {
        "news_score":    score,
        "news_label":    label,
        "top_headlines": [h[:120] for h in headlines[:3]],
    }


def earnings_momentum(symbol: str) -> dict:
    """
    Full earnings momentum signal for one ticker.
    Returns combined score from Finnhub EPS surprise + news headlines.
    """
    with ThreadPoolExecutor(max_workers=2) as ex:
        ft_eps  = ex.submit(_finnhub_earnings_surprise, symbol)
        ft_news = ex.submit(_earnings_news_search, symbol)

    eps_data  = ft_eps.result()
    news_data = ft_news.result()

    eps_score  = eps_data.get("score", 0.0)
    news_score = news_data.get("news_score", 0.0)

    # Weighted: EPS surprise 70%, news sentiment 30%
    if eps_data.get("label") == "no_data" or not eps_data:
        combined_score = round(news_score, 3)
    else:
        combined_score = round(eps_score * 0.7 + news_score * 0.3, 3)

    label = "strong_bullish" if combined_score >= 0.6 else (
            "bullish"        if combined_score >= 0.2 else (
            "neutral"        if combined_score >= -0.2 else (
            "bearish"        if combined_score >= -0.6 else "strong_bearish")))

    return {
        "available":      True,
        "combined_score": combined_score,
        "label":          label,
        "eps_surprise":   eps_data,
        "news_sentiment": news_data,
    }


# ─────────────────────────────────────────────────────────────
# GLOBAL MACRO / GEOPOLITICAL NEWS (market-wide, once per cycle)
# ─────────────────────────────────────────────────────────────

def _search_macro_news() -> list[str]:
    """Pull global macro/geopolitical headlines from multiple sources."""
    results = []
    queries = [
        "global geopolitical risk markets war sanctions tariff 2025",
        "US Iran war oil shock global markets",
        "trade war tariff escalation stock market impact",
        "Federal Reserve rate decision inflation shock",
        "global recession risk financial crisis 2025",
    ]

    # SearXNG — no key needed, always try first
    for q in queries[:2]:
        try:
            r = requests.get(
                "https://searx.be/search",
                params={"q": q, "format": "json", "categories": "news"},
                headers=_HEADERS,
                timeout=_TIMEOUT,
            )
            if r.status_code == 200:
                for x in r.json().get("results", [])[:3]:
                    results.append(x.get("title", "") + " " + x.get("content", "")[:200])
        except Exception:
            pass

    skip = _get_skip_set()

    # Tavily — broader macro context
    if config.TAVILY_API_KEY and "tavily" not in skip:
        for q in queries[:3]:
            try:
                r = requests.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": config.TAVILY_API_KEY,
                        "query": q,
                        "max_results": 3,
                        "search_depth": "basic",
                        "topic": "news",
                    },
                    timeout=_TIMEOUT,
                )
                if r.status_code == 200:
                    for x in r.json().get("results", [])[:3]:
                        results.append(x.get("title", "") + " " + x.get("content", "")[:200])
                else:
                    _quota_hit("tavily", r.status_code, r.text)
                    break
            except Exception:
                pass

    # Serper — Google News
    if config.SERPER_API_KEY and "serper" not in skip:
        try:
            r = requests.post(
                "https://google.serper.dev/news",
                headers={"X-API-KEY": config.SERPER_API_KEY, "Content-Type": "application/json"},
                json={"q": "global markets geopolitical risk war oil sanctions tariff", "num": 5},
                timeout=_TIMEOUT,
            )
            if r.status_code == 200:
                for x in r.json().get("news", [])[:5]:
                    results.append(x.get("title", "") + " " + x.get("snippet", ""))
            else:
                _quota_hit("serper", r.status_code, r.text)
        except Exception:
            pass

    return results


def global_macro_momentum() -> dict:
    """
    Market-wide macro/geopolitical momentum signal.
    Call once per cycle — expensive (multiple searches).
    Returns risk_off / neutral / risk_on with score and key themes.
    """
    headlines = _search_macro_news()

    if not headlines:
        return {
            "available": False,
            "label":     "neutral",
            "score":     0.0,
            "themes":    [],
            "headline_count": 0,
        }

    combined = " ".join(headlines)
    score = _keyword_score(combined, _GEO_RISK_ON, _GEO_RISK_OFF)

    label = "risk_on"  if score >= 0.15 else (
            "risk_off" if score <= -0.15 else "neutral")

    # Extract key themes mentioned
    themes = []
    theme_map = {
        "war/conflict":      ["war", "invasion", "airstrike", "missile", "conflict"],
        "sanctions/tariffs": ["sanction", "embargo", "tariff", "trade war"],
        "rate/inflation":    ["rate hike", "inflation surge", "fed", "interest rate"],
        "oil/energy":        ["oil shock", "energy crisis", "opec", "oil price"],
        "ceasefire/deal":    ["ceasefire", "peace deal", "trade deal", "agreement"],
        "recession risk":    ["recession", "slowdown", "gdp contraction"],
        "stimulus/cut":      ["rate cut", "stimulus", "bailout", "quantitative easing"],
    }
    lower_combined = combined.lower()
    for theme, keywords in theme_map.items():
        if any(kw in lower_combined for kw in keywords):
            themes.append(theme)

    return {
        "available":      True,
        "label":          label,
        "score":          score,
        "themes":         themes,
        "headline_count": len(headlines),
        "top_headlines":  [h[:120] for h in headlines[:4]],
    }
