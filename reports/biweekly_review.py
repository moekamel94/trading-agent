"""
Bi-weekly committee performance review.

Runs every other Saturday (or Monday). Pulls 14 days of:
  - Trade outcomes (P&L, win rate, avg return)
  - Signal accuracy (which signals predicted correctly)
  - Regime accuracy (did the macro call lead to the right sector bets)
  - Basket performance vs SPY
  - What was added/removed from basket and why

Sends everything to the full committee (Sonnet) with a structured prompt.
The committee returns a JSON report with:
  - what_worked:   list of things performing above expectations
  - needs_work:    list of things underperforming with specific fix
  - remove:        tickers or signals to drop entirely
  - param_changes: specific threshold/weight changes ({"param": ..., "from": ..., "to": ...})
  - summary:       2-sentence executive summary

Auto-applies param_changes that are within safe bounds.
Flags remove/high-risk changes to Discord for human approval.
"""
import json
import os
import re
from datetime import datetime, timezone, timedelta

import anthropic
import yfinance as yf
import pandas as pd

import config
from database import db
from learning import tracker

_SAFE_PARAM_BOUNDS: dict[str, tuple] = {
    "REGIME_MIN_SECTOR_WEIGHT":  (0.40, 0.70),
    "CRITERIA_RSI_MIN":          (20,   40),
    "CRITERIA_RSI_MAX":          (70,   90),
    "CRITERIA_EPS_GROWTH_MIN":   (-0.30, 0.10),
    "CRITERIA_REVENUE_GROWTH_MIN": (-0.20, 0.25),
    "CRITERIA_PE_MAX":           (30,   100),
    "UW_CONSECUTIVE_BEARISH_EXIT": (2,  6),
    "CACHE_STALE_DAYS":          (3,   14),
    "REGIME_TOP_N_SECTORS":      (2,   5),
}


# ── Data collection ───────────────────────────────────────────────────────────

def _collect_trade_performance(days: int = 14) -> dict:
    """Summarise trade P&L over the review window."""
    outcomes = db.get_outcomes_for_review(days=days)
    trades   = db.get_trades(limit=200)

    # Filter trades to review window
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    recent_trades = [t for t in trades if t["ts"] >= cutoff]

    buys  = [t for t in recent_trades if t["action"] == "BUY"]
    sells = [t for t in recent_trades if t["action"] == "SELL"]

    # Compute realised P&L from matched buy/sell pairs
    buy_map: dict[str, list] = {}
    for t in buys:
        buy_map.setdefault(t["symbol"], []).append(t)

    realised_pl: list[dict] = []
    for t in sells:
        sym = t["symbol"]
        if sym in buy_map and buy_map[sym]:
            entry = buy_map[sym].pop(0)
            pct   = round((t["price"] - entry["price"]) / entry["price"] * 100, 2)
            realised_pl.append({"symbol": sym, "return_pct": pct,
                                 "held_days": None, "rationale": entry.get("rationale","")[:120]})

    # Unrealised: outcomes table gives 14d return on recent entries
    unrealised = [
        {"symbol": r["symbol"], "return_14d": r.get("return_14d"),
         "regime": r.get("regime_label"), "sector": r.get("sector_key")}
        for r in outcomes if r.get("return_14d") is not None
    ]

    win_rate = None
    avg_ret  = None
    if unrealised:
        wins    = [u for u in unrealised if (u["return_14d"] or 0) > 0]
        win_rate = round(len(wins) / len(unrealised) * 100, 1)
        avg_ret  = round(sum(u["return_14d"] for u in unrealised) / len(unrealised), 2)

    return {
        "buys":          len(buys),
        "sells":         len(sells),
        "realised_pl":   realised_pl[:10],
        "unrealised_14d": unrealised[:15],
        "win_rate_pct":  win_rate,
        "avg_return_14d": avg_ret,
    }


def _collect_sector_performance(days: int = 14) -> dict:
    """Actual ETF returns for each of our tracked sectors over the review window."""
    spy_ret = _etf_return("SPY", days)
    results = {"spy_return_pct": spy_ret}
    for sector, etf in _SECTOR_ETFS.items():
        ret = _etf_return(etf, days)
        results[sector] = {"etf": etf, "return_pct": ret,
                           "vs_spy": round(ret - spy_ret, 2) if ret is not None and spy_ret is not None else None}
    return results


