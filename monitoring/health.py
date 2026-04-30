"""
Central health tracking for the trading agent.

Every module records errors here as they happen — no noise, no per-error pings.
At EOD (close summary time) one digest is sent to Discord covering the full day:

  API STATUS      — which APIs ran out of quota or failed, and when
  DATA QUALITY    — which signals were degraded (empty/missing) per symbol
  CYCLE FUNNEL    — candidates → filters → committee → buys/sells (per cycle)
  ORDER FAILURES  — orders that were attempted but placement failed
  SCHEDULER JOBS  — which jobs ran, missed, or crashed
  SILENT ERRORS   — bare exceptions caught with no other handler

Usage:
    from monitoring import health
    health.record_api_quota("fmp")
    health.record_signal_degraded("NVDA", "financial_data", "fmp_quota")
    health.record_cycle_stats(n_scanned=65, n_filtered=12, n_committee=8, buys=2, sells=1)
    health.record_order_failure("CRWD", "BUY", "insufficient buying power")
    health.heartbeat("trading_cycle_open")
    health.send_eod_digest()   # called from reporter.run_close()
"""

import os
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timezone, timedelta

_lock = threading.Lock()

# Record when this process started — used to suppress missed-job alerts for
# jobs scheduled before startup (e.g. morning jobs after a mid-day restart).
_PROCESS_START_UTC = datetime.now(timezone.utc)

# ── In-process accumulators (reset each calendar day) ────────────────────────

_api_quotas:      dict[str, list[str]] = defaultdict(list)    # api_name → [timestamp, ...]
_signal_degraded: list[dict]           = []                    # {symbol, signal, reason, ts}
_cycle_stats:     list[dict]           = []                    # per cycle funnel
_order_failures:  list[dict]           = []                    # {symbol, action, error, ts}
_silent_errors:   list[dict]           = []                    # {source, detail, ts}
_job_heartbeats:  dict[str, str]       = {}                    # job_name → last_run ISO ts

# Expected job windows (ET) — used by check_missed_jobs
# Format: (day_of_week, hour, minute, grace_minutes)
# day_of_week: "mon-fri" or specific day
_EXPECTED_JOBS = {
    "earnings_reaction":     ("mon-fri",  7, 30,  20),
    "gap_scan":              ("mon-fri",  8, 45,  20),
    "premarket_summary":     ("mon-fri",  9,  0,  20),
    "trading_cycle_open":    ("mon-fri",  9, 50,  30),
    "midday_check":          ("mon-fri", 12, 30,  20),
    "trading_cycle_close":   ("mon-fri", 15, 30,  30),
    "close_summary":         ("mon-fri", 16,  5,  20),
}

_TODAY_KEY = ""   # tracks which calendar day the accumulators belong to


def _reset_if_new_day():
    global _TODAY_KEY, _api_quotas, _signal_degraded, _cycle_stats
    global _order_failures, _silent_errors
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today != _TODAY_KEY:
        _TODAY_KEY       = today
        _api_quotas      = defaultdict(list)
        _signal_degraded = []
        _cycle_stats     = []
        _order_failures  = []
        _silent_errors   = []


# ── Public recording API ──────────────────────────────────────────────────────

def record_api_quota(api_name: str, detail: str = ""):
    """
    Call whenever an API returns a quota/rate-limit error (402/429).
    Also persists to DB so state survives process restarts.
    """
    with _lock:
        _reset_if_new_day()
        ts = _now()
        _api_quotas[api_name].append(ts)
        print(f"  [HEALTH] API quota: {api_name} at {ts}" + (f" — {detail}" if detail else ""))
    _db_log("api_quota", api_name, detail or "quota/rate-limit hit")


def record_signal_degraded(symbol: str, signal_name: str, reason: str = ""):
    """
    Call when a signal module returns empty/None due to data unavailability
    (NOT because the signal is genuinely neutral).
    """
    with _lock:
        _reset_if_new_day()
        _signal_degraded.append({
            "symbol": symbol, "signal": signal_name,
            "reason": reason, "ts": _now(),
        })


def record_cycle_stats(n_scanned: int, n_filtered: int, n_committee: int,
                       n_pregatebucket: int = 0, buys: int = 0, sells: int = 0,
                       cycle_label: str = "", basket_breakdown: dict | None = None):
    """
    Call at the end of each trading cycle with the candidate funnel numbers.
    Detects zero-trade cycles and notes whether it was data degradation or genuine no-signal.
    """
    with _lock:
        _reset_if_new_day()
        degraded_count = len(_signal_degraded)
        quota_count    = sum(len(v) for v in _api_quotas.values())
        _cycle_stats.append({
            "ts":               _now(),
            "label":            cycle_label or "cycle",
            "scanned":          n_scanned,
            "passed_filters":   n_filtered,
            "committee":        n_committee,
            "pre_gate_buckets": n_pregatebucket,
            "buys":             buys,
            "sells":            sells,
            "zero_trade":       (buys + sells) == 0,
            "degraded_signals": degraded_count,
            "quota_hits":       quota_count,
            "basket":           basket_breakdown or {},
        })


