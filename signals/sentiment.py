from broker import alpaca


def compute(symbol: str) -> dict:
    try:
        articles = alpaca.get_news(symbol, limit=15)
    except Exception:
        return {"score": None, "article_count": 0, "summary": "unavailable"}

    if not articles:
        return {"score": None, "article_count": 0, "summary": "no news"}

    scores = []
    headlines = []
    for article in articles:
        if hasattr(article, "sentiment") and article.sentiment is not None:
            scores.append(article.sentiment)
        if hasattr(article, "headline"):
            headlines.append(article.headline)

    avg_score = round(sum(scores) / len(scores), 4) if scores else None

    label = "neutral"
    if avg_score is not None:
        if avg_score > 0.1:
            label = "positive"
        elif avg_score < -0.1:
            label = "negative"

    return {
        "score":         avg_score,
        "label":         label,
        "article_count": len(articles),
        "top_headlines": headlines[:3],
    }
