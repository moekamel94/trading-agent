"""
Monthly research cache — stores per-ticker deep research so daily cycles
can make decisions without calling any paid APIs.

Monthly: research.compute(), financial_data.compute(), social.compute(),
         future_growth.compute(), sentiment.compute(), earnings_soon()
         are all run once and stored here.

Daily: only yfinance (technicals + fundamentals) + CNN/VIX (market context)
       are fetched live. Everything else is loaded from this cache.
"""
import json
import os
from datetime import datetime, date

_CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", "research_cache.json")


def save(symbol: str, data: dict):
    cache = _load_raw()
    cache[symbol] = {**data, "_cached_at": datetime.utcnow().isoformat()}
    with open(_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, default=str)


def load(symbol: str) -> dict:
    return _load_raw().get(symbol, {})


def load_all() -> dict:
    return _load_raw()


def cache_age_days(symbol: str) -> int | None:
    entry = _load_raw().get(symbol, {})
    cached_at = entry.get("_cached_at")
    if not cached_at:
        return None
    try:
        cached_dt = datetime.fromisoformat(cached_at)
        return (datetime.utcnow() - cached_dt).days
    except Exception:
        return None


def needs_refresh(max_days: int = 30) -> bool:
    cache = _load_raw()
    if not cache:
        return True
    dates = [v.get("_cached_at", "2000-01-01") for v in cache.values() if isinstance(v, dict)]
    if not dates:
        return True
    oldest = min(dates)
    try:
        days_old = (datetime.utcnow() - datetime.fromisoformat(oldest)).days
        return days_old >= max_days
    except Exception:
        return True


def days_to_earnings_cached(symbol: str) -> int | None:
    """Compute days to earnings from cached date (no API call needed)."""
    entry = _load_raw().get(symbol, {})
    earnings_date = (entry.get("earnings_data") or {}).get("earnings_date")
    if not earnings_date:
        return None
    try:
        return (date.fromisoformat(earnings_date) - date.today()).days
    except Exception:
        return None


def summary(tickers: list[str]) -> str:
    cache = _load_raw()
    cached   = [s for s in tickers if s in cache]
    uncached = [s for s in tickers if s not in cache]
    oldest   = None
    if cached:
        dates = [cache[s].get("_cached_at", "") for s in cached if cache[s].get("_cached_at")]
        if dates:
            oldest = min(dates)[:10]
    return (
        f"Cache: {len(cached)}/{len(tickers)} tickers covered | "
        f"Oldest entry: {oldest or 'N/A'} | "
        f"Uncached: {uncached[:10]}{' ...' if len(uncached) > 10 else ''}"
    )


def _load_raw() -> dict:
    if not os.path.exists(_CACHE_FILE):
        return {}
    try:
        with open(_CACHE_FILE) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"[Cache] research_cache.json corrupted — starting fresh: {e}")
        return {}
    except Exception as e:
        print(f"[Cache] Could not load research_cache.json: {e}")
        return {}
