"""
Entry point.
  python main.py              # daily cheap scan (uses research cache)
  python main.py --dry-run    # simulate without placing trades
  python main.py --monthly    # run full deep research, update cache, refresh basket
  python main.py --schedule   # start APScheduler
  python main.py --discord    # start Discord bot + scheduler
  python main.py --btc-check  # check BTC position only
"""
import sys
import argparse
from datetime import datetime, timezone

import config
import database.db as db
import database.research_cache as research_cache
from broker import alpaca
from signals import technical, sentiment, congress, insider, fundamentals, research, financial_data, social, market_context, future_growth, momentum_news
from agent import claude_agent
from risk import manager
from summaries import reporter
from basket import manager as basket_mgr
from basket import curation as basket_curation
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
    # Sector concentration
    sector_pcts: dict = {}
    for p in positions:
        sector = config.SECTOR_MAP.get(p["symbol"])
        if sector and equity:
            val = abs(p["qty"] * p["current_price"])
            sector_pcts[sector] = sector_pcts.get(sector, 0) + val / equity * 100

    # Speculative tier tracking
    spec_positions = [p for p in positions
                      if config.TICKER_TIERS.get(p["symbol"]) == "speculative"]
    spec_val = sum(abs(p["qty"] * p["current_price"]) for p in spec_positions)

    return {
        **portfolio,
        "position_count":    len(positions),
        "options_pct":       (options_value / equity * 100) if equity else 0,
        "crypto_pct":        (crypto_value  / equity * 100) if equity else 0,
        "sector_pcts":       sector_pcts,
        "speculative_count": len(spec_positions),
        "speculative_pct":   (spec_val / equity * 100) if equity else 0,
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


def run_monthly_research():
    """
    Full deep research cycle — run once per month.
    Sets KIMMY_MONTHLY=1 so paid research.compute() calls are allowed.
    Calls all paid APIs (research, financial_data, social, future_growth, sentiment,
    earnings_momentum) for every basket ticker and stores results in research_cache.json.
    After caching, refreshes the basket with congress buys.
    Daily cycles then use this cache for free.
    """
    import os as _os
    _os.environ["KIMMY_MONTHLY"] = "1"

    print(f"\n{'='*60}")
    print(f"MONTHLY RESEARCH started at {datetime.now(timezone.utc).isoformat()}")
    print('='*60)

    db.init()
    stock_basket = basket_mgr.load()
    stocks = [s for s in stock_basket if not _is_crypto(s)]

    # Split by research depth to manage cost:
    #   LIGHT  (mega + large_growth): yfinance + FMP financials only — no SerpAPI/social
    #   FULL   (mid_growth + speculative): all paid APIs — these change fastest, need full picture
    light_tiers = {"mega", "large_growth"}
    light = [s for s in stocks if config.TICKER_TIERS.get(s, "mid_growth") in light_tiers]
    full  = [s for s in stocks if config.TICKER_TIERS.get(s, "mid_growth") not in light_tiers]

    print(f"  Research plan: {len(light)} light (mega/large) + {len(full)} full (mid/speculative)")
    tg.send(f"Monthly research starting — {len(light)} light + {len(full)} full depth")

    done = 0
    for symbol in stocks:
        tier      = config.TICKER_TIERS.get(symbol, "mid_growth")
        is_light  = tier in light_tiers
        depth_tag = "light" if is_light else "full"
        print(f"\n  [{symbol}] {depth_tag} research ({tier})...")
        try:
            bars  = _get_bars(symbol)
            tech  = technical.compute(bars)
            fund  = fundamentals.compute(symbol)       # yfinance — always free
            sent  = sentiment.compute(symbol)          # RSS — free
            cong  = congress.compute(symbol)           # scraper — free
            insd  = insider.compute(symbol)            # SEC EDGAR — free
            earnings_data = market_context.earnings_soon(symbol)  # Finnhub — cheap

            if is_light:
                # Mega/large: skip expensive web research + social + growth scoring
                fin_data      = financial_data.compute(symbol)  # FMP/Finnhub only
                research_data = {}
                social_data   = {}
                growth_data   = {}
                earn_mom      = {}
            else:
                # Mid/speculative: full picture — all paid APIs
                research_data = research.compute(symbol)
                fin_data      = financial_data.compute(symbol)
                social_data   = social.compute(symbol)
                growth_data   = future_growth.compute(symbol)
                earn_mom      = momentum_news.earnings_momentum(symbol)

            g_score = growth_data.get("score", "cached") if growth_data else "-"
            print(f"  [{symbol}] growth={g_score} | sent={sent.get('label')} | depth={depth_tag}")

            research_cache.save(symbol, {
                "fundamentals":          fund,
                "sentiment":             sent,
                "congressional":         cong,
                "insider":               insd,
                "future_growth":         growth_data,
                "financial_data":        fin_data,
                "social":                social_data,
                "earnings_data":         earnings_data,
                "earnings_momentum":     earn_mom,
                "research_snippets":     (research_data or {}).get("snippets", []),
                "research_source_count": (research_data or {}).get("source_count", 0),
            })
            done += 1
        except Exception as e:
            print(f"  [{symbol}] ERROR: {e}")

    # --- Monthly basket curation: scan market, add new stocks, remove weak ones ---
    print("\n  Running monthly basket curation...")
    from basket import curation
    cached_all = research_cache.load_all()
    to_add, to_remove, curation_reasoning = curation.run(stocks, cached_all, config)

    # Apply removes from config tiers (basket.json gets rebuilt below)
    for sym in to_remove:
        config.TICKER_TIERS.pop(sym, None)
        config.SECTOR_MAP.pop(sym, None)

    # Apply adds to config tiers (default to mid_growth if unknown)
    for sym in to_add:
        if sym not in config.TICKER_TIERS:
            config.TICKER_TIERS[sym] = "mid_growth"

    # Rebuild basket: sector list + adds - removes + congress buys
    from basket.manager import SECTOR_LIST
    updated_sector_list = [s for s in SECTOR_LIST if s not in to_remove] + to_add

    # Refresh basket (congress buys merged inside refresh)
    print("\n  Refreshing basket with congress buys + curation changes...")
    basket_mgr.refresh()

    changes = []
    if to_add:    changes.append(f"Added: {', '.join(to_add)}")
    if to_remove: changes.append(f"Removed: {', '.join(to_remove)}")
    changes_str = " | ".join(changes) if changes else "No changes to basket"

    msg = (f"Monthly research complete — {done}/{len(stocks)} tickers cached\n"
           f"Basket curation: {changes_str}\n"
           f"{curation_reasoning[:300]}")
    print(f"\n{msg}")
    tg.send(msg)


def run_cycle(dry_run: bool = False):
    import zoneinfo
    now_et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    time_label = now_et.strftime("%I:%M %p ET")
    day_label  = now_et.strftime("%a %b %d").replace(" 0", " ")
    trades_allowed = dry_run or is_market_hours()

    print(f"\n{'='*60}")
    print(f"Trading cycle started at {datetime.now(timezone.utc).isoformat()}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE PAPER TRADING'}")
    print('='*60)

    db.init()
    portfolio = alpaca.get_portfolio()
    positions = alpaca.get_positions()
    port_ctx  = _portfolio_context(portfolio, positions)

    # --- Market-wide context (once per cycle — free: CNN F&G + yfinance VIX) ---
    mkt_ctx = market_context.compute()
    print(f"  Market: Fear&Greed={mkt_ctx['fear_and_greed'].get('score','?')} ({mkt_ctx['market_risk']}) | VIX={mkt_ctx['vix'].get('vix','?')}")
    if mkt_ctx.get("upcoming_macro_events"):
        print(f"  Macro events this week: {[e['event'] for e in mkt_ctx['upcoming_macro_events']]}")

    print(f"Portfolio: equity=${portfolio['equity']:,.2f}  cash=${portfolio['cash']:,.2f}  positions={port_ctx['position_count']}")

    # --- Check stop-loss / take-profit ---
    exits = manager.check_stops(positions, signals_map={})
    for exit_order in exits:
        sym    = exit_order["symbol"]
        reason = exit_order["reason"]
        action = exit_order.get("action", "SELL")
        print(f"  [EXIT] {sym} — {action} | {reason}")
        if action == "REVIEW":
            tg.send(f"🔍 REVIEW **{sym}** | {reason} — check if thesis still intact")
            continue
        if not dry_run:
            try:
                alpaca.place_market_order(sym, 0, "SELL")
                db.log_trade(sym, "SELL", "auto", 0, 0, 0, 10, reason)
            except Exception as e:
                print(f"    ERROR closing {sym}: {e}")

    # --- Signal + decision loop ---
    stock_basket = basket_mgr.load()
    watchlist = stock_basket + config.CRYPTO_WATCHLIST
    signals_map = {}  # used by exit checks

    # Cycle tracking for end-of-cycle summary
    scanned_count  = 0
    claude_count   = 0
    cycle_buys:  list[str] = []
    cycle_sells: list[str] = []
    cycle_holds: list[str] = []  # tickers Claude reviewed but held

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

        scanned_count += 1
        sent = sentiment.compute(symbol)
        cong = congress.compute(symbol)
        insd = insider.compute(symbol)
        fund = fundamentals.compute(symbol)

        db.log_signals(symbol, tech, sent, cong, insd, fund)

        signals = {
            "_symbol":       symbol,
            "technical":     tech,
            "sentiment":     sent,
            "congressional": cong,
            "insider":       insd,
            "fundamentals":  fund,
            "market_context": mkt_ctx,  # needed for bear market override
        }
        signals_map[symbol] = signals

        # --- Hard criteria gate: block BUY before calling Claude (saves API cost) ---
        passes, criteria_reason = manager.check_entry_criteria(signals)
        if not passes:
            print(f"  [{symbol}] -> SKIP | {criteria_reason}")
            continue

        # --- Earnings check using cached date (free — no Finnhub call) ---
        if not _is_crypto(symbol):
            dte = research_cache.days_to_earnings_cached(symbol)
            if dte is not None and 0 <= dte <= config.CRITERIA_EARNINGS_DAYS:
                print(f"  [{symbol}] -> SKIP | Earnings in {dte} day(s) — binary event risk (cached)")
                continue

        # --- Preliminary gate (technicals + free fundamentals only) ---
        if not _is_crypto(symbol):
            tier = config.TICKER_TIERS.get(symbol, "mid_growth")

            if tier == "speculative":
                pass  # always proceed — thesis checked post-cache load
            else:
                prelim_score = 0
                eps = fund.get("eps_growth_yoy")
                rev = fund.get("revenue_growth")
                r1m = tech.get("return_1m")
                r3m = tech.get("return_3m")
                rsi = tech.get("rsi")
                gc  = tech.get("golden_cross")
                dc  = tech.get("death_cross")
                if eps is None or eps > 0:   prelim_score += 1
                if rev is None or rev > 0:   prelim_score += 1
                if r1m is None or r1m > -5:  prelim_score += 1
                if r3m is None or r3m > 0:   prelim_score += 1
                if gc:                        prelim_score += 2
                if dc:                        prelim_score -= 2
                if rsi and rsi > 70:         prelim_score -= 1
                threshold = config.MID_GROWTH_PRELIM_MIN if tier == "mid_growth" else 3
                if prelim_score < threshold:
                    print(f"  [{symbol}] -> SKIP | Prelim score {prelim_score} (tier={tier}, need {threshold})")
                    continue

        # --- Load cached research (free — no API calls) ---
        if not _is_crypto(symbol):
            cached = research_cache.load(symbol)
            if not cached:
                print(f"  [{symbol}] -> SKIP | No research cache — run python main.py --monthly first")
                continue
            earnings_data = cached.get("earnings_data") or {}
            research_data = {"snippets": cached.get("research_snippets", []),
                             "source_count": cached.get("research_source_count", 0)}
            fin_data      = cached.get("financial_data") or {}
            social_data   = cached.get("social") or {}
            growth_data   = cached.get("future_growth") or {}
            earn_momentum = cached.get("earnings_momentum") or {}
            sent          = cached.get("sentiment") or sent
            cong          = cached.get("congressional") or cong
            insd          = cached.get("insider") or insd
        else:
            # BTC: no cache, fetch live sentiment
            earnings_data = {}
            research_data = {}
            fin_data      = {}
            social_data   = social.compute(symbol)
            growth_data   = {}
            earn_momentum = {}

        signals["research"]          = research_data
        signals["financial_data"]    = fin_data
        signals["social"]            = social_data
        signals["earnings"]          = earnings_data
        signals["earnings_momentum"] = earn_momentum
        signals["market_context"]    = mkt_ctx
        signals["future_growth"]     = growth_data

        g_score = growth_data.get("score", 0)
        g_class = growth_data.get("classification", "cached")
        g_winds = growth_data.get("tailwinds", [])
        em_label = earn_momentum.get("label", "n/a")
        em_score = earn_momentum.get("combined_score", "n/a")
        print(f"  [{symbol}] growth={g_score}/100 ({g_class}) | tailwinds={g_winds} | "
              f"social={social_data.get('combined_label')} | earn_momentum={em_label}({em_score})")

        claude_count += 1
        decision = claude_agent.decide(symbol, signals, port_ctx)
        decision = {**decision, "_symbol": symbol}
        decision = manager.apply_conviction_bonuses(decision, signals)
        decision = manager.validate(decision, port_ctx)

        action     = decision["action"]
        confidence = decision["confidence"]
        alloc      = decision["allocation_pct"]
        rationale  = decision["rationale"]
        asset_type = decision["asset_type"]

        print(f"  [{symbol}] -> {action} | confidence={confidence} | alloc={alloc}% | {rationale}")

        if action == "HOLD":
            cycle_holds.append(symbol)

        if action in ("BUY", "SELL") and trades_allowed:
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
                        if action == "BUY":
                            cycle_buys.append(symbol)
                        elif action == "SELL":
                            cycle_sells.append(symbol)

                        # Build rich notification with key signals
                        tier   = config.TICKER_TIERS.get(symbol, "mid_growth")
                        g_data = signals.get("future_growth", {})
                        g_score = g_data.get("score", "?")
                        r1m    = tech.get("return_1m")
                        rsi    = tech.get("rsi")
                        pe     = signals.get("fundamentals", {}).get("pe_ratio")
                        peg    = g_data.get("peg_ratio")
                        r40    = g_data.get("rule_of_40")
                        em     = (signals.get("earnings_momentum") or {}).get("label", "")
                        dollar_amt = round(portfolio["equity"] * alloc / 100, 0)

                        snap = []
                        if g_score != "?":    snap.append(f"growth={g_score}/100")
                        if peg:               snap.append(f"PEG={peg:.2f}")
                        if r40:               snap.append(f"R40={r40:.0f}")
                        if rsi:               snap.append(f"RSI={rsi:.0f}")
                        if r1m is not None:   snap.append(f"1M={r1m:+.1f}%")
                        if pe:                snap.append(f"PE={pe:.0f}")
                        if em:                snap.append(f"earnings={em}")

                        emoji = "🟢" if action == "BUY" else "🔴"
                        tg.send(
                            f"{emoji} {action} {symbol} [{tier}]\n"
                            f"conf={confidence}/10 | {alloc}% (${dollar_amt:,.0f}) @ ${price:.2f}\n"
                            f"{' | '.join(snap)}\n"
                            f"WHY: {rationale}"
                        )
                except Exception as e:
                    print(f"    ORDER ERROR: {e}")

    # --- Snapshot ---
    portfolio_final = alpaca.get_portfolio()
    positions_final = alpaca.get_positions()
    db.log_snapshot(portfolio_final["equity"], portfolio_final["cash"], positions_final)

    equity = portfolio_final["equity"]
    cash   = portfolio_final["cash"]
    print(f"\nCycle complete. Equity: ${equity:,.2f}")

    # Build positions P&L block
    pos_lines = []
    for p in sorted(positions_final, key=lambda x: abs(x.get("unrealized_pl") or 0), reverse=True)[:8]:
        sym    = p["symbol"]
        upl    = p.get("unrealized_pl") or 0
        uplpct = p.get("unrealized_plpc") or 0
        arrow  = "▲" if upl >= 0 else "▼"
        pos_lines.append(f"  {sym:<6} {arrow} ${upl:+,.0f} ({uplpct*100:+.1f}%)")

    # Build trade summary
    trade_lines = []
    exit_syms = [e["symbol"] for e in exits] if exits else []
    if exit_syms:
        trade_lines.append(f"🔴 Exits triggered: {', '.join(exit_syms)}")
    if cycle_buys:
        trade_lines.append(f"🟢 Bought: {', '.join(cycle_buys)}")
    if cycle_sells:
        trade_lines.append(f"🔴 Sold: {', '.join(cycle_sells)}")
    if not trade_lines:
        trade_lines.append("No trades — held positions, nothing met entry criteria")

    fg_score = mkt_ctx.get("fear_and_greed", {}).get("score", "?")
    fg_label = mkt_ctx.get("fear_and_greed", {}).get("label", "")
    vix_val  = mkt_ctx.get("vix", {}).get("vix", "?")

    msg_parts = [
        f"📊 Daily Scan — {time_label} | {day_label}",
        f"Market: F&G={fg_score} ({fg_label}) | VIX={vix_val}",
        f"Portfolio: ${equity:,.0f} equity | ${cash:,.0f} cash | {len(positions_final)} positions",
        "",
    ]
    if pos_lines:
        msg_parts.append("Open positions:")
        msg_parts.extend(pos_lines)
        msg_parts.append("")
    msg_parts.append(f"Scanned {len(watchlist)} tickers | {scanned_count} passed filters | {claude_count} reached Claude")
    if cycle_holds:
        msg_parts.append(f"Held (reviewed): {', '.join(cycle_holds)}")
    msg_parts.extend(trade_lines)

    summary = "\n".join(msg_parts)
    print(summary)
    tg.send(summary)


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
    signals = {"_symbol": "BTC/USD", "technical": tech, "sentiment": sent,
               "congressional": {}, "insider": {}, "fundamentals": {}}

    passes, reason = manager.check_entry_criteria(signals)
    if not passes:
        print(f"[BTC CHECK] Criteria not met: {reason}")
        return

    portfolio = alpaca.get_portfolio()
    port_ctx  = _portfolio_context(portfolio, positions)
    decision  = claude_agent.decide("BTC/USD", signals, port_ctx)
    decision  = {**decision, "_symbol": "BTC/USD"}
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
    parser.add_argument("--monthly",        action="store_true")
    parser.add_argument("--btc-check",      action="store_true")
    parser.add_argument("--premarket",      action="store_true")
    parser.add_argument("--close-summary",  action="store_true")
    parser.add_argument("--basket-refresh", action="store_true")
    parser.add_argument("--bot",            action="store_true")
    parser.add_argument("--discord",        action="store_true")
    args = parser.parse_args()

    if args.monthly:
        run_monthly_research()
        return

    if args.discord:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        # Safe wrappers — a crash in one job must never kill the whole bot
        def _safe_cycle():
            try:
                run_cycle()
            except Exception as e:
                msg = f"❌ Trading cycle crashed: {e}"
                print(msg)
                try: tg.send(msg)
                except Exception: pass

        def _safe_monthly():
            try:
                run_monthly_research()
            except Exception as e:
                msg = f"❌ Monthly research crashed: {e}"
                print(msg)
                try: tg.send(msg)
                except Exception: pass

        def _safe_premarket():
            try:
                reporter.run_premarket(dry_run=False)
            except Exception as e:
                print(f"Premarket summary error: {e}")

        def _safe_close():
            try:
                reporter.run_close()
            except Exception as e:
                print(f"Close summary error: {e}")

        # misfire_grace_time: if the bot was down when the job fired, run it
        # within this many seconds of restart instead of skipping it entirely
        GRACE = 3600  # 1 hour — catches bot restarts mid-session

        scheduler = BackgroundScheduler(timezone="America/New_York")
        scheduler.add_job(
            _safe_monthly,
            CronTrigger(day="1-7", day_of_week="mon",
                        hour=config.BASKET_REFRESH_HOUR,
                        minute=config.BASKET_REFRESH_MINUTE),
            id="monthly_research",
            misfire_grace_time=GRACE,
        )
        scheduler.add_job(
            _safe_premarket,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.PREMARKET_SUMMARY_HOUR,
                        minute=config.PREMARKET_SUMMARY_MINUTE),
            id="premarket_summary",
            misfire_grace_time=GRACE,
        )
        scheduler.add_job(
            _safe_cycle,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.RUN_HOUR,
                        minute=config.RUN_MINUTE),
            id="trading_cycle_open",
            misfire_grace_time=GRACE,
        )
        scheduler.add_job(
            _safe_cycle,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.AFTERNOON_HOUR,
                        minute=config.AFTERNOON_MINUTE),
            id="trading_cycle_close",
            misfire_grace_time=GRACE,
        )
        scheduler.add_job(
            _safe_close,
            CronTrigger(day_of_week="mon-fri",
                        hour=config.CLOSE_SUMMARY_HOUR,
                        minute=config.CLOSE_SUMMARY_MINUTE),
            id="close_summary",
            misfire_grace_time=GRACE,
        )
        scheduler.start()
        print(
            f"[Scheduler] Started inside Kimmy:\n"
            f"  Monthly research   : 1st Monday/month {config.BASKET_REFRESH_HOUR}:{config.BASKET_REFRESH_MINUTE:02d} ET\n"
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
            run_monthly_research,
            CronTrigger(day="1-7", day_of_week="mon",
                        hour=config.BASKET_REFRESH_HOUR,
                        minute=config.BASKET_REFRESH_MINUTE),
            id="monthly_research",
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
            f"  Monthly research   : 1st Monday/month {config.BASKET_REFRESH_HOUR}:{config.BASKET_REFRESH_MINUTE:02d} ET\n"
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
