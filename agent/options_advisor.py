"""
Options Advisor — committee-reviewed institutional flow filter and proposal engine.

Architecture (two entry points):
  run(...)              — Phase 4: screen → score → Claude → notify + log
  check_sell_signals()  — Phase 5: monitor active proposals for exit conditions

Scoring system (0-100, committee-approved 2026-04):
  Premium Size (ticker-normalized) : 20 pts
  Repetition / Accumulation        : 20 pts
  Aggressiveness (sweep/ask)       : 15 pts
  Trend & Multi-Signal Alignment   : 20 pts
  Directional Purity + V>OI        : 10 pts
  Time Horizon & Liquidity         : 10 pts
  Freshness                        :  5 pts

Hard blocks (any one → immediate reject, no Claude call):
  - UW normalized premium pct < 30 (below $300K threshold)
  - sweep_count_7d < 2 (no repetition)
  - IV rank > 70 for calls (premium too expensive)
  - direction = call AND (price < sma50 OR death_cross) → not in uptrend
  - direction = put  AND (price > sma50 AND golden_cross) → not in downtrend
  - earnings within 5 trading days
  - market cap < $2B
  - VIX > 35 (regime block — no new options)
  - VIX > 25 AND direction = call (elevated vol block for calls)
  - macro = risk_off AND direction = call
  - flow_signal in (neutral, no_data) AND committee confidence < 7
"""
import json
import anthropic
import config

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

# ── Thresholds ──────────────────────────────────────────────────────────────
_SCORE_MINIMUM      = 65   # must score >= 65 to propose
_SCORE_STANDARD     = 75   # standard conviction (1% position size)
_SCORE_HIGH         = 85   # high conviction (1.5% position size)
_MAX_PROPOSALS      = 2    # max new proposals per trading cycle
_DEDUP_HOURS        = 72   # skip if same symbol+direction already proposed

# ── System prompt (committee-approved criteria) ─────────────────────────────
_SYSTEM = """You are Kimmy's Options Advisor — a committee member specialising in institutional
options flow. A Python scoring engine has already applied hard filters and computed a
0-100 signal score. Your job: verify the score feels right, identify any qualitative
factors the engine missed, then provide the specific execution plan.

PROPOSE ONLY when ALL of:
  1. Score >= 65 (engine pre-check, re-confirm with your own read)
  2. Directional thesis is unambiguous — not hedging, not market-making
  3. Clear catalyst or technical setup within the recommended expiry window
  4. At least ONE independent confirmation: dark pool, insider/congress, golden cross,
     volume surge, or sector rotation tailwind
  5. Quantifiable exit levels exist (not just "sell if thesis breaks")

SCORING GUIDANCE:
  Premium size (0-20): proportional to how far above the ticker's normal daily options activity
  Repetition (0-20):   how many aligned prints at same strike/expiry confirm accumulation
  Aggressiveness (0-15): sweep > block at ask > mid; penalise bid-side or passive
  Trend alignment (0-20): trend + dark pool + RS + broader market direction
  Directional purity (0-10): call/put dominance >= 70%; V/OI > 1 confirms new positioning
  Time horizon (0-10): 30-90 DTE ideal; 90-120 DTE acceptable; penalise <21 or >150 DTE
  Freshness (0-5): how recent is the flow; decay quickly after 72 hours

SELL TRIGGER must include ALL of:
  (a) Stop level — underlying price or % option premium loss (-35% is the hard floor)
  (b) Profit target — at least 2:1 R/R; scale 50% at +50% gain, trail rest
  (c) Time stop — mandatory exit at 21 DTE
  (d) Signal reversal — opposing institutional flow >= $500K or UW flow reverses for 2 cycles

ENTRY LOGIC:
  score < 85: enter on pullback to support (prior day close, SMA50 retest); valid 2 days
  score >= 85: allow immediate entry at market
"""

_SCHEMA = """Return ONLY a valid JSON array, one object per candidate in input order:
[{
  "symbol": "<ticker>",
  "propose": true|false,
  "score_adjustment": <-10 to +10, your qualitative delta to the engine score>,
  "score_reasoning": "<one sentence explaining your adjustment>",
  "direction": "call"|"put",
  "expiry_weeks": <3-12>,
  "strike_strategy": "atm"|"5pct_otm"|"10pct_otm",
  "entry_logic": "immediate"|"pullback",
  "entry_price_low": <float — lower bound of buy zone for underlying stock>,
  "entry_price_high": <float — upper bound of buy zone; equals entry_price_low for immediate entries>,
  "target_price": <float — underlying price at profit target (minimum 2:1 R/R from entry midpoint)>,
  "stop_price": <float — underlying price that invalidates the thesis (support level or SMA50)>,
  "bull_case": "<one sentence — the specific institutional thesis>",
  "bear_case": "<one sentence — main risk to this option position>",
  "sell_trigger": "<concise: stop=$X | target=$X (+50% option premium) | time=21 DTE | reversal=opposing sweep $500K+>"
}]
All price fields are the UNDERLYING STOCK price, not option premium.
No prose. No markdown. Only the JSON array."""


