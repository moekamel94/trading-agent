import json
import anthropic
import config

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

_SYSTEM = """You are Kimmy, a disciplined position/swing trading portfolio manager.
Goal: maximize profit on multi-day to multi-month holds. Never day-trade.

You receive a complete picture — 15+ data sources synthesized into a unified view of
each stock: technicals, fundamentals, financial data (Finnhub/AV/FMP/Polygon), analyst
consensus, DCF valuation, social sentiment, web research, congress/insider activity,
future growth score, and macro context.

Your task: read the holistic synthesis and all raw signals, then make ONE decision.
No single signal rules. Weigh the full picture — bull signals vs bear signals vs risks.

BASKET CONTEXT:
Stocks come from S&P 500, Nasdaq-100 (QQQ), and component holdings of:
• QTUM — Defiance Quantum ETF (quantum computing, semiconductors, photonics)
• BOTT — ProShares Robotics & AI ETF (robotics, automation, artificial intelligence)
• SPWO — Global ex-US ETF (international ADRs: TSM, BABA, NVS, etc.)
Congress-bought tickers are also auto-added and carry extra conviction weight.

═══════════════════════════════════════════════════════
HARD BLOCKS — these alone stop a BUY (very few, true extremes only):
• Market extreme fear: Fear & Greed < 15
• RSI below 25 or above 78
• Earnings in ≤ 3 days (binary event risk)
• Congress selling + negative sentiment simultaneously
═══════════════════════════════════════════════════════

═══════════════════════════════════════════════════════
HIGH-GROWTH STOCKS (Future Growth Score >= 70):
• Can have high P/E — they are growing INTO their valuation
• PEG < 1.5 = buying growth cheaply even if P/E looks high
• Rule of 40 >= 40 = healthy growth tech company
• HOLD longer — don't sell on normal pullbacks if thesis is intact
• Prioritize: revenue acceleration, expanding margins, earnings beats
• Wider stop-loss justified — growth stocks compound fast
═══════════════════════════════════════════════════════

═══════════════════════════════════════════════════════
CONVICTION BOOSTERS (raise your confidence score):
• Congress buying last 60 days → strong conviction signal
• Net insider buying (Form 4) → positive signal
• Analyst consensus buy/strong buy → +conviction
• Analyst price target > current + 15% → upside confirmed
• DCF value above current price → fundamentally undervalued
• Social sentiment bullish → retail momentum behind it
• Golden cross active → technical tailwind
• High growth score in tailwind sector → structural edge
═══════════════════════════════════════════════════════

═══════════════════════════════════════════════════════
EXIT SIGNALS — sell on real signals, not arbitrary targets:
• Stop loss: −8% from entry (handled by risk manager)
• Dead money: held > 90 days AND profit < +3% → SELL
• On PROFITABLE positions — need 2 bearish signals to SELL:
  - RSI > 80 | MACD bearish | Death cross | BB upper + MACD bearish
• Earnings in < 5 days AND position > +10% profit → SELL (lock in)
• Analyst target cut below current price → SELL
• Congress net selling this ticker → SELL
• 3+ insider sell filings in one week → SELL
NO fixed take-profit ceiling — let winners run.
═══════════════════════════════════════════════════════

═══════════════════════════════════════════════════════
BTC/USD — pure momentum + macro, no fundamentals:
• Hard blocks: VIX > 38, F&G < 20, RSI < 30 or > 75, death cross
• Must pass: 2 of 3 momentum checks, price above SMA50 OR SMA200
• Exit: RSI > 82 + MACD bearish, or death cross
═══════════════════════════════════════════════════════

═══════════════════════════════════════════════════════
EARNINGS MOMENTUM SIGNAL (earnings_momentum):
You now receive the actual most-recent quarterly earnings result for every stock.
Use this as a strong directional signal:

• strong_beat (surprise ≥ +5%)  → STRONG BUY signal — momentum is real
  - Combined with bullish tech/analyst → raise confidence +1
  - Company is executing; institutions will re-rate upward
• beat (surprise 0 to +5%)      → mild positive boost to conviction
• in_line                        → neutral, no adjustment
• miss (surprise 0 to -5%)      → bearish flag — reduce confidence, consider HOLD over BUY
• strong_miss (surprise ≤ -5%)  → STRONG SELL signal if holding
  - If you do not hold it → DO NOT BUY regardless of other signals
  - Earnings misses trigger institutional selling for weeks

Trend matters too:
• consistent_beats (3+ quarters) → structural earnings quality, very bullish
• consistent_misses              → avoid the stock, thesis is broken

If earnings_momentum label is bearish or strong_bearish AND you hold the position → SELL.
If earnings_momentum label is strong_bullish → treat like a conviction booster (+1 confidence).
═══════════════════════════════════════════════════════

═══════════════════════════════════════════════════════
GLOBAL MACRO / GEOPOLITICAL SIGNAL (market_context.macro_momentum):
You now receive a real-time scan of global macro and geopolitical news.
This is a market-wide signal — it affects ALL positions and ALL buy decisions.

• risk_off (score ≤ -0.15) — DANGER MODE:
  Themes like: war/conflict, sanctions, tariffs, oil shock, rate hikes, recession risk
  → Do NOT open new positions unless conviction is 9+
  → Consider reducing or exiting positions with weak fundamentals
  → Raise cash. Capital preservation > returns during geopolitical shocks
  → Example: US-Iran war escalation → oil spike → tech sell-off → HOLD or SELL

• neutral — Normal operations. Proceed with regular signals.

• risk_on (score ≥ +0.15) — TAILWIND MODE:
  Themes like: ceasefire, trade deal, rate cut, stimulus
  → Normal or slightly more aggressive positioning
  → Conviction boosters apply normally

Specific scenario rules:
- Active war/conflict headlines + energy/oil themes → energy stocks may be BUY, tech = HOLD/SELL
- Fed rate cut confirmed → growth/tech stocks strong BUY signal
- Trade war / tariff escalation → domestic US stocks less affected, global ADRs (SPWO) = risky
- Sanctions on major economy → evaluate sector exposure (semiconductors = risk if China tensions)
═══════════════════════════════════════════════════════

═══════════════════════════════════════════════════════
POSITION SIZING:
• Confidence 7 → 4% | 8 → 5% | 9 → 6% | 10 → 8%
• Congress buying bonus: +2% | Insider buying bonus: +1%
• Hard cap: 8% per position | Max 15 open positions
• Max 20% crypto, 20% options
═══════════════════════════════════════════════════════

HOLD is ALWAYS the safe default. Only BUY when multiple signals clearly agree.
Return valid JSON only — no prose, no markdown fences."""

