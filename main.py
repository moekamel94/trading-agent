"""
Entry point.
  python main.py              # run one full cycle now
  python main.py --dry-run    # simulate without placing trades
  python main.py --schedule   # start APScheduler (legacy, prefer Task Scheduler)
  python main.py --btc-check  # check BTC position only (runs every 6h via Task Scheduler)
"""
import sys
import argparse
from datetime import datetime, timezone

import config
import database.db as db
from broker import alpaca
from signals import technical, sentiment, congress, insider, fundamentals
from agent import claude_agent
from risk import manager
from summaries import reporter
from basket import manager as basket_mgr
from notifications import telegram_bot as tg

# Load S&P 500 list once at startup for options eligibility check
_SP500 = config.get_sp500_tickers()


def _portfolio_context(portfolio, positions):
    equity = portfolio["equity"]
    options_value = sum(
        abs(p["qty"] * p["current_price"])
        for p in positions if p.get("asset_class") == "us_option"
    )
    crypto_value = sum(
        abs(p["qty"] * p["current_price"])
        for p in positions if p.get("asset_class") == "crypto"
    )
    return {
        **portfolio,
        "position_count": len(positions),
        "options_pct": (options_value / equity * 100) if equity else 0,
        "crypto_pct":  (crypto_value  / equity * 100) if equity else 0,
    }


def _is_crypto(symbol: str) -> bool:
    return "/" in symbol


def _get_bars(symbol: str):
    if _is_crypto(symbol):
        return alpaca.get_crypto_bars(symbol)
    return alpaca.get_stock_bars(symbol)


