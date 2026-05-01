"""
Continuous learning tracker.

Two jobs:
  1. outcome_update()  — called once daily. Fetches price for every pending
     signal_outcome entry that is now ≥7 or ≥14 days old, then writes the
     return. No external calls on the main trading cycle.

  2. compute_weights() — derives per-signal hit rates from resolved outcomes
     and writes adaptive weights back to signal_weights in the DB. A signal
     with a 70% hit rate gets weight 1.30; 40% hit rate gets 0.80. Weights
     are bounded [0.60, 1.50] so no single signal can dominate or disappear.

  3. conviction_adjustment(signals) — lightweight lookup called per ticker
     during signal collection. Returns a float (−1.5 to +1.5) to add to the
     committee conviction score, based on which signals fired and their current
     adaptive weight.

Hit definition: return_14d > 0 (stock is up 14 days after entry).
Positive-only: we're a long-only fund, so bearish signals are evaluated by
whether a non-entry (skip) avoided a loss — tracked separately as "avoided_loss".
"""
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import yfinance as yf

import config
from database import db

_WEIGHTS_PATH = os.path.join(os.path.dirname(__file__), "weights.json")

# Signals tracked for hit-rate learning
_SIGNAL_SOURCES = [
    "uw_flow",        # bullish_sweep / bearish_sweep / bullish_lean / bearish_lean
    "uw_darkpool",    # strong_accumulation / accumulation
    "congress",       # bullish (buy signal from congress)
    "insider",        # bullish (insider buying)
    "sentiment",      # positive (news/social positive)
]

# Weight bounds — prevents a single signal from taking over or vanishing
_WEIGHT_MIN = 0.60
_WEIGHT_MAX = 1.50
_MIN_SAMPLES = 10    # require ≥10 resolved outcomes before adjusting weight


def outcome_update() -> int:
    """
    Fill in price_7d / price_14d for pending signal_outcome entries.
    Called once per day (e.g., in run_cycle pre-loop).
    Returns number of outcomes updated.
    """
    pending = db.get_pending_outcomes()
    if not pending:
        return 0

    now = datetime.now(timezone.utc)
    updated = 0

    for row in pending:
        entry_dt = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)
        age_days = (now - entry_dt).days

        if age_days < 7:
            continue  # too early for even 7-day outcome

        sym = row["symbol"]
        try:
            hist = yf.Ticker(sym).history(period="30d")
            if hist.empty:
                continue
            prices = hist["Close"].tolist()
            # Index 0 = oldest (≈ entry day), latest = most recent
            # We want prices at +7d and +14d from entry
            # hist covers the last 30 calendar days; entry was `age_days` ago
            # Map calendar days to trading-day indices (approx 5/7 ratio)
            trading_days_7  = max(1, round(7  * 5 / 7))
            trading_days_14 = max(1, round(14 * 5 / 7))

            n = len(prices)
            # Prices from entry forward: the entry was `age_days` calendar days ago
            entry_idx = max(0, n - round(age_days * 5 / 7) - 1)
            idx_7d    = min(n - 1, entry_idx + trading_days_7)
            idx_14d   = min(n - 1, entry_idx + trading_days_14)

            p7  = round(float(prices[idx_7d]),  4) if age_days >= 7  else None
            p14 = round(float(prices[idx_14d]), 4) if age_days >= 14 else None

            if p7 is not None or p14 is not None:
                db.update_signal_outcomes(sym, p7, p14)
                updated += 1
        except Exception as e:
            print(f"  [Tracker] outcome_update error {sym}: {e}")

    if updated:
        print(f"  [Tracker] Updated {updated} signal outcome(s)")
    return updated


