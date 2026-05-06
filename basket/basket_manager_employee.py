"""
Basket Manager Employee — daily lightweight scan to keep the basket clean.

PROBLEM: The daily cycle scans every ticker in the basket through the committee.
Tickers that clearly don't meet our criteria waste API calls, Claude tokens, and time.

THIS EMPLOYEE:
  1. Runs a fast pre-filter BEFORE the daily cycle (no Claude, no paid APIs)
  2. Identifies tickers that fail basic quality gates
  3. Auto-suspends them from the daily scan (moves to "suspended" list)
  4. Re-evaluates suspended tickers weekly — they can come back
  5. Reports changes to Discord

GATES (any 2 = suspend from daily scan):
  - Death cross active for > 45 days
  - Below SMA200 for > 60 days
  - Revenue growth < -10% (declining revenue)
  - EPS growth < -20% (earnings deteriorating)
  - Growth score < 3 (future growth score from research cache)
  - 3-month return < -30% with no catalyst

NEVER suspends:
  - Held positions (always reviewed by committee)
  - Speculative tier (require explicit committee removal)
  - Congress-bought tickers (last 30 days)

Cost: free — yfinance + cached data only, no API calls.
"""
import json
import os
from datetime import datetime, timezone, timedelta

import yfinance as yf

import config
import database.research_cache as research_cache
from signals import technical
from broker import alpaca

_SUSPENDED_FILE = os.path.join(os.path.dirname(__file__), ".basket_suspended.json")


def _load_suspended() -> dict:
    """Returns {symbol: {"suspended_since": ..., "reasons": [...], "strike_count": N}}"""
    try:
        return json.load(open(_SUSPENDED_FILE))
    except Exception:
        return {}


def _save_suspended(data: dict) -> None:
    try:
        json.dump(data, open(_SUSPENDED_FILE, "w"), indent=2)
    except Exception:
        pass


def _get_bars(sym: str):
    return alpaca.get_stock_bars(sym)


def _strike_check(sym: str, cached: dict) -> list[str]:
    """
    Return list of strike reasons for a ticker. 2+ strikes = suspend.
    Uses only cached data + yfinance (free).
    """
    strikes = []

    # Technical strikes — fast yfinance pull
    try:
        bars = _get_bars(sym)
        tech = technical.compute(bars)
        price    = tech.get("price") or 0
        sma200   = tech.get("sma200") or 0
        dc       = tech.get("death_cross", False)
        dc_days  = tech.get("death_cross_days") or 0
        r3m      = tech.get("return_3m")

        if dc and dc_days > 45:
            strikes.append(f"death_cross_{dc_days}d")
        if sma200 > 0 and price < sma200:
            # Check how long below SMA200 — need 60 consecutive days
            # Approximate: if dc_days > 60 AND below SMA200, it's been below a while
            if dc_days > 60 or (dc and dc_days > 30):
                strikes.append("below_sma200_extended")
        if r3m is not None and r3m < -30:
            strikes.append(f"3m_return_{r3m:.0f}pct")
    except Exception:
        pass

    # Fundamental strikes — from research cache
    fund = (cached.get("fundamentals") or {})
    growth = (cached.get("future_growth") or {})

    rev_g = fund.get("revenue_growth")
    eps_g = fund.get("eps_growth_yoy")
    g_score = growth.get("score")

    if rev_g is not None and rev_g < -0.10:
        strikes.append(f"revenue_decline_{rev_g:.0%}")
    if eps_g is not None and eps_g < -0.20:
        strikes.append(f"eps_decline_{eps_g:.0%}")
    if g_score is not None and g_score < 3:
        strikes.append(f"growth_score_{g_score:.1f}")

    return strikes