def _etf_return(ticker: str, days: int) -> float | None:
    try:
        hist = yf.Ticker(ticker).history(period=f"{days+5}d")
        if len(hist) < 2:
            return None
        start = float(hist["Close"].iloc[0])
        end   = float(hist["Close"].iloc[-1])
        return round((end - start) / start * 100, 2)
    except Exception:
        return None


_SECTOR_ETFS = {
    "defense":            "ITA",
    "energy_oil":         "XLE",
    "cyber":              "CIBR",
    "semis":              "SOXX",
    "ai_software":        "IGV",
    "nuclear":            "NLR",
    "commodities_metals": "GDX",
    "biotech":            "XBI",
    "healthcare":         "XLV",
    "fintech":            "FINX",
    "robotics":           "BOTZ",
    "ai_infra":           "PAVE",
    "ecommerce":          "IBUY",
    "mega_tech":          "QQQ",
}


def _rsi(closes: pd.Series, period: int = 14) -> float | None:
    """Simple RSI without an external TA lib."""
    if len(closes) < period + 1:
        return None
    delta = closes.diff().dropna()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, float("nan"))
    rsi   = 100 - (100 / (1 + rs))
    val   = rsi.iloc[-1]
    return round(float(val), 1) if not pd.isna(val) else None


def _collect_sector_momentum() -> dict:
    """
    Multi-timeframe ETF momentum + technicals for each tracked sector.
    Returns 5d/14d/30d/60d returns, RSI(14), 50-DMA status, 200-DMA status,
    and a momentum_direction derived from the return curve slope.
    """
    spy_close = None
    try:
        spy_hist  = yf.Ticker("SPY").history(period="90d")
        spy_close = spy_hist["Close"]
    except Exception:
        pass

    def _spy_ret(days):
        if spy_close is None or len(spy_close) < days:
            return None
        return round((float(spy_close.iloc[-1]) - float(spy_close.iloc[-days])) /
                     float(spy_close.iloc[-days]) * 100, 2)

    results = {
        "spy": {
            "return_5d":  _spy_ret(5),
            "return_14d": _spy_ret(14),
            "return_30d": _spy_ret(30),
            "return_60d": _spy_ret(60),
        }
    }

    for sector, etf in _SECTOR_ETFS.items():
        try:
            hist   = yf.Ticker(etf).history(period="90d")
            closes = hist["Close"]
            if len(closes) < 20:
                results[sector] = {"etf": etf, "error": "insufficient_data"}
                continue

            last = float(closes.iloc[-1])

            def ret(n):
                if len(closes) < n:
                    return None
                return round((last - float(closes.iloc[-n])) / float(closes.iloc[-n]) * 100, 2)

            r5, r14, r30, r60 = ret(5), ret(14), ret(30), ret(60)

            # Momentum direction: compare recent (5d) slope to medium (30d) slope
            if r5 is not None and r30 is not None:
                if r5 > 0 and r5 > (r30 / 6 if r30 else 0):
                    momentum = "accelerating"
                elif r5 < 0 and r30 is not None and r30 > 0:
                    momentum = "decelerating"
                elif r5 < 0 and (r30 or 0) < 0:
                    momentum = "downtrend"
                else:
                    momentum = "steady"
            else:
                momentum = "unknown"

            # Moving averages
            ma50  = float(closes.rolling(50).mean().iloc[-1])  if len(closes) >= 50 else None
            ma200 = float(closes.rolling(200).mean().iloc[-1]) if len(closes) >= 200 else None

            spy_r14 = _spy_ret(14)
            vs_spy  = round(r14 - spy_r14, 2) if (r14 is not None and spy_r14 is not None) else None

            results[sector] = {
                "etf":            etf,
                "return_5d":      r5,
                "return_14d":     r14,
                "return_30d":     r30,
                "return_60d":     r60,
                "vs_spy_14d":     vs_spy,
                "rsi_14":         _rsi(closes),
                "above_50dma":    round(last / ma50  - 1, 3) if ma50  else None,
                "above_200dma":   round(last / ma200 - 1, 3) if ma200 else None,
                "momentum":       momentum,
            }
        except Exception as e:
            results[sector] = {"etf": etf, "error": str(e)[:60]}

    return results


