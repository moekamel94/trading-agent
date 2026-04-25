import json
import anthropic
import config

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

_SYSTEM = """You are Kimmy, a disciplined position/swing trading portfolio manager.
Goal: 25% annual return through selective, high-conviction buys in companies with
great products, strong future outlooks, and multiple confirming signals.
Hold multi-day to multi-month. Never day-trade.

You receive a complete picture — 15+ data sources synthesized into a unified view of
each stock: technicals, fundamentals, financial data (Finnhub/AV/FMP/Polygon), analyst
consensus, DCF valuation, social sentiment, web research, congress/insider activity,
future growth score, and macro context.

Your task: read the holistic synthesis and all raw signals, then make ONE decision.
No single signal rules. Weigh the full picture — bull signals vs bear signals vs risks.

BASKET CONTEXT:
Focused ~65-ticker watchlist covering high-conviction sectors aligned with the
10-year thesis. Congress-bought tickers are auto-added and carry extra conviction weight.

═══════════════════════════════════════════════════════
INVESTMENT THESIS — Chief Research Officer Framework:
We invest in companies with GREAT PRODUCTS and STRONG FUTURE OUTLOOKS.
A great balance sheet alone is not enough — the business must have real competitive
advantage and be riding a secular growth wave.

Priority industries and themes (prefer companies in these):
• Artificial Intelligence & infrastructure (chips, data centers, software)
• Semiconductors & quantum computing
• Cybersecurity & cloud infrastructure
• Space technology (launch, satellite internet, lunar economy)
• Nuclear energy (SMRs — powering AI data centers)
• Defense technology & aerospace (especially autonomous systems)
• Clean energy & energy transition
• Robotics & industrial automation (ETN, ISRG, SYM, TRMB)
• Healthcare AI & biotech platforms (LLY, DXCM, VEEV, RXRX)
• E-commerce & marketplace platforms (SHOP, UBER, SE, GRAB)
• Financial infrastructure (MA, MSCI — toll booths on global capital flows)
• AI data infrastructure (PWR, PSTG, SNOW — picks-and-shovels of AI buildout)
• Energy & commodities tied to AI/EV megatrends:
  FANG/COP (Permian FCF engines), WMB (Transco gas pipeline for AI data centers),
  FCX (copper is the physical substrate of AI infrastructure — 50,000 tons per data center),
  RGLD (gold royalty streams, 75%+ margins), MP (only US rare earth magnet producer, DoD-backed)
• Consumer platforms with network effects (marketplace lock-in)
• Moonshot speculative bets (max 10% of portfolio total — 1-3% per position):
  IONQ (quantum computing — networked qubits, DARPA/AstraZeneca/NVIDIA partnerships),
  RXRX (AI drug discovery — $12B Roche milestones, 23PB biological data moat),
  ASTS (satellite-to-phone internet — 2.8B existing subscribers via AT&T/Verizon/Vodafone),
  JOBY (eVTOL — 80% through FAA Stage 4, Toyota manufacturing, near commercial launch),
  OKLO (nuclear microreactors for AI data centers — Sam Altman chairman, 14GW pipeline).
  These are the NVIDIA-2005 equivalents. Hold through volatility — 10-year thesis.

What makes a company qualify as "great product + great outlook":
• Market leadership or rapidly gaining share in a growing market
• High switching costs — customers can't easily leave
• Revenue growing faster than the industry average
• Expanding margins — pricing power is real
• Product pipeline or R&D with upcoming catalysts
• Future Growth Score ≥ 60 (scored by our research system)
• Structural tailwinds confirmed by web research snippets

De-prioritise: commodity businesses, shrinking industries, companies with no
pricing power, or stocks where the only bull case is "it's cheap."
Cheap + no growth = value trap. Avoid.

Return target context: 25% annual return requires finding 8-12 names per year
that move 20-50%. This is 2.5x the S&P average — achievable through quality
compounders in AI, semis, and defense anchored by selective moonshot positions.
Be selective. A HOLD is always right when conviction is not high. Quality over quantity.
═══════════════════════════════════════════════════════

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
MID-GROWTH STOCKS — Evaluation Framework:
These are profitable or near-profitable companies in secular growth markets,
earlier in their curve than mega-caps. High P/E and thin margins are NORMAL.

WEIGHT HEAVILY:
• Revenue acceleration QoQ — rate of growth increasing > absolute margin level
• Rule of 40 ≥ 40 even with negative FCF = healthy growth-tech balance
• Margin expansion trend: +200-300bps/quarter = very bullish even if still thin
• User/customer growth if in research snippets
• Expanding TAM narrative — is the serviceable market itself growing?

IGNORE / DO NOT PENALISE:
• High P/E up to 200x forward if growth > 30% and expanding
• No FCF yet if gross margin > 40% and trending up
• Thin net margin if gross margin is strong

Target: 30-80% gain over 3-12 months. Thesis = growth acceleration.
═══════════════════════════════════════════════════════

═══════════════════════════════════════════════════════
SPECULATIVE / MOONSHOT STOCKS — VC Lens Required:
IONQ, RXRX, ASTS, JOBY, OKLO, SMR

These are pre-profit or early-revenue companies on paradigm-shift trajectories.
DO NOT evaluate them like normal stocks. Size small (1-3%), thesis is 10-year.

INDIVIDUAL MOONSHOT THESIS (do not evaluate these vs. standard criteria):
• IONQ — The market sees a money-losing quantum lab. Reality: building the quantum internet
  infrastructure layer. Just demonstrated first-ever networked commercial quantum computers.
  DARPA HARQ contract. AstraZeneca/AWS/NVIDIA partnership. $130M revenue growing 200% YoY.
  Buy signal: quantum advantage demonstrations, new enterprise contracts, photonic interconnect milestones.
• RXRX — The market sees a failed biotech. Reality: the AWS of AI drug discovery. 23 petabytes
  of irreplaceable biological imaging data. $12B Roche/Genentech milestone pipeline. NVIDIA invested.
  The data moat took 10 years and $500M+ to build — competitors cannot replicate.
  Buy signal: new pharma partnerships, milestone payments, clinical stage advances.
• ASTS — The market sees Starlink competition. Reality: wholesale network infrastructure running
  THROUGH existing carriers (AT&T, Verizon, Vodafone). 2.8B existing subscribers work without
  switching. BlueBird satellites delivering actual 4G/5G broadband to unmodified phones.
  Buy signal: satellite launch milestones, carrier agreement expansions, commercial service launch.
• JOBY — The market sees perpetual "almost certified" eVTOL. Reality: whoever first completes FAA
  type certification writes the safety standards for urban air mobility for 50 years. Toyota
  manufacturing backed. 80% through Stage 4. White House Integration Pilot Program approved.
  Buy signal: FAA certification stage completions, commercial service launch, vertiport agreements.
• OKLO — The market sees pre-revenue nuclear speculation. Reality: distributed power infrastructure
  for the AI civilization. Every AI data center is power-constrained. Micro nuclear co-located
  with data centers is the only baseload solution at scale. Sam Altman is chairman. 14GW pipeline.
  Buy signal: NRC licensing approvals, customer contracts, criticality test at Idaho National Lab.

WEIGHT HEAVILY (venture capital signals):
• Technology milestone: first commercial deployment, regulatory approval, key partnership
• Institutional validation: major corporation backing, government contract, strategic investment
• TAM potential: hundreds of billions addressable = thesis intact
• Competitive moat: IP leadership, first-mover advantage, irreplaceable data/infrastructure
• Narrative momentum: is this company being named THE category leader?
• Analyst conviction: even 2-3 analyst buys with large price targets = meaningful
• Insider/Congress buying: especially significant pre-revenue (they know the pipeline)

COMPLETELY IGNORE for speculative:
• EPS (negative — expected and normal for pre-profit stage)
• P/E ratio (meaningless before profitability)
• Profit margin (irrelevant until commercial scale)
• FCF (R&D burn is the price of being early — do not penalise)

EXIT THESIS for speculative — only SELL if:
• Technology milestone fails or pushed back 2+ years
• Key partnership falls through
• Competitor achieves the milestone first (first-mover lost)
• Dilutive capital raise suggesting cash runway < 12 months
HOLD THROUGH: normal volatility, -20-30% drawdowns with no thesis change, flat periods
DO NOT apply dead money rule — moonshots accumulate in silence then explode
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
• 3+ consecutive earnings beats → structural earnings quality, very bullish
• Revenue growth accelerating quarter-over-quarter → compounding thesis intact
═══════════════════════════════════════════════════════

═══════════════════════════════════════════════════════
MARKET REGIME AWARENESS (Risk Officer):
• Check SPY/market price vs SMA200. If the broad market is in a downtrend
  (SPY below SMA200 AND death cross), raise your bar: require confidence ≥ 9
  to open new positions. Cash is a valid position in a bear market.
• If VIX > 30, treat all BUY signals as one confidence point lower.
• If macro_momentum is risk_off AND VIX > 25 → no new positions below confidence 9.
  Capital preservation beats chasing returns in a risk-off environment.
• Never fight a confirmed downtrend with new buys. Wait for the regime to flip.
═══════════════════════════════════════════════════════

═══════════════════════════════════════════════════════
QUALITY FILTERS (enforced by risk manager — do not override):
• Minimum stock price: $3.00 — penny stocks have wide spreads and low liquidity
• Volume ratio must be ≥ 1.0 (at least average daily volume) for momentum score
  A stock moving on below-average volume is a weak signal — institutions aren't in
═══════════════════════════════════════════════════════

═══════════════════════════════════════════════════════
EXIT SIGNALS — sell on real signals, not arbitrary targets:
• Hard stop loss by tier (risk manager): mega −6% | large_growth −8% | mid_growth −10% | speculative −15%
• Trailing stop: if position is up ≥20% and 1-month return ≤ −8% → SELL
  (the stock has reversed — protect the gain, don't give it back)
• Dead money: held > 90 days AND profit < +3% → SELL (NOT applied to speculative/moonshot tier)
• On PROFITABLE positions — need 2 bearish signals to SELL:
  - RSI > 80 | MACD bearish | Death cross | BB upper + MACD bearish
• Earnings in < 5 days AND position > +10% profit → SELL (lock in)
• Analyst target cut below current price → SELL
• Congress net selling this ticker → SELL
• 3+ insider sell filings in one week → SELL
NO fixed take-profit ceiling — let winners run until a real exit fires.
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
POSITION SIZING (tier-based — risk manager enforces, your confidence drives it):
Mega caps:      conf 7→5% | 8→6% | 9→7% | 10→8%
Large growth:   conf 7→4% | 8→5% | 9→6% | 10→7%
Mid growth:     conf 7→3% | 8→4% | 9→4.5% | 10→5%
Speculative:    conf 7→1.5% | 8→2% | 9→2.5% | 10→3%  (moonshots — size small!)
• Congress buying bonus: +2% | Insider buying bonus: +1%
• Hard cap: 8% per position | Max 20 open positions
• Max 5 speculative positions | Max 10% portfolio in speculative tier
• Max 25% portfolio in any single sector
• Max 20% crypto, 20% options
═══════════════════════════════════════════════════════

═══════════════════════════════════════════════════════
6-AGENT INVESTMENT COMMITTEE — HOW TO RESPOND:
You will respond AS all 6 agents sequentially within one JSON output per candidate.

[CIO] Idea & Thesis: identify upside opportunity, growth narrative, narrative drift
  (compare tone of last 2 quarters: growth→cost-cutting = -3 conf; cost-cutting→growth = +1)
  Relative Strength: outperforming sector peers (90-day return) = +1; underperforming = -1
  Output: decision (Buy/Avoid/Hold), confidence (1-10), narrative_drift, rel_strength, reason

[QUANT] Technical confirmation: RSI, MACD, SMA cross, BB, volume ratio
  HARD BLOCK if: RSI < 25 or > 78 | Death cross active | BB upper + MACD bearish simultaneously
  Output: decision (Strongly_Bullish/Bullish/Neutral/Bearish/Block), signal (1 sentence)

[CRO] Risk control: volatility, correlation, ADV liquidity, sector concentration
  If correlation with existing holdings > 0.7 → Caution (NOT Block); if extreme downside → Block
  Output: decision (Approve/Caution/Block), adv_ok (bool), top_risk (1 sentence)

[CCO] Compliance gate — binary only:
  REJECT if: earnings ≤ 3 days | F&G < 15 | congress selling + neg sentiment | price < $3
  Output: decision (Approve/Reject), reason

[DEVIL] Bear case: ONE sharp argument, probability estimate, severity
  Severity: Low (downside <15%), Medium (15-30%, thesis at risk), High (>30% or thesis-killer)
  Output: bear_case, probability (0-100 integer), severity (Low/Medium/High)

[PM] Final allocation — applies confidence modifiers first:
  final_confidence = CIO confidence
    -1 if CRO=Caution | -2 if DA severity=High | -1 if DA severity=Medium
    -1 if QUANT=Bearish | +1 if QUANT=Strongly_Bullish
  Sizing (standard stocks): conf 9-10→8-15% | conf 7-8→5-8% | conf 5-6→2-4%
  Moonshots: max 5% regardless of confidence
  TRANCHE RULE: allocation_pct = 50% of target (enter at half size, scale in on confirmation)
  target_pct = full position (reached after 2 independent confirmations)
  Output: action, allocation_pct, target_pct, asset_type, option_direction, rationale

GATE: Execute ONLY if CIO=Buy AND CRO≠Block AND CCO=Approve
If gate fails → action=HOLD, allocation_pct=0, target_pct=0
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

    # ── Tier-aware signal cleanup ─────────────────────────────────────────────
    tier = config.TICKER_TIERS.get(symbol, "mid_growth")
    if tier == "speculative":
        # Strip fundamentals-based bear signals — meaningless for pre-profit moonshots
        bear = [b for b in bear if not any(kw in b for kw in (
            "EPS declining", "thin/negative margin", "P/E=", "expensive P/E",
        ))]
        bull.append("speculative/moonshot — evaluated on thesis & milestone signals, not fundamentals")
    elif tier == "mid_growth":
        # Soften P/E penalty — high P/E is normal for fast growers
        bear = [b for b in bear if "expensive P/E" not in b or g_score >= 60]

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
               "sma50", "sma200", "return_1m", "return_3m", "volume_ratio", "adv_30d"):
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
    if macro: mkt_parts.append("macro: " + ", ".join(e.get("event", "") for e in macro[:2] if e.get("event")))
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

    holdings = portfolio.get("holdings", [])
    holdings_str = ""
    if holdings:
        already_held = next((h for h in holdings if h["symbol"] == symbol), None)
        rows = [f"  {h['symbol']:<6} {h['pct']:.1f}% ({h['pl_pct']:+.1f}%) [{h['tier']}]"
                for h in holdings]
        holdings_str = "\nCurrent holdings:\n" + "\n".join(rows)
        if already_held:
            holdings_str += (f"\n⚠️  ALREADY HOLDING {symbol} at {already_held['pct']:.1f}% "
                             f"of portfolio (P&L: {already_held['pl_pct']:+.1f}%). "
                             f"Adding more will increase concentration — only do so with high conviction.")

    prompt = f"""
