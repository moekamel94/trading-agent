"""
Basket manager — tier-keyed watchlist covering Kimmy's thesis sectors,
plus congress buys auto-added and classified by tier.

Tiers: mega | large_growth | mid_growth | speculative
"""
import json
import os
import re
from datetime import datetime

import config
from basket.tier_criteria import TIER_CRITERIA

BASKET_FILE       = os.path.join(os.path.dirname(__file__), "basket.json")
BASKET_MT_FILE    = os.path.join(os.path.dirname(__file__), "basket_mt.json")
UW_PENDING_FILE   = os.path.join(os.path.dirname(__file__), "uw_pending.json")
EXCLUDED_FILE     = os.path.join(os.path.dirname(__file__), ".basket_excluded.json")
FAIL_COUNTS_FILE  = os.path.join(os.path.dirname(__file__), ".filter_failures.json")

# Always scanned regardless of anything else
_PINNED: list[str] = []

# Tier-keyed sector watchlist — single source of truth for what Kimmy watches.
# Must stay aligned with config.TICKER_TIERS.
SECTOR_LIST: dict[str, list[str]] = {
    "mega": [
        "MSFT", "GOOGL", "META", "AMZN", "NVDA", "AAPL", "TSLA",
    ],
    "large_growth": [
        # Semiconductors
        "AMD", "AVGO", "ARM", "MRVL", "TSM", "ASML", "SNPS", "KEYS", "APH",
        # AI / Software / Cloud
        "ORCL", "PLTR", "CRM", "NOW", "AI", "ANET",
        # Cybersecurity
        "CRWD", "PANW", "ZS", "FTNT",
        # Defense
        "LMT", "RTX", "NOC", "GD", "AXON", "GE", "CACI",
        # Infrastructure / Energy
        "ETN", "PWR", "VRT", "WMB", "FCX", "RGLD", "FANG", "COP",
        # Healthcare / Fintech / E-commerce
        "LLY", "ISRG", "MA", "MSCI", "SHOP", "UBER", "COIN",
    ],
    "mid_growth": [
        # Semiconductors (equipment / memory — earlier in cycle)
        "AMAT", "LRCX", "KLAC", "MU",
        # Quantum
        "RGTI", "IBM",
        # Space
        "RKLB",
        # Nuclear
        "CCJ", "CEG", "BWXT",
        # Cybersecurity mid
        "S",
        # Fintech / Consumer
        "HOOD", "MELI", "NU",
        # Healthcare
        "DXCM", "VEEV",
        # Consumer tech
        "DUOL", "RDDT",
        # AI Infrastructure
        "PSTG", "SNOW",
        # International high-growth
        "SE", "GRAB",
        # Robotics / Automation
        "ABB", "SYM", "TRMB",
        # Energy / Commodities
        "KTOS", "ATI",
        # Clean Energy
        "FSLR",
        # Consumer
        "CELH", "CAVA",
    ],
    "speculative": [
        "IONQ",   # quantum computing — $130M revenue +200% YoY, DARPA/NVIDIA
        "MP",     # only US rare earth magnet producer — DoD + Apple
        "SOUN",   # AI voice platform — $84M revenue +90% YoY, NVIDIA partner
        "LUNR",   # NASA lunar infrastructure — $200M+ Artemis contracts
        "RXRX",   # AI drug discovery — $12B Roche milestones, 23PB data moat
        "ASTS",   # satellite-to-phone internet — binary moonshot slot (1% max)
    ],
}


def get_all_tickers() -> list[str]:
    """Flatten tier-keyed SECTOR_LIST into a deduplicated list."""
    seen: set[str] = set()
    result: list[str] = []
    for tickers in SECTOR_LIST.values():
        for t in tickers:
            if t not in seen:
                seen.add(t)
                result.append(t)
    return result


def classify_ticker(symbol: str) -> str:
    """
    Return the tier for a ticker.
    Priority: config.TICKER_TIERS → SECTOR_LIST → default mid_growth.
    """
    if symbol in config.TICKER_TIERS:
        return config.TICKER_TIERS[symbol]
    for tier, tickers in SECTOR_LIST.items():
        if symbol in tickers:
            return tier
    return "mid_growth"