# ═══════════════════════════════════════════════════════════════════════════
# Hard filters
# ═══════════════════════════════════════════════════════════════════════════

def _hard_filter(item: dict, mkt_ctx: dict) -> tuple[bool, str]:
    """
    Returns (passes, reason). Any fail = reject before scoring.
    """
    uw   = item["uw"]
    tech = item["tech"]
    sigs = item.get("signals", {})
    dirn = item.get("direction_hint", "call")
    dec  = item.get("committee_decision", {})

    norm_pct    = uw.get("normalized_prem_pct", 0) or 0
    sweep_count = uw.get("sweep_count_7d", 0) or 0
    iv_rank     = uw.get("iv_rank")
    flow_sig    = uw.get("flow_signal", "no_data")
    price       = tech.get("price", 0) or 0
    sma50       = tech.get("sma50")
    gc          = tech.get("golden_cross", False)
    dc          = tech.get("death_cross", False)

    # VIX checks
    vix = None
    try:
        vix = float((mkt_ctx.get("vix") or {}).get("vix", 0) or 0)
    except (TypeError, ValueError):
        pass
    if vix and vix > 35:
        return False, f"VIX={vix:.1f} > 35 — regime block, no new options"
    if vix and vix > 25 and dirn == "call":
        return False, f"VIX={vix:.1f} > 25 — elevated vol, calls blocked"

    # Macro regime for calls
    macro_label = (mkt_ctx.get("macro_momentum") or {}).get("label", "neutral")
    if macro_label == "risk_off" and dirn == "call":
        return False, "macro=risk_off — calls blocked in risk-off regime"

    # Minimum premium signal
    if norm_pct < 30:
        return False, f"normalized_prem_pct={norm_pct:.0f} < 30 (below $300K threshold)"

    # Minimum repetition
    if sweep_count < 2:
        return False, f"sweep_count_7d={sweep_count} < 2 (no repetition confirmed)"

    # IV rank guard for calls (don't buy expensive premium)
    if iv_rank is not None and iv_rank > 70 and dirn == "call":
        return False, f"IV rank={iv_rank:.0f} > 70 — options too expensive for calls"

    # Trend alignment
    if dirn == "call":
        if price and sma50 and price < sma50:
            return False, f"price=${price:.2f} < SMA50=${sma50:.2f} — not in uptrend (call blocked)"
        if dc and not gc:
            return False, "death cross active — bearish trend, call blocked"
    elif dirn == "put":
        if price and sma50 and price > sma50 * 1.05 and gc:
            return False, f"price=${price:.2f} > SMA50 with golden cross — strong uptrend, put blocked"

    # Earnings exclusion
    earnings_days = None
    for key in ("days_to_next_earnings", "days_until_earnings"):
        v = (sigs.get("earnings_data") or sigs.get("earnings") or {}).get(key)
        if v is not None:
            try:
                earnings_days = int(v)
                break
            except (ValueError, TypeError):
                pass
    if earnings_days is not None and 0 <= earnings_days <= 5:
        return False, f"earnings in {earnings_days} trading days — blocked (< 5-day window)"

    # Market cap filter
    mktcap = (sigs.get("fundamentals") or {}).get("market_cap")
    if mktcap is not None:
        try:
            if float(mktcap) < 2e9:
                return False, f"market_cap=${float(mktcap)/1e9:.1f}B < $2B minimum"
        except (ValueError, TypeError):
            pass

    # Flow signal must be at least directional
    if flow_sig in ("no_data", "neutral") and dec.get("confidence", 0) < 7:
        return False, f"flow={flow_sig} + committee conf={dec.get('confidence',0)}/10 < 7 — insufficient signal"

    return True, "ok"


# ═══════════════════════════════════════════════════════════════════════════
# Scoring engine
# ═══════════════════════════════════════════════════════════════════════════

