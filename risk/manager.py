"""
Entry/exit criteria and position sizing for Kimmy.

Entry uses a 3-layer scoring system — stocks don't need to be perfect everywhere,
they need to be strong overall. Hard blocks are reserved for true extremes.

BTC has its own separate criteria block at the bottom.
"""
from datetime import date, datetime
import config
import database.db as _db


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_btc(symbol: str) -> bool:
    return "BTC" in symbol.upper()


def _days_to_earnings(earnings: dict) -> int | None:
    ed = (earnings or {}).get("earnings_date")
    if not ed:
        return None
    try:
        return (date.fromisoformat(ed) - date.today()).days
    except Exception:
        return None


# ── Stock entry criteria ───────────────────────────────────────────────────────

def check_entry_criteria(signals: dict, bucket: str = None) -> tuple[bool, str]:
    """
    Dispatch to tier-appropriate entry check.
    bucket="medium_term" runs the dedicated medium-term momentum/catalyst gate
    (on top of the tier gate) for large_growth and mid_growth candidates.
    Mega and speculative are always long_term.
    """
    sym = signals.get("_symbol", "")
    if _is_btc(sym):
        return check_btc_entry(signals)
    tier = config.TICKER_TIERS.get(sym, "mid_growth")
    if tier == "speculative":
        return _check_speculative_entry(signals)
    if tier == "mid_growth":
        ok, reason = _check_mid_growth_entry(signals)
    else:
        ok, reason = _check_standard_entry(signals)
    if not ok:
        return False, reason
    if bucket == "medium_term" and tier not in ("mega", "speculative"):
        return _check_medium_term_entry(signals)
    return True, reason


def _check_standard_entry(signals: dict) -> tuple[bool, str]:
    """Mega / large_growth: strict 3-layer scoring."""

    tech = signals.get("technical", {})
    fund = signals.get("fundamentals", {})
    sent = signals.get("sentiment", {})
    cong = signals.get("congressional", {})
    mkt  = signals.get("market_context", {})
    earn = signals.get("earnings", {})
    fin  = signals.get("financial_data", {})

    # ── Hard Block 1: Market panic ─────────────────────────────────────────
    fg_score = (mkt.get("fear_and_greed") or {}).get("score")
    if fg_score is not None and fg_score < config.CRITERIA_FG_PANIC:
        return False, f"Market extreme fear (F&G={fg_score:.0f}) — pausing new buys"

    # ── Bear market override: true panic only → only mega allowed ─
    vix_val     = (mkt.get("vix") or {}).get("vix", 0)
    market_risk = mkt.get("market_risk", "unknown")
    if vix_val and vix_val > 40 and market_risk == "extreme_fear":
        tier = config.TICKER_TIERS.get(signals.get("_symbol", ""), "mid_growth")
        if tier != "mega":
            return False, f"Panic mode (VIX={vix_val:.0f}, extreme_fear) — only mega-caps during market panic"

    # ── Hard Block 2: RSI extremes ─────────────────────────────────────────
    rsi = tech.get("rsi")
    if rsi is not None:
        if rsi < config.CRITERIA_RSI_MIN:
            return False, f"RSI {rsi:.1f} — extreme panic/crash territory"
        if rsi > config.CRITERIA_RSI_MAX:
            return False, f"RSI {rsi:.1f} — extreme overbought"

    # ── Hard Block 5: Penny stock / liquidity filter (Risk Officer) ────────
    price = tech.get("price")
    if price is not None and price < config.MIN_STOCK_PRICE:
        return False, f"Price ${price:.2f} below minimum ${config.MIN_STOCK_PRICE} — liquidity risk"

    # ── Hard Block 3: Earnings imminent ───────────────────────────────────
    dte = _days_to_earnings(earn)
    if dte is not None and 0 <= dte <= config.CRITERIA_EARNINGS_DAYS:
        return False, f"Earnings in {dte} day(s) — avoiding binary event"

    # ── Hard Block 4: Congress actively selling ────────────────────────────
    # Only block if congress is selling AND sentiment is negative (double confirm)
    if cong.get("net_signal") == "bearish" and sent.get("label") == "negative":
        return False, "Congress selling + negative sentiment — double bearish signal"

    # ── Future outlook gate (Chief Research): prefer companies in growth industries
    # A future growth score below 30 means declining/commodity business — skip unless
    # fundamentals are exceptionally strong (4+/5). This enforces the product quality thesis.
    growth = signals.get("future_growth", {})
    g_score = growth.get("score", None)
    if g_score is not None and g_score < 30:
        # Allow through only if fundamentals are very strong — otherwise skip
        fund_quick = 0
        fund_quick += 1 if (signals.get("fundamentals", {}).get("eps_growth_yoy") or 0) > 0 else 0
        fund_quick += 1 if (signals.get("fundamentals", {}).get("revenue_growth") or 0) > 0 else 0
        fund_quick += 1 if (signals.get("fundamentals", {}).get("profit_margin") or 0) > 10 else 0
        fund_quick += 1 if (signals.get("fundamentals", {}).get("pe_ratio") or 999) < 15 else 0
        if fund_quick < 4:
            return False, f"Future outlook weak (growth score {g_score}/100) — not aligned with product quality thesis"

    # ── Fundamental scoring (need 3 of 5) ─────────────────────────────────
    fund_score = 0
    fund_hits  = []

    eps = fund.get("eps_growth_yoy")
    if eps is None or eps > config.CRITERIA_EPS_GROWTH_MIN:
        fund_score += 1; fund_hits.append("eps")

    rev = fund.get("revenue_growth")
    if rev is None or rev > config.CRITERIA_REVENUE_GROWTH_MIN:
        fund_score += 1; fund_hits.append("rev")

    pe = fund.get("pe_ratio")
    # High-growth stocks (score >= 70 or PEG < 1.5) get a wider P/E ceiling
    pe_max = config.CRITERIA_PE_MAX
    if (g_score or 0) >= 70 or (growth.get("peg_ratio") or 99) < 1.5:
        pe_max = 200
    if pe is None or pe < pe_max:
        fund_score += 1; fund_hits.append("pe")

    margin = fund.get("profit_margin")
    if margin is None or margin > config.CRITERIA_PROFIT_MARGIN_MIN:
        fund_score += 1; fund_hits.append("margin")

    fcf = (fin.get("fmp") or {}).get("free_cash_flow")
    if fcf is None or (isinstance(fcf, (int, float)) and fcf > 0):
        fund_score += 1; fund_hits.append("fcf")

    if fund_score < config.CRITERIA_FUNDAMENTALS_NEEDED:
        return False, f"Fundamentals: {fund_score}/5 passed (need {config.CRITERIA_FUNDAMENTALS_NEEDED}) — weak on {set(['eps','rev','pe','margin','fcf'])-set(fund_hits)}"

    # ── Momentum scoring (need 2 of 4) ────────────────────────────────────
    mom_score = 0
    mom_hits  = []

    r1m = tech.get("return_1m")
    if r1m is None or r1m > -5:
        mom_score += 1; mom_hits.append("1m")

    r3m = tech.get("return_3m")
    if r3m is None or r3m > 0:
        mom_score += 1; mom_hits.append("3m")

    vr = tech.get("volume_ratio")
    if vr is None or vr > config.MIN_VOLUME_RATIO:
        mom_score += 1; mom_hits.append("vol")

    if tech.get("macd_cross") != "bearish":
        mom_score += 1; mom_hits.append("macd")

    if mom_score < config.CRITERIA_MOMENTUM_NEEDED:
        return False, f"Momentum: {mom_score}/4 passed (need {config.CRITERIA_MOMENTUM_NEEDED})"

    # ── Technical scoring (need 2 of 3) ───────────────────────────────────
    # Check 1: above SMA50 (short-term uptrend)
    # Check 2: no death cross (no confirmed macro downtrend — SMA200 lags too much in recovery)
    # Check 3: not at BB upper (not overextended)
    tech_score = 0

    price = tech.get("price")
    sma50 = tech.get("sma50")
    if (price and sma50 and price > sma50) or tech.get("golden_cross"):
        tech_score += 1

    if not tech.get("death_cross"):
        tech_score += 1

    if tech.get("bb_position") != "above_upper":
        tech_score += 1

    if tech_score < config.CRITERIA_TECHNICAL_NEEDED:
        return False, f"Technical: {tech_score}/3 passed (need {config.CRITERIA_TECHNICAL_NEEDED})"

    return True, f"passed (fund={fund_score}/5, mom={mom_score}/4, tech={tech_score}/3)"