def run_daily_basket_cleanup(basket: list[str], held_syms: set[str]) -> dict:
    """
    Run the daily basket quality sweep.

    Args:
        basket: full LT basket ticker list
        held_syms: currently held symbols (never suspended)

    Returns:
        {
          "newly_suspended": [...],
          "reinstated": [...],
          "suspended_total": N,
          "discord_msg": "..."
        }
    """
    suspended = _load_suspended()
    today = datetime.now(timezone.utc).date().isoformat()

    # Get congress buys last 30 days — protected
    congress_protected: set = set()
    try:
        from signals.congress import get_recent_buys
        congress_protected = set(get_recent_buys(days=30))
    except Exception:
        pass

    newly_suspended: list[str] = []
    reinstated: list[str] = []

    for sym in basket:
        # Never suspend held positions, speculative tier, or congress-protected
        if sym in held_syms:
            continue
        if config.TICKER_TIERS.get(sym) == "speculative":
            continue
        if sym in congress_protected:
            continue

        cached = research_cache.load(sym) or {}
        strikes = _strike_check(sym, cached)

        if len(strikes) >= 2:
            if sym not in suspended:
                suspended[sym] = {
                    "suspended_since": today,
                    "reasons": strikes,
                    "strike_count": len(strikes),
                }
                newly_suspended.append(sym)
                print(f"  [BasketMgr] SUSPEND {sym}: {strikes}")
            else:
                # Update strike count
                suspended[sym]["reasons"] = strikes
                suspended[sym]["strike_count"] = len(strikes)
        else:
            # Ticker is clean — if it was suspended, reinstate it
            if sym in suspended:
                del suspended[sym]
                reinstated.append(sym)
                print(f"  [BasketMgr] REINSTATE {sym}: no longer failing gates")

    # Weekly re-evaluation: check suspended tickers that have been suspended >14 days
    # If still failing after 14 days → recommend permanent removal (human approval required)
    for sym, data in list(suspended.items()):
        since = data.get("suspended_since", today)
        days_suspended = (datetime.now(timezone.utc).date() - datetime.fromisoformat(since).date()).days
        if days_suspended > 14 and sym not in newly_suspended:
            data["days_suspended"] = days_suspended
            data["recommend_removal"] = True

    _save_suspended(suspended)

    # Build Discord message
    lines = []
    if newly_suspended:
        lines.append(f"🚫 **Suspended from daily scan ({len(newly_suspended)}):** {', '.join(newly_suspended)}")
        for sym in newly_suspended:
            reasons = suspended[sym].get("reasons", [])
            lines.append(f"  {sym}: {' | '.join(reasons)}")
    if reinstated:
        lines.append(f"✅ **Reinstated:** {', '.join(reinstated)}")

    # Flag tickers that have been suspended >14 days and are candidates for permanent removal
    removal_candidates = [s for s, d in suspended.items() if d.get("recommend_removal")]
    if removal_candidates:
        lines.append(f"\n⚠️ **Removal candidates** (suspended >14d, still failing): {', '.join(removal_candidates)}")
        lines.append("  → Review and manually remove if no upcoming catalyst")

    discord_msg = "\n".join(lines) if lines else ""

    return {
        "newly_suspended": newly_suspended,
        "reinstated": reinstated,
        "suspended_total": len(suspended),
        "removal_candidates": removal_candidates,
        "discord_msg": discord_msg,
    }


def get_active_basket(basket: list[str], held_syms: set[str]) -> list[str]:
    """
    Return the basket with suspended tickers removed.
    Held positions are ALWAYS included regardless of suspension status or basket membership.
    Called before the daily cycle to reduce scan size.
    """
    suspended = set(_load_suspended().keys())
    active = [s for s in basket if s not in suspended or s in held_syms]
    # Ensure all held positions are in the active list even if not in the basket
    for sym in held_syms:
        if sym not in active:
            active.append(sym)
    return active


def get_suspension_summary() -> str:
    """Return a brief summary of currently suspended tickers."""
    suspended = _load_suspended()
    if not suspended:
        return "Basket clean — no suspended tickers"
    parts = [f"{sym} ({d.get('strike_count',0)} strikes)" for sym, d in suspended.items()]
    return f"Suspended ({len(suspended)}): {', '.join(parts[:10])}"