def _collect_basket_sector_coverage(held_symbols: set[str]) -> dict:
    """
    For every sector in SECTOR_MAP, show:
    - all basket tickers (from TICKER_TIERS)
    - which ones we currently hold
    - which ones are available to enter (basket - held)
    - tier breakdown of available candidates
    This directly answers "where can we add?" for each sector.
    """
    basket = set(config.TICKER_TIERS.keys())

    coverage: dict[str, dict] = {}
    for sym, sector in config.SECTOR_MAP.items():
        if sym not in basket:
            continue
        entry = coverage.setdefault(sector, {
            "all_basket":   [],
            "held":         [],
            "available":    [],
        })
        entry["all_basket"].append(sym)
        if sym in held_symbols:
            entry["held"].append(sym)
        else:
            tier = config.TICKER_TIERS.get(sym, "unknown")
            entry["available"].append({"symbol": sym, "tier": tier})

    # Sort available by tier priority: mega > large_growth > mid_growth > speculative
    _tier_order = {"mega": 0, "large_growth": 1, "mid_growth": 2, "speculative": 3, "unknown": 4}
    for sec in coverage:
        coverage[sec]["available"].sort(key=lambda x: _tier_order.get(x["tier"], 4))
        coverage[sec]["total_basket"] = len(coverage[sec]["all_basket"])
        coverage[sec]["held_count"]   = len(coverage[sec]["held"])
        coverage[sec]["open_slots"]   = len(coverage[sec]["available"])

    return coverage


def _collect_regime_accuracy() -> dict:
    """Check if the sectors the macro regime predicted as winners actually outperformed."""
    outcomes = db.get_outcomes_for_review(days=30)
    if not outcomes:
        return {}

    regime_hits: dict[str, list[bool]] = {}
    for row in outcomes:
        if row.get("return_14d") is None:
            continue
        regime = row.get("regime_label", "unknown")
        win    = row["return_14d"] > 0
        regime_hits.setdefault(regime, []).append(win)

    return {
        regime: {
            "entries": len(hits),
            "win_rate": round(sum(hits) / len(hits) * 100, 1) if hits else None,
        }
        for regime, hits in regime_hits.items()
    }


def _collect_current_holdings() -> list[dict]:
    """
    Return each open position with entry metadata and current return.
    Uses position_tranches for thesis data + yfinance for current price.
    """
    tranches = db.get_all_tranches()
    holdings = []
    for t in tranches:
        sym = t["symbol"]
        entry_ts  = t.get("tranche1_ts") or ""
        entry_date = entry_ts[:10] if entry_ts else "unknown"

        # Compute days held
        try:
            held_days = (datetime.now(timezone.utc).date() -
                         datetime.fromisoformat(entry_ts).date()).days if entry_ts else None
        except Exception:
            held_days = None

        # Current price vs entry price (rough — uses first buy trade)
        current_ret = None
        try:
            hist = yf.Ticker(sym).history(period="2d")
            current_price = float(hist["Close"].iloc[-1]) if len(hist) >= 1 else None
        except Exception:
            current_price = None

        holdings.append({
            "symbol":               sym,
            "sector":               config.SECTOR_MAP.get(sym, "unknown"),
            "entry_date":           entry_date,
            "held_days":            held_days,
            "current_tranche":      t.get("current_tranche"),
            "final_confidence":     t.get("final_confidence"),
            "catalyst_note":        (t.get("catalyst_note") or "")[:200],
            "thesis_break_criteria": (t.get("thesis_break_criteria") or "")[:200],
            "price_target":         t.get("price_target"),
            "price_target_basis":   (t.get("price_target_basis") or "")[:120],
            "expected_holding_weeks": t.get("expected_holding_weeks"),
            "bucket":               t.get("bucket"),
            "current_price":        current_price,
        })
    return holdings


