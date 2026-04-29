"""
Pre-open gap and catalyst scanner — runs at 08:45 ET, no Claude calls.

Checks:
  1. Overnight gaps >4% on held positions
  2. Earnings today/tomorrow for any held or basket ticker
  3. Speculative catalyst dates from thesis_state.json
  4. Earnings position cap — flags positions oversized into binary events

Returns an alerts dict consumed by the 09:50 trading cycle.
"""
import json
import os
from datetime import datetime, date, timedelta

import yfinance as yf

_THESIS_STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "basket", "thesis_state.json")


def _load_thesis_state() -> dict:
    try:
        with open(_THESIS_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _overnight_gap(symbol: str) -> float | None:
    """Return overnight gap % for a symbol. Positive = gap up, negative = gap down."""
    try:
        tk = yf.Ticker(symbol)
        hist = tk.history(period="5d", interval="1d")
        if len(hist) < 2:
            return None
        prev_close = float(hist["Close"].iloc[-2])
        # Use pre-market price if available, else today's open
        today_open = float(hist["Open"].iloc[-1])
        if prev_close <= 0:
            return None
        return (today_open - prev_close) / prev_close * 100
    except Exception:
        return None


def _earnings_within_days(symbol: str, days: int = 2) -> bool:
    """Quick yfinance check — earnings within N calendar days."""
    try:
        tk = yf.Ticker(symbol)
        cal = tk.calendar
        if cal is None or cal.empty:
            return False
        earnings_dates = cal.columns.tolist() if hasattr(cal, "columns") else []
        # yfinance calendar returns dict-like with 'Earnings Date' key
        if isinstance(cal, dict):
            edate = cal.get("Earnings Date")
        else:
            edate = cal.get("Earnings Date") if "Earnings Date" in cal.index else None
        if edate is None:
            return False
        if isinstance(edate, (list, tuple)):
            edate = edate[0]
        target = date.today() + timedelta(days=days)
        return date.today() <= edate.date() <= target if hasattr(edate, "date") else False
    except Exception:
        return False


def _upcoming_catalysts(thesis_state: dict) -> list[dict]:
    """Flag speculative milestones due within 30 days."""
    alerts = []
    today = date.today()
    window = timedelta(days=30)
    for ticker, state in thesis_state.items():
        if not state.get("thesis_intact", True):
            continue
        for m in state.get("milestones", []):
            if m.get("status") == "hit":
                continue
            td_str = m.get("target_date", "")
            if not td_str:
                continue
            try:
                # Parse "2025-Q3" style dates — use start of quarter
                if "-Q" in td_str:
                    year, q = td_str.split("-Q")
                    month = (int(q) - 1) * 3 + 1
                    target_dt = date(int(year), month, 1)
                else:
                    target_dt = datetime.strptime(td_str, "%Y-%m-%d").date()
                if today <= target_dt <= today + window:
                    alerts.append({
                        "type":      "catalyst",
                        "ticker":    ticker,
                        "milestone": m["name"],
                        "due":       td_str,
                    })
            except Exception:
                continue
    return alerts


def _uw_premarket_scan(held: dict, basket: list[str], config_mod) -> tuple[list, list]:
    """
    Pre-market Unusual Whales scan — runs inside the 08:45 gap scanner.
    Two outputs:
      uw_bearish_alerts  — bearish sweeps on HELD positions overnight
      uw_discovery       — large bullish sweeps on tickers NOT in our basket
    Gracefully returns ([], []) if API key not set or API unavailable.
    """
    if not getattr(config_mod, "UNUSUAL_WHALES_API_KEY", ""):
        return [], []

    try:
        from signals import options_flow as uw_flow
    except ImportError:
        return [], []

    basket_set = set(basket)

    # Bearish check on held positions
    bearish_alerts = []
    for sym in list(held.keys()):
        try:
            result = uw_flow.compute(sym)
            if result.get("bearish_alert") and result.get("normalized_prem_pct", 0) >= 70:
                bearish_alerts.append({
                    "symbol":   sym,
                    "norm_pct": result["normalized_prem_pct"],
                    "cp_ratio": result.get("call_put_ratio", "?"),
                })
        except Exception:
            pass

    # Market-wide sweep feed for out-of-basket discovery
    discovery = []
    try:
        sweeps = uw_flow.get_market_sweep_feed(min_premium=750_000, limit=100)
        for s in sweeps:
            sym = s["symbol"]
            if sym not in basket_set and sym not in held and s["side"] == "call" and s.get("is_sweep"):
                discovery.append({
                    "symbol":       sym,
                    "premium":      s["premium"],
                    "expiry_weeks": s.get("expiry_weeks"),
                })
    except Exception:
        pass

    return bearish_alerts, discovery


def scan(positions: list[dict], basket: list[str], config) -> dict:
    """
    Main entry point — call from the 08:45 pre-open job.

    Args:
        positions: list of current held positions (from alpaca.get_positions())
        basket:    current watchlist (from basket_mgr.load())
        config:    config module (for TICKER_TIERS, EARNINGS_CAP_PCT etc.)

    Returns dict:
        {
          "gap_alerts":      [{"symbol", "gap_pct", "direction"}],
          "earnings_today":  [{"symbol", "tier"}],
          "earnings_cap_flags": [{"symbol", "current_pct", "cap_pct", "tier"}],
          "catalyst_alerts": [{"ticker", "milestone", "due"}],
          "summary":         str,
        }
    """
    from basket.tier_criteria import EARNINGS_CAP_PCT

    thesis_state = _load_thesis_state()
    held = {p["symbol"]: p for p in positions}

    gap_alerts:       list[dict] = []
    earnings_today:   list[dict] = []
    earnings_cap_flags: list[dict] = []

    # ── Gap check on held positions ───────────────────────────────────────────
    for sym, pos in held.items():
        gap = _overnight_gap(sym)
        if gap is not None and abs(gap) >= 4.0:
            gap_alerts.append({
                "symbol":    sym,
                "gap_pct":   round(gap, 2),
                "direction": "up" if gap > 0 else "down",
            })

    # ── Earnings check on held + basket ───────────────────────────────────────
    check_set = set(held.keys()) | set(basket[:40])  # limit basket check to top 40
    for sym in check_set:
        if _earnings_within_days(sym, days=2):
            tier = getattr(config, "TICKER_TIERS", {}).get(sym, "mid_growth")
            earnings_today.append({"symbol": sym, "tier": tier})

            # Cap check — is held position oversized into this earnings event?
            if sym in held:
                pos = held[sym]
                portfolio_equity = float(pos.get("market_value", 0)) / max(
                    float(pos.get("unrealized_plpc", 0) / 100 + 1), 0.01
                )
                try:
                    from broker import alpaca
                    port = alpaca.get_portfolio()
                    equity = port.get("equity", 0)
                except Exception:
                    equity = 0

                if equity > 0:
                    mv = abs(float(pos.get("market_value", 0)) or
                             float(pos.get("qty", 0)) * float(pos.get("current_price", 0)))
                    current_pct = mv / equity * 100
                    cap_pct = EARNINGS_CAP_PCT.get(tier, 6.0)
                    if current_pct > cap_pct:
                        earnings_cap_flags.append({
                            "symbol":      sym,
                            "current_pct": round(current_pct, 2),
                            "cap_pct":     cap_pct,
                            "tier":        tier,
                        })

    # ── Speculative catalyst alerts ───────────────────────────────────────────
    catalyst_alerts = _upcoming_catalysts(thesis_state)

    # ── Unusual Whales pre-market scan ────────────────────────────────────────
    uw_bearish_alerts, uw_discovery = _uw_premarket_scan(held, basket, config)

    # ── Build summary ─────────────────────────────────────────────────────────
    lines = [f"[GapScan 08:45] {datetime.now().strftime('%a %b %d')}"]
    if gap_alerts:
        lines.append("GAP ALERTS: " + ", ".join(
            f"{a['symbol']} {a['gap_pct']:+.1f}%" for a in gap_alerts))
    if earnings_today:
        lines.append("EARNINGS TODAY/TOMORROW: " + ", ".join(
            f"{e['symbol']}[{e['tier']}]" for e in earnings_today))
    if earnings_cap_flags:
        lines.append("EARNINGS CAP FLAGS (oversized): " + ", ".join(
            f"{f['symbol']} {f['current_pct']:.1f}%→cap {f['cap_pct']}%" for f in earnings_cap_flags))
    if catalyst_alerts:
        lines.append("CATALYST WINDOW (30d): " + ", ".join(
            f"{c['ticker']}:{c['milestone']} ({c['due']})" for c in catalyst_alerts))
    if uw_bearish_alerts:
        lines.append("UW OVERNIGHT BEARISH SWEEPS (held): " + ", ".join(
            f"{a['symbol']} pct={a['norm_pct']} C/P={a['cp_ratio']}" for a in uw_bearish_alerts))
    if uw_discovery:
        top = sorted(uw_discovery, key=lambda x: x["premium"], reverse=True)[:5]
        lines.append("UW OUT-OF-BASKET SWEEPS (discovery): " + ", ".join(
            f"{d['symbol']} ${d['premium']/1e6:.1f}M" for d in top))
    if not any([gap_alerts, earnings_today, catalyst_alerts, uw_bearish_alerts]):
        lines.append("No alerts — clean open")

    return {
        "gap_alerts":          gap_alerts,
        "earnings_today":      earnings_today,
        "earnings_cap_flags":  earnings_cap_flags,
        "catalyst_alerts":     catalyst_alerts,
        "uw_bearish_alerts":   uw_bearish_alerts,
        "uw_discovery":        uw_discovery,
        "summary":             "\n".join(lines),
    }