# ── Mid-growth entry criteria ──────────────────────────────────────────────────

def _check_mid_growth_entry(signals: dict) -> tuple[bool, str]:
    """
    Mid-growth: relaxed fundamentals (2/5), wider PE/margin tolerance,
    only 1/3 technical needed. Revenue acceleration > perfect margins.
    """
    tech   = signals.get("technical", {})
    fund   = signals.get("fundamentals", {})
    sent   = signals.get("sentiment", {})
    cong   = signals.get("congressional", {})
    mkt    = signals.get("market_context", {})
    earn   = signals.get("earnings", {})
    fin    = signals.get("financial_data", {})
    growth = signals.get("future_growth", {})

    # Hard blocks (shared with all tiers)
    fg_score = (mkt.get("fear_and_greed") or {}).get("score")
    if fg_score is not None and fg_score < config.CRITERIA_FG_PANIC:
        return False, f"Market extreme fear (F&G={fg_score:.0f})"

    vix_val     = (mkt.get("vix") or {}).get("vix", 0)
    market_risk = mkt.get("market_risk", "unknown")
    if vix_val and vix_val > 40 and market_risk == "extreme_fear":
        return False, f"Panic mode (VIX={vix_val:.0f}) — mid_growth blocked during market panic"

    rsi = tech.get("rsi")
    if rsi is not None:
        if rsi < config.CRITERIA_RSI_MIN:
            return False, f"RSI {rsi:.1f} — extreme panic"
        if rsi > config.CRITERIA_RSI_MAX:
            return False, f"RSI {rsi:.1f} — extreme overbought"

    price = tech.get("price")
    if price is not None and price < config.MIN_STOCK_PRICE:
        return False, f"Price ${price:.2f} below minimum — liquidity risk"

    dte = _days_to_earnings(earn)
    if dte is not None and 0 <= dte <= config.CRITERIA_EARNINGS_DAYS:
        return False, f"Earnings in {dte} day(s) — binary event"

    if cong.get("net_signal") == "bearish" and sent.get("label") == "negative":
        return False, "Congress selling + negative sentiment"

    g_score = (growth or {}).get("score")
    if g_score is not None and g_score < 25:
        return False, f"Future outlook too weak for mid_growth ({g_score}/100)"

    # Fundamentals: relaxed — 2/5, wider PE/margin tolerance
    fund_score = 0
    fund_hits  = []
    eps = fund.get("eps_growth_yoy")
    if eps is None or eps > config.CRITERIA_EPS_GROWTH_MIN:
        fund_score += 1; fund_hits.append("eps")
    rev = fund.get("revenue_growth")
    if rev is None or rev > config.CRITERIA_REVENUE_GROWTH_MIN:
        fund_score += 1; fund_hits.append("rev")
    pe = fund.get("pe_ratio")
    if pe is None or pe < config.MID_GROWTH_PE_MAX:
        fund_score += 1; fund_hits.append("pe")
    margin = fund.get("profit_margin")
    if margin is None or margin > config.MID_GROWTH_MARGIN_MIN:
        fund_score += 1; fund_hits.append("margin")
    fcf = (fin.get("fmp") or {}).get("free_cash_flow") if fin else None
    if fcf is None or (isinstance(fcf, (int, float)) and fcf > 0):
        fund_score += 1; fund_hits.append("fcf")

    if fund_score < config.MID_GROWTH_FUNDAMENTALS_NEEDED:
        return False, f"Mid-growth fundamentals: {fund_score}/5 (need {config.MID_GROWTH_FUNDAMENTALS_NEEDED})"

    # Momentum: 2/4
    mom_score = 0
    if (tech.get("return_1m") is None) or tech.get("return_1m", -99) > -5:
        mom_score += 1
    if (tech.get("return_3m") is None) or tech.get("return_3m", -99) > 0:
        mom_score += 1
    if (tech.get("volume_ratio") is None) or tech.get("volume_ratio", 0) > config.MIN_VOLUME_RATIO:
        mom_score += 1
    if tech.get("macd_cross") != "bearish":
        mom_score += 1

    if mom_score < config.MID_GROWTH_MOMENTUM_NEEDED:
        return False, f"Mid-growth momentum: {mom_score}/4 (need {config.MID_GROWTH_MOMENTUM_NEEDED})"

    # Technical: just 1/3 — only block fully confirmed crash
    tech_score = 0
    p, s50 = tech.get("price"), tech.get("sma50")
    if (p and s50 and p > s50) or tech.get("golden_cross"):
        tech_score += 1
    if not tech.get("death_cross"):
        tech_score += 1
    if tech.get("bb_position") != "above_upper":
        tech_score += 1

    if tech_score < config.MID_GROWTH_TECHNICAL_NEEDED:
        return False, f"Mid-growth technical: {tech_score}/3 (need {config.MID_GROWTH_TECHNICAL_NEEDED})"

    return True, f"mid_growth passed (fund={fund_score}/5, mom={mom_score}/4, tech={tech_score}/3)"


