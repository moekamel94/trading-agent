import json
import anthropic
import config

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

_SYSTEM = """You are a disciplined quantitative portfolio manager making paper trading decisions.

STRICT TRADING CRITERIA — you must enforce these. If any criterion is not clearly met, return HOLD.

ENTRY (all must be true to BUY):
- RSI between 40 and 68 (momentum without being overbought)
- Price above SMA50 (confirmed uptrend)
- MACD not in bearish crossover
- EPS growth YoY > 5%
- Revenue growth YoY > 3%
- Profit margin > 8%
- P/E ratio < 55
- Sentiment not negative

EXIT (any one triggers SELL):
- Position is profitable and RSI > 75 (take profits before reversal)
- MACD turns bearish crossover on a profitable position

POSITION SIZING by confidence:
- Confidence 7/10 -> 3% allocation
- Confidence 8/10 -> 4% allocation
- Confidence 9-10/10 -> 5% allocation

Only trade when signals CLEARLY align. HOLD is always the safe default.
Return valid JSON only — no prose, no markdown fences."""

_SCHEMA = """
Return exactly this JSON:
{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": <integer 1-10>,
  "allocation_pct": <float 0.0-5.0>,
  "asset_type": "stock" | "crypto" | "option",
  "option_direction": "call" | "put" | null,
  "rationale": "<one concise sentence stating which criteria drove the decision>"
}
"""


def _format_financial_data(fin: dict) -> str:
    if not fin:
        return ""
    active = fin.get("sources_active", [])
    if not active:
        return ""
    blocks = []

    fh = fin.get("finnhub", {})
    if fh:
        q = fh.get("quote", {})
        recs = fh.get("analyst_recommendations", {})
        news = fh.get("news_headlines", [])
        parts = []
        if q.get("current_price"):
            parts.append(f"price=${q['current_price']} ({q.get('pct_change_day','?')}% today)")
        if recs:
            parts.append(f"analyst: {recs.get('strong_buy',0)} strong buy / {recs.get('buy',0)} buy / {recs.get('hold',0)} hold / {recs.get('sell',0)} sell")
        if news:
            parts.append("news: " + " | ".join(news[:3]))
        if parts:
            blocks.append("Finnhub (real-time): " + " — ".join(parts))

    av = fin.get("alpha_vantage", {})
    if av:
        parts = []
        if av.get("rsi"):
            parts.append(f"RSI={av['rsi']}")
        if av.get("macd"):
            parts.append(f"MACD={av['macd']} sig={av.get('macd_signal','?')}")
        if av.get("analyst_target"):
            parts.append(f"analyst target=${av['analyst_target']}")
        if av.get("52w_high"):
            parts.append(f"52w {av.get('52w_low')}–{av['52w_high']}")
        if parts:
            blocks.append("Alpha Vantage (indicators): " + " | ".join(parts))

    td = fin.get("twelve_data", {})
    if td:
        parts = []
        if td.get("price"):
            parts.append(f"price={td['price']}")
        if td.get("pct_change"):
            parts.append(f"chg={td['pct_change']}%")
        if td.get("is_market_open") is not None:
            parts.append(f"market={'open' if td['is_market_open'] else 'closed'}")
        if parts:
            blocks.append("Twelve Data: " + " | ".join(parts))

    fmp = fin.get("fmp", {})
    if fmp:
        parts = []
        if fmp.get("dcf_value"):
            parts.append(f"DCF=${fmp['dcf_value']:.2f}")
        if fmp.get("pe_ratio"):
            parts.append(f"P/E={fmp['pe_ratio']}")
        if fmp.get("revenue_growth") is not None:
            parts.append(f"rev growth={fmp['revenue_growth']}%")
        if fmp.get("debt_to_equity"):
            parts.append(f"D/E={fmp['debt_to_equity']}")
        if fmp.get("roe"):
            parts.append(f"ROE={fmp['roe']:.2%}")
        if fmp.get("description"):
            parts.append(f"co: {fmp['description'][:150]}")
        if parts:
            blocks.append("FMP (deep data): " + " | ".join(parts))

    poly = fin.get("polygon", {})
    if poly:
        parts = []
        if poly.get("vwap"):
            parts.append(f"VWAP={poly['vwap']}")
        if poly.get("prev_volume"):
            parts.append(f"vol={poly['prev_volume']:,.0f}")
        if poly.get("news"):
            parts.append("news: " + " | ".join(poly["news"][:2]))
        if parts:
            blocks.append("Polygon: " + " | ".join(parts))

    yah = fin.get("yahoo", {})
    if yah and not fmp:  # only show yahoo if FMP didn't provide deeper data
        parts = [f"{k}={v}" for k, v in yah.items() if v is not None]
        if parts:
            blocks.append("Yahoo Finance (fallback): " + " | ".join(parts))

    if not blocks:
        return ""
    return "\nFinancial Data:\n" + "\n".join(f"  {b}" for b in blocks) + "\n"


