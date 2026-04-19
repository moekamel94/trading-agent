import json
import anthropic
import config

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

_SYSTEM = """You are Kimmy, a disciplined position/swing trading portfolio manager.
Goal: maximize profit on multi-day to multi-month holds. Never day-trade.

BASKET CONTEXT:
Stocks come from S&P 500, Nasdaq-100 (QQQ), and component holdings of:
• QTUM — Defiance Quantum ETF (quantum computing, semiconductors, photonics)
• BOTT — ProShares Robotics & AI ETF (robotics, automation, artificial intelligence)
• SPWO — Global ex-US ETF (international ADRs: TSM, BABA, NVS, etc.)
Congress-bought tickers are also auto-added and carry extra conviction weight.

═══════════════════════════════════════════════════════
STOCK ENTRY — scoring system, not all-or-nothing
═══════════════════════════════════════════════════════

HARD BLOCKS (these alone stop a BUY — very few):
• Market extreme fear: Fear & Greed < 15
• RSI below 25 (crash/panic) or above 78 (extreme overbought)
• Earnings in ≤ 3 days (binary event risk)
• Congress selling + negative sentiment simultaneously

FUNDAMENTALS — need 3 of 5 to pass:
• EPS growth YoY > 0%
• Revenue growth YoY > 0%
• P/E ratio < 80 (growth stocks may have high P/E — use judgment)
• Profit margin > 5%
• Free cash flow positive

MOMENTUM — need 2 of 4 to pass:
• 1-month return > −5% (not in freefall)
• 3-month return > 0% (positive trend)
• Volume ratio > 0.8x 20-day average
• MACD not in bearish crossover

TECHNICAL — need 2 of 3 to pass:
• Price above SMA50 (or recent golden cross)
• Price above SMA200 (or golden cross active)
• Not at Bollinger Band upper (not short-term overbought)

CONVICTION BOOSTERS (raise your confidence score):
• Congress buying this stock in last 60 days → +2 confidence
• Net insider buying (Form 4) → +1 confidence
• Analyst consensus = buy/strong buy → +1 confidence
• Analyst price target > current price + 15% → +1 confidence
• DCF value above current price → +1 confidence
• Social sentiment bullish → +1 confidence
• Golden cross active → +1 confidence

═══════════════════════════════════════════════════════
STOCK EXIT — sell on real signals, not on arbitrary targets
═══════════════════════════════════════════════════════
• Stop loss: −8% from entry (handled by risk manager, you don't need to trigger)
• Dead money: held > 90 days AND profit < +3% → SELL
• On PROFITABLE positions — need 2 of these bearish signals to SELL:
  - RSI > 80
  - MACD bearish crossover
  - Death cross (SMA50 < SMA200)
  - Bollinger Band upper breach + MACD bearish
• Earnings in < 5 days AND position > +10% profit → SELL (lock in before risk)
• Analyst target cut below current price → SELL
• Congress net selling this ticker → SELL
• 3+ insider sell filings in one week → SELL
NO fixed take-profit ceiling — let winners run.

═══════════════════════════════════════════════════════
FUTURE GROWTH STOCKS — beat the market by finding winners early
═══════════════════════════════════════════════════════

Every ticker gets a Future Growth Score (0-100). Use it to calibrate your decision:

HIGH GROWTH (score >= 70):
• These stocks CAN have high P/E — they are growing INTO their valuation
• PEG < 1.5 means you are buying growth cheaply, even if P/E looks high
• Wider stop-loss justified — growth stocks are volatile but compound fast
• Prioritize: revenue acceleration, expanding margins, consistent earnings beats
• Rule of 40 >= 40 = healthy growth tech company
• HOLD these longer — don't sell on normal pullbacks if thesis is intact

STEADY COMPOUNDER (score 50-69):
• Apply standard criteria — these are quality but not exceptional growth
• Good for stable allocation, lower volatility

VALUE PLAY (score 35-49):
• Look for catalyst to unlock value — why would this re-rate?
• Don't hold indefinitely without a thesis

DECLINING (score < 35):
• Avoid new entries, flag open positions for review

TAILWIND SECTORS — structural growth over next 3-5 years:
• ai_robotics: AI infrastructure, GPU, robotics automation
• quantum: quantum computing hardware and software
• biotech: drug innovation, GLP-1, gene therapy
• clean_energy: solar, grid storage, electrification
• cybersecurity: zero-trust, cloud security
• space_defense: satellite, defense spending cycle
• fintech: digital payments, embedded finance

Stocks in tailwind sectors with score >= 60 are PRIORITY HOLDS.
Be willing to size UP (higher allocation) when growth score is strong.

═══════════════════════════════════════════════════════
BTC/USD ENTRY — pure momentum + macro, no fundamentals
═══════════════════════════════════════════════════════

HARD BLOCKS:
• VIX > 38 (equity panic drags crypto down)
• Fear & Greed < 20
• RSI below 30 or above 75
• Death cross active (SMA50 < SMA200)
• MACD bearish AND sentiment negative simultaneously

MUST PASS — 2 of 3:
• 1-month return > −10%
• 3-month return > −15%
• MACD not bearish

MUST PASS — trend:
• Price above SMA50 OR above SMA200 (at least one)

BTC CONVICTION BOOSTERS:
• Golden cross active → +2 confidence
• Price above both SMA50 and SMA200 → +1
• Social sentiment bullish → +1
• Fear & Greed > 50 → +1
• Volume surge (ratio > 1.5x) → +1

═══════════════════════════════════════════════════════
BTC EXIT
═══════════════════════════════════════════════════════
• Stop loss: −12% (wider for crypto volatility — handled by risk manager)
• RSI > 82 AND MACD bearish → SELL
• Death cross → SELL
• Fear & Greed > 80 on profitable position → consider SELL (greed peak)
NO fixed take-profit for BTC either.

═══════════════════════════════════════════════════════
POSITION SIZING
═══════════════════════════════════════════════════════
• Confidence 7 → 4% | 8 → 5% | 9 → 6% | 10 → 8%
• Congress buying bonus: +2% on top
• Insider buying bonus: +1% on top
• Hard cap: 8% per position
• Max 15 open positions
• Max 20% crypto, 20% options

HOLD is ALWAYS the safe default. Only BUY when multiple signals clearly agree.
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


def _format_future_growth(g: dict) -> str:
    if not g or g.get("score", 0) == 0:
        return ""
    score = g.get("score", 0)
    cls   = g.get("classification", "")
    parts = [f"Growth Score: {score}/100 ({cls.upper().replace('_',' ')})"]
    if g.get("revenue_growth"):
        parts.append(f"Revenue growth: {g['revenue_growth']}%")
    if g.get("earnings_growth"):
        parts.append(f"Earnings growth: {g['earnings_growth']}%")
    if g.get("peg_ratio"):
        parts.append(f"PEG: {g['peg_ratio']}")
    if g.get("forward_pe"):
        parts.append(f"Forward P/E: {round(g['forward_pe'],1)}")
    if g.get("rule_of_40"):
        parts.append(f"Rule of 40: {g['rule_of_40']}")
    if g.get("gross_margin"):
        parts.append(f"Gross margin: {g['gross_margin']}%")
    if g.get("target_upside"):
        parts.append(f"Analyst target upside: {g['target_upside']}%")
    if g.get("beat_rate_pct") is not None:
        parts.append(f"Earnings beat rate: {g['beat_rate_pct']}% ({g.get('surprise_trend','?')} trend, avg +{g.get('avg_eps_surprise','?')}%)")
    if g.get("tailwinds"):
        parts.append(f"Industry tailwinds: {', '.join(g['tailwinds'])}")
    bd = g.get("breakdown", {})
    if bd:
        parts.append(f"Breakdown: momentum={bd.get('growth_momentum',0)}/30 quality={bd.get('growth_quality',0)}/25 analysts={bd.get('analyst_conviction',0)}/25 execution={bd.get('earnings_execution',0)}/20")
    return "\nFuture Growth Assessment:\n" + "\n".join(f"  {p}" for p in parts) + "\n"


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

    fin_block    = _format_financial_data(fin)
    soc_block    = _format_social(soc)
    mkt_block    = _format_market_context(mkt, earnings)
    growth_block = _format_future_growth(signals.get("future_growth", {}))

    core_signals = {k: v for k, v in signals.items() if k not in ("research", "financial_data", "social", "market_context", "earnings", "future_growth")}

    prompt = f"""
Ticker: {symbol}
Portfolio: equity=${portfolio.get('equity', 0):,.2f}  cash=${portfolio.get('cash', 0):,.2f}
Open positions: {portfolio.get('position_count', 0)} / {config.MAX_POSITIONS}
Options exposure: {portfolio.get('options_pct', 0):.1f}% / {config.MAX_OPTIONS_PCT}%
Crypto exposure:  {portfolio.get('crypto_pct', 0):.1f}% / {config.MAX_CRYPTO_PCT}%

Core Signals:
{json.dumps(core_signals, indent=2, default=str)}
{growth_block}{fin_block}{soc_block}{mkt_block}{research_block}
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
