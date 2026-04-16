import yfinance as yf


def compute(symbol: str) -> dict:
    clean = symbol.replace("/", "-")
    try:
        info = yf.Ticker(clean).info
    except Exception:
        return {}

    def safe(key):
        v = info.get(key)
        return round(v, 4) if isinstance(v, float) else v

    return {
        "pe_ratio":        safe("trailingPE"),
        "eps_growth_yoy":  safe("earningsGrowth"),
        "revenue_growth":  safe("revenueGrowth"),
        "profit_margin":   safe("profitMargins"),
        "market_cap":      info.get("marketCap"),
        "sector":          info.get("sector"),
        "recommendation":  info.get("recommendationKey"),
    }
