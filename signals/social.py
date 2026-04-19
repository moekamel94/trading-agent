"""
Social sentiment signals (per ticker):
- StockTwits: real-time bullish/bearish tags from traders
- Reddit: r/wallstreetbets, r/stocks, r/investing mention + sentiment
"""
import re
import requests
from concurrent.futures import ThreadPoolExecutor

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
_TIMEOUT = 10
_POS = {"buy","bullish","moon","calls","long","rally","breakout","squeeze","upside","green","pump"}
_NEG = {"sell","bearish","puts","short","crash","dump","red","loss","avoid","downside","overvalued"}


def _score(text: str) -> float:
    words = set(re.findall(r"[a-z]+", text.lower()))
    pos, neg = len(words & _POS), len(words & _NEG)
    return (pos - neg) / (pos + neg) if (pos + neg) else 0.0


def _stocktwits(symbol: str) -> dict:
    clean = symbol.split("/")[0] if "/" in symbol else symbol
    try:
        r = requests.get(
            f"https://api.stocktwits.com/api/2/streams/symbol/{clean}.json",
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return {}
        messages = r.json().get("messages", [])
        if not messages:
            return {}

        bullish = sum(
            1 for m in messages
            if (m.get("entities") or {}).get("sentiment", {}).get("basic") == "Bullish"
        )
        bearish = sum(
            1 for m in messages
            if (m.get("entities") or {}).get("sentiment", {}).get("basic") == "Bearish"
        )
        tagged = bullish + bearish

        if tagged:
            label = "bullish" if bullish > bearish else ("bearish" if bearish > bullish else "neutral")
            bull_pct = round(bullish / tagged * 100)
        else:
            avg = sum(_score(m.get("body", "")) for m in messages) / len(messages)
            label = "bullish" if avg > 0.05 else ("bearish" if avg < -0.05 else "neutral")
            bull_pct = None

        return {
            "message_count": len(messages),
            "bullish": bullish,
            "bearish": bearish,
            "bull_pct": bull_pct,
            "label": label,
        }
    except Exception:
        return {}


def _reddit(symbol: str) -> dict:
    clean = symbol.split("/")[0] if "/" in symbol else symbol
    hits = []
    for sub in ["wallstreetbets", "stocks", "investing"]:
        try:
            r = requests.get(
                f"https://www.reddit.com/r/{sub}/search.json",
                params={"q": clean, "sort": "new", "limit": 10, "t": "week"},
                headers=_HEADERS, timeout=_TIMEOUT,
            )
            if r.status_code != 200:
                continue
            for post in r.json().get("data", {}).get("children", []):
                d = post.get("data", {})
                title = d.get("title", "")
                if clean.lower() in title.lower() or f"${clean.upper()}" in title:
                    hits.append({
                        "title": title,
                        "score": d.get("score", 0),
                        "upvote_ratio": d.get("upvote_ratio", 0.5),
                    })
        except Exception:
            continue

    if not hits:
        return {}

    avg_ratio = sum(h["upvote_ratio"] for h in hits) / len(hits)
    avg_text  = sum(_score(h["title"]) for h in hits) / len(hits)
    label = "bullish" if (avg_ratio > 0.7 and avg_text >= 0) else (
            "bearish" if (avg_ratio < 0.4 or avg_text < -0.05) else "neutral")

    return {
        "mention_count": len(hits),
        "avg_upvote_ratio": round(avg_ratio, 2),
        "top_posts": [h["title"] for h in sorted(hits, key=lambda x: x["score"], reverse=True)[:3]],
        "label": label,
    }


def compute(symbol: str) -> dict:
    with ThreadPoolExecutor(max_workers=2) as ex:
        ft_st = ex.submit(_stocktwits, symbol)
        ft_rd = ex.submit(_reddit, symbol)
    st = ft_st.result()
    rd = ft_rd.result()

    labels = [d["label"] for d in [st, rd] if d.get("label")]
    if labels:
        bull = labels.count("bullish")
        bear = labels.count("bearish")
        combined = "bullish" if bull > bear else ("bearish" if bear > bull else "neutral")
    else:
        combined = "neutral"

    return {"stocktwits": st, "reddit": rd, "combined_label": combined}
