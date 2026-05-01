"""
Unusual Whales options flow signal — full data extraction.

Per-ticker signals (compute):
  flow_signal              : "bullish_sweep" | "bearish_sweep" | "bullish_lean" | "bearish_lean" | "neutral" | "no_data"
  normalized_prem_pct      : 0-100 (premium vs ticker's own 30-day rolling baseline)
  expiry_alignment_score   : 1.0 (3-6w), 0.5 (6-10w), 0.0 (outside)
  call_put_ratio           : float (premium-weighted)
  sweep_count_7d           : int (qualifying sweeps in last 7 days)
  net_flow_prem            : float (call_prem - put_prem in USD — directional bet size)
  short_interest_pct       : float (% of float sold short)
  borrow_rate              : float (annualised cost to borrow, %)
  short_squeeze_score      : "high" | "moderate" | "low" | "no_data"
  iv_rank                  : int 0-100 (0=cheapest, 100=most expensive in 52w)
  iv_percentile            : int 0-100
  implied_move_pct         : float (expected ±% move from ATM straddle pricing)

Market-wide context (get_uw_market_context — once per cycle, cached 30 min):
  market_tide              : "bullish" | "bearish" | "neutral" | "no_data"
  market_net_flow_bias     : float (net call-put premium ratio, market-wide)
  market_put_call_ratio    : float
  sector_flows             : dict sector → "bullish"|"bearish"|"neutral"
  spy_flow                 : flow_signal for SPY (market proxy)
  qqq_flow                 : flow_signal for QQQ (tech proxy)

SHADOW MODE:
  Bullish +0.5 bonus gated until 20+ signals with ≥55% hit rate.
  Bearish -0.5 penalty always live. All signals logged for validation.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, date
import requests

import config

_TIMEOUT = 15
_BASE_URL = "https://api.unusualwhales.com"

# Per-ticker caches
_baseline_cache:      dict[str, dict] = {}   # {symbol: {ts, p30, p90}}
_darkpool_cache:      dict[str, dict] = {}   # {symbol: {ts, result}}
_short_cache:         dict[str, dict] = {}   # {symbol: {ts, result}}
_iv_cache:            dict[str, dict] = {}   # {symbol: {ts, result}}
_oi_cache:            dict[str, dict] = {}   # {symbol: {ts, result}}
_flow_cache:          dict[str, dict] = {}   # {symbol: {ts, trades}} — short TTL dedup cache

# Market-wide context cache
_market_context_cache: dict = {}

# VIX term structure cache
_vts_cache: dict = {}

_SHADOW_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "uw_shadow.db")


def _is_market_hours() -> bool:
    """True during regular trading hours Mon-Fri 9:30-16:00 ET."""
    try:
        import zoneinfo
        now = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
        if now.weekday() >= 5:
            return False
        t = now.hour * 60 + now.minute
        return 570 <= t <= 960  # 9:30 to 16:00
    except Exception:
        return False


def _ttl(after_hours_h: float, market_hours_h: float) -> float:
    """Return appropriate TTL in hours based on whether market is open."""
    return market_hours_h if _is_market_hours() else after_hours_h


# ── Shadow mode DB ─────────────────────────────────────────────────────────────

def _shadow_init():
    with sqlite3.connect(_SHADOW_DB) as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS uw_signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            flow_signal     TEXT,
            norm_pct        REAL,
            expiry_score    REAL,
            call_put_ratio  REAL,
            sweep_count     INTEGER,
            price_at_signal REAL,
            price_10d       REAL,
            price_20d       REAL,
            price_30d       REAL,
            outcome_filled  INTEGER DEFAULT 0
        )
        """)


def _shadow_log(symbol: str, result: dict, price: float = None):
    try:
        _shadow_init()
        with sqlite3.connect(_SHADOW_DB) as c:
            c.execute(
                "INSERT INTO uw_signals (ts,symbol,flow_signal,norm_pct,expiry_score,"
                "call_put_ratio,sweep_count,price_at_signal) VALUES (?,?,?,?,?,?,?,?)",
                (datetime.utcnow().isoformat(), symbol,
                 result.get("flow_signal"), result.get("normalized_prem_pct"),
                 result.get("expiry_alignment_score"), result.get("call_put_ratio"),
                 result.get("sweep_count_7d"), price)
            )
    except Exception:
        pass


def get_shadow_hit_rate(lookback_days: int = 90) -> dict:
    """Return validation stats for the 90-day shadow period."""
    try:
        _shadow_init()
        cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).isoformat()
        with sqlite3.connect(_SHADOW_DB) as c:
            rows = c.execute(
                "SELECT flow_signal, norm_pct, price_at_signal, price_20d "
                "FROM uw_signals WHERE ts > ? AND outcome_filled = 1",
                (cutoff,)
            ).fetchall()
        if not rows:
            return {"status": "insufficient_data", "n": 0}

        bullish = [(r[2], r[3]) for r in rows if r[0] == "bullish_sweep" and r[2] and r[3]]
        hit = sum(1 for entry, exit in bullish if exit > entry)
        return {
            "status":             "shadow_active",
            "n_bullish_signals":  len(bullish),
            "hit_rate_20d":       round(hit / len(bullish) * 100, 1) if bullish else None,
            "total_signals":      len(rows),
        }
    except Exception:
        return {"status": "error"}


# ── API helpers ────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.UNUSUAL_WHALES_API_KEY}",
        "Accept":        "application/json, text/plain",
    }