def _compute_score(item: dict) -> tuple[int, dict]:
    """
    Returns (total_score 0-100, dimension_breakdown dict).
    Must pass hard filter before calling.
    """
    uw   = item["uw"]
    tech = item["tech"]
    dec  = item.get("committee_decision", {})
    dirn = item.get("direction_hint", "call")
    sigs = item.get("signals", {})

    norm_pct    = uw.get("normalized_prem_pct", 0) or 0
    sweep_count = uw.get("sweep_count_7d", 0) or 0
    flow_sig    = uw.get("flow_signal", "no_data")
    iv_rank     = uw.get("iv_rank")
    cp_ratio    = uw.get("call_put_ratio", 1.0) or 1.0
    expiry_aln  = uw.get("expiry_alignment_score", 0) or 0
    dp          = uw.get("darkpool", {}) or {}

    price    = tech.get("price", 0) or 0
    sma50    = tech.get("sma50")
    sma200   = tech.get("sma200")
    gc       = tech.get("golden_cross", False)
    vol_r    = tech.get("volume_ratio", 1.0) or 1.0
    r3m      = tech.get("return_3m", 0) or 0

    comm_conf = dec.get("confidence", 0) or 0
    dp_sig    = dp.get("darkpool_signal", "no_data")
    dp_count  = dp.get("large_print_count", 0) or 0

    breakdown = {}

    # ── (1) Premium size (0-20) ──────────────────────────────────────────────
    if norm_pct >= 90:
        prem_pts = 20
    elif norm_pct >= 70:
        prem_pts = 15
    elif norm_pct >= 50:
        prem_pts = 10
    elif norm_pct >= 30:
        prem_pts = 5
    else:
        prem_pts = 0
    breakdown["premium_size"] = prem_pts

    # ── (2) Repetition (0-20) ────────────────────────────────────────────────
    if sweep_count >= 5:
        rep_pts = 20
    elif sweep_count >= 3:
        rep_pts = 12
    elif sweep_count >= 2:
        rep_pts = 6
    else:
        rep_pts = 0
    breakdown["repetition"] = rep_pts

    # ── (3) Aggressiveness (0-15) ────────────────────────────────────────────
    if flow_sig in ("bullish_sweep", "bearish_sweep"):
        agg_pts = 15
    elif flow_sig in ("bullish_lean", "bearish_lean"):
        agg_pts = 5
    else:
        agg_pts = 0
    breakdown["aggressiveness"] = agg_pts

    # ── (4) Trend & multi-signal alignment (0-20) ────────────────────────────
    trend_pts = 0
    # Core trend
    if dirn == "call":
        if price and sma50 and price > sma50 and gc:
            trend_pts += 8   # price above SMA50 + golden cross
        elif price and sma50 and price > sma50:
            trend_pts += 4
    else:  # put
        if price and sma50 and price < sma50:
            trend_pts += 8
        elif price and sma50:
            trend_pts += 2
    # Dark pool
    if dp_sig == "bullish" and dirn == "call" and dp_count >= 2:
        trend_pts += 5
    elif dp_sig == "bearish" and dirn == "put" and dp_count >= 2:
        trend_pts += 5
    elif dp_sig in ("bullish", "bearish") and dp_count >= 1:
        trend_pts += 2
    # Relative strength
    if r3m and r3m > 15 and dirn == "call":
        trend_pts += 4
    elif r3m and r3m > 5 and dirn == "call":
        trend_pts += 2
    # Volume surge
    if vol_r and vol_r > 1.5:
        trend_pts += 3
    elif vol_r and vol_r > 1.2:
        trend_pts += 1
    # Committee confidence bonus
    if comm_conf >= 8:
        trend_pts += 3 if trend_pts > 0 else 1
    elif comm_conf >= 6:
        trend_pts += 1
    # Congress/insider signal
    if item.get("congress_buying") and dirn == "call":
        trend_pts += 3
    if item.get("insider_buying") and dirn == "call":
        trend_pts += 2
    breakdown["trend_alignment"] = min(trend_pts, 20)

    # ── (5) Directional purity + V>OI (0-10) ────────────────────────────────
    purity_pts = 0
    if dirn == "call":
        if cp_ratio >= 2.5:
            purity_pts = 10
        elif cp_ratio >= 1.8:
            purity_pts = 7
        elif cp_ratio >= 1.3:
            purity_pts = 4
    else:  # put
        if cp_ratio <= 0.4:
            purity_pts = 10
        elif cp_ratio <= 0.6:
            purity_pts = 7
        elif cp_ratio <= 0.8:
            purity_pts = 4
    breakdown["directional_purity"] = purity_pts

    # ── (6) Time horizon & liquidity (0-10) ──────────────────────────────────
    # expiry_alignment_score: 1.0 = 3-6w (21-42d ideal zone), 0.5 = 6-10w (good medium-term)
    if expiry_aln >= 1.0:
        time_pts = 10
    elif expiry_aln >= 0.5:
        time_pts = 7
    else:
        time_pts = 3  # unknown/out-of-window — not a hard block but lower score
    breakdown["time_horizon"] = time_pts

    # ── (7) Freshness (0-5) ──────────────────────────────────────────────────
    # Approximate: high normalized_prem_pct suggests very recent activity
    if norm_pct >= 80:
        fresh_pts = 5   # spike-level activity = very recent
    elif norm_pct >= 60:
        fresh_pts = 3
    else:
        fresh_pts = 1
    breakdown["freshness"] = fresh_pts

    total = sum(breakdown.values())
    return total, breakdown


# ═══════════════════════════════════════════════════════════════════════════
# Candidate screening
# ═══════════════════════════════════════════════════════════════════════════