def _collect_sector_distribution(holdings: list[dict]) -> dict:
    """
    Group current holdings by sector. Compare holding count to current regime
    sector weights so the committee can see alignment vs. misalignment.
    """
    sector_counts: dict[str, list[str]] = {}
    for h in holdings:
        sec = h.get("sector", "unknown")
        sector_counts.setdefault(sec, []).append(h["symbol"])

    total = len(holdings) or 1

    # Pull current regime weights if available
    try:
        from signals.macro_regime import get_macro_regime
        regime_data = get_macro_regime()
        regime_weights = regime_data.get("sector_weights", {})
        regime_label   = regime_data.get("regime_label", "unknown")
    except Exception:
        regime_weights = {}
        regime_label   = "unknown"

    distribution = {
        "regime_label": regime_label,
        "sectors": {},
    }
    all_sectors = set(list(sector_counts.keys()) + list(regime_weights.keys()))
    for sec in sorted(all_sectors):
        tickers      = sector_counts.get(sec, [])
        regime_wt    = regime_weights.get(sec)
        holding_pct  = round(len(tickers) / total * 100, 1)

        # Classify alignment
        if regime_wt is not None:
            if regime_wt >= 0.70 and holding_pct == 0:
                alignment = "UNDERWEIGHT_IN_FOCUS_SECTOR"
            elif regime_wt < 0.40 and holding_pct > 0:
                alignment = "EXPOSED_TO_AVOID_SECTOR"
            elif regime_wt >= 0.70 and holding_pct > 0:
                alignment = "ALIGNED"
            else:
                alignment = "NEUTRAL"
        else:
            alignment = "NO_REGIME_DATA"

        distribution["sectors"][sec] = {
            "tickers":       tickers,
            "holding_count": len(tickers),
            "holding_pct":   holding_pct,
            "regime_weight": regime_wt,
            "alignment":     alignment,
        }

    return distribution


def _collect_basket_changes(days: int = 14) -> list[dict]:
    """Pull audit log for basket add/remove events in the review window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        import sqlite3
        path = os.path.join(os.path.dirname(__file__), "..", "trading.db")
        with sqlite3.connect(path) as c:
            rows = c.execute(
                """SELECT ts, event_type, symbol, detail FROM audit_log
                   WHERE ts >= ? AND event_type IN
                     ('basket_add','basket_remove','prelim_drop_criteria',
                      'prelim_drop_techfilter','sector_gate_reject')
                   ORDER BY ts DESC LIMIT 50""",
                (cutoff,)
            ).fetchall()
        return [{"ts": r[0], "event": r[1], "symbol": r[2], "detail": r[3]} for r in rows]
    except Exception:
        return []


# ── Report generation ─────────────────────────────────────────────────────────

def run_biweekly_review(dry_run: bool = False) -> dict:
    """
    Full bi-weekly committee review. Collects 14 days of performance data,
    asks the committee for structured feedback, applies safe changes.
    Returns the full report dict.
    """
    print("\n" + "="*60)
    print("BI-WEEKLY COMMITTEE PERFORMANCE REVIEW")
    print("="*60)

    period_end   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    period_start = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")

    # Collect all performance data
    trade_perf    = _collect_trade_performance(days=14)
    sector_perf   = _collect_sector_performance(days=14)
    regime_acc    = _collect_regime_accuracy()
    signal_acc    = tracker.get_accuracy_summary()
    basket_events = _collect_basket_changes(days=14)
    holdings      = _collect_current_holdings()
    sector_dist   = _collect_sector_distribution(holdings)
    sector_mom    = _collect_sector_momentum()
    held_symbols  = {h["symbol"] for h in holdings}
    basket_cov    = _collect_basket_sector_coverage(held_symbols)

    # Current config snapshot for context
    current_params = {
        k: getattr(config, k, None)
        for k in _SAFE_PARAM_BOUNDS
    }

    prompt = _build_review_prompt(
        period_start, period_end,
        trade_perf, sector_perf, regime_acc, signal_acc,
        basket_events, current_params,
        holdings, sector_dist,
        sector_mom, basket_cov,
    )

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        system=(
            "You are the full Kimmy investment committee — CIO, Chief Research Strategist, "
            "Chief Risk Officer, Quantitative Analyst, Data Analyst, and Portfolio Manager — "
            "conducting a structured bi-weekly self-review. "
            "Be precise, critical, and data-driven. Do not manufacture praise. "
            "If something is not working, say so directly and give a specific fix."
        ),
        messages=[{"role": "user", "content": prompt}],
    )

    raw = resp.content[0].text.strip()
    # Extract JSON block
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        print(f"  [Review] Failed to parse committee response — saving raw")
        report = {"raw": raw, "parse_error": True}
        changes_applied = []
    else:
        report = json.loads(m.group(0))
        changes_applied = _apply_safe_changes(report, dry_run=dry_run)

    db.log_review(period_start, period_end, report, changes_applied)

    _print_report(report, changes_applied)
    return report


def _build_review_prompt(period_start, period_end, trade_perf, sector_perf,
                          regime_acc, signal_acc, basket_events, current_params,
                          holdings=None, sector_dist=None,
                          sector_mom=None, basket_cov=None) -> str:
    def fmt(x):
        return json.dumps(x, indent=2)

    holdings      = holdings or []
    sector_dist   = sector_dist or {}
    sector_mom    = sector_mom or {}
    basket_cov    = basket_cov or {}

    return f"""BI-WEEKLY PERFORMANCE REVIEW — {period_start} to {period_end}