# ── Speculative / Moonshot entry criteria ──────────────────────────────────────

def _check_speculative_entry(signals: dict) -> tuple[bool, str]:
    """
    Speculative/moonshot: NO fundamentals gate.
    Uses a VC-style thesis gate: 2 of 5 signals (analyst upside, institutional
    buying, narrative momentum, revenue trajectory, future growth score).
    """
    tech   = signals.get("technical", {})
    mkt    = signals.get("market_context", {})
    earn   = signals.get("earnings", {})
    cong   = signals.get("congressional", {})
    sent   = signals.get("sentiment", {})
    insd   = signals.get("insider", {})
    soc    = signals.get("social", {})
    fund   = signals.get("fundamentals", {})
    growth = signals.get("future_growth", {})
    fin    = signals.get("financial_data", {}) or {}

    # Hard block 1: Macro panic — no speculative buys in a crash
    fg_score = (mkt.get("fear_and_greed") or {}).get("score")
    if fg_score is not None and fg_score < config.CRITERIA_FG_PANIC:
        return False, f"Market extreme fear (F&G={fg_score:.0f}) — no speculative buys"

    # Hard block 2: Only extreme RSI capitulation (not the standard 25 threshold)
    rsi = tech.get("rsi")
    if rsi is not None and rsi < 20:
        return False, f"RSI {rsi:.1f} — speculative stock in capitulation, wait for base"

    # Hard block 3: Price floor
    price = tech.get("price")
    if price is not None and price < config.MIN_STOCK_PRICE:
        return False, f"Price ${price:.2f} below minimum — liquidity risk"

    # Hard block 4: Earnings binary event
    dte = _days_to_earnings(earn)
    if dte is not None and 0 <= dte <= config.CRITERIA_EARNINGS_DAYS:
        return False, f"Earnings in {dte} day(s) — binary event"

    # Hard block 5: Congress selling + negative sentiment = thesis broken
    if cong.get("net_signal") == "bearish" and sent.get("label") == "negative":
        return False, "Congress selling + negative sentiment — thesis may be broken"

    # THESIS GATE: 2 of 5 signals
    thesis_score = 0
    thesis_hits  = []

    # Signal 1: Analyst upside
    av = (fin.get("alpha_vantage") or {})
    target = av.get("analyst_target")
    if target and price and price > 0:
        try:
            upside = (float(target) - price) / price * 100
            if upside > config.SPEC_ANALYST_UPSIDE_MIN:
                thesis_score += 1; thesis_hits.append(f"analyst_upside={upside:.0f}%")
        except (ValueError, TypeError):
            pass
    fg_upside = (growth or {}).get("target_upside")
    if fg_upside and fg_upside > config.SPEC_ANALYST_UPSIDE_MIN and not any("analyst_upside" in h for h in thesis_hits):
        thesis_score += 1; thesis_hits.append(f"growth_target_upside={fg_upside:.0f}%")

    # Signal 2: Institutional conviction
    if cong.get("net_signal") == "bullish" or insd.get("net_signal") == "bullish":
        thesis_score += 1; thesis_hits.append("institutional_buying")

    # Signal 3: Narrative momentum
    soc_label = (soc or {}).get("combined_label")
    if soc_label == "bullish" or sent.get("label") == "positive":
        thesis_score += 1; thesis_hits.append("narrative_momentum")

    # Signal 4: Revenue growing (even pre-profit must show growth)
    rev_growth = fund.get("revenue_growth")
    if rev_growth is None or rev_growth > config.SPEC_REVENUE_GROWTH_MIN:
        thesis_score += 1; thesis_hits.append(f"revenue_ok({rev_growth})")

    # Signal 5: Future growth score (TAM + tailwinds)
    g_score = (growth or {}).get("score")
    if g_score is None or g_score >= config.SPEC_GROWTH_SCORE_MIN:
        thesis_score += 1; thesis_hits.append(f"growth_score={'N/A' if g_score is None else g_score}")

    if thesis_score < config.SPEC_THESIS_SIGNALS_NEEDED:
        return False, f"Speculative thesis weak: {thesis_score}/5 (need {config.SPEC_THESIS_SIGNALS_NEEDED}) — {thesis_hits}"

    # Minimal technical sanity: not in confirmed crash (death cross + >15% below SMA50)
    p, s50 = tech.get("price"), tech.get("sma50")
    far_below_sma50 = (p and s50 and p < s50 * 0.85)
    if tech.get("death_cross") and far_below_sma50:
        return False, "Speculative: death cross + >15% below SMA50 — wait for base"

    return True, f"speculative thesis passed ({thesis_score}/5: {thesis_hits})"


# ── Medium-term entry criteria ─────────────────────────────────────────────────
# Applied on top of the tier gate when bucket="medium_term".
# Requires: relative strength, identifiable catalyst, confirmed uptrend, volume.

