"""
SEC EDGAR free API — insider filings (Form 4) and institutional holdings (13F).
No API key needed. Rate limit: ~10 requests/second.
Cache: 24 hours.
"""
import os, json, time, requests
from datetime import date, timedelta

_TIMEOUT   = 15
_CACHE_FILE = "/tmp/kimmy_edgar_cache.json"
_CACHE_TTL  = 86400  # 24 hours
_HEADERS    = {"User-Agent": "Kimmy Trading Bot research@kimmy.ai"}


def _load_cache() -> dict:
    try:
        data = json.load(open(_CACHE_FILE))
        if time.time() - data.get("_ts", 0) < _CACHE_TTL:
            return data
    except Exception:
        pass
    return {}


def _save_cache(data: dict) -> None:
    data["_ts"] = time.time()
    try:
        json.dump(data, open(_CACHE_FILE, "w"))
    except Exception:
        pass


def get_insider_transactions(symbol: str, days: int = 30) -> list[dict]:
    """
    Get recent Form 4 insider transactions for a symbol.
    Returns list of {date, insider_name, transaction_type, shares, value_usd}
    """
    cache = _load_cache()
    cache_key = f"insider_{symbol}_{days}"
    if cache_key in cache:
        return cache[cache_key]

    results = []
    try:
        # Search EDGAR full-text for Form 4s with this ticker
        from_date = (date.today() - timedelta(days=days)).isoformat()
        r = requests.get(
            "https://efts.sec.gov/LATEST/search-index",
            params={
                "q": f'"{symbol}"',
                "dateRange": "custom",
                "startdt": from_date,
                "forms": "4",
                "_source": "file_date,entity_name,file_num"
            },
            headers=_HEADERS,
            timeout=_TIMEOUT
        )
        if r.status_code == 200:
            hits = r.json().get("hits", {}).get("hits", [])
            for hit in hits[:20]:
                src = hit.get("_source", {})
                results.append({
                    "date": src.get("file_date", ""),
                    "insider_name": src.get("entity_name", ""),
                    "transaction_type": "Form4",
                    "shares": None,
                    "value_usd": None,
                })
    except Exception:
        pass

    cache[cache_key] = results
    _save_cache(cache)
    return results


def get_institutional_holders(symbol: str) -> list[dict]:
    """
    Get recent 13F filings showing institutional holders.
    Returns list of {institution, shares, value_usd, period}
    """
    cache = _load_cache()
    cache_key = f"inst_{symbol}"
    if cache_key in cache:
        return cache[cache_key]

    results = []
    try:
        r = requests.get(
            "https://efts.sec.gov/LATEST/search-index",
            params={
                "q": f'"{symbol}"',
                "forms": "13F-HR",
                "_source": "entity_name,file_date",
                "dateRange": "custom",
                "startdt": (date.today() - timedelta(days=90)).isoformat(),
            },
            headers=_HEADERS,
            timeout=_TIMEOUT
        )
        if r.status_code == 200:
            hits = r.json().get("hits", {}).get("hits", [])
            for hit in hits[:10]:
                src = hit.get("_source", {})
                results.append({
                    "institution": src.get("entity_name", ""),
                    "period": src.get("file_date", ""),
                    "shares": None,
                    "value_usd": None,
                })
    except Exception:
        pass

    cache[cache_key] = results
    _save_cache(cache)
    return results