═══════════════════════════════════════
TRADE PERFORMANCE (last 14 days)
═══════════════════════════════════════
Buys: {trade_perf['buys']} | Sells: {trade_perf['sells']}
Win rate (14d outcomes): {trade_perf['win_rate_pct']}%
Average 14d return on new entries: {trade_perf['avg_return_14d']}%

Recent realised P&L:
{fmt(trade_perf['realised_pl'])}

14-day unrealised positions:
{fmt(trade_perf['unrealised_14d'])}

═══════════════════════════════════════
SECTOR PERFORMANCE vs SPY ({sector_perf.get('spy_return_pct')}% SPY)
═══════════════════════════════════════
{fmt({k: v for k, v in sector_perf.items() if k != 'spy_return_pct'})}

═══════════════════════════════════════
MACRO REGIME ACCURACY (30-day outcomes)
═══════════════════════════════════════
{fmt(regime_acc)}

═══════════════════════════════════════
SIGNAL ACCURACY (90-day hit rates)
═══════════════════════════════════════
{fmt(signal_acc)}

═══════════════════════════════════════
BASKET EVENTS (last 14 days)
═══════════════════════════════════════
{fmt(basket_events[:20])}

═══════════════════════════════════════
CURRENT HOLDINGS & ORIGINAL THESIS
═══════════════════════════════════════
{fmt(holdings)}

═══════════════════════════════════════
SECTOR DISTRIBUTION (holdings vs regime)
═══════════════════════════════════════
Regime: {sector_dist.get('regime_label', 'unknown')}
{fmt(sector_dist.get('sectors', {}))}

═══════════════════════════════════════
SECTOR MOMENTUM — MULTI-TIMEFRAME (5d / 14d / 30d / 60d)
RSI · 50-DMA gap · 200-DMA gap · momentum direction
═══════════════════════════════════════
{fmt(sector_mom)}

Interpretation guide:
  above_50dma / above_200dma = fraction above MA (positive = above, negative = below)
  momentum: accelerating | steady | decelerating | downtrend
  Use this to judge whether a sector is early/mid/late in its move.

═══════════════════════════════════════
BASKET COVERAGE BY SECTOR
(which tickers are in our basket, held vs. available to enter)
═══════════════════════════════════════
{fmt(basket_cov)}

═══════════════════════════════════════
CURRENT SYSTEM PARAMETERS
═══════════════════════════════════════
{fmt(current_params)}

═══════════════════════════════════════
COMMITTEE TASK
═══════════════════════════════════════
Review the data above as a full investment committee. Be honest and critical.

For each currently held position, restate the thesis and explain why the committee
is still holding it (or flag if the thesis is broken). For each active sector,
describe where you see opportunities and how the team should be hunting for them.

