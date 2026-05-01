"""
Monthly Deep-Dive — full committee analysis, runs once per month at end of run_monthly_research().

Covers:
  1. Thesis review for every held position (intact / weakening / broken + action)
  2. Sector landscape (are we in the best stock per sector?)
  3. Watchlist top-5 prioritisation (scored from cached signals — see _score_candidate)
  4. Portfolio stress test (bull + bear scenarios, tail risk)
  5. Portfolio construction (cash deployment, missing exposures, rebalancing)

Uses claude-opus-4-7 — best model, ~$0.40 total, runs once/month.
"""
import json
import re
from datetime import datetime, timezone

import anthropic

import config
import database.db as db
import database.research_cache as research_cache
from broker import alpaca
from signals import technical
from notifications import discord_bot as discord


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_bars(symbol: str):
    return alpaca.get_stock_bars(symbol)


def _v(val, fmt=".1f", suffix="", na="N/A"):
    if val is None:
        return na
    try:
        return f"{val:{fmt}}{suffix}"
    except Exception:
        return str(val)


# ── watchlist conviction scorer ───────────────────────────────────────────────

def _score_candidate(sym: str, data: dict) -> dict:
    """
    Score an unowned basket candidate 0–100 for watchlist priority.
    Combines: growth trajectory, earnings momentum, sentiment, options flow,
    short-squeeze potential, analyst consensus, and technical trend.
    Returns a dict with score + component breakdown for the committee.
    """
    score = 0
    notes = []

    growth  = data.get("future_growth") or {}
    fund    = data.get("fundamentals") or {}
    fin     = data.get("financial_data") or {}
    sent    = data.get("sentiment") or {}
    social  = data.get("social") or {}
    earn_m  = data.get("earnings_momentum") or {}
    uw_iv   = data.get("uw_iv") or {}
    uw_si   = data.get("uw_short_interest") or {}
    uw_earn = data.get("uw_earnings_beat") or {}
    cong    = data.get("congressional") or {}
    insider = data.get("insider") or {}
    snippets = data.get("research_snippets") or []

    # Growth score (0-25)
    g = growth.get("score")
    if g is not None:
        pts = min(25, int(g * 25 / 10)) if g <= 10 else 25
        score += pts
        notes.append(f"growth={g}")

    # Earnings momentum (0-15)
    em_label = earn_m.get("label", "")
    if "strong" in em_label.lower() and "beat" in em_label.lower():
        score += 15; notes.append("strong earns beat")
    elif "beat" in em_label.lower():
        score += 10; notes.append("earns beat")
    elif "miss" in em_label.lower():
        score -= 5;  notes.append("earns miss")

    # EPS beat rate (0-10)
    br = uw_earn.get("beat_rate")
    if br is not None:
        if br >= 0.75:   score += 10; notes.append(f"beat_rate={br:.0%}")
        elif br >= 0.60: score += 5

    # Revenue growth (0-10)
    rg = fund.get("revenue_growth")
    if rg is not None:
        if rg >= 0.20:   score += 10; notes.append(f"rev_growth={rg:.0%}")
        elif rg >= 0.10: score += 5
        elif rg < 0:     score -= 5

    # Sentiment (0-10)
    s_score = sent.get("score", 0) or 0
    if s_score > 0.5:  score += 10; notes.append(f"sent={s_score:.2f}")
    elif s_score > 0:  score += 5
    elif s_score < -0.3: score -= 5

    # Social (0-5)
    soc_score = social.get("score", 0) or 0
    if soc_score > 0.3: score += 5

    # Analyst consensus (0-10)
    sb  = fin.get("strong_buy", 0) or 0
    b   = fin.get("buy", 0) or 0
    h   = fin.get("hold", 0) or 0
    sl  = fin.get("sell", 0) or 0
    total_ana = sb + b + h + sl
    if total_ana > 0:
        bull_pct = (sb + b) / total_ana
        if bull_pct >= 0.70: score += 10; notes.append(f"analysts={bull_pct:.0%} buy")
        elif bull_pct >= 0.50: score += 5

    # Congressional / insider buying (0-5 each)
    if cong.get("has_activity") and cong.get("recent_buys"):
        score += 5; notes.append("congress buys")
    if insider.get("has_activity") and insider.get("net_sentiment") == "bullish":
        score += 5; notes.append("insider buys")

    # Short squeeze potential (0-5)
    squeeze = uw_si.get("squeeze_score", "")
    if squeeze == "high":
        score += 5; notes.append("squeeze=high")

    # IV rank — high IV means expensive options, slight negative for entry timing (-5/0/+3)
    iv_rank = uw_iv.get("iv_rank")
    if iv_rank is not None:
        if iv_rank > 80:  score -= 5; notes.append(f"iv_rank={iv_rank} (expensive)")
        elif iv_rank < 30: score += 3; notes.append(f"iv_rank={iv_rank} (cheap)")

    score = max(0, min(100, score))

    return {
        "symbol":           sym,
        "watchlist_score":  score,
        "tier":             config.TICKER_TIERS.get(sym, "mid_growth"),
        "sector":           config.SECTOR_MAP.get(sym, "unknown"),
        "growth_score":     g,
        "earnings_momentum": em_label,
        "sentiment":        _v(s_score, ".2f"),
        "beat_rate":        _v(br, ".0%") if br is not None else "N/A",
        "analyst_bull_pct": f"{(sb+b)/(total_ana):.0%}" if total_ana else "N/A",
        "analyst_target":   fin.get("analyst_target"),
        "revenue_growth":   _v(rg, ".1%") if rg is not None else "N/A",
        "score_notes":      ", ".join(notes[:6]),
        "recent_news":      snippets[:2],
    }