def _screen(opt_queue: list, candidates: list, decisions: list, signals_map: dict) -> list:
    """
    Merge committee-flagged options plays with UW sweep discoveries.
    Returns list of candidates that pass hard filters, with score attached.
    """
    dec_map  = {d["symbol"]: d for d in decisions}
    screened = []
    seen     = set()

    def _add(sym, dirn, source, uw, tech, dec, sigs):
        if sym in seen:
            return
        item = {
            "symbol":             sym,
            "direction_hint":     dirn,
            "source":             source,
            "uw":                 uw,
            "tech":               tech,
            "committee_decision": dec,
            "signals":            sigs,
            "congress_buying":    (sigs.get("congressional") or {}).get("net_signal") == "bullish",
            "insider_buying":     (sigs.get("insider") or {}).get("net_signal") == "bullish",
        }
        passes, reason = _hard_filter(item, {})  # VIX added later in run()
        if not passes:
            print(f"  [OptionsAdvisor] {sym} {dirn.upper()} rejected: {reason}")
            return
        score, breakdown = _compute_score(item)
        item["engine_score"]   = score
        item["score_breakdown"] = breakdown
        print(f"  [OptionsAdvisor] {sym} {dirn.upper()} score={score}/100 "
              f"({', '.join(f'{k}:{v}' for k, v in breakdown.items())})")
        screened.append(item)
        seen.add(sym)

    # (a) Committee explicitly flagged these as options plays
    for q in opt_queue:
        sym  = q["symbol"]
        sigs = q.get("signals") or signals_map.get(sym, {})
        uw   = sigs.get("options_flow", {})
        tech = q.get("tech") or sigs.get("technical", {})
        dec  = q.get("decision") or dec_map.get(sym, {})
        _add(sym, q.get("direction") or "call", "committee", uw, tech, dec, sigs)

    # (b) UW sweep discoveries from the full candidate scan
    for c in candidates:
        sym  = c["symbol"]
        sigs = c["signals"]
        uw   = sigs.get("options_flow", {})
        tech = sigs.get("technical", {})
        dec  = dec_map.get(sym, {})
        flow = uw.get("flow_signal", "no_data")
        if flow not in ("bullish_sweep", "bearish_sweep"):
            continue
        dirn = "call" if flow == "bullish_sweep" else "put"
        _add(sym, dirn, "uw_sweep", uw, tech, dec, sigs)

    # Sort by engine score descending
    screened.sort(key=lambda x: x["engine_score"], reverse=True)
    return screened


# ═══════════════════════════════════════════════════════════════════════════
# Claude evaluation
# ═══════════════════════════════════════════════════════════════════════════

def _build_block(idx: int, c: dict) -> str:
    sym   = c["symbol"]
    dec   = c["committee_decision"]
    uw    = c["uw"]
    tech  = c["tech"]
    score = c["engine_score"]
    bdown = c["score_breakdown"]
    dirn  = c["direction_hint"]
    src   = c["source"]

    flow_sig  = uw.get("flow_signal", "no_data")
    iv_rank   = uw.get("iv_rank", "?")
    impl_mv   = uw.get("implied_move_pct", "?")
    sweeps    = uw.get("sweep_count_7d", 0)
    cp_ratio  = uw.get("call_put_ratio", "?")
    norm_pct  = uw.get("normalized_prem_pct", "?")
    dp_sig    = (uw.get("darkpool") or {}).get("darkpool_signal", "no_data")
    dp_cnt    = (uw.get("darkpool") or {}).get("large_print_count", 0)

    price   = tech.get("price", "?")
    sma50   = tech.get("sma50", "?")
    sma200  = tech.get("sma200", "?")
    gc      = tech.get("golden_cross", False)
    rsi     = tech.get("rsi", "?")
    r1m     = tech.get("return_1m", "?")
    r3m     = tech.get("return_3m", "?")
    vol_r   = tech.get("volume_ratio", "?")
    atr     = tech.get("atr", "?")

    conf   = dec.get("confidence", 0)
    act    = dec.get("action", "HOLD")
    rat    = dec.get("rationale", "")
    bear   = dec.get("da_bear_case", "")
    thbk   = dec.get("thesis_break_criteria", "N/A")
    quant  = dec.get("quant_decision", "Neutral")

    flags = []
    if c.get("congress_buying"): flags.append("CONGRESS_BUYING")
    if c.get("insider_buying"):  flags.append("INSIDER_BUYING")
    flag_str = " | ".join(flags) if flags else "none"

    bd_str = " | ".join(f"{k}={v}" for k, v in bdown.items())

    return (
        f"[{idx}] {sym}  source={src} direction_hint={dirn}\n"
        f"  ENGINE SCORE: {score}/100 ({bd_str})\n"
        f"  Committee: action={act} conf={conf}/10 quant={quant}\n"
        f"  Thesis: {rat}\n"
        f"  Bear: {bear} | Thesis-break: {thbk}\n"
        f"  UW: flow={flow_sig} sweeps={sweeps} norm%={norm_pct} C/P={cp_ratio}\n"
        f"  IV: rank={iv_rank}/100 implied_move=±{impl_mv}%\n"
        f"  DarkPool: signal={dp_sig} large_prints={dp_cnt}\n"
        f"  Tech: price={price} SMA50={sma50} SMA200={sma200} golden_cross={gc}\n"
        f"  Momentum: RSI={rsi} 1M={r1m}% 3M={r3m}% vol_ratio={vol_r} ATR={atr}\n"
        f"  Flags: {flag_str}"
    )


