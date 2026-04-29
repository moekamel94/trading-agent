"""
Weekly portfolio review — runs every Sunday at 6:00 PM ET.

One Claude Haiku call reviews all open positions holistically:
- Position sizing vs limits
- P&L and thesis health per holding
- Sector concentration
- Dead money candidates
- Rebalancing recommendations

Sends a full action plan to Discord. Uses only cached research + live
yfinance (no paid APIs). Cost: ~$0.002 per week.
"""
import json
import anthropic
import config
import database.db as db
import database.research_cache as research_cache
from broker import alpaca
from signals import technical
from notifications import discord_bot as tg


def _get_bars(symbol: str):
    if "/" in symbol:
        return alpaca.get_crypto_bars(symbol)
    return alpaca.get_stock_bars(symbol)


def run():
    print("\n" + "=" * 60)
    print("WEEKLY PORTFOLIO REVIEW")
    print("=" * 60)

    try:
        portfolio  = alpaca.get_portfolio()
        positions  = alpaca.get_positions()
    except Exception as e:
        tg.send(f"❌ Weekly review failed — could not fetch portfolio: {e}")
        return

    equity = portfolio["equity"]
    cash   = portfolio["cash"]

    if not positions:
        tg.send("📋 Weekly Review: No open positions — fully in cash.")
        return

    # Build a detailed snapshot of every position
    pos_data = []
    for p in positions:
        sym   = p["symbol"]
        qty   = p["qty"]
        price = p["current_price"]
        entry = p.get("avg_entry") or p.get("avg_entry_price") or 0
        val   = abs(qty * price)
        pct   = val / equity * 100 if equity else 0
        upl   = p.get("unrealized_pl") or 0
        uplpc = p.get("unrealized_plpc", 0)
        tier  = config.TICKER_TIERS.get(sym, "unknown")
        sector = config.SECTOR_MAP.get(sym, "unknown")

        # Live technicals (free — yfinance)
        try:
            bars  = _get_bars(sym)
            tech  = technical.compute(bars)
            rsi   = tech.get("rsi")
            gc    = tech.get("golden_cross")
            dc    = tech.get("death_cross")
            r1m   = tech.get("return_1m")
            r3m   = tech.get("return_3m")
            above_sma200 = (tech.get("price") or 0) > (tech.get("sma200") or 0)
        except Exception:
            rsi = gc = dc = r1m = r3m = None
            above_sma200 = None

        # Cached fundamentals (free)
        cached  = research_cache.load(sym) or {}
        fund    = cached.get("fundamentals") or {}
        growth  = cached.get("future_growth") or {}
        earn_m  = cached.get("earnings_momentum") or {}

        pos_data.append({
            "sym":    sym,
            "tier":   tier,
            "sector": sector,
            "pct":    round(pct, 1),
            "pl_pct": round(uplpc, 1),
            "rsi":    round(rsi, 1) if rsi else None,
            "gc":     gc,
            "dc":     dc,
            "r1m":    round(r1m, 1) if r1m is not None else None,
            "r3m":    round(r3m, 1) if r3m is not None else None,
            "sma200": above_sma200,
            "rev_g":  fund.get("revenue_growth"),
            "eps_g":  fund.get("eps_growth_yoy"),
            "g_score": growth.get("score"),
            "earn":   earn_m.get("label"),
        })

    # Sector concentration
    sector_pcts: dict = {}
    for p in pos_data:
        s = p["sector"]
        sector_pcts[s] = sector_pcts.get(s, 0) + p["pct"]

    spec_pct = sum(p["pct"] for p in pos_data if p["tier"] == "speculative")
    spec_count = sum(1 for p in pos_data if p["tier"] == "speculative")

    # Factor cluster concentrations
    cluster_pcts: dict = {}
    for p in pos_data:
        sym = p["sym"]
        for cluster, tickers in config.FACTOR_CLUSTERS.items():
            if sym in tickers:
                cluster_pcts[cluster] = cluster_pcts.get(cluster, 0) + p["pct"]
    cluster_breaches = {k: v for k, v in cluster_pcts.items() if v >= config.FACTOR_CLUSTER_CAP * 100}

    prompt = f"""You are Kimmy's investment committee doing a weekly portfolio review.
Today is a Sunday review — market opens Monday. Your job is to review every position
and give clear, actionable recommendations before the week begins.

PORTFOLIO OVERVIEW:
  Total equity : ${equity:,.2f}
  Cash         : ${cash:,.2f} ({cash/equity*100 if equity else 0.0:.1f}% of portfolio)
  Positions    : {len(pos_data)}
  Speculative  : {spec_count} positions = {spec_pct:.1f}% (max allowed: {config.MAX_SPECULATIVE_PCT}%)

SECTOR CONCENTRATION:
{json.dumps(sector_pcts, indent=2)}

FACTOR CLUSTER CONCENTRATION (max 40% per cluster):
{json.dumps({k: f"{v:.1f}%" for k, v in sorted(cluster_pcts.items(), key=lambda x: -x[1]) if v > 0}, indent=2)}
{"⚠️ CLUSTER BREACHES: " + ", ".join(f"{k}={v:.1f}%" for k,v in cluster_breaches.items()) if cluster_breaches else "No cluster breaches."}

POSITION DETAILS:
{json.dumps(pos_data, indent=2, default=str)}

LIMITS TO ENFORCE:
  - Max 8% per position
  - Max 10% total in speculative tier (max 5 positions)
  - Max 25% in any single sector | Max 40% in any single factor cluster
  - TRIM RULE: flag any position ≥ 1.4× its target tier allocation OR up >50% in <30 days → TRIM 33%
  - Dead money rule: if held >90 days AND profit <3% → recommend exit (NOT for speculative)
  - Speculative: hold through volatility unless technology thesis broken
  - BENCHMARK: we target 2× SPY return (positive years) / beat SPY (negative years).
    Flag any position that is underperforming SPY by >10% over 90 days with no catalyst.

KIMMY'S THESIS: AI/semis, cybersecurity, defense, nuclear energy, space, healthcare AI,
fintech, energy/commodities tied to AI.
RETURN TARGET: 2× SPY annual return. If SPY is up 15% YTD, we need +30%. Benchmark everything against this.
A position that tracks SPY is a failure — we need names that materially outperform.

REVIEW EACH POSITION AND ANSWER:
1. Is the position sized correctly? Flag any over/underweight.
2. Is the thesis still intact? Check revenue/EPS trends.
3. Any technical warning signs? (death cross, below SMA200, RSI extremes)
4. Is it dead money? Flat for too long with no catalyst?
5. Any position to trim, add to, or exit?

OUTPUT FORMAT — respond in this exact JSON:
{{
  "actions": [
    {{
      "symbol": "XXXX",
      "recommendation": "HOLD" | "TRIM" | "EXIT" | "ADD",
      "urgency": "immediate" | "this_week" | "monitor",
      "reason": "one concise sentence"
    }}
  ],
  "portfolio_health": "brief 2-sentence overall assessment",
  "cash_comment": "should we deploy cash, hold it, or raise more?",
  "top_concern": "single biggest risk in the current portfolio"
}}"""

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)
    except Exception as e:
        tg.send(f"❌ Weekly review Claude call failed: {e}")
        return

    actions   = result.get("actions", [])
    health    = result.get("portfolio_health", "")
    cash_note = result.get("cash_comment", "")
    concern   = result.get("top_concern", "")

    action_emoji = {"EXIT": "🔴 EXIT", "TRIM": "🟠 TRIM", "ADD": "🟢 ADD", "HOLD": "⚪ HOLD"}

    # Build a lookup of current positions
    pos_lookup = {p["sym"]: p for p in pos_data}

    # --- Execute trades for EXIT and TRIM ---
    executed = []
    for a in actions:
        sym  = a["symbol"]
        rec  = a["recommendation"]
        if rec not in ("EXIT", "TRIM"):
            continue
        if sym not in pos_lookup:
            continue

        pos = pos_lookup[sym]
        try:
            position_raw = next((p for p in positions if p["symbol"] == sym), None)
            if not position_raw:
                continue
            qty   = abs(float(position_raw["qty"]))
            price = float(position_raw["current_price"])

            if rec == "EXIT":
                alpaca.place_market_order(sym, qty, "SELL")
                db.log_trade(sym, "SELL", "stock", qty, price, 0, 8,
                             f"[Weekly review] {a['reason']}")
                executed.append(f"🔴 SOLD {sym} ({qty:.2f} shares @ ${price:.2f}) — {a['reason']}")
                print(f"  [Weekly] EXIT {sym} executed")

            elif rec == "TRIM":
                # Trim to max position size (8%) — sell the excess
                current_pct = pos["pct"]
                target_pct  = config.MAX_POSITION_PCT * 0.75  # trim to 75% of max
                if current_pct > config.MAX_POSITION_PCT:
                    excess_pct = current_pct - target_pct
                    trim_value = equity * excess_pct / 100
                    trim_qty   = round(trim_value / price, 6) if price else 0
                    if trim_qty > 0 and trim_qty < qty:
                        alpaca.place_market_order(sym, trim_qty, "SELL")
                        db.log_trade(sym, "SELL", "stock", trim_qty, price, excess_pct, 7,
                                     f"[Weekly trim] {a['reason']}")
                        executed.append(f"🟠 TRIMMED {sym} — sold {trim_qty:.2f} shares "
                                        f"(reduced from {current_pct:.1f}% to ~{target_pct:.1f}%) "
                                        f"— {a['reason']}")
                        print(f"  [Weekly] TRIM {sym} executed")
                    else:
                        executed.append(f"🟠 TRIM {sym} skipped — position not oversized enough")
                else:
                    executed.append(f"🟠 TRIM {sym} noted but position ({current_pct:.1f}%) within limits")
        except Exception as e:
            executed.append(f"⚠️ {rec} {sym} failed: {e}")
            print(f"  [Weekly] {rec} {sym} error: {e}")

    # --- Format Discord report ---
    immediate = [a for a in actions if a["urgency"] == "immediate"]
    this_week = [a for a in actions if a["urgency"] == "this_week"]
    monitor   = [a for a in actions if a["urgency"] == "monitor"]

    lines = ["📋 **Weekly Portfolio Review**\n"]
    lines.append(f"**Health:** {health}")
    lines.append(f"**Cash:** {cash_note}")
    lines.append(f"**Top concern:** {concern}\n")

    if executed:
        lines.append("**✅ Actions taken:**")
        for e in executed:
            lines.append(f"  {e}")
        lines.append("")

    if immediate:
        lines.append("**🔴 Immediate:**")
        for a in immediate:
            lines.append(f"  {action_emoji.get(a['recommendation'], a['recommendation'])} "
                         f"{a['symbol']} — {a['reason']}")

    if this_week:
        lines.append("\n**🟡 This week:**")
        for a in this_week:
            lines.append(f"  {action_emoji.get(a['recommendation'], a['recommendation'])} "
                         f"{a['symbol']} — {a['reason']}")

    if monitor:
        lines.append("\n**🔵 Holding:**")
        for a in monitor:
            lines.append(f"  {action_emoji.get(a['recommendation'], a['recommendation'])} "
                         f"{a['symbol']} — {a['reason']}")

    msg = "\n".join(lines)
    print(msg)
    tg.send(msg)
    db.log_summary("weekly_review", msg)
    print("\nWeekly review complete.")