def run_cycle(dry_run: bool = False):
    print(f"\n{'='*60}")
    print(f"Trading cycle started at {datetime.now(timezone.utc).isoformat()}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE PAPER TRADING'}")
    print('='*60)

    db.init()
    portfolio = alpaca.get_portfolio()
    positions = alpaca.get_positions()
    port_ctx  = _portfolio_context(portfolio, positions)

    print(f"Portfolio: equity=${portfolio['equity']:,.2f}  cash=${portfolio['cash']:,.2f}  positions={port_ctx['position_count']}")

    # --- Check stop-loss / take-profit ---
    exits = manager.check_stops(positions, signals_map={})
    for exit_order in exits:
        sym    = exit_order["symbol"]
        reason = exit_order["reason"]
        print(f"  [EXIT] {sym} — {reason}")
        if not dry_run:
            try:
                alpaca.place_market_order(sym, 0, "SELL")  # Alpaca close position
                db.log_trade(sym, "SELL", "auto", 0, 0, 0, 10, reason)
            except Exception as e:
                print(f"    ERROR closing {sym}: {e}")

    # --- Signal + decision loop ---
    stock_basket = basket_mgr.load()
    watchlist = stock_basket + config.CRYPTO_WATCHLIST
    signals_map = {}  # used by exit checks

    for symbol in watchlist:
        print(f"\n  [{symbol}] collecting signals...")

        bars = _get_bars(symbol)
        tech = technical.compute(bars)
        sent = sentiment.compute(symbol)
        cong = congress.compute(symbol)
        insd = insider.compute(symbol)
        fund = fundamentals.compute(symbol)

        db.log_signals(symbol, tech, sent, cong, insd, fund)

        signals = {
            "technical":    tech,
            "sentiment":    sent,
            "congressional": cong,
            "insider":      insd,
            "fundamentals": fund,
        }
        signals_map[symbol] = signals

        # --- Hard criteria gate: block BUY before calling Claude (saves API cost) ---
        passes, criteria_reason = manager.check_entry_criteria(signals)
        if not passes:
            print(f"  [{symbol}] -> SKIP | {criteria_reason}")
            continue

        decision = claude_agent.decide(symbol, signals, port_ctx)
        decision = manager.validate(decision, port_ctx)

        action     = decision["action"]
        confidence = decision["confidence"]
        alloc      = decision["allocation_pct"]
        rationale  = decision["rationale"]
        asset_type = decision["asset_type"]

        print(f"  [{symbol}] -> {action} | confidence={confidence} | alloc={alloc}% | {rationale}")

        if action in ("BUY", "SELL") and not dry_run:
            price = tech.get("price", 0) or 1
            qty   = manager.compute_qty(symbol, alloc, price, portfolio)

            if qty > 0:
                try:
                    if asset_type == "option":
                        if symbol not in _SP500:
                            print(f"    SKIPPED option on {symbol} — not in S&P 500")
                            continue
                        from alpaca.trading.enums import ContractType
                        direction = decision.get("option_direction", "call")
                        contracts = alpaca.get_option_chain(symbol, config.OPTION_DAYS_TO_EXPIRY)
                        target_type = ContractType.CALL if direction == "call" else ContractType.PUT
                        matches = [c for c in contracts if c.type == target_type]
                        if matches:
                            contract = matches[len(matches) // 2]  # roughly ATM
                            alpaca.place_option_order(contract.symbol, max(1, int(qty)), action)
                            db.log_trade(symbol, action, "option", qty, price, alloc, confidence, rationale)
                    else:
                        alpaca.place_market_order(symbol, qty, action)
                        db.log_trade(symbol, action, asset_type, qty, price, alloc, confidence, rationale)
                        tg.send(
                            f"<b>TRADE EXECUTED</b>\n"
                            f"{'BUY' if action=='BUY' else 'SELL'} {symbol}\n"
                            f"Type: {asset_type} | Qty: {qty:.4f} @ ${price:,.2f}\n"
                            f"Allocation: {alloc:.1f}% | Confidence: {confidence}/10\n"
                            f"Why: {rationale}"
                        )
                except Exception as e:
                    print(f"    ORDER ERROR: {e}")

    # --- Snapshot ---
    portfolio_final = alpaca.get_portfolio()
    positions_final = alpaca.get_positions()
    db.log_snapshot(portfolio_final["equity"], portfolio_final["cash"], positions_final)

    print(f"\nCycle complete. Equity: ${portfolio_final['equity']:,.2f}")


def run_btc_check():
    """Lightweight BTC-only check — runs every 6h via Task Scheduler."""
    print(f"\n[BTC CHECK] {datetime.now(timezone.utc).isoformat()}")
    db.init()
    positions = alpaca.get_positions()
    btc_positions = [p for p in positions if "BTC" in p["symbol"]]

    if not btc_positions:
        print("[BTC CHECK] No BTC position held — nothing to check.")
        return

    exits = manager.check_stops(btc_positions)
    for exit_order in exits:
        sym    = exit_order["symbol"]
        reason = exit_order["reason"]
        print(f"[BTC CHECK] EXIT {sym} — {reason}")
        try:
            alpaca.place_market_order(sym, abs(btc_positions[0]["qty"]), "SELL")
            db.log_trade(sym, "SELL", "crypto", btc_positions[0]["qty"], 0, 0, 10, reason)
        except Exception as e:
            print(f"[BTC CHECK] Order error: {e}")
        return

    # Also buy BTC if not holding and signals are good
    from signals import technical
    bars = alpaca.get_crypto_bars("BTC/USD")
    tech = technical.compute(bars)
    from signals import sentiment as sent_mod
    sent = sent_mod.compute("BTC/USD")
    signals = {"technical": tech, "sentiment": sent, "congressional": {}, "insider": {}, "fundamentals": {}}

    passes, reason = manager.check_entry_criteria(signals)
    if not passes:
        print(f"[BTC CHECK] Criteria not met: {reason}")
        return

    portfolio = alpaca.get_portfolio()
    port_ctx  = _portfolio_context(portfolio, positions)
    decision  = claude_agent.decide("BTC/USD", signals, port_ctx)
    decision  = manager.validate(decision, port_ctx)

    if decision["action"] == "BUY":
        price = tech.get("price", 1)
        qty   = manager.compute_qty("BTC/USD", decision["allocation_pct"], price, portfolio)
        if qty > 0:
            try:
                alpaca.place_market_order("BTC/USD", qty, "BUY")
                db.log_trade("BTC/USD", "BUY", "crypto", qty, price,
                             decision["allocation_pct"], decision["confidence"], decision["rationale"])
                print(f"[BTC CHECK] BUY executed — {decision['rationale']}")
            except Exception as e:
                print(f"[BTC CHECK] Order error: {e}")
    else:
        print(f"[BTC CHECK] HOLD — {decision.get('rationale', '')}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",        action="store_true")
    parser.add_argument("--schedule",       action="store_true")
    parser.add_argument("--btc-check",      action="store_true")
    parser.add_argument("--premarket",      action="store_true")
    parser.add_argument("--close-summary",  action="store_true")
    parser.add_argument("--basket-refresh", action="store_true")
    parser.add_argument("--bot",            action="store_true")
    args = parser.parse_args()

    if args.bot:
        tg.run_bot()
    elif args.btc_check:
        run_btc_check()
    elif args.premarket:
        db.init()
        reporter.run_premarket(dry_run=False)
    elif args.close_summary:
        db.init()
        reporter.run_close()
    elif args.basket_refresh:
        basket_mgr.refresh()
    elif args.schedule:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = BlockingScheduler(timezone="America/New_York")
        scheduler.add_job(
            basket_mgr.refresh,
            CronTrigger(day_of_week="mon",
                        hour=config.BASKET_REFRESH_HOUR,
                        minute=config.BASKET_REFRESH_MINUTE),
            id="basket_refresh",
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
            id="trading_cycle",
        )
        scheduler.add_job(
            reporter.run_close,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.CLOSE_SUMMARY_HOUR,
                        minute=config.CLOSE_SUMMARY_MINUTE),
            id="close_summary",
        )
        print(
            f"Scheduler started:\n"
            f"  Basket refresh     : Mondays {config.BASKET_REFRESH_HOUR}:{config.BASKET_REFRESH_MINUTE:02d} ET\n"
            f"  Pre-market summary : Mon-Fri {config.PREMARKET_SUMMARY_HOUR}:{config.PREMARKET_SUMMARY_MINUTE:02d} ET\n"
            f"  Trading cycle      : Mon-Fri {config.RUN_HOUR}:{config.RUN_MINUTE:02d} ET\n"
            f"  Close summary      : Mon-Fri {config.CLOSE_SUMMARY_HOUR}:{config.CLOSE_SUMMARY_MINUTE:02d} ET"
        )
        try:
            scheduler.start()
        except KeyboardInterrupt:
            print("Scheduler stopped.")
    else:
        run_cycle(dry_run=args.dry_run)



if __name__ == "__main__":
    main()
