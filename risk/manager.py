import config


def validate(decision: dict, portfolio: dict) -> dict:
    """Clamp and reject decisions that violate risk rules. Returns adjusted decision."""
    action = decision.get("action", "HOLD")
    confidence = decision.get("confidence", 0)
    allocation = decision.get("allocation_pct", 0)
    asset_type = decision.get("asset_type", "stock")

    if confidence < config.MIN_CONFIDENCE:
        return _hold(decision, f"confidence {confidence} below minimum {config.MIN_CONFIDENCE}")

    if portfolio.get("position_count", 0) >= config.MAX_POSITIONS and action == "BUY":
        return _hold(decision, "max positions reached")

    if asset_type == "option" and portfolio.get("options_pct", 0) >= config.MAX_OPTIONS_PCT:
        return _hold(decision, "max options exposure reached")

    if asset_type == "crypto" and portfolio.get("crypto_pct", 0) >= config.MAX_CRYPTO_PCT:
        return _hold(decision, "max crypto exposure reached")

    # Clamp allocation to max allowed
    allocation = min(allocation, config.MAX_POSITION_PCT)
    decision = {**decision, "allocation_pct": allocation}

    return decision


def compute_qty(symbol: str, allocation_pct: float, price: float, portfolio: dict) -> float:
    equity = portfolio.get("equity", 0)
    dollar_amount = equity * (allocation_pct / 100)
    if price <= 0:
        return 0
    qty = dollar_amount / price
    return max(round(qty, 6), 0)


def check_stops(positions: list) -> list[dict]:
    """Return list of {symbol, action} for positions hitting stop-loss or take-profit."""
    exits = []
    for p in positions:
        pct = p.get("unrealized_plpc", 0)
        if pct <= -config.STOP_LOSS_PCT:
            exits.append({"symbol": p["symbol"], "action": "SELL", "reason": "stop_loss"})
        elif pct >= config.TAKE_PROFIT_PCT:
            exits.append({"symbol": p["symbol"], "action": "SELL", "reason": "take_profit"})
    return exits


def _hold(decision: dict, reason: str) -> dict:
    return {**decision, "action": "HOLD", "allocation_pct": 0,
            "rationale": f"Risk override: {reason}"}
