"""
Daily reporting: pre-market briefing and close-of-day recap.
"""
import json
import os
from datetime import datetime, timezone

import database.db as db
from broker import alpaca
from signals import technical
from notifications import discord_bot as tg


def _pl_emoji(pct):
    if pct >= 5:   return "🟢"
    if pct >= 0:   return "🔵"
    if pct >= -5:  return "🟡"
    return "🔴"


def _parse_price_stop(criteria: str) -> str | None:
    """Extract price_stop value from thesis_break_criteria string."""
    if not criteria:
        return None
    for part in criteria.split("|"):
        part = part.strip()
        if part.lower().startswith("price_stop"):
            val = part.split(":", 1)[-1].strip()
            return val if val else None
    return None


def _load_macro_regime() -> dict:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".macro_regime.json")
    try:
        return json.load(open(path))
    except Exception:
        return {}


def run_premarket(dry_run: bool = False):
    """
    09:00 AM ET — Portfolio briefing for the day.

    Sections:
      1. Portfolio overview (NAV, cash %, unrealised P&L)
      2. Macro context + upcoming events
      3. Important news: macro headlines + 1 headline per held stock
      4. Holdings with exit targets (dynamic stop + tranche target)
    """
    import config

    now   = datetime.now(timezone.utc)
    label = now.strftime("%a %d %b %Y")

    try:
        portfolio = alpaca.get_portfolio()
        positions = alpaca.get_positions()
    except Exception as e:
        tg.send(f"⚠️ Pre-market: portfolio fetch failed — {e}")
        return

    equity   = portfolio.get("equity", 0)
    cash     = portfolio.get("cash", 0)
    cash_pct = cash / equity * 100 if equity else 0
    n_pos    = len(positions)

    # ── 1. Portfolio overview ────────────────────────────────────────────────
    overview_lines = [
        f"**🌅 PRE-MARKET BRIEFING — {label}**",
        "",
        f"**💼 Portfolio**",
        f"NAV: ${equity:,.0f}  |  Cash: ${cash:,.0f} ({cash_pct:.0f}%)  |  {n_pos} positions",
    ]

    # ── 2. Macro context ─────────────────────────────────────────────────────
    macro = _load_macro_regime()
    regime = macro.get("regime", {})
    raw    = macro.get("raw", {})
    events = macro.get("upcoming_events", [])

    macro_lines = ["", "**📊 Macro Context**"]
    vix_val = raw.get("vix", "?")
    fg_line = f"VIX: {vix_val}"

    inflation = regime.get("inflation_trend", "")
    cpi       = regime.get("cpi_yoy_est", "")
    y10       = regime.get("yield_10y", "")
    spread    = regime.get("yield_spread_bps", "")
    dxy       = regime.get("dxy", "")
    gold      = regime.get("gold", "")
    oil       = regime.get("oil_wti", "")
    consumer  = regime.get("consumer_mood", "")
    bias      = regime.get("rotation_bias", "")

    macro_lines.append(
        f"CPI: {cpi}% ({inflation})  |  10Y: {y10}%  |  Spread: {spread}bps  |  DXY: {dxy}"
    )
    macro_lines.append(
        f"Gold: ${gold}  |  Oil: ${oil}  |  Consumer: {consumer}  |  Rotation: {bias}"
    )

    # Macro warnings
    warnings = []
    if isinstance(cpi, (int, float)) and cpi > 5:
        warnings.append(f"⚠️ CPI {cpi}% — elevated inflation, watch rate sensitivity")
    if isinstance(spread, (int, float)) and spread < 30:
        warnings.append(f"⚠️ Yield spread {spread}bps — curve flattening, recession watch")
    if consumer in ("pessimistic", "extreme_fear"):
        warnings.append(f"⚠️ Consumer sentiment {consumer} — discretionary at risk")
    if warnings:
        macro_lines.extend(warnings)

    if events:
        macro_lines.append("📅 Upcoming: " + "  |  ".join(
            f"{e.get('event','?')} ({e.get('date','')})" for e in events[:3]
        ))

    # ── 3. News ──────────────────────────────────────────────────────────────
    # Macro headlines from research_cache momentum_news (cached in mkt_ctx on last cycle)
    # Fallback: load from research cache snippets for any held stock
    news_lines = ["", "**📰 Key News**"]

    # Stock-specific news from research cache
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "research_cache.json")
    cache_data = {}
    try:
        cache_data = json.load(open(cache_path))
    except Exception:
        pass

    stock_news = []
    for p in positions:
        sym = p["symbol"]
        if "/" in sym:
            continue
        cached = cache_data.get(sym, {})
        headlines = (cached.get("financial_data") or {}).get("finnhub", {}).get("news_headlines") or []
        if headlines:
            stock_news.append(f"**{sym}**: {headlines[0]}")

    # Macro snippets from any research cache entry
    macro_snippets = []
    for sym, d in list(cache_data.items())[:20]:
        snippets = d.get("research_snippets") or []
        for s in snippets:
            if any(kw in s.lower() for kw in ("fed", "cpi", "inflation", "rate", "gdp", "recession", "tariff", "macro")):
                macro_snippets.append(s[:150])
                break
        if len(macro_snippets) >= 3:
            break

    if macro_snippets:
        news_lines.append("*Macro:*")
        for s in macro_snippets[:3]:
            news_lines.append(f"• {s}")
    if stock_news:
        news_lines.append("*Holdings:*")
        for n in stock_news[:8]:
            news_lines.append(f"• {n}")
    if not macro_snippets and not stock_news:
        news_lines.append("No cached headlines available — run cycle to refresh.")

    # ── 4. Holdings with exit targets ────────────────────────────────────────
    holdings_lines = ["", "**📋 Holdings & Exit Targets**"]

    # Load tranche data for stop prices and targets
    tranche_map = {t["symbol"]: t for t in db.get_all_tranches()}

    for p in sorted(positions, key=lambda x: abs(x.get("unrealized_plpc", 0) or 0), reverse=True):
        sym      = p["symbol"]
        cur      = float(p.get("current_price", 0))
        entry    = float(p.get("avg_entry", 0))
        upl_pct  = float(p.get("unrealized_plpc", 0) or 0)
        upl_usd  = float(p.get("unrealized_pl", 0) or 0)
        emoji    = _pl_emoji(upl_pct)

        # Technical signals
        tech_str = ""
        try:
            is_crypto = "/" in sym
            bars  = alpaca.get_crypto_bars(sym) if is_crypto else alpaca.get_stock_bars(sym)
            tech  = technical.compute(bars)
            rsi   = tech.get("rsi")
            gc    = tech.get("golden_cross")
            dc    = tech.get("death_cross")
            sma200 = tech.get("sma200") or 0
            above200 = cur > sma200 if sma200 else None
            parts = []
            if rsi:     parts.append(f"RSI:{rsi:.0f}")
            if gc:      parts.append("GC✅")
            elif dc:    parts.append("DC⚠️")
            if above200 is True:  parts.append("↑SMA200")
            elif above200 is False: parts.append("↓SMA200")
            tech_str = "  " + " | ".join(parts) if parts else ""
        except Exception:
            pass

        # Tranche data: stop price + target allocation
        tranche    = tranche_map.get(sym)
        stop_str   = ""
        target_str = ""
        tranche_n  = ""
        if tranche:
            criteria = tranche.get("thesis_break_criteria") or ""
            stop_price = _parse_price_stop(criteria)
            if stop_price:
                stop_str = f"Stop: {stop_price}"
            target_pct = tranche.get("target_pct") or 0
            if target_pct and equity:
                target_usd = equity * target_pct / 100
                target_str = f"Target alloc: {target_pct:.1f}% (${target_usd:,.0f})"
            t_num = tranche.get("current_tranche", 1)
            tranche_n = f"T{t_num}/3"
            bucket = tranche.get("bucket", "long_term")

        # Analyst consensus
        rec_str = ""
        cached = cache_data.get(sym, {})
        recs = (cached.get("financial_data") or {}).get("finnhub", {}).get("analyst_recommendations") or {}
        if recs:
            total = sum(recs.values())
            if total:
                buys_n = recs.get("strong_buy", 0) + recs.get("buy", 0)
                rec_str = f"Analysts: {buys_n}/{total} buy"

        line = (
            f"{emoji} **{sym}**  ${cur:.2f}  |  entry ${entry:.2f}  "
            f"({upl_pct:+.1f}%  ${upl_usd:+,.0f})"
        )
        if tranche_n:
            line += f"  [{tranche_n}]"
        detail_parts = []
        if stop_str:     detail_parts.append(stop_str)
        if target_str:   detail_parts.append(target_str)
        if rec_str:      detail_parts.append(rec_str)
        detail = "  " + "  |  ".join(detail_parts) if detail_parts else ""

        holdings_lines.append(line + tech_str)
        if detail:
            holdings_lines.append(detail)

    # ── Assemble and send ────────────────────────────────────────────────────
    all_lines = overview_lines + macro_lines + news_lines + holdings_lines
    msg = "\n".join(all_lines)

    print("\n" + msg)
    db.log_summary("premarket", msg)
    tg.send(msg)