def _is_us_ticker(symbol: str) -> bool:
    return bool(re.match(r"^[A-Z]{1,6}$", symbol))


def _fetch_congress_buys() -> list[str]:
    try:
        from signals.congress import get_recent_buys
        return get_recent_buys(days=45)
    except Exception as e:
        print(f"  [Basket] Congress buys fetch failed: {e}")
        return []


def _classify_congress_buy(symbol: str) -> str:
    """
    Classify a congress-bought ticker into a tier before inserting.
    Uses market cap from yfinance to determine appropriate tier.
    Falls back to mid_growth if data unavailable.
    """
    existing = classify_ticker(symbol)
    if existing != "mid_growth" or symbol in config.TICKER_TIERS:
        return existing
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info or {}
        mcap = info.get("marketCap", 0) or 0
        mega_min  = TIER_CRITERIA["mega"]["min_mcap"]
        large_min = TIER_CRITERIA["large_growth"]["min_mcap"]
        mid_min   = TIER_CRITERIA["mid_growth"]["min_mcap"]
        if mcap >= mega_min:
            return "large_growth"   # congress buy on a mega = treat as large_growth (already known)
        elif mcap >= large_min:
            return "large_growth"
        elif mcap >= mid_min:
            return "mid_growth"
        else:
            return "mid_growth"    # small cap congress buy — mid_growth until proven otherwise
    except Exception:
        return "mid_growth"


def refresh() -> list[str]:
    print("  [Basket] Building tier-keyed sector basket + congress buys...")

    cong_buys = _fetch_congress_buys()
    cong_classified: dict[str, str] = {}
    for sym in cong_buys:
        cong_classified[sym] = _classify_congress_buy(sym)

    all_sector = get_all_tickers()

    seen: set[str] = set()
    merged: list[str] = []
    for sym in _PINNED + all_sector:
        s = sym.strip().upper()
        if s and s not in seen:
            seen.add(s)
            merged.append(s)
    for sym in cong_buys:
        s = sym.strip().upper()
        if s and s not in seen:
            seen.add(s)
            merged.append(s)

    # Register congress buys in config.TICKER_TIERS if not already classified
    for sym, tier in cong_classified.items():
        if sym not in config.TICKER_TIERS:
            config.TICKER_TIERS[sym] = tier

    tier_counts = {t: len(v) for t, v in SECTOR_LIST.items()}
    data = {
        "updated": datetime.utcnow().isoformat(),
        "tickers": merged,
        "sources": {
            "tier_counts":  tier_counts,
            "congress":     len(cong_buys),
            "pinned":       _PINNED,
            "total":        len(merged),
        },
    }
    with open(BASKET_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"  [Basket] Total watchlist: {len(merged)} tickers "
          f"(mega={tier_counts['mega']} large={tier_counts['large_growth']} "
          f"mid={tier_counts['mid_growth']} spec={tier_counts['speculative']} "
          f"congress={len(cong_buys)})")
    return merged


_EXCLUSION_DAYS = 30   # ticker is re-eligible after this many days


def _load_excluded_raw() -> dict:
    """Return {ticker: iso_date_excluded} dict."""
    try:
        return json.load(open(EXCLUDED_FILE))
    except Exception:
        return {}


def load_excluded() -> set:
    """Return the set of tickers still within their 30-day cooldown window."""
    from datetime import date
    raw = _load_excluded_raw()
    today = date.today()
    active = set()
    for sym, date_str in raw.items():
        try:
            excluded_on = datetime.strptime(date_str, "%Y-%m-%d").date()
            if (today - excluded_on).days < _EXCLUSION_DAYS:
                active.add(sym)
        except Exception:
            active.add(sym)   # malformed date — keep excluded to be safe
    return active


def add_excluded(tickers: list[str]) -> None:
    """Add tickers to the 30-day cooldown list. Re-exclusion resets the clock."""
    raw = _load_excluded_raw()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    for t in tickers:
        raw[t.upper()] = today
    json.dump(raw, open(EXCLUDED_FILE, "w"), indent=2)


def remove_excluded(tickers: list[str]) -> None:
    """Manually lift the cooldown for specific tickers (e.g. after a turnaround)."""
    raw = _load_excluded_raw()
    for t in tickers:
        raw.pop(t.upper(), None)
    json.dump(raw, open(EXCLUDED_FILE, "w"), indent=2)


