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
    """09:00 AM ET — concise pre-market briefing."""
    now   = datetime.now(timezone.utc)
    label = now.strftime("%a %d %b %Y")

    try:
        portfolio = alpaca.get_portfolio()
        positions = alpaca.get_positions()
    except Exception as e:
        discord.send(f"\u26a0\ufe0f Pre-market: portfolio fetch failed \u2014 {e}")
        return

    equity   = portfolio.get("equity", 0)
    cash   = portfolio.get("cash", 0)
    cash_pct = cash / equity * 100 if equity else 0
    n_pos    = len(positions)

    spy      = _spy_returns()
    port_ret = _portfolio_returns(equity)
    entry_dt = _entry_date_map()
    macro    = _load_macro_regime()

    p7  = port_ret.get("port_7d")
    p30 = port_ret.get("port_30d")
    s7  = spy.get("spy_7d")
    s30 = spy.get("spy_30d")

    mandate_note = ""
    if p30 is not None and s30 is not None:
        gap = p30 - (s30 * 2)
        mandate_note = (f"\u2705 +{gap:.1f}pp vs 2\xd7SPY" if gap >= 0
                        else f"\u26a0\ufe0f {abs(gap):.1f}pp behind 2\xd7SPY target")

    out = [
        f"**\U0001f305 PRE-MARKET \u2014 {label}**",
        f"NAV ${equity:,.0f}  |  Cash {cash_pct:.0f}%  |  {n_pos} positions",
    ]

    perf = []
    if p7 is not None and s7 is not None:
        perf.append(f"7d: Kimmy {p7:+.1f}% vs SPY {s7:+.1f}%")
    if p30 is not None and s30 is not None:
        perf.append(f"30d: Kimmy {p30:+.1f}% vs SPY {s30:+.1f}%")
    if perf:
        out.append("  ".join(perf))
    if mandate_note:
        out.append(mandate_note)

    # Macro — one line
    regime  = macro.get("regime", {}) if isinstance(macro.get("regime"), dict) else {}
    raw_mac = macro.get("raw", {})
    vix_v   = raw_mac.get("vix", "?")
    bias    = regime.get("rotation_bias", "?")
    cpi     = regime.get("cpi_yoy_est", 0)
    spread  = regime.get("yield_spread_bps", 100)
    events  = macro.get("upcoming_events", [])
    event_str = " | ".join(e.get("event","") for e in events[:2]) if events else ""
    out.append("")
    out.append(f"**\U0001f4c8 Macro** VIX {vix_v} | Bias: {bias}" +
               (f" | \U0001f4c5 {event_str}" if event_str else ""))
    if isinstance(cpi, (int, float)) and cpi > 5:
        out.append(f"  \u26a0\ufe0f CPI {cpi}% elevated \u2014 watch rate-sensitive positions")
    if isinstance(spread, (int, float)) and spread < 30:
        out.append(f"  \u26a0\ufe0f Yield spread {spread}bps \u2014 recession signal")

    # Regi shift warning
    fc = macro.get("regime_forecast", {})
    if fc.get("likely_next_regime") and fc.get("transition_probability", 0) >= 30:
        nxt  = fc["likely_next_regime"].upper().replace("_", " ")
        prob = fc["transition_probability"]
        hrz  = fc.get("horizon_weeks", "?")
        pre  = fc.get("pre_position_sectors", [])
        out += ["", f"**\u26a1 Regime Shift ({prob}% \u2192 {nxt} in {hrz}w)**"]
        if pre:
            out.append(f"  \u2192 Pre-position: {', '.join(pre[:4])}")

    # Holdings sorted by P&L
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "research_cache.json")
    cache_data = {}
    try:
        cache_data = json.load(open(cache_path))
    except Exception:
        pass

    out.append("")
    out.append("**\U0001f4cb Positions** (sorted by P&L)")
    sorted_pos = sorted(positions, key=lambda x: float(x.get("unrealized_plpc", 0) or 0), reverse=True)
    for p in sorted_pos:
        sym     = p["symbol"]
        cur     = float(p.get("current_price", 0) or 0)
        upl_pct = float(p.get("unrealized_plpc", 0) or 0)
        upl_usd = float(p.get("unrealized_pl", 0) or 0)
        emoji   = _pl_emoji(upl_pct)
        edt     = entry_dt.get(sym)
        days_held = f" [{(now-edt).days}d]" if edt else ""

        # Earnings warning
        cached = cache_data.get(sym, {})
        ed = (cached.get("earnings_data") or {})
        earn_date = ed.get("earnings_date", "")
        earn_tag = ""
        if earn_date:
            try:
                _ed = datetime.strptime(earn_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                dte = (_ed.date() - now.date()).days
                if 0 <= dte <= 14:
                    earn_tag = f" \U0001f6a8E{dte}d"
            except Exception:
                pass

        out.append(f"{emoji} **{sym}** ${cur:.2f} {upl_pct:+.1f}% (${upl_usd:+,.0f}){days_held}{earn_tag}")

    msg = "\n".join(out)
    print("\n" + msg)
    db.log_summary("premarket", msg)
    if not dry_run:
        discord.send(msg)



def run_close():
    """4:05 PM ET — close recap."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"**\U0001f4c9 CLOSE RECAP \u2014 {ts}**"]

    try:
        portfolio = alpaca.get_portfolio()
        positions = alpaca.get_positions()
        equity = portfolio["equity"]
        cash   = portfolio["cash"]
        n_pos  = len(positions)
        out.append(f"NAV: ${equity:,.0f}  |  Cash: ${cash:,.0f} ({cash/equity*100:.0f}%)  |  {n_pos} positions")
    except Exception as e:
        positions = []
        equity = 0
        out.append(f"[Portfolio error: {e}]")

    # SPY alpha
    try:
        spy      = _spy_returns()
        port_ret = _portfolio_returns(equity)
        p7 = port_ret.get("port_7d")
        s7 = spy.get("spy_7d")
        if p7 is not None and s7 is not None:
            alpha = round(p7 - s7, 1)
            sign  = "+" if alpha >= 0 else ""
            out.append(f"7d alpha: Kimmy {p7:+.1f}% vs SPY {s7:+.1f}% ({sign}{aha}pp)")
    except Exception:
        pass

    # Trades today
    trades = db.get_today_trades()
    if not trades:
        out.append("\nNo trades today.")
    else:
        buys  = [t for t in trades if t["action"] == "BUY"]
        sells = [t for t in trades if t["action"] == "SELL"]
        if buys:
            out.append(f"\n**Bought ({len(buys)}):**")
            for t in buys:
                out.append(f"  {t['symbol']} {t.get('allocation',0):.1f}% @${t['price']:,.2f} conf={t.get('confidence','?')}/10")
                if t.get('rationale'):
                    out.append(f"  \u21b3 {t['rationale'][:120]}")
        if sells:
            out.append(f"\n**Sold ({len(sells)}):**")
            for t in sells:
                out.append(f"  {t['symbol']} @${t['price']:,.2f} conf={t.get('confidence','?')}/10")
                if t.get('rationale'):
                    out.append(f"  \u21b3 {t['rationale'][:120]}")

    # Top 5 positions by P&L
    if positions:
        sorted_pos = sorted(positions, key=lambda x: float(x.get("unrealized_plpc", 0) or 0), reverse=True)
        out.append("\n**Top positions:**")
        for p in sorted_pos[:5]:
            sym     = p["symbol"]
            cur     = float(p.get("current_price", 0) or 0)
            upl_pct = float(p.get("unrealized_plpc", 0) or 0)
            upl_usd = float(p.get("unrealized_pl", 0) or 0)
            emoji   = _pl_emoji(upl_pct)
            out.append(f"  {emoji} {sym} ${cur:.2f} {upl_pct:+.1f}% (${upl_usd:+,.0f})")

    body = "\n".join(out)
    print("\n" + body)
    db.log_summary("close", body)
    discord.send(body)

    try:
        from monitoring import health
        health.send_eod_digest()
    except Exception as e:
        print(f"  [Health] EOD digest failed: {e}")