def _call_claude(screened: list, mkt_ctx: dict) -> list:
    vix_val  = (mkt_ctx.get("vix") or {}).get("vix", "?")
    tide     = (mkt_ctx.get("uw_market") or {}).get("market_tide", "neutral")
    macro    = (mkt_ctx.get("macro_momentum") or {}).get("label", "neutral")
    fg       = (mkt_ctx.get("fear_and_greed") or {}).get("score", "?")

    blocks = [_build_block(i + 1, c) for i, c in enumerate(screened)]
    prompt = (
        f"Market: VIX={vix_val} tide={tide} macro={macro} fear&greed={fg}\n\n"
        f"Evaluate {len(screened)} pre-scored candidate(s) for options proposals:\n\n"
        + "\n\n".join(blocks)
        + f"\n\n{_SCHEMA}"
    )

    resp = _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=250 * len(screened) + 400,
        system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        raw = raw[raw.index("["):raw.rindex("]") + 1]
    except ValueError:
        pass
    return json.loads(raw)


# ═══════════════════════════════════════════════════════════════════════════
# Notification formatter
# ═══════════════════════════════════════════════════════════════════════════

def _position_size_guidance(score: int, equity: float) -> str:
    """Return position size guidance based on signal score."""
    if score >= _SCORE_HIGH:
        risk_pct = 1.0
        tier = "HIGH conviction"
    elif score >= _SCORE_STANDARD:
        risk_pct = 0.75
        tier = "standard conviction"
    else:
        risk_pct = 0.5
        tier = "light conviction"
    premium_budget = round(equity * (risk_pct / 100) / 0.35, 0)
    return f"${premium_budget:,.0f} premium budget ({risk_pct}% acct risk, {tier})"


def _compute_price_levels(prop: dict, item: dict) -> dict:
    """
    Derive concrete $ price levels for the notification.
    Claude provides entry_price_low/high, target_price, stop_price.
    We fill in any gaps using technical data as fallback.
    All prices are for the UNDERLYING stock, not the option premium.
    """
    tech    = item["tech"]
    uw      = item["uw"]
    dirn    = prop.get("direction", item["direction_hint"])

    price   = float(tech.get("price") or 0)
    sma50   = float(tech.get("sma50") or 0)
    atr     = float(tech.get("atr") or 0)
    impl_mv = uw.get("implied_move_pct")

    # ── Entry (buy zone for the underlying) ─────────────────────────────────
    raw_lo = prop.get("entry_price_low")
    raw_hi = prop.get("entry_price_high")
    try:
        entry_lo = float(raw_lo) if raw_lo else 0.0
        entry_hi = float(raw_hi) if raw_hi else 0.0
    except (TypeError, ValueError):
        entry_lo = entry_hi = 0.0

    if not entry_lo or not entry_hi:
        if prop.get("entry_logic") == "immediate":
            entry_lo = entry_hi = round(price, 2) if price else 0.0
        else:
            # Fallback: pullback to SMA50 if above it, else current price -1 ATR
            support = sma50 if sma50 and price > sma50 else (price - atr if atr else price * 0.98)
            entry_lo = round(support, 2) if support else 0.0
            entry_hi = round(price, 2) if price else 0.0

    entry_mid = (entry_lo + entry_hi) / 2 if entry_lo and entry_hi else price

    # ── Target (profit-taking level on the underlying) ───────────────────────
    raw_tgt = prop.get("target_price")
    try:
        target = float(raw_tgt) if raw_tgt else 0.0
    except (TypeError, ValueError):
        target = 0.0

    if not target and entry_mid:
        # Fallback: implied move × 1.5 above entry for calls, below for puts
        if impl_mv and impl_mv > 0:
            move = entry_mid * (impl_mv / 100) * 1.5
        elif atr:
            move = atr * 3
        else:
            move = entry_mid * 0.08  # 8% default
        target = round(entry_mid + move, 2) if dirn == "call" else round(entry_mid - move, 2)

    # ── Stop (underlying price that invalidates thesis) ──────────────────────
    raw_stp = prop.get("stop_price")
    try:
        stop = float(raw_stp) if raw_stp else 0.0
    except (TypeError, ValueError):
        stop = 0.0

    if not stop and entry_mid:
        if dirn == "call":
            # Fallback: SMA50 if below entry, else entry - 2×ATR
            stop = round(sma50, 2) if sma50 and sma50 < entry_mid else round(entry_mid - 2 * atr, 2) if atr else round(entry_mid * 0.93, 2)
        else:
            stop = round(sma50, 2) if sma50 and sma50 > entry_mid else round(entry_mid + 2 * atr, 2) if atr else round(entry_mid * 1.07, 2)

    return {
        "entry_lo":  entry_lo,
        "entry_hi":  entry_hi,
        "entry_mid": entry_mid,
        "target":    target,
        "stop":      stop,
    }