# ── position data builder ─────────────────────────────────────────────────────

def _build_position_block(pos: dict, cached: dict, tech: dict) -> str:
    sym    = pos["symbol"]
    price  = float(pos.get("current_price", 0) or 0)
    entry  = float(pos.get("avg_entry", 0) or 0)
    upl_pc = float(pos.get("unrealized_plpc", 0) or 0)
    upl_dl = float(pos.get("unrealized_pl", 0) or 0)

    fund    = cached.get("fundamentals") or {}
    fin     = cached.get("financial_data") or {}
    growth  = cached.get("future_growth") or {}
    earn_m  = cached.get("earnings_momentum") or {}
    sent    = cached.get("sentiment") or {}
    social  = cached.get("social") or {}
    cong    = cached.get("congressional") or {}
    insider = cached.get("insider") or {}
    uw_si   = cached.get("uw_short_interest") or {}
    uw_iv   = cached.get("uw_iv") or {}
    uw_earn = cached.get("uw_earnings_beat") or {}
    earn_dt = (cached.get("earnings_data") or {}).get("earnings_date", "unknown")
    snippets = (cached.get("research_snippets") or [])[:3]

    sb = fin.get("strong_buy", 0) or 0
    b  = fin.get("buy", 0) or 0
    h  = fin.get("hold", 0) or 0
    sl = fin.get("sell", 0) or 0

    rsi        = tech.get("rsi")
    r1m        = tech.get("return_1m")
    r3m        = tech.get("return_3m")
    sma200_val = tech.get("sma200")
    gc         = tech.get("golden_cross")
    dc         = tech.get("death_cross")
    trend      = "above SMA200" if sma200_val and price > sma200_val else "below SMA200"
    cross      = "GOLDEN CROSS" if gc else ("DEATH CROSS ⚠" if dc else "no cross signal")

    lines = [
        f"=== {sym} | {config.SECTOR_MAP.get(sym,'?')} | {config.TICKER_TIERS.get(sym,'?')} ===",
        f"Price ${price:.2f} | Entry ${entry:.2f} | P&L {upl_pc:+.1f}% (${upl_dl:+,.0f})",
        f"",
        f"FUNDAMENTALS: P/E={_v(fund.get('pe_ratio'))} P/S={_v(fund.get('price_to_sales'))} "
        f"EV/EBITDA={_v(fund.get('ev_to_ebitda'))}",
        f"  Revenue growth={_v(fund.get('revenue_growth'),'.1%')} "
        f"EPS YoY={_v(fund.get('eps_growth_yoy'),'.1%')} "
        f"Gross margin={_v(fund.get('gross_margin'),'.1%')} "
        f"Op margin={_v(fund.get('operating_margin'),'.1%')}",
        f"  Debt/Equity={_v(fund.get('debt_to_equity'))} "
        f"Current ratio={_v(fund.get('current_ratio'))} "
        f"FCF margin={_v(fund.get('fcf_margin'),'.1%')}",
        f"",
        f"GROWTH: score={growth.get('score','N/A')} | {growth.get('summary','')}",
        f"EARNINGS: momentum={earn_m.get('label','N/A')} | "
        f"beat_rate={_v(uw_earn.get('beat_rate'),'.0%')} | next={earn_dt}",
        f"",
        f"TECHNICAL: RSI={_v(rsi)} | {trend} | {cross} | "
        f"1m={_v(r1m,'+.1f','%')} 3m={_v(r3m,'+.1f','%')}",
        f"",
        f"ANALYST: {sb} strong_buy / {b} buy / {h} hold / {sl} sell | "
        f"Target=${_v(fin.get('analyst_target'),'.2f')}",
        f"",
        f"OPTIONS/SHORT: IV_rank={_v(uw_iv.get('iv_rank'),'.0f')} | "
        f"Implied_move={_v(uw_iv.get('implied_move_pct'),'.1f','%')} | "
        f"Short%={_v(uw_si.get('short_interest_pct'),'.1f','%')} | "
        f"Squeeze={uw_si.get('squeeze_score','N/A')}",
        f"",
        f"SENTIMENT: news={sent.get('label','N/A')} ({_v(sent.get('score'),'.2f')}) | "
        f"social={social.get('label','N/A')}",
        f"Congressional: {'YES — ' + str(cong.get('recent_buys','')) if cong.get('has_activity') else 'none'}",
        f"Insider: {'YES — ' + str(insider.get('net_sentiment','')) if insider.get('has_activity') else 'none'}",
    ]
    if snippets:
        lines += ["", "RECENT NEWS:"] + [f"  • {s}" for s in snippets]
    return "\n".join(lines)


