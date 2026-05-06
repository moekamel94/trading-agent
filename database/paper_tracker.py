"""
Paper Trading Tracker — logs every committee BUY/SELL recommendation and tracks forward returns.

Tracks:
- 7-day, 14-day, 30-day, 60-day returns from entry price
- Win rate (% profitable at 30 days)
- Average return vs SPY over same period
- Weekly scorecard sent to Discord every Sunday
"""
import sqlite3
import os
from datetime import datetime, timezone, timedelta, date

import yfinance as yf

from notifications import discord_bot as discord

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "paper_tracker.db")


def _get_conn():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    """Create paper tracker tables if they don't exist."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT NOT NULL,
            action      TEXT NOT NULL,  -- BUY or SELL
            confidence  INTEGER,
            entry_price REAL,
            entry_ts    TEXT,
            rationale   TEXT,
            -- Forward return tracking
            price_7d    REAL,
            price_14d   REAL,
            price_30d   REAL,
            price_60d   REAL,
            return_7d   REAL,
            return_14d  REAL,
            return_30d  REAL,
            return_60d  REAL,
            spy_return_30d REAL,
            updated_at  TEXT
        );
    """)
    conn.commit()
    conn.close()


def log_recommendation(symbol: str, action: str, confidence: int, price: float, rationale: str):
    """Log a new committee recommendation."""
    init()
    conn = _get_conn()
    conn.execute("""
        INSERT INTO paper_trades (symbol, action, confidence, entry_price, entry_ts, rationale)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (symbol, action, confidence, price, datetime.now(timezone.utc).isoformat(), rationale))
    conn.commit()
    conn.close()


def update_forward_returns():
    """
    Update forward returns for all open paper trades.
    Called daily by the health check or scheduler.
    """
    init()
    conn = _get_conn()
    trades = conn.execute(
        "SELECT * FROM paper_trades WHERE entry_price > 0 AND entry_ts IS NOT NULL"
    ).fetchall()

    now = datetime.now(timezone.utc)
    updated = 0

    for trade in trades:
        sym = trade["symbol"]
        try:
            entry_ts = datetime.fromisoformat(trade["entry_ts"])
        except Exception:
            continue

        days_held = (now - entry_ts).days

        # Only update milestones that haven't been set yet
        milestones = {7: "7d", 14: "14d", 30: "30d", 60: "60d"}
        entry_price = trade["entry_price"]

        updates = {}
        for days, label in milestones.items():
            if days_held >= days and trade[f"return_{label}"] is None:
                # Get price at this milestone
                milestone_date = (entry_ts + timedelta(days=days)).strftime("%Y-%m-%d")
                try:
                    hist = yf.Ticker(sym).history(start=milestone_date, period="5d")
                    if not hist.empty:
                        milestone_price = float(hist["Close"].iloc[0])
                        ret = (milestone_price / entry_price - 1) * 100
                        updates[f"price_{label}"] = milestone_price
                        updates[f"return_{label}"] = round(ret, 2)

                        # Also get SPY return for 30d milestone
                        if days == 30:
                            spy_hist = yf.Ticker("SPY").history(start=entry_ts.strftime("%Y-%m-%d"), period="35d")
                            spy_start = float(spy_hist["Close"].iloc[0]) if not spy_hist.empty else None
                            spy_hist2 = yf.Ticker("SPY").history(start=milestone_date, period="5d")
                            spy_end = float(spy_hist2["Close"].iloc[0]) if not spy_hist2.empty else None
                            if spy_start and spy_end:
                                updates["spy_return_30d"] = round((spy_end / spy_start - 1) * 100, 2)
                except Exception:
                    pass

        if updates:
            updates["updated_at"] = now.isoformat()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE paper_trades SET {set_clause} WHERE id = ?",
                list(updates.values()) + [trade["id"]]
            )
            updated += 1

    conn.commit()
    conn.close()
    return updated


def get_win_rate(days: int = 30, lookback_trades: int = None) -> dict:
    """
    Calculate win rate and stats for recommendations.
    Returns: {win_rate, avg_return, avg_spy_return, alpha, best_trade, worst_trade, total_trades}
    """
    init()
    conn = _get_conn()

    where = f"return_{days}d IS NOT NULL AND action = 'BUY'"
    if lookback_trades:
        trades = conn.execute(
            f"SELECT * FROM paper_trades WHERE {where} ORDER BY entry_ts DESC LIMIT ?",
            (lookback_trades,)
        ).fetchall()
    else:
        trades = conn.execute(
            f"SELECT * FROM paper_trades WHERE {where} ORDER BY entry_ts DESC"
        ).fetchall()

    conn.close()

    if not trades:
        return {}

    returns = [t[f"return_{days}d"] for t in trades]
    wins = sum(1 for r in returns if r > 0)

    best = max(trades, key=lambda t: t[f"return_{days}d"] or -999)
    worst = min(trades, key=lambda t: t[f"return_{days}d"] or 999)

    spy_returns = [t["spy_return_30d"] for t in trades if t["spy_return_30d"] is not None]
    avg_spy = round(sum(spy_returns) / len(spy_returns), 2) if spy_returns else None
    avg_ret = round(sum(returns) / len(returns), 2)

    return {
        "win_rate": round(wins / len(trades) * 100, 1),
        "avg_return": avg_ret,
        "avg_spy_return": avg_spy,
        "alpha": round(avg_ret - avg_spy, 2) if avg_spy else None,
        "best_trade": {"symbol": best["symbol"], "return": best[f"return_{days}d"]},
        "worst_trade": {"symbol": worst["symbol"], "return": worst[f"return_{days}d"]},
        "total_trades": len(trades),
    }


def send_weekly_scorecard():
    """
    Send weekly Discord scorecard every Sunday.
    '📊 KIMMY SCORECARD: Win rate X% | Avg return X% vs SPY X% | Best: [ticker +X%] | Worst: [ticker -X%]'
    """
    update_forward_returns()

    stats = get_win_rate(days=30, lookback_trades=20)
    if not stats:
        discord.send("📊 KIMMY SCORECARD: No completed trades to score yet.")
        return

    wr = stats.get("win_rate", 0)
    avg_ret = stats.get("avg_return", 0)
    avg_spy = stats.get("avg_spy_return")
    best = stats.get("best_trade", {})
    worst = stats.get("worst_trade", {})

    spy_str = f" vs SPY {avg_spy:+.1f}%" if avg_spy else ""
    alpha_str = ""
    if stats.get("alpha") is not None:
        alpha_str = f" | Alpha: {stats['alpha']:+.1f}%"

    lines = [
        f"📊 KIMMY SCORECARD (last {stats['total_trades']} trades at 30d):",
        f"  Win rate: {wr:.0f}% | Avg return: {avg_ret:+.1f}%{spy_str}{alpha_str}",
        f"  Best call: {best.get('symbol','?')} {best.get('return', 0):+.1f}%",
        f"  Worst call: {worst.get('symbol','?')} {worst.get('return', 0):+.1f}%",
    ]

    # Alert if win rate degrading
    # Check last 4 weeks of win rates — simplified: just check current
    if wr < 50:
        lines.append("  ⚠️ Win rate below 50% — recommend signal review")

    discord.send("\n".join(lines))