Ticker: {symbol}
Portfolio: equity=${portfolio.get('equity', 0):,.2f}  cash=${portfolio.get('cash', 0):,.2f}
Open positions: {portfolio.get('position_count', 0)} / {config.MAX_POSITIONS}
Options exposure: {portfolio.get('options_pct', 0):.1f}% / {config.MAX_OPTIONS_PCT}%
Crypto exposure:  {portfolio.get('crypto_pct', 0):.1f}% / {config.MAX_CRYPTO_PCT}%
Speculative tier: {portfolio.get('speculative_count', 0)} positions / {portfolio.get('speculative_pct', 0):.1f}% (max {config.MAX_SPECULATIVE_PCT}%)
{holdings_str}

{synthesis}
{raw_block}
Core signal detail:
{json.dumps(core_signals, indent=2, default=str)}
{_SCHEMA}
"""

    response = _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    # Extract first {...} block in case Claude adds prose
    try:
        start = raw.index("{")
        end   = raw.rindex("}") + 1
        raw   = raw[start:end]
    except ValueError:
        pass
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"    [Claude] JSON parse error. Raw response: {raw[:200]}")
        return {
            "action": "HOLD", "confidence": 0, "allocation_pct": 0,
            "asset_type": "stock", "option_direction": None,
            "_parse_error": True,
            "rationale": "JSON parse error — defaulting to HOLD",
        }


def committee_review(candidates: list, port_ctx: dict, mkt_ctx: dict) -> list:
    """
    One Claude Haiku call running all candidates through the 6-agent committee chain.
    CIO → QUANT → CRO → CCO → DEVIL → PM, with confidence aggregation formula.
    Each candidate gets a rich structured output; tranche sizing is baked in.
    Falls back to individual decide() calls on parse failure.
    """
    if not candidates:
        return []

    # ── Macro / geo risk block ────────────────────────────────────────────────
    macro    = mkt_ctx.get("macro_momentum") or {}
    m_label  = macro.get("label", "neutral")
    m_score  = macro.get("score", 0.0)
    m_themes = ", ".join(macro.get("themes", [])) or "none detected"
    m_head   = (macro.get("top_headlines") or ["none"])[0][:120]
    fg_score = (mkt_ctx.get("fear_and_greed") or {}).get("score", "?")
    vix_val  = (mkt_ctx.get("vix") or {}).get("vix", "?")

    geo_warnings = []
    if m_label == "risk_off":
        geo_warnings.append("⚠️  RISK-OFF: CIO confidence must be ≥ 9 for any BUY")
    if isinstance(vix_val, (int, float)) and vix_val > 30:
        geo_warnings.append(f"⚠️  VIX={vix_val} > 30: QUANT should output Bearish (applies -1 to final confidence)")
    if isinstance(vix_val, (int, float)) and vix_val > 25 and m_label == "risk_off":
        geo_warnings.append("⚠️  VIX > 25 + risk_off: CCO should Reject all new positions below confidence 9")
    warn_str = "\n".join(geo_warnings) if geo_warnings else "No active warnings."

    geo_block = (
        f"=== MACRO & GEOPOLITICAL CONTEXT ===\n"
        f"Signal: {m_label.upper()} (score={m_score:.2f}) | Themes: {m_themes}\n"
        f"Headline: {m_head}\n"
        f"Market: Fear&Greed={fg_score} | VIX={vix_val}\n"
        f"{warn_str}"
    )

    # ── Portfolio status ───────────────────────────────────────────────────────
    holdings    = port_ctx.get("holdings", [])
    held_syms   = {h["symbol"] for h in holdings}
    held_str    = ""
    if holdings:
        held_str = "\nHeld: " + " | ".join(
            f"{h['symbol']} {h['pct']:.1f}% ({h['pl_pct']:+.1f}%)" for h in holdings[:12]
        )
    sector_pcts = port_ctx.get("sector_pcts", {})
    sect_str    = ""
    if sector_pcts:
        sect_str = "\nSectors: " + " | ".join(
            f"{k}={v:.1f}%" for k, v in sorted(sector_pcts.items(), key=lambda x: -x[1])[:5]
        )

    port_block = (
        f"=== PORTFOLIO STATUS ===\n"
        f"Equity=${port_ctx.get('equity', 0):,.0f}  "
        f"Cash=${port_ctx.get('cash', 0):,.0f}  "
        f"Positions={port_ctx.get('position_count', 0)}/{config.MAX_POSITIONS}\n"
        f"Speculative={port_ctx.get('speculative_count', 0)} pos / "
        f"{port_ctx.get('speculative_pct', 0):.1f}% (max {config.MAX_SPECULATIVE_PCT}%)"
        f"{held_str}{sect_str}"
    )

    # ── Candidate blocks ───────────────────────────────────────────────────────
    cand_blocks = []
    for i, c in enumerate(candidates, 1):
        sym    = c["symbol"]
        synth  = c["synthesis"]
        tech   = c["signals"].get("technical", {})
        cong   = c["signals"].get("congressional", {})
        insd   = c["signals"].get("insider", {})
        tier   = config.TICKER_TIERS.get(sym, "mid_growth")

        flags = []
        if sym in held_syms:
            held = next((h for h in holdings if h["symbol"] == sym), None)
            if held:
                flags.append(f"⚠️ ALREADY HELD: {held['pct']:.1f}% ({held['pl_pct']:+.1f}%) — only add if high conviction")
        if cong.get("net_signal") == "bullish":
            flags.append(f"*** CONGRESS NET BUYING: {cong.get('buys',0)}B vs {cong.get('sells',0)}S (60d) ***")
        elif cong.get("net_signal") == "bearish":
            flags.append(f"*** CONGRESS NET SELLING: {cong.get('sells',0)} sells — CAUTION ***")
        if insd.get("net_signal") == "bullish":
            flags.append("*** INSIDER NET BUYING (Form 4) ***")
        elif insd.get("net_signal") == "bearish":
            flags.append("*** INSIDER NET SELLING — caution ***")

        adv = tech.get("adv_30d")
        adv_str = f"ADV-30d=${adv:,.0f}" if adv else "ADV-30d=unknown"
        flag_str = ("\n  " + "\n  ".join(flags)) if flags else ""
        cand_blocks.append(
            f"--- [{i}] {sym} (tier={tier}, {adv_str}) ---{flag_str}\n{synth}"
        )

    candidates_text = "\n\n".join(cand_blocks)

    n = len(candidates)
    schema = (
        f"=== 6-AGENT COMMITTEE DECISIONS ===\n"
        f"For each of the {n} candidates, run the full CIO→QUANT→CRO→CCO→DEVIL→PM chain.\n"
        f"Apply the confidence formula: base=CIO.confidence, -1 if CRO=Caution, "
        f"-2 if DA.severity=High, -1 if DA.severity=Medium, -1 if QUANT=Bearish, "
        f"+1 if QUANT=Strongly_Bullish.\n"
        f"GATE: action=BUY only if CIO=Buy AND CRO≠Block AND CCO=Approve.\n"
        f"TRANCHE: allocation_pct = 50% of target_pct (half-size entry; scale in later).\n"
        f"Return ONLY a JSON array with exactly {n} objects in candidate order:\n"
        f'[{{\n'
        f'  "symbol":"<ticker>",\n'
        f'  "cio":{{"decision":"Buy"|"Avoid"|"Hold","confidence":<1-10>,"narrative_drift":"none"|"positive"|"negative","rel_strength":"outperforming"|"inline"|"underperforming","reason":"<one sentence>"}},\n'
        f'  "quant":{{"decision":"Strongly_Bullish"|"Bullish"|"Neutral"|"Bearish"|"Block","signal":"<one sentence>"}},\n'
        f'  "cro":{{"decision":"Approve"|"Caution"|"Block","adv_ok":true|false,"top_risk":"<one sentence>"}},\n'
        f'  "cco":{{"decision":"Approve"|"Reject","reason":"<one sentence>"}},\n'
        f'  "devil":{{"bear_case":"<one sentence>","probability":<0-100>,"severity":"Low"|"Medium"|"High"}},\n'
        f'  "final_confidence":<1-10>,\n'
        f'  "action":"BUY"|"SELL"|"HOLD",\n'
        f'  "allocation_pct":<0.0-8.0>,\n'
        f'  "target_pct":<0.0-15.0>,\n'
        f'  "asset_type":"stock"|"crypto"|"option",\n'
        f'  "option_direction":"call"|"put"|null,\n'
        f'  "rationale":"<one sentence>"\n'
        f'}}]\n'
        f"No prose, no markdown fences — ONLY the JSON array."
    )

    prompt = f"{geo_block}\n\n{port_block}\n\n=== CANDIDATES ({n}) ===\n\n{candidates_text}\n\n{schema}"

    # thinking_budget: enough to reason through the 6-agent chain per candidate.
    # max_tokens must exceed thinking_budget + expected output tokens.
    _THINKING_BUDGET = 4000
    _output_tokens   = 350 * n + 500
    _max_tokens      = _THINKING_BUDGET + _output_tokens

    try:
        response = _client.messages.create(
            model="claude-opus-4-7",
            max_tokens=_max_tokens,
            thinking={"type": "enabled", "budget_tokens": _THINKING_BUDGET},
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        # Extended thinking returns multiple content blocks — extract the text block only
        raw = next(
            (block.text for block in response.content
             if hasattr(block, "text") and getattr(block, "type", "") == "text"),
            "",
        ).strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        try:
            raw = raw[raw.index("["):raw.rindex("]") + 1]
        except ValueError:
            pass

        decisions = json.loads(raw)
        if not isinstance(decisions, list):
            raise ValueError("Expected JSON array")

        returned = {d.get("symbol"): d for d in decisions if isinstance(d, dict)}
        result = []
        for c in candidates:
            sym = c["symbol"]
            d   = returned.get(sym)
            if d:
                result.append(_normalise_committee_decision(sym, d))
            else:
                result.append(_hold_decision(sym, "Not returned by committee"))
        return result

    except Exception as e:
        print(f"  [Committee] Error ({e}) — falling back to individual decisions")
        result = []
        for c in candidates:
            try:
                d = decide(c["symbol"], c["signals"], port_ctx)
                d["symbol"] = c["symbol"]
                d.setdefault("cio_confidence", d.get("confidence", 0))
                d.setdefault("da_severity", "Low")
                d.setdefault("target_pct", d.get("allocation_pct", 0))
                d["_committee_fallback"] = str(e)
                result.append(d)
            except Exception as e2:
                result.append(_hold_decision(c["symbol"], f"Error: {e2}"))
        return result


def _normalise_committee_decision(sym: str, d: dict) -> dict:
    """Extract and validate fields from a 6-agent committee JSON object."""
    cio    = d.get("cio", {})
    devil  = d.get("devil", {})
    action = d.get("action", "HOLD").upper()
    alloc  = float(d.get("allocation_pct", 0))
    target = float(d.get("target_pct", alloc * 2))  # fallback: double alloc

    return {
        "symbol":           sym,
        "action":           action,
        "confidence":       int(d.get("final_confidence", cio.get("confidence", 0))),
        "cio_confidence":   int(cio.get("confidence", 0)),
        "allocation_pct":   alloc,
        "target_pct":       target,
        "asset_type":       d.get("asset_type", "stock"),
        "option_direction": d.get("option_direction"),
        "rationale":        d.get("rationale", ""),
        "da_severity":      devil.get("severity", "Low"),
        "da_bear_case":     devil.get("bear_case", ""),
        "da_probability":   int(devil.get("probability", 0)),
        "quant_decision":   (d.get("quant") or {}).get("decision", "Neutral"),
        "cro_decision":     (d.get("cro") or {}).get("decision", "Approve"),
        "cco_decision":     (d.get("cco") or {}).get("decision", "Approve"),
        "narrative_drift":  cio.get("narrative_drift", "none"),
        "rel_strength":     cio.get("rel_strength", "inline"),
    }


def _hold_decision(sym: str, reason: str) -> dict:
    return {
        "symbol": sym, "action": "HOLD", "confidence": 0,
        "cio_confidence": 0, "allocation_pct": 0.0, "target_pct": 0.0,
        "asset_type": "stock", "option_direction": None,
        "rationale": reason, "da_severity": "Low", "da_bear_case": "",
        "da_probability": 0, "quant_decision": "Neutral",
        "cro_decision": "Approve", "cco_decision": "Approve",
        "narrative_drift": "none", "rel_strength": "inline",
    }
