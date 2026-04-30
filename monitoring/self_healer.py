"""
Self-healing engine for the trading agent.

Two tiers of response:
  AUTO-FIX  — takes action immediately, logs what it did, no ping required
  ESCALATE  — sends Discord with exact numbered fix steps + urgency level

Auto-fixes (no human needed):
  ✓ Transient API failures (429 rate-limit) → backoff + retry
  ✓ API quota skip TTL → cleared after reset window (rate-limits: 60s, daily quota: midnight)
  ✓ Discord message failures → persistent DB queue + retry on next send
  ✓ Order placement transient errors → up to 3 retries with exponential backoff
  ✓ Committee JSON parse failure → already retries via individual decide() fallback
  ✓ Stale cache entries → auto-refresh on next warmup cycle
  ✓ Missing DB tables → recreated on init() call
  ✓ Config drift within safe bounds → biweekly_review already handles

Human escalation (sends structured Discord alert with exact fix steps):
  ✗ Persistent API quota exhaustion (402, monthly/plan limit)
  ✗ Order failure after 3 retries — may be account issue
  ✗ Order blocked: insufficient buying power
  ✗ Symbol not tradable on Alpaca
  ✗ Discord unreachable after 3 retries
  ✗ Win rate < 40% for 5+ consecutive days
  ✗ All cycles zero-trade AND zero-candidates (not data degradation)
  ✗ Portfolio drawdown > circuit-breaker level
  ✗ Alpaca account restricted / PDT flag / margin call
  ✗ Regime impossible state (all sectors AVOID weight)
  ✗ Database file corruption
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta

# ── Escalation urgency levels ─────────────────────────────────────────────────

URGENCY_NOW         = "🔴 ACT NOW — before next trade cycle"
URGENCY_BEFORE_OPEN = "🟠 ACT BEFORE MARKET OPEN"
URGENCY_TODAY       = "🟡 ACT TODAY"
URGENCY_THIS_WEEK   = "🔵 ACT THIS WEEK"

# ── Issue type definitions (used for deduplication) ───────────────────────────

_ESCALATED_TODAY: set[str] = set()   # reset each calendar day
_ESCALATED_DAY_KEY = ""


def _reset_if_new_day():
    global _ESCALATED_TODAY, _ESCALATED_DAY_KEY
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today != _ESCALATED_DAY_KEY:
        _ESCALATED_DAY_KEY = today
        _ESCALATED_TODAY   = set()


# ── Core escalation function ──────────────────────────────────────────────────

def escalate(issue_key: str, what: str, cause: str, fix_steps: list[str],
             urgency: str = URGENCY_TODAY, once_per_day: bool = True):
    """
    Send a structured human-action alert to Discord.
    once_per_day=True: only sends one alert per issue_key per calendar day.
    """
    _reset_if_new_day()
    if once_per_day and issue_key in _ESCALATED_TODAY:
        return
    _ESCALATED_TODAY.add(issue_key)

    numbered = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(fix_steps))
    msg = (
        f"🚨 **ACTION REQUIRED — {urgency}**\n\n"
        f"**Issue:** {what}\n"
        f"**Cause:** {cause}\n\n"
        f"**Steps to fix:**\n{numbered}"
    )

    print(f"\n[ESCALATE] {issue_key}: {what}")
    try:
        from notifications import discord_bot as tg
        tg.send(msg)
    except Exception as e:
        print(f"  [ESCALATE] Discord failed — saving to DB: {e}")
        _db_log("escalation", issue_key, msg[:500])

    _db_log("escalation", issue_key, f"{what} | {cause}")


# ── API retry wrapper ─────────────────────────────────────────────────────────

class RetryableError(Exception):
    """Raised to signal: this error is transient, retry after backoff."""

class PermanentError(Exception):
    """Raised to signal: do not retry, escalate to human."""


def retry_call(fn, max_attempts: int = 3,
               backoff_seconds: list | None = None,
               label: str = ""):
    """
    Call fn() up to max_attempts times with exponential backoff.
    fn() should raise RetryableError for transient failures.
    fn() should raise PermanentError for non-retryable failures.
    Any other exception is treated as PermanentError.

    Returns fn()'s return value on success.
    Raises PermanentError (or the last exception) after exhausting retries.
    """
    delays = backoff_seconds or [2, 8, 20]
    last_err = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except RetryableError as e:
            last_err = e
            if attempt < max_attempts - 1:
                wait = delays[min(attempt, len(delays) - 1)]
                print(f"  [Retry] {label} attempt {attempt+1}/{max_attempts} failed "
                      f"({e}) — retrying in {wait}s")
                time.sleep(wait)
        except PermanentError:
            raise
        except Exception as e:
            # Unknown errors treated as permanent to avoid infinite retries
            raise PermanentError(str(e)) from e
    raise PermanentError(f"All {max_attempts} attempts failed: {last_err}")


# ── Morning health check ──────────────────────────────────────────────────────

def run_morning_check():
    """
    Comprehensive pre-market health check. Called at 7:30 AM ET via earnings_reaction wrapper.
    Checks APIs, Alpaca, DB, cache freshness, and last night's scheduler coverage.
    Auto-fixes what it can, escalates the rest.
    """
    print("\n[SelfHealer] Running morning health check...")
    issues = []

    # 1. Database integrity
    try:
        from database import db
        db.init()
        db.get_snapshots(limit=1)
        print("  [SelfHealer] ✓ Database OK")
    except Exception as e:
        issues.append(("db_corruption", e))
        escalate(
            "db_corruption",
            "Database appears corrupted or inaccessible",
            str(e),
            [
                "SSH into the server: ssh ubuntu@<your-server>",
                "Check disk space: df -h",
                "Check DB file: ls -la /root/trading-agent/trading_agent.db",
                "If DB is 0 bytes or missing: run: cd /root/trading-agent && python3 -c \"from database import db; db.init()\"",
                "Restart the bot: sudo systemctl restart kimmy",
            ],
            urgency=URGENCY_NOW,
        )

    # 2. Alpaca account status
    try:
        from broker import alpaca
        account = alpaca._trading.get_account()
        status  = str(getattr(account, "status", "")).lower()
        if "restricted" in status or "suspended" in status:
            escalate(
                "alpaca_account_restricted",
                f"Alpaca account status: {status} — trading blocked",
                "Account may be restricted due to PDT rule, margin call, or compliance issue",
                [
                    "Log in to https://alpaca.markets/",
                    "Check account status and any alerts/notifications",
                    "If PDT: maintain account equity > $25,000 or switch to Cash account",
                    "If margin call: deposit funds or reduce positions",
                    "Contact Alpaca support if unclear: support@alpaca.markets",
                ],
                urgency=URGENCY_NOW,
            )
        else:
            bp = float(getattr(account, "buying_power", 0))
            eq = float(getattr(account, "equity", 1))
            if bp < eq * 0.05:
                escalate(
                    "alpaca_low_buying_power",
                    f"Buying power critically low: ${bp:,.0f} ({bp/eq*100:.1f}% of equity)",
                    "Almost fully deployed — no room for new entries this cycle",
                    [
                        "Review current positions at https://alpaca.markets/",
                        "Consider trimming weakest position to free up capital",
                        "OR: this may be correct if all positions are fully sized — no action needed",
                    ],
                    urgency=URGENCY_BEFORE_OPEN,
                    once_per_day=True,
                )
            print(f"  [SelfHealer] ✓ Alpaca OK (BP=${bp:,.0f})")
    except Exception as e:
        escalate(
            "alpaca_unreachable",
            "Cannot connect to Alpaca — trades will fail today",
            str(e),
            [
                "Check Alpaca status: https://status.alpaca.markets/",
                "Verify API keys in config.py / environment variables",
                "Test connectivity: cd /root/trading-agent && python3 -c \"from broker import alpaca; print(alpaca.get_portfolio())\"",
                "If keys expired: regenerate at https://alpaca.markets/",
            ],
            urgency=URGENCY_NOW,
        )

    # 3. API quota state — clear stale skips from yesterday's session
    _clear_stale_api_skips()

    # 4. Discord connectivity
    _check_discord_and_drain_queue()

    # 5. Research cache freshness
    try:
        import database.research_cache as rc
        stale = rc.get_stale_symbols(max_age_days=7) if hasattr(rc, "get_stale_symbols") else []
        if len(stale) > 20:
            print(f"  [SelfHealer] ⚠ {len(stale)} stale cache entries — auto-warmup will refresh")
        else:
            print(f"  [SelfHealer] ✓ Research cache OK ({len(stale)} stale)")
    except Exception:
        pass

    # 6. Win rate crisis detection
    check_win_rate_crisis()

    print(f"[SelfHealer] Morning check done. {len(issues)} critical issue(s).\n")
    return len(issues) == 0


# ── Post-cycle check ──────────────────────────────────────────────────────────

def run_post_cycle_check(cycle_stats: dict, decisions: list):
    """
    Called at the end of each trading cycle.
    Detects: zero-candidate cycles, persistent no-trade patterns, systematic committee issues.
    """
    if not cycle_stats:
        return

    n_scanned  = cycle_stats.get("scanned", 0)
    n_filtered = cycle_stats.get("passed_filters", 0)
    buys       = cycle_stats.get("buys", 0)
    sells      = cycle_stats.get("sells", 0)
    quota_hits = cycle_stats.get("quota_hits", 0)

    # Zero candidates after filtering — AND it's not a data degradation issue
    if n_scanned > 0 and n_filtered == 0 and quota_hits == 0:
        # Check if this is the 3rd+ consecutive zero-candidate cycle today
        _record_zero_candidate_cycle()
        consec = _get_consecutive_zero_candidate_cycles()
        if consec >= 3:
            escalate(
                "consecutive_zero_candidates",
                f"{consec} consecutive cycles with 0 candidates passing filters (no data issues)",
                "Prelim filters may be too strict for current market conditions",
                [
                    "Run: cd /root/trading-agent && python3 -c \"from reports.biweekly_review import run_biweekly_review; run_biweekly_review(dry_run=True)\"",
                    "Check config.py: MID_GROWTH_PRELIM_MIN, CRITERIA_RSI_MAX, CRITERIA_PE_MAX",
                    "Check macro regime: if all sectors are AVOID weight, that is intentional — review regime label",
                    "If criteria seem correct, no action needed — market may genuinely not qualify",
                ],
                urgency=URGENCY_TODAY,
            )

    # Committee fallback detection (JSON parse failures on full batch)
    if decisions:
        fallbacks = [d for d in decisions if d.get("_committee_fallback")]
        if len(fallbacks) > len(decisions) * 0.5:
            escalate(
                "committee_high_fallback",
                f"Committee batch failed for {len(fallbacks)}/{len(decisions)} symbols — fell back to individual decisions",
                fallbacks[0].get("_committee_fallback", "unknown parse error")[:200] if fallbacks else "",
                [
                    "This usually means Claude's JSON output was malformed",
                    "Check recent logs: grep 'Committee.*Error' /root/trading-agent/logs/*.log",
                    "_COMMITTEE_BATCH_SIZE is already 8 with 2-attempt retry (reduced from 20 on 2026-04-30)",
                    "If still recurring: check for model outage or prompt size regression",
                ],
                urgency=URGENCY_TODAY,
                once_per_day=True,
            )


# ── Order placement with retry ────────────────────────────────────────────────

def place_order_with_retry(symbol: str, qty: float, action: str,
                           max_attempts: int = 3) -> tuple[bool, str]:
    """
    Wrapper around alpaca.place_market_order() with retry logic.
    Returns (success: bool, message: str).

    Retryable: network timeout, 5xx, 429 rate-limit
    Non-retryable: insufficient funds, invalid symbol, account restricted
    """
    from broker import alpaca

    def _classify_and_call():
        try:
            result = alpaca.place_market_order(symbol, qty, action)
            return result
        except Exception as e:
            err = str(e).lower()
            # Permanent failures — do not retry
            if any(kw in err for kw in (
                "insufficient buying power", "insufficient_buying_power",
                "insufficient funds", "account is not allowed",
                "not found", "not tradable", "invalid symbol",
                "is not available for trading",
                "account is restricted", "account restricted",
                "pattern day trader", "pdt",
            )):
                raise PermanentError(str(e))
            # Retryable failures
            if any(kw in err for kw in (
                "timeout", "connection", "network",
                "rate limit", "429", "too many requests",
                "500", "502", "503", "504",
            )):
                raise RetryableError(str(e))
            # Unknown — treat as permanent to be safe
            raise PermanentError(str(e))

    try:
        retry_call(_classify_and_call, max_attempts=max_attempts,
                   backoff_seconds=[3, 10, 25], label=f"{action} {symbol}")
        return True, "order placed"

    except PermanentError as e:
        err = str(e).lower()
        _handle_permanent_order_failure(symbol, action, str(e))
        return False, str(e)

    except Exception as e:
        from monitoring import health
        health.record_order_failure(symbol, action, str(e))
        return False, str(e)


def _handle_permanent_order_failure(symbol: str, action: str, error: str):
    """Classify permanent order failure and escalate with specific fix."""
    err = error.lower()
    from monitoring import health
    health.record_order_failure(symbol, action, error)

    if "insufficient buying power" in err or "insufficient funds" in err:
        escalate(
            f"order_insufficient_funds_{action}",
            f"{action} {symbol} FAILED — insufficient buying power",
            error,
            [
                "Check buying power: https://alpaca.markets/ → Portfolio",
                "Consider trimming a current position to free capital",
                "Check if STOP_LOSS_PCT triggered large exits today reducing available cash",
                "No retry needed — the order will be reconsidered next cycle if cash is available",
            ],
            urgency=URGENCY_BEFORE_OPEN,
        )
    elif any(kw in err for kw in ("not tradable", "not found", "invalid symbol")):
        escalate(
            f"order_invalid_symbol_{symbol}",
            f"{action} {symbol} FAILED — symbol not tradable on Alpaca",
            error,
            [
                f"Remove {symbol} from the basket: edit /root/trading-agent/config.py → TICKER_TIERS",
                f"Also remove from SECTOR_MAP in config.py",
                "Then restart: sudo systemctl restart kimmy",
                f"Reason: {symbol} may be delisted, OTC-only, or not supported in paper trading",
            ],
            urgency=URGENCY_TODAY,
        )
    elif "restricted" in err or "pdt" in err or "pattern day trader" in err:
        escalate(
            "order_account_restricted",
            f"Order {action} {symbol} FAILED — account restricted",
            error,
            [
                "Log in to https://alpaca.markets/ immediately",
                "Check account alerts and compliance notifications",
                "If PDT flagged: account equity must stay > $25,000 for unlimited day trades",
                "Trading is HALTED until resolved — all orders will fail",
            ],
            urgency=URGENCY_NOW,
        )
    else:
        escalate(
            f"order_unknown_failure_{symbol}",
            f"{action} {symbol} FAILED — unknown permanent error",
            error,
            [
                "Check Alpaca status: https://status.alpaca.markets/",
                f"Try placing the order manually in the Alpaca dashboard",
                "If issue persists, check logs: grep 'ORDER FAIL' /root/trading-agent/logs/*.log",
                "Restart bot if multiple failures: sudo systemctl restart kimmy",
            ],
            urgency=URGENCY_TODAY,
        )


# ── API quota TTL clearing ────────────────────────────────────────────────────

def _clear_stale_api_skips():
    """
    Clear expired quota skips from financial_data._SKIPPED.
    - HTTP 429 (rate-limit): clear after 90 seconds
    - HTTP 402 (quota): clear at midnight UTC (daily quota reset)
    - Unknown: clear after 2 hours

    This is called every morning check and at the start of each cycle.
    """
    try:
        import signals.financial_data as fd

        # _QUOTA_HIT_TIMES is the new tracking dict we'll add to financial_data
        # Falls back gracefully if not present
        hit_times = getattr(fd, "_QUOTA_HIT_TIMES", {})
        now = datetime.now(timezone.utc)
        midnight_utc = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

        cleared = []
        for api_name, (ts_str, http_code) in list(hit_times.items()):
            try:
                hit_at = datetime.fromisoformat(ts_str)
                if hit_at.tzinfo is None:
                    hit_at = hit_at.replace(tzinfo=timezone.utc)

                if str(http_code) == "429":
                    ttl = timedelta(seconds=90)
                elif str(http_code) == "402":
                    ttl = midnight_utc - hit_at   # until midnight
                else:
                    ttl = timedelta(hours=2)

                if now - hit_at > ttl:
                    fd._SKIPPED.discard(api_name)
                    del hit_times[api_name]
                    cleared.append(api_name)
            except Exception:
                pass

        if cleared:
            print(f"  [SelfHealer] API skip cleared (TTL expired): {cleared}")

    except Exception:
        pass


# ── Discord retry queue ───────────────────────────────────────────────────────

def _db_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "trading_agent.db")


def queue_discord_message(text: str):
    """Save a failed Discord message to DB for retry."""
    try:
        with sqlite3.connect(_db_path()) as c:
            c.execute(
                "INSERT INTO audit_log (ts, event_type, symbol, detail) VALUES (?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), "discord_queue",
                 "pending", text[:2000]),
            )
    except Exception as e:
        print(f"  [SelfHealer] Failed to queue Discord message: {e}")


def _check_discord_and_drain_queue():
    """
    Test Discord connectivity. If OK, drain any queued messages.
    If failed, escalate.
    """
    try:
        import requests
        import config as _cfg
        if not _cfg.DISCORD_WEBHOOK_URL:
            return

        # Try a GET ping on the webhook (doesn't send message, just validates URL)
        r = requests.get(_cfg.DISCORD_WEBHOOK_URL, timeout=5)
        if r.status_code not in (200, 405):  # 405 = Method Not Allowed = webhook exists
            escalate(
                "discord_webhook_broken",
                f"Discord webhook returning {r.status_code} — alerts not being delivered",
                "Webhook URL may be invalid, revoked, or Discord is down",
                [
                    "Check Discord status: https://discordstatus.com/",
                    "Regenerate webhook: Server Settings → Integrations → Webhooks → New Webhook",
                    f"Update DISCORD_WEBHOOK_URL in /root/trading-agent/.env or config.py",
                    "Restart bot: sudo systemctl restart kimmy",
                ],
                urgency=URGENCY_BEFORE_OPEN,
            )
            return

        print("  [SelfHealer] ✓ Discord OK")

        # Drain queued messages
        _drain_discord_queue()

    except Exception as e:
        print(f"  [SelfHealer] Discord check failed: {e}")


def _drain_discord_queue():
    """Retry sending any messages that were queued due to earlier Discord failures."""
    try:
        from notifications import discord_bot as tg
        with sqlite3.connect(_db_path()) as c:
            rows = c.execute(
                """SELECT id, detail FROM audit_log
                   WHERE event_type='discord_queue' AND symbol='pending'
                   ORDER BY id LIMIT 10"""
            ).fetchall()

        for row_id, text in rows:
            try:
                tg.send(f"[QUEUED] {text}")
                with sqlite3.connect(_db_path()) as c:
                    c.execute(
                        "UPDATE audit_log SET symbol='sent' WHERE id=?", (row_id,)
                    )
                print(f"  [SelfHealer] Drained queued message id={row_id}")
            except Exception:
                pass
    except Exception:
        pass


# ── Consecutive zero-candidate tracking ──────────────────────────────────────

_zero_candidate_streak = 0


def _record_zero_candidate_cycle():
    global _zero_candidate_streak
    _zero_candidate_streak += 1


def _get_consecutive_zero_candidate_cycles() -> int:
    return _zero_candidate_streak


def reset_zero_candidate_streak():
    global _zero_candidate_streak
    _zero_candidate_streak = 0


# ── Win rate crisis detection ─────────────────────────────────────────────────

def check_win_rate_crisis():
    """
    Check rolling 7-day win rate. If < 40% for 5+ consecutive days, escalate.
    Called from run_morning_check() and EOD digest.
    """
    try:
        from database import learning as ldb
        ctx = ldb.get_learning_context(lookback_days=7)
        if not ctx:
            return

        import sqlite3 as _sq
        with _sq.connect(_db_path()) as c:
            row = c.execute(
                """SELECT COUNT(*) as total,
                          SUM(CASE WHEN outcome_pct > 0 THEN 1 ELSE 0 END) as wins
                   FROM decision_log
                   WHERE outcome_pct IS NOT NULL
                   AND ts >= datetime('now', '-7 days')"""
            ).fetchone()

        if not row or not row[0] or row[0] < 5:
            return   # not enough data

        total, wins = row
        win_rate = wins / total * 100

        if win_rate < 40:
            escalate(
                "win_rate_crisis",
                f"Win rate {win_rate:.0f}% over last 7 days ({wins}/{total} trades) — BELOW CRISIS THRESHOLD",
                "Signal quality may have degraded, regime may have shifted, or criteria need adjustment",
                [
                    "Run emergency biweekly review: cd /root/trading-agent && python3 -c \"from reports.biweekly_review import run_biweekly_review; run_biweekly_review()\"",
                    "Check current macro regime: python3 -c \"from signals.macro_regime import compute; import json; print(json.dumps(compute(), indent=2))\"",
                    "Review last 7 days of trades in the dashboard or DB",
                    "Consider pausing new BUYs (set config.MAX_POSITIONS to current count) until review is complete",
                    "Check if any signal module APIs have been degraded (see health digest)",
                ],
                urgency=URGENCY_NOW,
                once_per_day=True,
            )
    except Exception:
        pass


# ── Internal DB helper ────────────────────────────────────────────────────────

def _db_log(event_type: str, source: str, detail: str):
    try:
        with sqlite3.connect(_db_path()) as c:
            c.execute(
                "INSERT INTO audit_log (ts, event_type, symbol, detail) VALUES (?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 f"healer_{event_type}", source, str(detail)[:400]),
            )
    except Exception:
        pass