def _fetch_flow(symbol: str, days: int = 7, use_cache: bool = True) -> list[dict]:
    """
    Fetch recent options flow for a ticker.
    Unusual Whales API: GET /api/stock/{ticker}/options-flow
    Short in-memory dedup cache (5 min during market hours, 30 min AH) prevents
    redundant calls within the same intraday scan cycle.
    """
    if not config.UNUSUAL_WHALES_API_KEY:
        return []
    if use_cache:
        cached = _flow_cache.get(symbol)
        if cached:
            ttl_min = 5 if _is_market_hours() else 30
            age_min = (datetime.utcnow() - datetime.fromisoformat(cached["ts"])).total_seconds() / 60
            if age_min < ttl_min:
                trades = cached["trades"]
                cutoff = datetime.utcnow() - timedelta(days=days)
                return [t for t in trades if _trade_after(t, cutoff)]
    try:
        url = f"{_BASE_URL}/api/stock/{symbol}/flow-alerts"
        params = {"limit": 200}
        r = requests.get(url, headers=_headers(), params=params, timeout=_TIMEOUT)
        if r.status_code == 401:
            print(f"  [UW] 401 Unauthorized — check UNUSUAL_WHALES_API_KEY")
            return []
        if r.status_code == 429:
            print(f"  [UW] Rate limited")
            return []
        if r.status_code != 200:
            return []
        data = r.json()
        # API may return {data: [...]} or directly [...]
        all_trades = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(all_trades, list):
            return []
        # Store full response in dedup cache (unfiltered — filter per caller's days param)
        _flow_cache[symbol] = {"ts": datetime.utcnow().isoformat(), "trades": all_trades}
        cutoff = datetime.utcnow() - timedelta(days=days)
        return [t for t in all_trades if _trade_after(t, cutoff)]
    except Exception as e:
        print(f"  [UW] fetch error {symbol}: {e}")
        return []


def _trade_after(t: dict, cutoff: datetime) -> bool:
    ts_str = t.get("created_at") or t.get("timestamp") or ""
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00").replace("+00:00", ""))
        return ts >= cutoff
    except Exception:
        return True  # include if date unparseable


def _fetch_30d_baseline(symbol: str) -> tuple[float, float]:
    """
    Return (p30_daily_avg_premium, p90_daily_spike_premium) for the ticker's
    own 30-day rolling options premium baseline.
    Uses a session-level cache; refreshes if >12 hours old.
    """
    cached = _baseline_cache.get(symbol)
    if cached:
        age_h = (datetime.utcnow() - datetime.fromisoformat(cached["ts"])).total_seconds() / 3600
        if age_h < 12:
            return cached["p30"], cached["p90"]

    trades_30d = _fetch_flow(symbol, days=30)
    if not trades_30d:
        return 0.0, 0.0

    # Group by day, sum premium per day
    daily: dict[str, float] = {}
    for t in trades_30d:
        prem = _parse_premium(t)
        day = (t.get("created_at") or "")[:10] or "unknown"
        daily[day] = daily.get(day, 0) + prem

    vals = sorted(daily.values())
    if not vals:
        return 0.0, 0.0

    p30 = sum(vals) / len(vals)
    p90_idx = max(0, int(len(vals) * 0.90) - 1)
    p90 = vals[p90_idx]

    _baseline_cache[symbol] = {"ts": datetime.utcnow().isoformat(), "p30": p30, "p90": p90}
    return p30, p90


def _parse_premium(trade: dict) -> float:
    """Extract premium in USD from a trade record (handles various field names)."""
    for field in ("premium", "total_premium", "notional_value", "price"):
        v = trade.get(field)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    # Fallback: price × size × 100
    price = trade.get("ask") or trade.get("mid_price") or 0
    size  = trade.get("size") or trade.get("volume") or 0
    try:
        return float(price) * float(size) * 100
    except (ValueError, TypeError):
        return 0.0


def _parse_expiry_weeks(trade: dict) -> float | None:
    """Return weeks to expiry from a trade record, or None."""
    for field in ("expiration_date", "expiry", "expiry_date", "expires"):
        v = trade.get(field)
        if v:
            try:
                exp = date.fromisoformat(str(v)[:10])
                return max(0, (exp - date.today()).days / 7)
            except Exception:
                pass
    return None


def _classify_side(trade: dict) -> str:
    """Return 'call' or 'put' from a trade record."""
    t = str(trade.get("option_type") or trade.get("type") or trade.get("put_call") or "").lower()
    if "put" in t:
        return "put"
    return "call"


def _is_sweep(trade: dict) -> bool:
    """True if this trade is flagged as a sweep or aggressive order."""
    flags = str(trade.get("trade_type") or trade.get("execution_estimate") or "").lower()
    return "sweep" in flags or "above_ask" in flags or "ask" in flags


# ── Short interest ─────────────────────────────────────────────────────────────

def get_short_interest(symbol: str) -> dict:
    """
    Short interest + borrow rate for a ticker.
    Squeeze score: high = short_pct>25% AND borrow_rate>30%, moderate = either condition.
    Cached 4h per ticker.
    """
    _empty = {"short_interest_pct": None, "borrow_rate": None, "short_squeeze_score": "no_data"}
    if not config.UNUSUAL_WHALES_API_KEY:
        return _empty

    cached = _short_cache.get(symbol)
    if cached:
        age_h = (datetime.utcnow() - datetime.fromisoformat(cached["ts"])).total_seconds() / 3600
        if age_h < _ttl(after_hours_h=4.0, market_hours_h=1.0):
            return cached["result"]

    for endpoint in (f"/api/stock/{symbol}/short-interest", f"/api/stock/{symbol}/shorts",
                     f"/api/stock/{symbol}/short"):
        try:
            r = requests.get(f"{_BASE_URL}{endpoint}", headers=_headers(), timeout=_TIMEOUT)
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                break
            data = r.json()
            payload = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(payload, list) and payload:
                payload = payload[0]
            if not isinstance(payload, dict):
                continue

            short_pct   = None
            borrow_rate = None
            for key in ("short_float_pct", "short_percent_float", "short_interest_pct",
                        "shortPercentOfFloat", "short_float"):
                v = payload.get(key)
                if v is not None:
                    try:
                        short_pct = float(v) * (1 if float(v) > 1 else 100)
                        break
                    except (ValueError, TypeError):
                        pass

            for key in ("borrow_rate", "borrowRate", "fee_rate", "short_borrow_rate",
                        "cost_to_borrow"):
                v = payload.get(key)
                if v is not None:
                    try:
                        borrow_rate = float(v)
                        break
                    except (ValueError, TypeError):
                        pass

            score = "no_data"
            if short_pct is not None:
                high_si    = short_pct >= 25
                high_borrow = borrow_rate is not None and borrow_rate >= 30
                if high_si and high_borrow:
                    score = "high"
                elif high_si or high_borrow:
                    score = "moderate"
                else:
                    score = "low"

            result = {"short_interest_pct": short_pct, "borrow_rate": borrow_rate,
                      "short_squeeze_score": score}
            _short_cache[symbol] = {"ts": datetime.utcnow().isoformat(), "result": result}
            return result
        except Exception:
            continue

    _short_cache[symbol] = {"ts": datetime.utcnow().isoformat(), "result": _empty}
    return _empty