def _check_medium_term_entry(signals: dict) -> tuple[bool, str]:
    """
    Medium-term bucket (3-8 week catalyst plays).
    Requires a catalyst window, momentum confirmation, and uptrend structure.
    """
    tech  = signals.get("technical", {})
    earn  = signals.get("earnings", {})
    sent  = signals.get("sentiment", {})
    soc   = signals.get("social", {})
    mkt   = signals.get("market_context", {})

    # Hard block: no medium-term in risk-off / bear market
    vix_val     = (mkt.get("vix") or {}).get("vix", 0)
    market_risk = mkt.get("market_risk", "unknown")
    if vix_val and vix_val > 30 and market_risk in ("extreme_fear", "fear"):
        return False, "Bear market (VIX>30 + fear regime) — medium-term bucket blocked"

    # ── Catalyst window gate ───────────────────────────────────────────────────
    # Earnings within 3-8 weeks is the primary catalyst.
    # Alternatively, positive research/sentiment signals act as a soft catalyst proxy.
    dte = _days_to_earnings(earn)
    weeks_min = config.MEDIUM_TERM_CATALYST_WEEKS_MIN * 7
    weeks_max = config.MEDIUM_TERM_CATALYST_WEEKS_MAX * 7
    has_earnings_catalyst = dte is not None and weeks_min <= dte <= weeks_max
    has_soft_catalyst = (
        sent.get("label") == "positive" or
        (soc or {}).get("combined_label") == "bullish"
    )
    if not has_earnings_catalyst and not has_soft_catalyst:
        return False, (
            f"Medium-term: no catalyst in {config.MEDIUM_TERM_CATALYST_WEEKS_MIN}-"
            f"{config.MEDIUM_TERM_CATALYST_WEEKS_MAX} week window "
            f"(earnings={dte}d, sentiment={sent.get('label')})"
        )

    # ── Relative strength / momentum gates ───────────────────────────────────
    # RSI and MACD are the hard core — both required (QUANT amendment).
    # Three confirmatory checks: positive 1M, positive 3M >2%, elevated volume.
    # Need core (RSI + MACD) + at least one confirmatory = ≥3/5 with a mandatory minimum.
    r1m = tech.get("return_1m")
    r3m = tech.get("return_3m")
    rsi = tech.get("rsi")

    rsi_ok  = rsi is not None and 45 <= rsi <= 72
    macd_ok = tech.get("macd_cross") != "bearish"

    # Hard core: RSI AND MACD must both pass
    if not rsi_ok:
        return False, (
            f"Medium-term: RSI {rsi} outside healthy range 45-72 — required core signal"
        )
    if not macd_ok:
        return False, (
            f"Medium-term: MACD bearish — required core signal failed"
        )

    # Confirmatory (need ≥1 of 3)
    conf_score = 0
    if r1m is not None and r1m > 0:           conf_score += 1
    if r3m is not None and r3m > 2:           conf_score += 1
    if tech.get("volume_ratio", 0) >= 1.2:    conf_score += 1

    if conf_score < 1:
        return False, (
            f"Medium-term: momentum weak — RSI/MACD pass but no confirmatory signal "
            f"(1M={r1m}, 3M={r3m}, vol_ratio={tech.get('volume_ratio')})"
        )

    # ── Uptrend structure ─────────────────────────────────────────────────────
    # SMA20 is mandatory (tactically relevant for 3-8 week holds); SMA50 or SMA200 as fallback.
    # QUANT amendment: "SMA20 AND (SMA50 OR SMA200)" — SMA20 required.
    price  = tech.get("price")
    sma20  = tech.get("sma20")
    sma50  = tech.get("sma50")
    sma200 = tech.get("sma200")
    above_sma20 = price and sma20  and price > sma20
    above_sma50 = price and sma50  and price > sma50
    above_sma200 = price and sma200 and price > sma200

    if tech.get("death_cross"):
        return False, "Medium-term: death cross — macro downtrend, no medium-term entry"
    if not above_sma20:
        return False, (
            f"Medium-term: price ${price} below SMA20 ${sma20} — SMA20 is mandatory for medium-term entries"
        )
    if not above_sma50 and not above_sma200:
        return False, (
            f"Medium-term: price below both SMA50 and SMA200 — no established uptrend beyond short-term"
        )
    if tech.get("bb_position") == "above_upper" and (r1m or 0) < 5:
        return False, "Medium-term: extended above Bollinger upper on weak 1M — poor timing"

    return True, (
        f"medium_term passed: catalyst={'earnings ' + str(dte) + 'd' if has_earnings_catalyst else 'sentiment'}, "
        f"conf={conf_score}/3, uptrend={'SMA20' if above_sma20 else 'SMA50'}"
    )


def check_reunderwriting_alerts(days_held_map: dict) -> list[dict]:
    """
    Identify long-term large_growth positions approaching 90-day re-underwriting.
    Returns list of {symbol, days_held, action: 'REUNDERWRITE'} alerts.
    """
    alerts = []
    try:
        symbols = _db.get_positions_for_reunderwriting(
            window_days=config.LONG_TERM_REUNDERWRITE_WINDOW
        )
    except Exception:
        return alerts
    for sym in symbols:
        days = days_held_map.get(sym, 0)
        if days > 0:
            alerts.append({
                "symbol": sym,
                "action": "REUNDERWRITE",
                "reason": (
                    f"90-day re-underwriting due ({days}d held) — "
                    "migrate to confirmed long_term or exit within 10 trading days"
                ),
            })
    return alerts


# ── BTC entry criteria ─────────────────────────────────────────────────────────

def check_btc_entry(signals: dict) -> tuple[bool, str]:
    """BTC-specific entry criteria — crypto has no fundamentals, pure momentum/sentiment."""
    tech = signals.get("technical", {})
    mkt  = signals.get("market_context", {})
    sent = signals.get("sentiment", {})
    soc  = signals.get("social", {})

    # Hard Block 1: Macro panic — equity panic drags BTC down
    vix_val = (mkt.get("vix") or {}).get("vix")
    if vix_val and vix_val > config.BTC_VIX_MAX:
        return False, f"VIX={vix_val:.1f} — equity panic, BTC likely to drop"

    # Hard Block 2: Market Fear & Greed extreme
    fg = (mkt.get("fear_and_greed") or {}).get("score")
    if fg is not None and fg < config.BTC_FG_PANIC:
        return False, f"Extreme fear (F&G={fg:.0f}) — no BTC buys in panic"

    # Hard Block 3: RSI extremes (crypto-specific range)
    rsi = tech.get("rsi")
    if rsi is not None:
        if rsi < config.BTC_RSI_MIN:
            return False, f"BTC RSI {rsi:.1f} — capitulation territory, wait for base"
        if rsi > config.BTC_RSI_MAX:
            return False, f"BTC RSI {rsi:.1f} — overbought, wait for pullback"

    # Hard Block 4: Downtrend confirmed
    if tech.get("death_cross"):
        return False, "BTC death cross (SMA50 < SMA200) — macro downtrend active"

    # Hard Block 5: MACD bearish crossover AND negative sentiment (double confirm)
    if tech.get("macd_cross") == "bearish" and (sent.get("label") == "negative" or soc.get("combined_label") == "bearish"):
        return False, "BTC MACD bearish + negative sentiment — momentum turning down"

    # Momentum check (need 2 of 3)
    mom_score = 0
    r1m = tech.get("return_1m")
    if r1m is None or r1m > -10:
        mom_score += 1
    r3m = tech.get("return_3m")
    if r3m is None or r3m > -15:
        mom_score += 1
    if tech.get("macd_cross") != "bearish":
        mom_score += 1

    if mom_score < 2:
        return False, f"BTC momentum weak: {mom_score}/3"

    # Trend check
    price  = tech.get("price")
    sma50  = tech.get("sma50")
    sma200 = tech.get("sma200")
    above_sma50  = price and sma50  and price > sma50
    above_sma200 = price and sma200 and price > sma200

    if not above_sma50 and not above_sma200:
        return False, "BTC below both SMA50 and SMA200 — no uptrend"

    return True, f"BTC criteria passed (RSI={f'{rsi:.1f}' if rsi is not None else 'N/A'}, mom={mom_score}/3)"


