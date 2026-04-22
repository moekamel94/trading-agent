"""
Layered financial data pipeline for Kimmy:

  Layer 1 — Finnhub      : real-time quote, news, analyst recommendations
  Layer 2 — Alpha Vantage : technical indicators (RSI, MACD, SMA)
  Layer 3 — Twelve Data   : additional real-time price + indicators
  Layer 4 — FMP           : deep fundamentals (income, ratios, DCF)
  Layer 5 — Polygon       : aggregates, previous close, news
  Fallback — Yahoo Finance : always available, no key required

Each layer is skipped gracefully if its key is missing.
All layers run in parallel then merged into one dict for Claude.
"""
import requests
import yfinance as yf
import config
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

_TIMEOUT = 10

# Session-level skip set — APIs added here are skipped for the rest of the process
_SKIPPED: set[str] = set()


def _quota_hit(name: str, status: int, body: str = "") -> bool:
    """Return True if response indicates quota/trial exhausted; adds to skip set."""
    lower = body.lower()
    hit = status in (402, 429) or any(
        kw in lower for kw in ("quota", "limit exceeded", "trial", "out of credits",
                               "subscription required", "rate limit", "exceeded your")
    )
    if hit and name not in _SKIPPED:
        _SKIPPED.add(name)
        print(f"  [API_SKIP] {name}: quota/trial exceeded — skipping for this session")
    return hit


# ── Layer 1: Finnhub — real-time ────────────────────────────────────────────

def _finnhub_quote(symbol: str) -> dict:
    if not config.FINNHUB_API_KEY or "finnhub" in _SKIPPED:
        return {}
    clean = symbol.replace("/", "")
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": clean, "token": config.FINNHUB_API_KEY},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            _quota_hit("finnhub", r.status_code, r.text)
            return {}
        d = r.json()
        return {
            "current_price":  d.get("c"),
            "high_day":       d.get("h"),
            "low_day":        d.get("l"),
            "open_day":       d.get("o"),
            "prev_close":     d.get("pc"),
            "pct_change_day": round((d["c"] - d["pc"]) / d["pc"] * 100, 2) if d.get("c") and d.get("pc") else None,
        }
    except Exception:
        return {}


def _finnhub_news(symbol: str) -> list[str]:
    if not config.FINNHUB_API_KEY or "finnhub" in _SKIPPED:
        return []
    clean = symbol.split("/")[0] if "/" in symbol else symbol
    today = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": clean, "from": week_ago, "to": today, "token": config.FINNHUB_API_KEY},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            _quota_hit("finnhub", r.status_code, r.text)
            return []
        articles = r.json()[:5]
        return [a.get("headline", "") for a in articles if a.get("headline")]
    except Exception:
        return []


def _finnhub_recommendations(symbol: str) -> dict:
    if not config.FINNHUB_API_KEY or "finnhub" in _SKIPPED:
        return {}
    clean = symbol.split("/")[0] if "/" in symbol else symbol
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/stock/recommendation",
            params={"symbol": clean, "token": config.FINNHUB_API_KEY},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200 or not r.json():
            _quota_hit("finnhub", r.status_code, r.text)
            return {}
        latest = r.json()[0]
        return {
            "strong_buy":  latest.get("strongBuy"),
            "buy":         latest.get("buy"),
            "hold":        latest.get("hold"),
            "sell":        latest.get("sell"),
            "strong_sell": latest.get("strongSell"),
        }
    except Exception:
        return {}


def _finnhub_layer(symbol: str) -> dict:
    quote = _finnhub_quote(symbol)
    news  = _finnhub_news(symbol)
    recs  = _finnhub_recommendations(symbol)
    if not quote and not news and not recs:
        return {}
    return {"quote": quote, "news_headlines": news, "analyst_recommendations": recs}


# ── Layer 2: Alpha Vantage — indicators ─────────────────────────────────────

