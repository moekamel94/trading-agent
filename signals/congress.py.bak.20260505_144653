import requests
from datetime import datetime, timedelta

_TIMEOUT = 20
_HEADERS = {"User-Agent": "trading-agent/1.0"}

# Session-level cache — fetched once per process, reused across all tickers
_uw_congress_cache: list | None = None

_UW_BASE = "https://api.unusualwhales.com"


def _uw_api_key() -> str:
    try:
        import config
        return config.UNUSUAL_WHALES_API_KEY or ""
    except Exception:
        return ""


def _load_uw_congress() -> list:
    """
    Fetch recent congress trades from Unusual Whales (primary source).
    Returns list of normalised trade dicts with keys:
      ticker, txn_type ('Buy'/'Sell'), transaction_date, name, member_type, amounts
    """
    global _uw_congress_cache
    if _uw_congress_cache is not None:
        return _uw_congress_cache

    key = _uw_api_key()
    if not key:
        _uw_congress_cache = []
        return []

    try:
        r = requests.get(
            f"{_UW_BASE}/api/congress/recent-trades",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        if r.status_code == 200:
            _uw_congress_cache = r.json().get("data", [])
            print(f"  [Congress] Loaded {len(_uw_congress_cache)} recent trades via UW")
            return _uw_congress_cache
        print(f"  [Congress] UW returned {r.status_code}")
    except Exception as e:
        print(f"  [Congress] UW load failed: {e}")

    _uw_congress_cache = []
    return []


def _parse_date(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime((s or "")[:10], fmt)
        except ValueError:
            pass
    return None


def _is_buy(action: str) -> bool:
    a = action.lower()
    return "purchase" in a or ("buy" in a and "sale" not in a)


def _is_sell(action: str) -> bool:
    a = action.lower()
    return "sale" in a or "sell" in a


def compute(symbol: str, days: int = 60) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=days)
    trades = []

    for t in _load_uw_congress():
        if (t.get("ticker") or "").strip().upper() != symbol:
            continue
        d = _parse_date(t.get("transaction_date") or t.get("filed_at_date", ""))
        if d is None or d < cutoff:
            continue
        action = t.get("txn_type", "")
        trades.append({
            "politician": t.get("name", "?"),
            "party":      t.get("party", "?"),
            "date":       (t.get("transaction_date") or "")[:10],
            "action":     action,
            "amount":     t.get("amounts", "?"),
            "chamber":    t.get("member_type", "?").capitalize(),
        })

    buys  = sum(1 for t in trades if _is_buy(t["action"]))
    sells = sum(1 for t in trades if _is_sell(t["action"]))

    if buys > sells * 1.5:
        net = "bullish"
    elif sells > buys * 1.5:
        net = "bearish"
    else:
        net = "neutral"

    return {"trades": trades, "buys": buys, "sells": sells, "net_signal": net}


def get_recent_buys(days: int = 45) -> list[str]:
    """Return tickers with net congress buying — used by basket manager and daily discovery."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    counts: dict[str, dict] = {}

    for t in _load_uw_congress():
        sym = (t.get("ticker") or "").strip().upper()
        if not sym or len(sym) > 6 or not sym.isalpha():
            continue
        d = _parse_date(t.get("transaction_date") or t.get("filed_at_date", ""))
        if d is None or d < cutoff:
            continue
        rec = counts.setdefault(sym, {"buys": 0, "sells": 0})
        action = t.get("txn_type", "")
        if _is_buy(action):
            rec["buys"] += 1
        elif _is_sell(action):
            rec["sells"] += 1

    result = [
        sym for sym, v in counts.items()
        if v["buys"] > v["sells"] * 1.5 and v["buys"] >= 2
    ]
    if result:
        print(f"  [Congress] {len(result)} tickers with net congress buying: {result[:15]}")
    return result