# ── IV rank & implied move ──────────────────────────────────────────────────────

def get_iv_data(symbol: str) -> dict:
    """
    IV rank (0-100), IV percentile, and implied move for a ticker.
    Implied move = expected ±% from ATM straddle (market's priced-in uncertainty).
    Cached 2h per ticker.
    """
    _empty = {"iv_rank": None, "iv_percentile": None, "implied_move_pct": None}
    if not config.UNUSUAL_WHALES_API_KEY:
        return _empty

    cached = _iv_cache.get(symbol)
    if cached:
        age_h = (datetime.utcnow() - datetime.fromisoformat(cached["ts"])).total_seconds() / 3600
        if age_h < _ttl(after_hours_h=2.0, market_hours_h=0.25):
            return cached["result"]

    for endpoint in (f"/api/stock/{symbol}/iv-rank", f"/api/stock/{symbol}/options/iv",
                     f"/api/stock/{symbol}/volatility", f"/api/stock/{symbol}/options/volatility"):
        try:
            r = requests.get(f"{_BASE_URL}{endpoint}", headers=_headers(), timeout=_TIMEOUT)
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                break
            data = r.json()
            payload = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(payload, list) and payload:
                payload = payload[0]
            if not isinstance(payload, dict):
                continue

            iv_rank = iv_pct = implied_move = None
            for key in ("iv_rank", "ivRank", "iv_rank_1y", "rank"):
                v = payload.get(key)
                if v is not None:
                    try:
                        iv_rank = int(float(v) * (1 if float(v) > 1 else 100))
                        break
                    except (ValueError, TypeError):
                        pass

            for key in ("iv_percentile", "ivPercentile", "iv_pct"):
                v = payload.get(key)
                if v is not None:
                    try:
                        iv_pct = int(float(v) * (1 if float(v) > 1 else 100))
                        break
                    except (ValueError, TypeError):
                        pass

            for key in ("implied_move", "impliedMove", "expected_move", "straddle_pct",
                        "implied_move_pct"):
                v = payload.get(key)
                if v is not None:
                    try:
                        implied_move = round(float(v) * (1 if float(v) > 1 else 100), 1)
                        break
                    except (ValueError, TypeError):
                        pass

            result = {"iv_rank": iv_rank, "iv_percentile": iv_pct,
                      "implied_move_pct": implied_move}
            _iv_cache[symbol] = {"ts": datetime.utcnow().isoformat(), "result": result}
            return result
        except Exception:
            continue

    _iv_cache[symbol] = {"ts": datetime.utcnow().isoformat(), "result": _empty}
    return _empty


# ── Open interest changes ──────────────────────────────────────────────────────

def get_oi_changes(symbol: str) -> dict:
    """
    Open interest changes — unusual call OI accumulation = institutional positioning.
    call_accumulation: smart money quietly building long exposure before a move.
    put_accumulation: hedging or directional bearish positioning.
    Cached 2h per ticker.
    """
    _empty = {"oi_change_signal": "no_data", "net_oi_change": None,
              "call_oi_delta": None, "put_oi_delta": None}
    if not config.UNUSUAL_WHALES_API_KEY:
        return _empty

    cached = _oi_cache.get(symbol)
    if cached:
        age_h = (datetime.utcnow() - datetime.fromisoformat(cached["ts"])).total_seconds() / 3600
        if age_h < _ttl(after_hours_h=2.0, market_hours_h=0.25):
            return cached["result"]

    for endpoint in (
        f"/api/stock/{symbol}/options/oi-changes",
        f"/api/stock/{symbol}/options/open-interest",
        f"/api/stock/{symbol}/oi",
    ):
        try:
            r = requests.get(f"{_BASE_URL}{endpoint}", headers=_headers(), timeout=_TIMEOUT)
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                break
            data = r.json()
            payload = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(payload, list) and payload:
                payload = payload[0]
            if not isinstance(payload, dict):
                continue

            call_oi = put_oi = None
            for key in ("call_oi_change", "call_open_interest_change", "call_oi_delta",
                        "calls_oi_change", "call_oi"):
                v = payload.get(key)
                if v is not None:
                    try: call_oi = float(v); break
                    except (ValueError, TypeError): pass

            for key in ("put_oi_change", "put_open_interest_change", "put_oi_delta",
                        "puts_oi_change", "put_oi"):
                v = payload.get(key)
                if v is not None:
                    try: put_oi = float(v); break
                    except (ValueError, TypeError): pass

            net = None
            if call_oi is not None and put_oi is not None:
                net = call_oi - put_oi
            elif call_oi is not None:
                net = call_oi
            elif put_oi is not None:
                net = -put_oi

            signal = "no_data"
            if net is not None:
                if net > 5_000:    signal = "call_accumulation"
                elif net < -5_000: signal = "put_accumulation"
                else:              signal = "quiet"

            result = {
                "oi_change_signal": signal,
                "net_oi_change":    net,
                "call_oi_delta":    call_oi,
                "put_oi_delta":     put_oi,
            }
            _oi_cache[symbol] = {"ts": datetime.utcnow().isoformat(), "result": result}
            return result
        except Exception:
            continue

    _oi_cache[symbol] = {"ts": datetime.utcnow().isoformat(), "result": _empty}
    return _empty


