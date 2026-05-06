"""
Entry point.
  python main.py              # daily cheap scan (uses research cache)
  python main.py --dry-run    # simulate without placing trades
  python main.py --monthly    # run full deep research, update cache, refresh basket
  python main.py --schedule   # start APScheduler
  python main.py --discord    # start Discord bot + scheduler
"""
import sys
import argparse
import threading
from datetime import datetime, timezone

import config
import database.db as db
import database.research_cache as research_cache
import database.learning as learning_db
from broker import alpaca
from signals import technical, sentiment, congress, insider, fundamentals, research, financial_data, social, market_context, future_growth, momentum_news
from signals import gap_scanner
from signals import options_flow as uw_flow
from signals import uw_scanner
from agent import claude_agent
from agent import options_advisor
from database import options_positions as op_db
from risk import manager
from summaries import reporter
from summaries import weekly_review
from basket import manager as basket_mgr
from basket import curation as basket_curation
from notifications import discord_bot as discord

# Load S&P 500 list once at startup for options eligibility check
_SP500 = config.get_sp500_tickers()

# UW sector key → our internal sector_weights keys
_UW_SECTOR_TO_INTERNAL: dict[str, list[str]] = {
    "semis":    ["semis"],
    "ai_tech":  ["ai_software", "ai_infra", "mega_tech"],
    "cyber":    ["cyber"],
    "defense":  ["defense"],
    "biotech":  ["biotech"],
    "energy":   ["energy_oil"],
    "fintech":  ["fintech"],
    "robotics": ["robotics"],
    "nuclear":  ["nuclear"],
    "quantum":  ["quantum"],
    "space":    ["space"],
}


def _apply_uw_sector_overlay(macro: dict, sector_flows: dict) -> dict:
    """
    Adjust macro sector weights using UW real-time institutional options flow.

    UW flow on sector ETFs (SOXX, ITA, XLE, CIBR, etc.) leads FRED data by
    1-3 days. When UW shows bullish flow in a sector the macro already favours,
    that's a confirmation. When UW diverges from the macro label, treat it as
    an early warning and soften the weight accordingly.

    Multipliers (capped / floored to keep weights in [0.10, 0.95]):
      bullish UW flow  →  × 1.15  (institutional positioning ahead of news)
      bearish UW flow  →  × 0.82  (smart money exiting — soften, don't eliminate)
      neutral / no_data→  no change
    """
    import copy as _copy
    updated = _copy.deepcopy(macro)
    weights = updated.get("sector_weights", {})
    if not weights:
        return macro

    adjustments: list[str] = []
    for uw_key, flow in sector_flows.items():
        mult = 1.15 if flow == "bullish" else (0.82 if flow == "bearish" else None)
        if mult is None:
            continue
        for internal in _UW_SECTOR_TO_INTERNAL.get(uw_key, []):
            if internal not in weights:
                continue
            old = weights[internal]
            new = round(min(0.95, max(0.10, old * mult)), 3)
            if abs(new - old) >= 0.02:          # only report meaningful changes
                adjustments.append(f"{internal} {old:.2f}→{new:.2f}({flow[:4]})")
            weights[internal] = new

    updated["sector_weights"] = weights
    if adjustments:
        print(f"  [UW Sector Overlay] {' | '.join(adjustments)}")
    return updated


def _portfolio_context(portfolio, positions):
    equity = portfolio["equity"]
    options_value = sum(
        abs(p["qty"] * p["current_price"])
        for p in positions if p.get("asset_class") == "us_option"
    )
    # Sector concentration
    sector_pcts: dict = {}
    for p in positions:
        sector = config.SECTOR_MAP.get(p["symbol"])
        if sector and equity:
            val = abs(p["qty"] * p["current_price"])
            sector_pcts[sector] = sector_pcts.get(sector, 0) + val / equity * 100

    # Speculative tier tracking
    spec_positions = [p for p in positions
                      if config.TICKER_TIERS.get(p["symbol"]) == "speculative"]
    spec_val = sum(abs(p["qty"] * p["current_price"]) for p in spec_positions)

    # Holdings list — so Claude knows exactly what's held and at what size
    holdings = []
    for p in positions:
        val = abs(p["qty"] * p["current_price"])
        pct = val / equity * 100 if equity else 0
        upl = p.get("unrealized_pl") or 0
        uplpct = p.get("unrealized_plpc", 0)
        holdings.append({
            "symbol":   p["symbol"],
            "pct":      round(pct, 1),
            "pl_pct":   round(uplpct, 1),
            "pl_usd":   round(upl, 0),
            "tier":     config.TICKER_TIERS.get(p["symbol"], "unknown"),
        })
    holdings.sort(key=lambda x: x["pct"], reverse=True)

    # Portfolio beta — weighted average from cached future_growth beta values
    total_beta_weighted = 0.0
    total_weight = 0.0
    for p in positions:
        sym = p["symbol"]
        val = abs(p["qty"] * p["current_price"])
        weight = val / equity if equity > 0 else 0
        cached = research_cache.load(sym) or {}
        beta = (cached.get("future_growth") or {}).get("beta")
        if beta and isinstance(beta, (int, float)):
            total_beta_weighted += beta * weight
            total_weight += weight
    portfolio_beta = round(total_beta_weighted / total_weight, 2) if total_weight > 0 else None

    # Portfolio drawdown from recent peak (last 60 snapshots, ~3 months of daily cycles)
    peak_equity = equity
    try:
        snaps = db.get_snapshots(limit=60)
        if snaps:
            peak_equity = max((s["equity"] for s in snaps if s.get("equity")), default=equity)
    except Exception:
        pass
    portfolio_drawdown_pct = round((equity - peak_equity) / peak_equity * 100, 2) if peak_equity > 0 else 0.0

    # 70/30 bucket allocation snapshot
    bucket_snapshot = db.portfolio_allocation_snapshot(positions, equity)

    return {
        **portfolio,
        "position_count":    len(positions),
        "options_pct":       (options_value / equity * 100) if equity else 0,
        "sector_pcts":       sector_pcts,
        "speculative_count": len(spec_positions),
        "speculative_pct":   (spec_val / equity * 100) if equity else 0,
        "holdings":          holdings,
        "portfolio_beta":    portfolio_beta,
        **bucket_snapshot,           # long_term_pct, medium_term_pct, long_term_count, medium_term_count
        "portfolio_drawdown_pct": portfolio_drawdown_pct,
    }


def _get_bars(symbol: str):
    return alpaca.get_stock_bars(symbol)


def is_market_hours() -> bool:
    """Return True if current ET time is within regular market hours (Mon-Fri 9:30–16:00)."""
    import zoneinfo
    now_et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    open_time  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_time = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_time <= now_et < close_time


def run_gap_scan():
    """
    08:45 ET — Pre-open gap and catalyst scanner. No Claude calls.
    Detects: overnight gaps >4%, earnings today/tomorrow, speculative milestones due,
    positions oversized into earnings events.
    Stores alerts so the 09:50 cycle can consume them.
    """
    print(f"\n{'='*60}")
    print(f"GAP SCAN started at {datetime.now(timezone.utc).isoformat()}")
    print('='*60)
    db.init()
    positions    = alpaca.get_positions()
    stock_basket = basket_mgr.load_combined()   # scan both LT and MT
    alerts       = gap_scanner.scan(positions, stock_basket, config)
    print(alerts["summary"])

    # Build an actionable Discord alert (only if there's something to act on)
    has_alerts = any([
        alerts["gap_alerts"], alerts["earnings_today"], alerts["earnings_cap_flags"],
        alerts.get("uw_bearish_alerts"), alerts.get("uw_discovery"),
    ])
    if has_alerts:
        _pos_map   = {p["symbol"]: p for p in positions}
        _cache     = {}
        try:
            import json as _j
            _cp = os.path.join(os.path.dirname(__file__), "..", "research_cache.json") if False else \
                  os.path.join(os.path.dirname(__file__), "research_cache.json")
            _cache = _j.load(open(_cp))
        except Exception:
            pass

        _alert_lines = [f"**⚡ PRE-OPEN ALERTS — {datetime.now(timezone.utc).strftime('%a %b %d')}**"]

        # Gap alerts with entry price + stop context
        if alerts["gap_alerts"]:
            _alert_lines.append("\n**📊 Overnight Gaps (held positions):**")
            for a in alerts["gap_alerts"]:
                sym  = a["symbol"]
                gap  = a["gap_pct"]
                pos  = _pos_map.get(sym, {})
                cur  = float(pos.get("current_price", 0) or 0)
                entry = float(pos.get("avg_entry", 0) or 0)
                upl  = float(pos.get("unrealized_plpc", 0) or 0)
                direction = "⬆️ GAP UP" if gap > 0 else "⬇️ GAP DOWN"
                action = ("Consider trimming into strength" if gap > 6
                          else "Monitor closely — may see follow-through" if gap > 0
                          else ("🚨 Review stop — gap threatens entry" if entry and cur < entry * 1.05
                                else "Watch for bounce vs breakdown"))
                _alert_lines.append(
                    f"  {direction} **{sym}** {gap:+.1f}%  |  entry ${entry:.2f}  P&L {upl:+.1f}%"
                )
                _alert_lines.append(f"    → {action}")

        # Earnings today/tomorrow — what to do
        if alerts["earnings_today"]:
            _alert_lines.append("\n**📅 Earnings This Week:**")
            for e in alerts["earnings_today"]:
                sym  = e["symbol"]
                tier = e["tier"]
                is_held = sym in _pos_map
                tag = "HELD" if is_held else "BASKET"
                note = ("Size check required — see cap flags below" if is_held
                        else "Watch for reaction — potential entry after settle")
                _alert_lines.append(f"  [{tag}] **{sym}** ({tier})  → {note}")

        # Cap flags
        if alerts["earnings_cap_flags"]:
            _alert_lines.append("\n**🚨 Oversized Into Earnings (trim before open):**")
            for f in alerts["earnings_cap_flags"]:
                _alert_lines.append(
                    f"  **{f['symbol']}** {f['current_pct']:.1f}% NAV → cap is {f['cap_pct']}% "
                    f"({f['tier']}) — TRIM to {f['cap_pct']}% before earnings"
                )

        # UW bearish sweeps
        if alerts.get("uw_bearish_alerts"):
            _alert_lines.append("\n**🐻 Unusual Whales Bearish Sweeps (held):**")
            for a in alerts["uw_bearish_alerts"]:
                _alert_lines.append(
                    f"  **{a['symbol']}** prem_pct={a['norm_pct']}  C/P={a['cp_ratio']}"
                    f"  → Elevated put activity — review thesis"
                )

        # UW discovery
        if alerts.get("uw_discovery"):
            top = sorted(alerts["uw_discovery"], key=lambda x: x["premium"], reverse=True)[:4]
            _alert_lines.append("\n**🔭 Large Bullish Sweeps (not in basket):**")
            for d in top:
                exp = f"  exp {d['expiry_weeks']}wk" if d.get("expiry_weeks") else ""
                _alert_lines.append(f"  **{d['symbol']}** ${d['premium']/1e6:.1f}M{exp}")

        # Catalyst window
        if alerts.get("catalyst_alerts"):
            _alert_lines.append("\n**🎯 Upcoming Catalysts (30d window):**")
            for c in alerts["catalyst_alerts"][:4]:
                _alert_lines.append(f"  **{c['ticker']}** — {c['milestone']} due {c['due']}")

        discord.send("\n".join(_alert_lines))
    # Store for 09:50 cycle pickup
    import json as _json, os as _os
    _alerts_path = _os.path.join(_os.path.dirname(__file__), ".gap_alerts.json")
    with open(_alerts_path, "w") as f:
        _json.dump(alerts, f)


def run_midday_check():
    """
    12:30 ET — Midday risk check. ATR-based stop check on open positions only.
    No new entries. No Claude calls.
    """
    print(f"\n[MIDDAY CHECK] {datetime.now(timezone.utc).isoformat()}")
    if not is_market_hours():
        print("[MIDDAY CHECK] Market closed — skipping")
        return
    db.init()
    positions    = alpaca.get_positions()
    held_tech_map = {}
    for p in positions:
        sym = p["symbol"]
        try:
            held_tech_map[sym] = {"technical": technical.compute(_get_bars(sym))}
        except Exception:
            pass
    exits = manager.check_stops(positions, signals_map=held_tech_map)
    pos_lookup = {p["symbol"]: p for p in positions}
    triggered  = []
    for exit_order in exits:
        sym    = exit_order["symbol"]
        reason = exit_order["reason"]
        action = exit_order.get("action", "SELL")
        print(f"  [MIDDAY EXIT] {sym} — {action} | {reason}")
        if action == "REVIEW":
            discord.send(f"🔍 MIDDAY REVIEW **{sym}** | {reason}")
            continue
        if action == "STOP_REVIEW":
            discord.send(
                f"⚠️ **STOP-LOSS TRIGGERED — AWAITING COMMITTEE REVIEW**\n"
                f"**{sym}** hit its stop-loss at midday. Position held — daily committee will decide.\n"
                f"📌 {reason}\n"
                f"Emergency auto-sell fires if loss reaches 2× the tier stop."
            )
            continue
        try:
            pos_raw  = pos_lookup.get(sym)
            qty      = abs(float(pos_raw["qty"])) if pos_raw else 0
            price    = float(pos_raw.get("current_price", 0)) if pos_raw else 0
            if action == "TRIM" and qty > 0:
                trim_qty = round(qty * config.TRIM_SIZE_PCT, 6)
                if trim_qty > 0:
                    alpaca.place_market_order(sym, trim_qty, "SELL")
                    db.log_trade(sym, "SELL", "stock", trim_qty, price, 0, 0, f"[MidTrim] {reason}")
                    discord.send(f"✂️ MIDDAY TRIM {sym} | {reason}")
                    triggered.append(sym)
            elif action == "SELL" and qty > 0:
                alpaca.place_market_order(sym, qty, "SELL")
                db.log_trade(sym, "SELL", "auto", qty, price, 0, 10, f"[MidStop] {reason}")
                discord.send(f"🔴 MIDDAY STOP {sym} | {reason}")
                triggered.append(sym)
        except Exception as e:
            print(f"    [MIDDAY] order error {sym}: {e}")
    if not triggered:
        print("[MIDDAY CHECK] No stops triggered — positions healthy")
        try:
            n = len(positions)
            syms = ", ".join(p["symbol"] for p in positions) if positions else "none"
            discord.send(f"✅ **MIDDAY CHECK** — {n} position{'s' if n != 1 else ''} healthy, no stops triggered\n📋 {syms}")
        except Exception:
            pass

    # Re-underwriting check: flag long-term large_growth positions at the 90-day gate
    _run_reunderwriting_alerts(positions, held_tech_map)


def _run_reunderwriting_alerts(positions: list, held_tech_map: dict):
    """
    Check for long-term positions hitting the 90-day re-underwriting trigger.
    Sends REVIEW alerts so the PM can explicitly re-confirm or exit within 10 trading days.
    """
    days_held_map = {}
    for p in positions:
        sym = p["symbol"]
        last_buy = db.get_last_buy_date(sym)
        if last_buy:
            try:
                from datetime import date as _date
                days = (_date.today() - datetime.fromisoformat(last_buy).date()).days
                days_held_map[sym] = days
            except Exception:
                pass

    alerts = manager.check_reunderwriting_alerts(days_held_map)
    for alert in alerts:
        sym    = alert["symbol"]
        reason = alert["reason"]
        print(f"  [REUNDERWRITE] {sym} — {reason}")
        discord.send(f"🔄 RE-UNDERWRITE **{sym}** | {reason}")
        db.update_reunderwriting_date(sym)  # prevent re-alert for 80 days


def run_spec_research():
    """
    Bi-weekly Wednesday 18:00 ET — full paid-API research for speculative tier only.
    Refreshes: web snippets, social, future growth score, earnings momentum.
    Keeps speculative cache fresh between monthly runs (half-life of social ~48h).
    """
    import os as _os
    _os.environ["KIMMY_MONTHLY"] = "1"  # allow paid API calls

    print(f"\n{'='*60}")
    print(f"SPEC RESEARCH started at {datetime.now(timezone.utc).isoformat()}")
    print('='*60)

    db.init()
    spec_tickers = [s for s, t in config.TICKER_TIERS.items() if t == "speculative"]
    print(f"  Refreshing {len(spec_tickers)} speculative tickers: {spec_tickers}")
    discord.send(f"Bi-weekly spec refresh starting — {spec_tickers}")

    done = 0
    for symbol in spec_tickers:
        print(f"\n  [{symbol}] full speculative research...")
        try:
            bars          = _get_bars(symbol)
            tech          = technical.compute(bars)
            fund          = fundamentals.compute(symbol)
            sent          = sentiment.compute(symbol)
            cong          = congress.compute(symbol)
            insd          = insider.compute(symbol)
            earnings_data = market_context.earnings_soon(symbol)
            research_data = research.compute(symbol)
            fin_data      = financial_data.compute(symbol)
            social_data   = social.compute(symbol)
            growth_data   = future_growth.compute(symbol)
            earn_mom      = momentum_news.earnings_momentum(symbol)

            uw_short = uw_iv = uw_oi = uw_dp = uw_earn = {}
            if config.UNUSUAL_WHALES_API_KEY:
                uw_short = uw_flow.get_short_interest(symbol)
                uw_iv    = uw_flow.get_iv_data(symbol)
                uw_oi    = uw_flow.get_oi_changes(symbol)
                uw_dp    = uw_flow.get_darkpool(symbol)
                uw_earn  = uw_flow.get_earnings_beat_rate(symbol)

            research_cache.save(symbol, {
                "fundamentals":          fund,
                "sentiment":             sent,
                "congressional":         cong,
                "insider":               insd,
                "future_growth":         growth_data,
                "financial_data":        fin_data,
                "social":                social_data,
                "earnings_data":         earnings_data,
                "earnings_momentum":     earn_mom,
                "research_snippets":     (research_data or {}).get("snippets", []),
                "research_source_count": (research_data or {}).get("source_count", 0),
                "spec_refresh_ts":       datetime.now(timezone.utc).isoformat(),
                "uw_short_interest":     uw_short,
                "uw_iv":                 uw_iv,
                "uw_oi_changes":         uw_oi,
                "uw_darkpool":           uw_dp,
                "uw_earnings_beat":      uw_earn,
            })
            g_score = (growth_data or {}).get("score", "?")
            beat_str = f" | beat={uw_earn.get('beat_rate')}" if uw_earn.get('beat_rate') is not None else ""
            print(f"  [{symbol}] growth={g_score} | social={social_data.get('combined_label')} | "
                  f"short={uw_short.get('short_interest_pct')}% IV_rank={uw_iv.get('iv_rank')}{beat_str} ✓")
            done += 1
        except Exception as e:
            print(f"  [{symbol}] ERROR: {e}")

    _os.environ.pop("KIMMY_MONTHLY", None)
    msg = f"Bi-weekly spec refresh complete — {done}/{len(spec_tickers)} refreshed"
    print(msg)
    discord.send(msg)