# ── Exit monitoring ────────────────────────────────────────────────────────────

def check_hard_cap_violations(positions: list, equity: float) -> list[dict]:
    """
    Hard position cap: trim any position over MAX_POSITION_PCT (8%) every cycle.
    Calculates exact excess to sell — not a fixed 33%. Runs before the committee
    phase so oversized positions self-correct without manual intervention.
    """
    violations = []
    if equity <= 0:
        return violations
    for p in positions:
        sym = p.get("symbol", "")
        if "/" in sym:  # skip crypto
            continue
        qty   = abs(float(p.get("qty") or 0))
        price = float(p.get("current_price") or 0)
        if qty <= 0 or price <= 0:
            continue
        pos_pct = qty * price / equity * 100
        if pos_pct > config.MAX_POSITION_PCT + 0.1:   # 0.1% buffer avoids float-rounding noise
            target_mv  = equity * config.MAX_POSITION_PCT / 100
            current_mv = qty * price
            excess_mv  = current_mv - target_mv
            trim_qty   = round(excess_mv / price, 6)
            if trim_qty > 0 and excess_mv >= 50:       # minimum $50 trim — ignore micro-rounding
                violations.append({
                    "symbol":      sym,
                    "trim_qty":    trim_qty,
                    "excess_mv":   round(excess_mv, 2),
                    "current_pct": round(pos_pct, 1),
                    "reason": (
                        f"hard_cap: {pos_pct:.1f}% > {config.MAX_POSITION_PCT}% limit — "
                        f"trimming {trim_qty:.4f} sh (${excess_mv:,.0f}) back to {config.MAX_POSITION_PCT}%"
                    ),
                })
    return violations


def check_bearish_flow_alerts(positions: list, signals_map: dict) -> list[dict]:
    """
    Check held positions for bearish options flow alerts from Unusual Whales.
    Returns list of {symbol, action: 'BEARISH_FLOW_REVIEW', reason}.
    Applies regardless of shadow mode — risk management is always live.
    """
    alerts = []
    for p in positions:
        sym = p.get("symbol", "")
        uw  = (signals_map.get(sym) or {}).get("options_flow") or {}
        if uw.get("bearish_alert") and uw.get("normalized_prem_pct", 0) >= 70:
            alerts.append({
                "symbol": sym,
                "action": "BEARISH_FLOW_REVIEW",
                "reason": (
                    f"BEARISH_FLOW_ALERT: put sweep on {sym} — "
                    f"norm_pct={uw['normalized_prem_pct']}, "
                    f"C/P={uw.get('call_put_ratio', '?')}, "
                    f"expiry_score={uw.get('expiry_alignment_score', '?')} — "
                    f"48h decision window: reaffirm thesis or exit"
                ),
            })
    return alerts


