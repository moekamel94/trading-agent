"""
Generates pre-market (9:00 AM ET) and close (4:05 PM ET) daily summaries.
Summaries are stored in the DB and served to the dashboard.
"""
from datetime import datetime, timezone

import database.db as db
from broker import alpaca
from signals import technical, sentiment, congress, insider, fundamentals
from agent import claude_agent


def _fmt_usd(v):
    return f"${v:,.2f}" if v is not None else "N/A"


def _pl_str(v):
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def run_premarket(dry_run: bool = False):
    """
    Runs 30 min before market open (9:00 AM ET).
    Scans all watchlist tickers, collects signals, asks Claude for outlook,
    and stores a plain-text summary in the DB.
    """
    from config import STOCK_WATCHLIST, CRYPTO_WATCHLIST

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"PRE-MARKET SUMMARY — {ts}",
        "=" * 60,
    ]

    try:
        portfolio = alpaca.get_portfolio()
        positions = alpaca.get_positions()
        lines.append(f"Portfolio equity : {_fmt_usd(portfolio['equity'])}")
        lines.append(f"Cash available   : {_fmt_usd(portfolio['cash'])}")
        lines.append(f"Open positions   : {len(positions)}")
        lines.append("")
    except Exception as e:
        lines.append(f"[Portfolio fetch error: {e}]")
        lines.append("")

    port_ctx = {
        "equity": portfolio.get("equity", 0),
        "cash": portfolio.get("cash", 0),
        "position_count": len(positions),
        "options_pct": 0,
        "crypto_pct": 0,
    }

    watchlist = STOCK_WATCHLIST + CRYPTO_WATCHLIST
    lines.append("SIGNAL OUTLOOK PER TICKER")
    lines.append("-" * 60)

    for symbol in watchlist:
        try:
            bars = alpaca.get_crypto_bars(symbol) if "/" in symbol else alpaca.get_stock_bars(symbol)
            tech = technical.compute(bars)
            sent = sentiment.compute(symbol)
            cong = congress.compute(symbol)
            insd = insider.compute(symbol)
            fund = fundamentals.compute(symbol)

            signals = {
                "technical": tech, "sentiment": sent,
                "congressional": cong, "insider": insd, "fundamentals": fund,
            }

            decision = claude_agent.decide(symbol, signals, port_ctx)

            rsi_str  = f"RSI {tech.get('rsi', 'N/A')}" if tech else "no data"
            sent_lbl = sent.get("label", "neutral")
            cong_lbl = cong.get("net_signal", "neutral")

            lines.append(
                f"  {symbol:<10} -> {decision['action']:<4}  conf={decision['confidence']}/10 | "
                f"{rsi_str} | sentiment={sent_lbl} | congress={cong_lbl}"
            )
            lines.append(f"    Rationale: {decision.get('rationale', '')}")
        except Exception as e:
            lines.append(f"  {symbol:<10} -> ERROR: {e}")

    body = "\n".join(lines)
    print("\n" + body)
    db.log_summary("premarket", body)


def run_close():
    """
    Runs just after market close (4:05 PM ET).
    Summarises everything traded today — what was bought/sold and why.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"CLOSE-OF-DAY SUMMARY — {ts}",
        "=" * 60,
    ]

    try:
        portfolio = alpaca.get_portfolio()
        positions = alpaca.get_positions()
        lines.append(f"Portfolio equity   : {_fmt_usd(portfolio['equity'])}")
        lines.append(f"Cash remaining     : {_fmt_usd(portfolio['cash'])}")
        lines.append(f"Open positions     : {len(positions)}")
        lines.append("")
    except Exception as e:
        lines.append(f"[Portfolio fetch error: {e}]")
        lines.append("")

    trades = db.get_today_trades()

    if not trades:
        lines.append("No trades executed today.")
    else:
        lines.append(f"TRADES TODAY ({len(trades)} total)")
        lines.append("-" * 60)
        buys  = [t for t in trades if t["action"] == "BUY"]
        sells = [t for t in trades if t["action"] == "SELL"]

        if buys:
            lines.append("\nBOUGHT:")
            for t in buys:
                lines.append(
                    f"  {t['symbol']:<10} {t['asset_type']:<8} "
                    f"qty={t['qty']:.4f}  @{_fmt_usd(t['price'])}  "
                    f"alloc={t['allocation']:.1f}%  conf={t['confidence']}/10"
                )
                lines.append(f"    Why: {t['rationale']}")

        if sells:
            lines.append("\nSOLD:")
            for t in sells:
                lines.append(
                    f"  {t['symbol']:<10} {t['asset_type']:<8} "
                    f"qty={t['qty']:.4f}  @{_fmt_usd(t['price'])}  "
                    f"conf={t['confidence']}/10"
                )
                lines.append(f"    Why: {t['rationale']}")

    lines.append("")
    lines.append("OPEN POSITIONS AT CLOSE")
    lines.append("-" * 60)

    try:
        positions = alpaca.get_positions()
        if not positions:
            lines.append("  No open positions.")
        for p in positions:
            lines.append(
                f"  {p['symbol']:<10} qty={p['qty']:.4f}  "
                f"entry={_fmt_usd(p['avg_entry'])}  "
                f"now={_fmt_usd(p['current_price'])}  "
                f"P&L={_pl_str(p['unrealized_plpc'])}"
            )
    except Exception as e:
        lines.append(f"  [Error: {e}]")

    body = "\n".join(lines)
    print("\n" + body)
    db.log_summary("close", body)