# ── main entry point ──────────────────────────────────────────────────────────

def run(positions: list | None = None,
        portfolio: dict | None = None,
        macro_regime: dict | None = None):
    """
    Run the monthly deep-dive. Can be called standalone or from run_monthly_research().
    Fetches positions/portfolio itself if not supplied.
    """
    print("\n" + "="*60)
    print("MONTHLY DEEP-DIVE — Committee Analysis")
    print("="*60)

    if portfolio is None:
        try:
            portfolio = alpaca.get_portfolio()
        except Exception as e:
            discord.send(f"❌ Monthly deep-dive: could not fetch portfolio — {e}")
            return
    if positions is None:
        try:
            positions = alpaca.get_positions()
        except Exception as e:
            discord.send(f"❌ Monthly deep-dive: could not fetch positions — {e}")
            return

    equity   = portfolio.get("equity", 0)
    cash     = portfolio.get("cash", 0)
    cash_pct = cash / equity * 100 if equity else 0

    if not positions:
        discord.send("📊 Monthly Deep-Dive: No open positions — fully in cash.")
        return

    # ── Build held-position blocks ────────────────────────────────────────────
    print(f"  Building position data for {len(positions)} holdings...")
    pos_blocks = []
    for pos in positions:
        sym    = pos["symbol"]
        cached = research_cache.load(sym) or {}
        try:
            tech = technical.compute(_get_bars(sym))
        except Exception:
            tech = {}
        pos_blocks.append(_build_position_block(pos, cached, tech))

    # ── Score and rank basket candidates ─────────────────────────────────────
    print("  Scoring basket candidates for watchlist...")
    held_syms   = {p["symbol"] for p in positions}
    cache_all   = research_cache.load_all()
    candidates  = []
    for sym, data in cache_all.items():
        if sym in held_syms:
            continue
        if sym not in config.TICKER_TIERS:
            continue
        candidates.append(_score_candidate(sym, data))
    candidates.sort(key=lambda x: x["watchlist_score"], reverse=True)
    top_candidates = candidates[:12]  # give committee the top 12 to rank down to 5

    # ── Sector allocation ─────────────────────────────────────────────────────
    sector_alloc: dict = {}
    for p in positions:
        sec = config.SECTOR_MAP.get(p["symbol"], "unknown")
        val = abs(float(p.get("qty", 0)) * float(p.get("current_price", 0)))
        sector_alloc[sec] = sector_alloc.get(sec, 0) + (val / equity * 100 if equity else 0)

    # ── Macro block ───────────────────────────────────────────────────────────
    macro_text = ""
    if macro_regime:
        r = macro_regime
        macro_text = (
            f"MACRO REGIME: {r.get('regime','unknown')}\n"
            f"  VIX={r.get('vix','?')} | 10Y={r.get('ten_year_yield','?')}% | "
            f"CPI={r.get('cpi','?')}% | Spread={r.get('credit_spread','?')}bps\n"
            f"  Fed bias={r.get('fed_bias','?')} | Risks: {', '.join(r.get('top_risks',[])[:3])}\n"
        )

    # ── Learning context ──────────────────────────────────────────────────────
    learning_text = ""
    try:
        from database.learning import get_learning_context
        learning_text = get_learning_context(lookback_days=60)
    except Exception:
        pass

    # ── Build prompt ──────────────────────────────────────────────────────────
    sector_str = json.dumps(
        {k: f"{v:.1f}%" for k, v in sorted(sector_alloc.items(), key=lambda x: -x[1])},
        indent=2,
    )

    held_blocks_text = "\n\n".join(pos_blocks)
    candidates_text  = json.dumps(top_candidates, indent=2)

    prompt = f"""You are Kimmy's Senior Investment Committee conducting the MONTHLY DEEP-DIVE.
This is the most thorough analysis of the month. You have full research data for every holding.
Think like a top-tier portfolio manager. Be specific, quantitative, and decisive.

DATE: {datetime.now(timezone.utc).strftime('%B %d, %Y')}

PORTFOLIO:
  NAV: ${equity:,.2f} | Cash: ${cash:,.2f} ({cash_pct:.1f}%)
  Positions: {len(positions)}
  Sector allocation:
{sector_str}

{macro_text}
{('WHAT WORKED / WHAT FAILED (last 60 days):\n' + learning_text + '\n') if learning_text else ''}
{'='*60}
HELD POSITIONS — FULL DATA
{'='*60}

{held_blocks_text}

{'='*60}
BASKET CANDIDATES — PRE-SCORED (top {len(top_candidates)}, not yet owned)
watchlist_score = 0–100 composite: growth + earnings momentum + sentiment + analyst consensus + options intelligence
{'='*60}
{candidates_text}

{'='*60}
YOUR MANDATE
{'='*60}

A. THESIS REVIEW (every held position):
   - thesis_status: intact | weakening | broken
   - action: ADD | HOLD | TRIM | EXIT
   - 6-month price target in dollars
   - top 2 bull points, top 2 risks
   - catalyst_watch: one thing that would change your view

B. SECTOR LANDSCAPE (each sector we're in):
   - Is the sector bullish/neutral/bearish right now?
   - Are we in the best stock in that sector? If not, name what should replace it.

C. WATCHLIST TOP 5 (pick from basket candidates above, can only choose unowned ones):
   - Rank the 5 highest-conviction unowned candidates
   - Give a specific entry trigger (price level, event, or condition)
   - Conviction score 1–10

D. STRESS TEST:
   - Bull scenario (market up 20% this year): which 3 positions gain most?
   - Bear scenario (Fed hikes + recession fear): which 3 are most vulnerable?
   - Biggest single tail risk in this portfolio?

E. PORTFOLIO CONSTRUCTION:
   - Cash at {cash_pct:.0f}% — deploy / hold / raise more? If deploy, where?
   - Any missing sector or factor exposures?
   - Any rebalancing needed?

RESPOND IN THIS EXACT JSON — no markdown, no extra text:
{{
  "thesis_reviews": [
    {{
      "symbol": "...",
      "thesis_status": "intact|weakening|broken",
      "action": "ADD|HOLD|TRIM|EXIT",
      "price_target_6m": 0,
      "bull_points": ["...", "..."],
      "key_risks": ["...", "..."],
      "catalyst_watch": "...",
      "rationale": "2-3 sentences"
    }}
  ],
  "sector_analysis": [
    {{
      "sector": "...",
      "our_holdings": ["..."],
      "sector_outlook": "bullish|neutral|bearish",
      "positioning_verdict": "well-positioned|swap X for Y|add exposure",
      "note": "..."
    }}
  ],
  "watchlist_top5": [
    {{
      "symbol": "...",
      "conviction": 0,
      "entry_trigger": "...",
      "note": "..."
    }}
  ],
  "stress_test": {{
    "bull_winners": ["...", "...", "..."],
    "bear_vulnerable": ["...", "...", "..."],
    "biggest_tail_risk": "..."
  }},
  "portfolio_construction": {{
    "cash_verdict": "deploy|hold|raise",
    "deploy_into": "...",
    "missing_exposures": "...",
    "rebalancing_needed": "..."
  }},
  "monthly_summary": "3-4 sentence executive summary of portfolio health and outlook",
  "top_priority_action": "the single most important thing to act on this month"
}}"""

    print(f"  Calling Opus for monthly deep-dive (prompt ~{len(prompt)//4:,} tokens)...")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        resp = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=6000,
            system=(
                "You are Kimmy's Senior Investment Committee: CIO, Chief Research Strategist, "
                "Chief Risk Officer, Quantitative Analyst, and Portfolio Manager. "
                "This is the monthly deep-dive — your most thorough work. "
                "Be direct and quantitative. Every held position needs a clear verdict. "
                "Do not hedge. Do not say 'monitor'. Give specific price targets and entry triggers."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
    except Exception as e:
        discord.send(f"❌ Monthly deep-dive failed (API error): {e}")
        return

    # ── Parse JSON ────────────────────────────────────────────────────────────
    report = None
    try:
        start = raw.find('{')
        end   = raw.rfind('}') + 1
        if start != -1 and end > start:
            candidate = raw[start:end]
            candidate = re.sub(r',\s*([}\]])', r'\1', candidate)
            report = json.loads(candidate)
    except Exception:
        pass

    if not report:
        discord.send(f"📊 Monthly Deep-Dive (parse error):\n{raw[:1800]}")
        return

    _send_discord(report, equity, cash_pct, len(positions))
    print("  Monthly deep-dive complete.")


# ── Discord formatting ─────────────────────────────────────────────────────────

def _send_discord(report: dict, equity: float, cash_pct: float, n_pos: int):
    now_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    summary    = report.get("monthly_summary", "")
    top_action = report.get("top_priority_action", "")

    # ── 1: Header + Summary ───────────────────────────────────────────────────
    discord.send(
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 **MONTHLY DEEP-DIVE** — {now_str}\n"
        f"NAV ${equity:,.0f} | Cash {cash_pct:.0f}% | {n_pos} positions\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{summary}\n\n"
        f"🎯 **TOP PRIORITY THIS MONTH:** {top_action}"
    )

    # ── 2: Thesis Reviews ─────────────────────────────────────────────────────
    reviews = report.get("thesis_reviews", [])
    if reviews:
        s_icon  = {"intact": "✅", "weakening": "⚠️", "broken": "🔴"}
        a_icon  = {"ADD": "➕", "HOLD": "🔵", "TRIM": "✂️", "EXIT": "🚪"}
        lines   = ["━━━ **THESIS REVIEWS** ━━━"]
        for r in reviews:
            sym    = r.get("symbol", "?")
            status = r.get("thesis_status", "?")
            action = r.get("action", "?")
            target = r.get("price_target_6m")
            rat    = r.get("rationale", "")
            risks  = r.get("key_risks", [])
            cat    = r.get("catalyst_watch", "")
            t_str  = f" | 6m target **${target}**" if target else ""
            lines.append(
                f"\n{s_icon.get(status,'❓')} **{sym}** — {status.upper()} | "
                f"{a_icon.get(action,'')}{action}{t_str}"
            )
            if rat:    lines.append(f"  _{rat}_")
            if risks:  lines.append(f"  ⚠️ {' | '.join(risks[:2])}")
            if cat:    lines.append(f"  👁 Watch: {cat}")
        discord.send("\n".join(lines))

    # ── 3: Watchlist Top 5 ────────────────────────────────────────────────────
    watchlist = report.get("watchlist_top5", [])
    if watchlist:
        lines = ["━━━ **WATCHLIST TOP 5** (next buys) ━━━"]
        for i, w in enumerate(watchlist[:5], 1):
            sym     = w.get("symbol", "?")
            conv    = w.get("conviction", "?")
            trigger = w.get("entry_trigger", "")
            note    = w.get("note", "")
            lines.append(f"\n**{i}. {sym}** — conviction {conv}/10")
            if trigger: lines.append(f"  📌 Entry trigger: {trigger}")
            if note:    lines.append(f"  _{note}_")
        discord.send("\n".join(lines))

    # ── 4: Stress Test + Construction ─────────────────────────────────────────
    stress = report.get("stress_test", {})
    const  = report.get("portfolio_construction", {})
    if stress or const:
        lines = ["━━━ **STRESS TEST & CONSTRUCTION** ━━━"]
        if stress:
            winners  = stress.get("bull_winners", [])
            vuln     = stress.get("bear_vulnerable", [])
            tail     = stress.get("biggest_tail_risk", "")
            if winners: lines.append(f"\n🐂 **Bull winners:** {', '.join(winners)}")
            if vuln:    lines.append(f"🐻 **Bear vulnerable:** {', '.join(vuln)}")
            if tail:    lines.append(f"☢️ **Tail risk:** {tail}")
        if const:
            verdict = const.get("cash_verdict", "").upper()
            deploy  = const.get("deploy_into", "")
            missing = const.get("missing_exposures", "")
            rebal   = const.get("rebalancing_needed", "")
            lines.append(f"\n💵 **Cash ({cash_pct:.0f}%):** {verdict}")
            if deploy:  lines.append(f"  → Deploy into: {deploy}")
            if missing: lines.append(f"  🔍 Missing exposure: {missing}")
            if rebal:   lines.append(f"  ⚖️ Rebalance: {rebal}")
        discord.send("\n".join(lines))

    # ── 5: Sector Landscape ───────────────────────────────────────────────────
    sectors = report.get("sector_analysis", [])
    if sectors:
        o_icon = {"bullish": "🟢", "neutral": "🟡", "bearish": "🔴"}
        lines  = ["━━━ **SECTOR LANDSCAPE** ━━━"]
        for s in sectors:
            sector   = s.get("sector", "?")
            outlook  = s.get("sector_outlook", "neutral")
            verdict  = s.get("positioning_verdict", "")
            note     = s.get("note", "")
            holdings = s.get("our_holdings", [])
            hold_str = f" ({', '.join(holdings)})" if holdings else ""
            lines.append(
                f"\n{o_icon.get(outlook,'⚪')} **{sector.title()}{hold_str}** — {outlook}"
            )
            if verdict: lines.append(f"  {verdict}")
            if note:    lines.append(f"  _{note}_")
        discord.send("\n".join(lines))