def run_monthly_research():
    """
    Full deep research cycle — run once per month (1st Monday).
    Sets KIMMY_MONTHLY=1 so paid research.compute() calls are allowed.

    Research depth:
      LIGHT+WEB  (mega + large_growth): yfinance + FMP financials + web snippets
                 (added web: NVDA/PLTR news context is too important to skip)
      FULL       (mid_growth + speculative): all paid APIs
    """
    import os as _os
    _os.environ["KIMMY_MONTHLY"] = "1"

    print(f"\n{'='*60}")
    print(f"MONTHLY RESEARCH started at {datetime.now(timezone.utc).isoformat()}")
    print('='*60)

    db.init()
    stock_basket = basket_mgr.load_combined()   # LT + MT for research coverage
    stocks = stock_basket

    light_tiers = {"mega", "large_growth"}
    light = [s for s in stocks if config.TICKER_TIERS.get(s, "mid_growth") in light_tiers]
    full  = [s for s in stocks if config.TICKER_TIERS.get(s, "mid_growth") not in light_tiers]

    print(f"  Research plan: {len(light)} light+web (mega/large) + {len(full)} full (mid/spec)")
    discord.send(f"Monthly research starting — {len(light)} light+web + {len(full)} full")

    done = 0
    for symbol in stocks:
        tier      = config.TICKER_TIERS.get(symbol, "mid_growth")
        is_light  = tier in light_tiers
        depth_tag = "light+web" if is_light else "full"
        print(f"\n  [{symbol}] {depth_tag} research ({tier})...")
        try:
            bars  = _get_bars(symbol)
            tech  = technical.compute(bars)
            fund  = fundamentals.compute(symbol)
            sent  = sentiment.compute(symbol)
            cong  = congress.compute(symbol)
            insd  = insider.compute(symbol)
            earnings_data = market_context.earnings_soon(symbol)

            if is_light:
                # Mega/large: financials + web snippets (no social/growth scoring)
                fin_data      = financial_data.compute(symbol)
                research_data = research.compute(symbol)   # web snippets now included
                social_data   = {}
                growth_data   = {}
                earn_mom      = {}
            else:
                # Mid/speculative: full picture
                research_data = research.compute(symbol)
                fin_data      = financial_data.compute(symbol)
                social_data   = social.compute(symbol)
                growth_data   = future_growth.compute(symbol)
                earn_mom      = momentum_news.earnings_momentum(symbol)

            # UW structural data — slow-moving signals worth caching for all tiers
            uw_short = uw_iv = uw_oi = uw_dp = uw_earn = {}
            if config.UNUSUAL_WHALES_API_KEY:
                uw_short = uw_flow.get_short_interest(symbol)
                uw_iv    = uw_flow.get_iv_data(symbol)
                uw_oi    = uw_flow.get_oi_changes(symbol)
                uw_dp    = uw_flow.get_darkpool(symbol)
                uw_earn  = uw_flow.get_earnings_beat_rate(symbol)

            g_score = growth_data.get("score", "cached") if growth_data else "-"
            beat_str = f" | beat={uw_earn.get('beat_rate')}" if uw_earn.get('beat_rate') is not None else ""
            print(f"  [{symbol}] growth={g_score} | sent={sent.get('label')} | depth={depth_tag} | "
                  f"short={uw_short.get('short_interest_pct')}% IV_rank={uw_iv.get('iv_rank')}{beat_str}")

            research_cache.save(symbol, {
                "fundamentals":          fund,
                "sentiment":             sent,
                "congressional":         cong,
                "insider":               insd,
                "future_growth":         growth_data,
                "financial_data":        fin_data,
                "social":                social_data,
                "earnings_data":         earnings_data,
                "earnings_momentum":     earn_mom,
                "research_snippets":     (research_data or {}).get("snippets", []),
                "research_source_count": (research_data or {}).get("source_count", 0),
                "uw_short_interest":     uw_short,
                "uw_iv":                 uw_iv,
                "uw_oi_changes":         uw_oi,
                "uw_darkpool":           uw_dp,
                "uw_earnings_beat":      uw_earn,
            })
            done += 1
        except Exception as e:
            print(f"  [{symbol}] ERROR: {e}")

    # --- Monthly basket curation: scan market, add new stocks, remove weak ones ---
    print("\n  Running monthly basket curation...")
    from basket import curation
    cached_all = research_cache.load_all()
    to_add, to_remove, curation_reasoning = curation.run(stocks, cached_all, config)

    # Apply removes from config tiers (basket.json gets rebuilt below)
    for sym in to_remove:
        config.TICKER_TIERS.pop(sym, None)
        config.SECTOR_MAP.pop(sym, None)

    # Apply adds to config tiers (default to mid_growth if unknown)
    for sym in to_add:
        if sym not in config.TICKER_TIERS:
            config.TICKER_TIERS[sym] = "mid_growth"

    # Refresh basket (congress buys merged inside refresh)
    print("\n  Refreshing basket with congress buys + curation changes...")
    basket_mgr.refresh()

    changes = []
    if to_add:    changes.append(f"Added: {', '.join(to_add)}")
    if to_remove: changes.append(f"Removed: {', '.join(to_remove)}")
    changes_str = " | ".join(changes) if changes else "No changes to basket"

    _os.environ.pop("KIMMY_MONTHLY", None)  # re-enable cost guard for daily cycles

    msg = (f"Monthly research complete — {done}/{len(stocks)} tickers cached\n"
           f"Basket curation: {changes_str}\n"
           f"{curation_reasoning[:300]}")
    print(f"\n{msg}")
    discord.send(msg)

    # ── Monthly deep-dive: full committee analysis using the freshly-cached data ──
    try:
        from reports.monthly_deep_dive import run as _deep_dive
        from signals.macro_regime import compute as _macro_compute
        _portfolio  = alpaca.get_portfolio()
        _positions  = alpaca.get_positions()
        _macro      = _macro_compute()
        _deep_dive(positions=_positions, portfolio=_portfolio, macro_regime=_macro)
    except Exception as _e:
        print(f"  [Monthly deep-dive] ERROR: {_e}")
        discord.send(f"❌ Monthly deep-dive failed: {_e}")


def run_new_ticker_onboarding(new_tickers: list[str]) -> int:
    """
    Full monthly-depth research for brand-new basket tickers (no cache entry at all).
    Runs automatically at the start of each daily scan — no manual --monthly needed.
    Same depth as run_monthly_research: web snippets + social + financials + UW.
    Sets KIMMY_MONTHLY=1 so paid research APIs are allowed.
    Returns count of tickers successfully onboarded.
    """
    import os as _os
    stocks = [s for s in new_tickers if not research_cache.load(s)]
    if not stocks:
        return 0

    _os.environ["KIMMY_MONTHLY"] = "1"
    print(f"\n  [Onboarding] {len(stocks)} new basket ticker(s) — running full research: {stocks}")
    try:
        discord.send(f"🆕 New ticker onboarding: {', '.join(stocks)} — running full research")
    except Exception:
        pass

    done = 0
    light_tiers = {"mega", "large_growth"}
    for symbol in stocks:
        tier     = config.TICKER_TIERS.get(symbol, "mid_growth")
        is_light = tier in light_tiers
        depth    = "light+web" if is_light else "full"
        print(f"\n  [{symbol}] onboarding ({depth}, {tier})...")
        try:
            fund          = fundamentals.compute(symbol)
            sent          = sentiment.compute(symbol)
            cong          = congress.compute(symbol)
            insd          = insider.compute(symbol)
            earnings_data = market_context.earnings_soon(symbol)
            fin_data      = financial_data.compute(symbol)
            research_data = research.compute(symbol)          # web snippets always

            if is_light:
                social_data = {}
                growth_data = {}
                earn_mom    = {}
            else:
                social_data = social.compute(symbol)
                growth_data = future_growth.compute(symbol)
                earn_mom    = momentum_news.earnings_momentum(symbol)

            uw_short = uw_iv = uw_oi = uw_dp = {}
            if config.UNUSUAL_WHALES_API_KEY:
                uw_short = uw_flow.get_short_interest(symbol)
                uw_iv    = uw_flow.get_iv_data(symbol)
                uw_oi    = uw_flow.get_oi_changes(symbol)
                uw_dp    = uw_flow.get_darkpool(symbol)

            research_cache.save(symbol, {
                "fundamentals":          fund,
                "sentiment":             sent,
                "congressional":         cong,
                "insider":               insd,
                "future_growth":         growth_data,
                "financial_data":        fin_data,
                "social":                social_data,
                "earnings_data":         earnings_data,
                "earnings_momentum":     earn_mom,
                "research_snippets":     (research_data or {}).get("snippets", []),
                "research_source_count": (research_data or {}).get("source_count", 0),
                "uw_short_interest":     uw_short,
                "uw_iv":                 uw_iv,
                "uw_oi_changes":         uw_oi,
                "uw_darkpool":           uw_dp,
            })
            g_score = (growth_data or {}).get("score", "n/a")
            snippets = (research_data or {}).get("source_count", 0)
            print(f"  [{symbol}] onboarded: growth={g_score} sent={sent.get('label','?')} "
                  f"snippets={snippets} short={uw_short.get('short_interest_pct')}%")
            done += 1
        except Exception as e:
            print(f"  [{symbol}] onboarding error: {e}")

    _os.environ.pop("KIMMY_MONTHLY", None)
    print(f"\n  [Onboarding] Complete — {done}/{len(stocks)} new tickers fully researched")
    try:
        discord.send(f"✅ Onboarding complete — {done}/{len(stocks)} new tickers ready")
    except Exception:
        pass
    return done


def run_mt_cache_warmup(
    tickers: list[str],
    max_age_days: int | None = None,
    max_symbols: int | None = None,
    silent: bool = False,
) -> tuple[int, int]:
    """
    Seed / refresh research cache for tickers that are missing or stale.

    Called after basket updates (max_age_days=None → only uncached) and at the
    start of each daily scan (max_age_days=CACHE_STALE_DAYS, max_symbols=AUTO_WARMUP_MAX).

    Uses cheap/free signals only — no web search (Tavily/Exa/Serper).
    Web snippets are populated on the next monthly run.

    Returns (warmed_count, still_uncached_count).
    """
    stocks = list(tickers)

    # Build candidate list: uncached first, then stale (oldest first)
    uncached = [s for s in stocks if not research_cache.load(s)]
    if max_age_days is not None:
        stale = [
            s for s in stocks
            if s not in uncached
            and (research_cache.cache_age_days(s) or 0) >= max_age_days
        ]
        # Sort stale by age descending so oldest get refreshed first
        stale.sort(key=lambda s: research_cache.cache_age_days(s) or 0, reverse=True)
    else:
        stale = []

    to_warm = uncached + stale
    if not to_warm:
        if not silent:
            print("  [Cache Warmup] All tickers fresh — nothing to warm")
        return 0, 0

    if max_symbols is not None:
        to_warm = to_warm[:max_symbols]

    still_uncached = len([s for s in uncached if s not in to_warm])

    if not silent:
        print(f"\n  [Cache Warmup] Warming {len(to_warm)} symbol(s) "
              f"({len(uncached)} uncached, {len(stale)} stale≥{max_age_days}d): {to_warm}")
    try:
        if not silent and (uncached or stale):
            discord.send(f"📚 Cache warmup: {len(to_warm)} symbol(s) — {', '.join(to_warm)}")
    except Exception:
        pass

    done = 0
    for symbol in to_warm:
        age = research_cache.cache_age_days(symbol)
        tag = f"stale {age}d" if age is not None else "uncached"
        print(f"\n  [{symbol}] warmup ({tag}, light — no web search)...")
        try:
            bars          = _get_bars(symbol)
            tech          = technical.compute(bars)
            fund          = fundamentals.compute(symbol)
            sent          = sentiment.compute(symbol)
            cong          = congress.compute(symbol)
            insd          = insider.compute(symbol)
            earnings_data = market_context.earnings_soon(symbol)
            fin_data      = financial_data.compute(symbol)
            growth_data   = future_growth.compute(symbol)
            earn_mom      = momentum_news.earnings_momentum(symbol)

            uw_short = uw_iv = uw_oi = uw_dp = {}
            if config.UNUSUAL_WHALES_API_KEY:
                uw_short = uw_flow.get_short_interest(symbol)
                uw_iv    = uw_flow.get_iv_data(symbol)
                uw_oi    = uw_flow.get_oi_changes(symbol)
                uw_dp    = uw_flow.get_darkpool(symbol)

            research_cache.save(symbol, {
                "fundamentals":          fund,
                "sentiment":             sent,
                "congressional":         cong,
                "insider":               insd,
                "future_growth":         growth_data,
                "financial_data":        fin_data,
                "social":                {},
                "earnings_data":         earnings_data,
                "earnings_momentum":     earn_mom,
                "research_snippets":     [],
                "research_source_count": 0,
                "uw_short_interest":     uw_short,
                "uw_iv":                 uw_iv,
                "uw_oi_changes":         uw_oi,
                "uw_darkpool":           uw_dp,
            })
            g_score = (growth_data or {}).get("score", "?")
            print(f"  [{symbol}] warmup done: growth={g_score} sent={sent.get('label','?')} "
                  f"short={uw_short.get('short_interest_pct')}% IV_rank={uw_iv.get('iv_rank')}")
            done += 1
        except Exception as e:
            print(f"  [{symbol}] warmup error: {e}")

    if not silent:
        msg = f"Cache warmup complete — {done}/{len(to_warm)} warmed"
        if still_uncached:
            msg += f" | {still_uncached} still uncached (will onboard on next cycle)"
        print(f"\n  [Cache Warmup] {msg}")
        try:
            discord.send(f"✅ {msg}")
        except Exception:
            pass

    return done, still_uncached


def run_weekly_basket_review():
    """
    Weekly basket maintenance — runs every Friday 16:30 ET (23:30 AST).
    LT basket: free signals + tier-aware curation (no paid APIs).
    MT basket: congress buys + earnings catalysts + sector rotation + UW discoveries.
    """
    print(f"\n{'='*60}")
    print(f"WEEKLY BASKET REVIEW started at {datetime.now(timezone.utc).isoformat()}")
    print('='*60)
    db.init()

    # ── LT basket review (unchanged process) ─────────────────────────────────
    existing_lt = basket_mgr.load()
    stocks_lt   = existing_lt
    cached_all  = research_cache.load_all()

    lt_add, lt_remove, lt_reasoning = basket_curation.run_weekly(stocks_lt, cached_all)

    lt_changes = []
    if lt_add or lt_remove:
        for sym in lt_remove:
            config.TICKER_TIERS.pop(sym, None)
            config.SECTOR_MAP.pop(sym, None)
        for sym in lt_add:
            if sym not in config.TICKER_TIERS:
                config.TICKER_TIERS[sym] = "mid_growth"
        basket_mgr.refresh()
        if lt_add:    lt_changes.append(f"LT Added: {', '.join(lt_add)}")
        if lt_remove: lt_changes.append(f"LT Removed: {', '.join(lt_remove)}")

    # ── MT basket review ─────────────────────────────────────────────────────
    mt_tickers, mt_meta, mt_reasoning = basket_curation.run_mt_weekly(stocks_lt)
    basket_mgr.save_mt(mt_tickers, mt_meta)

    # Full monthly-depth onboarding for any ticker with no cache (new additions).
    # Light warmup for stale-but-existing cache entries.
    combined_tickers = list(dict.fromkeys(stocks_lt + mt_tickers + (lt_add or [])))
    _no_cache = [s for s in combined_tickers if not research_cache.load(s)]
    _stale    = [s for s in combined_tickers if s not in _no_cache]
    if _no_cache:
        print(f"  [Weekly Review] {len(_no_cache)} ticker(s) with no cache — running full onboarding: {_no_cache}")
        run_new_ticker_onboarding(_no_cache)
    if _stale:
        run_mt_cache_warmup(_stale)

    mt_prev = set(basket_mgr.load_mt())  # post-save
    mt_changes = []
    src_counts = {}
    for sym, m in mt_meta.items():
        s = m.get("source", "unknown")
        src_counts[s] = src_counts.get(s, 0) + 1

    # ── Discord summary ───────────────────────────────────────────────────────
    parts = []
    if lt_changes:
        parts.append(" | ".join(lt_changes))
        parts.append(f"LT reasoning: {lt_reasoning[:200]}")
    else:
        parts.append("LT basket: no changes")

    src_str = " | ".join(f"{k}={v}" for k, v in src_counts.items())
    parts.append(f"MT basket: {len(mt_tickers)} tickers ({src_str})")
    if mt_tickers:
        parts.append(f"MT holdings: {', '.join(mt_tickers[:15])}")
    parts.append(f"MT reasoning: {mt_reasoning[:200]}")

    msg = "Weekly basket review:\n" + "\n".join(parts)
    print(msg)
    discord.send(msg)


def _mt_weekly_deployed_pct(portfolio_equity: float) -> float:
    """
    Return total allocation_pct of MT BUY decisions executed since last Monday.
    Uses decisions_log (has sleeve + allocation_pct); trades table does not.
    """
    try:
        from datetime import timedelta
        import zoneinfo
        from database import decisions_log as _dlog
        now_et    = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
        monday_et = now_et - timedelta(days=now_et.weekday())
        monday_et = monday_et.replace(hour=0, minute=0, second=0, microsecond=0)
        monday_utc_str = monday_et.astimezone(timezone.utc).isoformat()
        records   = _dlog.get_recent_decisions(days=7)
        mt_buys   = [r for r in records
                     if r.get("action") == "BUY"
                     and r.get("sleeve") == "medium_term"
                     and r.get("ts", "") >= monday_utc_str]
        return sum(r.get("allocation_pct", 0) for r in mt_buys)
    except Exception:
        return 0.0


def run_daily_basket_review():
    """
    Daily MT basket review — Mon-Fri 16:15 ET (23:15 AST).
    Evaluates each MT position for momentum, thesis validity, and TTL.
    Removes broken/expired slots and sources 1:1 replacements.
    """
    print(f"\n{'='*60}")
    print(f"DAILY BASKET REVIEW started at {datetime.now(timezone.utc).isoformat()}")
    print('='*60)
    db.init()

    lt_basket = basket_mgr.load()
    updated_tickers, updated_meta, discord_msg = basket_curation.run_daily_basket_review(lt_basket)

    if discord_msg:
        discord.send(discord_msg)


def run_uw_sweep_scan():
    """
    Poll UW sweep feed every 5 min during market hours.
    Basket/held hits → Discord alert.
    Out-of-basket call sweeps ≥$1M → Discord + MT basket queue.
    Call sweeps ≥$2M → also trigger immediate onboarding research.
    Cost: 1 API call per run (78/day).
    """
    if not uw_flow._is_market_hours():
        return
    try:
        combined   = basket_mgr.load_combined()
        basket     = basket_mgr.load()
        held       = [p["symbol"] for p in alpaca.get_positions()]
        alerts, discoveries = uw_scanner.run_sweep_feed_scan(basket, held)

        for msg in alerts:
            discord.send(msg)

        if not discoveries:
            return

        # Queue all discoveries to uw_pending (consumed at next run_cycle)
        basket_mgr.queue_uw_discovery(discoveries)

        # For very large call sweeps (≥$2M) add to MT basket and research NOW —
        # don't wait for the next cycle. Institutional sweeps at this size move fast.
        combined_set = set(combined)
        big = [d for d in discoveries
               if d.get("premium", 0) >= 2_000_000
               and d["symbol"] not in combined_set]

        if big:
            today      = datetime.now(timezone.utc).date().isoformat()
            big_syms   = list(dict.fromkeys(d["symbol"] for d in big))
            ex_mt      = basket_mgr.load_mt()
            ex_meta    = basket_mgr.load_mt_metadata()
            new_meta   = {
                s: {
                    "source":  "uw_sweep",
                    "added":   today,
                    "expires": None,
                    "note":    f"UW call sweep ≥$2M — immediate onboarding",
                }
                for s in big_syms
            }
            basket_mgr.save_mt(list(dict.fromkeys(ex_mt + big_syms)),
                               {**ex_meta, **new_meta})
            run_new_ticker_onboarding(big_syms)
            prem_str = " | ".join(
                f"{d['symbol']} ${d['premium']/1e6:.1f}M" for d in big
            )
            discord.send(f"🔭 HIGH-CONVICTION UW SWEEP → immediate research: {prem_str}")

    except Exception as e:
        print(f"  [UW sweep scan] error: {e}")