_SCHEMA = """
Return exactly this JSON:
{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": <integer 1-10>,
  "allocation_pct": <float 0.0-8.0>,
  "asset_type": "stock" | "crypto" | "option",
  "option_direction": "call" | "put" | null,
  "rationale": "<one concise sentence stating which signals drove the decision>"
}
"""


def _build_synthesis(symbol: str, signals: dict) -> str:
    """
    Pre-process ALL signals into a unified bull/bear/risk view.
    This is shown to Claude so it synthesizes holistically instead of
    scanning 15 separate data blocks independently.
    """
    tech    = signals.get("technical", {})
    fund    = signals.get("fundamentals", {})
    sent    = signals.get("sentiment", {})
    cong    = signals.get("congressional", {})
    insd    = signals.get("insider", {})
    soc     = signals.get("social", {})
    mkt     = signals.get("market_context", {})
    earn    = signals.get("earnings", {})
    fin     = signals.get("financial_data", {}) or {}
    growth  = signals.get("future_growth", {})
    research = signals.get("research", {})

    bull = []
    bear = []
    risks = []

    # ── Technical ─────────────────────────────────────────────────────────────
    rsi = tech.get("rsi")
    if rsi is not None:
        if rsi < 40:    bear.append(f"RSI={rsi:.0f} (weak/oversold)")
        elif rsi > 65:  bear.append(f"RSI={rsi:.0f} (elevated, overbought risk)")
        else:           bull.append(f"RSI={rsi:.0f} (healthy range)")

    if tech.get("golden_cross"):  bull.append("golden cross active (SMA50 > SMA200)")
    if tech.get("death_cross"):   bear.append("death cross active (SMA50 < SMA200)")

    macd = tech.get("macd_cross")
    if macd == "bullish":   bull.append("MACD bullish crossover")
    elif macd == "bearish": bear.append("MACD bearish crossover")

    price  = tech.get("price")
    sma50  = tech.get("sma50")
    sma200 = tech.get("sma200")
    if price and sma50:
        if price > sma50:   bull.append(f"price above SMA50 (${sma50:.0f})")
        else:               bear.append(f"price below SMA50 (${sma50:.0f})")
    if price and sma200:
        if price > sma200:  bull.append(f"price above SMA200 (${sma200:.0f})")
        else:               bear.append(f"price below SMA200 (${sma200:.0f})")

    r1m = tech.get("return_1m")
    r3m = tech.get("return_3m")
    if r1m is not None:
        if r1m > 5:     bull.append(f"strong 1-month return +{r1m:.1f}%")
        elif r1m < -5:  bear.append(f"weak 1-month return {r1m:.1f}%")
    if r3m is not None:
        if r3m > 10:    bull.append(f"strong 3-month trend +{r3m:.1f}%")
        elif r3m < 0:   bear.append(f"negative 3-month trend {r3m:.1f}%")

    vr = tech.get("volume_ratio")
    if vr and vr > 1.3: bull.append(f"volume surge {vr:.1f}x 20-day avg")

    bb = tech.get("bb_position")
    if bb == "above_upper": bear.append("at Bollinger upper band (short-term overbought)")

    # ── Fundamentals ──────────────────────────────────────────────────────────
    eps = fund.get("eps_growth_yoy")
    if eps is not None:
        if eps > 20:    bull.append(f"strong EPS growth +{eps:.0f}% YoY")
        elif eps > 0:   bull.append(f"positive EPS growth +{eps:.0f}%")
        else:           bear.append(f"EPS declining {eps:.0f}%")

    rev = fund.get("revenue_growth")
    if rev is not None:
        if rev > 20:    bull.append(f"strong revenue growth +{rev:.0f}%")
        elif rev > 0:   bull.append(f"positive revenue growth +{rev:.0f}%")
        else:           bear.append(f"revenue declining {rev:.0f}%")

    pe = fund.get("pe_ratio")
    g_score = growth.get("score", 0)
    if pe is not None:
        if pe < 20 and pe > 0:          bull.append(f"cheap valuation P/E={pe:.0f}")
        elif pe > 80 and g_score < 70:  bear.append(f"expensive P/E={pe:.0f} (not justified by growth)")

    margin = fund.get("profit_margin")
    if margin is not None:
        if margin > 15:  bull.append(f"strong margin {margin:.0f}%")
        elif margin < 5: bear.append(f"thin/negative margin {margin:.0f}%")

    # ── Financial data ────────────────────────────────────────────────────────
    fmp = fin.get("fmp", {})
    if fmp:
        dcf = fmp.get("dcf_value")
        if dcf and price and price > 0:
            upside = (dcf - price) / price * 100
            if upside > 15:     bull.append(f"DCF=${dcf:.0f} — stock undervalued by {upside:.0f}%")
            elif upside < -10:  bear.append(f"DCF=${dcf:.0f} — stock overvalued by {abs(upside):.0f}%")

    fh = fin.get("finnhub", {})
    recs = (fh or {}).get("analyst_recommendations", {})
    if recs:
        total = sum([recs.get("strong_buy", 0), recs.get("buy", 0),
                     recs.get("hold", 0), recs.get("sell", 0), recs.get("strong_sell", 0)])
        buys = recs.get("strong_buy", 0) + recs.get("buy", 0)
        if total > 0:
            buy_pct = buys / total
            if buy_pct > 0.60:  bull.append(f"analysts bullish ({buys}/{total} buy ratings)")
            elif buy_pct < 0.30: bear.append(f"analysts bearish (only {buys}/{total} buy ratings)")

    av = fin.get("alpha_vantage", {})
    if av and price and price > 0:
        at = av.get("analyst_target")
        if at:
            try:
                at_f = float(at)
                upside = (at_f - price) / price * 100
                if upside > 15:    bull.append(f"analyst price target ${at} (+{upside:.0f}% upside)")
                elif upside < -5:  bear.append(f"analyst target ${at} below current price")
            except (ValueError, TypeError):
                pass

    # ── Future Growth Score ───────────────────────────────────────────────────
    if g_score >= 70:
        g_cls = growth.get("classification", "").replace("_", " ")
        bull.append(f"HIGH GROWTH company — score {g_score}/100 ({g_cls})")
        winds = growth.get("tailwinds", [])
        if winds: bull.append(f"structural tailwinds: {', '.join(winds)}")
        peg = growth.get("peg_ratio")
        if peg and 0 < peg < 1.5: bull.append(f"PEG={peg:.1f} — buying growth cheaply")
        r40 = growth.get("rule_of_40")
        if r40 and r40 >= 40: bull.append(f"Rule of 40 = {r40:.0f} (healthy growth company)")
        beat = growth.get("beat_rate_pct")
        trend = growth.get("surprise_trend", "")
        if beat and beat >= 75: bull.append(f"earnings beat rate {beat:.0f}% ({trend} trend)")
        upside_g = growth.get("target_upside")
        if upside_g and upside_g > 20: bull.append(f"analyst consensus target upside {upside_g:.0f}%")
    elif g_score >= 50:
        bull.append(f"steady compounder — growth score {g_score}/100")
        winds = growth.get("tailwinds", [])
        if winds: bull.append(f"in tailwind sectors: {', '.join(winds)}")
    elif 0 < g_score < 35:
        bear.append(f"low/declining growth company — score {g_score}/100")

    # ── Congress & Insider ────────────────────────────────────────────────────
    cong_sig = cong.get("net_signal")
    if cong_sig == "bullish":   bull.append("congress members NET BUYING (last 60 days) — strong conviction signal")
    elif cong_sig == "bearish": bear.append("congress members NET SELLING — insider government signal")

    insd_sig = insd.get("net_signal")
    if insd_sig == "bullish":   bull.append("corporate insiders net buying (Form 4)")
    elif insd_sig == "bearish": bear.append("corporate insiders net selling (Form 4)")

    # ── Sentiment & Social ────────────────────────────────────────────────────
    sent_label = sent.get("label")
    if sent_label == "positive": bull.append("news/web sentiment positive")
    elif sent_label == "negative": bear.append("news/web sentiment negative")

    soc_label = (soc or {}).get("combined_label")
    if soc_label == "bullish":   bull.append("social sentiment bullish (Reddit/StockTwits)")
    elif soc_label == "bearish": bear.append("social sentiment bearish")

    # ── Market Context ────────────────────────────────────────────────────────
    fg = (mkt.get("fear_and_greed") or {}).get("score")
    vix_val = (mkt.get("vix") or {}).get("vix")
    if fg is not None:
        if fg > 70:    bear.append(f"extreme market greed (F&G={fg}) — crowded, late-cycle risk")
        elif fg > 55:  bear.append(f"market elevated greed (F&G={fg})")
        elif fg < 25:  bear.append(f"market fear (F&G={fg}) — macro headwind for new buys")
        else:          bull.append(f"market sentiment healthy (F&G={fg})")
    if vix_val:
        if vix_val > 25:  bear.append(f"elevated VIX={vix_val:.1f} — volatility risk")
        elif vix_val < 18: bull.append(f"low VIX={vix_val:.1f} — calm market environment")

    # ── Earnings risk ─────────────────────────────────────────────────────────
    if (earn or {}).get("earnings_soon"):
        days_to = earn.get("days_to_earnings", "soon")
        risks.append(f"EARNINGS IN {days_to} DAYS — binary event, elevated risk")
        eps_est = earn.get("eps_estimate")
        rev_est = earn.get("revenue_estimate")
        if eps_est or rev_est:
            risks.append(f"  estimates: EPS={eps_est} Rev={rev_est}")

    # ── Research highlights ───────────────────────────────────────────────────
    snips = (research or {}).get("snippets", [])
    if snips:
        bull_keywords = {"surge", "beat", "record", "growth", "launch", "partner",
                         "upgrade", "buy", "strong", "wins", "raises", "expand"}
        bear_keywords = {"miss", "decline", "loss", "lawsuit", "downgrade",
                         "cut", "risk", "fraud", "probe", "recall", "warns"}
        for s in snips[:8]:
            sl = s.lower()
            if any(w in sl for w in bull_keywords): bull.append(f"news: {s[:90]}")
            elif any(w in sl for w in bear_keywords): bear.append(f"news: {s[:90]}")
        # Deduplicate (same snippet shouldn't appear twice)
        bull = list(dict.fromkeys(bull))
        bear = list(dict.fromkeys(bear))

    # ── Build output ──────────────────────────────────────────────────────────
    lines = [f"=== HOLISTIC SYNTHESIS: {symbol} ==="]

    if bull:
        lines.append("\nBULL SIGNALS:")
        for b in bull: lines.append(f"  + {b}")

    if bear:
        lines.append("\nBEAR SIGNALS:")
        for b in bear: lines.append(f"  - {b}")

    if risks:
        lines.append("\nRISK FLAGS:")
        for r in risks: lines.append(f"  ! {r}")

    bull_n = len(bull)
    bear_n = len(bear)
    if bull_n == 0 and bear_n == 0:
        balance = "INSUFFICIENT DATA"
    elif bear_n == 0 or bull_n >= bear_n * 2.5:
        balance = "STRONGLY BULLISH"
    elif bull_n >= bear_n * 1.5:
        balance = "BULLISH"
    elif bull_n == 0 or bear_n >= bull_n * 2.5:
        balance = "STRONGLY BEARISH"
    elif bear_n >= bull_n * 1.5:
        balance = "BEARISH"
    else:
        balance = "MIXED — use judgment"

    lines.append(f"\nSignal balance: {bull_n} bull vs {bear_n} bear -> {balance}")
    lines.append("=" * 50)

    return "\n".join(lines)


