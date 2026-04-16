import sqlite3
import json
from datetime import datetime

DB_PATH = "trading_agent.db"


def _conn():
    return sqlite3.connect(DB_PATH)


def init():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            action      TEXT NOT NULL,
            asset_type  TEXT NOT NULL,
            qty         REAL,
            price       REAL,
            allocation  REAL,
            confidence  INTEGER,
            rationale   TEXT
        );

        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            technical   TEXT,
            sentiment   TEXT,
            congress    TEXT,
            insider     TEXT,
            fundamentals TEXT
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            equity      REAL,
            cash        REAL,
            positions   TEXT
        );
        """)


def log_trade(symbol, action, asset_type, qty, price, allocation, confidence, rationale):
    with _conn() as c:
        c.execute(
            "INSERT INTO trades (ts,symbol,action,asset_type,qty,price,allocation,confidence,rationale) VALUES (?,?,?,?,?,?,?,?,?)",
            (datetime.utcnow().isoformat(), symbol, action, asset_type, qty, price, allocation, confidence, rationale),
        )


def log_signals(symbol, technical, sentiment, congress, insider, fundamentals):
    with _conn() as c:
        c.execute(
            "INSERT INTO signals (ts,symbol,technical,sentiment,congress,insider,fundamentals) VALUES (?,?,?,?,?,?,?)",
            (datetime.utcnow().isoformat(), symbol,
             json.dumps(technical), json.dumps(sentiment),
             json.dumps(congress), json.dumps(insider), json.dumps(fundamentals)),
        )


def log_snapshot(equity, cash, positions):
    with _conn() as c:
        c.execute(
            "INSERT INTO snapshots (ts,equity,cash,positions) VALUES (?,?,?,?)",
            (datetime.utcnow().isoformat(), equity, cash, json.dumps(positions)),
        )


def get_trades(limit=100):
    with _conn() as c:
        rows = c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(zip(["id","ts","symbol","action","asset_type","qty","price","allocation","confidence","rationale"], r)) for r in rows]


def get_snapshots(limit=30):
    with _conn() as c:
        rows = c.execute("SELECT ts, equity, cash FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"ts": r[0], "equity": r[1], "cash": r[2]} for r in rows]


def get_signals(limit=50):
    with _conn() as c:
        rows = c.execute("SELECT ts, symbol, technical, sentiment, congress, insider, fundamentals FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for r in rows:
        result.append({
            "ts": r[0], "symbol": r[1],
            "technical": json.loads(r[2] or "{}"),
            "sentiment": json.loads(r[3] or "{}"),
            "congress":  json.loads(r[4] or "{}"),
            "insider":   json.loads(r[5] or "{}"),
            "fundamentals": json.loads(r[6] or "{}"),
        })
    return result
