import config


# ---------------------------------------------------------------------------
# Hard entry gate — ALL criteria must pass or BUY is blocked before Claude
# ---------------------------------------------------------------------------

def check_entry_criteria(signals: dict) -> tuple[bool, str]:
    """
    Returns (passes: bool, reason: str).
    Every condition must be True for a BUY to proceed.
    """
    tech  = signals.get("technical", {})
    sent  = signals.get("sentiment", {})
    fund  = signals.get("fundamentals", {})

    rsi = tech.get("rsi")
    if rsi is not None:
        if rsi < config.CRITERIA_RSI_MIN:
            return False, f"RSI {rsi:.1f} below floor {config.CRITERIA_RSI_MIN} (panic/crash territory)"
        if rsi > config.CRITERIA_RSI_MAX:
            return False, f"RSI {rsi:.1f} above ceiling {config.CRITERIA_RSI_MAX} (overbought)"

    if config.CRITERIA_PRICE_ABOVE_SMA50:
        price  = tech.get("price")
        sma50  = tech.get("sma50")
        if price is not None and sma50 is not None and price < sma50:
            return False, f"Price {price:.2f} below SMA50 {sma50:.2f} (downtrend)"

    if config.CRITERIA_MACD_NOT_BEARISH:
        if tech.get("macd_cross") == "bearish":
            return False, "MACD bearish crossover — momentum turning down"

    eps_growth = fund.get("eps_growth_yoy")
    if eps_growth is not None and eps_growth < config.CRITERIA_EPS_GROWTH_MIN:
        return False, f"EPS growth {eps_growth*100:.1f}% below minimum {config.CRITERIA_EPS_GROWTH_MIN*100:.0f}%"

    rev_growth = fund.get("revenue_growth")
    if rev_growth is not None and rev_growth < config.CRITERIA_REVENUE_GROWTH_MIN:
        return False, f"Revenue growth {rev_growth*100:.1f}% below minimum {config.CRITERIA_REVENUE_GROWTH_MIN*100:.0f}%"

    margin = fund.get("profit_margin")
    if margin is not None and margin < config.CRITERIA_PROFIT_MARGIN_MIN:
        return False, f"Profit margin {margin*100:.1f}% below minimum {config.CRITERIA_PROFIT_MARGIN_MIN*100:.0f}%"

    pe = fund.get("pe_ratio")
    if pe is not None and pe > config.CRITERIA_PE_MAX:
        return False, f"P/E {pe:.1f} above maximum {config.CRITERIA_PE_MAX} (overvalued)"

    if config.CRITERIA_SENTIMENT_NOT_NEG:
        if sent.get("label") == "negative":
            return False, "Negative news sentiment — headwind risk"

    return True, "all criteria passed"


# ---------------------------------------------------------------------------
# Hard exit gate — checked against open positions each cycle
# ---------------------------------------------------------------------------

def check_stops(positions: list, signals_map: dict = None) -> list[dict]:
    """Return list of {symbol, action, reason} for positions that must be exited."""
    exits = []
    signals_map = signals_map or {}

    for p in positions:
        sym = p["symbol"]
        pct = p.get("unrealized_plpc", 0)

        if pct <= -config.STOP_LOSS_PCT:
            exits.append({"symbol": sym, "action": "SELL", "reason": "stop_loss"})
            continue

        if pct >= config.TAKE_PROFIT_PCT:
            exits.append({"symbol": sym, "action": "SELL", "reason": "take_profit"})
            continue

        # Technical exit signals (only if we have fresh signal data)
        tech = signals_map.get(sym, {}).get("technical", {})

        if config.EXIT_MACD_BEARISH_CROSS and tech.get("macd_cross") == "bearish" and pct > 0:
            exits.append({"symbol": sym, "action": "SELL", "reason": "MACD bearish crossover on profitable position"})
            continue

        rsi = tech.get("rsi")
        if rsi and rsi > config.EXIT_RSI_OVERBOUGHT and pct > 0:
            exits.append({"symbol": sym, "action": "SELL", "reason": f"RSI {rsi:.0f} overbought — trimming profit"})

    return exits


# ---------------------------------------------------------------------------
# Post-Claude validation — enforce position limits and exposure caps
# ---------------------------------------------------------------------------

def validate(decision: dict, portfolio: dict) -> dict:
    action     = decision.get("action", "HOLD")
    confidence = decision.get("confidence", 0)
    allocation = decision.get("allocation_pct", 0)
    asset_type = decision.get("asset_type", "stock")

    if confidence < config.MIN_CONFIDENCE:
        return _hold(decision, f"confidence {confidence}/10 below minimum {config.MIN_CONFIDENCE}/10")

    if portfolio.get("position_count", 0) >= config.MAX_POSITIONS and action == "BUY":
        return _hold(decision, "max open positions reached")

    if asset_type == "option" and portfolio.get("options_pct", 0) >= config.MAX_OPTIONS_PCT:
        return _hold(decision, "max options exposure reached")

    if asset_type == "crypto" and portfolio.get("crypto_pct", 0) >= config.MAX_CRYPTO_PCT:
        return _hold(decision, "max crypto exposure reached")

    # Scale allocation by confidence: 7->3%, 8->4%, 9-10->5%
    if action == "BUY":
        conf_alloc = {7: 3.0, 8: 4.0, 9: 5.0, 10: 5.0}
        max_alloc = conf_alloc.get(min(confidence, 10), 3.0)
        allocation = min(allocation, max_alloc, config.MAX_POSITION_PCT)
        decision = {**decision, "allocation_pct": allocation}

    return decision


def compute_qty(symbol: str, allocation_pct: float, price: float, portfolio: dict) -> float:
    equity = portfolio.get("equity", 0)
    dollar_amount = equity * (allocation_pct / 100)
    if price <= 0:
        return 0
    return max(round(dollar_amount / price, 6), 0)


def _hold(decision: dict, reason: str) -> dict:
    return {**decision, "action": "HOLD", "allocation_pct": 0,
            "rationale": f"Blocked: {reason}"}
