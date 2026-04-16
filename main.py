"""
Entry point. Run directly for a single cycle, or with --schedule for daily automation.
Usage:
  python main.py            # run one full cycle now
  python main.py --dry-run  # simulate without placing trades
  python main.py --schedule # start scheduler (runs daily at configured time)
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
    exits = manager.check_stops(positions)
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
    watchlist = config.STOCK_WATCHLIST + config.CRYPTO_WATCHLIST

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

        decision = claude_agent.decide(symbol, signals, port_ctx)
        decision = manager.validate(decision, port_ctx)

        action     = decision["action"]
        confidence = decision["confidence"]
        alloc      = decision["allocation_pct"]
        rationale  = decision["rationale"]
        asset_type = decision["asset_type"]

        print(f"  [{symbol}] → {action} | confidence={confidence} | alloc={alloc}% | {rationale}")

        if action in ("BUY", "SELL") and not dry_run:
            price = tech.get("price", 0) or 1
            qty   = manager.compute_qty(symbol, alloc, price, portfolio)

            if qty > 0:
                try:
                    if asset_type == "option":
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
                except Exception as e:
                    print(f"    ORDER ERROR: {e}")

    # --- Snapshot ---
    portfolio_final = alpaca.get_portfolio()
    positions_final = alpaca.get_positions()
    db.log_snapshot(portfolio_final["equity"], portfolio_final["cash"], positions_final)

    print(f"\nCycle complete. Equity: ${portfolio_final['equity']:,.2f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",  action="store_true", help="Simulate without placing trades")
    parser.add_argument("--schedule", action="store_true", help="Run on daily schedule")
    args = parser.parse_args()

    if args.schedule:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = BlockingScheduler(timezone="America/New_York")
        scheduler.add_job(
            lambda: run_cycle(dry_run=args.dry_run),
            CronTrigger(
                day_of_week="mon-fri",
                hour=config.RUN_HOUR,
                minute=config.RUN_MINUTE,
            ),
        )
        print(f"Scheduler started — runs Mon-Fri at {config.RUN_HOUR}:{config.RUN_MINUTE:02d} ET")
        try:
            scheduler.start()
        except KeyboardInterrupt:
            print("Scheduler stopped.")
    else:
        run_cycle(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
