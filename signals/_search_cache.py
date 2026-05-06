"""
Centralized search result cache to reduce API costs 70%+.
Tavily: 4-hour TTL (news)
Exa: 24-hour TTL (research)
Serper: 12-hour TTL (fallback)
"""
import os, json, hashlib, time

_CACHE_DIR = "/tmp/kimmy_search_cache"
os.makedirs(_CACHE_DIR, exist_ok=True)

_TTL = {"tavily": 4 * 3600, "exa": 24 * 3600, "serper": 12 * 3600}


def _key(provider: str, query: str, ticker: str = "") -> str:
    h = hashlib.md5(f"{provider}:{ticker}:{query}".encode()).hexdigest()[:12]
    return os.path.join(_CACHE_DIR, f"{provider}_{h}.json")


def get(provider: str, query: str, ticker: str = "") -> list[str] | None:
    """Return cached results or None if missing/expired."""
    path = _key(provider, query, ticker)
    try:
        data = json.load(open(path))
        if time.time() - data.get("ts", 0) < _TTL.get(provider, 3600):
            return data["results"]
    except Exception:
        pass
    return None


def put(provider: str, query: str, results: list[str], ticker: str = "") -> None:
    """Cache search results."""
    path = _key(provider, query, ticker)
    try:
        json.dump({"ts": time.time(), "results": results}, open(path, "w"))
    except Exception:
        pass