def record_order_failure(symbol: str, action: str, error: str):
    """
    Call when an Alpaca order placement fails after the committee approved it.
    """
    with _lock:
        _reset_if_new_day()
        entry = {"symbol": symbol, "action": action, "error": str(error)[:200], "ts": _now()}
        _order_failures.append(entry)
        print(f"  [HEALTH] Order failure: {action} {symbol} — {error}")
    _db_log("order_failure", symbol, f"{action}: {str(error)[:120]}")


def record_silent_error(source: str, detail: str, severity: str = "low"):
    """
    Call from bare except blocks that previously just did `pass` or `return {}`.
    Replaces silent swallowing with tracked-but-quiet logging.
    """
    with _lock:
        _reset_if_new_day()
        _silent_errors.append({
            "source": source, "detail": str(detail)[:200],
            "severity": severity, "ts": _now(),
        })
    if severity in ("high", "critical"):
        _db_log("silent_error", source, f"[{severity}] {str(detail)[:120]}")
        _send_immediate_alert(f"🔴 [{severity.upper()}] {source}: {str(detail)[:160]}")


def heartbeat(job_name: str):
    """
    Call at the start of each scheduled job to record it ran.
    Used by check_missed_jobs() to detect silent scheduler failures.
    """
    ts = _now()
    with _lock:
        _job_heartbeats[job_name] = ts
    _db_log("heartbeat", job_name, ts)


def check_missed_jobs() -> list[str]:
    """
    Check whether expected jobs have run within their grace window.
    Returns list of missed job names. Sends immediate alert for any miss.
    Called from watchdog (every 30 min during market hours).
    """
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    dow    = now_et.weekday()   # 0=Mon, 4=Fri
    missed = []

    for job, (days, h, m, grace) in _EXPECTED_JOBS.items():
        if days == "mon-fri" and dow > 4:
            continue   # weekend — skip

        # Only check after the job's expected start + grace window
        expected = now_et.replace(hour=h, minute=m, second=0, microsecond=0)
        check_after = expected + timedelta(minutes=grace)
        if now_et < check_after:
            continue   # too early to call it missed

        # If this process started AFTER the job's window, we cannot retroactively
        # run it — suppress the alert to avoid noise after mid-day restarts.
        expected_utc = expected.astimezone(timezone.utc)
        if _PROCESS_START_UTC > expected_utc + timedelta(minutes=grace):
            continue   # job's window passed before we started — not our fault

        last_run_str = _job_heartbeats.get(job) or _db_last_heartbeat(job)
        if not last_run_str:
            missed.append(job)
            continue

        try:
            last_run = datetime.fromisoformat(last_run_str)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=timezone.utc)
            # Job should have run today — check if it ran after today's expected time
            today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
            today_start_utc = today_start.astimezone(timezone.utc)
            if last_run < today_start_utc:
                missed.append(job)
        except Exception:
            missed.append(job)

    if missed:
        msg = f"⚠️ MISSED SCHEDULER JOBS: {', '.join(missed)}"
        print(f"  [HEALTH] {msg}")
        _send_immediate_alert(msg)

    return missed


# ── EOD digest ────────────────────────────────────────────────────────────────