# ── VIX term structure ─────────────────────────────────────────────────────────

def get_vix_term_structure() -> dict:
    """
    VIX term structure using yfinance (^VIX spot vs ^VIX3M 3-month implied vol).
    Inverted = front-month VIX > 3-month VIX → acute fear, regime risk.
    System rule: inverted term structure → automatic 25% gross exposure reduction.
    Cached 30 min. Uses yfinance (no UW key required).
    """
    _empty = {"inverted": None, "vix_spot": None, "vix_3m": None, "spread": None}

    if _vts_cache.get("ts"):
        age_m = (datetime.utcnow() - datetime.fromisoformat(
            _vts_cache["ts"])).total_seconds() / 60
        if age_m < 30:
            return _vts_cache.get("result", _empty)

    try:
        import yfinance as yf
        vix_hist  = yf.Ticker("^VIX").history(period="2d")
        vix3_hist = yf.Ticker("^VIX3M").history(period="2d")
        if len(vix_hist) < 1 or len(vix3_hist) < 1:
            return _empty
        v1 = round(float(vix_hist["Close"].iloc[-1]),  2)
        v3 = round(float(vix3_hist["Close"].iloc[-1]), 2)
        spread = round(v1 - v3, 2)
        result = {
            "inverted":  v1 > v3,
            "vix_spot":  v1,
            "vix_3m":    v3,
            "spread":    spread,
        }
        _vts_cache["ts"]     = datetime.utcnow().isoformat()
        _vts_cache["result"] = result
        return result
    except Exception:
        return _empty


# ── Market-wide context ─────────────────────────────────────────────────────────

def get_uw_market_context() -> dict:
    """
    Market-wide options intelligence — called ONCE per cycle, cached 30 min.
    Returns:
      market_tide        : "bullish" | "bearish" | "neutral" | "no_data"
      market_put_call_ratio : float
      sector_flows       : {sector_etf: "bullish"|"bearish"|"neutral"}
      spy_flow           : flow_signal for SPY (market proxy)
      qqq_flow           : flow_signal for QQQ (tech proxy)
    """
    _empty = {
        "market_tide":           "no_data",
        "market_put_call_ratio": None,
        "sector_flows":          {},
        "spy_flow":              "no_data",
        "qqq_flow":              "no_data",
        "vix_term_structure":    {},
    }
    if not config.UNUSUAL_WHALES_API_KEY:
        return _empty

    # Return cached if fresh
    if _market_context_cache.get("ts"):
        age_m = (datetime.utcnow() - datetime.fromisoformat(
            _market_context_cache["ts"])).total_seconds() / 60
        if age_m < 30:
            return _market_context_cache.get("result", _empty)

    result = dict(_empty)

    # ── Market tide / net flow ────────────────────────────────────────────────
    for endpoint in ("/api/market/market-tide", "/api/market/tide", "/api/market/net-flow",
                     "/api/market/overview", "/api/market/general"):
        try:
            r = requests.get(f"{_BASE_URL}{endpoint}", headers=_headers(), timeout=_TIMEOUT)
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                break
            data = r.json()
            payload = data.get("data", data) if isinstance(data, dict) else data

            # market-tide returns a list of 5-min intervals with net_call_premium/net_put_premium
            if isinstance(payload, list) and payload:
                # Aggregate all intervals to compute cumulative tide
                total_call = sum(float(row.get("net_call_premium") or 0) for row in payload)
                total_put  = sum(float(row.get("net_put_premium")  or 0) for row in payload)
                total_vol  = sum(int(row.get("net_volume") or 0)         for row in payload)
                if total_call != 0 or total_put != 0:
                    # net positive call premium + positive volume = bullish
                    call_dom = total_call > total_put * 1.1
                    put_dom  = total_put  > total_call * 1.1
                    vol_bull = total_vol > 0
                    if call_dom and vol_bull:
                        result["market_tide"] = "bullish"
                    elif put_dom and not vol_bull:
                        result["market_tide"] = "bearish"
                    else:
                        result["market_tide"] = "neutral"
                    # derive synthetic P/C from aggregated premium
                    if total_call > 0:
                        pc = round(abs(total_put) / abs(total_call), 3) if total_call != 0 else None
                        result["market_put_call_ratio"] = pc
                    break
                # Fall through if no meaningful data in list
                payload = payload[-1]  # use most recent entry as fallback dict

            if not isinstance(payload, dict):
                continue

            pc = None
            for key in ("put_call_ratio", "putCallRatio", "pc_ratio", "market_put_call"):
                v = payload.get(key)
                if v is not None:
                    try:
                        pc = round(float(v), 3)
                        break
                    except (ValueError, TypeError):
                        pass

            tide = payload.get("tide") or payload.get("market_tide") or payload.get("sentiment")
            if tide:
                tide = str(tide).lower()
                if "bull" in tide:
                    result["market_tide"] = "bullish"
                elif "bear" in tide:
                    result["market_tide"] = "bearish"
                else:
                    result["market_tide"] = "neutral"
            elif pc is not None:
                result["market_tide"] = "bearish" if pc > 1.1 else ("bullish" if pc < 0.85 else "neutral")

            if pc is not None:
                result["market_put_call_ratio"] = pc
            break
        except Exception:
            continue

    # ── SPY and QQQ flow (use existing compute, no double-count toward rate limit) ──
    for sym, key in (("SPY", "spy_flow"), ("QQQ", "qqq_flow")):
        try:
            f = compute(sym)
            result[key] = f.get("flow_signal", "no_data")
        except Exception:
            pass

    # ── Sector ETF flow ───────────────────────────────────────────────────────
    sector_etfs = {
        "semis":    "SOXX",
        "ai_tech":  "IGV",
        "cyber":    "CIBR",
        "defense":  "ITA",
        "biotech":  "XBI",
        "energy":   "XLE",
        "fintech":  "FINX",
        "robotics": "BOTZ",
        "nuclear":  "NLR",
        "quantum":  "QTUM",
        "space":    "ROKT",
    }
    sector_flows = {}
    for sector, etf in sector_etfs.items():
        try:
            f = compute(etf)
            sig = f.get("flow_signal", "no_data")
            if sig in ("bullish_sweep", "bullish_lean"):
                sector_flows[sector] = "bullish"
            elif sig in ("bearish_sweep", "bearish_lean"):
                sector_flows[sector] = "bearish"
            elif sig == "neutral":
                sector_flows[sector] = "neutral"
        except Exception:
            pass

    result["sector_flows"] = sector_flows

    # VIX term structure (yfinance — no extra API key needed)
    vts = get_vix_term_structure()
    result["vix_term_structure"] = vts
    vts_note = ""
    if vts.get("inverted"):
        vts_note = f" | ⚠️ VIX TERM STRUCTURE INVERTED spot={vts['vix_spot']} 3m={vts['vix_3m']}"

    _market_context_cache["ts"]     = datetime.utcnow().isoformat()
    _market_context_cache["result"] = result
    print(f"  [UW Market] tide={result['market_tide']} P/C={result['market_put_call_ratio']} "
          f"SPY={result['spy_flow']} QQQ={result['qqq_flow']} "
          f"sectors={sector_flows}{vts_note}")
    return result