Return a SINGLE JSON object with this exact schema:
{{
  "what_worked": [
    {{"finding": "...", "evidence": "specific numbers from the data", "keep_doing": "..."}}
  ],
  "needs_work": [
    {{"problem": "...", "evidence": "...", "specific_fix": "concrete action"}}
  ],
  "remove": [
    {{"item": "ticker or signal name", "type": "ticker|signal|criterion",
      "reason": "...", "requires_human_approval": true}}
  ],
  "param_changes": [
    {{"param": "CONFIG_PARAM_NAME", "from": current_value, "to": new_value,
      "rationale": "...", "confidence": "high|medium|low"}}
  ],
  "sector_verdict": {{
    "concentrate": ["sector1", "sector2"],
    "reduce":      ["sector3"],
    "rationale":   "one sentence"
  }},
  "holdings_thesis": [
    {{
      "symbol":             "TICKER",
      "thesis_restatement": "1-2 sentence summary of the original investment thesis",
      "why_still_holding":  "specific reason the thesis remains intact as of today",
      "conviction_change":  "higher|unchanged|lower",
      "exit_condition":     "the exact condition that would make us sell"
    }}
  ],
  "sector_opportunity_outlook": {{
    "sector_name": {{
      "outlook":            "bullish|neutral|bearish",
      "momentum_read":      "accelerating|peaking|decelerating|recovering — based on 5d/14d/30d curve",
      "technical_health":   "strong|stretched|neutral|weak — RSI + DMA position",
      "regime_weight":      0.0,
      "why":                "one sentence on macro/regime driver for this sector right now",
      "opportunity":        "specific sub-theme or catalyst (e.g. 'Iran premium keeping energy bid', 'DOGE budget cuts benefiting CACI/KTOS')",
      "entry_candidates":   ["TICKER1","TICKER2"],
      "entry_trigger":      "exact condition to pull the trigger (e.g. 'CRWD breaks $380 on volume + RSI < 65')",
      "size_guidance":      "full|half|wait — whether to enter full size, scale in, or wait for setup",
      "invalidation":       "what would kill this thesis (e.g. 'ceasefire deal collapses oil below $90')",
      "action":             "add_new|increase_existing|hold|trim|avoid"
    }}
  }},
  "summary": "2-sentence executive summary of this review period"
}}

