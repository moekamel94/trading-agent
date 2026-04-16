import re
import requests
import xml.etree.ElementTree as ET
import config

_NEWDATA_URL  = "https://api.newdata.io/v1/news/financial"
_REUTERS_URLS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/reuters/technologyNews",
]
_HEADERS = {"User-Agent": "trading-agent mohammed.a.kamil@gmail.com"}

# Simple positive/negative keyword lists for RSS scoring
_POS = {"surge", "rally", "gain", "rise", "beat", "record", "profit", "growth",
        "bullish", "upgrade", "buy", "strong", "boost", "soar", "jump", "high"}
_NEG = {"fall", "drop", "crash", "loss", "miss", "bear", "bearish", "decline",
        "downgrade", "sell", "weak", "plunge", "risk", "slump", "cut", "low"}


def _keyword_score(text: str) -> float:
    words = set(re.findall(r"[a-z]+", text.lower()))
    pos = len(words & _POS)
    neg = len(words & _NEG)
    total = pos + neg
    return (pos - neg) / total if total else 0.0


def _fetch_newdata(symbol: str) -> list[dict]:
    if not config.NEWDATA_API_KEY:
        return []
    # Strip / for crypto (BTC/USD → BTC)
    query = symbol.split("/")[0] if "/" in symbol else symbol
    try:
        r = requests.get(
            _NEWDATA_URL,
            params={"token": config.NEWDATA_API_KEY, "q": query, "language": "en", "num_pages": 1},
            headers=_HEADERS,
            timeout=10,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        articles = data.get("data", {}).get("news", []) or []
        results = []
        for a in articles[:15]:
            title = a.get("title", "")
            score = _keyword_score(title + " " + a.get("description", ""))
            results.append({"headline": title, "score": score, "source": "newdata.io"})
        return results
    except Exception:
        return []


def _fetch_reuters(symbol: str) -> list[dict]:
    query = symbol.split("/")[0].lower() if "/" in symbol else symbol.lower()
    results = []
    for url in _REUTERS_URLS:
        try:
            r = requests.get(url, headers=_HEADERS, timeout=10)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            for item in root.iter("item"):
                title = item.findtext("title") or ""
                desc  = item.findtext("description") or ""
                combined = (title + " " + desc).lower()
                if query not in combined:
                    continue
                score = _keyword_score(title + " " + desc)
                results.append({"headline": title, "score": score, "source": "Reuters"})
        except Exception:
            continue
    return results


def compute(symbol: str) -> dict:
    articles = _fetch_newdata(symbol) + _fetch_reuters(symbol)

    if not articles:
        return {"score": None, "label": "neutral", "article_count": 0, "top_headlines": [], "sources": []}

    avg_score = round(sum(a["score"] for a in articles) / len(articles), 4)

    label = "neutral"
    if avg_score > 0.05:
        label = "positive"
    elif avg_score < -0.05:
        label = "negative"

    sources = list({a["source"] for a in articles})

    return {
        "score":         avg_score,
        "label":         label,
        "article_count": len(articles),
        "top_headlines": [a["headline"] for a in articles[:3]],
        "sources":       sources,
    }