def compute_weights() -> dict[str, float]:
    """
    Derive adaptive signal weights from the last 90 days of resolved outcomes.
    Hit = return_14d > 0.  Only evaluates rows where that signal was bullish.

    Writes weights to DB (signal_weights table) and to weights.json.
    Returns {source: weight} dict.
    """
    outcomes = db.get_outcomes_for_review(days=90)
    if not outcomes:
        return _load_weights()

    # Signal → list of (hit: bool, ts: str) for rows where that signal fired bullish
    hits: dict[str, list[tuple[bool, str]]] = {s: [] for s in _SIGNAL_SOURCES}

    for row in outcomes:
        if row.get("return_14d") is None:
            continue
        win = row["return_14d"] > 0
        ts  = row.get("ts", "")

        # UW flow
        flow = row.get("uw_flow", "")
        if flow in ("bullish_sweep", "bullish_lean"):
            hits["uw_flow"].append((win, ts))

        # UW darkpool
        dp = row.get("uw_darkpool", "")
        if dp in ("strong_accumulation", "accumulation"):
            hits["uw_darkpool"].append((win, ts))

        # Congress
        if row.get("congress_signal") == "bullish":
            hits["congress"].append((win, ts))

        # Insider
        if row.get("insider_signal") == "bullish":
            hits["insider"].append((win, ts))

        # Sentiment
        if row.get("sentiment_label") == "positive":
            hits["sentiment"].append((win, ts))

    cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    weights: dict[str, float] = {}
    for source, hit_list in hits.items():
        n = len(hit_list)
        if n < _MIN_SAMPLES:
            weights[source] = _load_weights().get(source, 1.0)
            db.upsert_signal_weight(source, weights[source], None, None, n)
            continue

        rate = sum(h for h, _ in hit_list) / n
        # Linear mapping: 50% hit rate → weight 1.0, 70% → 1.4, 35% → 0.70
        raw_weight = 0.60 + (rate / 0.70) * 0.90   # at 70% hit rate → 1.50
        w = round(min(_WEIGHT_MAX, max(_WEIGHT_MIN, raw_weight)), 3)
        weights[source] = w

        # 30-day subset: only rows whose entry timestamp is within the last 30 days
        recent = [h for h, ts in hit_list if ts >= cutoff_30d]
        rate_30 = sum(recent) / len(recent) if recent else rate

        db.upsert_signal_weight(source, w, round(rate_30, 3), round(rate, 3), n)
        print(f"  [Tracker] {source}: hit_rate={rate:.1%} n={n} → weight={w:.3f}")

    _save_weights(weights)
    return weights


def conviction_adjustment(signals: dict) -> float:
    """
    Return a small conviction score adjustment (−1.5 to +1.5) based on which
    signals fired and their current adaptive weights.  Called per ticker during
    signal collection.  Reads from cached weights.json (no DB call on hot path).

    Positive = boost (signals are credible AND bullish).
    Negative = drag (credible signals are bearish).
    """
    weights = _load_weights()
    if not weights:
        return 0.0

    adj = 0.0
    uw = signals.get("options_flow", {})

    # UW flow
    flow = uw.get("flow_signal", "")
    w_uw = weights.get("uw_flow", 1.0)
    if flow in ("bullish_sweep", "bullish_lean"):
        adj += 0.5 * w_uw
    elif flow in ("bearish_sweep", "bearish_lean"):
        adj -= 0.5 * w_uw

    # UW darkpool
    dp = (uw.get("darkpool") or {}).get("darkpool_signal", "")
    w_dp = weights.get("uw_darkpool", 1.0)
    if dp in ("strong_accumulation", "accumulation"):
        adj += 0.4 * w_dp

    # Congress
    cong = signals.get("congressional", {}).get("net_signal", "")
    w_cong = weights.get("congress", 1.0)
    if cong == "bullish":
        adj += 0.4 * w_cong
    elif cong == "bearish":
        adj -= 0.3 * w_cong

    # Insider
    insd = signals.get("insider", {}).get("signal", "")
    w_insd = weights.get("insider", 1.0)
    if insd == "bullish":
        adj += 0.3 * w_insd

    # Sentiment
    sent = signals.get("sentiment", {}).get("label", "")
    w_sent = weights.get("sentiment", 1.0)
    if sent == "positive":
        adj += 0.2 * w_sent
    elif sent == "negative":
        adj -= 0.2 * w_sent

    return round(max(-1.5, min(1.5, adj)), 2)


def get_accuracy_summary() -> dict:
    """Return a human-readable summary of signal accuracy for the review report."""
    summary = {}
    try:
        import sqlite3
        path = os.path.join(os.path.dirname(__file__), "..", "trading.db")
        with sqlite3.connect(path) as c:
            rows = c.execute(
                "SELECT signal_source, weight, hit_rate_30d, hit_rate_90d, sample_count FROM signal_weights"
            ).fetchall()
            for row in rows:
                summary[row[0]] = {
                    "weight":       row[1],
                    "hit_rate_30d": row[2],
                    "hit_rate_90d": row[3],
                    "samples":      row[4],
                }
    except Exception:
        pass
    # Fill in any missing sources with defaults
    for source in _SIGNAL_SOURCES:
        if source not in summary:
            summary[source] = {"weight": 1.0, "hit_rate_30d": None, "hit_rate_90d": None, "samples": 0}
    return summary


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_weights() -> dict[str, float]:
    try:
        with open(_WEIGHTS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_weights(weights: dict[str, float]) -> None:
    try:
        with open(_WEIGHTS_PATH, "w") as f:
            json.dump(weights, f, indent=2)
    except Exception as e:
        print(f"  [Tracker] Failed to save weights: {e}")
