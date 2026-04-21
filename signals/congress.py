import requests
from datetime import datetime, timedelta

_TIMEOUT = 20
_HEADERS = {"User-Agent": "trading-agent/1.0"}

# Session-level cache — fetched once per process, reused for all tickers
_house_cache: list | None = None
_senate_cache: list | None = None

_HOUSE_URL  = "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"
_SENATE_URL = "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json"


def _load_house() -> list:
    global _house_cache
    if _house_cache is not None:
        return _house_cache
    try:
        r = requests.get(_HOUSE_URL, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 200:
            _house_cache = r.json()
            print(f"  [Congress] Loaded {len(_house_cache)} House trades")
            return _house_cache
    except Exception as e:
        print(f"  [Congress] House load failed: {e}")
    _house_cache = []
    return []


def _load_senate() -> list:
    global _senate_cache
    if _senate_cache is not None:
        return _senate_cache
    try:
        r = requests.get(_SENATE_URL, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 200:
            _senate_cache = r.json()
            print(f"  [Congress] Loaded {len(_senate_cache)} Senate trades")
            return _senate_cache
    except Exception as e:
        print(f"  [Congress] Senate load failed: {e}")
    _senate_cache = []
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

    for t in _load_house():
        if (t.get("ticker") or "").strip().upper() != symbol:
            continue
        d = _parse_date(t.get("transaction_date") or t.get("disclosure_date", ""))
        if d is None or d < cutoff:
            continue
        trades.append({
            "politician": t.get("representative", "?"),
            "party":      t.get("party", "?"),
            "date":       (t.get("transaction_date") or "")[:10],
            "action":     t.get("type", "?"),
            "amount":     t.get("amount", "?"),
            "chamber":    "House",
        })

    for t in _load_senate():
        if (t.get("ticker") or "").strip().upper() != symbol:
            continue
        d = _parse_date(t.get("transaction_date") or t.get("date", ""))
        if d is None or d < cutoff:
            continue
        trades.append({
            "politician": t.get("senator", "?"),
            "party":      t.get("party", "?"),
            "date":       (t.get("transaction_date") or "")[:10],
            "action":     t.get("type", "?"),
            "amount":     t.get("amount", "?"),
            "chamber":    "Senate",
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
    """Return list of tickers with net congress buying — used by basket manager."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    counts: dict[str, dict] = {}

    for t in _load_house() + _load_senate():
        sym = (t.get("ticker") or "").strip().upper()
        if not sym or len(sym) > 6 or not sym.isalpha():
            continue
        d = _parse_date(t.get("transaction_date") or t.get("disclosure_date") or t.get("date", ""))
        if d is None or d < cutoff:
            continue
        rec = counts.setdefault(sym, {"buys": 0, "sells": 0})
        action = t.get("type", "")
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