def send_eod_digest():
    """
    Build and send the end-of-day health digest to Discord.
    Call from reporter.run_close() after the portfolio recap.
    """
    with _lock:
        _reset_if_new_day()
        api_q      = dict(_api_quotas)
        sig_deg    = list(_signal_degraded)
        cyc_stats  = list(_cycle_stats)
        ord_fail   = list(_order_failures)
        sil_errs   = list(_silent_errors)
        heartbeats = dict(_job_heartbeats)

    lines = ["\n**🩺 DAILY HEALTH DIGEST**"]
    issues = 0

    # ── API quota status ──────────────────────────────────────────────────────
    if api_q:
        issues += 1
        lines.append("\n**⚠️ API Quota Hits:**")
        for api, times in sorted(api_q.items()):
            lines.append(f"  `{api}` — hit {len(times)}× (first: {times[0][11:16]} UTC)")
        lines.append("  _Signals from these APIs returned empty data for the rest of the session._")
    else:
        lines.append("\n✅ All APIs: no quota issues")

    # ── Cycle funnel ─────────────────────────────────────────────────────────
    if cyc_stats:
        lines.append("\n**📊 Cycle Funnel:**")
        for c in cyc_stats:
            label    = c["label"]
            scanned  = c["scanned"]
            filtered = c["passed_filters"]
            comm     = c["committee"]
            buys     = c["buys"]
            sells    = c["sells"]
            pre_gate = c["pre_gate_buckets"]
            basket   = c.get("basket", {})

            # Basket composition line
            if basket:
                b_parts = []
                if basket.get("lt"):       b_parts.append(f"LT:{basket['lt']}")
                if basket.get("mt"):       b_parts.append(f"MT:{basket['mt']}")
                if basket.get("discovery"):   b_parts.append(f"+{basket['discovery']} discovery")
                if basket.get("regime_scan"): b_parts.append(f"+{basket['regime_scan']} regime")
                if basket.get("orphaned"):    b_parts.append(f"+{basket['orphaned']} orphaned")
                if basket.get("crypto"):      b_parts.append(f"+{basket['crypto']} crypto")
                basket_str = f" [{', '.join(b_parts)}]"
            else:
                basket_str = ""

            funnel = (f"{scanned} scanned{basket_str} → {filtered} passed filters → "
                      f"{comm} committee → {buys}B/{sells}S")
            if pre_gate:
                funnel += f" ({pre_gate} regime pre-gated)"

            zero_note = ""
            if c["zero_trade"] and c["quota_hits"] > 0:
                zero_note = " ⚠️ zero trades — possible data degradation"
                issues += 1
            elif c["zero_trade"] and filtered == 0:
                zero_note = " — no candidates passed filters (legitimate)"
            elif c["zero_trade"]:
                zero_note = " — committee found no actionable setups"
            lines.append(f"  `{label}`: {funnel}{zero_note}")
    else:
        lines.append("\n📊 No cycles ran today")

    # ── Signal degradation ────────────────────────────────────────────────────
    if sig_deg:
        issues += 1
        by_signal: dict[str, list[str]] = defaultdict(list)
        for s in sig_deg:
            by_signal[s["signal"]].append(s["symbol"])
        lines.append(f"\n**🔶 Degraded Signals ({len(sig_deg)} events):**")
        for sig, syms in sorted(by_signal.items()):
            uniq = sorted(set(syms))
            lines.append(f"  `{sig}` — {len(uniq)} symbols: {', '.join(uniq[:8])}" +
                         (" ..." if len(uniq) > 8 else ""))
        lines.append("  _These evaluated with incomplete data — decisions may have lower signal quality._")
    else:
        lines.append("\n✅ Signal data: no degradation recorded")

    # ── Order failures ────────────────────────────────────────────────────────
    if ord_fail:
        issues += 1
        lines.append(f"\n**🚨 Order Failures ({len(ord_fail)}):**")
        for o in ord_fail:
            lines.append(f"  `{o['symbol']}` {o['action']} @ {o['ts'][11:16]} UTC — {o['error'][:100]}")
        lines.append("  _These positions were committee-approved but NOT executed._")
    else:
        lines.append("\n✅ Order placement: no failures")

    # ── Scheduler coverage ────────────────────────────────────────────────────
    if heartbeats:
        ran = sorted(heartbeats.keys())
        lines.append(f"\n**⏱ Scheduler Jobs Ran:** {', '.join(ran)}")
    missed = check_missed_jobs()
    if missed:
        issues += 1
        lines.append(f"\n**❌ Missed Jobs:** {', '.join(missed)}")

    # ── Silent errors ─────────────────────────────────────────────────────────
    high_sev = [e for e in sil_errs if e["severity"] in ("high", "critical")]
    low_sev  = [e for e in sil_errs if e["severity"] not in ("high", "critical")]
    if high_sev:
        issues += 1
        lines.append(f"\n**🔴 High-Severity Errors ({len(high_sev)}):**")
        for e in high_sev[:5]:
            lines.append(f"  `{e['source']}`: {e['detail'][:100]}")
    if low_sev:
        lines.append(f"\n_ℹ️ {len(low_sev)} low-severity errors suppressed (check logs)_")

    # ── Summary line ──────────────────────────────────────────────────────────
    if issues == 0:
        lines.append("\n✅ **No issues detected today.**")
    else:
        lines.append(f"\n⚠️ **{issues} issue category{'s' if issues > 1 else ''} detected. Review before tomorrow's open.**")

    msg = "\n".join(lines)
    print(msg)
    _db_log("eod_digest", "health", msg[:500])
    _send_immediate_alert(msg)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "trading_agent.db")


def _db_log(event_type: str, source: str, detail: str):
    """Persist health event to audit_log table (already exists in db.py)."""
    try:
        with sqlite3.connect(_db_path()) as c:
            c.execute(
                "INSERT INTO audit_log (ts, event_type, symbol, detail) VALUES (?,?,?,?)",
                (_now(), f"health_{event_type}", source, str(detail)[:400]),
            )
    except Exception:
        pass   # DB logging itself must never crash the system


def _db_last_heartbeat(job_name: str) -> str | None:
    """Check DB for last heartbeat of a job (survives process restarts)."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with sqlite3.connect(_db_path()) as c:
            row = c.execute(
                """SELECT detail FROM audit_log
                   WHERE event_type='health_heartbeat' AND symbol=? AND ts >= ?
                   ORDER BY ts DESC LIMIT 1""",
                (job_name, today),
            ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _send_immediate_alert(msg: str):
    """Send an urgent alert now (not batched into EOD). Used for high-severity only."""
    try:
        from notifications import discord_bot as tg
        tg.send(msg)
    except Exception:
        print(f"  [HEALTH] Discord send failed — alert dropped: {msg[:100]}")
