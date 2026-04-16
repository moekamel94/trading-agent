"""
Generates pre-market (9:00 AM ET) and close (4:05 PM ET) daily summaries
and emails them to the configured address.
"""
from datetime import datetime, timezone

import database.db as db
from broker import alpaca
from signals import technical, sentiment
from risk import manager as risk_mgr
from basket import manager as basket_mgr
from notifications import email


def _fmt_usd(v):
    return f"${v:,.2f}" if v is not None else "N/A"


def _pl_str(v):
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def run_premarket(dry_run: bool = False):
    """9:00 AM ET — scan signals, email outlook for the day."""
    import config
    from signals import congress, insider, fundamentals
    from agent import claude_agent

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"PRE-MARKET SUMMARY  {ts}",
        f"Mode: {'DRY RUN' if dry_run else 'LIVE PAPER TRADING'}",
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
        portfolio = {"equity": 0, "cash": 0}
        positions = []
        lines.append(f"[Portfolio fetch error: {e}]")
        lines.append("")

    port_ctx = {
        "equity": portfolio.get("equity", 0),
        "cash":   portfolio.get("cash", 0),
        "position_count": len(positions),
        "options_pct": 0,
        "crypto_pct":  0,
    }

    watchlist = basket_mgr.load() + config.CRYPTO_WATCHLIST
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

            passes, criteria_reason = risk_mgr.check_entry_criteria(signals)
            if not passes:
                lines.append(f"  {symbol:<10} SKIP  | {criteria_reason}")
                continue

            decision = claude_agent.decide(symbol, signals, port_ctx)
            rsi_str  = f"RSI {tech.get('rsi', 'N/A')}" if tech else "no data"
            sent_lbl = sent.get("label", "neutral")

            lines.append(
                f"  {symbol:<10} {decision['action']:<4}  conf={decision['confidence']}/10 | "
                f"{rsi_str} | sentiment={sent_lbl}"
            )
            lines.append(f"    Why: {decision.get('rationale', '')}")
        except Exception as e:
            lines.append(f"  {symbol:<10} ERROR: {e}")

    body = "\n".join(lines)
    print("\n" + body)
    db.log_summary("premarket", body)
    email.send(f"[Trading Agent] Pre-Market Summary {datetime.now(timezone.utc).strftime('%Y-%m-%d')}", body)


def run_close():
    """4:05 PM ET — recap all trades made today and current positions."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"CLOSE-OF-DAY SUMMARY  {ts}",
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
        positions = []
        lines.append(f"[Portfolio fetch error: {e}]")
        lines.append("")

    trades = db.get_today_trades()

    if not trades:
        lines.append("No trades were executed today.")
        lines.append("(All stocks either failed the entry criteria or Claude confidence was below 7/10)")
    else:
        buys  = [t for t in trades if t["action"] == "BUY"]
        sells = [t for t in trades if t["action"] == "SELL"]
        lines.append(f"TRADES TODAY  ({len(buys)} bought, {len(sells)} sold)")
        lines.append("-" * 60)

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
    email.send(f"[Trading Agent] Close-of-Day Summary {datetime.now(timezone.utc).strftime('%Y-%m-%d')}", body)