def _format_proposal(prop: dict, item: dict, proposal_id: int, equity: float) -> str:
    sym    = item["symbol"]
    dirn   = prop.get("direction", item["direction_hint"])
    score  = item["engine_score"] + (prop.get("score_adjustment") or 0)
    expw   = prop.get("expiry_weeks", 5)
    strat  = prop.get("strike_strategy", "atm")
    entry  = prop.get("entry_logic", "pullback")
    bull   = prop.get("bull_case", "")
    bear   = prop.get("bear_case", "")
    adj_r  = prop.get("score_reasoning", "")
    src    = item["source"]

    uw    = item["uw"]
    tech  = item["tech"]
    bdown = item["score_breakdown"]

    price   = tech.get("price") or 0
    iv_rank = uw.get("iv_rank")
    impl_mv = uw.get("implied_move_pct")
    flow    = uw.get("flow_signal", "no_data")
    sweeps  = uw.get("sweep_count_7d", 0)

    levels = _compute_price_levels(prop, item)
    e_lo, e_hi, tgt, stp = levels["entry_lo"], levels["entry_hi"], levels["target"], levels["stop"]

    dirn_e   = "📈" if dirn == "call" else "📉"
    strike_m = {"atm": "ATM", "5pct_otm": "5% OTM", "10pct_otm": "10% OTM"}
    src_tag  = "Committee" if src == "committee" else "UW Sweep"
    sizing   = _position_size_guidance(score, equity) if equity else "size to 0.75% account risk"

    # ── Header ───────────────────────────────────────────────────────────────
    lines = [
        f"{dirn_e} OPTIONS PROPOSAL — {sym} {dirn.upper()} [{src_tag}]",
        f"Score: {score}/100 | Expiry: ~{expw} wks out | Strike: {strike_m.get(strat, strat)}",
    ]

    # ── Underlying price context ─────────────────────────────────────────────
    if isinstance(price, (int, float)) and price:
        lines.append(f"Stock now: ${price:,.2f}")

    # ── THE THREE NUMBERS — buy zone / target / stop ─────────────────────────
    lines.append("")
    lines.append("ACTION LEVELS (underlying stock price):")
    if e_lo and e_hi and abs(e_hi - e_lo) > 0.01:
        lines.append(f"  BUY ZONE   ${e_lo:,.2f} – ${e_hi:,.2f}")
    elif e_lo:
        entry_tag = "at market" if entry == "immediate" else "on pullback"
        lines.append(f"  BUY        ${e_lo:,.2f} ({entry_tag})")
    if tgt:
        rr = round((tgt - e_lo) / (e_lo - stp), 1) if e_lo and stp and e_lo != stp and dirn == "call" else None
        rr_str = f"  [{rr:.1f}:1 R/R]" if rr and rr > 0 else ""
        lines.append(f"  TARGET     ${tgt:,.2f}{rr_str}")
    if stp:
        pct_risk = round(abs(e_lo - stp) / e_lo * 100, 1) if e_lo else None
        pct_str = f"  [-{pct_risk}% thesis break]" if pct_risk else ""
        lines.append(f"  STOP       ${stp:,.2f}{pct_str}")
    lines.append(f"  OPTION STOP  -35% of option premium from entry (hard floor)")
    lines.append(f"  TIME STOP    exit at 21 DTE regardless of P&L")
    lines.append("")

    # ── Position size ────────────────────────────────────────────────────────
    lines.append(f"Size: {sizing}")

    # ── Signal context ───────────────────────────────────────────────────────
    detail = []
    if flow != "no_data":             detail.append(f"UW: {flow} ({sweeps} sweeps)")
    if iv_rank is not None:           detail.append(f"IV rank {iv_rank:.0f}/100")
    if impl_mv:                       detail.append(f"±{impl_mv:.1f}% implied move")
    if detail:
        lines.append(" | ".join(detail))

    # Score breakdown (compact)
    bd_line = " | ".join(f"{k.replace('_', ' ')}={v}" for k, v in bdown.items())
    lines.append(f"Signal breakdown: {bd_line}")
    if adj_r:
        lines.append(f"Advisor note: {adj_r}")

    # ── Thesis ───────────────────────────────────────────────────────────────
    lines.append(f"WHY: {bull}")
    lines.append(f"RISK: {bear}")
    lines.append(f"Committee monitors 2x/day. Sell alert fires when exit conditions trigger. "
                 f"(Proposal #{proposal_id})")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 4 entry point
# ═══════════════════════════════════════════════════════════════════════════