def check_stops(positions: list, signals_map: dict = None, days_held_map: dict = None) -> list[dict]:
    """Check open positions for exit signals. Returns list of {symbol, action, reason}."""
    exits       = []
    signals_map = signals_map or {}
    days_held_map = days_held_map or {}

    # Load bucket assignments once for the whole batch
    try:
        bucket_map = _db.get_bucket_assignments()
    except Exception:
        bucket_map = {}

    for p in positions:
        sym  = p["symbol"]
        pct  = p.get("unrealized_plpc", 0)
        tech = signals_map.get(sym, {}).get("technical", {})
        rsi  = tech.get("rsi")
        bucket = bucket_map.get(sym, "long_term")

        if _is_btc(sym):
            stop = config.BTC_STOP_LOSS_PCT
            tier = "crypto"
        else:
            tier = config.TICKER_TIERS.get(sym, "mid_growth")
            # Medium-term uses a tighter fixed stop regardless of tier
            if bucket == "medium_term":
                stop = config.MEDIUM_TERM_STOP_LOSS_PCT
            else:
                stop = config.STOP_LOSS_BY_TIER.get(tier, config.STOP_LOSS_PCT)

        # ATR-based stop (preferred) — replaces fixed % when ATR is available
        atr          = tech.get("atr")
        current_price = tech.get("price")
        entry_price   = float(p.get("avg_entry_price") or p.get("avg_entry") or 0)
        atr_stop_hit  = False
        if not _is_btc(sym) and atr and entry_price > 0 and current_price and current_price > 0:
            mult = (config.ATR_STOP_MULT_MEGA_LARGE
                    if tier in ("mega", "large_growth")
                    else config.ATR_STOP_MULT_MID_SPEC)
            atr_stop_price = entry_price - (mult * atr)
            if current_price <= atr_stop_price:
                exits.append({"symbol": sym, "action": "SELL",
                               "reason": f"ATR_stop: price ${current_price:.2f} ≤ stop ${atr_stop_price:.2f} "
                                         f"(entry ${entry_price:.2f} - {mult}×ATR ${atr:.2f})"})
                atr_stop_hit = True

        if atr_stop_hit:
            continue

        # Fallback fixed % stop (when ATR unavailable)
        if pct <= -stop:
            exits.append({"symbol": sym, "action": "SELL", "reason": f"stop_loss ({pct:.1f}%)"})
            continue

        # Trim rule: winner grown too large OR parabolic gain
        days = days_held_map.get(sym, 0)
        pos_pct_of_portfolio = abs(p.get("market_value", 0)) / max(p.get("portfolio_equity", 1), 1) * 100 \
            if p.get("portfolio_equity") else None
        if pos_pct_of_portfolio is None:
            pos_pct_of_portfolio = p.get("pct", 0)
        max_tier_alloc = max(config.TIER_ALLOC.get(tier, {}).values(), default=config.MAX_POSITION_PCT)
        trim_threshold = max_tier_alloc * config.TRIM_TRIGGER_MULTIPLE
        if pos_pct_of_portfolio >= trim_threshold:
            exits.append({"symbol": sym, "action": "TRIM",
                           "reason": f"trim_rule: position {pos_pct_of_portfolio:.1f}% ≥ {trim_threshold:.1f}% "
                                     f"(1.4× max tier alloc {max_tier_alloc:.1f}%) — trim 33%"})
            continue
        if pct >= config.TRIM_FAST_GAIN_PCT and days < 30:
            exits.append({"symbol": sym, "action": "TRIM",
                           "reason": f"trim_rule: +{pct:.1f}% gain in {days}d (<30d parabolic) — trim 33%"})
            continue

        # Dead money: speculative → REVIEW; medium-term → shorter 45d window; others → sell
        days = days_held_map.get(sym, 0)
        if tier == "speculative":
            if days >= config.DEAD_MONEY_DAYS and pct < config.DEAD_MONEY_MIN_PCT:
                exits.append({"symbol": sym, "action": "REVIEW",
                               "reason": f"speculative_dead_money ({days}d, {pct:.1f}%) — is entry thesis still intact?"})
                continue
        elif bucket == "medium_term":
            # Medium-term: catalyst should have resolved by 45 days; exit if still flat
            if days >= config.MEDIUM_TERM_DEAD_MONEY_DAYS and pct < config.MEDIUM_TERM_DEAD_MONEY_MIN_PCT:
                exits.append({"symbol": sym, "action": "SELL",
                               "reason": f"medium_term_dead_money ({days}d, {pct:.1f}%) — catalyst window expired"})
                continue
        else:
            skip_dead_money = False
            if tier == "mid_growth":
                rev_g = (signals_map.get(sym, {}).get("fundamentals") or {}).get("revenue_growth") or 0
                skip_dead_money = rev_g > config.MID_GROWTH_DEAD_MONEY_REV_EXEMPT
            if not skip_dead_money:
                if days >= config.DEAD_MONEY_DAYS and pct < config.DEAD_MONEY_MIN_PCT:
                    exits.append({"symbol": sym, "action": "SELL", "reason": f"dead_money ({days}d, {pct:.1f}% gain)"})
                    continue

        # Speculative thesis re-evaluation: flag after 18 months (not auto-sell)
        if tier == "speculative":
            days = days_held_map.get(sym, 0)
            if days >= config.SPEC_THESIS_HOLD_MONTHS * 30:
                exits.append({"symbol": sym, "action": "REVIEW",
                               "reason": f"speculative_thesis_review: held {days}d ({days//30}mo) — revalidate thesis"})
                continue

        # Bucket/tier-aware trailing stop
        if tier == "speculative":
            ts_min, ts_drop = config.SPEC_TRAILING_STOP_MIN_GAIN, config.SPEC_TRAILING_STOP_1M_DROP
        elif bucket == "medium_term":
            ts_min, ts_drop = config.MEDIUM_TERM_TRAILING_STOP_MIN_GAIN, config.MEDIUM_TERM_TRAILING_STOP_1M_DROP
        elif tier == "mid_growth":
            ts_min, ts_drop = config.MID_TRAILING_STOP_MIN_GAIN, config.MID_TRAILING_STOP_1M_DROP
        else:
            ts_min, ts_drop = config.TRAILING_STOP_MIN_GAIN, config.TRAILING_STOP_1M_DROP

        if pct >= ts_min:
            r1m = tech.get("return_1m")
            if r1m is not None and r1m <= ts_drop:
                exits.append({"symbol": sym, "action": "SELL",
                               "reason": f"trailing_stop ({tier}): up {pct:.1f}% overall but 1M={r1m:.1f}%"})
                continue

        # Technical exits on profitable positions (need 2 signals for stocks)
        if pct > 0 and not _is_btc(sym):
            bearish_signals = 0
            if tech.get("macd_cross") == "bearish":
                bearish_signals += 1
            if rsi and rsi > config.EXIT_RSI_OVERBOUGHT:
                bearish_signals += 1
            if tech.get("death_cross"):
                bearish_signals += 1
            if tech.get("bb_position") == "above_upper" and tech.get("macd_cross") == "bearish":
                bearish_signals += 1
            if bearish_signals >= 2:
                exits.append({"symbol": sym, "action": "SELL", "reason": f"technical_exit ({bearish_signals} bearish signals: RSI={f'{rsi:.0f}' if rsi else '?'}, MACD={tech.get('macd_cross')})"})
                continue

        # BTC: single strong signal is enough
        if pct > 0 and _is_btc(sym):
            if rsi and rsi > 82 and tech.get("macd_cross") == "bearish":
                exits.append({"symbol": sym, "action": "SELL", "reason": f"BTC RSI={rsi:.0f} + MACD bearish — trimming profit"})
                continue
            if tech.get("death_cross"):
                exits.append({"symbol": sym, "action": "SELL", "reason": "BTC death cross — exiting"})
                continue

    return exits


# ── Post-Claude validation ─────────────────────────────────────────────────────

