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
from notifications import discord_bot as tg


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
    """09:00 AM ET — full portfolio briefing."""
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

    # Load supporting data
    spy      = _spy_returns()
    port_ret = _portfolio_returns(equity)
    entry_dt = _entry_date_map()
    tranche_map = {t["symbol"]: t for t in db.get_all_tranches()}
    macro    = _load_macro_regime()
    agenda   = _load_committee_agenda()

    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "research_cache.json")
    cache_data: dict = {}
    try:
        cache_data = json.load(open(cache_path))
    except Exception:
        pass

    # ── 1. Portfolio overview + benchmark ────────────────────────────────────
    lines = [
        f"**🌅 PRE-MARKET BRIEFING — {label}**",
        "",
        "**💼 Portfolio vs Benchmark**",
        f"NAV: ${equity:,.0f}  |  Cash: ${cash:,.0f} ({cash_pct:.0f}%)  |  {n_pos} positions",
    ]

    # Performance table
    p7  = port_ret.get("port_7d")
    p30 = port_ret.get("port_30d")
    s7  = spy.get("spy_7d")
    s30 = spy.get("spy_30d")
    if p7 is not None or s7 is not None:
        head = f"{'':12}{'7-day':>8}{'30-day':>8}"
        port_row = f"{'Portfolio':12}{f'{p7:+.1f}%' if p7 is not None else 'N/A':>8}{f'{p30:+.1f}%' if p30 is not None else 'N/A':>8}"
        spy_row  = f"{'SPY':12}{f'{s7:+.1f}%' if s7 is not None else 'N/A':>8}{f'{s30:+.1f}%' if s30 is not None else 'N/A':>8}"
        # Mandate check: target is 2× SPY
        mandate_note = ""
        if p30 is not None and s30 is not None:
            target_30d = s30 * 2
            gap = p30 - target_30d
            if gap >= 0:
                mandate_note = f"✅ Ahead of 2×SPY target by {gap:+.1f}pp"
            else:
                mandate_note = f"⚠️ Behind 2×SPY target by {abs(gap):.1f}pp (need {target_30d:.1f}%, have {p30:.1f}%)"
        lines += [head, port_row, spy_row]
        if mandate_note:
            lines.append(mandate_note)

    # Sector concentration
    sect_lines = _sector_concentration(positions, equity)
    if sect_lines:
        lines.append("")
        lines.append("**📊 Sector Concentration**")
        lines.extend(sect_lines)

    # ── 2. Macro context ─────────────────────────────────────────────────────
    regime = macro.get("regime", {})
    raw    = macro.get("raw", {})
    events = macro.get("upcoming_events", [])
    cpi    = regime.get("cpi_yoy_est", "")
    y10    = regime.get("yield_10y", "")
    spread = regime.get("yield_spread_bps", "")
    dxy    = regime.get("dxy", "")
    gold   = regime.get("gold", "")
    oil    = regime.get("oil_wti", "")
    vix_v  = raw.get("vix", "?")
    consumer = regime.get("consumer_mood", "")
    bias   = regime.get("rotation_bias", "")
    inflation = regime.get("inflation_trend", "")

    lines += [
        "",
        "**📈 Macro Context**",
        f"VIX: {vix_v}  |  CPI: {cpi}% ({inflation})  |  10Y: {y10}%  |  Spread: {spread}bps",
        f"DXY: {dxy}  |  Gold: ${gold}  |  Oil: ${oil}  |  Consumer: {consumer}  |  Bias: {bias}",
    ]
    if isinstance(cpi, (int, float)) and cpi > 5:
        lines.append(f"⚠️ CPI {cpi}% — rate sensitivity elevated across portfolio")
    if isinstance(spread, (int, float)) and spread < 30:
        lines.append(f"⚠️ Yield spread {spread}bps — flattening curve, recession risk rising")
    if consumer in ("pessimistic", "extreme_fear"):
        lines.append(f"⚠️ Consumer sentiment {consumer} — discretionary and consumer-facing exposure at risk")
    if events:
        lines.append("📅 " + "  |  ".join(
            f"{e.get('event','?')} {e.get('date','')}" for e in events[:4]
        ))

    # ── 3. Cash deployment plan ───────────────────────────────────────────────
    deploy_items = [a for a in agenda if a.get("priority") in ("high", "medium")]
    if deploy_items:
        lines += ["", "**🎯 Open Committee Directives**"]
        for item in deploy_items[:3]:
            pri = item.get("priority", "").upper()
            lines.append(f"[{pri}] {item.get('title','')}")

    # ── 4. Key news ───────────────────────────────────────────────────────────
    stock_news, macro_snippets = [], []
    for p in positions:
        sym = p["symbol"]
        if "/" in sym:
            continue
        fh = (cache_data.get(sym, {}).get("financial_data") or {}).get("finnhub") or {}
        headlines = fh.get("news_headlines") or []
        if headlines:
            stock_news.append(f"**{sym}**: {headlines[0]}")

    for sym, d in list(cache_data.items())[:30]:
        for s in (d.get("research_snippets") or []):
            if any(kw in s.lower() for kw in ("fed", "cpi", "inflation", "rate", "gdp", "recession", "tariff", "macro")):
                macro_snippets.append(s[:160])
                break
        if len(macro_snippets) >= 3:
            break

    lines += ["", "**📰 Key News**"]
    if macro_snippets:
        lines.append("*Macro:*")
        lines.extend(f"• {s}" for s in macro_snippets[:3])
    if stock_news:
        lines.append("*Holdings:*")
        lines.extend(f"• {n}" for n in stock_news[:8])
    if not macro_snippets and not stock_news:
        lines.append("No cached headlines — will refresh on next cycle.")

    # ── 5. Holdings with full context ────────────────────────────────────────
    lines += ["", "**📋 Holdings**"]

    for p in sorted(positions, key=lambda x: abs(x.get("unrealized_plpc", 0) or 0), reverse=True):
        sym     = p["symbol"]
        cur     = float(p.get("current_price", 0) or 0)
        entry   = float(p.get("avg_entry", 0) or 0)
        upl_pct = float(p.get("unrealized_plpc", 0) or 0)
        upl_usd = float(p.get("unrealized_pl", 0) or 0)
        qty     = float(p.get("qty", 0) or 0)
        mkt_val = qty * cur
        emoji   = _pl_emoji(upl_pct)

        # Days held
        days_held = ""
        edt = entry_dt.get(sym)
        if edt:
            days = (now - edt).days
            days_held = f"{days}d"

        # Technical signals
        rsi_str = gc_str = sma_str = range_str = ""
        try:
            is_crypto = "/" in sym
            bars  = alpaca.get_crypto_bars(sym) if is_crypto else alpaca.get_stock_bars(sym)
            tech  = technical.compute(bars)
            rsi   = tech.get("rsi")
            gc    = tech.get("golden_cross")
            dc    = tech.get("death_cross")
            sma200 = tech.get("sma200") or 0
            if rsi:    rsi_str = f"RSI:{rsi:.0f}"
            if gc:     gc_str  = "GC✅"
            elif dc:   gc_str  = "DC⚠️"
            if sma200: sma_str = "↑SMA200" if cur > sma200 else "↓SMA200"
        except Exception:
            pass

        # 52-week range
        cached  = cache_data.get(sym, {})
        yah     = (cached.get("financial_data") or {}).get("yahoo") or {}
        w52h    = yah.get("52w_high")
        w52l    = yah.get("52w_low")
        if w52h and w52l and cur:
            rng_pct = (cur - w52l) / (w52h - w52l) * 100 if w52h != w52l else 0
            range_str = f"52wk:{rng_pct:.0f}%"

        # Earnings countdown
        earn_str = ""
        ed = (cached.get("earnings_data") or {})
        dte = ed.get("days_to_earnings")
        earn_date = ed.get("earnings_date", "")
        if dte is not None and 0 <= dte <= 30:
            flag = "🚨" if dte <= 5 else "📅"
            earn_str = f"{flag}Earnings in {dte}d ({earn_date})"

        # Tranche + stop + upside target
        tranche = tranche_map.get(sym)
        stop_val = None
        stop_str = upside_str = tranche_str = ""
        if tranche:
            stop_val = _parse_price_stop(tranche.get("thesis_break_criteria") or "")
            if stop_val and cur:
                dist = (cur - stop_val) / cur * 100
                stop_str = f"🛑 Stop: ${stop_val:.0f} ({dist:.0f}% down)"

            pt = tranche.get("price_target")
            pt_basis = tranche.get("price_target_basis") or ""
            if pt and cur:
                upside_pct = (pt - cur) / cur * 100
                arrow = "🎯" if upside_pct > 0 else "⚠️"
                upside_str = f"{arrow} Target: ${pt:.0f} ({upside_pct:+.0f}%)"
                if pt_basis:
                    upside_str += f"  [{pt_basis[:60]}]"

            t_num = tranche.get("current_tranche", 1)
            tranche_str = f"T{t_num}/3"

        # Analyst consensus
        recs = (cached.get("financial_data") or {}).get("finnhub", {}).get("analyst_recommendations") or {}
        rec_str = ""
        if recs:
            total = sum(recs.values())
            if total:
                buys_n = recs.get("strong_buy", 0) + recs.get("buy", 0)
                rec_str = f"Analysts:{buys_n}/{total}B"

        # Build lines
        tech_parts = [x for x in [rsi_str, gc_str, sma_str, range_str] if x]
        meta_parts = [x for x in [days_held and f"held {days_held}", tranche_str, rec_str] if x]

        header = (
            f"{emoji} **{sym}**  ${cur:.2f}  {upl_pct:+.1f}% (${upl_usd:+,.0f})"
            f"  mkt ${mkt_val:,.0f}"
        )
        lines.append(header)
        if tech_parts or meta_parts:
            lines.append("  " + "  |  ".join(tech_parts + meta_parts))
        if stop_str:
            lines.append(f"  {stop_str}")
        if upside_str:
            lines.append(f"  {upside_str}")
        if earn_str:
            lines.append(f"  {earn_str}")

    # ── Assemble and send ────────────────────────────────────────────────────
    msg = "\n".join(lines)
    print("\n" + msg)
    db.log_summary("premarket", msg)
    tg.send(msg)


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