def run(
    opt_queue: list,
    candidates: list,
    decisions: list,
    mkt_ctx: dict,
    signals_map: dict,
    op_db,
    discord,
) -> int:
    """
    Phase 4: screen → score → Claude → log + notify.
    Returns number of proposals sent this cycle.
    """
    screened = _screen(opt_queue, candidates, decisions, signals_map)
    if not screened:
        return 0

    # Apply VIX hard filter now that we have mkt_ctx
    vix = None
    try:
        vix = float((mkt_ctx.get("vix") or {}).get("vix", 0) or 0)
    except (TypeError, ValueError):
        pass

    filtered = []
    for item in screened:
        if vix and vix > 35:
            print(f"  [OptionsAdvisor] All options blocked — VIX={vix:.1f} > 35")
            return 0
        if vix and vix > 25 and item["direction_hint"] == "call":
            print(f"  [OptionsAdvisor] {item['symbol']} CALL blocked — VIX={vix:.1f} > 25")
            continue
        if item["engine_score"] < _SCORE_MINIMUM:
            print(f"  [OptionsAdvisor] {item['symbol']} score={item['engine_score']} < {_SCORE_MINIMUM} — skip")
            continue
        filtered.append(item)

    if not filtered:
        return 0

    print(f"\n  [OptionsAdvisor] Sending {len(filtered)} candidate(s) to Claude for evaluation...")

    try:
        results = _call_claude(filtered, mkt_ctx)
    except Exception as e:
        print(f"  [OptionsAdvisor] Claude call failed: {e}")
        return 0

    if not isinstance(results, list):
        return 0

    # Try to get equity for position sizing guidance
    equity = 0.0
    try:
        from broker import alpaca as _alp
        equity = _alp.get_portfolio().get("equity", 0)
    except Exception:
        pass

    sent = 0
    for item, result in zip(filtered, results):
        if not isinstance(result, dict):
            continue
        if not result.get("propose", False):
            continue

        # Final score = engine + Claude adjustment
        adj         = result.get("score_adjustment", 0) or 0
        final_score = max(0, min(100, item["engine_score"] + adj))
        if final_score < _SCORE_MINIMUM:
            continue

        if sent >= _MAX_PROPOSALS:
            print(f"  [OptionsAdvisor] Max {_MAX_PROPOSALS} proposals/cycle reached")
            break

        sym  = item["symbol"]
        dirn = result.get("direction") or item["direction_hint"]

        if op_db.was_recently_proposed(sym, dirn, hours=_DEDUP_HOURS):
            print(f"  [OptionsAdvisor] {sym} {dirn.upper()} already proposed within {_DEDUP_HOURS}h — skip")
            continue

        uw   = item["uw"]
        tech = item["tech"]
        dec  = item["committee_decision"]

        price   = tech.get("price") or 0
        iv_rank = uw.get("iv_rank")
        impl_mv = uw.get("implied_move_pct")
        flow    = uw.get("flow_signal", "no_data")
        sweeps  = uw.get("sweep_count_7d", 0)

        levels = _compute_price_levels(result, item)
        sell_trigger_full = (
            f"stop=${levels['stop']:,.2f} | "
            f"target=${levels['target']:,.2f} | "
            f"time=21 DTE | "
            f"reversal=opposing sweep $500K+"
            + (f" | {result.get('sell_trigger','')}" if result.get("sell_trigger") else "")
        )

        proposal_id = op_db.log_proposal(
            symbol=sym,
            direction=dirn,
            confidence=final_score,
            expiry_weeks=result.get("expiry_weeks", 5),
            strike_strategy=result.get("strike_strategy", "atm"),
            iv_rank=iv_rank,
            implied_move_pct=impl_mv,
            flow_signal=flow,
            sweep_count=sweeps,
            price_at_proposal=price if isinstance(price, (int, float)) else 0,
            bull_case=result.get("bull_case", ""),
            bear_case=result.get("bear_case", ""),
            thesis_break=dec.get("thesis_break_criteria", ""),
            rationale=dec.get("rationale", ""),
            sell_trigger=sell_trigger_full,
            entry_price_low=levels["entry_lo"],
            entry_price_high=levels["entry_hi"],
            target_price=levels["target"],
            stop_price=levels["stop"],
        )

        msg = _format_proposal(result, item, proposal_id, equity)
        discord.send(msg)
        print(f"  [OptionsAdvisor] PROPOSAL #{proposal_id}: {sym} {dirn.upper()} "
              f"score={final_score}/100 | {result.get('bull_case','')[:80]}")
        sent += 1

    return sent


# ═══════════════════════════════════════════════════════════════════════════
# Phase 5 — sell signal monitoring
# ═══════════════════════════════════════════════════════════════════════════