def validate(decision: dict, portfolio: dict) -> dict:
    action     = decision.get("action", "HOLD")
    confidence = decision.get("confidence", 0)
    asset_type = decision.get("asset_type", "stock")
    symbol     = decision.get("_symbol", "")

    # BUCKET: watchlist hold — no capital deployed, pass through unchanged
    if action == "BUCKET":
        _db.log_audit("bucket", symbol, f"conf={confidence} rationale={decision.get('rationale','')[:120]}")
        return {**decision, "allocation_pct": 0.0}

    if confidence < config.MIN_CONFIDENCE:
        reason = f"confidence {confidence}/10 below minimum {config.MIN_CONFIDENCE}/10"
        _db.log_audit("rule_breach", symbol, reason)
        return _hold(decision, reason)

    if action == "BUY":
        if portfolio.get("position_count", 0) >= config.MAX_POSITIONS:
            reason = "max open positions reached"
            _db.log_audit("rule_breach", symbol, reason)
            return _hold(decision, reason)

        # 70/30 bucket enforcement
        bucket    = decision.get("bucket", "long_term")
        tier      = config.TICKER_TIERS.get(symbol, "mid_growth")
        # Mega and speculative are always long_term
        if tier in ("mega", "speculative"):
            bucket = "long_term"
            decision = {**decision, "bucket": "long_term"}

        lt_pct  = portfolio.get("long_term_pct",  0)
        mt_pct  = portfolio.get("medium_term_pct", 0)
        lt_cnt  = portfolio.get("long_term_count",  0)
        mt_cnt  = portfolio.get("medium_term_count", 0)

        if bucket == "medium_term":
            # CRO circuit breaker: suspend medium-term entries during deep portfolio drawdowns
            drawdown = portfolio.get("portfolio_drawdown_pct", 0)
            if drawdown <= -config.PORTFOLIO_MT_SUSPEND_DRAWDOWN:
                reason = (f"mt_circuit_breaker: portfolio down {drawdown:.1f}% from peak "
                          f"(>{config.PORTFOLIO_MT_SUSPEND_DRAWDOWN}% threshold) — "
                          f"medium-term new entries suspended until recovery")
                _db.log_audit("circuit_breaker", symbol, reason)
                return _hold(decision, reason)

            if mt_cnt >= config.MAX_POSITIONS_MEDIUM_TERM:
                reason = f"medium-term sleeve full ({mt_cnt}/{config.MAX_POSITIONS_MEDIUM_TERM} positions)"
                _db.log_audit("rule_breach", symbol, reason)
                return _hold(decision, reason)
            alloc_est = decision.get("allocation_pct", 0)
            if mt_pct + alloc_est > config.MEDIUM_TERM_PCT_CAP:
                reason = (f"medium-term cap: {mt_pct:.1f}% + {alloc_est:.1f}% "
                          f"> {config.MEDIUM_TERM_PCT_CAP}%")
                _db.log_audit("rule_breach", symbol, reason)
                return _hold(decision, reason)
        else:
            if lt_cnt >= config.MAX_POSITIONS_LONG_TERM:
                reason = f"long-term sleeve full ({lt_cnt}/{config.MAX_POSITIONS_LONG_TERM} positions)"
                _db.log_audit("rule_breach", symbol, reason)
                return _hold(decision, reason)
            alloc_est = decision.get("allocation_pct", 0)
            if lt_pct + alloc_est > config.LONG_TERM_PCT_CAP:
                reason = (f"long-term cap: {lt_pct:.1f}% + {alloc_est:.1f}% "
                          f"> {config.LONG_TERM_PCT_CAP}%")
                _db.log_audit("rule_breach", symbol, reason)
                return _hold(decision, reason)

        # Add cadence: max 1 add per position per 10 trading days
        cadence_ok, cadence_reason = check_add_cadence(symbol, portfolio.get("holdings", []))
        if not cadence_ok:
            _db.log_audit("cadence_block", symbol, cadence_reason)
            return _hold(decision, cadence_reason)

        # Portfolio beta cap
        portfolio_beta = portfolio.get("portfolio_beta")
        if portfolio_beta and portfolio_beta >= config.PORTFOLIO_BETA_CAP:
            _db.log_audit("beta_warning", symbol,
                          f"portfolio_beta={portfolio_beta:.2f} >= cap {config.PORTFOLIO_BETA_CAP}")
            # Soft enforcement: reduce allocation by 33% rather than hard block
            decision = {**decision, "_beta_reduced": True}
        if asset_type == "option" and portfolio.get("options_pct", 0) >= config.MAX_OPTIONS_PCT:
            return _hold(decision, "max options exposure reached")
        if asset_type == "crypto" and portfolio.get("crypto_pct", 0) >= config.MAX_CRYPTO_PCT:
            return _hold(decision, "max crypto exposure reached")

        # Tier lookup
        tier = config.TICKER_TIERS.get(symbol, "mid_growth")

        # Speculative tier portfolio limits
        if tier == "speculative":
            if portfolio.get("speculative_count", 0) >= config.MAX_SPECULATIVE_POSITIONS:
                return _hold(decision, f"max speculative positions ({config.MAX_SPECULATIVE_POSITIONS}) reached")
            if portfolio.get("speculative_pct", 0) >= config.MAX_SPECULATIVE_PCT:
                return _hold(decision, f"max speculative exposure ({config.MAX_SPECULATIVE_PCT}%) reached")

        # Sector concentration check
        sector = config.SECTOR_MAP.get(symbol)
        if sector:
            sector_pct = portfolio.get("sector_pcts", {}).get(sector, 0)
            if sector_pct >= config.MAX_SECTOR_PCT:
                reason = f"sector '{sector}' at {sector_pct:.1f}% — max {config.MAX_SECTOR_PCT}%"
                _db.log_audit("rule_breach", symbol, reason)
                return _hold(decision, reason)
            # Long-term sleeve sub-rule: no sector > 28% of total portfolio within long-term
            if bucket == "long_term" and sector_pct >= config.LONG_TERM_SECTOR_CAP_PCT:
                reason = (f"long-term sector sub-rule: '{sector}' at {sector_pct:.1f}% "
                          f"≥ {config.LONG_TERM_SECTOR_CAP_PCT}% (40% of 70% sleeve)")
                _db.log_audit("lt_sector_cap", symbol, reason)
                return _hold(decision, reason)

        # Factor cluster concentration check — include the proposed position in the test
        # Use Claude's proposed allocation as a proxy (risk manager may adjust it later)
        alloc = decision.get("allocation_pct", 0)
        test_holdings = portfolio.get("holdings", []) + [{"symbol": symbol, "pct": alloc}]
        cluster_pcts = _compute_cluster_pcts(test_holdings)
        for cluster_name, tickers in config.FACTOR_CLUSTERS.items():
            if symbol in tickers:
                cluster_pct = cluster_pcts.get(cluster_name, 0)
                if cluster_pct >= config.FACTOR_CLUSTER_CAP * 100:
                    reason = (f"factor cluster '{cluster_name}' at {cluster_pct:.1f}% — "
                              f"max {config.FACTOR_CLUSTER_CAP*100:.0f}%")
                    _db.log_audit("cluster_cap", symbol, reason)
                    return _hold(decision, reason)

        # Bucket-aware allocation: medium-term capped at 6% regardless of tier
        if bucket == "medium_term":
            tier_table = config.MEDIUM_TERM_TIER_ALLOC
        else:
            tier_table = config.TIER_ALLOC.get(tier, config.TIER_ALLOC["mid_growth"])
        conf_key   = min(confidence, 10)
        base       = tier_table.get(conf_key)
        if base is None:
            # Find nearest lower key
            for k in sorted(tier_table.keys(), reverse=True):
                if k <= conf_key:
                    base = tier_table[k]
                    break
            if base is None:
                base = tier_table[min(tier_table.keys())]

        # Conviction bonuses
        congress_bonus = decision.get("_congress_bonus", 0)
        insider_bonus  = decision.get("_insider_bonus", 0)
        alloc = min(base + congress_bonus + insider_bonus, config.MAX_POSITION_PCT)

        # ADV liquidity cap applied on final alloc (after bonuses, to prevent silent override)
        adv_30d = decision.get("_adv_30d")
        if adv_30d and adv_30d > 0:
            equity      = portfolio.get("equity", 0)
            dollar_pos  = equity * (alloc / 100)
            adv_cap_usd = adv_30d * config.ADV_POSITION_PCT_MAX
            if dollar_pos > adv_cap_usd and adv_cap_usd > 0:
                capped_pct = (adv_cap_usd / equity * 100) if equity > 0 else alloc
                print(f"    [Risk] ADV cap: ${dollar_pos:,.0f} > {config.ADV_POSITION_PCT_MAX:.0%} "
                      f"of ADV ${adv_30d:,.0f} — capping alloc {alloc:.1f}% → {capped_pct:.1f}%")
                alloc = round(capped_pct, 2)

        # Bear market: halve all new position sizes in fear/extreme_fear regime
        if decision.get("_bear_market"):
            original = alloc
            alloc = round(alloc * config.BEAR_MARKET_SIZE_FACTOR, 2)
            print(f"    [Risk] Bear market mode: {symbol} alloc {original:.1f}% → {alloc:.1f}%")
            _db.log_audit("bear_market_reduction", symbol,
                          f"alloc {original:.1f}% → {alloc:.1f}% (fear regime)")

        # Portfolio beta cap: reduce allocation by 33% when portfolio beta is elevated
        if decision.get("_beta_reduced"):
            original = alloc
            alloc = round(alloc * 0.67, 2)
            print(f"    [Risk] Beta cap: {symbol} alloc {original:.1f}% → {alloc:.1f}%")

        # CCO audit: log alpha mandate rationale for every long-term BUY
        rationale = decision.get("rationale", "")
        if bucket == "long_term":
            _db.log_audit("alpha_mandate", symbol,
                          f"conf={confidence} bucket=long_term rationale={rationale[:200]}")

        # Log conviction override if committee used the ±0.5 discretionary slot
        if "override" in rationale.lower():
            _db.log_audit("conviction_override", symbol,
                          f"conf={confidence} rationale={rationale[:150]}")

        decision = {**decision, "allocation_pct": alloc}

    return decision


