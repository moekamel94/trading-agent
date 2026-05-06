"""
Close summary report — fires at 4:05 PM ET daily.
"""
import json, sqlite3, os
from datetimezone
from notifications import discord_bot as discord

DB = os.path.join(os.path.dirname(__file__), "..", "trading_agent.db")

def run_close_summary():
    try:
        conn = sqlite3.connect(DB)
        cur = conn.cursor()
        cur.execute("SELECT equity, cash, ts FROM snapshots ORDER BY rowid DESC LIMIT 1")
        row = cur.fetchone()
        cur.execute("SELECT symbol, action, price, ts FROM trades WHERE ts >= date('now') ORDER BY ts DESC")
        trades = cur.fetchall()
        conn.close()

        equity = row[0] if row else 0
        cash = row[1] if row else 0
        cash_pct = round(cash / equity * 100, 1) if equity else 0
        positions = round((equity - cash) / equity * 100, 1) if equity else 0

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trade_lines = ""
        if trades:
            for t in trades:
                trade_lines += f"\n  {t[1]} {t[0]} @ ${t[2]:.2f}"
        else:
            trade_lines = "\n  No trades today"

        msg = (
            f"**📉 CLOSE RECAP — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC**\n"
            f"NAV: ${equity:,.0f}  |  Cash: ${cash:,.0f} ({cash_pct}%)\n"
            f"Positions: {positions}% deployed\n"
            f"Trades today:{trade_lines}"
        )
        discord.send(msg)
        print(msg)
    except Exception as e:
        print(f"[CloseSummary] Error: {e}")
        discord.send(f"⚠️ Close summary error: {e}")