def _alpha_vantage_indicator(function: str, symbol: str, extra: dict = {}) -> dict:
    if not config.ALPHA_VANTAGE_KEY or "alpha_vantage" in _SKIPPED:
        return {}
    clean = symbol.split("/")[0] if "/" in symbol else symbol
    try:
        params = {
            "function": function,
            "symbol": clean,
            "interval": "daily",
            "apikey": config.ALPHA_VANTAGE_KEY,
            **extra,
        }
        r = requests.get("https://www.alphavantage.co/query", params=params, timeout=_TIMEOUT)
        if r.status_code != 200:
            _quota_hit("alpha_vantage", r.status_code, r.text)
            return {}
        d = r.json()
        # Alpha Vantage embeds rate-limit notices in the JSON body
        if "Note" in d or "Information" in d:
            _quota_hit("alpha_vantage", 429, str(d))
            return {}
        return d
    except Exception:
        return {}


def _alpha_vantage_layer(symbol: str) -> dict:
    if not config.ALPHA_VANTAGE_KEY:
        return {}
    result = {}

    rsi_data = _alpha_vantage_indicator("RSI", symbol, {"time_period": "14", "series_type": "close"})
    rsi_series = rsi_data.get("Technical Analysis: RSI", {})
    if rsi_series:
        latest_rsi = list(rsi_series.values())[0]
        result["rsi"] = round(float(latest_rsi.get("RSI", 0)), 2)

    macd_data = _alpha_vantage_indicator("MACD", symbol, {"series_type": "close"})
    macd_series = macd_data.get("Technical Analysis: MACD", {})
    if macd_series:
        latest_macd = list(macd_series.values())[0]
        result["macd"]        = round(float(latest_macd.get("MACD", 0)), 4)
        result["macd_signal"] = round(float(latest_macd.get("MACD_Signal", 0)), 4)
        result["macd_hist"]   = round(float(latest_macd.get("MACD_Hist", 0)), 4)

    overview = _alpha_vantage_indicator("OVERVIEW", symbol)
    if overview and "Symbol" in overview:
        result["pe_ratio"]      = overview.get("PERatio")
        result["eps"]           = overview.get("EPS")
        result["revenue_ttm"]   = overview.get("RevenueTTM")
        result["profit_margin"] = overview.get("ProfitMargin")
        result["52w_high"]      = overview.get("52WeekHigh")
        result["52w_low"]       = overview.get("52WeekLow")
        result["analyst_target"]= overview.get("AnalystTargetPrice")

    return result


# ── Layer 3: Twelve Data — price + indicators ────────────────────────────────

def _twelve_data_layer(symbol: str) -> dict:
    if not config.TWELVE_DATA_KEY or "twelve_data" in _SKIPPED:
        return {}
    clean = symbol.replace("/", "/")  # Twelve Data supports BTC/USD natively
    result = {}
    try:
        r = requests.get(
            "https://api.twelvedata.com/quote",
            params={"symbol": clean, "apikey": config.TWELVE_DATA_KEY},
            timeout=_TIMEOUT,
        )
        if r.status_code == 200:
            d = r.json()
            if d.get("code") in (400, 401, 403, 429):
                _quota_hit("twelve_data", d["code"], str(d))
            elif "close" in d:
                result["price"]          = d.get("close")
                result["volume"]         = d.get("volume")
                result["52w_high"]       = d.get("fifty_two_week", {}).get("high")
                result["52w_low"]        = d.get("fifty_two_week", {}).get("low")
                result["pct_change"]     = d.get("percent_change")
                result["is_market_open"] = d.get("is_market_open")
        else:
            _quota_hit("twelve_data", r.status_code, r.text)
    except Exception:
        pass
    return result


# ── Layer 4: Financial Modeling Prep — deep data ─────────────────────────────