# ── Main compute ───────────────────────────────────────────────────────────────

def compute(symbol: str, current_price: float = None) -> dict:
    """
    Compute options flow signal for a ticker.
    Returns dict with: flow_signal, normalized_prem_pct, expiry_alignment_score,
    call_put_ratio, sweep_count_7d, is_shadow_mode, bearish_alert.
    """
    empty = {
        "flow_signal":             "no_data",
        "normalized_prem_pct":     0,
        "expiry_alignment_score":  0.0,
        "call_put_ratio":          1.0,
        "sweep_count_7d":          0,
        "net_flow_prem":           None,
        "is_shadow_mode":          getattr(config, "UNUSUAL_WHALES_SHADOW_MODE", True),
        "bearish_alert":           False,
        "flow_momentum":           None,
        "flow_by_expiry":          {},
        "oi_changes":              {"oi_change_signal": "no_data", "net_oi_change": None,
                                    "call_oi_delta": None, "put_oi_delta": None},
        "short_interest_pct":      None,
        "borrow_rate":             None,
        "short_squeeze_score":     "no_data",
        "iv_rank":                 None,
        "iv_percentile":           None,
        "implied_move_pct":        None,
    }

    if not config.UNUSUAL_WHALES_API_KEY:
        return empty

    trades = _fetch_flow(symbol, days=7)
    if not trades:
        return empty

    cutoff_1d = datetime.utcnow() - timedelta(hours=24)

    # ── Compute aggregates ────────────────────────────────────────────────────
    call_prem = put_prem = 0.0
    sweep_count = 0
    aligned_call_prem = aligned_put_prem = 0.0  # within 3-6w expiry window
    total_prem_7d = 0.0
    prem_1d = 0.0   # last 24h — for flow momentum

    # Expiry distribution buckets
    near_prem  = 0.0   # ≤2w  — event/weekly plays
    sweet_prem = 0.0   # 3-6w — medium-term catalyst window
    mid_prem   = 0.0   # 6-10w — extended catalyst
    leaps_prem = 0.0   # >10w  — long-thesis institutional conviction

    for t in trades:
        prem   = _parse_premium(t)
        side   = _classify_side(t)
        weeks  = _parse_expiry_weeks(t)
        sweep  = _is_sweep(t)

        if side == "call":
            call_prem += prem
        else:
            put_prem += prem

        total_prem_7d += prem
        if sweep:
            sweep_count += 1

        # 1-day flow (for momentum)
        ts_str = t.get("created_at") or t.get("timestamp") or ""
        try:
            ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00").replace("+00:00", ""))
            if ts_dt >= cutoff_1d:
                prem_1d += prem
        except Exception:
            pass

        # Expiry alignment: 3-6 weeks = fully aligned with medium-term window
        if weeks is not None and 3 <= weeks <= 6:
            if side == "call":
                aligned_call_prem += prem
            else:
                aligned_put_prem += prem

        # Expiry distribution
        if weeks is not None:
            if weeks <= 2:
                near_prem  += prem
            elif weeks <= 6:
                sweet_prem += prem
            elif weeks <= 10:
                mid_prem   += prem
            else:
                leaps_prem += prem

    call_put_ratio = round(call_prem / put_prem, 2) if put_prem > 0 else (10.0 if call_prem > 0 else 1.0)

    # ── Normalized premium percentile vs ticker's 30-day baseline ─────────────
    p30_avg, p90_spike = _fetch_30d_baseline(symbol)
    norm_pct = 0
    if p30_avg > 0:
        norm_pct = min(100, round(total_prem_7d / p30_avg / 7 * 100, 0))  # daily avg vs baseline
    if p90_spike > 0 and total_prem_7d / 7 >= p90_spike:
        norm_pct = min(100, max(norm_pct, 90))  # at or above 90th percentile spike

    # ── Expiry alignment score ────────────────────────────────────────────────
    # 1.0 = dominant flow in 3-6w window, 0.5 = 6-10w, 0.0 = outside
    total_aligned = aligned_call_prem + aligned_put_prem
    if total_prem_7d > 0:
        align_ratio = total_aligned / total_prem_7d
        if align_ratio >= 0.4:
            expiry_score = 1.0
        elif align_ratio >= 0.2:
            expiry_score = 0.5
        else:
            expiry_score = 0.0
    else:
        expiry_score = 0.0

    # ── Flow signal classification ─────────────────────────────────────────────
    # Bullish sweep: high premium, call-dominant, aligned expiry
    # Bearish sweep: high premium, put-dominant, aligned expiry
    is_high_premium = norm_pct >= 85
    is_call_dominant = call_put_ratio >= 1.8
    is_put_dominant  = call_put_ratio <= 0.55  # inverse: put/call ≥ 1.8

    if is_high_premium and is_call_dominant and expiry_score >= 1.0:
        flow_signal = "bullish_sweep"
    elif is_high_premium and is_put_dominant and expiry_score >= 1.0:
        flow_signal = "bearish_sweep"
    elif call_put_ratio >= 1.4:
        flow_signal = "bullish_lean"
    elif call_put_ratio <= 0.7:
        flow_signal = "bearish_lean"
    else:
        flow_signal = "neutral"

    # Bearish alert: any bearish sweep (even outside full threshold) on a held position
    bearish_alert = flow_signal in ("bearish_sweep",) and norm_pct >= 70

    # ── Flow momentum: today vs 7-day daily average ───────────────────────────
    daily_avg_7d = total_prem_7d / 7
    flow_momentum = round(prem_1d / daily_avg_7d, 1) if daily_avg_7d > 10_000 else None

    # ── Expiry distribution ────────────────────────────────────────────────────
    flow_by_expiry: dict = {}
    if total_prem_7d > 0:
        flow_by_expiry = {
            "near_pct":  int(near_prem  / total_prem_7d * 100),
            "sweet_pct": int(sweet_prem / total_prem_7d * 100),
            "mid_pct":   int(mid_prem   / total_prem_7d * 100),
            "leaps_pct": int(leaps_prem / total_prem_7d * 100),
        }

    # ── Short interest + IV + OI changes (enrichment, non-blocking) ──────────
    short_data = get_short_interest(symbol)
    iv_data    = get_iv_data(symbol)
    oi_data    = get_oi_changes(symbol)

    result = {
        "flow_signal":             flow_signal,
        "normalized_prem_pct":     int(norm_pct),
        "expiry_alignment_score":  expiry_score,
        "call_put_ratio":          call_put_ratio,
        "sweep_count_7d":          sweep_count,
        "net_flow_prem":           round(call_prem - put_prem, 0),
        "is_shadow_mode":          getattr(config, "UNUSUAL_WHALES_SHADOW_MODE", True),
        "bearish_alert":           bearish_alert,
        # Flow momentum + expiry distribution
        "flow_momentum":           flow_momentum,
        "flow_by_expiry":          flow_by_expiry,
        # OI changes
        "oi_changes":              oi_data,
        # Short interest
        "short_interest_pct":      short_data.get("short_interest_pct"),
        "borrow_rate":             short_data.get("borrow_rate"),
        "short_squeeze_score":     short_data.get("short_squeeze_score", "no_data"),
        # IV
        "iv_rank":                 iv_data.get("iv_rank"),
        "iv_percentile":           iv_data.get("iv_percentile"),
        "implied_move_pct":        iv_data.get("implied_move_pct"),
    }

    # Always log to shadow DB regardless of shadow mode (for ongoing validation)
    _shadow_log(symbol, result, price=current_price)

    return result