def check_sell_signals(decisions: list, signals_map: dict, op_db, discord, cfg) -> int:
    """
    Phase 5: check every active proposal against current committee + UW data.
    Fires sell alerts for: committee reversal, UW flow flip, DTE warnings.
    Returns number of alerts sent.
    """
    max_days = getattr(cfg, "OPTIONS_PROPOSAL_ACTIVE_DAYS", 42)
    active   = op_db.get_active_proposals(max_age_days=max_days)
    if not active:
        return 0

    from datetime import datetime, timezone
    dec_map = {d["symbol"]: d for d in decisions}
    now     = datetime.now(timezone.utc)
    alerts  = 0

    dte_warn = getattr(cfg, "OPTIONS_DTE_WARNING_WEEKS", 2.5)
    dte_exit = getattr(cfg, "OPTIONS_DTE_EXIT_WEEKS", 1.0)
    # 21-DTE mandatory exit rule (committee): alert when <= 3 weeks remain
    dte_mandatory = 3.0

    for prop in active:
        sym  = prop["symbol"]
        pid  = prop["id"]
        dirn = prop["direction"]
        expw = prop.get("expiry_weeks") or 6
        sig_score = prop.get("confidence", 65)  # stored as signal score

        try:
            age_days = (now - datetime.fromisoformat(prop["ts"])).days
        except Exception:
            age_days = 0
        weeks_left = max(0.0, expw - (age_days / 7))

        sell_reasons = []
        force_close  = False

        # ── Committee reversal signals ───────────────────────────────────────
        dec = dec_map.get(sym)
        if dec:
            act  = dec.get("action", "HOLD")
            conf = dec.get("confidence", 10)
            qd   = dec.get("quant_decision", "Neutral")

            if dirn == "call":
                if act == "SELL":
                    sell_reasons.append(f"committee SELL on underlying (conf={conf}/10)")
                    force_close = True
                elif conf <= 4:
                    sell_reasons.append(f"committee conviction collapsed to {conf}/10")
                    force_close = True
                elif act == "TRIM" and conf < 6:
                    sell_reasons.append(f"committee TRIM + weak conviction ({conf}/10)")
                if qd == "Block":
                    sell_reasons.append("QUANT agent issued Block signal")
                    force_close = True

            elif dirn == "put":
                if act == "BUY" and conf >= 7:
                    sell_reasons.append(f"committee BUY on underlying (conf={conf}/10) — put thesis broken")
                    force_close = True

        # ── UW flow reversal ─────────────────────────────────────────────────
        uw_now   = (signals_map.get(sym) or {}).get("options_flow", {})
        flow_now = uw_now.get("flow_signal", "no_data")
        sweeps_n = uw_now.get("sweep_count_7d", 0) or 0
        cp_now   = uw_now.get("call_put_ratio", 1.0) or 1.0

        if dirn == "call" and flow_now == "bearish_sweep" and sweeps_n >= 2:
            sell_reasons.append(f"UW reversed to bearish_sweep ({sweeps_n} sweeps)")
            force_close = True
        elif dirn == "call" and cp_now < 0.6 and flow_now in ("bearish_sweep", "bearish_lean"):
            sell_reasons.append(f"UW C/P ratio dropped to {cp_now:.2f} (bearish flow dominance)")
            force_close = True
        elif dirn == "put" and flow_now == "bullish_sweep" and sweeps_n >= 2:
            sell_reasons.append(f"UW reversed to bullish_sweep ({sweeps_n} sweeps)")
            force_close = True

        # ── Mandatory 21-DTE time stop (committee rule) ──────────────────────
        if weeks_left <= dte_exit:
            sell_reasons.append(
                f"~{weeks_left:.1f} week(s) to expiry — EXIT OR ROLL NOW (21-DTE mandatory stop)"
            )
            force_close = True
        elif weeks_left <= dte_mandatory:
            sell_reasons.append(
                f"~{weeks_left:.1f} weeks remaining — begin scaling out at 30 DTE, "
                f"mandatory full exit at 21 DTE"
            )
            # Don't force close on the mandatory warning, only on the hard stop

        if not sell_reasons:
            continue

        reason_str   = " | ".join(sell_reasons)
        dirn_e       = "📈" if dirn == "call" else "📉"
        entry_px     = prop.get("price_at_proposal") or 0
        entry_lo     = prop.get("entry_price_low") or 0
        entry_hi     = prop.get("entry_price_high") or 0
        tgt_px       = prop.get("target_price") or 0
        stp_px       = prop.get("stop_price") or 0

        # Get current price for live context
        curr_price = 0.0
        uw_now     = (signals_map.get(sym) or {}).get("options_flow", {})
        try:
            curr_price = float((signals_map.get(sym) or {}).get("technical", {}).get("price") or 0)
        except Exception:
            pass

        lines = [
            f"🔔 SELL OPTION ALERT — {sym} {dirn.upper()} (Proposal #{pid})",
            f"Score at entry: {sig_score}/100",
            f"Reason: {reason_str}",
            "",
            "ORIGINAL ACTION LEVELS:",
        ]
        if entry_lo and entry_hi and abs(entry_hi - entry_lo) > 0.01:
            lines.append(f"  Buy zone   ${entry_lo:,.2f} – ${entry_hi:,.2f}")
        elif entry_lo:
            lines.append(f"  Entry      ${entry_lo:,.2f}")
        elif entry_px:
            lines.append(f"  Stock at proposal  ${entry_px:,.2f}")
        if tgt_px:
            lines.append(f"  Target     ${tgt_px:,.2f}")
        if stp_px:
            lines.append(f"  Stop       ${stp_px:,.2f}")
        if curr_price:
            direction_arrow = "▲" if (dirn == "call" and curr_price > (entry_lo or entry_px)) else "▼" if (dirn == "put" and curr_price < (entry_lo or entry_px)) else "→"
            lines.append(f"  Now        ${curr_price:,.2f} {direction_arrow}")
        lines.append("")
        lines.append("Review your position — decide to close, roll, or hold.")

        discord.send("\n".join(lines))
        print(f"  [OptionsMonitor] SELL ALERT #{pid}: {sym} {dirn.upper()} | {sell_reasons[0]}")

        if force_close:
            op_db.close_proposal(pid, reason_str)

        alerts += 1

    return alerts
