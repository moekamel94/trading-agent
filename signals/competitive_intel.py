"""
Competitive intelligence — maps M&A activity, competitive threats, and supply chain signals.
"""
import config

# Supply chain map — when upstream beats, downstream likely beats too
SUPPLY_CHAIN_MAP = {
    "TSM":  ["NVDA", "AMD", "AVGO", "ARM"],    # TSMC strong → chip designers beat
    "ANET": ["NVDA", "MSFT", "META", "AMZN"],   # Arista network → hyperscalers building
    "SMCI": ["NVDA", "AMD"],                     # SuperMicro strong → NVDA demand confirmed
    "MRVL": ["AVGO", "ANET", "AMZN"],           # Marvell strong → custom silicon cycle
}

# Sector consolidation tracker — when a sector sees M&A, remaining independents get premium
ACQUISITION_PREMIUMS = {
    "cyber":   ["CRWD", "PANW", "ZS", "NET"],   # cybersecurity consolidation ongoing
    "space":   ["RKLB", "ASTS"],                 # space sector M&A cycle
    "nuclear": ["OKLO", "BWXT", "CCJ"],          # nuclear revival acquisitions
}


def get_supply_chain_signals(symbol: str) -> list[dict]:
    """
    When a supply chain upstream stock reports strong earnings,
    flag downstream companies as likely to beat.
    Returns list of {symbol, reason, signal_strength}
    """
    downstream = SUPPLY_CHAIN_MAP.get(symbol, [])
    return [
        {
            "symbol": ds,
            "reason": f"{symbol} supply chain beat — {ds} likely to benefit",
            "signal_strength": 65,
        }
        for ds in downstream
        if ds in config.TICKER_TIERS
    ]


def get_acquisition_candidates(sector: str) -> list[dict]:
    """
    After an M&A deal in a sector, remaining independents become acquisition targets.
    Returns candidates with acquisition premium signal.
    """
    candidates = ACQUISITION_PREMIUMS.get(sector, [])
    return [
        {
            "symbol": sym,
            "reason": f"M&A activity in {sector} sector — {sym} is an acquisition target",
            "signal_strength": 60,
        }
        for sym in candidates
        if sym in config.TICKER_TIERS
    ]