def get_darkpool(symbol: str, days: int = 3) -> dict:
    """
    Fetch recent dark pool prints for a ticker.
    Large dark pool buys (>$1M notional) = institutional accumulation signal.
    Returns: darkpool_signal ("strong_accumulation"|"accumulation"|"active"|"quiet"|"no_data"),
             large_print_count, large_print_5m_count, total_prints_3d.
    Cached 6h per ticker to avoid redundant calls.
    """
    _empty = {"darkpool_signal": "no_data", "large_print_count": 0, "total_prints_3d": 0,
              "total_notional_3d": 0, "avg_print_notional": 0}
    if not config.UNUSUAL_WHALES_API_KEY:
        return _empty

    cached = _darkpool_cache.get(symbol)
    if cached:
        age_h = (datetime.utcnow() - datetime.fromisoformat(cached["ts"])).total_seconds() / 3600
        if age_h < _ttl(after_hours_h=6.0, market_hours_h=0.5):
            return cached["result"]

    try:
        url = f"{_BASE_URL}/api/darkpool/{symbol}/recent"
        r = requests.get(url, headers=_headers(), params={"limit": 50}, timeout=_TIMEOUT)
        if r.status_code != 200:
            _darkpool_cache[symbol] = {"ts": datetime.utcnow().isoformat(), "result": _empty}
            return _empty

        data = r.json()
        trades = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(trades, list):
            return _empty

        cutoff = datetime.utcnow() - timedelta(days=days)
        recent = []
        for t in trades:
            ts_str = t.get("created_at") or t.get("timestamp") or t.get("date") or ""
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "").replace("+00:00", ""))
                if ts >= cutoff:
                    recent.append(t)
            except Exception:
                recent.append(t)

        large_prints = []
        large_prints_5m = []
        for t in recent:
            price  = float(t.get("price") or 0)
            volume = float(t.get("volume") or t.get("size") or t.get("shares") or 0)
            notional = float(t.get("premium") or t.get("notional") or (price * volume) or 0)
            if notional >= 1_000_000:
                large_prints.append(t)
            if notional >= 5_000_000:
                large_prints_5m.append(t)

        count = len(recent)
        large_count = len(large_prints)
        large_count_5m = len(large_prints_5m)

        # Total notional across all prints (not just large ones)
        total_notional = 0.0
        for t in recent:
            price  = float(t.get("price") or 0)
            volume = float(t.get("volume") or t.get("size") or t.get("shares") or 0)
            total_notional += float(t.get("premium") or t.get("notional") or (price * volume) or 0)
        avg_notional = total_notional / count if count > 0 else 0.0

        # strong_accumulation: ≥2 prints >$5M = highest-conviction institutional signal (+2)
        # accumulation: ≥1 print >$1M = standard institutional block trade (+1)
        if large_count_5m >= 2:
            dp_signal = "strong_accumulation"
        elif large_count >= 1:
            dp_signal = "accumulation"
        elif count >= 5:
            dp_signal = "active"         # frequent smaller prints
        else:
            dp_signal = "quiet"

        result = {
            "darkpool_signal":    dp_signal,
            "large_print_count":  large_count,
            "large_print_5m_count": large_count_5m,
            "total_prints_3d":    count,
            "total_notional_3d":  round(total_notional, 0),
            "avg_print_notional": round(avg_notional, 0),
        }
        _darkpool_cache[symbol] = {"ts": datetime.utcnow().isoformat(), "result": result}
        return result
    except Exception as e:
        print(f"  [UW darkpool] {symbol}: {e}")
        return _empty


