"""
Multi-source research module — queries 6 sources in parallel:
SerpAPI, Tavily, Exa AI, Serper, Firecrawl, SearXNG (no key needed)
Results are fed to Claude to improve decision quality.
"""
import requests
import config
from concurrent.futures import ThreadPoolExecutor, as_completed

_TIMEOUT = 10
_HEADERS = {"User-Agent": "trading-agent mohammed.a.kamil@gmail.com"}

_SKIPPED: set[str] = set()


def _quota_hit(name: str, status: int, body: str = "") -> bool:
    lower = body.lower()
    hit = status in (402, 429) or any(
        kw in lower for kw in ("quota", "limit exceeded", "trial", "out of credits",
                               "subscription required", "rate limit", "exceeded your")
    )
    if hit and name not in _SKIPPED:
        _SKIPPED.add(name)
        print(f"  [API_SKIP] {name}: quota/trial exceeded — skipping for this session")
    return hit


def _serpapi(symbol: str) -> list[str]:
    if not config.SERPAPI_KEY or "serpapi" in _SKIPPED:
        return []
    try:
        r = requests.get(
            "https://serpapi.com/search.json",
            params={"q": f"{symbol} stock analysis news", "api_key": config.SERPAPI_KEY, "num": 5},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            _quota_hit("serpapi", r.status_code, r.text)
            return []
        return [
            f"{x.get('title','')} — {x.get('snippet','')}"
            for x in r.json().get("organic_results", [])[:5]
        ]
    except Exception:
        return []


def _tavily(symbol: str) -> list[str]:
    if not config.TAVILY_API_KEY or "tavily" in _SKIPPED:
        return []
    try:
        r = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": config.TAVILY_API_KEY, "query": f"{symbol} stock market outlook analysis", "max_results": 5},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            _quota_hit("tavily", r.status_code, r.text)
            return []
        return [
            f"{x.get('title','')} — {x.get('content','')[:250]}"
            for x in r.json().get("results", [])[:5]
        ]
    except Exception:
        return []


def _exa(symbol: str) -> list[str]:
    if not config.EXA_API_KEY or "exa" in _SKIPPED:
        return []
    try:
        r = requests.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": config.EXA_API_KEY, "Content-Type": "application/json"},
            json={"query": f"{symbol} stock financial analysis earnings", "numResults": 5, "useAutoprompt": True},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            _quota_hit("exa", r.status_code, r.text)
            return []
        return [
            f"{x.get('title','')} — {x.get('snippet', x.get('text',''))[:250]}"
            for x in r.json().get("results", [])[:5]
        ]
    except Exception:
        return []


def _serper(symbol: str) -> list[str]:
    if not config.SERPER_API_KEY or "serper" in _SKIPPED:
        return []
    try:
        r = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": config.SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": f"{symbol} stock earnings outlook analyst", "num": 5},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            _quota_hit("serper", r.status_code, r.text)
            return []
        return [
            f"{x.get('title','')} — {x.get('snippet','')}"
            for x in r.json().get("organic", [])[:5]
        ]
    except Exception:
        return []


def _firecrawl(symbol: str) -> list[str]:
    if not config.FIRECRAWL_API_KEY or "firecrawl" in _SKIPPED:
        return []
    url = f"https://finance.yahoo.com/quote/{symbol}/news/"
    try:
        r = requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {config.FIRECRAWL_API_KEY}", "Content-Type": "application/json"},
            json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
            timeout=20,
        )
        if r.status_code != 200:
            _quota_hit("firecrawl", r.status_code, r.text)
            return []
        content = r.json().get("data", {}).get("markdown", "")
        return [f"Yahoo Finance: {content[:600]}"] if content else []
    except Exception:
        return []


def _searxng(symbol: str) -> list[str]:
    # Public SearXNG instance — no key required
    try:
        r = requests.get(
            "https://searx.be/search",
            params={"q": f"{symbol} stock news analysis", "format": "json", "categories": "news,general"},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return []
        return [
            f"{x.get('title','')} — {x.get('content','')[:250]}"
            for x in r.json().get("results", [])[:5]
        ]
    except Exception:
        return []


def compute(symbol: str) -> dict:
    """Query all sources in parallel. Returns snippets + metadata for Claude.
    MONTHLY ONLY — never call this from run_cycle(). Paid APIs: SerpAPI, Tavily, Exa, Serper, Firecrawl.
    """
    import traceback, os
    # Hard guard: crash loudly if called outside --monthly context
    if os.environ.get("KIMMY_MONTHLY") != "1":
        print(f"  [COST GUARD] research.compute({symbol}) blocked — not in monthly context")
        return {"snippets": [], "snippet_count": 0, "source_count": 0}
    tasks = {
        "SerpAPI":   lambda: _serpapi(symbol),
        "Tavily":    lambda: _tavily(symbol),
        "Exa":       lambda: _exa(symbol),
        "Serper":    lambda: _serper(symbol),
        "Firecrawl": lambda: _firecrawl(symbol),
        "SearXNG":   lambda: _searxng(symbol),
    }

    all_snippets = []
    active_sources = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                snippets = future.result()
                if snippets:
                    all_snippets.extend(snippets)
                    active_sources.append(name)
            except Exception:
                pass

    return {
        "snippets":      all_snippets[:20],
        "snippet_count": len(all_snippets),
        "source_count":  len(active_sources),
        "sources":       active_sources,
    }