def _fmp_layer(symbol: str) -> dict:
    if not config.FMP_API_KEY or "fmp" in _SKIPPED:
        return {}
    clean = symbol.split("/")[0] if "/" in symbol else symbol
    result = {}
    base = "https://financialmodelingprep.com/stable"
    params = {"symbol": clean, "apikey": config.FMP_API_KEY}

    try:
        r = requests.get(f"{base}/profile", params=params, timeout=_TIMEOUT)
        if r.status_code not in (200,):
            _quota_hit("fmp", r.status_code, r.text)
            return result
        if r.status_code == 200 and r.json():
            p = r.json()[0]
            result["company_name"] = p.get("companyName")
            result["sector"]       = p.get("sector")
            result["industry"]     = p.get("industry")
            result["market_cap"]   = p.get("mktCap")
            result["beta"]         = p.get("beta")
            result["price"]        = p.get("price")
            result["description"]  = (p.get("description") or "")[:300]
    except Exception:
        pass

    try:
        r = requests.get(f"{base}/key-metrics", params={**params, "limit": 1}, timeout=_TIMEOUT)
        if r.status_code == 200 and r.json():
            m = r.json()[0]
            result["pe_ratio"]       = m.get("peRatio")
            result["debt_to_equity"] = m.get("debtToEquity")
            result["current_ratio"]  = m.get("currentRatio")
            result["roe"]            = m.get("roe")
            result["free_cash_flow"] = m.get("freeCashFlowPerShare")
    except Exception:
        pass

    try:
        r = requests.get(f"{base}/income-statement", params={**params, "limit": 2}, timeout=_TIMEOUT)
        _income = r.json() if r.status_code == 200 else []
        if len(_income) >= 2:
            latest, prior = _income[0], _income[1]
            rev_latest = latest.get("revenue", 0) or 0
            rev_prior  = prior.get("revenue") or None
            result["revenue_latest"] = rev_latest
            result["revenue_growth"] = round((rev_latest - rev_prior) / rev_prior * 100, 2) if rev_prior else None
            result["net_income"]     = latest.get("netIncome")
            result["gross_profit"]   = latest.get("grossProfit")
            result["ebitda"]         = latest.get("ebitda")
    except Exception:
        pass

    return result


# ── Layer 5: Polygon — aggregates + news ────────────────────────────────────

def _polygon_layer(symbol: str) -> dict:
    if not config.POLYGON_API_KEY or "polygon" in _SKIPPED:
        return {}
    if "/" in symbol:
        return {}  # Polygon free tier focuses on stocks
    result = {}
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev",
            params={"apiKey": config.POLYGON_API_KEY},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            _quota_hit("polygon", r.status_code, r.text)
        elif r.json().get("results"):
            d = r.json()["results"][0]
            result["prev_open"]   = d.get("o")
            result["prev_close"]  = d.get("c")
            result["prev_high"]   = d.get("h")
            result["prev_low"]    = d.get("l")
            result["prev_volume"] = d.get("v")
            result["vwap"]        = d.get("vw")
    except Exception:
        pass

    try:
        r = requests.get(
            "https://api.polygon.io/v2/reference/news",
            params={"ticker": symbol, "limit": 5, "apiKey": config.POLYGON_API_KEY},
            timeout=_TIMEOUT,
        )
        if r.status_code == 200:
            articles = r.json().get("results", [])
            result["news"] = [a.get("title", "") for a in articles if a.get("title")]
    except Exception:
        pass

    return result


# ── Fallback: Yahoo Finance — always available ────────────────────────────────

def _yahoo_fallback(symbol: str) -> dict:
    clean = symbol.replace("/", "-")
    try:
        info = yf.Ticker(clean).fast_info
        return {
            "price":      getattr(info, "last_price", None),
            "52w_high":   getattr(info, "year_high", None),
            "52w_low":    getattr(info, "year_low", None),
            "market_cap": getattr(info, "market_cap", None),
            "volume":     getattr(info, "three_month_average_volume", None),
        }
    except Exception:
        return {}


# ── Main entry point ─────────────────────────────────────────────────────────

def compute(symbol: str) -> dict:
    """
    Run all layers in parallel. Returns:
    {
        "finnhub":      {...},   # real-time quote + news + recs
        "alpha_vantage":{...},   # RSI, MACD, overview
        "twelve_data":  {...},   # price, volume, 52w range
        "fmp":          {...},   # deep fundamentals, DCF, income
        "polygon":      {...},   # prev OHLCV, VWAP, news
        "yahoo":        {...},   # fallback always present
        "sources_active": [...]
    }
    """
    layers = {
        "finnhub":       lambda: _finnhub_layer(symbol),
        "alpha_vantage": lambda: _alpha_vantage_layer(symbol),
        "twelve_data":   lambda: _twelve_data_layer(symbol),
        "fmp":           lambda: _fmp_layer(symbol),
        "polygon":       lambda: _polygon_layer(symbol),
        "yahoo":         lambda: _yahoo_fallback(symbol),
    }

    result = {}
    active = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fn): name for name, fn in layers.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                data = future.result()
                result[name] = data
                if data:
                    active.append(name)
            except Exception:
                result[name] = {}

    result["sources_active"] = active
    return result