def load() -> list[str]:
    excluded = load_excluded()
    if os.path.exists(BASKET_FILE):
        try:
            with open(BASKET_FILE) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            print("[Basket] basket.json corrupted — rebuilding")
            return refresh()
        tickers = [t for t in data.get("tickers", []) if t not in excluded]
        for sym in _PINNED:
            if sym not in tickers and sym not in excluded:
                tickers.insert(0, sym)
        return tickers
    return refresh()


def load_mt() -> list[str]:
    """Load the medium-term catalyst basket. Returns list of ticker symbols."""
    excluded = load_excluded()
    if os.path.exists(BASKET_MT_FILE):
        try:
            with open(BASKET_MT_FILE) as f:
                data = json.load(f)
            return [t for t in data.get("tickers", []) if t not in excluded]
        except (json.JSONDecodeError, OSError):
            return []
    return []


def load_mt_metadata() -> dict:
    """Load MT basket with full metadata {symbol: {source, added, expires, note}}."""
    if os.path.exists(BASKET_MT_FILE):
        try:
            with open(BASKET_MT_FILE) as f:
                data = json.load(f)
            return data.get("metadata", {})
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def load_combined() -> list[str]:
    """Return deduplicated union of LT basket + MT basket for scanning."""
    lt = load()
    mt = load_mt()
    seen: set = set()
    result: list = []
    for sym in lt + mt:
        if sym not in seen:
            seen.add(sym)
            result.append(sym)
    return result


def save_mt(tickers: list[str], metadata: dict) -> None:
    """Persist the MT basket to disk."""
    data = {
        "updated":  datetime.utcnow().isoformat(),
        "tickers":  list(dict.fromkeys(tickers)),   # dedup preserving order
        "metadata": metadata,
        "sources": {
            s: len([t for t, m in metadata.items() if m.get("source") == s])
            for s in ("congress_buy", "earnings_catalyst", "sector_rotation", "uw_discovery")
        },
    }
    with open(BASKET_MT_FILE, "w") as f:
        json.dump(data, f, indent=2)

    src = data["sources"]
    print(f"  [MT Basket] {len(tickers)} tickers — "
          f"congress={src['congress_buy']} earnings={src['earnings_catalyst']} "
          f"sector={src['sector_rotation']} uw={src['uw_discovery']}")


def queue_uw_discovery(discoveries: list[dict]) -> None:
    """
    Queue out-of-basket UW discoveries for the next MT weekly refresh.
    discoveries: list of {symbol, premium, expiry_weeks, timestamp}
    Existing entries are kept; duplicates update the timestamp.
    """
    pending = {}
    if os.path.exists(UW_PENDING_FILE):
        try:
            with open(UW_PENDING_FILE) as f:
                pending = json.load(f)
        except Exception:
            pending = {}

    for d in discoveries:
        sym = d.get("symbol", "").upper()
        if not sym:
            continue
        # Update or add — keep the most recent sweep data
        pending[sym] = {
            "symbol":       sym,
            "premium":      d.get("premium", 0),
            "expiry_weeks": d.get("expiry_weeks"),
            "queued_at":    datetime.utcnow().isoformat(),
        }

    with open(UW_PENDING_FILE, "w") as f:
        json.dump(pending, f, indent=2)


def get_uw_pending() -> list[dict]:
    """Return all pending UW discoveries not yet processed into MT basket."""
    if not os.path.exists(UW_PENDING_FILE):
        return []
    try:
        with open(UW_PENDING_FILE) as f:
            data = json.load(f)
        return list(data.values())
    except Exception:
        return []


def clear_uw_pending() -> None:
    """Clear the UW pending queue after it has been consumed by MT curation."""
    if os.path.exists(UW_PENDING_FILE):
        os.remove(UW_PENDING_FILE)


def needs_refresh() -> bool:
    if not os.path.exists(BASKET_FILE):
        return True
    with open(BASKET_FILE) as f:
        data = json.load(f)
    updated  = datetime.fromisoformat(data.get("updated", "2000-01-01"))
    days_old = (datetime.utcnow() - updated).days
    return days_old >= 30


def _fallback() -> list[str]:
    return _PINNED + get_all_tickers()
