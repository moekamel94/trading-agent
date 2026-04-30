"""
Scheduler watchdog — runs every 30 minutes during market hours.
Checks that scheduled jobs fired within their expected windows.
Alerts immediately on Discord if a job is missing.

Also exposes get_system_status() for Discord /status commands.
"""

from datetime import datetime, timezone
from monitoring import health


def run_watchdog():
    """
    Called on a 30-minute interval (mon-fri, market hours).
    Records its own heartbeat, then checks for missed jobs.
    """
    health.heartbeat("watchdog")
    missed = health.check_missed_jobs()
    if not missed:
        print("  [Watchdog] All expected jobs on schedule.")
    return missed


def get_system_status() -> str:
    """
    Returns a human-readable status string for Discord /status command.
    Summarises today's heartbeats, API quota state, and order failures.
    """
    from monitoring.health import (
        _api_quotas, _cycle_stats, _order_failures,
        _signal_degraded, _job_heartbeats,
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**🤖 System Status — {now}**"]

    # Jobs that ran today
    if _job_heartbeats:
        lines.append("**Jobs ran:** " + ", ".join(sorted(_job_heartbeats.keys())))
    else:
        lines.append("**Jobs ran:** none recorded today")

    # API health
    bad_apis = list(_api_quotas.keys())
    if bad_apis:
        lines.append(f"**⚠️ Quota hit:** {', '.join(bad_apis)}")
    else:
        lines.append("**APIs:** all healthy")

    # Last cycle
    if _cycle_stats:
        last = _cycle_stats[-1]
        lines.append(
            f"**Last cycle:** {last['label']} @ {last['ts'][11:16]} UTC — "
            f"{last['scanned']} scanned / {last['buys']}B {last['sells']}S"
        )

    # Order failures
    if _order_failures:
        lines.append(f"**⚠️ Order failures today:** {len(_order_failures)}")

    # Signal degradation
    if _signal_degraded:
        lines.append(f"**ℹ️ Degraded signals:** {len(_signal_degraded)} events")

    return "\n".join(lines)
