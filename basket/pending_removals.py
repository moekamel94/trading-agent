"""
PM 24-hour veto window for daily basket review removals.

Proposed removals are saved here. The next day's review applies them only after
24 hours have elapsed without a PM veto. Mohammed can type /veto_removal TICKER
in Discord to cancel any pending removal.
"""
import json
import os
from datetime import datetime, timezone, timedelta

_PATH = os.path.join(os.path.dirname(__file__), "pending_removals.json")


def _load() -> dict:
    try:
        with open(_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict) -> None:
    with open(_PATH, "w") as f:
        json.dump(data, f, indent=2)


def propose(symbol: str, reason: str, criterion: str = "") -> None:
    """Record a proposed removal. Overwrites if already pending."""
    data = _load()
    data[symbol.upper()] = {
        "proposed_ts": datetime.now(timezone.utc).isoformat(),
        "reason":      reason,
        "criterion":   criterion,
    }
    _save(data)


def get_all() -> dict:
    """Return all pending removals as {symbol: {proposed_ts, reason, criterion}}."""
    return _load()


def get_ready(hours: int = 24) -> list[dict]:
    """Return removals whose veto window has closed (≥ hours old)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for sym, info in _load().items():
        try:
            ts = datetime.fromisoformat(info["proposed_ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts <= cutoff:
                out.append({"symbol": sym, **info})
        except Exception:
            pass
    return out


def cancel(symbol: str) -> bool:
    """PM veto — remove a symbol from pending. Returns True if it was pending."""
    data = _load()
    sym = symbol.upper()
    if sym in data:
        del data[sym]
        _save(data)
        return True
    return False


def clear_symbols(symbols: list[str]) -> None:
    """Remove applied symbols from the pending file."""
    data = _load()
    for s in symbols:
        data.pop(s.upper(), None)
    _save(data)


def summary_lines() -> list[str]:
    """Discord-friendly list of currently pending removals."""
    data = _load()
    if not data:
        return []
    lines = []
    now = datetime.now(timezone.utc)
    for sym, info in data.items():
        try:
            ts = datetime.fromisoformat(info["proposed_ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            hours_left = max(0, 24 - (now - ts).total_seconds() / 3600)
            lines.append(
                f"• **{sym}** — {info['reason'][:60]}  "
                f"_(applies in {hours_left:.0f}h unless vetoed)_"
            )
        except Exception:
            lines.append(f"• **{sym}** — {info.get('reason', '')[:60]}")
    return lines
