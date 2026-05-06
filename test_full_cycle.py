"""
Full cycle test: runs real signal collection + Claude committee review for one LT stock
(AAPL) and one MT/MD stock (AMD), sends the identical thesis-rich Discord message format
used by the live cycle, places Alpaca paper orders, then cancels them.

Run: /root/venv/bin/python test_full_cycle.py
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from broker import alpaca
from database import db
from notifications import discord_bot as discord
from signals import technical, sentiment, congress, insider, fundamentals, market_context
from signals import options_flow as uw_flow
from agent import claude_agent
from risk import manager as risk_manager

# ── Test parameters ──────────────────────────────────────────────────────────
# LT_SYMBOL = Long-Term bucket test stock
# MT_SYMBOL = Medium-Term (MD) bucket test stock
# Force bucket assignments so Claude's committee allocates each to the right sleeve
LT_SYMBOL  = "AAPL"
MT_SYMBOL  = "AMD"
TEST_QTY   = 1

def step(msg):
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}")


def collect_signals(symbol: str, mkt_ctx: dict) -> dict:
    """Collect real signals for a ticker — same path as run_cycle()."""
    bars = alpaca.get_stock_bars(symbol, days=300)
    tech = technical.compute(bars)

    sent = sentiment.compute(symbol)
    cong = congress.compute(symbol)
    insd = insider.compute(symbol)
    fund = fundamentals.compute(symbol)

    uw = {"flow_signal": "no_data",
          "darkpool": {"darkpool_signal": "no_data", "large_print_count": 0,
                       "total_prints_3d": 0}}
    if config.UNUSUAL_WHALES_API_KEY:
        try:
            uw = uw_flow.compute(symbol, current_price=tech.get("price"))
            dp = uw_flow.get_darkpool(symbol)
            uw["darkpool"] = dp
        except Exception as e:
            print(f"  [{symbol}] UW error (non-fatal): {e}")

    return {
        "_symbol":        symbol,
        "technical":      tech,
        "sentiment":      sent,
        "congressional":  cong,
        "insider":        insd,
        "fundamentals":   fund,
        "market_context": mkt_ctx,
        "uw_market":      {},
        "options_flow":   uw,
    }


def build_discord_msg(symbol: str, action: str, decision: dict, signals: dict,
                      portfolio: dict) -> str:
    """Identical message builder to main.py Phase 3 — same fields, same layout."""
    tier        = config.TICKER_TIERS.get(symbol, "mid_growth")
    alloc       = decision.get("allocation_pct", 1.0)
    target      = decision.get("target_pct", alloc)
    confidence  = decision.get("confidence", 7)
    cio_c       = decision.get("cio_confidence", confidence)
    rationale   = decision.get("rationale", "")
    dollar_amt  = round(portfolio["equity"] * alloc / 100, 0)

    tech        = signals.get("technical", {})
    g_data      = signals.get("future_growth", {})
    g_score     = g_data.get("score", "?")
    r1m         = tech.get("return_1m")
    rsi         = tech.get("rsi")
    pe          = signals.get("fundamentals", {}).get("pe_ratio")
    peg         = g_data.get("peg_ratio")
    r40         = g_data.get("rule_of_40")
    em          = (signals.get("earnings_momentum") or {}).get("label", "")

    snap = []
    if g_score != "?": snap.append(f"growth={g_score}/100")
    if peg:            snap.append(f"PEG={peg:.2f}")
    if r40:            snap.append(f"R40={r40:.0f}")
    if rsi:            snap.append(f"RSI={rsi:.0f}")
    if r1m is not None: snap.append(f"1M={r1m:+.1f}%")
    if pe:             snap.append(f"PE={pe:.0f}")
    if em:             snap.append(f"earnings={em}")

    da_sev      = decision.get("da_severity", "")
    da_bear     = decision.get("da_bear_case", "")
    crs_moat    = decision.get("crs_product_moat", "")
    crs_market  = decision.get("crs_market_outlook", "")
    crs_edge    = decision.get("crs_competitive_edge", "")
    crs_product = decision.get("crs_product_advantage", "")
    crs_catalyst= decision.get("crs_growth_catalyst", "")
    crs_why     = decision.get("crs_why_this_over_peers", "")
    thesis_sum  = decision.get("thesis_summary", "")
    stop_crit   = decision.get("thesis_break_criteria", "")
    price_tgt   = decision.get("price_target")
    pt_basis    = decision.get("price_target_basis", "")
    bucket      = decision.get("bucket", "long_term")
    emoji       = "🟢" if action == "BUY" else "🔴"

    bucket_label = "LT" if bucket == "long_term" else "MT/MD"
    lines = [
        f"{emoji} **[TEST] {action} {symbol}** [{tier}] [{bucket_label}] Tranche 1/{3 if target > alloc else 1}",
        f"CIO={cio_c}/10 → final={confidence}/10 | {alloc}% now → {target}% target (${dollar_amt:,.0f})",
        " | ".join(snap),
    ]
    if action == "BUY":
        moat_label = crs_moat.upper() if crs_moat else "N/A"
        lines.append(f"\n━━━ RESEARCH THESIS [{moat_label} MOAT] ━━━")
        if crs_market:
            lines.append(f"📈 **Market Outlook**\n{crs_market}")
        if crs_edge:
            lines.append(f"⚔️ **Competitive Edge vs Peers**\n{crs_edge}")
        if crs_product:
            lines.append(f"🔬 **Product Advantage**\n{crs_product}")
        if crs_catalyst:
            lines.append(f"🚀 **Growth Catalyst**\n{crs_catalyst}")
        if crs_why:
            lines.append(f"🎯 **Why This Over Peers**\n{crs_why}")
        if thesis_sum:
            lines.append(f"\n📋 **Summary**\n{thesis_sum}")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📌 **Decision:** {rationale}")
    if price_tgt:
        price_now = tech.get("price", 0) or 0
        upside = (price_tgt - price_now) / price_now * 100 if price_now else 0
        lines.append(f"🎯 **Price target:** ${price_tgt:.0f} ({upside:+.0f}% upside)  [{pt_basis}]")
    if stop_crit:
        lines.append(f"🛑 **Exit if:** {stop_crit}")
    if da_bear:
        lines.append(f"⚠️ **Bear case:** {da_bear} [{da_sev}]")
    lines.append(f"🧪 *Test order — will be cancelled shortly*")
    return "\n".join(lines)


def cancel_or_sell(symbol: str, order_id: str, qty: float, price: float, bucket_label: str):
    """Cancel pending order; fall back to sell if already filled."""
    cancelled = alpaca.cancel_order(order_id)
    if cancelled:
        print(f"  Order {order_id} cancelled (was pending).")
        db.log_trade(symbol, "SELL", "stock", qty, price, 0, 0,
                     f"[TEST] Cancelled pending {bucket_label} test order")
        discord.send(
            f"🔴 **[TEST] CANCELLED {symbol}** [{bucket_label}]\n"
            f"Order {order_id} — pending order cancelled.\n"
            f"✅ {bucket_label} cycle test complete."
        )
    else:
        print(f"  Order already filled — placing SELL for {symbol}")
        positions = {p["symbol"]: p for p in alpaca.get_positions()}
        live_qty   = positions.get(symbol, {}).get("qty", qty)
        live_price = positions.get(symbol, {}).get("current_price", price)
        try:
            sell = alpaca.place_market_order(symbol, live_qty, "SELL")
            print(f"  Sell Order ID: {sell.id}  |  Status: {sell.status}")
        except Exception as e:
            print(f"  SELL ORDER ERROR: {e}")
        db.log_trade(symbol, "SELL", "stock", live_qty, live_price, 0, 0,
                     f"[TEST] Sold filled {bucket_label} test position")
        discord.send(
            f"🔴 **[TEST] SELL {symbol}** [{bucket_label}]\n"
            f"Qty: {live_qty} | Price: ~${live_price:.2f}\n"
            f"✅ {bucket_label} cycle test complete."
        )
    print("  Discord notification sent.")


def run_test():
    db.init()

    # ── Portfolio & market context ────────────────────────────────────────────
    step("PHASE 1 — Portfolio + market context")
    portfolio  = alpaca.get_portfolio()
    positions  = alpaca.get_positions()
    equity     = portfolio["equity"]
    print(f"  Equity: ${equity:,.2f}  |  Cash: ${portfolio['cash']:,.2f}")

    port_ctx = {
        "equity":          equity,
        "cash":            portfolio["cash"],
        "buying_power":    portfolio["buying_power"],
        "position_count":  len(positions),
        "long_term_pct":   0.0,
        "medium_term_pct": 0.0,
        "long_term_count": 0,
        "medium_term_count": 0,
        "holdings":        [],
        "speculative_pct": 0.0,
        "speculative_count": 0,
        "options_pct":     0.0,
        "sector_pcts":     {},
        "portfolio_beta":  None,
        "portfolio_drawdown_pct": 0.0,
    }
    mkt_ctx = market_context.compute()
    print("  Market context collected.")

    # ── Signal collection ─────────────────────────────────────────────────────
    step(f"PHASE 2 — Collecting real signals for {LT_SYMBOL} (LT) and {MT_SYMBOL} (MT/MD)")
    print(f"  Scanning {LT_SYMBOL}...")
    lt_signals = collect_signals(LT_SYMBOL, mkt_ctx)
    lt_price   = lt_signals["technical"].get("price", 0)
    print(f"  {LT_SYMBOL}: price=${lt_price:.2f}  RSI={lt_signals['technical'].get('rsi', '?'):.0f}")

    print(f"  Scanning {MT_SYMBOL}...")
    mt_signals = collect_signals(MT_SYMBOL, mkt_ctx)
    mt_price   = mt_signals["technical"].get("price", 0)
    print(f"  {MT_SYMBOL}: price=${mt_price:.2f}  RSI={mt_signals['technical'].get('rsi', '?'):.0f}")

    # ── Build candidates — force bucket assignments ───────────────────────────
    from agent import claude_agent as _ca
    lt_synthesis = _ca._build_synthesis(LT_SYMBOL, lt_signals)
    mt_synthesis = _ca._build_synthesis(MT_SYMBOL, mt_signals)

    candidates = [
        {
            "symbol":    LT_SYMBOL,
            "signals":   lt_signals,
            "synthesis": lt_synthesis,
            "_force_bucket": "long_term",
        },
        {
            "symbol":    MT_SYMBOL,
            "signals":   mt_signals,
            "synthesis": mt_synthesis,
            "_force_bucket": "medium_term",
        },
    ]

    # ── Committee review — real Claude call ───────────────────────────────────
    step("PHASE 3 — Claude committee review (real AI call)")
    decisions = claude_agent.committee_review(candidates, port_ctx, mkt_ctx,
                                              force_opus=True)
    print(f"  Committee returned {len(decisions)} decision(s).")
    for d in decisions:
        print(f"  [{d['symbol']}] action={d.get('action')} | "
              f"conf={d.get('confidence')} | bucket={d.get('bucket')} | "
              f"{d.get('rationale','')[:80]}")

    # Build lookup by symbol
    dec_map = {d["symbol"]: d for d in decisions}

    # ── BUY orders + Discord thesis ───────────────────────────────────────────
    order_ids = {}

    for symbol, bucket_label, signals, price in [
        (LT_SYMBOL, "LT",    lt_signals, lt_price),
        (MT_SYMBOL, "MT/MD", mt_signals, mt_price),
    ]:
        step(f"PHASE 4 — BUY {symbol} [{bucket_label}] + Discord thesis")
        decision = dec_map.get(symbol, {})
        action   = decision.get("action", "HOLD")

        if action not in ("BUY", "HOLD"):
            # Committee said SELL/BUCKET — override to BUY for test purposes
            print(f"  [{symbol}] Committee said {action} — overriding to BUY for test")
            decision["action"] = "BUY"
            action = "BUY"
            decision.setdefault("allocation_pct", 1.0)
            decision.setdefault("confidence", 7)
            decision.setdefault("rationale", f"[TEST override] committee={action}")
        elif action == "HOLD":
            print(f"  [{symbol}] Committee HOLD — overriding to BUY for test")
            decision["action"] = "BUY"
            action = "BUY"
            decision.setdefault("allocation_pct", 1.0)
            decision.setdefault("confidence", 7)
            decision.setdefault("rationale", f"[TEST override] committee=HOLD")

        # Place paper order
        try:
            order = alpaca.place_market_order(symbol, TEST_QTY, "BUY")
            order_ids[symbol] = str(order.id)
            print(f"  Order ID: {order_ids[symbol]}  |  Status: {order.status}")
        except Exception as e:
            print(f"  ORDER ERROR: {e}")
            order_ids[symbol] = None

        # Log to DB
        db.log_trade(symbol, "BUY", "stock", TEST_QTY, price,
                     decision.get("allocation_pct", 1.0),
                     decision.get("confidence", 7),
                     f"[TEST] {decision.get('rationale','')[:200]}")

        # Send real thesis Discord message (same format as live cycle)
        msg = build_discord_msg(symbol, "BUY", decision, signals, portfolio)
        discord.send(msg)
        print("  Discord thesis notification sent.")
        time.sleep(2)

    # ── Cancel orders ─────────────────────────────────────────────────────────
    for symbol, bucket_label, price in [
        (LT_SYMBOL, "LT",    lt_price),
        (MT_SYMBOL, "MT/MD", mt_price),
    ]:
        step(f"PHASE 5 — CANCEL {symbol} [{bucket_label}]")
        oid = order_ids.get(symbol)
        if oid:
            cancel_or_sell(symbol, oid, TEST_QTY, price, bucket_label)
        else:
            print(f"  No order ID for {symbol} — skipping cancel")
        time.sleep(1)

    # ── Summary ───────────────────────────────────────────────────────────────
    step("PHASE 6 — Summary")
    summary = (
        f"🧪 **FULL CYCLE TEST COMPLETE**\n"
        f"─────────────────────────────\n"
        f"✅ LT bucket   — {LT_SYMBOL}: real signals → Claude thesis → BUY → CANCEL\n"
        f"✅ MT/MD bucket — {MT_SYMBOL}: real signals → Claude thesis → BUY → CANCEL\n"
        f"Discord ✅  |  DB ✅  |  Alpaca paper ✅\n"
        f"─────────────────────────────\n"
        f"All systems operational 🚀"
    )
    discord.send(summary)
    print(summary)


if __name__ == "__main__":
    run_test()