def _format_social(soc: dict) -> str:
    if not soc:
        return ""
    parts = []
    st = soc.get("stocktwits", {})
    if st:
        bull_pct = f" ({st['bull_pct']}% bull)" if st.get("bull_pct") else ""
        parts.append(f"StockTwits: {st.get('label','?')}{bull_pct} — {st.get('message_count',0)} messages")
    rd = soc.get("reddit", {})
    if rd:
        parts.append(f"Reddit: {rd.get('label','?')} — {rd.get('mention_count',0)} mentions (upvote ratio {rd.get('avg_upvote_ratio','?')})")
        if rd.get("top_posts"):
            parts.append("  top posts: " + " | ".join(rd["top_posts"][:2]))
    combined = soc.get("combined_label", "")
    if combined:
        parts.insert(0, f"Social combined: {combined.upper()}")
    return "\nSocial Sentiment:\n" + "\n".join(f"  {p}" for p in parts) + "\n" if parts else ""


def _format_market_context(mkt: dict, earnings: dict) -> str:
    if not mkt:
        return ""
    parts = []
    fg = mkt.get("fear_and_greed", {})
    if fg.get("score") is not None:
        parts.append(f"Fear & Greed: {fg['score']}/100 — {fg.get('label','')} (risk: {mkt.get('market_risk','')})")
    vix = mkt.get("vix", {})
    if vix.get("vix"):
        parts.append(f"VIX: {vix['vix']} ({vix.get('label','')})")
    macro = mkt.get("upcoming_macro_events", [])
    if macro:
        parts.append("Macro events this week: " + ", ".join(e["event"] for e in macro[:3]))
    if earnings and earnings.get("earnings_soon"):
        parts.append(f"⚠ EARNINGS IN <14 DAYS: {earnings.get('earnings_date')} | EPS est={earnings.get('eps_estimate')} | Rev est={earnings.get('revenue_estimate')}")
    return "\nMarket Context:\n" + "\n".join(f"  {p}" for p in parts) + "\n" if parts else ""


def decide(symbol: str, signals: dict, portfolio: dict) -> dict:
    research = signals.get("research", {})
    fin      = signals.get("financial_data", {})
    soc      = signals.get("social", {})
    mkt      = signals.get("market_context", {})
    earnings = signals.get("earnings", {})

    research_block = ""
    if research.get("snippets"):
        lines = "\n".join(f"  - {s}" for s in research["snippets"][:10])
        research_block = f"\nWeb Research ({research.get('source_count', 0)} sources):\n{lines}\n"

    fin_block = _format_financial_data(fin)
    soc_block = _format_social(soc)
    mkt_block = _format_market_context(mkt, earnings)

    core_signals = {k: v for k, v in signals.items() if k not in ("research", "financial_data", "social", "market_context", "earnings")}

    prompt = f"""
Ticker: {symbol}
Portfolio: equity=${portfolio.get('equity', 0):,.2f}  cash=${portfolio.get('cash', 0):,.2f}
Open positions: {portfolio.get('position_count', 0)} / {config.MAX_POSITIONS}
Options exposure: {portfolio.get('options_pct', 0):.1f}% / {config.MAX_OPTIONS_PCT}%
Crypto exposure:  {portfolio.get('crypto_pct', 0):.1f}% / {config.MAX_CRYPTO_PCT}%

Core Signals:
{json.dumps(core_signals, indent=2, default=str)}
{fin_block}{soc_block}{mkt_block}{research_block}
{_SCHEMA}
"""

    response = _client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "action": "HOLD", "confidence": 0, "allocation_pct": 0,
            "asset_type": "stock", "option_direction": None,
            "rationale": "JSON parse error — defaulting to HOLD",
        }