def get_market_sweep_feed(min_premium: int = 500_000, limit: int = 100) -> list[dict]:
    """
    Fetch the most recent unusual options sweeps across the whole market.
    Used in pre-market gap scan to detect institutional activity on ANY ticker,
    including tickers not in our basket (out-of-basket discovery).
    Returns list of {symbol, side, premium, expiry_weeks, is_sweep, timestamp}.
    Only includes sweeps above min_premium (default $500K) to filter noise.
    """
    if not config.UNUSUAL_WHALES_API_KEY:
        return []

    # Try two likely endpoint variants; UW API path may vary by plan tier
    for endpoint in ("/api/option-trades/flow-alerts", "/api/options-flow/alerts", "/api/options-flow/recent"):
        try:
            r = requests.get(
                f"{_BASE_URL}{endpoint}",
                headers=_headers(),
                params={"limit": limit, "order": "desc"},
                timeout=_TIMEOUT,
            )
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                return []
            data = r.json()
            trades = data.get("data", data) if isinstance(data, dict) else data
            if not isinstance(trades, list):
                continue

            results = []
            for t in trades:
                sym = (t.get("ticker") or t.get("symbol") or "").upper()
                if not sym or "/" in sym:
                    continue
                prem = _parse_premium(t)
                if prem < min_premium:
                    continue
                results.append({
                    "symbol":       sym,
                    "side":         _classify_side(t),
                    "premium":      prem,
                    "expiry_weeks": _parse_expiry_weeks(t),
                    "is_sweep":     _is_sweep(t),
                    "timestamp":    t.get("created_at") or t.get("timestamp") or "",
                })
            return results
        except Exception:
            continue
    return []


def check_shadow_graduation() -> bool:
    """
    Check if UW shadow mode validation criteria are met and auto-update config.
    Criteria: ≥20 bullish sweep signals tracked with ≥55% 20-day forward hit rate.
    Returns True if shadow mode was just turned off.
    Call once per cycle — cheap (reads local DB, no API call).
    """
    if not getattr(config, "UNUSUAL_WHALES_SHADOW_MODE", True):
        return False  # already live, nothing to do

    min_sigs = getattr(config, "UW_SHADOW_MIN_SIGNALS",  20)
    min_rate = getattr(config, "UW_SHADOW_MIN_HIT_RATE", 55.0)

    stats    = get_shadow_hit_rate(lookback_days=90)
    n        = stats.get("n_bullish_signals", 0)
    hit_rate = stats.get("hit_rate_20d") or 0.0

    if n >= min_sigs and hit_rate >= min_rate:
        config.UNUSUAL_WHALES_SHADOW_MODE = False
        print(f"  [UW] Shadow mode GRADUATED — {n} signals, {hit_rate:.1f}% hit rate "
              f"→ +0.5 bullish-sweep bonus is now LIVE")
        return True

    if n > 0:
        print(f"  [UW] Shadow progress: {n}/{min_sigs} signals | "
              f"{hit_rate:.1f}%/{min_rate:.0f}% hit rate needed")
    return False


def conviction_bonus(flow_result: dict) -> float:
    """
    Return the conviction bonus to add to CIO confidence.
    Returns 0.0 during shadow mode (first 90 days).
    High-conviction: bullish_sweep + norm_pct ≥ 90 + expiry_score = 1.0 → +0.5
    Standard: bullish_sweep + norm_pct ≥ 85 → +0.5 (same bonus, tagged differently)
    Returns -0.5 for bearish_sweep (applies regardless of shadow mode for risk management).
    """
    if flow_result.get("flow_signal") == "no_data":
        return 0.0

    # Bearish penalty is always live — risk management cannot wait for shadow validation
    if flow_result.get("flow_signal") == "bearish_sweep" and flow_result.get("normalized_prem_pct", 0) >= 70:
        return -0.5

    # Bullish bonus is gated by shadow mode
    if flow_result.get("is_shadow_mode", True):
        return 0.0  # shadow mode: log but do not boost conviction

    if (flow_result.get("flow_signal") == "bullish_sweep"
            and flow_result.get("normalized_prem_pct", 0) >= 85):
        return 0.5

    return 0.0


# ── Intraday scanner helpers ──────────────────────────────────────────────────

