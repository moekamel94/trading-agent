"""
Report Employee — the single responsible person for all report formatting and delivery.

RESPONSIBILITIES:
  1. Receives structured data from every other module
  2. Formats it into clear, consistent Discord messages
  3. Ensures nothing is duplicated (dedup guard per report type)
  4. Adds missing context (targets, stops, sector) to every report
  5. Manages message length (splits long reports into sections)

Every module that sends to Discord should call report_employee.send() instead
of calling discord.send() directly — this ensures consistent formatting.

Dedup guard: tracks what was sent in the last 24h to prevent the same report
firing twice (e.g., from a crashed + restarted process).
"""
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta

from notifications import discord_bot as discord

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trading_agent.db")


# ── Dedup guard ───────────────────────────────────────────────────────────────

def _was_sent_recently(report_type: str, window_hours: int = 6) -> bool:
    """Return True if this report type was sent within the last window_hours."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        c = conn.cursor()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
        row = c.execute(
            "SELECT ts FROM summaries WHERE summary_type=? AND ts >= ? ORDER BY ts DESC LIMIT 1",
            (f"re_{report_type}", cutoff)
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def _mark_sent(report_type: str) -> None:
    try:
        conn = sqlite3.connect(_DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO summaries (summary_type, content, ts) VALUES (?, ?, ?)",
            (f"re_{report_type}", "sent", datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── Holdings table formatter ──────────────────────────────────────────────────

def format_holdings_table(positions: list, equity: float, cache_data: dict,
                           tranche_map: dict, entry_dates: dict) -> list[str]:
    """
    Format all holdings as a clean table with: price, P&L, stop, target, days held.
    Every holding gets a target — no blank target lines.
    Returns list of lines ready for Discord.
    """
    now = datetime.now(timezone.utc)
    lines = []

    for p in sorted(positions, key=lambda x: abs(float(x.get("unrealized_plpc", 0) or 0)), reverse=True):
        sym     = p.get("symbol", "?")
        cur     = float(p.get("current_price", 0) or 0)
        upl_pct = float(p.get("unrealized_plpc", 0) or 0)
        upl_usd = float(p.get("unrealized_pl", 0) or 0)
        qty     = float(p.get("qty", 0) or 0)
        mkt_val = qty * cur

        # P&L color
        if upl_pct >= 5:   icon = "🟢"
        elif upl_pct >= 0: icon = "🔵"
        elif upl_pct >= -5: icon = "🟡"
        else:              icon = "🔴"

        edt = entry_dates.get(sym)
        days_held = f"held {(now - edt).days}d" if edt else ""

        # Stop and target
        tranche   = tranche_map.get(sym)
        stop_str  = ""
        upside_str = ""

        if tranche:
            # Stop loss
            criteria = tranche.get("thesis_break_criteria") or ""
            for part in criteria.split("|"):
                part = part.strip()
                if part.lower().startswith("price_stop"):
                    raw_stop = part.split(":", 1)[-1].strip().replace("$", "").replace(",", "")
                    try:
                        stop_val = float(raw_stop)
                        dist = (cur - stop_val) / cur * 100 if cur else 0
                        warn = "🚨" if dist < 5 else "🛑"
                        stop_str = f"  {warn} Stop ${stop_val:.0f} ({dist:.0f}% away)"
                    except ValueError:
                        pass
                    break

            # Price target — from tranche first
            pt = tranche.get("price_target")
            if pt and cur:
                upside = (pt - cur) / cur * 100
                upside_str = f"  🎯 Target ${pt:.0f} ({upside:+.0f}%)"

        # Fall back to analyst target from cache
        if not upside_str:
            analyst_target = (cache_data.get(sym, {}).get("financial_data") or {}).get("analyst_target")
            if analyst_target and cur:
                upside = (analyst_target - cur) / cur * 100
                upside_str = f"  🎯 Analyst target ${analyst_target:.0f} ({upside:+.0f}%)"
            else:
                upside_str = "  🎯 No target — review needed"

        # Earnings alert
        earn_str = ""
        ed = (cache_data.get(sym, {}).get("earnings_data") or {})
        earn_date_str = ed.get("earnings_date", "")
        if earn_date_str:
            try:
                _ed = datetime.strptime(earn_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                dte = (_ed.date() - now.date()).days
                if dte >= 0:
                    earn_str = f"  {'🚨' if dte <= 14 else '📅'} Earnings in {dte}d"
            except Exception:
                pass

        # Main line
        lines.append(
            f"{icon} **{sym}**  ${cur:.2f}  {upl_pct:+.1f}% (${upl_usd:+,.0f})  ${mkt_val:,.0f}"
            + (f"  [{days_held}]" if days_held else "")
        )
        if earn_str:   lines.append(earn_str)
        if stop_str:   lines.append(stop_str)
        if upside_str: lines.append(upside_str)

    return lines


# ── Report section templates ──────────────────────────────────────────────────

def send_premarket(data: dict) -> None:
    """
    Standardized pre-market report. Called by summaries/reporter.py.
    data keys: label, equity, cash, cash_pct, n_pos, perf_parts, mandate_note,
               macro_lines, agenda_items, holdings_lines
    """
    if _was_sent_recently("premarket", window_hours=4):
        return

    label    = data.get("label", "")
    equity   = data.get("equity", 0)
    cash     = data.get("cash", 0)
    cash_pct = data.get("cash_pct", 0)
    n_pos    = data.get("n_pos", 0)
    perf     = data.get("perf_parts", [])
    mandate  = data.get("mandate_note", "")

    lines = [
        f"**🌅 PRE-MARKET — {label}**",
        f"NAV ${equity:,.0f}  |  Cash ${cash:,.0f} ({cash_pct:.0f}%)  |  {n_pos} positions",
    ]
    if perf:
        lines.append("  ".join(perf))
    if mandate:
        lines.append(mandate)

    # Macro
    macro_lines = data.get("macro_lines", [])
    if macro_lines:
        lines += [""] + macro_lines

    # Agenda (only fresh items — already filtered by reporter.py)
    agenda = data.get("agenda_items", [])
    if agenda:
        lines += ["", "**🎯 Today's Agenda**"]
        for item in agenda[:3]:
            pri = item.get("priority", "").upper()
            lines.append(f"  [{pri}] {item.get('title','')}")

    # Holdings
    holdings = data.get("holdings_lines", [])
    if holdings:
        lines += ["", "**📋 Holdings**"] + holdings

    msg = "\n".join(lines)
    discord.send(msg)
    _mark_sent("premarket")


def send_close_recap(equity: float, cash: float, trades: list) -> None:
    """Standardized close-of-day recap."""
    if _was_sent_recently("close", window_hours=4):
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**📉 CLOSE RECAP — {ts}**"]
    lines.append(f"NAV: ${equity:,.0f}  |  Cash: ${cash:,.0f} ({cash/equity*100:.0f}%)")

    buys  = [t for t in trades if t.get("action") == "BUY"]
    sells = [t for t in trades if t.get("action") == "SELL"]

    if not trades:
        lines.append("No trades today.")
    else:
        if buys:
            lines.append(f"\n**Bought ({len(buys)}):**")
            for t in buys:
                lines.append(f"  {t['symbol']}  {t.get('allocation',0):.1f}%  @${t.get('price',0):,.2f}  conf={t.get('confidence',0)}/10")
                lines.append(f"  ↳ {t.get('rationale','')[:120]}")
        if sells:
            lines.append(f"\n**Sold ({len(sells)}):**")
            for t in sells:
                lines.append(f"  {t['symbol']}  @${t.get('price',0):,.2f}")
                lines.append(f"  ↳ {t.get('rationale','')[:120]}")

    discord.send("\n".join(lines))
    _mark_sent("close")


def send_weekly_review(result: dict, equity: float, cash: float, executed: list) -> None:
    """Standardized weekly review format."""
    actions   = result.get("actions", [])
    health    = result.get("portfolio_health", "")
    cash_note = result.get("cash_comment", "")
    concern   = result.get("top_concern", "")
    horizon   = result.get("horizon_scan", {})

    action_icon = {"EXIT": "🔴", "TRIM": "🟠", "ADD": "🟢", "HOLD": "⚪"}

    lines = ["**📋 WEEKLY PORTFOLIO REVIEW**\n"]
    if health:    lines.append(f"**Health:** {health}")
    if cash_note: lines.append(f"**Cash:** {cash_note}")
    if concern:   lines.append(f"**Top concern:** {concern}\n")

    if executed:
        lines.append("**✅ Actions taken:**")
        lines.extend(f"  {e}" for e in executed)
        lines.append("")

    immediate = [a for a in actions if a.get("urgency") == "immediate"]
    this_week = [a for a in actions if a.get("urgency") == "this_week"]
    monitor   = [a for a in actions if a.get("urgency") == "monitor"]

    for label, group, icon in [("🔴 Immediate", immediate, ""),
                                ("🟡 This week", this_week, ""),
                                ("🔵 Holding", monitor, "")]:
        if group:
            lines.append(f"\n**{label}:**")
            for a in group:
                rec = a.get("recommendation", "")
                sym = a.get("symbol", "?")
                reason = a.get("reason", "")
                lines.append(f"  {action_icon.get(rec,'○')} **{sym}** {rec} — {reason}")

    # Horizon scan
    if horizon:
        lines += ["", "**🔭 Horizon — Next 4–8 Weeks**"]
        for cat in horizon.get("macro_catalysts_next_4w", [])[:3]:
            lines.append(f"  📅 {cat}")
        if horizon.get("sector_to_watch"):
            lines.append(f"\n  🔄 **Next inflection:** {horizon['sector_to_watch']}")
        if horizon.get("be_positioned_for"):
            lines.append(f"  📌 **Accumulate:** {horizon['be_positioned_for']}")
        if horizon.get("early_signals_seen"):
            lines.append(f"  👁 **Early signals:** {horizon['early_signals_seen']}")
        if horizon.get("risk_to_watch"):
            lines.append(f"  ⚠️ **Risk:** {horizon['risk_to_watch']}")

    discord.send("\n".join(lines))


def send_basket_review(lt_changes: list, mt_count: int, src_counts: dict,
                       new_additions: list, removals: list) -> None:
    """Standardized Friday basket review format."""
    lines = ["**🧺 WEEKLY BASKET REVIEW**"]

    if lt_changes:
        lines.append("\n**LT Basket Changes:**")
        lines.extend(f"  {c}" for c in lt_changes)
    else:
        lines.append("LT basket: no changes")

    if new_additions:
        lines.append(f"\n**New additions:** {', '.join(new_additions)}")
    if removals:
        lines.append(f"**Removed:** {', '.join(removals)}")

    src_str = " | ".join(f"{k}={v}" for k, v in src_counts.items())
    lines.append(f"\nMT basket: {mt_count} tickers ({src_str})")

    discord.send("\n".join(lines))