def apply_conviction_bonuses(decision: dict, signals: dict) -> dict:
    """Add congress/insider bonuses, ADV, and bear market flag to decision before validate()."""
    if decision.get("action") not in ("BUY", "BUCKET"):
        return decision
    cong = signals.get("congressional", {})
    insd = signals.get("insider", {})
    congress_bonus = config.CONGRESS_BONUS_PCT if cong.get("net_signal") == "bullish" else 0
    insider_bonus  = config.INSIDER_BONUS_PCT  if insd.get("net_signal") == "bullish"  else 0
    adv_30d = (signals.get("technical") or {}).get("adv_30d")

    # Bear market flag: reduce position sizes in fear regimes
    mkt         = signals.get("market_context", {})
    market_risk = mkt.get("market_risk", "unknown")
    vix_val     = (mkt.get("vix") or {}).get("vix", 0) or 0
    bear_market = market_risk == "extreme_fear" or vix_val > 40

    return {**decision, "_congress_bonus": congress_bonus,
            "_insider_bonus": insider_bonus, "_adv_30d": adv_30d,
            "_bear_market": bear_market}


def compute_qty(symbol: str, allocation_pct: float, price: float, portfolio: dict) -> float:
    equity = portfolio.get("equity", 0)
    dollar_amount = equity * (allocation_pct / 100)
    if price <= 0:
        return 0
    return max(round(dollar_amount / price, 6), 0)


def check_add_cadence(symbol: str, holdings: list) -> tuple[bool, str]:
    """Block adds on already-held positions if last BUY was < ADD_CADENCE_DAYS trading days ago."""
    held = any(h.get("symbol") == symbol for h in holdings)
    if not held:
        return True, ""
    last_buy_ts = _db.get_last_buy_date(symbol)
    if not last_buy_ts:
        return True, ""
    try:
        last_date = datetime.fromisoformat(last_buy_ts).date()
        calendar_days = (date.today() - last_date).days
        trading_days = max(0, int(calendar_days * 5 / 7))
        if trading_days < config.ADD_CADENCE_DAYS:
            return False, (f"add_cadence: last add ~{trading_days} trading days ago "
                           f"(need {config.ADD_CADENCE_DAYS})")
    except Exception:
        pass
    return True, ""


def _compute_cluster_pcts(holdings: list) -> dict:
    """Compute % of portfolio in each factor cluster from current holdings."""
    cluster_pcts = {name: 0.0 for name in config.FACTOR_CLUSTERS}
    for h in holdings:
        sym = h.get("symbol", "")
        pct = h.get("pct", 0)
        for cluster_name, tickers in config.FACTOR_CLUSTERS.items():
            if sym in tickers:
                cluster_pcts[cluster_name] = cluster_pcts.get(cluster_name, 0) + pct
    return cluster_pcts


def _hold(decision: dict, reason: str) -> dict:
    return {**decision, "action": "HOLD", "allocation_pct": 0,
            "rationale": f"Blocked: {reason}"}