def run_uw_intraday_scan():
    """
    Refresh UW flow + all data signals for the full basket every 15 min.
    Market-hours TTLs in options_flow ensure each call hits the API fresh.
    Cost: ~190 API calls per run (4,900/day at target cadence).
    Also runs discovery scan once at 10:00 ET and sends hits to Discord.
    """
    if not uw_flow._is_market_hours():
        return
    try:
        import zoneinfo
        basket = basket_mgr.load()
        held   = [p["symbol"] for p in alpaca.get_positions()]

        # Refresh all basket + held tickers
        uw_scanner.run_basket_refresh(basket, held)

        # Once-daily discovery scan at 10:00 (±7 min window)
        now_et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
        if now_et.hour == 10 and now_et.minute < 8:
            hits  = uw_scanner.run_discovery_scan(basket)
            lines = uw_scanner.discovery_discord_lines(hits)
            if lines:
                discord.send("\n".join(lines))
            # Queue darkpool accumulation hits to uw_pending so the next
            # run_cycle picks them up via _run_daily_discovery().
            if hits:
                basket_mgr.queue_uw_discovery([
                    {
                        "symbol":  h["symbol"],
                        "premium": h.get("total_notional_3d", 0),
                        "source":  "uw_darkpool_scan",
                    }
                    for h in hits
                ])
    except Exception as e:
        print(f"  [UW intraday scan] error: {e}")


def run_basket_intelligence(mkt_ctx: dict, macro_regime: dict, uw_mkt_ctx: dict,
                            current_basket: list[str]) -> None:
    """
    Daily proactive basket scan — runs after the morning committee cycle.
    Uses a cheap Haiku call to analyse UW sector flows + macro regime and surface
    tickers to add/remove from the basket BEFORE the market prices in the rotation.
    Only escalates to full research when the model flags a real opportunity.

    Cost: ~$0.01-0.03 per call (Haiku) vs $0.30-0.50 for a full committee cycle.
    """
    try:
        import os as _os
        from signals.macro_regime import format_for_prompt as _fmt_macro
        _client = __import__("anthropic").Anthropic(api_key=config.ANTHROPIC_API_KEY)

        # Build the intelligence brief
        macro_txt = _fmt_macro(macro_regime) if macro_regime else "No macro data."

        sector_flows = uw_mkt_ctx.get("sector_flows") or {}
        sf_lines = []
        for sector, flow in sector_flows.items():
            if flow and flow != "no_data":
                sf_lines.append(f"  {sector}: {flow}")
        sf_txt = "\n".join(sf_lines) if sf_lines else "  No sector flow data."

        tide = uw_mkt_ctx.get("market_tide", "no_data")
        pc   = uw_mkt_ctx.get("market_put_call_ratio", "?")

        # Upcoming economic events
        events = macro_regime.get("upcoming_events", []) if macro_regime else []
        high_events = [e for e in events if e.get("impact") == "High"]
        ev_txt = "\n".join(f"  {e['date']}: {e['event']}" for e in high_events[:5]) or "  None in next 14 days."

        current_themes = "\n".join(f"  {s}" for s in sorted(current_basket)[:30])

        prompt = f"""You are a proactive equity research analyst. Your job is to identify stocks that should be added to our watchlist BEFORE the market prices in the opportunity — not after.

CURRENT MACRO REGIME:
{macro_txt}

UW MARKET-WIDE SIGNALS:
Market tide: {tide} | Put/Call ratio: {pc}
Sector institutional flows:
{sf_txt}

UPCOMING HIGH-IMPACT ECONOMIC EVENTS:
{ev_txt}

CURRENT BASKET (first 30):
{current_themes}

TASK: Based on the macro regime, sector rotation signals, and upcoming catalysts, identify:
1. UP TO 3 sectors or themes that institutional money is rotating INTO right now
2. UP TO 5 specific tickers (NOT already in the basket above) that should be added to capture this rotation BEFORE it's priced in
3. UP TO 2 basket tickers that should be REMOVED because the macro/rotation is working against them

RULES:
- Only suggest tickers with real institutional backing (large cap or proven growth)
- Focus on what's HAPPENING NOW in the data, not general thesis
- If no clear signal, output nothing — do not manufacture opportunities
- Be specific about WHY each ticker benefits from the current regime

OUTPUT FORMAT (JSON only, no prose):
{{
  "rotation_themes": ["theme1", "theme2"],
  "add": [{{"ticker": "X", "reason": "one sentence tied to current macro/UW data"}}],
  "remove": [{{"ticker": "X", "reason": "one sentence"}}],
  "confidence": "high|medium|low",
  "summary": "one sentence for Discord"
}}
If no clear signal: {{"rotation_themes": [], "add": [], "remove": [], "confidence": "low", "summary": "No clear rotation signal today."}}"""

        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
            raw = raw.strip()

        import json as _json
        result = _json.loads(raw)

        conf = result.get("confidence", "low")
        adds = result.get("add", [])
        removes = result.get("remove", [])
        summary = result.get("summary", "")
        themes = result.get("rotation_themes", [])

        print(f"\n  [BasketIntel] {summary}")
        if themes:
            print(f"  [BasketIntel] Rotation into: {', '.join(themes)}")

        if conf == "low" and not adds and not removes:
            return

        # ── Hard sector-alignment gate on Haiku's suggestions ─────────────────
        # Haiku already sees the macro sector routing in its prompt, but we enforce
        # it structurally here so a hallucinated suggestion in an avoid-sector never
        # reaches the basket regardless of what the model outputs.
        _sw = (macro_regime or {}).get("sector_weights", {})
        if _sw:
            _min_w = config.REGIME_MIN_SECTOR_WEIGHT
            _aligned: list[dict] = []
            for _item in adds:
                _sym = (_item.get("ticker") or "").upper()
                if not _sym:
                    continue
                # Known sector (fast path — no API call)
                _internal = config.SECTOR_MAP.get(_sym)
                if _internal:
                    _w = _sw.get(_internal, 0.60)
                    if _w < _min_w:
                        print(f"  [BasketIntel] REJECTED {_sym}: sector '{_internal}' "
                              f"weight {_w:.2f} < {_min_w} — not in winning sectors")
                        continue
                else:
                    # Unknown ticker — quick yfinance sector lookup
                    try:
                        import yfinance as _yf_bi
                        _yf_sec = (_yf_bi.Ticker(_sym).info or {}).get("sector", "")
                        from basket.curation import _YF_SECTOR_TO_INTERNAL
                        _candidates = _YF_SECTOR_TO_INTERNAL.get(_yf_sec, [])
                        _best_w = max((_sw.get(s, 0) for s in _candidates), default=0.60)
                        if _candidates and _best_w < _min_w:
                            print(f"  [BasketIntel] REJECTED {_sym}: yfinance sector "
                                  f"'{_yf_sec}' best weight {_best_w:.2f} < {_min_w}")
                            continue
                    except Exception:
                        pass   # lookup failed — allow through rather than block on error
                _aligned.append(_item)
            adds = _aligned

        # Execute adds — add to MT basket + run full onboarding
        new_syms = []
        for item in adds:
            sym = (item.get("ticker") or "").upper()
            reason = item.get("reason", "")
            if sym and sym not in set(current_basket):
                new_syms.append(sym)
                print(f"  [BasketIntel] ADD {sym}: {reason}")

        if new_syms:
            today = datetime.now(timezone.utc).date().isoformat()
            existing_mt   = basket_mgr.load_mt()
            existing_meta = basket_mgr.load_mt_metadata()
            new_meta      = {s: {"source": "uw_discovery", "added": today,
                                 "expires": None, "note": f"MacroIntel: {next((x['reason'] for x in adds if x.get('ticker','').upper()==s), '')[:80]}"}
                             for s in new_syms}
            basket_mgr.save_mt(list(dict.fromkeys(existing_mt + new_syms)), {**existing_meta, **new_meta})
            run_new_ticker_onboarding(new_syms)
            discord.send(f"📡 Basket Intelligence [{conf.upper()}]: Adding {', '.join(new_syms)}\n"
                    f"Rotation: {', '.join(themes)}\n{summary}")

        # Surface removes as committee agenda items (don't auto-remove — committee decides)
        for item in removes:
            sym = (item.get("ticker") or "").upper()
            reason = item.get("reason", "")
            if sym:
                print(f"  [BasketIntel] FLAG FOR REMOVAL {sym}: {reason}")
                discord.send(f"⚠️ Basket Intel: Consider removing {sym} | {reason}")

    except Exception as _e:
        print(f"  [BasketIntel] error: {_e}")


def _run_daily_discovery(current_basket: list[str]) -> list[str]:
    """
    Runs at the start of every trading cycle.
    Checks congress buys (last 7 days) + UW pending queue for tickers NOT in the
    combined basket. Adds them to the MT basket immediately and returns the new
    symbols so the onboarding block picks them up in the same cycle.
    """
    import os as _os
    basket_set = set(current_basket)
    new_syms: list[str] = []
    new_meta: dict = {}
    today = datetime.now(timezone.utc).date().isoformat()

    # 1. Congress buys — last 7 days, not already in basket
    try:
        from signals.congress import get_recent_buys
        recent = get_recent_buys(days=7)
        for sym in recent:
            if sym not in basket_set and sym not in new_syms:
                new_syms.append(sym)
                new_meta[sym] = {
                    "source":  "congress_buy",
                    "added":   today,
                    "expires": None,
                    "note":    "Recent congress buy (last 7 days)",
                }
                print(f"  [Daily Discovery] Congress buy: {sym} — adding to MT basket")
    except Exception as _e:
        print(f"  [Daily Discovery] Congress check error: {_e}")

    # 2. UW pending queue — out-of-basket sweeps queued by gap scanner
    try:
        pending = basket_mgr.get_uw_pending()
        for disc in pending:
            sym = (disc.get("symbol") or "").upper()
            if sym and sym not in basket_set and sym not in new_syms:
                new_syms.append(sym)
                prem = disc.get("premium", 0) or 0
                new_meta[sym] = {
                    "source":  "uw_discovery",
                    "added":   today,
                    "expires": None,
                    "note":    f"UW sweep ${prem/1e6:.1f}M",
                }
                print(f"  [Daily Discovery] UW discovery: {sym} — ${prem/1e6:.1f}M sweep, adding to MT basket")
    except Exception as _e:
        print(f"  [Daily Discovery] UW pending error: {_e}")

    # 3. UW market OI buildup — stocks with unusual call OI accumulation today
    try:
        oi_hits = uw_flow.get_market_oi_buildup(min_mcap=2_000_000_000, top_n=30)
        added_oi = 0
        for hit in oi_hits:
            sym = hit["symbol"]
            if sym not in basket_set and sym not in new_syms:
                new_syms.append(sym)
                new_meta[sym] = {
                    "source":  "uw_oi_buildup",
                    "added":   today,
                    "expires": None,
                    "note":    (f"OI buildup +{hit['oi_change_pct']:.0f}% | "
                                f"{hit['oi_diff']:,} contracts | "
                                f"{hit['days_increasing']}d increasing"),
                }
                added_oi += 1
        if added_oi:
            print(f"  [Daily Discovery] OI buildup: {added_oi} stocks showing unusual call positioning")
    except Exception as _e:
        print(f"  [Daily Discovery] OI buildup error: {_e}")

    if not new_syms:
        return []

    # Persist to MT basket
    existing_mt   = basket_mgr.load_mt()
    existing_meta = basket_mgr.load_mt_metadata()
    combined_mt   = list(dict.fromkeys(existing_mt + new_syms))
    basket_mgr.save_mt(combined_mt, {**existing_meta, **new_meta})
    basket_mgr.clear_uw_pending()

    msg = f"🆕 Daily discovery: {', '.join(new_syms)} added to MT basket — running full research now"
    print(f"  [Daily Discovery] {msg}")
    discord.send(msg)
    return new_syms


def _run_regime_sector_scan(macro_regime: dict, existing_basket: list[str]) -> list[str]:
    """
    Daily regime-driven sector scan.

    When the macro regime identifies winning sectors (weight ≥ 0.70), this function
    runs a broad FMP screener for EACH winning sector — not just the 65 pre-selected
    tickers — and adds the best new names directly to the MT basket for same-day
    committee review. This closes the gap between regime classification and actual
    new-stock discovery.

    Rate-limited: runs once per calendar day via a .lock file.
    Capped at max_per_sector=3 new names per winning sector.
    Returns list of newly discovered symbols added to the MT basket.
    """
    import os as _os
    if not macro_regime:
        return []

    # Run only once per day — morning cycle sets the lock, afternoon skips
    _today  = datetime.now(timezone.utc).date().isoformat()
    _lock   = _os.path.join(_os.path.dirname(__file__), f".regime_scan_{_today}.lock")
    if _os.path.exists(_lock):
        return []

    sector_weights  = macro_regime.get("sector_weights", {})
    regime_label    = macro_regime.get("regime_label", "growth_driven")
    regime_shift    = macro_regime.get("regime_shift", False)
    prev_label      = macro_regime.get("prev_regime_label")

    winning = [(s, w) for s, w in sector_weights.items() if w >= 0.70]
    if not winning:
        try: open(_lock, "w").close()
        except Exception: pass
        return []

    # Regime shift: alert before running scan
    if regime_shift and prev_label:
        _shift_msg = (
            f"⚡ MACRO REGIME SHIFT DETECTED\n"
            f"{prev_label.upper().replace('_',' ')} → {regime_label.upper().replace('_',' ')}\n"
            f"Winning sectors now: {', '.join(s for s,_ in sorted(winning, key=lambda x:-x[1]))}\n"
            f"Running sector opportunity scan for new names..."
        )
        print(f"\n  [RegimeScan] {_shift_msg}")
        discord.send(_shift_msg)

    print(f"\n  [RegimeScan] Running sector opportunity scan "
          f"({regime_label.upper()}) — {len(winning)} winning sectors...")

    try:
        from basket.curation import run_sector_opportunity_scan
        discoveries = run_sector_opportunity_scan(
            macro_regime, existing_basket, max_per_sector=3
        )
    except Exception as _e:
        print(f"  [RegimeScan] scan error: {_e}")
        try: open(_lock, "w").close()
        except Exception: pass
        return []

    if not discoveries:
        print("  [RegimeScan] No new opportunities found outside current basket")
        try: open(_lock, "w").close()
        except Exception: pass
        return []

    # Add all discoveries to the MT basket
    new_syms   = list(dict.fromkeys(d["symbol"] for d in discoveries))
    _today_str = datetime.now(timezone.utc).date().isoformat()
    existing_mt   = basket_mgr.load_mt()
    existing_meta = basket_mgr.load_mt_metadata()
    new_meta = {
        d["symbol"]: {
            "source":   "regime_scan",
            "added":    _today_str,
            "expires":  None,
            "note":     d["note"],
        }
        for d in discoveries
    }
    basket_mgr.save_mt(
        list(dict.fromkeys(existing_mt + new_syms)),
        {**existing_meta, **new_meta}
    )

    # Full research onboarding so the committee sees complete signals today
    run_new_ticker_onboarding(new_syms)

    # Discord summary grouped by sector
    from collections import defaultdict as _dd
    by_sector: dict = _dd(list)
    for d in discoveries:
        by_sector[d["sector_key"]].append(d["symbol"])
    sector_lines = "\n".join(
        f"  {sk} ({sector_weights.get(sk,0):.2f}): {', '.join(syms)}"
        for sk, syms in sorted(by_sector.items(), key=lambda x: -sector_weights.get(x[0], 0))
    )
    disc_msg = (
        f"🔭 Regime Scan [{regime_label.upper()}] — {len(new_syms)} new opportunities\n"
        f"{sector_lines}\n"
        f"All added to MT basket for committee review."
    )
    print(f"\n  [RegimeScan] {disc_msg}")
    discord.send(disc_msg)

    try: open(_lock, "w").close()
    except Exception: pass

    return new_syms