def run_close():
    """4:05 PM ET — recap all trades made today and current positions."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**📉 CLOSE RECAP — {ts}**"]

    portfolio = {"equity": 0, "cash": 0}
    try:
        portfolio = alpaca.get_portfolio()
        positions = alpaca.get_positions()
        equity = portfolio["equity"]
        cash   = portfolio["cash"]
        lines.append(f"NAV: ${equity:,.0f}  |  Cash: ${cash:,.0f} ({cash/equity*100:.0f}%)")
        lines.append(f"{len(positions)} positions open")
    except Exception as e:
        positions = []
        lines.append(f"[Portfolio fetch error: {e}]")

    trades = db.get_today_trades()
    if not trades:
        lines.append("\nNo trades today.")
    else:
        buys  = [t for t in trades if t["action"] == "BUY"]
        sells = [t for t in trades if t["action"] == "SELL"]
        if buys:
            lines.append(f"\n**Bought ({len(buys)}):**")
            for t in buys:
                lines.append(f"  {t['symbol']}  {t['allocation']:.1f}%  @${t['price']:,.2f}  conf={t['confidence']}/10")
                lines.append(f"  ↳ {t['rationale']}")
        if sells:
            lines.append(f"\n**Sold ({len(sells)}):**")
            for t in sells:
                lines.append(f"  {t['symbol']}  @${t['price']:,.2f}  conf={t['confidence']}/10")
                lines.append(f"  ↳ {t['rationale']}")

    body = "\n".join(lines)
    print("\n" + body)
    db.log_summary("close", body)
