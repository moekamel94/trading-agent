"""
Weekly review report — fires Friday 4:30 PM ET.
"""
import json, sqlite3, os, yfinance as yf
from datetime import datetime, timezone, timedelta
from notifications import discord_bot as discord

DB = os.path.join(os.path.dirname(__file__), "..", "trading_agent.db")

def run_weekly_review():
    try:
        cconnect(DB)
        cur = conn.cursor()
        cur.execute("SELECT equity, cash, ts FROM snapshots ORDER BY rowid DESC LIMIT 1")
        row = cur.fetchone()
        cur.execute("SELECT symbol, action, price, ts FROM trades WHERE ts >= date('now', \'-7 days\') ORDER BY ts DESC")
        trades = cur.fetchall()
        conn.close()

        equity = row[0] if row else 0
        cash = row[1] if row else 0

        # SPY comparison
        spy_return = "N/A"
        try:
            baseline = json.load(open(os.path.join(os.path.dirname(__file__), "..", "spy_baseline.json")))
            spy_now = float(yf.Ticker("SPY").history(period="1d")["Close"].iloc[-1])
            spy_pct = round((spy_now - baseline["spy_price"]) / baseline["spy_price"] * 100, 2)
            kimmy_pct = round((equity - baseline["kimmy_equity"]) / baseline["kimmy_equity"] * 100, 2)
            alpha = round(kimmy_pct - spy_pct, 2)
            spy_return = f"Kimmy: {kimmy_pct:+.2f}% | SPY: {spy_pct:+.2f}% | Alpha: {alpha:+.2f}%"
        except Exception as e:
            spy_return = f"Could not compute: {e}"

        trade_summary = f"{len(trades)} trades this week" if trades else "No trades this week"

        msg = (
            f"**📊 WEEKLY REVIEW — {datetime.now(timezone.utc).strftime(\"%Y-%m-%d\")}**\n"
            f"NAV: ${equity:,.0f}  |  Cash: ${cash:,.0f}\n"
            f"Performance vs SPY: {spy_return}\n"
            f"Activity: {trade_summary}\n"
            f"Target: 2× SPY annual return"
        )
        discord.send(msg)
        print(msg)
    except Exception as e:
        print(f"[WeeklyReview] Error: {e}")
        discord.send(f"⚠️ Weekly review error: {e}")