def run_cycle(dry_run: bool = False, force_opus: bool = False):
    import zoneinfo
    now_et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    time_label = now_et.strftime("%I:%M %p ET")
    day_label  = now_et.strftime("%a %b %d").replace(" 0", " ")
    trades_allowed = dry_run or is_market_hours()

    print(f"\n{'='*60}")
    print(f"Trading cycle started at {datetime.now(timezone.utc).isoformat()}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE PAPER TRADING'}")
    print('='*60)

    db.init()
    portfolio = alpaca.get_portfolio()
    positions = alpaca.get_positions()
    port_ctx  = _portfolio_context(portfolio, positions)

    # --- Market-wide context (once per cycle — free: CNN F&G + yfinance VIX) ---
    mkt_ctx = market_context.compute()
    # Augment with VIX term structure (^VIX spot vs ^VIX3M — inversion = regime risk)
    try:
        mkt_ctx["vix_term_structure"] = uw_flow.get_vix_term_structure()
    except Exception:
        mkt_ctx["vix_term_structure"] = {}
    # SPY daily return — used by geo_block to flag high-velocity down days
    try:
        import yfinance as _yf
        _spy_hist = _yf.Ticker("SPY").history(period="3d")
        if len(_spy_hist) >= 2:
            _spy_today  = float(_spy_hist["Close"].iloc[-1])
            _spy_prev   = float(_spy_hist["Close"].iloc[-2])
            _spy_vol    = float(_spy_hist["Volume"].iloc[-1])
            _spy_avgvol = float(_spy_hist["Volume"].mean())
            mkt_ctx["spy_day_return"]    = round((_spy_today - _spy_prev) / _spy_prev * 100, 2)
            mkt_ctx["spy_volume_ratio"]  = round(_spy_vol / _spy_avgvol, 2) if _spy_avgvol else 1.0
        else:
            mkt_ctx["spy_day_return"]   = 0.0
            mkt_ctx["spy_volume_ratio"] = 1.0
    except Exception:
        mkt_ctx["spy_day_return"]   = 0.0
        mkt_ctx["spy_volume_ratio"] = 1.0
    vts = mkt_ctx.get("vix_term_structure", {})
    vts_note = f" | VTS={'INVERTED⚠️' if vts.get('inverted') else 'normal'}({vts.get('vix_spot','?')}/{vts.get('vix_3m','?')})" if vts.get("inverted") is not None else ""
    _spy_dr = mkt_ctx.get("spy_day_return", 0)
    print(f"  Market: Fear&Greed={mkt_ctx['fear_and_greed'].get('score','?')} ({mkt_ctx['market_risk']}) | VIX={mkt_ctx['vix'].get('vix','?')}{vts_note} | SPY={_spy_dr:+.1f}%")
    if mkt_ctx.get("upcoming_macro_events"):
        print(f"  Macro events this week: {[e['event'] for e in mkt_ctx['upcoming_macro_events']]}")

    # --- Macro economic regime (FRED + yfinance, free, daily cache) ---
    # Gives the committee CPI trend, yield curve, NFP, jobless claims, dollar strength,
    # and upcoming high-impact economic calendar — so it can pre-position ahead of catalysts.
    _macro_regime: dict = {}
    try:
        from signals.macro_regime import compute as _macro_compute
        _macro_regime = _macro_compute()
    except Exception as _e:
        print(f"  [MacroRegime] error: {_e}")

    # --- UW market-wide context (once per cycle — tide, sector flows, SPY/QQQ flow) ---
    uw_mkt_ctx: dict = {}
    if config.UNUSUAL_WHALES_API_KEY:
        try:
            uw_mkt_ctx = uw_flow.get_uw_market_context()
        except Exception as _e:
            print(f"  [UW market context] error: {_e}")

    # Apply UW real-time sector flow overlay onto macro sector weights.
    # UW options flow on sector ETFs leads FRED data by 1-3 days — it shows
    # where institutional money is positioning NOW, before the macro stats confirm it.
    # e.g., FRED says growth_driven but UW shows defense=bullish → lift defense weight.
    if _macro_regime and uw_mkt_ctx.get("sector_flows"):
        _macro_regime = _apply_uw_sector_overlay(_macro_regime, uw_mkt_ctx["sector_flows"])

    print(f"Portfolio: equity=${portfolio['equity']:,.2f}  cash=${portfolio['cash']:,.2f}  positions={port_ctx['position_count']}")

    # --- Load gap scan alerts from 08:45 pre-open job ---
    import json as _json, os as _os
    _alerts_path = _os.path.join(_os.path.dirname(__file__), ".gap_alerts.json")
    gap_alerts_ctx: dict = {}
    if _os.path.exists(_alerts_path):
        try:
            with open(_alerts_path) as _f:
                gap_alerts_ctx = _json.load(_f)
            _os.remove(_alerts_path)  # consume once
        except Exception:
            pass
    if gap_alerts_ctx.get("summary"):
        print(f"  [GapScan context] {gap_alerts_ctx['summary']}")

    # Alert on out-of-basket UW discoveries from pre-market scan + queue for MT basket
    uw_disc = gap_alerts_ctx.get("uw_discovery", [])
    if uw_disc:
        top_disc = sorted(uw_disc, key=lambda x: x["premium"], reverse=True)[:5]
        disc_str = " | ".join(
            f"{d['symbol']} ${d['premium']/1e6:.1f}M call sweep"
            + (f" ({d['expiry_weeks']:.0f}w)" if d.get("expiry_weeks") else "")
            for d in top_disc
        )
        print(f"  [UW Discovery] Out-of-basket sweeps: {disc_str}")
        discord.send(f"🔭 UW OUT-OF-BASKET SWEEPS (queued for MT basket):\n{disc_str}")
        # Queue discoveries for MT basket — consumed at next weekly MT curation
        basket_mgr.queue_uw_discovery(top_disc)

    # --- Earnings cap enforcement (from gap scan flags) ---
    from basket.tier_criteria import EARNINGS_CAP_PCT
    pos_lookup_pre = {p["symbol"]: p for p in positions}
    for flag in (gap_alerts_ctx.get("earnings_cap_flags") or []):
        sym      = flag["symbol"]
        cap_pct  = flag["cap_pct"]
        cur_pct  = flag["current_pct"]
        tier     = flag["tier"]
        pos_raw  = pos_lookup_pre.get(sym)
        if not pos_raw:
            continue
        try:
            qty     = abs(float(pos_raw["qty"]))
            price   = float(pos_raw.get("current_price", 0))
            equity  = portfolio["equity"]
            mv      = qty * price
            target_mv  = equity * cap_pct / 100
            excess_mv  = mv - target_mv
            if excess_mv > 0 and price > 0:
                trim_qty = round(excess_mv / price, 6)
                if trim_qty > 0 and not dry_run:
                    alpaca.place_market_order(sym, trim_qty, "SELL")
                    db.log_trade(sym, "SELL", "stock", trim_qty, price, 0, 0,
                                 f"[EarningsCap] trimmed {cur_pct:.1f}%→{cap_pct}% before earnings")
                    discord.send(f"✂️ EARNINGS CAP {sym} [{tier}] "
                            f"trimmed {cur_pct:.1f}% → {cap_pct}% before binary event")
                    print(f"  [EarningsCap] {sym} trimmed {trim_qty:.4f} sh "
                          f"({cur_pct:.1f}%→{cap_pct}%)")
        except Exception as e:
            print(f"  [EarningsCap] error on {sym}: {e}")

    # --- Check stop-loss / take-profit ---
    # Build tech for held positions first so ATR-based stops can fire
    held_tech_map = {}
    for p in positions:
        sym = p["symbol"]
        try:
            held_tech_map[sym] = {"technical": technical.compute(_get_bars(sym))}
        except Exception:
            pass

    exits = manager.check_stops(positions, signals_map=held_tech_map)

    # Entry-day low breach check: if a new position closes below its entry-day low
    # for 2 consecutive sessions, flag it for immediate QUANT re-review.
    for p in positions:
        sym = p["symbol"]
        cur_price = float(p.get("current_price", 0))
        if cur_price > 0:
            try:
                streak = db.check_entry_low_breach(sym, cur_price)
                if streak >= 2:
                    exits.append({
                        "symbol": sym,
                        "action": "REVIEW",
                        "reason": (f"Entry-day low breached {streak} consecutive sessions "
                                   f"— QUANT re-review: is thesis still intact at ${cur_price:.2f}?")
                    })
            except Exception:
                pass

    pos_lookup = {p["symbol"]: p for p in positions}
    _stop_review_syms: dict = {}   # sym → stop reason; injected into committee candidates below
    for exit_order in exits:
        sym    = exit_order["symbol"]
        reason = exit_order["reason"]
        action = exit_order.get("action", "SELL")
        print(f"  [EXIT] {sym} — {action} | {reason}")
        if action == "REVIEW":
            discord.send(f"🔍 REVIEW **{sym}** | {reason} — check if thesis still intact")
            continue
        if action == "STOP_REVIEW":
            _stop_review_syms[sym] = reason
            discord.send(
                f"⚠️ **STOP-LOSS TRIGGERED — COMMITTEE REVIEW**\n"
                f"**{sym}** hit its stop-loss. Holding for committee decision this cycle.\n"
                f"📌 {reason}\n"
                f"Emergency auto-sell threshold: 2× tier stop."
            )
            continue
        if not dry_run:
            try:
                pos_raw = pos_lookup.get(sym)
                qty   = abs(float(pos_raw["qty"]))   if pos_raw else 0
                price = float(pos_raw.get("current_price", 0)) if pos_raw else 0
                if action == "TRIM":
                    trim_qty = round(qty * config.TRIM_SIZE_PCT, 6)
                    if trim_qty > 0:
                        alpaca.place_market_order(sym, trim_qty, "SELL")
                        db.log_trade(sym, "SELL", "stock", trim_qty, price, 0, 0,
                                     f"[Trim] {reason}")
                        discord.send(f"✂️ TRIM {sym} — sold {trim_qty:.4f} sh ({config.TRIM_SIZE_PCT:.0%}) | {reason}")
                elif qty > 0:
                    alpaca.place_market_order(sym, qty, "SELL")
                    db.log_trade(sym, "SELL", "auto", qty, price, 0, 10, reason)
                    # Notify — stop-loss exits had no Discord message before this fix
                    try:
                        _entry   = float(pos_raw.get("avg_entry_price") or pos_raw.get("cost_basis", 0) or 0)
                        _uplpct  = float(pos_raw.get("unrealized_plpc", 0))  # already scaled in broker/alpaca.py
                        _upldol  = float(pos_raw.get("unrealized_pl", 0))
                        _pl_str  = f"{_uplpct:+.1f}% (${_upldol:+,.0f})"
                        _ep_str  = f" | Entry ${_entry:.2f} → Exit ${price:.2f}" if _entry else ""
                        discord.send(
                            f"🔴 **STOP SELL {sym}** — {qty:.4f} sh @ ${price:.2f}{_ep_str}\n"
                            f"P&L: {_pl_str}\n"
                            f"📌 {reason}"
                        )
                    except Exception:
                        discord.send(f"🔴 **STOP SELL {sym}** | {reason}")
                    # Record outcome for learning — get P&L from position
                    try:
                        uplpct = float(pos_raw.get("unrealized_plpc", 0))
                        learning_db.record_outcome(sym, round(uplpct, 2), hold_days=0)
                    except Exception:
                        pass
            except Exception as e:
                print(f"    ERROR closing {sym}: {e}")

    # Detect position cap violations — NO auto-trim. Committee decides trim vs hold.
    # Violations are annotated on the candidate's synthesis so the committee gets full context.
    cap_violations = manager.check_hard_cap_violations(positions, portfolio["equity"])
    _cap_violation_map = {c["symbol"]: c for c in cap_violations}
    for cap in cap_violations:
        print(f"  [CapFlag] {cap['symbol']} at {cap['current_pct']:.1f}% | {cap['reason']} → flagging for committee")

    # ── Continuous learning: update 7/14d price outcomes for past entries ───────
    try:
        from learning import tracker as _ltracker
        _ltracker.outcome_update()
        # Recompute adaptive signal weights from accumulated outcomes
        _ltracker.compute_weights()
    except Exception as _le:
        print(f"  [Tracker] error: {_le}")

    # ── Bi-weekly committee review (runs every 13+ days) ─────────────────────
    try:
        from reports import biweekly_review as _review
        if _review.should_run_today():
            import zoneinfo as _rzi
            _now_et = datetime.now(_rzi.ZoneInfo("America/New_York"))
            # Run on weekends only (avoid disrupting live trading days)
            if _now_et.weekday() in (5, 6):   # 5 = Saturday, 6 = Sunday
                _rpt = _review.run_biweekly_review(dry_run=False)
                _summary = _rpt.get("summary", "")
                _changes = _rpt.get("param_changes", [])
                discord.send(
                    f"📋 BI-WEEKLY REVIEW COMPLETE\n{_summary}\n"
                    f"Auto-applied {len([c for c in _changes if c.get('confidence')=='high'])} param change(s). "
                    f"Full report saved to DB."
                )
    except Exception as _re:
        print(f"  [BiweeklyReview] error: {_re}")

    # Shadow mode graduation check — auto-unlocks +0.5 bonus once validation criteria met
    if config.UNUSUAL_WHALES_API_KEY:
        graduated = uw_flow.check_shadow_graduation()
        if graduated:
            discord.send("✅ UW shadow mode graduated — bullish sweep +0.5 conviction bonus is now LIVE")

    # Unusual Whales bearish flow check on HELD positions (risk management, always live)
    if config.UNUSUAL_WHALES_API_KEY:
        held_uw_signals = {}
        for p in positions:
            sym = p["symbol"]
            uw = uw_flow.compute(sym, current_price=float(p.get("current_price") or 0))
            held_uw_signals[sym] = {"options_flow": uw}
        bearish_flow_alerts = manager.check_bearish_flow_alerts(positions, held_uw_signals)

        # Consecutive bearish streak tracking — escalate after N days
        bearish_syms = set()
        for alert in bearish_flow_alerts:
            sym    = alert["symbol"]
            streak = db.increment_uw_bearish_streak(sym)
            alert["streak"] = streak
            bearish_syms.add(sym)
            if streak >= config.UW_CONSECUTIVE_BEARISH_EXIT:
                alert["action"] = "BEARISH_FLOW_SELL"
                alert["reason"] += f" ⚠️ STREAK={streak} consecutive bearish cycles — SELL recommended"

        # Reset streak for positions that had no bearish alert this cycle
        for p in positions:
            sym = p["symbol"]
            if sym not in bearish_syms:
                db.reset_uw_bearish_streak(sym)

        for alert in bearish_flow_alerts:
            sym    = alert["symbol"]
            reason = alert["reason"]
            action = alert.get("action", "BEARISH_FLOW_REVIEW")
            print(f"  [UW BEARISH ALERT] {sym} — {reason}")
            if action == "BEARISH_FLOW_SELL" and not dry_run:
                # Execute sell on streak-escalated bearish flow
                pos_raw = pos_lookup.get(sym)
                if pos_raw:
                    qty   = abs(float(pos_raw.get("qty", 0)))
                    price = float(pos_raw.get("current_price", 0))
                    if qty > 0:
                        try:
                            alpaca.place_market_order(sym, qty, "SELL")
                            db.log_trade(sym, "SELL", "stock", qty, price, 0, 0, reason)
                            discord.send(f"🐻🔴 BEARISH FLOW SELL **{sym}** | {reason}")
                        except Exception as e:
                            print(f"    [UW SELL ERROR] {sym}: {e}")
                            discord.send(f"🐻 BEARISH FLOW **{sym}** | {reason}")
            else:
                discord.send(f"🐻 BEARISH FLOW **{sym}** | {reason}")
            db.log_audit("bearish_flow_alert", sym, reason)

    # =========================================================
    # PHASE 1: Collect signals for all candidates (no Claude)
    # =========================================================
    stock_basket = basket_mgr.load_combined()   # LT + MT baskets merged

    # Daily discovery: congress buys (last 7 days) + UW pending queue.
    # Any out-of-basket ticker found is added to the MT basket immediately and
    # will be onboarded below — no waiting until Friday's weekly review.
    _discovery_added = _run_daily_discovery(stock_basket)
    if _discovery_added:
        stock_basket = basket_mgr.load_combined()   # reload to include new additions

    # Regime-driven sector scan: for each winning sector (weight ≥ 0.70) run a
    # broad FMP screener and add the best NEW names to the MT basket for today's
    # committee review. This is the structured link between the macro regime and
    # stock discovery — we are not limited to the 65 pre-selected tickers.
    # Rate-limited to once per calendar day via a .lock file.
    _regime_scan_added = _run_regime_sector_scan(_macro_regime, stock_basket)
    if _regime_scan_added:
        stock_basket = basket_mgr.load_combined()   # reload to include regime scan additions

    # Always include held positions even if they've been removed from the basket.
    # This ensures the committee can output a SELL decision on stale/demoted holdings
    # rather than silently ignoring them until a mechanical stop fires.
    _held_syms = {p["symbol"] for p in positions}
    _orphaned  = [s for s in _held_syms if s not in stock_basket]
    if _orphaned:
        print(f"  [Watchlist] +{len(_orphaned)} orphaned held position(s) added for exit review: {_orphaned}")
        stock_basket = stock_basket + _orphaned

    watchlist = stock_basket
    _n_watchlist    = len(watchlist)
    _n_lt           = len(basket_mgr.load())
    _n_mt           = len(basket_mgr.load_mt())
    _n_discovery    = len(_discovery_added) if _discovery_added else 0
    _n_regime_scan  = len(_regime_scan_added) if _regime_scan_added else 0
    _n_orphaned     = len(_orphaned) if _orphaned else 0
    print(f"  [Watchlist] {_n_watchlist} total: "
          f"LT={_n_lt} MT={_n_mt} discovery={_n_discovery} "
          f"regime_scan={_n_regime_scan} orphaned={_n_orphaned}")
    signals_map = {}
    tech_map: dict = {}  # tech per symbol for Phase 3 trade execution

    # Tickers with active PM/CIO directives bypass all pre-filters — the committee must
    # see them to either execute the directive or explicitly defer with a stated reason.
    import os as _os, json as _json
    _agenda_path = _os.path.join(_os.path.dirname(__file__), "basket", "committee_agenda.json")
    _directive_tickers: set = set()
    try:
        with open(_agenda_path) as _af:
            _agenda_data = _json.load(_af)
        for _item in _agenda_data.get("items", []):
            for _t in _item.get("force_review_tickers", []):
                _directive_tickers.add(_t)
    except Exception:
        pass
    if _directive_tickers:
        print(f"  [Agenda] Force-review tickers (bypass filters): {sorted(_directive_tickers)}")

    # --- New ticker onboarding: any basket ticker with NO cache gets full monthly-depth
    # research right now, automatically. No manual --monthly needed for new additions.
    _new_tickers = [s for s in watchlist if not research_cache.load(s)]
    if _new_tickers:
        run_new_ticker_onboarding(_new_tickers)

    # Stale cache refresh (separate from new-ticker onboarding — free signals only)
    _auto_warmed, _still_uncached = run_mt_cache_warmup(
        watchlist,
        max_age_days=config.CACHE_STALE_DAYS,
        max_symbols=config.AUTO_WARMUP_MAX,
        silent=True,
    )
    if _auto_warmed:
        print(f"  [Cache Warmup] Refreshed {_auto_warmed} stale ticker(s) before scan")

    scanned_count  = 0
    _drop_criteria = 0   # hard entry criteria failures (RSI, price, volume)
    _drop_earnings = 0   # earnings within CRITERIA_EARNINGS_DAYS
    _drop_prelim   = 0   # preliminary score gate
    _drop_cache    = 0   # on-demand warmup failed / cache still empty
    _filter_failed_syms: set = set()   # non-held MT tickers that failed pre-filter this cycle
    candidates: list[dict] = []

    for symbol in watchlist:
        print(f"\n  [{symbol}] collecting signals...")

        bars = _get_bars(symbol)
        tech = technical.compute(bars)

        # Held positions always bypass pre-filters — the committee must see them to exit.
        # Directive tickers (from committee agenda force_review_tickers) also bypass all
        # pre-filters so the committee can explicitly execute or defer the standing order.
        _is_held = symbol in _held_syms
        _is_directive = symbol in _directive_tickers

        # Quick technical pre-filter: skip only if price is below SMA200 AND a FRESH death
        # cross is active (<60 trading days old). Stale crosses (>60d) are carried by stocks
        # in early recovery — blocking them throws away the best mean-reversion entries.
        # Pre-filters are bypassed for currently held positions and directive tickers.
        if not _is_held and not _is_directive:
            price   = tech.get("price") or 0
            sma50   = tech.get("sma50") or 0
            sma200  = tech.get("sma200") or 0
            below_sma200     = sma200 > 0 and price < sma200
            below_sma50      = sma50 > 0 and price < sma50
            death_cross      = bool(tech.get("death_cross"))
            death_cross_days = tech.get("death_cross_days") or 0
            golden_cross     = bool(tech.get("golden_cross"))
            at_bb_upper      = tech.get("bb_position") == "above_upper"
            fresh_death_cross = death_cross and (death_cross_days == 0 or death_cross_days <= 60)
            if below_sma200 and fresh_death_cross and not golden_cross:
                print(f"  [{symbol}] -> SKIP | Quick filter: below SMA200 + death cross ({death_cross_days}d old)")
                db.log_audit("prelim_drop_techfilter", symbol,
                             f"below_sma200 + fresh_death_cross ({death_cross_days}d old)")
                _filter_failed_syms.add(symbol)
                continue
            if at_bb_upper and below_sma50:
                print(f"  [{symbol}] -> SKIP | Quick filter: overextended + below SMA50")
                db.log_audit("prelim_drop_techfilter", symbol, "at_bb_upper + below_sma50")
                _filter_failed_syms.add(symbol)
                continue

        scanned_count += 1
        sent = sentiment.compute(symbol)
        cong = congress.compute(symbol)
        insd = insider.compute(symbol)
        fund = fundamentals.compute(symbol)

        # Unusual Whales — fetch for every scanned LT/MT stock, not just final candidates.
        # Short interest, IV rank, OI changes, and dark pool are relevant to every
        # position in the book, not just options plays.
        uw = {"flow_signal": "no_data",
              "darkpool": {"darkpool_signal": "no_data", "large_print_count": 0,
                           "total_prints_3d": 0}}
        if config.UNUSUAL_WHALES_API_KEY:
            uw = uw_flow.compute(symbol, current_price=tech.get("price"))
            dp = uw_flow.get_darkpool(symbol)
            uw["darkpool"] = dp
            _uw_bonus  = uw_flow.conviction_bonus(uw)
            _dp_sig    = dp.get("darkpool_signal", "no_data")
            if uw["flow_signal"] != "no_data" or _dp_sig not in ("no_data", "quiet"):
                _mode_tag = "(shadow)" if uw.get("is_shadow_mode") else "(live)"
                print(f"  [{symbol}] UW flow: {uw['flow_signal']} "
                      f"pct={uw['normalized_prem_pct']} C/P={uw['call_put_ratio']} "
                      f"expiry={uw['expiry_alignment_score']} bonus={_uw_bonus:+.1f} {_mode_tag} | "
                      f"darkpool={_dp_sig} ({dp.get('large_print_count',0)} large prints) | "
                      f"short={uw.get('short_interest_pct')}% squeeze={uw.get('short_squeeze_score')} "
                      f"IV_rank={uw.get('iv_rank')} impl_move=±{uw.get('implied_move_pct')}%")

        db.log_signals(symbol, tech, sent, cong, insd, fund)

        signals = {
            "_symbol":         symbol,
            "technical":       tech,
            "sentiment":       sent,
            "congressional":   cong,
            "insider":         insd,
            "fundamentals":    fund,
            "market_context":  mkt_ctx,
            "uw_market":       uw_mkt_ctx,
            "options_flow":    uw,
        }
        signals_map[symbol] = signals

        # Hard criteria gate
        passes, criteria_reason = manager.check_entry_criteria(signals)
        if not passes and not _is_held and not _is_directive:
            print(f"  [{symbol}] -> SKIP | {criteria_reason}")
            db.log_audit("prelim_drop_criteria", symbol, criteria_reason)
            _drop_criteria += 1
            _filter_failed_syms.add(symbol)
            continue

        # Earnings check — cached first, live fallback if no date in cache
        # Held positions and directive tickers bypass — committee must see them.
        if not _is_held and not _is_directive:
            dte = research_cache.days_to_earnings_cached(symbol)
            if dte is None:
                # No cached date — live Finnhub call to catch recently-announced earnings
                live_earn = market_context.earnings_soon(symbol)
                if live_earn.get("earnings_soon"):
                    dte = live_earn.get("days_to_earnings")
            if dte is not None and 0 <= dte <= config.CRITERIA_EARNINGS_DAYS:
                print(f"  [{symbol}] -> SKIP | Earnings in {dte} day(s) — binary event risk")
                db.log_audit("prelim_drop_earnings", symbol, f"earnings in {dte}d")
                _drop_earnings += 1
                _filter_failed_syms.add(symbol)
                continue

        # Preliminary gate (technicals + free fundamentals only)
        # Held positions and directive tickers bypass — committee must review them.
        if not _is_held and not _is_directive:
            tier = config.TICKER_TIERS.get(symbol, "mid_growth")

            if tier == "speculative":
                # Spec hard gates: prevent falling-knife entries.
                # Gate 1: below SMA200 AND MACD bearish — the single rule that would have
                # blocked RXRX, MP, OKLO, and IONQ at their entry dates.
                _spec_price  = tech.get("price") or 0
                _spec_sma200 = tech.get("sma200") or 0
                _spec_macd_b = tech.get("macd_bearish") or (tech.get("macd_hist", 0) or 0) < 0
                _spec_below200 = _spec_sma200 > 0 and _spec_price < _spec_sma200
                if _spec_below200 and _spec_macd_b:
                    print(f"  [{symbol}] -> SKIP | Spec gate: below SMA200 + MACD bearish (falling knife)")
                    db.log_audit("prelim_drop_spec_gate", symbol,
                                 f"spec_below_sma200={_spec_below200} macd_bearish={_spec_macd_b}")
                    _drop_prelim += 1
                    _filter_failed_syms.add(symbol)
                    continue

                # Gate 2: 3m return < -15% with no nearby hard catalyst — no catching falling knives.
                _spec_r3m = tech.get("return_3m")
                if _spec_r3m is not None and _spec_r3m < -15:
                    print(f"  [{symbol}] -> SKIP | Spec gate: 3m return {_spec_r3m:.1f}% < -15% (no catalyst)")
                    db.log_audit("prelim_drop_spec_gate", symbol,
                                 f"3m_return={_spec_r3m:.1f}%")
                    _drop_prelim += 1
                    _filter_failed_syms.add(symbol)
                    continue

                # Gate 3: institutional confirmation — require at least one real signal.
                # Dark pool accumulation, insider net buying, or congress net buying in last 60d.
                _dp_sig   = uw.get("darkpool", {}).get("darkpool_signal", "no_data")
                _insd_sig = insd.get("net_signal", "neutral")
                _cong_sig = cong.get("net_signal", "neutral")
                _has_inst_confirm = (
                    _dp_sig in ("accumulation", "strong_accumulation") or
                    _insd_sig == "bullish" or
                    _cong_sig == "bullish"
                )
                if not _has_inst_confirm:
                    print(f"  [{symbol}] -> SKIP | Spec gate: no institutional confirmation "
                          f"(dp={_dp_sig}, insider={_insd_sig}, congress={_cong_sig})")
                    db.log_audit("prelim_drop_spec_gate", symbol,
                                 f"no_inst_confirm dp={_dp_sig} insider={_insd_sig} congress={_cong_sig}")
                    _drop_prelim += 1
                    _filter_failed_syms.add(symbol)
                    continue
            else:
                prelim_score = 0
                eps = fund.get("eps_growth_yoy")
                rev = fund.get("revenue_growth")
                r1m = tech.get("return_1m")
                r3m = tech.get("return_3m")
                rsi = tech.get("rsi")
                gc  = tech.get("golden_cross")
                dc  = tech.get("death_cross")
                dc_days = tech.get("death_cross_days") or 252
                macd_ha = tech.get("macd_hist_accel", False)
                if eps is None or eps > 0:   prelim_score += 1
                if rev is None or rev > 0:   prelim_score += 1
                if r1m is None or r1m > -5:  prelim_score += 1
                if r3m is None or r3m > 0:   prelim_score += 1
                if gc:                        prelim_score += 2
                # Only penalise fresh death crosses (<60d). Stale crosses are carried by
                # recovering stocks — treating them like new breakdowns floods the SKIP log.
                if dc and dc_days <= 60:      prelim_score -= 2
                elif dc and dc_days <= 120:   prelim_score -= 1   # fading penalty for older cross
                if rsi and rsi > 70:         prelim_score -= 1
                if macd_ha:                  prelim_score += 1   # MACD histogram accelerating = momentum building
                # UW enrichment: institutional signals adjust the prelim score
                _uw_dp  = uw.get("darkpool", {}).get("darkpool_signal", "no_data")
                _uw_oi  = (uw.get("oi_changes") or {}).get("oi_change_signal", "no_data")
                _uw_sig = uw.get("flow_signal", "no_data")
                _uw_ep  = uw.get("expiry_alignment_score", 0) or 0
                _uw_pct = uw.get("normalized_prem_pct", 0) or 0
                # Strong accumulation (≥2 prints >$5M) = +2; regular accumulation = +1
                if _uw_dp == "strong_accumulation":   prelim_score += 2
                elif _uw_dp == "accumulation":        prelim_score += 1
                if _uw_oi  == "call_accumulation":    prelim_score += 1
                # High-conviction sweep (expiry aligned + top 10% premium) = +2; plain sweep = +1
                if _uw_sig == "bullish_sweep" and _uw_ep >= 1.0 and _uw_pct >= 90:
                    prelim_score += 2
                elif _uw_sig == "bullish_sweep":      prelim_score += 1
                if _uw_sig == "bearish_sweep":        prelim_score -= 2
                # Adaptive learning adjustment: bump prelim score by the weighted
                # conviction delta from continuous learning (bounded ±1 point here).
                try:
                    from learning import tracker as _ltracker
                    _learn_adj = _ltracker.conviction_adjustment(signals)
                    _learn_bump = max(-1, min(1, round(_learn_adj)))
                    if _learn_bump != 0:
                        prelim_score += _learn_bump
                except Exception:
                    pass
                threshold = config.MID_GROWTH_PRELIM_MIN if tier == "mid_growth" else 1
                if prelim_score < threshold:
                    print(f"  [{symbol}] -> SKIP | Prelim score {prelim_score} "
                          f"(tier={tier}, need {threshold}, dc_age={dc_days}d)")
                    db.log_audit("prelim_drop_score", symbol,
                                 f"prelim={prelim_score} threshold={threshold} tier={tier} "
                                 f"eps={eps} rev={rev} r1m={r1m} r3m={r3m} gc={gc} dc={dc} dc_days={dc_days}")
                    _drop_prelim += 1
                    _filter_failed_syms.add(symbol)
                    continue

        # Load cached research (free — no API calls)
        cached = research_cache.load(symbol)
        if not cached:
            # On-demand warmup: fetch all free signals now so the ticker isn't
            # silently dropped before committee. Reuses sent/cong/insd/fund already
            # computed above; only fetches the pieces missing from the cache.
            # Safety net: onboarding should have caught this pre-scan.
            # Run lightweight warmup now so the ticker isn't dropped.
            print(f"  [{symbol}] -> WARMING | No cache after onboarding — fallback on-demand warmup...")
            try:
                _earn_data = market_context.earnings_soon(symbol)
                _fin_data  = financial_data.compute(symbol)
                _growth    = future_growth.compute(symbol)
                _earn_mom  = momentum_news.earnings_momentum(symbol)
                research_cache.save(symbol, {
                    "fundamentals":          fund,
                    "sentiment":             sent,
                    "congressional":         cong,
                    "insider":               insd,
                    "future_growth":         _growth,
                    "financial_data":        _fin_data,
                    "social":                {},
                    "earnings_data":         _earn_data,
                    "earnings_momentum":     _earn_mom,
                    "research_snippets":     [],
                    "research_source_count": 0,
                    "uw_short_interest":     {"short_interest_pct": uw.get("short_interest_pct"),
                                              "borrow_rate":        uw.get("borrow_rate"),
                                              "short_squeeze_score":uw.get("short_squeeze_score","no_data")},
                    "uw_iv":                 {"iv_rank":          uw.get("iv_rank"),
                                              "iv_percentile":    uw.get("iv_percentile"),
                                              "implied_move_pct": uw.get("implied_move_pct")},
                    "uw_oi_changes":         uw.get("oi_changes") or {},
                    "uw_darkpool":           uw.get("darkpool") or {},
                })
                cached = research_cache.load(symbol)
                print(f"  [{symbol}] on-demand warmup done")
            except Exception as _warm_err:
                print(f"  [{symbol}] -> SKIP | On-demand warmup failed: {_warm_err}")
                db.log_audit("prelim_drop_cache", symbol, f"warmup_error: {type(_warm_err).__name__}: {str(_warm_err)[:200]}")
                _drop_cache += 1
                continue
        if not cached:
            print(f"  [{symbol}] -> SKIP | Cache still empty after on-demand warmup")
            db.log_audit("prelim_drop_cache", symbol, "cache_empty_after_warmup")
            _drop_cache += 1
            continue
        earnings_data = cached.get("earnings_data") or {}
        research_data = {"snippets": cached.get("research_snippets", []),
                         "source_count": cached.get("research_source_count", 0)}
        fin_data      = cached.get("financial_data") or {}
        social_data   = cached.get("social") or {}
        growth_data   = cached.get("future_growth") or {}
        earn_momentum = cached.get("earnings_momentum") or {}
        sent          = cached.get("sentiment") or sent
        cong          = cached.get("congressional") or cong
        insd          = cached.get("insider") or insd
        # Back-fill any UW fields the live compute missed with cached structural data
        if cached.get("uw_short_interest") and not uw.get("short_interest_pct"):
            _c = cached["uw_short_interest"]
            uw.setdefault("short_interest_pct",  _c.get("short_interest_pct"))
            uw.setdefault("borrow_rate",          _c.get("borrow_rate"))
            uw.setdefault("short_squeeze_score",  _c.get("short_squeeze_score", "no_data"))
        if cached.get("uw_iv") and not uw.get("iv_rank"):
            _c = cached["uw_iv"]
            uw.setdefault("iv_rank",         _c.get("iv_rank"))
            uw.setdefault("iv_percentile",   _c.get("iv_percentile"))
            uw.setdefault("implied_move_pct",_c.get("implied_move_pct"))
        if cached.get("uw_darkpool") and uw.get("darkpool", {}).get("darkpool_signal") == "no_data":
            uw["darkpool"] = cached["uw_darkpool"]
        signals["options_flow"] = uw

        signals["research"]          = research_data
        signals["financial_data"]    = fin_data
        signals["social"]            = social_data
        signals["earnings"]          = earnings_data
        signals["earnings_momentum"] = earn_momentum
        signals["market_context"]    = mkt_ctx
        signals["future_growth"]     = growth_data


        g_score  = growth_data.get("score", 0)
        g_class  = growth_data.get("classification", "cached")
        g_winds  = growth_data.get("tailwinds", [])
        em_label = earn_momentum.get("label", "n/a")
        em_score = earn_momentum.get("combined_score", "n/a")
        print(f"  [{symbol}] growth={g_score}/100 ({g_class}) | tailwinds={g_winds} | "
              f"social={social_data.get('combined_label')} | earn_momentum={em_label}({em_score})")

        synthesis = claude_agent._build_synthesis(symbol, signals)
        tech_map[symbol] = tech

        # For held positions: attach last-cycle decision + current P&L so the
        # committee can explicitly say "add more / hold / trim / sell" with context.
        _last_dec = None
        if _is_held:
            try:
                _last_dec = db.get_last_committee_decision(symbol)
            except Exception:
                pass
            if not _last_dec:
                # If no prior decision, note it so committee knows this is a new hold
                _last_dec = {"action": "NEW_POSITION", "confidence": 0,
                             "rationale": "No prior committee review found.",
                             "cio_conf": 0, "quant_dec": "", "da_severity": "", "ts": ""}

        candidates.append({
            "symbol":         symbol,
            "signals":        signals,
            "synthesis":      synthesis,
            "_last_decision": _last_dec,
            "_stop_review":   _stop_review_syms.get(symbol),  # None if no stop triggered
        })
        print(f"  [{symbol}] -> CANDIDATE | queued for committee review")

    # =========================================================
    # PHASE 2: One committee Claude call for ALL candidates
    # =========================================================
    cycle_buys:  list[str] = []
    cycle_sells: list[str] = []
    cycle_holds: list[str] = []
    near_miss_lt: list[dict] = []   # BUCKET decisions in long_term sleeve
    near_miss_mt: list[dict] = []   # BUCKET decisions in medium_term sleeve
    _forced_review_syms: set = set()   # symbols injected for post-sell assessment

    if candidates:
        # ── Annotate cap-violation candidates so committee decides trim vs hold ──
        for _cand in candidates:
            _cv = _cap_violation_map.get(_cand["symbol"])
            if _cv:
                _cand["synthesis"] = (
                    f"[⚠️ POSITION CAP ALERT: {_cv['symbol']} is at {_cv['current_pct']:.1f}% of portfolio "
                    f"(limit is {config.MAX_POSITION_PCT}% standard / {config.WINNER_POSITION_CAP_PCT}% winner). "
                    f"Excess: ${_cv['excess_mv']:,.0f}. "
                    f"YOU must decide: output TRIM to reduce size, or HOLD if conviction justifies the oversize. "
                    f"No automatic trim will execute.]\n\n"
                ) + (_cand.get("synthesis") or "")

        # ── Pre-gate: force BUCKET on spec tickers in regime AVOID sectors ────
        _regime_weights = (_macro_regime or {}).get("sector_weights", {})
        _spec_buckets = []
        _pass_to_committee = []
        for _cand in candidates:
            _sym  = _cand["symbol"]
            _tier = config.TICKER_TIERS.get(_sym, "")
            if _tier == "speculative":
                _sec = config.SECTOR_MAP.get(_sym)
                _wt  = _regime_weights.get(_sec, 1.0) if _sec else 1.0
                if _wt < config.REGIME_MIN_SECTOR_WEIGHT:
                    print(f"  [{_sym}] -> PRE-GATE BUCKET | spec tier + sector '{_sec}' "
                          f"weight={_wt:.2f} < {config.REGIME_MIN_SECTOR_WEIGHT} (AVOID)")
                    _spec_buckets.append({
                        "symbol": _sym, "action": "BUCKET",
                        "rationale": (
                            f"Regime pre-gate: speculative tier, sector '{_sec}' "
                            f"weight {_wt:.2f} below AVOID threshold "
                            f"{config.REGIME_MIN_SECTOR_WEIGHT}"
                        ),
                        "confidence": 0, "cio_confidence": 0,
                        "final_confidence": 0, "bucket": "long_term",
                        "allocation_pct": 0, "target_pct": 0,
                        "asset_type": "stock", "option_direction": None,
                        "da_severity": "Low",
                    })
                    continue
            _pass_to_committee.append(_cand)
        candidates = _pass_to_committee

        # ── Fetch supporting context for committee ────────────────────────────
        _signal_weights = {}
        try:
            from learning.tracker import _load_weights as _lw
            _signal_weights = _lw() or {}
        except Exception:
            pass

        _stop_exits = []
        try:
            _stop_exits = db.get_recent_stop_exits(days=30)
        except Exception:
            pass

        # ── Force post-sell review for positions stopped out since last cycle ──
        # Symbols auto-sold by ATR/stop logic may not appear as candidates because
        # they fail pre-filters (e.g. RSI overbought, low growth score). We force
        # them into the committee so the PM gets an explicit "was this right?" verdict.
        _forced_review_syms = set()
        try:
            from datetime import timedelta as _td2
            _cutoff_24h = (datetime.utcnow() - _td2(hours=24)).isoformat()
            _recent_sells = db.get_recent_stop_exits(days=1)
            _held_syms    = {p["symbol"] for p in positions}
            _cand_syms    = {c["symbol"] for c in candidates}
            for _se in _recent_sells:
                _sym = _se["symbol"]
                if _sym in _held_syms or _sym in _cand_syms:
                    continue   # still held or already queued — skip
                if _sym not in signals_map:
                    continue   # not in this cycle's basket — skip
                _forced_review_syms.add(_sym)
                _synth = claude_agent._build_synthesis(_sym, signals_map[_sym])
                _note  = (f"[POST-SELL REVIEW — stopped out today: {_se.get('reason', _se.get('rationale','')[:80])}] "
                          f"Position was auto-sold by risk manager. Committee: was this exit correct? "
                          f"Is thesis broken or is this a re-entry opportunity? Output BUCKET if thesis intact, "
                          f"HOLD if unclear, or rationale explaining why the sell was correct.")
                candidates.append({
                    "symbol":       _sym,
                    "signals":      signals_map[_sym],
                    "synthesis":    f"{_note}\n\n{_synth}",
                    "_post_sell":   True,
                    "_sell_reason": _se.get("reason", _se.get("rationale", "")),
                    "_sell_price":  _se.get("price", 0),
                    "_sell_pl_pct": _se.get("pl_pct", None),
                })
                print(f"  [{_sym}] -> FORCED POST-SELL REVIEW | stopped out today")
        except Exception as _fre:
            print(f"  [PostSellReview] error: {_fre}")

        print(f"\n  [Committee] Reviewing {len(candidates)} candidates in one call...")
        decisions = claude_agent.committee_review(
            candidates, port_ctx, mkt_ctx,
            macro_regime=_macro_regime,
            force_opus=force_opus,
            signal_weights=_signal_weights,
            recent_stop_exits=_stop_exits,
        )
        decisions = _spec_buckets + decisions
        # Alert on committee fallback (Opus parse failure degraded to individual Haiku calls)
        fallback_errors = {d["symbol"]: d["_committee_fallback"]
                           for d in decisions if d.get("_committee_fallback")}
        if fallback_errors:
            err_sample = next(iter(fallback_errors.values()))
            discord.send(f"⚠️ Committee review fallback ({len(fallback_errors)} symbols) — {err_sample[:120]}")
    else:
        decisions = []
        print("  [Committee] No candidates passed all filters — skipping committee call")

    # =========================================================
    # PHASE 3: Execute approved trades
    # =========================================================
    _opt_queue: list[dict] = []  # committee-flagged options plays queued for Phase 4

    # MT weekly deployment cap — compute once before the loop
    _macro      = mkt_ctx.get("macro_momentum") or {}
    _regime_lbl = _macro.get("label", "neutral")
    _vix_now    = (mkt_ctx.get("vix") or {}).get("vix", 0)
    if isinstance(_vix_now, (int, float)) and _vix_now > 35 and _regime_lbl == "extreme_fear":
        _mt_cap_pct = 0.0   # CRISIS — no new MT
    elif isinstance(_vix_now, (int, float)) and _vix_now > 28 and _regime_lbl in ("risk_off", "extreme_fear"):
        _mt_cap_pct = 5.0   # STRESS — 5% NAV/week
    elif _regime_lbl == "risk_off":
        _mt_cap_pct = 10.0  # ELEVATED — 10% NAV/week
    else:
        _mt_cap_pct = 30.0  # Normal — no meaningful cap (30% = max sleeve)
    _mt_deployed_this_week = _mt_weekly_deployed_pct(portfolio["equity"])
    print(f"  [MT Cap] regime={_regime_lbl} | weekly cap={_mt_cap_pct:.0f}% | "
          f"deployed this week={_mt_deployed_this_week:.1f}%")

    for decision in decisions:
        symbol = decision["symbol"]
        signals = signals_map.get(symbol, {})
        decision = {**decision, "_symbol": symbol}

        if decision.get("_parse_error"):
            discord.send(f"⚠️ [{symbol}] Claude JSON parse error — defaulted to HOLD")

        # Post-sell assessment notification — send before any other processing
        if symbol in _forced_review_syms:
            try:
                _ps_action  = decision.get("action", "HOLD")
                _ps_conf    = decision.get("final_confidence") or decision.get("confidence", 0)
                _ps_rat     = decision.get("rationale", "")
                _ps_thesis  = decision.get("thesis_summary", "")
                _ps_quant   = decision.get("quant_signal", "")
                _ps_qdec    = decision.get("quant_decision", "")
                _ps_cco     = decision.get("cco_reason", "")
                _ps_bear    = decision.get("da_bear_case", "")
                _ps_sev     = decision.get("da_severity", "")
                _ps_pt      = decision.get("price_target")
                _orig_cand  = next((c for c in candidates if c["symbol"] == symbol), {})
                _sell_rsn   = _orig_cand.get("_sell_reason", "auto stop")
                _sell_pl    = _orig_cand.get("_sell_pl_pct")
                _pl_str     = f" ({_sell_pl:+.1f}%)" if _sell_pl is not None else ""
                _verdict    = {"BUCKET": "✅ Thesis intact — watching for re-entry",
                               "HOLD":   "⏸️ Inconclusive — monitoring",
                               "BUY":    "🟢 Re-entry warranted",
                               "SELL":   "🔴 Exit confirmed — thesis broken"}.get(_ps_action, _ps_action)
                _lines = [
                    f"🔍 **POST-SELL ASSESSMENT: {symbol}**",
                    f"Stopped out today{_pl_str} | {_sell_rsn[:100]}",
                    f"━━━ Committee Verdict: {_verdict} ━━━",
                    f"CIO conf={_ps_conf}/10 | Action={_ps_action}",
                ]
                if _ps_quant:
                    _lines.append(f"📊 **Quant [{_ps_qdec}]:** {_ps_quant}")
                if _ps_cco:
                    _lines.append(f"🚫 **CCO:** {_ps_cco}")
                if _ps_bear:
                    _lines.append(f"🐻 **Bear Case [{_ps_sev}]:** {_ps_bear}")
                if _ps_thesis:
                    _lines.append(f"\n📋 **Thesis Status**\n{_ps_thesis}")
                if _ps_pt:
                    _lines.append(f"🎯 **Re-entry target:** ${_ps_pt:.0f}")
                _lines.append(f"📌 {_ps_rat}")
                discord.send("\n".join(_lines))
            except Exception as _pse:
                print(f"  [PostSellNotify] error: {_pse}")

        decision = manager.apply_conviction_bonuses(decision, signals)
        decision = manager.validate(decision, port_ctx)

        action     = decision["action"]
        confidence = decision["confidence"]
        alloc      = decision["allocation_pct"]
        rationale  = decision["rationale"]
        asset_type = decision["asset_type"]

        print(f"  [{symbol}] -> {action} | confidence={confidence} | alloc={alloc}% | {rationale}")

        # Log every decision to the learning database
        try:
            learning_db.log_decision(symbol, decision, signals_map.get(symbol, {}), config)
        except Exception:
            pass

        if action == "HOLD":
            cycle_holds.append(symbol)
        elif action == "BUCKET":
            cycle_holds.append(f"{symbol}(BUCKET)")
            print(f"  [{symbol}] -> BUCKET (watchlist, no capital) | {rationale}")
            nm_entry = {
                "symbol":     symbol,
                "confidence": confidence,
                "rationale":  rationale,
                "cco_decision":  decision.get("cco_decision", "Approve"),
                "cco_reason":    decision.get("cco_reason", ""),
                "cro_decision":  decision.get("cro_decision", "Approve"),
                "cro_top_risk":  decision.get("cro_top_risk", ""),
                "quant_decision": decision.get("quant_decision", "Neutral"),
                "quant_signal":   decision.get("quant_signal", ""),
                "valuation_risk": decision.get("valuation_risk", "Low"),
                "da_severity":   decision.get("da_severity", "Low"),
                "da_bear_case":  decision.get("da_bear_case", ""),
                "catalyst_note": decision.get("catalyst_note", "N/A"),
            }
            if decision.get("bucket") == "medium_term":
                near_miss_mt.append(nm_entry)
            else:
                near_miss_lt.append(nm_entry)
        elif action == "TRIM" and trades_allowed:
            pos_raw = pos_lookup.get(symbol)
            if pos_raw:
                qty_held  = abs(float(pos_raw.get("qty", 0)))
                price_val = (tech_map.get(symbol) or {}).get("price") or float(pos_raw.get("current_price", 0))
                trim_qty  = round(qty_held * config.TRIM_SIZE_PCT, 6)
                if trim_qty > 0 and price_val > 0:
                    try:
                        alpaca.place_market_order(symbol, trim_qty, "SELL")
                        db.log_trade(symbol, "SELL", "stock", trim_qty, price_val, 0, confidence,
                                     f"[CommitteeTrim] {rationale}")
                        discord.send(f"✂️ COMMITTEE TRIM **{symbol}** | conf={confidence}/10 | {rationale}")
                        cycle_sells.append(f"{symbol}(TRIM)")
                        print(f"  [TRADE] TRIM {symbol} -{config.TRIM_SIZE_PCT:.0%} conf={confidence}/10")
                    except Exception as e:
                        print(f"    TRIM ORDER ERROR {symbol}: {e}")

        if action in ("BUY", "SELL") and trades_allowed:
            tech  = tech_map.get(symbol, {})
            price = tech.get("price", 0) or 1

            if action == "SELL":
                # For committee-decided exits, sell the full held qty.
                # compute_qty(alloc=0) returns 0 and would silently skip the order.
                _held = pos_lookup.get(symbol)
                qty   = abs(float(_held.get("qty", 0))) if _held else 0.0
            else:
                qty = manager.compute_qty(symbol, alloc, price, portfolio)

            if qty > 0:
                try:
                    if asset_type == "option":
                        # Never auto-execute options — queue for Phase 4 proposal
                        dirn = decision.get("option_direction") or "call"
                        _opt_queue.append({
                            "symbol":    symbol,
                            "direction": dirn,
                            "decision":  decision,
                            "signals":   signals,
                            "tech":      tech,
                        })
                        print(f"  [OptionsAdvisor] Queued committee option: "
                              f"{symbol} {dirn.upper()} conf={confidence}/10")
                        continue
                    else:
                        # ── MT weekly deployment cap check ────────────────
                        _is_mt_buy = (action == "BUY"
                                      and decision.get("bucket") == "medium_term")
                        if _is_mt_buy and _mt_deployed_this_week + alloc > _mt_cap_pct:
                            discord.send(
                                f"⛔ MT cap: {symbol} BUY blocked "
                                f"({_mt_deployed_this_week:.1f}% + {alloc:.1f}% > {_mt_cap_pct:.0f}% weekly cap). "
                                f"BUCKET'd to watchlist."
                            )
                            print(f"  [{symbol}] MT weekly cap exceeded — converting BUY → BUCKET")
                            decision["action"] = "BUCKET"
                            action = "BUCKET"
                        else:
                            from monitoring import self_healer as _sh
                            _ok, _err = _sh.place_order_with_retry(symbol, qty, action)
                            if not _ok:
                                print(f"  [ORDER FAIL] {action} {symbol}: {_err}")
                                continue   # skip logging and alerting — order did not execute
                            db.log_trade(symbol, action, asset_type, qty, price, alloc, confidence, rationale)
                            print(f"  [TRADE] {action} {symbol} conf={confidence}/10")
                            if action == "BUY":
                                cycle_buys.append(symbol)
                                if _is_mt_buy:
                                    _mt_deployed_this_week += alloc
                                # Record today's intraday low as the entry-day low.
                                _entry_low = tech.get("day_low") or tech.get("low") or price
                                try:
                                    db.set_entry_day_low(symbol, _entry_low)
                                except Exception:
                                    pass
                                # Continuous learning: snapshot signals at entry for
                                # outcome tracking 7/14 days later.
                                try:
                                    from learning import tracker as _tracker
                                    _sec_key    = config.SECTOR_MAP.get(symbol, "")
                                    _sec_weight = (_macro_regime.get("sector_weights") or {}).get(_sec_key, 0.0)
                                    _tracker.record_signal_outcome = lambda *a, **kw: None  # lazy guard
                                    db.record_signal_outcome(
                                        symbol       = symbol,
                                        entry_price  = price,
                                        signals      = signals,
                                        regime_label = _macro_regime.get("regime_label", ""),
                                        sector_key   = _sec_key,
                                        sector_weight = _sec_weight,
                                    )
                                except Exception:
                                    pass
                                # Attribution logging
                                try:
                                    from database import decisions_log as _dlog
                                    _regime_str = (
                                        "crisis"   if (isinstance(_vix_now, (int, float)) and _vix_now > 35 and _regime_lbl == "extreme_fear") else
                                        "stress"   if (isinstance(_vix_now, (int, float)) and _vix_now > 28 and _regime_lbl in ("risk_off", "extreme_fear")) else
                                        "elevated" if _regime_lbl == "risk_off" else "normal"
                                    )
                                    _dlog.log_committee_decision(
                                        symbol=symbol,
                                        action="BUY",
                                        sleeve=decision.get("bucket", ""),
                                        confidence=confidence,
                                        regime=_regime_str,
                                        allocation_pct=alloc,
                                        catalyst_type=decision.get("catalyst_type", ""),
                                        catalyst_date=decision.get("catalyst_date", ""),
                                        rationale=rationale,
                                        cio_confidence=decision.get("cio_confidence", confidence),
                                        da_severity=decision.get("da_severity", ""),
                                        crs_growth_gate=decision.get("crs_growth_gate", ""),
                                        price_at_decision=price,
                                        price_target=decision.get("price_target", 0.0),
                                    )
                                except Exception:
                                    pass
                            elif action == "SELL":
                                cycle_sells.append(symbol)

                        # Build rich notification with key signals
                        tier   = config.TICKER_TIERS.get(symbol, "mid_growth")
                        g_data = signals.get("future_growth", {})
                        g_score = g_data.get("score", "?")
                        r1m    = tech.get("return_1m")
                        rsi    = tech.get("rsi")
                        pe     = signals.get("fundamentals", {}).get("pe_ratio")
                        peg    = g_data.get("peg_ratio")
                        r40    = g_data.get("rule_of_40")
                        em     = (signals.get("earnings_momentum") or {}).get("label", "")
                        dollar_amt = round(portfolio["equity"] * alloc / 100, 0)

                        snap = []
                        if g_score != "?":    snap.append(f"growth={g_score}/100")
                        if peg:               snap.append(f"PEG={peg:.2f}")
                        if r40:               snap.append(f"R40={r40:.0f}")
                        if rsi:               snap.append(f"RSI={rsi:.0f}")
                        if r1m is not None:   snap.append(f"1M={r1m:+.1f}%")
                        if pe:                snap.append(f"PE={pe:.0f}")
                        if em:                snap.append(f"earnings={em}")

                        da_sev              = decision.get("da_severity", "")
                        da_bear             = decision.get("da_bear_case", "")
                        target              = decision.get("target_pct", alloc)
                        cio_c               = decision.get("cio_confidence", confidence)
                        thesis_summary      = decision.get("thesis_summary", "")
                        crs_moat            = decision.get("crs_product_moat", "")
                        crs_market          = decision.get("crs_market_outlook", "")
                        crs_edge            = decision.get("crs_competitive_edge", "")
                        crs_product         = decision.get("crs_product_advantage", "")
                        crs_catalyst        = decision.get("crs_growth_catalyst", "")
                        crs_why             = decision.get("crs_why_this_over_peers", "")
                        stop_criteria       = decision.get("thesis_break_criteria", "")
                        emoji = "🟢" if action == "BUY" else "🔴"

                        msg_lines = [
                            f"{emoji} **{action} {symbol}** [{tier}] Tranche 1/{3 if target > alloc else 1}",
                            f"CIO={cio_c}/10 → final={confidence}/10 | {alloc}% now → {target}% target (${dollar_amt:,.0f})",
                            f"{' | '.join(snap)}",
                        ]
                        if action == "BUY":
                            msg_lines.append(f"\n━━━ RESEARCH THESIS [{crs_moat.upper()} MOAT] ━━━")
                            if crs_market:
                                msg_lines.append(f"📈 **Market Outlook**\n{crs_market}")
                            if crs_edge:
                                msg_lines.append(f"⚔️ **Competitive Edge vs Peers**\n{crs_edge}")
                            if crs_product:
                                msg_lines.append(f"🔬 **Product Advantage**\n{crs_product}")
                            if crs_catalyst:
                                msg_lines.append(f"🚀 **Growth Catalyst**\n{crs_catalyst}")
                            if crs_why:
                                msg_lines.append(f"🎯 **Why This Over Peers**\n{crs_why}")
                            if thesis_summary:
                                msg_lines.append(f"\n📋 **Summary**\n{thesis_summary}")
                            msg_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
                        elif action == "SELL":
                            msg_lines.append(f"\n━━━ SELL RATIONALE ━━━")
                            _quant_sig  = decision.get("quant_signal", "")
                            _quant_dec  = decision.get("quant_decision", "")
                            _cco_reason = decision.get("cco_reason", "")
                            _cro_risk   = decision.get("cro_top_risk", "")
                            if _quant_sig:
                                msg_lines.append(f"📊 **Quant [{_quant_dec}]:** {_quant_sig}")
                            if _cro_risk:
                                msg_lines.append(f"⚖️ **Top Risk:** {_cro_risk}")
                            if _cco_reason:
                                msg_lines.append(f"🚫 **CCO:** {_cco_reason}")
                            if da_bear:
                                msg_lines.append(f"🐻 **Bear Case [{da_sev}]:** {da_bear}")
                            if thesis_summary:
                                msg_lines.append(f"\n📋 **Original Thesis**\n{thesis_summary}")
                            msg_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
                        msg_lines.append(f"📌 **Decision:** {rationale}")
                        _pt     = decision.get("price_target")
                        _pt_bas = decision.get("price_target_basis", "")
                        if _pt:
                            _pt_price = tech.get("price", 0) or price
                            _upside   = (_pt - _pt_price) / _pt_price * 100 if _pt_price else 0
                            msg_lines.append(f"🎯 **Price target:** ${_pt:.0f} ({_upside:+.0f}% upside)  [{_pt_bas}]")
                        if stop_criteria:
                            msg_lines.append(f"🛑 **Exit if:** {stop_criteria}")
                        if da_bear:
                            msg_lines.append(f"⚠️ **Bear case:** {da_bear} [{da_sev}]")
                        discord.send("\n".join(msg_lines))
                except Exception as e:
                    print(f"    ORDER ERROR: {e}")

    # =========================================================
    # PHASE 3b: Scale-in — advance tranches for existing positions
    # =========================================================
    active_tranches = db.get_all_tranches()
    held_syms_set   = {p["symbol"] for p in positions}
    for tranche in active_tranches:
        sym    = tranche["symbol"]
        t_num  = tranche["current_tranche"]
        target = tranche["target_pct"]
        if sym not in held_syms_set:
            # Position was closed — clean up tranche record
            db.delete_tranche(sym)
            continue
        if t_num >= 3:
            continue  # fully scaled in

        tech   = tech_map.get(sym) or (technical.compute(_get_bars(sym)) if sym in watchlist else {})
        em_label = (signals_map.get(sym, {}).get("earnings_momentum") or {}).get("label", "")
        r1m    = tech.get("return_1m")
        price  = tech.get("price")
        sma50  = tech.get("sma50")
        gc     = tech.get("golden_cross")
        vol_r  = tech.get("volume_ratio")

        # Tranche 2 triggers: earnings beat OR price breakout above prior SMA50 on volume
        t2_trigger = None
        if em_label in ("strong_bullish", "bullish"):
            t2_trigger = f"earnings_beat({em_label})"
        elif price and sma50 and price > sma50 * 1.05 and vol_r and vol_r > 1.3:
            t2_trigger = f"breakout(price {(price/sma50-1):.1%} above SMA50, vol {vol_r:.1f}x)"
        elif gc and r1m and r1m > 5:
            t2_trigger = f"golden_cross(1M={r1m:+.1f}%)"

        # Tranche 3 triggers: second independent confirmation, different signal type
        t3_trigger = None
        if t_num == 2 and t2_trigger:
            existing_t2 = tranche.get("tranche2_trigger") or ""
            existing_t2_type = existing_t2.split("(")[0]
            # Require a different signal type — compare prefix before "("
            if existing_t2_type and not t2_trigger.startswith(existing_t2_type):
                t3_trigger = t2_trigger

        add_trigger = (t3_trigger if t_num == 2 else t2_trigger)
        if add_trigger and trades_allowed:
            add_qty_pct = target * (config.TRANCHE_2_PCT if t_num == 1 else config.TRANCHE_3_PCT)
            price_val   = price or 1
            qty         = manager.compute_qty(sym, add_qty_pct, price_val, portfolio)
            if qty > 0:
                try:
                    alpaca.place_market_order(sym, qty, "BUY")
                    new_t = db.advance_tranche(sym, add_trigger)
                    db.log_trade(sym, "BUY", "stock", qty, price_val, add_qty_pct,
                                 tranche.get("final_confidence", 0),
                                 f"Tranche {new_t}: {add_trigger}")
                    print(f"  [TRANCHE {new_t}] {sym} +{add_qty_pct:.1f}% — {add_trigger}")
                    discord.send(f"📈 TRANCHE {new_t} {sym} +{add_qty_pct:.1f}% | {add_trigger}")
                    cycle_buys.append(f"{sym}(T{new_t})")
                except Exception as e:
                    print(f"    TRANCHE ORDER ERROR {sym}: {e}")

    # Register new BUYs from this cycle as tranche 1 positions
    for decision in decisions:
        sym = decision["symbol"]
        if decision.get("action") == "BUY" and sym in [b.split("(")[0] for b in cycle_buys]:
            target = decision.get("target_pct", decision.get("allocation_pct", 0) * 2)
            if target > 0:
                db.set_tranche(
                    sym,
                    target_pct=target,
                    final_confidence=decision.get("confidence", 0),
                    cio_confidence=decision.get("cio_confidence", 0),
                    da_severity=decision.get("da_severity", "Low"),
                    thesis_break_criteria=decision.get("thesis_break_criteria", ""),
                    bucket=decision.get("bucket", "long_term"),
                    catalyst_note=decision.get("catalyst_note", ""),
                    price_target=decision.get("price_target"),
                    price_target_basis=decision.get("price_target_basis", ""),
                )

    # Update price targets from every committee decision on a held position —
    # BUY adds, HOLDs, TRIMs, even pre-exit reviews all carry fresh price_target.
    _held_syms_now = {p["symbol"] for p in positions}
    for decision in decisions:
        sym = decision.get("symbol")
        _pt = decision.get("price_target")
        if _pt and sym in _held_syms_now:
            try:
                db.update_price_target(sym, _pt, decision.get("price_target_basis", ""))
            except Exception:
                pass

    # =========================================================
    # PHASE 4: Options advisory — propose high-conviction plays, never execute
    # =========================================================
    try:
        n_proposals = options_advisor.run(
            opt_queue=_opt_queue,
            candidates=candidates,
            decisions=decisions,
            mkt_ctx=mkt_ctx,
            signals_map=signals_map,
            op_db=op_db,
            discord=discord,
        )
        if n_proposals:
            print(f"  [OptionsAdvisor] {n_proposals} proposal(s) sent this cycle")
    except Exception as _e:
        print(f"  [OptionsAdvisor] Phase 4 error: {_e}")

    # =========================================================
    # PHASE 5: Monitor active options proposals for sell signals
    # =========================================================
    try:
        n_alerts = options_advisor.check_sell_signals(
            decisions=decisions,
            signals_map=signals_map,
            op_db=op_db,
            discord=discord,
            cfg=config,
        )
        if n_alerts:
            print(f"  [OptionsMonitor] {n_alerts} sell alert(s) sent this cycle")
    except Exception as _e:
        print(f"  [OptionsMonitor] Phase 5 error: {_e}")

    # =========================================================
    # PHASE 5b: Monitor LIVE (manually placed) option positions
    # Auto-close when option premium reaches the user's target
    # =========================================================
    try:
        _live_trades = op_db.get_active_live_trades()
        for _lt in _live_trades:
            _ct_sym   = _lt["contract_symbol"]
            _lt_id    = _lt["id"]
            _lt_sym   = _lt["symbol"]
            _lt_dirn  = _lt["direction"]
            _lt_qty   = _lt["qty"]
            _lt_entry = _lt["entry_price"]
            _lt_tgt   = _lt["target_price"]
            _lt_expiry = _lt["expiry"]

            _curr_px = alpaca.get_option_last_price(_ct_sym)
            if _curr_px is None:
                print(f"  [LiveOptions] {_ct_sym}: no quote available")
                continue

            _pct_vs_entry = round((_curr_px - _lt_entry) / _lt_entry * 100, 1) if _lt_entry else 0
            print(f"  [LiveOptions] {_ct_sym}: last=${_curr_px:.2f} target=${_lt_tgt:.2f} ({_pct_vs_entry:+.1f}%)")

            if _curr_px >= _lt_tgt:
                # Place limit sell at target (slightly below to ensure fill)
                _sell_px = round(_lt_tgt * 0.99, 2)
                try:
                    alpaca.place_option_limit_order(_ct_sym, _lt_qty, "SELL", _sell_px)
                    _close_reason = f"target hit: last=${_curr_px:.2f} >= target=${_lt_tgt:.2f}"
                    op_db.close_live_trade(_lt_id, _close_reason, _curr_px)
                    _pnl_pct = round((_curr_px - _lt_entry) / _lt_entry * 100, 1)
                    _pnl_usd = round((_curr_px - _lt_entry) * _lt_qty * 100, 2)
                    discord.send(
                        f"✅ OPTIONS TARGET HIT — {_lt_sym} {_lt_dirn.upper()}\n"
                        f"Contract: {_ct_sym}\n"
                        f"Entry: ${_lt_entry:.2f} → Now: ${_curr_px:.2f} | Target was: ${_lt_tgt:.2f}\n"
                        f"P&L: +${_pnl_usd:,.2f} (+{_pnl_pct:.1f}%) on {_lt_qty} contract(s)\n"
                        f"Sell limit order placed at ${_sell_px:.2f}. Expires: {_lt_expiry}"
                    )
                    print(f"  [LiveOptions] TARGET HIT {_ct_sym} — sell order placed at ${_sell_px:.2f}")
                except Exception as _se:
                    print(f"  [LiveOptions] sell order failed for {_ct_sym}: {_se}")
    except Exception as _e5b:
        print(f"  [LiveOptions] Phase 5b error: {_e5b}")

    # --- Snapshot ---
    portfolio_final = alpaca.get_portfolio()
    positions_final = alpaca.get_positions()
    db.log_snapshot(portfolio_final["equity"], portfolio_final["cash"], positions_final)

    equity = portfolio_final["equity"]
    cash   = portfolio_final["cash"]
    print(f"\nCycle complete. Equity: ${equity:,.2f}")

    # Daily basket intelligence — proactive rotation/macro scan using Haiku (cheap).
    # Only runs in the morning cycle (before 11 AM ET) to avoid double-firing.
    import zoneinfo as _zi
    if datetime.now(_zi.ZoneInfo("America/New_York")).hour < 11:
        try:
            run_basket_intelligence(
                mkt_ctx, _macro_regime, uw_mkt_ctx,
                basket_mgr.load_combined(),
            )
        except Exception as _bie:
            print(f"  [BasketIntel] skipped: {_bie}")

    # ── Trades executed this cycle ───────────────────────────────────────────
    trade_parts: list[str] = []
    exit_syms = [e["symbol"] for e in exits] if exits else []
    if exit_syms:
        for sym in exit_syms:
            reason = next((e["reason"] for e in exits if e["symbol"] == sym), "stop triggered")
            trade_parts.append(f"🔴 **SOLD {sym}** — {reason}")
    for sym in [b.split("(")[0] for b in cycle_buys]:
        d = next((x for x in decisions if x.get("symbol") == sym and x.get("action") == "BUY"), {})
        alloc_b  = d.get("allocation_pct", 0)
        target_b = d.get("target_pct", alloc_b)
        conf_b   = d.get("confidence", 0)
        rat_b    = d.get("rationale", "")
        dollar_b = round(portfolio["equity"] * alloc_b / 100, 0)
        trade_parts.append(
            f"🟢 **BOUGHT {sym}** — {alloc_b:.0f}% (${dollar_b:,.0f}) → target {target_b:.0f}%  conf={conf_b}/10"
        )
        if rat_b:
            trade_parts.append(f"   ↳ {rat_b}")
    for sym in [s.split("(")[0] for s in cycle_sells if s not in exit_syms]:
        d = next((x for x in decisions if x.get("symbol") == sym and x.get("action") == "SELL"), {})
        rat_s = d.get("rationale", "")
        trade_parts.append(f"🔴 **SOLD {sym}**" + (f" — {rat_s}" if rat_s else ""))

    # ── Near-buy watch: what needs to change ────────────────────────────────
    def _blocking_reasons(nm: dict) -> str:
        parts = []
        if nm.get("cco_decision") == "Reject":
            parts.append(f"CCO: {nm.get('cco_reason') or 'compliance gate'}")
        if nm.get("crs_growth_gate") == "Fail":
            parts.append("CRS: no compelling growth thesis yet")
        if nm.get("cro_decision") == "Block":
            parts.append(f"Risk: {nm.get('cro_top_risk') or 'risk block'}")
        if nm.get("quant_decision") in ("Bearish", "Block"):
            parts.append(f"Technicals: {nm.get('quant_signal') or 'bearish setup'}")
        if nm.get("valuation_risk") == "Extreme":
            parts.append("Valuation extreme — wait for pullback")
        if nm.get("da_severity") == "High":
            parts.append(f"Bear risk: {nm.get('da_bear_case') or 'high severity'}")
        conf = nm.get("confidence", 0)
        if conf < 7 and not parts:
            parts.append(f"Conviction {conf}/10 — need ≥7")
        if not parts:
            parts.append("Timing — wait for better entry setup")
        return " | ".join(parts)

    near_buy_parts: list[str] = []
    all_near = sorted(near_miss_lt + near_miss_mt, key=lambda x: x["confidence"], reverse=True)[:5]
    for nm in all_near:
        sym   = nm["symbol"]
        conf  = nm["confidence"]
        block = _blocking_reasons(nm)
        cat   = nm.get("catalyst_note", "")
        sleeve = "LT" if nm in near_miss_lt else "MT"
        line  = f"**{sym}** [{sleeve}] conf={conf}/10 — needs: {block}"
        if cat and cat != "N/A":
            line += f"\n   Catalyst: {cat}"
        near_buy_parts.append(line)

    # ── Positions approaching exit ───────────────────────────────────────────
    exit_watch_parts: list[str] = []
    tranche_map_scan = {t["symbol"]: t for t in db.get_all_tranches()}
    for p in positions_final:
        sym     = p["symbol"]
        cur     = float(p.get("current_price", 0) or 0)
        upl_pct = float(p.get("unrealized_plpc", 0) or 0)
        tranche = tranche_map_scan.get(sym)
        if not tranche or not cur:
            continue
        criteria = tranche.get("thesis_break_criteria") or ""
        stop_str = None
        for part in criteria.split("|"):
            part = part.strip()
            if part.lower().startswith("price_stop"):
                stop_str = part.split(":", 1)[-1].strip()
                break
        pt_val = tranche.get("price_target")
        pt_basis = tranche.get("price_target_basis") or ""

        # Flag: deep loss or approaching stop
        if upl_pct < -10:
            note = f"⚠️ **{sym}** down {upl_pct:.1f}%"
            if stop_str:
                note += f" — 🛑 stop {stop_str}"
            if pt_val and cur:
                note += f"  |  🎯 target ${pt_val:.0f}"
            exit_watch_parts.append(note)
        elif stop_str:
            try:
                stop_val_f = float(stop_str.replace("$", "").replace(",", ""))
                dist_pct = (cur - stop_val_f) / cur * 100
                if dist_pct < 8:
                    note = f"⚠️ **{sym}** ${cur:.2f} — 🛑 stop ${stop_val_f:.0f} ({dist_pct:.1f}% away)"
                    if pt_val and cur:
                        upside = (pt_val - cur) / cur * 100
                        note += f"  |  🎯 target ${pt_val:.0f} ({upside:+.0f}%)"
                    exit_watch_parts.append(note)
            except Exception:
                pass

    # ── Tranche scale-in alerts ───────────────────────────────────────────────
    # Show any T1 position that is close to the T2 trigger or has already fired
    tranche_alerts: list[str] = []
    for t in db.get_all_tranches():
        sym    = t["symbol"]
        t_num  = t.get("current_tranche", 1)
        target = t.get("target_pct", 0)
        if t_num >= 3 or not target:
            continue
        tech_t = tech_map.get(sym) or {}
        cur_t  = tech_t.get("price", 0) or 0
        sma50  = tech_t.get("sma50", 0) or 0
        vol_r  = tech_t.get("volume_ratio", 0) or 0
        r1m    = tech_t.get("return_1m")
        gc     = tech_t.get("golden_cross")
        em_t   = (signals_map.get(sym, {}).get("earnings_momentum") or {}).get("label", "")
        notes  = []
        if em_t in ("strong_bullish", "bullish"):
            notes.append(f"earnings beat → T{t_num+1} ready to add")
        elif cur_t and sma50 and cur_t > sma50 * 1.05 and vol_r > 1.3:
            pct_above = (cur_t / sma50 - 1) * 100
            notes.append(f"+{pct_above:.1f}% above SMA50 on {vol_r:.1f}× vol → T{t_num+1} breakout trigger met")
        elif gc and r1m and r1m > 5:
            notes.append(f"golden cross + 1M={r1m:+.1f}% → T{t_num+1} trigger approaching")
        if notes:
            tranche_alerts.append(f"📈 **{sym}** (T{t_num}/{3} @ {target:.1f}% target): {notes[0]}")

    # ── Time-stop warnings ────────────────────────────────────────────────────
    # Warn on positions that are approaching or past their dead-money window
    timestop_alerts: list[str] = []
    try:
        db_path_ts = os.path.join(os.path.dirname(__file__), "trading_agent.db")
        import sqlite3 as _sq3
        _conn_ts = _sq3.connect(db_path_ts)
        _c_ts    = _conn_ts.cursor()
        _c_ts.execute("SELECT symbol, MAX(ts) FROM trades WHERE action='BUY' GROUP BY symbol")
        buy_dates_ts = {sym: ts for sym, ts in _c_ts.fetchall()}
        _conn_ts.close()
        for p in positions_final:
            sym     = p["symbol"]
            upl_pct = float(p.get("unrealized_plpc", 0) or 0)
            ts_str  = buy_dates_ts.get(sym)
            if not ts_str:
                continue
            try:
                buy_dt  = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
                days_held = (datetime.now(timezone.utc) - buy_dt).days
                tier      = config.TICKER_TIERS.get(sym, "mid_growth")
                dead_days = 45 if tier == "medium_term" else 90
                dead_gain = 5.0 if tier == "medium_term" else 3.0
                if days_held >= dead_days and upl_pct < dead_gain:
                    timestop_alerts.append(
                        f"🕐 **{sym}** held {days_held}d  {upl_pct:+.1f}% — "
                        f"dead-money window reached ({dead_gain:.0f}% gain in {dead_days}d threshold)"
                    )
                elif days_held >= int(dead_days * 0.75) and upl_pct < dead_gain:
                    timestop_alerts.append(
                        f"⏳ **{sym}** held {days_held}d  {upl_pct:+.1f}% — "
                        f"approaching dead-money window ({dead_days - days_held}d left to show {dead_gain:.0f}%+)"
                    )
            except Exception:
                pass
    except Exception:
        pass

    # ── Assemble message ─────────────────────────────────────────────────────
    fg_score = mkt_ctx.get("fear_and_greed", {}).get("score", "?")
    fg_label = mkt_ctx.get("fear_and_greed", {}).get("label", "")
    vix_val  = mkt_ctx.get("vix", {}).get("vix", "?")
    cash_pct_scan = cash / equity * 100 if equity else 0

    msg_parts = [
        f"**📊 {time_label} Cycle — {day_label}**",
        f"Market: F&G={fg_score} ({fg_label})  VIX={vix_val}  |  "
        f"NAV ${equity:,.0f}  Cash {cash_pct_scan:.0f}%",
        "",
    ]

    if trade_parts:
        msg_parts.append("**Trades**")
        msg_parts.extend(trade_parts)
    else:
        msg_parts.append("**No trades this cycle**")

    # Record cycle funnel stats for EOD health digest
    try:
        from monitoring import health as _h
        _n_pre_gate = len(_spec_buckets) if '_spec_buckets' in locals() else 0
        _h.record_cycle_stats(
            n_scanned       = _n_watchlist,
            n_filtered      = len(candidates) + _n_pre_gate,
            n_committee     = len(decisions) - _n_pre_gate if decisions else 0,
            n_pregatebucket = _n_pre_gate,
            buys            = len(cycle_buys),
            sells           = len(cycle_sells),
            cycle_label     = "open" if datetime.now(tz_et).hour < 12 else "close",
            basket_breakdown= {
                "lt": _n_lt, "mt": _n_mt,
                "discovery": _n_discovery,
                "regime_scan": _n_regime_scan,
                "orphaned": _n_orphaned,
            },
        )
    except Exception:
        pass

    # Post-cycle self-healer: detect zero-candidate streaks and committee fallback patterns
    try:
        from monitoring import self_healer as _sh
        _quota_hits = 0
        try:
            from monitoring import health as _hq
            _quota_hits = sum(len(v) for v in _hq._api_quotas.values())
        except Exception:
            pass
        _post_cycle_stats = {
            "scanned":        _n_watchlist,
            "passed_filters": len(candidates),
            "buys":           len(cycle_buys),
            "sells":          len(cycle_sells),
            "quota_hits":     _quota_hits,
        }
        if len(cycle_buys) + len(cycle_sells) > 0:
            _sh.reset_zero_candidate_streak()
        _sh.run_post_cycle_check(_post_cycle_stats, decisions if decisions else [])
    except Exception:
        pass

    if near_buy_parts:
        msg_parts.append("")
        msg_parts.append("**👀 Watching — almost there**")
        msg_parts.extend(near_buy_parts)

    if exit_watch_parts or timestop_alerts:
        msg_parts.append("")
        msg_parts.append("**🚨 Exit Watch**")
        msg_parts.extend(exit_watch_parts)
        msg_parts.extend(timestop_alerts)

    if tranche_alerts:
        msg_parts.append("")
        msg_parts.append("**📈 Scale-in Opportunities**")
        msg_parts.extend(tranche_alerts)

    # ── Basket pruning: remove tickers from full basket after consecutive filter failures ──
    # Mega caps are never pruned. Thresholds by tier: large_growth=5, mid_growth/MT=3, spec=2.
    try:
        import json as _bpj
        _held_now     = {p["symbol"] for p in positions}
        _lt_set       = set(basket_mgr.load())
        _mt_set       = set(basket_mgr.load_mt())
        _full_basket  = _lt_set | _mt_set
        # Only consider non-held tickers that actually failed a filter this cycle
        _failed_candidates = _filter_failed_syms & _full_basket - _held_now

        if _failed_candidates:
            try:
                _fail_counts: dict = _bpj.load(open(basket_mgr.FAIL_COUNTS_FILE))
            except Exception:
                _fail_counts = {}

            # Thresholds by tier
            def _prune_threshold(sym: str) -> int | None:
                _tier = config.TICKER_TIERS.get(sym, "mid_growth")
                if _tier == "mega":          return None   # never prune mega caps
                if _tier == "large_growth":  return 5
                if _tier == "speculative":   return 2
                return 3   # mid_growth and MT-only tickers

            # Increment failure counts; reset tickers that passed filters this cycle
            for _s in _full_basket:
                if _s in _failed_candidates:
                    _fail_counts[_s] = _fail_counts.get(_s, 0) + 1
                elif _s in _fail_counts:
                    _fail_counts[_s] = 0   # passed this cycle — reset streak

            # Determine what to prune
            _to_prune = []
            for _s, _cnt in _fail_counts.items():
                if _s not in _full_basket or _s in _held_now:
                    continue
                _thresh = _prune_threshold(_s)
                if _thresh is not None and _cnt >= _thresh:
                    _to_prune.append(_s)

            if _to_prune:
                # Add to permanent exclusion list (survives monthly LT refresh)
                basket_mgr.add_excluded(_to_prune)
                # Also physically remove from MT basket
                _cur_mt   = [s for s in basket_mgr.load_mt() if s not in _to_prune]
                _cur_meta = {k: v for k, v in basket_mgr.load_mt_metadata().items()
                             if k not in _to_prune}
                basket_mgr.save_mt(_cur_mt, _cur_meta)
                # Clear counts for pruned tickers
                for _ps in _to_prune:
                    _fail_counts.pop(_ps, None)
                _prune_msg = (
                    f"🗑️ **Basket Pruned** — {len(_to_prune)} ticker(s) dropped after "
                    f"consecutive filter failures (will not return on monthly refresh): "
                    f"{', '.join(sorted(_to_prune))}"
                )
                print(f"  [BasketPrune] {_prune_msg}")
                discord.send(_prune_msg)

            _bpj.dump(_fail_counts, open(basket_mgr.FAIL_COUNTS_FILE, "w"), indent=2)
    except Exception as _bp_err:
        print(f"  [BasketPrune] error: {_bp_err}")

    summary = "\n".join(msg_parts)
    print(summary)
    discord.send(summary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",        action="store_true")
    parser.add_argument("--schedule",       action="store_true")
    parser.add_argument("--monthly",        action="store_true")
    parser.add_argument("--premarket",      action="store_true")
    parser.add_argument("--close-summary",  action="store_true")
    parser.add_argument("--basket-refresh",  action="store_true")
    parser.add_argument("--weekly",          action="store_true")
    parser.add_argument("--weekly-basket",   action="store_true")
    parser.add_argument("--bot",            action="store_true")
    parser.add_argument("--discord",        action="store_true")
    parser.add_argument("--force-opus",     action="store_true",
                        help="Use Opus 4.7 for the committee (manual/ad-hoc runs)")
    args = parser.parse_args()

    if args.monthly:
        run_monthly_research()
        return

    if args.weekly:
        weekly_review.run()
        return

    if args.weekly_basket:
        run_weekly_basket_review()
        return

    if args.discord:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        # Safe wrappers — a crash in one job must never kill the whole bot
        from monitoring import health as _health

        _cycle_lock = threading.Lock()

        def _safe_cycle():
            import zoneinfo as _rzi
            _now_et = datetime.now(_rzi.ZoneInfo("America/New_York"))
            _job_id = "trading_cycle_open" if _now_et.hour < 12 else "trading_cycle_close"
            _health.heartbeat(_job_id)
            # Mutex prevents concurrent cycles (startup catchup + APScheduler misfire recovery).
            # Dedup guard catches sequential duplicates within 30 min.
            if not _cycle_lock.acquire(blocking=False):
                print("[Cycle] Skip — another cycle is already running")
                return
            try:
                _snaps = db.get_snapshots(limit=1)
                if _snaps:
                    _last_ts  = datetime.fromisoformat(_snaps[0]["ts"])
                    _age_min  = (datetime.utcnow() - _last_ts).total_seconds() / 60
                    if _age_min < 30:
                        print(f"[Cycle] Dedup skip — last cycle was {_age_min:.0f} min ago")
                        return
                run_cycle()
            except Exception as e:
                msg = f"❌ Trading cycle crashed: {e}"
                print(msg)
                _health.record_silent_error(_job_id, str(e), severity="high")
                try: discord.send(msg)
                except Exception: pass
            finally:
                _cycle_lock.release()

        def _safe_monthly():
            _health.heartbeat("monthly_research")
            try:
                run_monthly_research()
            except Exception as e:
                msg = f"❌ Monthly research crashed: {e}"
                print(msg)
                _health.record_silent_error("monthly_research", str(e), severity="high")
                try: discord.send(msg)
                except Exception: pass

        def _safe_earnings_reaction():
            _health.heartbeat("earnings_reaction")
            # Morning health check runs first — fixes what it can, escalates the rest
            try:
                from monitoring.self_healer import run_morning_check
                run_morning_check()
            except Exception as _mc_err:
                print(f"  [SelfHealer] Morning check error: {_mc_err}")
            try:
                from reports.earnings_reaction import run_earnings_reaction
                run_earnings_reaction(dry_run=False)
            except Exception as e:
                msg = f"❌ Earnings reaction crashed: {e}"
                print(msg)
                _health.record_silent_error("earnings_reaction", str(e), severity="high")
                try: discord.send(msg)
                except Exception: pass

        def _safe_premarket():
            _health.heartbeat("premarket_summary")
            try:
                reporter.run_premarket(dry_run=False)
            except Exception as e:
                print(f"Premarket summary error: {e}")
                _health.record_silent_error("premarket_summary", str(e))

        def _safe_close():
            _health.heartbeat("close_summary")
            try:
                reporter.run_close()
            except Exception as e:
                print(f"Close summary error: {e}")
                _health.record_silent_error("close_summary", str(e))

        def _safe_weekly():
            _health.heartbeat("weekly_review")
            try:
                weekly_review.run()
            except Exception as e:
                msg = f"❌ Weekly review crashed: {e}"
                print(msg)
                _health.record_silent_error("weekly_review", str(e), severity="high")
                try: discord.send(msg)
                except Exception: pass

        def _safe_weekly_basket():
            _health.heartbeat("weekly_basket_review")
            try:
                run_weekly_basket_review()
            except Exception as e:
                msg = f"❌ Weekly basket review crashed: {e}"
                print(msg)
                _health.record_silent_error("weekly_basket_review", str(e), severity="high")
                try: discord.send(msg)
                except Exception: pass

        def _safe_daily_basket_review():
            _health.heartbeat("daily_basket_review")
            try:
                run_daily_basket_review()
            except Exception as e:
                msg = f"❌ Daily basket review crashed: {e}"
                print(msg)
                _health.record_silent_error("daily_basket_review", str(e), severity="high")
                try: discord.send(msg)
                except Exception: pass

        def _safe_gap_scan():
            _health.heartbeat("gap_scan")
            try:
                run_gap_scan()
            except Exception as e:
                print(f"Gap scan error: {e}")
                _health.record_silent_error("gap_scan", str(e))

        def _safe_midday():
            _health.heartbeat("midday_check")
            try:
                run_midday_check()
            except Exception as e:
                print(f"Midday check error: {e}")
                _health.record_silent_error("midday_check", str(e))

        def _safe_spec_research():
            try:
                run_spec_research()
            except Exception as e:
                msg = f"❌ Spec research crashed: {e}"
                print(msg)
                try: discord.send(msg)
                except Exception: pass

        def _safe_watchdog():
            try:
                from monitoring.watchdog import run_watchdog
                run_watchdog()
            except Exception as e:
                print(f"Watchdog error: {e}")

        def _safe_uw_sweep_scan():
            try:
                run_uw_sweep_scan()
            except Exception as e:
                print(f"UW sweep scan error: {e}")

        def _safe_uw_intraday_scan():
            try:
                run_uw_intraday_scan()
            except Exception as e:
                print(f"UW intraday scan error: {e}")

        def _startup_catchup():
            """
            Runs 15s after bot start. APScheduler does NOT replay missed jobs across
            process restarts (misfire_grace_time only covers executor delays, not
            downtime). This function fills that gap for ALL scheduled morning jobs.
            """
            import zoneinfo
            from monitoring.health import _db_last_heartbeat as _last_hb
            now_et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
            if now_et.weekday() >= 5:
                print("[Startup] Catchup skipped — weekend")
                return

            # ── Replay missed pre-market jobs if past their scheduled time ──────
            # Each check: only run if (a) past the scheduled ET time today and
            # (b) no heartbeat recorded in the DB today (idempotency guard).
            today = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
            missed_jobs = []

            # Pre-market jobs are only worth replaying if we're still close to
            # when they should have run — stale pre-market content sent mid-day
            # or after-hours is confusing and irrelevant.
            _MAX_PREMARKET_REPLAY_MIN = 90  # skip replay if >90 min past schedule

            er_sched = today.replace(hour=config.EARNINGS_REACTION_HOUR,
                                     minute=config.EARNINGS_REACTION_MINUTE)
            if (now_et >= er_sched
                    and (now_et - er_sched).total_seconds() / 60 <= _MAX_PREMARKET_REPLAY_MIN
                    and not _last_hb("earnings_reaction")):
                missed_jobs.append("earnings_reaction")
                _safe_earnings_reaction()

            gs_sched = today.replace(hour=config.GAP_SCAN_HOUR,
                                     minute=config.GAP_SCAN_MINUTE)
            if (now_et >= gs_sched
                    and (now_et - gs_sched).total_seconds() / 60 <= _MAX_PREMARKET_REPLAY_MIN
                    and not _last_hb("gap_scan")):
                missed_jobs.append("gap_scan")
                _safe_gap_scan()

            pm_sched = today.replace(hour=config.PREMARKET_SUMMARY_HOUR,
                                     minute=config.PREMARKET_SUMMARY_MINUTE)
            if (now_et >= pm_sched
                    and (now_et - pm_sched).total_seconds() / 60 <= _MAX_PREMARKET_REPLAY_MIN
                    and not _last_hb("premarket_summary")):
                missed_jobs.append("premarket_summary")
                _safe_premarket()

            # Midday check: replay if within 20 min of scheduled time (lightweight, always safe to re-run)
            mc_sched = today.replace(hour=config.MIDDAY_HOUR, minute=config.MIDDAY_MINUTE)
            if (now_et >= mc_sched
                    and (now_et - mc_sched).total_seconds() / 60 <= 20
                    and not _last_hb("midday_check")):
                missed_jobs.append("midday_check")
                _safe_midday()

            if missed_jobs:
                print(f"[Startup] Replayed missed jobs: {', '.join(missed_jobs)}")
                try: discord.send(f"🔄 Startup catchup ran missed jobs: {', '.join(missed_jobs)}")
                except Exception: pass

            # ── No automatic cycle catchup on restart — cycles run on schedule only ──
            # Use /runcycle in Discord or restart at a scheduled time to trigger manually.
            print("[Startup] Catchup cycle disabled — waiting for next scheduled cycle.")

        # misfire_grace_time: covers executor delays (not process restarts —
        # those are handled by _startup_catchup above)
        GRACE = 3600

        scheduler = BackgroundScheduler(timezone="America/New_York")

        # Monthly: 1st Monday — full research + basket curation
        scheduler.add_job(
            _safe_monthly,
            CronTrigger(day="1-7", day_of_week="mon",
                        hour=config.BASKET_REFRESH_HOUR,
                        minute=config.BASKET_REFRESH_MINUTE),
            id="monthly_research",
            misfire_grace_time=GRACE,
        )
        # Overnight earnings reaction: Mon-Fri 7:30 ET
        scheduler.add_job(
            _safe_earnings_reaction,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.EARNINGS_REACTION_HOUR,
                        minute=config.EARNINGS_REACTION_MINUTE),
            id="earnings_reaction",
            misfire_grace_time=GRACE,
        )
        # Pre-market summary: Mon-Fri 9:00 ET (16:00 AST)
        scheduler.add_job(
            _safe_premarket,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.PREMARKET_SUMMARY_HOUR,
                        minute=config.PREMARKET_SUMMARY_MINUTE),
            id="premarket_summary",
            misfire_grace_time=GRACE,
        )
        # Gap + catalyst scanner: Mon-Fri 8:45 ET (lightweight, no Claude)
        scheduler.add_job(
            _safe_gap_scan,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.GAP_SCAN_HOUR,
                        minute=config.GAP_SCAN_MINUTE),
            id="gap_scan",
            misfire_grace_time=GRACE,
        )
        # Morning committee cycle: Mon-Fri 9:50 ET (post-auction)
        scheduler.add_job(
            _safe_cycle,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.RUN_HOUR,
                        minute=config.RUN_MINUTE),
            id="trading_cycle_open",
            misfire_grace_time=GRACE,
        )
        # Midday stop check: Mon-Fri 12:30 ET (no new entries, no Claude)
        scheduler.add_job(
            _safe_midday,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.MIDDAY_HOUR,
                        minute=config.MIDDAY_MINUTE),
            id="midday_check",
            misfire_grace_time=GRACE,
        )
        # Afternoon committee cycle: Mon-Fri 15:30 ET
        scheduler.add_job(
            _safe_cycle,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.AFTERNOON_HOUR,
                        minute=config.AFTERNOON_MINUTE),
            id="trading_cycle_close",
            misfire_grace_time=GRACE,
        )
        # Close summary: Mon-Fri 16:05 ET (23:05 AST)
        scheduler.add_job(
            _safe_close,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.CLOSE_SUMMARY_HOUR,
                        minute=config.CLOSE_SUMMARY_MINUTE),
            id="close_summary",
            misfire_grace_time=GRACE,
        )
        # Daily MT basket review: Mon-Thu 16:15 ET (after close summary; Fri uses weekly refresh)
        scheduler.add_job(
            _safe_daily_basket_review,
            CronTrigger(day_of_week="mon-thu", hour=16, minute=15),
            id="daily_basket_review",
            misfire_grace_time=GRACE,
        )
        # Weekly basket review: Friday 16:30 ET (moved from Saturday)
        scheduler.add_job(
            _safe_weekly_basket,
            CronTrigger(day_of_week="fri",
                        hour=config.BASKET_WEEKLY_REVIEW_HOUR,
                        minute=config.BASKET_WEEKLY_REVIEW_MINUTE),
            id="weekly_basket_review",
            misfire_grace_time=GRACE,
        )
        # Bi-weekly speculative refresh: Wednesday 18:00 ET (alternating weeks)
        # week_of_year % 2 == 0 targets even weeks — fires every other Wednesday
        scheduler.add_job(
            _safe_spec_research,
            CronTrigger(day_of_week="wed",
                        hour=config.SPEC_REFRESH_HOUR,
                        minute=config.SPEC_REFRESH_MINUTE,
                        week="*/2"),
            id="spec_research_biweekly",
            misfire_grace_time=GRACE,
        )
        # Weekly portfolio review: Sunday 18:00 ET (01:00 AST)
        scheduler.add_job(
            _safe_weekly,
            CronTrigger(day_of_week="sun", hour=18, minute=0),
            id="weekly_review",
            misfire_grace_time=GRACE,
        )
        # Watchdog: every 30 min Mon-Fri — checks for missed jobs, alerts immediately
        scheduler.add_job(
            _safe_watchdog,
            CronTrigger(day_of_week="mon-fri", hour="7-17", minute="*/30"),
            id="watchdog",
            misfire_grace_time=GRACE,
        )
        # UW sweep feed: every 5 min Mon-Fri (market hours guard inside)
        from apscheduler.triggers.interval import IntervalTrigger as _IntervalTrigger
        scheduler.add_job(
            _safe_uw_sweep_scan,
            _IntervalTrigger(minutes=5),
            id="uw_sweep_scan",
            misfire_grace_time=120,
        )
        # UW basket refresh: every 15 min Mon-Fri (market hours guard inside)
        scheduler.add_job(
            _safe_uw_intraday_scan,
            _IntervalTrigger(minutes=15),
            id="uw_intraday_scan",
            misfire_grace_time=300,
        )
        # One-time startup catchup — fires 15s after process start
        from datetime import timedelta as _td
        scheduler.add_job(
            _startup_catchup,
            "date",
            run_date=datetime.now() + _td(seconds=15),
            id="startup_catchup",
        )

        scheduler.start()
        print(
            f"[Scheduler] Started inside Kimmy:\n"
            f"  Monthly research      : 1st Monday/month {config.BASKET_REFRESH_HOUR}:{config.BASKET_REFRESH_MINUTE:02d}  (15:00 AST)\n"
            f"  Gap + catalyst scan   : Mon-Fri {config.GAP_SCAN_HOUR}:{config.GAP_SCAN_MINUTE:02d}  ET (15:45 AST) lightweight\n"
            f"  Pre-market summary    : Mon-Fri {config.PREMARKET_SUMMARY_HOUR}:{config.PREMARKET_SUMMARY_MINUTE:02d}  (16:00 AST)\n"
            f"  Trading cycle (AM)    : Mon-Fri {config.RUN_HOUR}:{config.RUN_MINUTE:02d}  ET (16:50 AST)\n"
            f"  Midday stop check     : Mon-Fri {config.MIDDAY_HOUR}:{config.MIDDAY_MINUTE:02d}  ET (19:30 AST) no Claude\n"
            f"  Trading cycle (PM)    : Mon-Fri {config.AFTERNOON_HOUR}:{config.AFTERNOON_MINUTE:02d}  ET (22:00 AST)\n"
            f"  Close summary         : Mon-Fri {config.CLOSE_SUMMARY_HOUR}:{config.CLOSE_SUMMARY_MINUTE:02d}  ET (23:05 AST)\n"
            f"  Daily MT basket review: Mon-Thu 16:15 ET (Haiku; removes broken, adds 1:1)\n"
            f"  Basket review         : Friday {config.BASKET_WEEKLY_REVIEW_HOUR}:{config.BASKET_WEEKLY_REVIEW_MINUTE:02d}  ET (23:30 AST) full weekly refresh\n"
            f"  Spec research refresh : Wednesday {config.SPEC_REFRESH_HOUR}:{config.SPEC_REFRESH_MINUTE:02d} ET (bi-weekly, spec tier only)\n"
            f"  Weekly portfolio review: Sunday 18:00 ET (01:00 AST)\n"
            f"  UW sweep feed scan    : every 5 min Mon-Fri 9:30-16:00 ET (23:00 AST)\n"
            f"  UW basket refresh     : every 15 min Mon-Fri 9:30-16:00 ET (23:00 AST) (~5K calls/day)"
        )

        from notifications import discord_bot
        discord_bot.run_bot()
        # If Discord token not set, keep scheduler alive with a simple loop
        import time as _time
        while True: _time.sleep(60)
    elif args.premarket:
        db.init()
        from reports.earnings_reaction import run_earnings_reaction
        run_earnings_reaction(dry_run=False)
        reporter.run_premarket(dry_run=False)
    elif args.close_summary:
        db.init()
        reporter.run_close()
    elif args.basket_refresh:
        db.init()
        basket_mgr.refresh()
        run_mt_cache_warmup(basket_mgr.load_combined())
    elif args.schedule:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = BlockingScheduler(timezone="America/New_York")
        scheduler.add_job(
            run_monthly_research,
            CronTrigger(day="1-7", day_of_week="mon",
                        hour=config.BASKET_REFRESH_HOUR,
                        minute=config.BASKET_REFRESH_MINUTE),
            id="monthly_research",
        )
        scheduler.add_job(
            run_gap_scan,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.GAP_SCAN_HOUR,
                        minute=config.GAP_SCAN_MINUTE),
            id="gap_scan",
        )
        scheduler.add_job(
            lambda: reporter.run_premarket(dry_run=args.dry_run),
            CronTrigger(day_of_week="mon-fri",
                        hour=config.PREMARKET_SUMMARY_HOUR,
                        minute=config.PREMARKET_SUMMARY_MINUTE),
            id="premarket_summary",
        )
        scheduler.add_job(
            lambda: run_cycle(dry_run=args.dry_run),
            CronTrigger(day_of_week="mon-fri",
                        hour=config.RUN_HOUR,
                        minute=config.RUN_MINUTE),
            id="trading_cycle_open",
        )
        scheduler.add_job(
            run_midday_check,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.MIDDAY_HOUR,
                        minute=config.MIDDAY_MINUTE),
            id="midday_check",
        )
        scheduler.add_job(
            lambda: run_cycle(dry_run=args.dry_run),
            CronTrigger(day_of_week="mon-fri",
                        hour=config.AFTERNOON_HOUR,
                        minute=config.AFTERNOON_MINUTE),
            id="trading_cycle_close",
        )
        scheduler.add_job(
            reporter.run_close,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.CLOSE_SUMMARY_HOUR,
                        minute=config.CLOSE_SUMMARY_MINUTE),
            id="close_summary",
        )
        scheduler.add_job(
            run_weekly_basket_review,
            CronTrigger(day_of_week="fri",
                        hour=config.BASKET_WEEKLY_REVIEW_HOUR,
                        minute=config.BASKET_WEEKLY_REVIEW_MINUTE),
            id="weekly_basket_review",
        )
        scheduler.add_job(
            run_spec_research,
            CronTrigger(day_of_week="wed",
                        hour=config.SPEC_REFRESH_HOUR,
                        minute=config.SPEC_REFRESH_MINUTE,
                        week="*/2"),
            id="spec_research_biweekly",
        )
        scheduler.add_job(
            weekly_review.run,
            CronTrigger(day_of_week="sun", hour=18, minute=0),
            id="weekly_review",
        )
        from apscheduler.triggers.interval import IntervalTrigger as _IntervalTrigger
        scheduler.add_job(
            run_uw_sweep_scan,
            _IntervalTrigger(minutes=5),
            id="uw_sweep_scan",
            misfire_grace_time=120,
        )
        scheduler.add_job(
            run_uw_intraday_scan,
            _IntervalTrigger(minutes=15),
            id="uw_intraday_scan",
            misfire_grace_time=300,
        )
        print(
            f"Scheduler started:\n"
            f"  Monthly research      : 1st Monday/month {config.BASKET_REFRESH_HOUR}:{config.BASKET_REFRESH_MINUTE:02d}  (15:00 AST)\n"
            f"  Gap + catalyst scan   : Mon-Fri {config.GAP_SCAN_HOUR}:{config.GAP_SCAN_MINUTE:02d}  (15:45 AST)\n"
            f"  Pre-market summary    : Mon-Fri {config.PREMARKET_SUMMARY_HOUR}:{config.PREMARKET_SUMMARY_MINUTE:02d}  (16:00 AST)\n"
            f"  Trading cycle (AM)    : Mon-Fri {config.RUN_HOUR}:{config.RUN_MINUTE:02d}  ET (16:50 AST)\n"
            f"  Midday stop check     : Mon-Fri {config.MIDDAY_HOUR}:{config.MIDDAY_MINUTE:02d}  (19:30 AST)\n"
            f"  Trading cycle (PM)    : Mon-Fri {config.AFTERNOON_HOUR}:{config.AFTERNOON_MINUTE:02d}  ET (22:00 AST)\n"
            f"  Close summary         : Mon-Fri {config.CLOSE_SUMMARY_HOUR}:{config.CLOSE_SUMMARY_MINUTE:02d}  ET (23:05 AST)\n"
            f"  Basket review         : Friday {config.BASKET_WEEKLY_REVIEW_HOUR}:{config.BASKET_WEEKLY_REVIEW_MINUTE:02d}  (23:30 AST)\n"
            f"  Spec research refresh : Wednesday {config.SPEC_REFRESH_HOUR}:{config.SPEC_REFRESH_MINUTE:02d} ET (bi-weekly)\n"
            f"  Weekly portfolio review: Sunday 18:00 ET (01:00 AST)\n"
            f"  UW sweep feed scan    : every 5 min (market hours only)\n"
            f"  UW basket refresh     : every 15 min (market hours only, ~5K calls/day)"
        )
        try:
            scheduler.start()
        except KeyboardInterrupt:
            print("Scheduler stopped.")
    else:
        run_cycle(dry_run=args.dry_run, force_opus=args.force_opus)



if __name__ == "__main__":
    main()
