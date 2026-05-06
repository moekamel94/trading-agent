"""
Analytics Employee — monitors portfolio performance and sends analysis reports.

Runs daily after close. Tracks:
- Portfolio returns vs SPY (7d, 14d, 30d, YTD)
- Progress toward 2× SPY target
- Signal accuracy (which signals are predicting correctly)
- Decision quality (win rate, avg return per decision type)
- Underperformance alerts when behind 2× SPY target

Sends a weekly analytics digest every Sunday alongside the weekly review.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta

import yfinance as yf

import config
import database.db as db
from notifications import discord_bot as discord


def _spy_return(days: int) -> float | None:
    """Return SPY % return over last N days."""
    try:
        hist = yf.Ticker("SPY").history(period=f"{days+5}d")
        if len(hist) < 2:
            return None
        latest = hist["Close"].iloc[-1]
        start_idx = max(0, len(hist) - days)
        start = hist["Close"].iloc[start_idx]
        return round((latest / start - 1) * 100, 2)
    except Exception:
        return None


def _portfolio_return_vs_spy(days: int, current_equity: float) -> dict:
    """Compare portfolio return vs SPY over last N days."""
    try:
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trading_agent.db")
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        row = c.execute(
            "SELECT equity FROM snapshots WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
            (cutoff,)
        ).fetchone()
        conn.close()

        if not row:
            return {}

        start_equity = float(row[0])
        port_ret = round((current_equity / start_equity - 1) * 100, 2)
        spy_ret = _spy_return(days) or 0
        target_ret = spy_ret * 2  # 2× SPY target
        gap = round(port_ret - target_ret, 2)

        return {
            "portfolio": port_ret,
            "spy": spy_ret,
            "target_2x_spy": target_ret,
            "gap_vs_target": gap,
            "beating_target": gap >= 0,
        }
    except Exception:
        return {}


def _signal_accuracy_summary() -> str:
    """Pull signal accuracy from learning weights."""
    try:
        weights_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "learning", "weights.json")
        weights = json.load(open(weights_path))
        lines = []
        for signal, weight in sorted(weights.items(), key=lambda x: -x[1]):
            bar = "🟢" if weight > 1.1 else ("🔴" if weight < 0.9 else "🟡")
            lines.append(f"  {bar} {signal}: {weight:.2f}x weight")
        return "\n".join(lines) if lines else "No signal data yet"
    except Exception:
        return "Signal accuracy unavailable"


def _decision_quality_summary(days: int = 30) -> dict:
    """Analyze decision quality from recent trades."""
    try:
        trades = db.get_trades(limit=200)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        recent_buys = [t for t in trades if t["action"] == "BUY" and t.get("ts", "") >= cutoff]

        if not recent_buys:
            return {}

        # Check which buys are currently winning
        from broker import alpaca
        positions = alpaca.get_positions()
        pos_map = {p["symbol"]: float(p.get("unrealized_plpc", 0) or 0) for p in positions}

        wins = sum(1 for t in recent_buys if pos_map.get(t["symbol"], 0) > 0)
        avg_conf = sum(t.get("confidence", 0) for t in recent_buys) / len(recent_buys) if recent_buys else 0

        return {
            "total_buys": len(recent_buys),
            "win_rate": round(wins / len(recent_buys) * 100, 1) if recent_buys else 0,
            "avg_confidence": round(avg_conf, 1),
        }
    except Exception:
        return {}


def run_daily_analytics(equity: float, positions: list | None = None) -> None:
    """
    Run after close. Check performance vs 2× SPY target.
    Only sends alert if underperforming by > 5pp vs target.
    """
    ret_30d = _portfolio_return_vs_spy(30, equity)
    if not ret_30d:
        return

    gap = ret_30d.get("gap_vs_target", 0)
    port = ret_30d.get("portfolio", 0)
    spy = ret_30d.get("spy", 0)
    target = ret_30d.get("target_2x_spy", 0)

    if gap < -5:  # underperforming by > 5pp
        discord.send(
            f"📊 **Analytics Alert**\n"
            f"30d: Portfolio {port:+.1f}% | SPY {spy:+.1f}% | 2×SPY target {target:+.1f}%\n"
            f"⚠️ **{abs(gap):.1f}pp behind target** — review positioning"
        )


def run_weekly_analytics() -> None:
    """
    Comprehensive weekly analytics digest. Runs Sunday alongside weekly review.
    """
    try:
        from broker import alpaca
        portfolio = alpaca.get_portfolio()
        equity = portfolio.get("equity", 0)
    except Exception:
        return

    lines = ["━━━ **ANALYTICS REPORT** ━━━"]

    # Performance vs SPY
    lines.append("\n**📈 Performance vs 2× SPY Target**")
    for days, label in [(7, "7d"), (14, "14d"), (30, "30d")]:
        ret = _portfolio_return_vs_spy(days, equity)
        if ret:
            gap = ret["gap_vs_target"]
            icon = "✅" if gap >= 0 else "⚠️"
            lines.append(
                f"  {icon} {label}: Port {ret['portfolio']:+.1f}% | "
                f"SPY {ret['spy']:+.1f}% | Target {ret['target_2x_spy']:+.1f}% | "
                f"Gap {gap:+.1f}pp"
            )

    # Signal accuracy
    lines.append("\n**🎯 Signal Accuracy (adaptive weights)**")
    lines.append(_signal_accuracy_summary())

    # Decision quality
    dq = _decision_quality_summary(30)
    if dq:
        lines.append("\n**🧠 Decision Quality (last 30d)**")
        lines.append(
            f"  Buys: {dq['total_buys']} | "
            f"Win rate: {dq['win_rate']:.0f}% | "
            f"Avg confidence: {dq['avg_confidence']:.1f}/10"
        )

    # Underperformance diagnosis
    ret_30 = _portfolio_return_vs_spy(30, equity)
    if ret_30 and ret_30.get("gap_vs_target", 0) < 0:
        gap = abs(ret_30["gap_vs_target"])
        lines.append(f"\n⚠️ **{gap:.1f}pp behind 2× SPY target over 30 days**")
        lines.append("  → Review sector allocation vs current macro regime")
        lines.append("  → Check if held positions have catalysts to re-rate")
        lines.append("  → Consider: are we holding too many SPY-trackers?")

    discord.send("\n".join(lines))
    print("[Analytics] Weekly analytics report sent")
