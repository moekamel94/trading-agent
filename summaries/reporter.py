"""
Daily reporting: pre-market briefing and close-of-day recap.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta

import database.db as db
from broker import alpaca
from signals import technical
from notifications import discord_bot as discord


# ── Helpers ──────────────────────────────────────────────────────────────────

def _pl_emoji(pct):
    if pct >= 5:   return "🟢"
    if pct >= 0:   return "🔵"
    if pct >= -5:  return "🟡"
    return "🔴"


def _parse_price_stop(criteria: str) -> float | None:
    """Return numeric price stop from thesis_break_criteria, or None."""
    if not criteria:
        return None
    for part in criteria.split("|"):
        part = part.strip()
        if part.lower().startswith("price_stop"):
            raw = part.split(":", 1)[-1].strip().replace("$", "").replace(",", "")
            try:
                return float(raw)
            except ValueError:
                return None
    return None


def _load_macro_regime() -> dict:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".macro_regime.json")
    try:
        return json.load(open(path))
    except Exception:
        return {}


def _load_committee_agenda() -> list[dict]:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "basket", "committee_agenda.json")
    try:
        return json.load(open(path)).get("items", [])
    except Exception:
        return []


def _spy_returns() -> dict:
    """Return SPY 7d and 30d price returns. Uses yfinance (free, no key needed)."""
    try:
        import yfinance as yf
        hist = yf.Ticker("SPY").history(period="35d")
        if len(hist) < 2:
            return {}
        latest = hist["Close"].iloc[-1]
        w1_idx = max(0, len(hist) - 6)
        m1_idx = max(0, len(hist) - 22)
        r7d  = (latest / hist["Close"].iloc[w1_idx]  - 1) * 100
        r30d = (latest / hist["Close"].iloc[m1_idx] - 1) * 100
        return {"spy_7d": round(r7d, 1), "spy_30d": round(r30d, 1), "spy_price": round(latest, 2)}
    except Exception:
        return {}


def _portfolio_returns(equity: float) -> dict:
    """Compute portfolio 7d and 30d returns from DB snapshots."""
    try:
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trading_agent.db")
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        now = datetime.now(timezone.utc)

        def _equity_n_days_ago(days: int) -> float | None:
            cutoff = (now - timedelta(days=days)).isoformat()
            row = c.execute(
                "SELECT equity FROM snapshots WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
                (cutoff,)
            ).fetchone()
            return float(row[0]) if row else None

        eq_7d  = _equity_n_days_ago(7)
        eq_30d = _equity_n_days_ago(30)
        conn.close()
        result = {}
        if eq_7d:
            result["port_7d"]  = round((equity / eq_7d  - 1) * 100, 1)
        if eq_30d:
            result["port_30d"] = round((equity / eq_30d - 1) * 100, 1)
        return result
    except Exception:
        return {}


def _entry_date_map() -> dict[str, datetime]:
    """Return {symbol: last_buy_datetime} from trades table."""
    try:
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trading_agent.db")
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute(
            "SELECT symbol, MAX(ts) FROM trades WHERE action='BUY' GROUP BY symbol"
        )
        result = {}
        for sym, ts in c.fetchall():
            try:
                result[sym] = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
            except Exception:
                pass
        conn.close()
        return result
    except Exception:
        return {}


def _sector_concentration(positions: list, equity: float) -> list[str]:
    """Group held positions by broad theme and show % NAV per theme."""
    try:
        import config
        THEME = {
            "mega":        "AI Mega-cap",
            "large_growth":"Large Growth",
            "mid_growth":  "Mid Growth",
            "speculative": "Speculative",
        }
        # More descriptive manual theme overrides for key tickers
        MANUAL = {
            "NVDA":"AI Chips", "AMD":"AI Chips", "AVGO":"AI Chips", "TSM":"AI Chips",
            "MU":"Memory/AI",  "AMAT":"Semis Equip", "LRCX":"Semis Equip", "KLAC":"Semis Equip",
            "MSFT":"AI Cloud",  "GOOGL":"AI Cloud", "META":"AI Cloud", "AMZN":"AI Cloud",
            "ORCL":"AI Cloud",  "CRM":"AI Cloud",   "NOW":"AI Cloud",
            "CRWD":"Cybersec",  "PANW":"Cybersec",  "ZS":"Cybersec",
            "VST":"Nuclear/Power", "TLN":"Nuclear/Power", "CEG":"Nuclear/Power",
            "CCJ":"Nuclear/Power", "OKLO":"Nuclear/Power", "SMR":"Nuclear/Power",
            "GEV":"Grid/Energy",   "ETN":"Grid/Energy",
            "LMT":"Defense", "RTX":"Defense", "NOC":"Defense", "GD":"Defense",
            "AXON":"Defense", "KTOS":"Defense", "BWXT":"Defense",
            "RKLB":"Space",  "ASTS":"Space",   "LUNR":"Space",
            "IONQ":"Quantum", "RGTI":"Quantum",
            "LLY":"Healthcare", "DXCM":"Healthcare", "RXRX":"Healthcare",
        }
        buckets: dict[str, float] = {}
        pos_map = {p["symbol"]: p for p in positions}
        for sym, p in pos_map.items():
            val = abs(float(p.get("unrealized_pl", 0) or 0))
            qty = abs(float(p.get("qty", 0) or 0))
            cur = float(p.get("current_price", 0) or 0)
            mkt_val = qty * cur
            theme = MANUAL.get(sym) or THEME.get(config.TICKER_TIERS.get(sym, "mid_growth"), "Other")
            buckets[theme] = buckets.get(theme, 0) + mkt_val

        if not equity:
            return []
        lines = []
        for theme, val in sorted(buckets.items(), key=lambda x: -x[1]):
            pct = val / equity * 100
            bar = "█" * int(pct / 2)
            lines.append(f"  {theme:<18} {pct:4.1f}%  {bar}")
        return lines
    except Exception:
        return []


# ── Main pre-market report ────────────────────────────────────────────────────

def run_premarket(dry_run: bool = False):
    """09:00 AM ET — concise pre-market briefing: NAV, macro, news, holdings, plan."""
    import config

    now   = datetime.now(timezone.utc)
    label = now.strftime("%a %d %b %Y")

    try:
        portfolio = alpaca.get_portfolio()
        positions = alpaca.get_positions()
    except Exception as e:
        discord.send(f"⚠️ Pre-market: portfolio fetch failed — {e}")
        return

    equity   = portfolio.get("equity", 0)
    cash     = portfolio.get("cash", 0)
    cash_pct = cash / equity * 100 if equity else 0
    n_pos    = len(positions)

    spy         = _spy_returns()
    port_ret    = _portfolio_returns(equity)
    entry_dt    = _entry_date_map()
    tranche_map = {t["symbol"]: t for t in db.get_all_tranches()}
    macro       = _load_macro_regime()
    agenda      = _load_committee_agenda()

    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "research_cache.json")
    cache_data: dict = {}
    try:
        cache_data = json.load(open(cache_path))
    except Exception:
        pass

    # ── 1. Portfolio snapshot ─────────────────────────────────────────────────
    p7  = port_ret.get("port_7d")
    p30 = port_ret.get("port_30d")
    s7  = spy.get("spy_7d")
    s30 = spy.get("spy_30d")
    perf_parts = []
    if p7 is not None:  perf_parts.append(f"7d: {p7:+.1f}%")
    if p30 is not None: perf_parts.append(f"30d: {p30:+.1f}%")
    if s7 is not None:  perf_parts.append(f"SPY 7d: {s7:+.1f}%")
    if s30 is not None: perf_parts.append(f"SPY 30d: {s30:+.1f}%")

    mandate_note = ""
    if p30 is not None and s30 is not None:
        gap = p30 - (s30 * 2)
        mandate_note = (f"✅ +{gap:.1f}pp vs 2×SPY target" if gap >= 0
                        else f"⚠️ {abs(gap):.1f}pp behind 2×SPY (need {s30*2:.1f}%, have {p30:.1f}%)")

    lines = [
        f"**🌅 PRE-MARKET — {label}**",
        f"NAV ${equity:,.0f}  |  Cash ${cash:,.0f} ({cash_pct:.0f}%)  |  {n_pos} positions",
    ]
    if perf_parts:
        lines.append("  ".join(perf_parts))
    if mandate_note:
        lines.append(mandate_note)

    # ── 2. Macro snapshot ─────────────────────────────────────────────────────
    regime   = macro.get("regime", {})
    raw_mac  = macro.get("raw", {})
    events   = macro.get("upcoming_events", [])
    cpi      = regime.get("cpi_yoy_est", "?")
    y10      = regime.get("yield_10y", "?")
    spread   = regime.get("yield_spread_bps", "?")
    vix_v    = raw_mac.get("vix", "?")
    consumer = regime.get("consumer_mood", "")
    bias     = regime.get("rotation_bias", "")
    inflation = regime.get("inflation_trend", "")

    lines += [
        "",
        f"**📈 Macro**  VIX {vix_v}  |  10Y {y10}%  |  CPI {cpi}% ({inflation})  |  Spread {spread}bps  |  Bias: {bias}",
    ]
    macro_alerts = []
    if isinstance(cpi, (int, float)) and cpi > 5:
        macro_alerts.append(f"CPI {cpi}% elevated — rate-sensitive names at risk")
    if isinstance(spread, (int, float)) and spread < 30:
        macro_alerts.append(f"Yield spread {spread}bps — curve flat, recession signal")
    if consumer in ("pessimistic", "extreme_fear"):
        macro_alerts.append(f"Consumer {consumer} — cut discretionary/consumer exposure")
    for a in macro_alerts:
        lines.append(f"  ⚠️ {a}")
    if events:
        lines.append("  📅 " + "  |  ".join(
            f"{e.get('event','?')} {e.get('date','')}" for e in events[:3]
        ))

    # ── Regime shift forecast — early warning to pre-position ────────────────
    fc = macro.get("regime_forecast", {})
    if fc.get("likely_next_regime") and fc.get("transition_probability", 0) >= 30:
        _nxt  = fc["likely_next_regime"].upper().replace("_", " ")
        _prob = fc["transition_probability"]
        _hrz  = fc.get("horizon_weeks", "?")
        _pre  = fc.get("pre_position_sectors", [])
        lines += ["", f"**⚡ Regime Shift Signal ({_prob}% → {_nxt} in {_hrz})**"]
        for _sig in fc.get("leading_signals", [])[:3]:
            lines.append(f"  • {_sig}")
        if _pre:
            lines.append(f"  → Start accumulating: **{', '.join(_pre[:4])}** before shift confirms")

    # ── 3. Today's action plan (committee directives + planned buys) ──────────
    high_items = [a for a in agenda if a.get("priority") == "high"]
    med_items  = [a for a in agenda if a.get("priority") == "medium"]
    if high_items or med_items:
        lines += ["", "**🎯 Committee Agenda**  _(no action needed from you — committee resolves each item in the cycle)_"]
        for item in (high_items + med_items)[:3]:
            pri = "HIGH" if item in high_items else "MED"
            lines.append(f"  [{pri}] {item.get('title','')}")
            ft = item.get("force_review_tickers", [])
            if ft:
                lines.append(f"    → Tickers: {', '.join(ft)}")

    # ── 4. Key news for holdings + planned buy tickers ────────────────────────
    held_syms    = {p["symbol"] for p in positions if "/" not in p["symbol"]}
    planned_syms = {t for item in agenda for t in item.get("force_review_tickers", [])}
    watch_syms   = held_syms | planned_syms

    news_lines, macro_snippets = [], []
    for sym in sorted(watch_syms):
        cached = cache_data.get(sym, {})
        fh = (cached.get("financial_data") or {}).get("finnhub") or {}
        headlines = fh.get("news_headlines") or []
        if headlines:
            news_lines.append(f"  **{sym}**: {headlines[0][:120]}")

    # 1-2 macro headlines from any cached ticker
    for sym, d in list(cache_data.items())[:25]:
        for s in (d.get("research_snippets") or []):
            if any(kw in s.lower() for kw in ("fed", "cpi", "inflation", "rate cut", "gdp", "tariff", "recession")):
                macro_snippets.append(s[:150])
                break
        if len(macro_snippets) >= 2:
            break

    if macro_snippets or news_lines:
        lines += ["", "**📰 News**"]
        for s in macro_snippets:
            lines.append(f"  🌐 {s}")
        lines.extend(news_lines[:6])

    # ── 5. Holdings — P&L, urgent flags, stop/target ─────────────────────────
    lines += ["", "**📋 Holdings**"]

    for p in sorted(positions, key=lambda x: abs(float(x.get("unrealized_plpc", 0) or 0)), reverse=True):
        sym     = p["symbol"]
        cur     = float(p.get("current_price", 0) or 0)
        upl_pct = float(p.get("unrealized_plpc", 0) or 0)
        upl_usd = float(p.get("unrealized_pl", 0) or 0)
        qty     = float(p.get("qty", 0) or 0)
        mkt_val = qty * cur
        emoji   = _pl_emoji(upl_pct)

        edt = entry_dt.get(sym)
        days_held = f"{(now - edt).days}d" if edt else ""

        cached  = cache_data.get(sym, {})
        # Latest news headline for this holding
        fh = (cached.get("financial_data") or {}).get("finnhub") or {}
        top_news = (fh.get("news_headlines") or [""])[0]

        # Earnings alert — recompute days from stored date string so it's always current
        earn_str = ""
        ed = (cached.get("earnings_data") or {})
        earnings_date_str = ed.get("earnings_date", "")
        dte = None
        if earnings_date_str:
            try:
                _ed = datetime.strptime(earnings_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                dte = (_ed.date() - now.date()).days
            except Exception:
                dte = ed.get("days_to_earnings")
        if dte is not None and dte >= 0:
            _earn_emoji = "🚨" if dte <= 14 else "📅"
            earn_str = f"  {_earn_emoji} Earnings in {dte}d ({earnings_date_str})"
        elif earnings_date_str:
            earn_str = f"  📅 Earnings: {earnings_date_str}"

        # Stop + target from tranche
        tranche   = tranche_map.get(sym)
        stop_str  = upside_str = t_label = ""
        if tranche:
            stop_val = _parse_price_stop(tranche.get("thesis_break_criteria") or "")
            if stop_val and cur:
                dist = (cur - stop_val) / cur * 100
                warn = "🚨" if dist < 5 else "🛑"
                stop_str = f"  {warn} Stop ${stop_val:.0f} ({dist:.0f}% away)"
            pt = tranche.get("price_target")
            if pt and cur:
                upside_pct = (pt - cur) / cur * 100
                upside_str = f"  🎯 Target ${pt:.0f} ({upside_pct:+.0f}%)"
            elif cur:
                upside_str = "  🎯 Target: committee sets in next cycle"
            t_label = f" T{tranche.get('current_tranche',1)}/3"

        meta = "  ".join(x for x in [days_held and f"held {days_held}", t_label.strip()] if x)
        lines.append(
            f"{emoji} **{sym}**  ${cur:.2f}  {upl_pct:+.1f}% (${upl_usd:+,.0f})  ${mkt_val:,.0f}"
            + (f"  [{meta}]" if meta else "")
        )
        if earn_str:   lines.append(earn_str)
        if stop_str:   lines.append(stop_str)
        if upside_str: lines.append(upside_str)

    # ── Assemble and send ────────────────────────────────────────────────────
    msg = "\n".join(lines)
    print("\n" + msg)
    db.log_summary("premarket", msg)
    if not dry_run:
        discord.send(msg)


# ── Close-of-day ─────────────────────────────────────────────────────────────

def run_close():
    """4:05 PM ET — close recap."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**📉 CLOSE RECAP — {ts}**"]

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
    discord.send(body)

    # EOD health digest — appended to close message
    try:
        from monitoring import health
        health.send_eod_digest()
    except Exception as e:
        print(f"  [Health] EOD digest failed: {e}")
