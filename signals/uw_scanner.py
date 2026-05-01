"""
Intraday Unusual Whales scanner — runs every 15 min during market hours.

Schedule (wired into main.py APScheduler):
  Every  5 min  9:30–16:00  sweep_feed poll  →  basket/held hit alerts
  Every 15 min  9:30–16:00  basket refresh   →  flow + darkpool for all tickers
  Every 30 min  9:30–16:00  full snapshot    →  flow + darkpool + IV + OI + short
  10:00 daily               discovery scan   →  S&P 100 darkpool for new names

Expected daily API call budget:
  Sweep feed    :  78  (78 × 1)
  Flow refresh  : 1,248 (26 × 48)
  Darkpool      :   624 (13 × 48 at 30-min TTL)
  IV + OI       : 2,496 (26 × 48 × 2 at 15-min TTL)
  Short interest:   312 ( 6.5 × 48 at 1-h TTL)
  Discovery     :   100 (100 tickers × 1 darkpool)
  ─────────────────────
  Total target  : ~4,900–5,200/day (25% of 20K limit)
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from signals import options_flow as uw_flow

# ── Alert dedup ────────────────────────────────────────────────────────────────
# Tracks recently Discord-alerted (symbol, signal) pairs so we don't spam.
_alert_cooldown: dict[str, datetime] = {}  # key = "SYMBOL:signal_type"
_ALERT_COOLDOWN_MIN = 60  # don't re-alert same ticker+signal within 60 min


def _should_alert(key: str) -> bool:
    last = _alert_cooldown.get(key)
    if last and (datetime.utcnow() - last).total_seconds() < _ALERT_COOLDOWN_MIN * 60:
        return False
    _alert_cooldown[key] = datetime.utcnow()
    return True


# ── Sweep feed scanner ─────────────────────────────────────────────────────────

def run_sweep_feed_scan(
    basket: list[str], held: list[str]
) -> tuple[list[str], list[dict]]:
    """
    Poll the market-wide sweep feed.

    Returns (messages, discoveries):
      messages    — Discord alert strings for basket/held hits
      discoveries — out-of-basket call sweeps ≥$1M as dicts:
                    {symbol, premium, expiry_weeks, side, source}
                    Caller is responsible for queuing discoveries to the MT basket.

    Called every 5 min during market hours.
    Cost: 1 API call per run = 78 calls/day.
    """
    if not config.UNUSUAL_WHALES_API_KEY:
        return [], []

    messages: list[str]    = []
    discoveries: list[dict] = []
    try:
        feed = uw_flow.get_market_sweep_feed(min_premium=250_000, limit=200)
    except Exception as e:
        print(f"  [UW-scanner] sweep feed error: {e}")
        return [], []

    watch = set(s.upper() for s in basket + held)

    for item in feed:
        sym   = (item.get("symbol") or "").upper()
        side  = item.get("side", "")
        prem  = item.get("premium", 0)
        exp   = item.get("expiry_weeks")
        sweep = item.get("is_sweep", False)

        if not sym or not sweep:
            continue

        if sym in watch:
            key = f"{sym}:sweep_{side}"
            if _should_alert(key):
                tag     = "BASKET" if sym in set(s.upper() for s in basket) else "HELD"
                exp_str = f" {exp:.1f}w exp" if exp else ""
                messages.append(
                    f"[UW SWEEP | {tag}] **{sym}** {side.upper()} sweep "
                    f"${prem/1e6:.2f}M{exp_str}"
                )
        elif prem >= 1_000_000 and side == "call":
            # Large out-of-basket CALL sweep — discovery candidate
            key = f"{sym}:discovery_call"
            if _should_alert(key):
                exp_str = f" {exp:.1f}w exp" if exp else ""
                messages.append(
                    f"[UW DISCOVERY] **{sym}** CALL sweep "
                    f"${prem/1e6:.2f}M{exp_str} — not in basket"
                )
                discoveries.append({
                    "symbol":       sym,
                    "premium":      prem,
                    "expiry_weeks": exp,
                    "side":         side,
                    "source":       "uw_sweep_feed",
                })

    return messages, discoveries


# ── Basket refresh ─────────────────────────────────────────────────────────────

def run_basket_refresh(basket: list[str], held: list[str]) -> dict[str, dict]:
    """
    Refresh UW flow + darkpool for every basket and held ticker.
    During market hours the reduced TTLs in options_flow ensure each call
    actually hits the API (not a cache hit), driving ~1,800+ calls/day.

    Called every 15 min during market hours.
    Returns {symbol: snapshot} for use by callers (e.g., to update committee context).
    """
    universe = list(dict.fromkeys(basket + held))  # deduplicate, preserve order
    snapshots: dict[str, dict] = {}

    for sym in universe:
        try:
            snap = uw_flow.get_ticker_snapshot(sym)
            snapshots[sym] = snap
            _log_notable(sym, snap)
        except Exception as e:
            print(f"  [UW-scanner] snapshot error {sym}: {e}")

    return snapshots


def _log_notable(symbol: str, snap: dict) -> None:
    """Print summary line for tickers with notable signals."""
    flow   = snap.get("flow_signal", "")
    dp     = snap.get("darkpool_signal", "")
    prem   = snap.get("net_flow_prem", 0) or 0
    if flow in ("bullish_sweep", "bearish_sweep") or dp in ("strong_accumulation",):
        print(
            f"  [UW-scanner] {symbol}: flow={flow} dp={dp} "
            f"net_prem=${prem/1e6:.2f}M sweep_count={snap.get('sweep_count_7d', 0)}"
        )


# ── Discovery scan ─────────────────────────────────────────────────────────────

# S&P 100 tickers — broad universe for darkpool discovery
_SP100 = [
    "AAPL","MSFT","AMZN","NVDA","GOOGL","META","TSLA","BRK-B","UNH","LLY",
    "JPM","V","XOM","AVGO","MA","JNJ","PG","HD","COST","MRK",
    "ABBV","CVX","CRM","AMD","NFLX","KO","PEP","TMO","LIN","ORCL",
    "MCD","ACN","WMT","ABT","CSCO","DIS","NKE","TXN","PM","DHR",
    "NEE","UNP","RTX","HON","AMGN","INTC","QCOM","UPS","GS","CAT",
    "IBM","SPGI","INTU","BLK","SYK","GILD","MS","BA","SCHW","ELV",
    "MDT","CVS","MO","CI","ZTS","REGN","ISRG","ADP","TGT","MMM",
    "DE","PYPL","GE","F","GM","PLD","AMT","CCI","EQIX","PFE",
    "WFC","BAC","C","USB","AXP","COF","TFC","CME","ICE","MCO",
    "AON","MMC","CB","ALL","MET","PRU","AFL","CINF","HIG","AIG",
]


def run_discovery_scan(basket: list[str]) -> list[dict]:
    """
    Darkpool scan across the S&P 100 for institutional accumulation.
    Only returns tickers NOT already in basket and showing accumulation signals.
    Runs once daily at 10:00 ET. Cost: ~100 API calls.
    """
    basket_set = set(s.upper() for s in basket)
    candidates = [s for s in _SP100 if s not in basket_set]

    hits = uw_flow.scan_discovery_universe(candidates)
    if hits:
        print(
            f"  [UW-scanner] discovery scan: {len(hits)} accumulation signals "
            f"from {len(candidates)} tickers scanned"
        )
    return hits


def discovery_discord_lines(hits: list[dict]) -> list[str]:
    """Format discovery scan results for Discord."""
    if not hits:
        return []
    lines = ["**[UW DISCOVERY SCAN]** Darkpool accumulation outside basket:"]
    for h in hits[:10]:  # cap at 10 to avoid Discord spam
        notional = h.get("total_notional_3d", 0)
        notional_str = f"${notional/1e6:.1f}M notional" if notional else ""
        large = h.get("large_print_count", 0)
        lines.append(
            f"  **{h['symbol']}** {h['darkpool_signal']} — "
            f"{large} large prints {notional_str}"
        )
    return lines


# ── Daily stats summary ────────────────────────────────────────────────────────

_scan_stats: dict = {"sweep_feed_calls": 0, "basket_refresh_calls": 0, "started": None}


def reset_daily_stats() -> None:
    _scan_stats.update({"sweep_feed_calls": 0, "basket_refresh_calls": 0,
                        "started": datetime.utcnow().isoformat()})


def get_daily_stats() -> dict:
    return dict(_scan_stats)
