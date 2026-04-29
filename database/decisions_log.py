"""
Append-only decisions log — JSONL format, one record per line.
Tracks every committee decision, daily-review decision, and fallback decision
with enough data to compute sleeve-level attribution vs SPY.

Usage:
  from database.decisions_log import log_committee_decision, log_daily_review_decision
  log_committee_decision(symbol="NVDA", action="BUY", sleeve="long_term", ...)
  log_daily_review_decision(symbol="DDOG", action="REMOVE", criterion="catalyst_fizzled", ...)
"""
import json
import os
from datetime import datetime, timezone, timedelta

_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "trading_decisions.jsonl")


def _append(record: dict) -> None:
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    with open(_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def log_committee_decision(
    *,
    symbol: str,
    action: str,
    sleeve: str,                        # "long_term" | "medium_term" | "speculative" | ""
    confidence: int,
    regime: str,                        # "normal" | "elevated" | "stress" | "crisis"
    allocation_pct: float,
    catalyst_type: str = "",            # from taxonomy: earnings | product_launch | etc.
    catalyst_date: str = "",            # YYYY-MM-DD
    rationale: str = "",
    cio_confidence: int = 0,
    da_severity: str = "",
    crs_growth_gate: str = "",
    price_at_decision: float = 0.0,
    price_target: float = 0.0,
    source: str = "committee",
) -> None:
    _append({
        "log_type":         "committee",
        "symbol":           symbol,
        "action":           action,
        "sleeve":           sleeve,
        "confidence":       confidence,
        "cio_confidence":   cio_confidence,
        "regime":           regime,
        "allocation_pct":   allocation_pct,
        "catalyst_type":    catalyst_type,
        "catalyst_date":    catalyst_date,
        "rationale":        rationale[:200],
        "da_severity":      da_severity,
        "crs_growth_gate":  crs_growth_gate,
        "price_at_decision": price_at_decision,
        "price_target":     price_target,
        "source":           source,
        "outcome_pct":      None,       # filled in by close_decision_outcome()
        "outcome_days":     None,
    })


def log_daily_review_decision(
    *,
    symbol: str,
    action: str,                        # "KEEP" | "REMOVE"
    criterion: str = "",                # which removal rule fired
    pct_change: float = 0.0,
    rsi: float = 0.0,
    above_sma20: bool = True,
    momentum: str = "",
    days_held: int = 0,
    ttl_remaining: int = 0,
    catalyst_passed: bool = False,
    source: str = "",                   # basket source (congress_buy, etc.)
) -> None:
    _append({
        "log_type":         "daily_review",
        "symbol":           symbol,
        "action":           action,
        "criterion":        criterion,
        "pct_change":       pct_change,
        "rsi":              rsi,
        "above_sma20":      above_sma20,
        "momentum":         momentum,
        "days_held":        days_held,
        "ttl_remaining":    ttl_remaining,
        "catalyst_passed":  catalyst_passed,
        "source":           source,
    })


def close_decision_outcome(symbol: str, outcome_pct: float, hold_days: int) -> None:
    """
    Back-fills outcome_pct and outcome_days into the most recent open committee
    BUY decision for this symbol. Called when a position is closed.
    """
    if not os.path.exists(_LOG_PATH):
        return
    lines = []
    updated = False
    with open(_LOG_PATH) as f:
        raw_lines = f.readlines()
    for line in reversed(raw_lines):
        rec = json.loads(line)
        if (not updated and rec.get("log_type") == "committee"
                and rec.get("symbol") == symbol
                and rec.get("action") == "BUY"
                and rec.get("outcome_pct") is None):
            rec["outcome_pct"]  = round(outcome_pct, 2)
            rec["outcome_days"] = hold_days
            updated = True
            lines.append(json.dumps(rec) + "\n")
        else:
            lines.append(line)
    if updated:
        with open(_LOG_PATH, "w") as f:
            f.writelines(reversed(lines))


def get_recent_decisions(days: int = 30) -> list[dict]:
    """Return all log records from the last N days."""
    if not os.path.exists(_LOG_PATH):
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = []
    with open(_LOG_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("ts", "") >= cutoff:
                    out.append(rec)
            except Exception:
                pass
    return out


def compute_sleeve_attribution(days: int = 90) -> dict:
    """
    Summarise closed committee BUY decisions by sleeve.
    Returns hit_rate, avg_return, avg_days per sleeve.
    """
    records = [r for r in get_recent_decisions(days)
               if r.get("log_type") == "committee"
               and r.get("action") == "BUY"
               and r.get("outcome_pct") is not None]

    sleeves = {}
    for r in records:
        sl = r.get("sleeve", "unknown")
        if sl not in sleeves:
            sleeves[sl] = {"wins": 0, "losses": 0, "total_return": 0.0, "total_days": 0, "count": 0}
        s = sleeves[sl]
        pct = r["outcome_pct"]
        s["count"]        += 1
        s["total_return"] += pct
        s["total_days"]   += r.get("outcome_days") or 0
        if pct > 0:
            s["wins"] += 1
        else:
            s["losses"] += 1

    result = {}
    for sl, s in sleeves.items():
        n = s["count"]
        result[sl] = {
            "count":      n,
            "hit_rate":   round(s["wins"] / n * 100, 1) if n else 0,
            "avg_return": round(s["total_return"] / n, 2) if n else 0,
            "avg_days":   round(s["total_days"] / n, 1) if n else 0,
        }
    return result