def _format_raw_signals(signals: dict) -> str:
    """Compact view of all raw signal data — for Claude to reference details."""
    tech   = signals.get("technical", {})
    fund   = signals.get("fundamentals", {})
    fin    = signals.get("financial_data", {}) or {}
    soc    = signals.get("social", {})
    mkt    = signals.get("market_context", {})
    earn   = signals.get("earnings", {})
    growth = signals.get("future_growth", {})

    parts = []

    # Technical snapshot
    t_items = []
    for k in ("price", "rsi", "macd_cross", "bb_position", "golden_cross", "death_cross",
               "sma50", "sma200", "return_1m", "return_3m", "volume_ratio"):
        v = tech.get(k)
        if v is not None: t_items.append(f"{k}={v}")
    if t_items: parts.append("Technical: " + " | ".join(t_items))

    # Fundamentals snapshot
    f_items = []
    for k in ("eps_growth_yoy", "revenue_growth", "pe_ratio", "profit_margin"):
        v = fund.get(k)
        if v is not None: f_items.append(f"{k}={v}")
    if f_items: parts.append("Fundamentals: " + " | ".join(f_items))

    # Growth snapshot
    g_items = []
    for k in ("score", "classification", "peg_ratio", "forward_pe", "revenue_growth",
               "earnings_growth", "rule_of_40", "target_upside", "beat_rate_pct",
               "surprise_trend", "gross_margin"):
        v = growth.get(k)
        if v is not None: g_items.append(f"{k}={v}")
    if growth.get("tailwinds"): g_items.append(f"tailwinds={growth['tailwinds']}")
    if growth.get("breakdown"):
        bd = growth["breakdown"]
        g_items.append(f"breakdown=momentum:{bd.get('growth_momentum',0)}/30 quality:{bd.get('growth_quality',0)}/25 analysts:{bd.get('analyst_conviction',0)}/25 execution:{bd.get('earnings_execution',0)}/20")
    if g_items: parts.append("Future Growth: " + " | ".join(str(x) for x in g_items))

    # Financial data
    fh = fin.get("finnhub", {})
    if fh:
        q    = fh.get("quote", {})
        recs = fh.get("analyst_recommendations", {})
        news = fh.get("news_headlines", [])
        fh_parts = []
        if q.get("current_price"): fh_parts.append(f"price=${q['current_price']} ({q.get('pct_change_day','?')}%)")
        if recs: fh_parts.append(f"recs: {recs.get('strong_buy',0)}SB/{recs.get('buy',0)}B/{recs.get('hold',0)}H/{recs.get('sell',0)}S")
        if news: fh_parts.append("news: " + " | ".join(news[:2]))
        if fh_parts: parts.append("Finnhub: " + " — ".join(fh_parts))

    fmp = fin.get("fmp", {})
    if fmp:
        fmp_parts = []
        if fmp.get("dcf_value"):     fmp_parts.append(f"DCF=${fmp['dcf_value']:.0f}")
        if fmp.get("pe_ratio"):      fmp_parts.append(f"P/E={fmp['pe_ratio']}")
        if fmp.get("revenue_growth") is not None: fmp_parts.append(f"rev_growth={fmp['revenue_growth']}%")
        if fmp.get("roe"):           fmp_parts.append(f"ROE={fmp['roe']:.1%}")
        if fmp_parts: parts.append("FMP: " + " | ".join(fmp_parts))

    av = fin.get("alpha_vantage", {})
    if av:
        av_parts = []
        if av.get("rsi"):             av_parts.append(f"RSI={av['rsi']}")
        if av.get("analyst_target"):  av_parts.append(f"target=${av['analyst_target']}")
        if av_parts: parts.append("AlphaVantage: " + " | ".join(av_parts))

    poly = fin.get("polygon", {})
    if poly:
        po_parts = []
        if poly.get("vwap"):        po_parts.append(f"VWAP={poly['vwap']}")
        if poly.get("prev_volume"): po_parts.append(f"vol={poly['prev_volume']:,.0f}")
        if po_parts: parts.append("Polygon: " + " | ".join(po_parts))

    # Social
    st = (soc or {}).get("stocktwits", {})
    rd = (soc or {}).get("reddit", {})
    soc_parts = []
    if st: soc_parts.append(f"StockTwits={st.get('label','?')} ({st.get('bull_pct','?')}% bull)")
    if rd: soc_parts.append(f"Reddit={rd.get('label','?')} {rd.get('mention_count',0)} mentions")
    combined = (soc or {}).get("combined_label")
    if combined: soc_parts.insert(0, f"combined={combined}")
    if soc_parts: parts.append("Social: " + " | ".join(soc_parts))

    # Market
    fg   = (mkt.get("fear_and_greed") or {}).get("score")
    vix  = (mkt.get("vix") or {}).get("vix")
    mkt_parts = []
    if fg is not None: mkt_parts.append(f"F&G={fg}")
    if vix:            mkt_parts.append(f"VIX={vix}")
    macro = mkt.get("upcoming_macro_events", [])
    if macro: mkt_parts.append("macro: " + ", ".join(e["event"] for e in macro[:2]))
    if mkt_parts: parts.append("Market: " + " | ".join(str(x) for x in mkt_parts))

    # Macro momentum (geopolitical/global)
    macro_mom = mkt.get("macro_momentum") or {}
    if macro_mom.get("available"):
        mm_parts = [f"label={macro_mom.get('label','?')}", f"score={macro_mom.get('score','?')}"]
        if macro_mom.get("themes"):
            mm_parts.append("themes: " + ", ".join(macro_mom["themes"][:3]))
        if macro_mom.get("top_headlines"):
            mm_parts.append("headline: " + macro_mom["top_headlines"][0][:100])
        parts.append("GlobalMacro: " + " | ".join(mm_parts))

    # Earnings
    if (earn or {}).get("earnings_soon"):
        parts.append(f"Earnings: date={earn.get('earnings_date')} eps_est={earn.get('eps_estimate')} rev_est={earn.get('revenue_estimate')}")

    # Earnings momentum (actual results + news)
    em = signals.get("earnings_momentum") or {}
    if em.get("available"):
        em_parts = [f"label={em.get('label','?')}", f"score={em.get('combined_score','?')}"]
        eps = em.get("eps_surprise") or {}
        if eps.get("label") and eps["label"] != "no_data":
            em_parts.append(f"EPS_surprise={eps.get('label')}({eps.get('surprise_pct','?')}%)")
        if eps.get("trend"):
            em_parts.append(f"trend={eps['trend']}")
        news = em.get("news_sentiment") or {}
        if news.get("top_headlines"):
            em_parts.append("headline: " + news["top_headlines"][0][:100])
        parts.append("EarningsMomentum: " + " | ".join(em_parts))

    # Research snippets
    snips = (signals.get("research") or {}).get("snippets", [])
    if snips:
        parts.append("Research (" + str(signals["research"].get("source_count", 0)) + " sources): " +
                     " || ".join(snips[:5]))

    return "\nRaw Data:\n" + "\n".join(f"  {p}" for p in parts) + "\n" if parts else ""


def decide(symbol: str, signals: dict, portfolio: dict) -> dict:
    synthesis = _build_synthesis(symbol, signals)
    raw_block = _format_raw_signals(signals)

    core_signals = {k: v for k, v in signals.items()
                    if k not in ("research", "financial_data", "social",
                                 "market_context", "earnings", "earnings_momentum",
                                 "future_growth")}

    prompt = f"""
Ticker: {symbol}
Portfolio: equity=${portfolio.get('equity', 0):,.2f}  cash=${portfolio.get('cash', 0):,.2f}
Open positions: {portfolio.get('position_count', 0)} / {config.MAX_POSITIONS}
Options exposure: {portfolio.get('options_pct', 0):.1f}% / {config.MAX_OPTIONS_PCT}%
Crypto exposure:  {portfolio.get('crypto_pct', 0):.1f}% / {config.MAX_CRYPTO_PCT}%

{synthesis}
{raw_block}
Core signal detail:
{json.dumps(core_signals, indent=2, default=str)}
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