Rules:
- Only suggest param_changes for params in: {list(_SAFE_PARAM_BOUNDS.keys())}
- param_changes must be incremental — max 20% change from current value
- If win_rate > 60% and avg_return > 2%, call out what's working specifically
- If win_rate < 45%, this is a crisis — identify the root cause
- All ticker removals require_human_approval: true
- Be specific: "increase X by Y because Z signal had Z% hit rate" not "improve signals"
- holdings_thesis must cover EVERY symbol in the holdings list above
- sector_opportunity_outlook must cover every sector with holding_pct > 0 OR regime_weight >= 0.70
- conviction_change must reflect actual data (return, signal hits) not wishful thinking
- entry_candidates must come ONLY from the available (not held) tickers in basket_cov for that sector
- momentum_read must be derived from the actual 5d/14d/30d numbers in sector_mom, not invented
- technical_health: RSI < 40 = weak, 40-60 = neutral, 60-70 = strong, > 70 = stretched
- size_guidance = "wait" if RSI > 72 or momentum = "decelerating"; "half" if RSI 65-72; "full" otherwise
- If a sector has regime_weight >= 0.85 but we have zero holdings AND available candidates exist, this is a PRIORITY ENTRY — flag it explicitly in opportunity
"""


def _apply_safe_changes(report: dict, dry_run: bool = False) -> list[dict]:
    """
    Auto-apply param_changes that are within safe bounds and rated high confidence.
    Returns list of changes that were actually applied.
    """
    applied = []
    param_changes = report.get("param_changes", [])

    for change in param_changes:
        param     = change.get("param", "")
        new_val   = change.get("to")
        confidence = change.get("confidence", "low")
        rationale  = change.get("rationale", "")

        if confidence != "high":
            print(f"  [Review] SKIP {param} (confidence={confidence}) — needs human review")
            continue

        bounds = _SAFE_PARAM_BOUNDS.get(param)
        if not bounds:
            print(f"  [Review] SKIP {param} — not in approved change list")
            continue

        lo, hi = bounds
        if not (lo <= new_val <= hi):
            print(f"  [Review] SKIP {param}={new_val} — outside safe bounds [{lo}, {hi}]")
            continue

        current = getattr(config, param, None)
        if current is None:
            continue

        # Max 20% change per review cycle (prevents runaway changes)
        max_delta = abs(current) * 0.20
        if abs(new_val - current) > max_delta:
            print(f"  [Review] SKIP {param}: proposed change too large "
                  f"({current}→{new_val}, max delta {max_delta:.3f})")
            continue

        if not dry_run:
            _patch_config(param, new_val)
        applied.append({
            "param":     param,
            "from":      current,
            "to":        new_val,
            "rationale": rationale[:100],
            "applied":   not dry_run,
        })
        print(f"  [Review] {'DRY ' if dry_run else ''}APPLIED {param}: {current} → {new_val} | {rationale[:80]}")

    return applied


def _patch_config(param: str, new_val) -> bool:
    """Write updated parameter value back to config.py."""
    config_path = os.path.join(os.path.dirname(__file__), "..", "config.py")
    try:
        with open(config_path) as f:
            src = f.read()

        # Match "PARAM_NAME = <value>" pattern
        pattern = rf'^({re.escape(param)}\s*=\s*)(.+)$'
        new_src  = re.sub(pattern, rf'\g<1>{repr(new_val)}', src, flags=re.MULTILINE)

        if new_src == src:
            print(f"  [Review] WARNING: {param} not found in config.py — skipping patch")
            return False

        with open(config_path, "w") as f:
            f.write(new_src)

        # Reload config in running process
        setattr(config, param, new_val)
        return True
    except Exception as e:
        print(f"  [Review] Config patch failed for {param}: {e}")
        return False


def _print_report(report: dict, changes_applied: list) -> None:
    """Print the review report to stdout in readable form."""
    print(f"\n{'─'*60}")
    print(f"SUMMARY: {report.get('summary', 'No summary')}")
    print(f"{'─'*60}")

    if report.get("what_worked"):
        print("\n✅ WHAT WORKED:")
        for w in report["what_worked"]:
            print(f"  • {w.get('finding','')} [{w.get('evidence','')}]")

    if report.get("needs_work"):
        print("\n⚠️  NEEDS IMPROVEMENT:")
        for n in report["needs_work"]:
            print(f"  • {n.get('problem','')} → FIX: {n.get('specific_fix','')}")

    if report.get("remove"):
        print("\n🗑️  FLAGGED FOR REMOVAL (requires human approval):")
        for r in report["remove"]:
            print(f"  • {r.get('type','').upper()} {r.get('item','')} — {r.get('reason','')}")

    if changes_applied:
        print(f"\n⚙️  AUTO-APPLIED CHANGES ({len(changes_applied)}):")
        for c in changes_applied:
            print(f"  • {c['param']}: {c['from']} → {c['to']}")

    if report.get("sector_verdict"):
        sv = report["sector_verdict"]
        print(f"\n📊 SECTOR VERDICT: concentrate={sv.get('concentrate',[])} | "
              f"reduce={sv.get('reduce',[])} | {sv.get('rationale','')}")

    if report.get("holdings_thesis"):
        print("\n📋 HOLDINGS THESIS:")
        for h in report["holdings_thesis"]:
            conv = h.get("conviction_change", "unchanged")
            marker = "▲" if conv == "higher" else ("▼" if conv == "lower" else "─")
            print(f"  {marker} {h.get('symbol','')}: {h.get('thesis_restatement','')}")
            print(f"      Still holding: {h.get('why_still_holding','')}")
            print(f"      Exit if: {h.get('exit_condition','')}")

    if report.get("sector_opportunity_outlook"):
        print("\n🔭 SECTOR OPPORTUNITY OUTLOOK:")
        # Sort: add_new/increase first, then hold, then trim/avoid
        _action_order = {"add_new": 0, "increase_existing": 1, "hold": 2, "trim": 3, "avoid": 4}
        sorted_sectors = sorted(
            report["sector_opportunity_outlook"].items(),
            key=lambda kv: _action_order.get(kv[1].get("action", "hold"), 5)
        )
        for sector, data in sorted_sectors:
            action   = data.get("action", "hold").upper()
            outlook  = data.get("outlook", "neutral").upper()
            momentum = data.get("momentum_read", "")
            health   = data.get("technical_health", "")
            rw       = data.get("regime_weight", "?")
            print(f"\n  [{action}] {sector.upper()} — {outlook} | {momentum} | tech: {health} | regime wt: {rw}")
            print(f"    Why now:    {data.get('why','')}")
            print(f"    Opportunity: {data.get('opportunity','')}")
            candidates = data.get("entry_candidates", [])
            if candidates:
                size = data.get("size_guidance", "")
                print(f"    Candidates: {', '.join(candidates)}  [{size} size]")
            print(f"    Trigger:    {data.get('entry_trigger','')}")
            print(f"    Kills it:   {data.get('invalidation','')}")

    print(f"{'─'*60}\n")


def should_run_today() -> bool:
    """Return True if a bi-weekly review is due (last review was ≥13 days ago)."""
    last = db.get_last_review_date()
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    return (datetime.now(timezone.utc).date() - last_dt.date()).days >= 13
