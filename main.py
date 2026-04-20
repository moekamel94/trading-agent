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
from signals import technical, sentiment, congress, insider, fundamentals, research, financial_data, social, market_context, future_growth, momentum_news
from agent import claude_agent
from risk import manager
from summaries import reporter
from basket import manager as basket_mgr
from notifications import discord_bot as tg

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


def is_market_hours() -> bool:
    """Return True if current ET time is within regular market hours (Mon-Fri 9:30–16:00)."""
    import zoneinfo
    now_et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    open_time  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_time = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_time <= now_et < close_time


def run_cycle(dry_run: bool = False):
    if not dry_run and not is_market_hours():
        print(f"[SKIP] run_cycle called outside market hours — no trades placed.")
        return

    print(f"\n{'='*60}")
    print(f"Trading cycle started at {datetime.now(timezone.utc).isoformat()}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE PAPER TRADING'}")
    print('='*60)

    db.init()
    portfolio = alpaca.get_portfolio()
    positions = alpaca.get_positions()
    port_ctx  = _portfolio_context(portfolio, positions)

    # --- Market-wide context (once per cycle) ---
    mkt_ctx = market_context.compute()

    # --- Global macro/geopolitical momentum (once per cycle) ---
    print("  [MACRO] Fetching global macro/geopolitical momentum news...")
    macro_momentum = momentum_news.global_macro_momentum()
    mkt_ctx["macro_momentum"] = macro_momentum
    print(f"  [MACRO] label={macro_momentum.get('label', '?')} score={macro_momentum.get('score', '?')} themes={macro_momentum.get('themes', [])}")
    print(f"  Market: Fear&Greed={mkt_ctx['fear_and_greed'].get('score','?')} ({mkt_ctx['market_risk']}) | VIX={mkt_ctx['vix'].get('vix','?')}")
    if mkt_ctx.get("upcoming_macro_events"):
        print(f"  Macro events this week: {[e['event'] for e in mkt_ctx['upcoming_macro_events']]}")

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

        # Quick technical pre-filter: skip immediately if price is below SMA50
        # AND a death cross is confirmed (both signals bearish = clear downtrend).
        # This avoids 4 expensive API calls per ticker that would fail the gate anyway.
        if not _is_crypto(symbol):
            price  = tech.get("price") or 0
            sma50  = tech.get("sma50") or 0
            below_sma50   = sma50 > 0 and price < sma50
            death_cross   = bool(tech.get("death_cross"))
            golden_cross  = bool(tech.get("golden_cross"))
            at_bb_upper   = tech.get("bb_position") == "above_upper"
            if below_sma50 and death_cross and not golden_cross:
                print(f"  [{symbol}] -> SKIP | Quick filter: below SMA50 + death cross")
                continue
            if at_bb_upper and below_sma50:
                print(f"  [{symbol}] -> SKIP | Quick filter: overextended + below SMA50")
                continue

        sent = sentiment.compute(symbol)
        cong = congress.compute(symbol)
        insd = insider.compute(symbol)
        fund = fundamentals.compute(symbol)

        db.log_signals(symbol, tech, sent, cong, insd, fund)

        signals = {
            "_symbol":      symbol,
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

        # --- Earnings check before expensive research (binary event guard) ---
        earnings_data = market_context.earnings_soon(symbol)
        dte = earnings_data.get("days_to_earnings")
        if dte is not None and 0 <= dte <= config.CRITERIA_EARNINGS_DAYS:
            print(f"  [{symbol}] -> SKIP | Earnings in {dte} day(s) — binary event risk")
            continue

        # --- Cheap preliminary gate: only run expensive research if worth it ---
        # Uses only yfinance fundamentals (already fetched) + technicals.
        # Saves 6 web searches + 6 financial API calls for stocks with weak prelim signal.
        if not _is_crypto(symbol):
            prelim_score = 0
            eps  = fund.get("eps_growth_yoy")
            rev  = fund.get("revenue_growth")
            pe   = fund.get("pe_ratio")
            r1m  = tech.get("return_1m")
            r3m  = tech.get("return_3m")
            rsi  = tech.get("rsi")
            gc   = tech.get("golden_cross")
            dc   = tech.get("death_cross")
            if eps is None or eps > 0:          prelim_score += 1
            if rev is None or rev > 0:          prelim_score += 1
            if r1m is None or r1m > -5:         prelim_score += 1
            if r3m is None or r3m > 0:          prelim_score += 1
            if gc:                               prelim_score += 2
            if dc:                               prelim_score -= 2
            if rsi and rsi > 70:                prelim_score -= 1
            if prelim_score < 2:
                print(f"  [{symbol}] -> SKIP | Prelim score {prelim_score} — not worth deep research")
                continue

        # --- Deep research + financial data + social + future growth ---
        print(f"  [{symbol}] running deep research + financial data + social + growth eval...")
        research_data  = research.compute(symbol)
        fin_data       = financial_data.compute(symbol)
        social_data    = social.compute(symbol)
        growth_data      = future_growth.compute(symbol)
        earn_momentum    = momentum_news.earnings_momentum(symbol)
        signals["research"]          = research_data
        signals["financial_data"]    = fin_data
        signals["social"]            = social_data
        signals["earnings"]          = earnings_data
        signals["earnings_momentum"] = earn_momentum
        signals["market_context"]    = mkt_ctx
        signals["future_growth"]     = growth_data
        g_score = growth_data.get("score", 0)
        g_class = growth_data.get("classification", "unknown")
        g_winds = growth_data.get("tailwinds", [])
        em_label = earn_momentum.get("label", "n/a")
        em_score = earn_momentum.get("combined_score", "n/a")
        print(f"  [{symbol}] growth={g_score}/100 ({g_class}) | tailwinds={g_winds} | social={social_data.get('combined_label')} | earnings_soon={earnings_data.get('earnings_soon')} | earn_momentum={em_label}({em_score})")

        decision = claude_agent.decide(symbol, signals, port_ctx)
        decision = manager.apply_conviction_bonuses(decision, signals)
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
                        print(f"  [TRADE] {action} {symbol} conf={confidence}/10")
                        tg.send(
                            f"{'🟢 BUY' if action == 'BUY' else '🔴 SELL'} **{symbol}** | "
                            f"conf={confidence}/10 | alloc={alloc}% | ${price:.2f}\n"
                            f"{rationale}"
                        )
                except Exception as e:
                    print(f"    ORDER ERROR: {e}")

    # --- Snapshot ---
    portfolio_final = alpaca.get_portfolio()
    positions_final = alpaca.get_positions()
    db.log_snapshot(portfolio_final["equity"], portfolio_final["cash"], positions_final)

    buys_this_cycle = [e for e in exits] if exits else []
    print(f"\nCycle complete. Equity: ${portfolio_final['equity']:,.2f}")
    tg.send(
        f"✅ Cycle complete | Equity: ${portfolio_final['equity']:,.2f} | "
        f"Cash: ${portfolio_final['cash']:,.2f} | Positions: {len(positions_final)}"
    )


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
    parser.add_argument("--discord",        action="store_true")
    args = parser.parse_args()

    if args.discord:
        # Run the trading scheduler in a background thread alongside the Discord bot.
        # This means one service (kimmy) handles both Discord and auto-trading.
        import threading
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = BackgroundScheduler(timezone="America/New_York")
        scheduler.add_job(
            basket_mgr.refresh,
            CronTrigger(day="1-7", day_of_week="mon",
                        hour=config.BASKET_REFRESH_HOUR,
                        minute=config.BASKET_REFRESH_MINUTE),
            id="basket_refresh",
        )
        scheduler.add_job(
            lambda: reporter.run_premarket(dry_run=False),
            CronTrigger(day_of_week="mon-fri",
                        hour=config.PREMARKET_SUMMARY_HOUR,
                        minute=config.PREMARKET_SUMMARY_MINUTE),
            id="premarket_summary",
        )
        # Morning cycle: catch overnight news and gaps
        scheduler.add_job(
            run_cycle,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.RUN_HOUR,
                        minute=config.RUN_MINUTE),
            id="trading_cycle_open",
        )
        # Afternoon cycle: daily bars ~97% complete — best signal quality
        scheduler.add_job(
            run_cycle,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.AFTERNOON_HOUR,
                        minute=config.AFTERNOON_MINUTE),
            id="trading_cycle_close",
        )
        scheduler.add_job(
            reporter.run_close,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.CLOSE_SUMMARY_HOUR,
                        minute=config.CLOSE_SUMMARY_MINUTE),
            id="close_summary",
        )
        scheduler.start()
        print(
            f"[Scheduler] Started inside Kimmy:\n"
            f"  Basket refresh     : 1st Monday/month {config.BASKET_REFRESH_HOUR}:{config.BASKET_REFRESH_MINUTE:02d} ET\n"
            f"  Pre-market summary : Mon-Fri {config.PREMARKET_SUMMARY_HOUR}:{config.PREMARKET_SUMMARY_MINUTE:02d} ET\n"
            f"  Trading cycle (AM) : Mon-Fri {config.RUN_HOUR}:{config.RUN_MINUTE:02d} ET\n"
            f"  Trading cycle (PM) : Mon-Fri {config.AFTERNOON_HOUR}:{config.AFTERNOON_MINUTE:02d} ET\n"
            f"  Close summary      : Mon-Fri {config.CLOSE_SUMMARY_HOUR}:{config.CLOSE_SUMMARY_MINUTE:02d} ET"
        )

        from notifications import discord_bot
        discord_bot.run_bot()
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
            CronTrigger(day="1-7", day_of_week="mon",
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
            id="trading_cycle_open",
        )
        scheduler.add_job(
            lambda: run_cycle(dry_run=args.dry_run),
            CronTrigger(day_of_week="mon-fri",
                        hour=config.AFTERNOON_HOUR,
                        minute=config.AFTERNOON_MINUTE),
            id="trading_cycle_close",
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
            f"  Basket refresh     : 1st Monday/month {config.BASKET_REFRESH_HOUR}:{config.BASKET_REFRESH_MINUTE:02d} ET\n"
            f"  Pre-market summary : Mon-Fri {config.PREMARKET_SUMMARY_HOUR}:{config.PREMARKET_SUMMARY_MINUTE:02d} ET\n"
            f"  Trading cycle (AM) : Mon-Fri {config.RUN_HOUR}:{config.RUN_MINUTE:02d} ET\n"
            f"  Trading cycle (PM) : Mon-Fri {config.AFTERNOON_HOUR}:{config.AFTERNOON_MINUTE:02d} ET\n"
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