def get_ticker_snapshot(symbol: str) -> dict:
    """
    Fetch all UW signals for one ticker in a single call bundle.
    Used by the intraday scanner — returns flow + darkpool + iv + oi + short.
    Respects market-hours TTLs (shorter during trading hours for higher refresh rate).
    """
    flow    = compute(symbol)
    dp      = get_darkpool(symbol)
    iv      = get_iv_data(symbol)
    oi      = get_oi_changes(symbol)
    short   = get_short_interest(symbol)
    return {
        "symbol":  symbol,
        "ts":      datetime.utcnow().isoformat(),
        **flow,
        "darkpool_signal":       dp.get("darkpool_signal"),
        "large_print_count":     dp.get("large_print_count"),
        "large_print_5m_count":  dp.get("large_print_5m_count", 0),
        "total_prints_3d":       dp.get("total_prints_3d"),
        "total_notional_3d":     dp.get("total_notional_3d"),
        "iv_rank":               iv.get("iv_rank"),
        "iv_percentile":         iv.get("iv_percentile"),
        "implied_move_pct":      iv.get("implied_move_pct"),
        "oi_call_change_pct":    oi.get("call_oi_change_pct"),
        "oi_put_change_pct":     oi.get("put_oi_change_pct"),
        "short_interest_pct":    short.get("short_interest_pct"),
        "short_squeeze_score":   short.get("short_squeeze_score"),
    }


def scan_discovery_universe(symbols: list[str]) -> list[dict]:
    """
    Bulk darkpool scan for a list of symbols (e.g., S&P 500 top 100).
    Returns only tickers with 'accumulation' or 'strong_accumulation' signals.
    No flow call — just darkpool to keep cost per ticker at 1 API call.
    """
    hits = []
    for sym in symbols:
        try:
            dp = get_darkpool(sym)
            if dp.get("darkpool_signal") in ("accumulation", "strong_accumulation"):
                hits.append({
                    "symbol":               sym,
                    "darkpool_signal":      dp["darkpool_signal"],
                    "large_print_count":    dp.get("large_print_count", 0),
                    "large_print_5m_count": dp.get("large_print_5m_count", 0),
                    "total_notional_3d":    dp.get("total_notional_3d", 0),
                })
        except Exception:
            continue
    return hits


def get_market_oi_buildup(min_mcap: int = 2_000_000_000, top_n: int = 50) -> list[dict]:
    """
    Scan market-wide OI changes to find stocks where call open interest is
    building unusually fast — early positioning signal before a move.

    Returns top_n stocks ranked by OI change magnitude, filtered to calls only
    and stocks with market cap >= min_mcap.  Each dict: {symbol, oi_change_pct,
    oi_diff, premium, days_increasing, source}.
    """
    if not config.UNUSUAL_WHALES_API_KEY:
        return []
    try:
        r = requests.get(
            f"{_BASE_URL}/api/market/oi-change",
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return []
        rows = r.json().get("data", r.json()) if isinstance(r.json(), dict) else r.json()
        if not isinstance(rows, list):
            return []

        # Keep only call-side OI builds (option_symbol contains 'C')
        seen_syms: set[str] = set()
        results: list[dict] = []
        for row in rows:
            opt_sym = row.get("option_symbol", "")
            if "C" not in opt_sym.upper()[-12:]:   # rough call detector from OCC symbol
                continue
            sym = (row.get("underlying_symbol") or "").upper()
            if not sym or sym in seen_syms or "/" in sym:
                continue
            seen_syms.add(sym)

            oi_change  = float(row.get("oi_change") or 0)
            oi_diff    = int(row.get("oi_diff_plain") or 0)
            premium    = float(row.get("prev_total_premium") or 0)
            days_up    = int(row.get("days_of_oi_increases") or 0)

            if oi_change < 10 or oi_diff < 500:   # filter noise
                continue

            results.append({
                "symbol":          sym,
                "oi_change_pct":   round(oi_change, 1),
                "oi_diff":         oi_diff,
                "premium":         premium,
                "days_increasing": days_up,
                "source":          "uw_oi_buildup",
            })
            if len(results) >= top_n:
                break

        return results
    except Exception as e:
        print(f"  [UW] oi_buildup error: {e}")
        return []


def get_earnings_beat_rate(symbol: str) -> dict:
    """
    Compute EPS beat rate from last 4 reported quarters via UW earnings endpoint.
    Beat = reported_eps > estimated_eps.

    Returns: {beat_rate: float 0-1, beat_count: int, total_quarters: int,
              avg_surprise_pct: float, next_earnings_date: str|None}
    """
    _empty = {"beat_rate": None, "beat_count": 0, "total_quarters": 0,
              "avg_surprise_pct": None, "next_earnings_date": None}
    if not config.UNUSUAL_WHALES_API_KEY:
        return _empty
    try:
        r = requests.get(
            f"{_BASE_URL}/api/stock/{symbol}/earnings",
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return _empty
        rows = r.json().get("data", r.json()) if isinstance(r.json(), dict) else r.json()
        if not isinstance(rows, list) or not rows:
            return _empty

        next_date = None
        reported  = []
        for row in rows:
            rep = row.get("reported_eps")
            est = row.get("estimated_eps")
            surp = row.get("surprise_percentage")
            date = row.get("report_date") or row.get("fiscal_date_ending")

            if rep is None and est is not None and date:
                if next_date is None:
                    next_date = date   # upcoming — no actual result yet
                continue

            if rep is not None and est is not None:
                try:
                    beat = float(rep) > float(est)
                    surprise_pct = float(surp) if surp is not None else (
                        (float(rep) - float(est)) / abs(float(est)) * 100 if float(est) != 0 else 0
                    )
                    reported.append({"beat": beat, "surprise_pct": surprise_pct})
                except (ValueError, TypeError):
                    pass
            if len(reported) >= 4:
                break

        if not reported:
            return {**_empty, "next_earnings_date": next_date}

        beat_count = sum(1 for r in reported if r["beat"])
        avg_surp   = round(sum(r["surprise_pct"] for r in reported) / len(reported), 2)
        return {
            "beat_rate":          round(beat_count / len(reported), 2),
            "beat_count":         beat_count,
            "total_quarters":     len(reported),
            "avg_surprise_pct":   avg_surp,
            "next_earnings_date": next_date,
        }
    except Exception as e:
        print(f"  [UW] earnings_beat_rate error {symbol}: {e}")
        return _empty
