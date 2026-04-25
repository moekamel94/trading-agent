import sqlite3
import json
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trading_agent.db")


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

        CREATE TABLE IF NOT EXISTS summaries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            kind        TEXT NOT NULL,
            body        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS position_tranches (
            symbol           TEXT PRIMARY KEY,
            target_pct       REAL NOT NULL,
            current_tranche  INTEGER NOT NULL DEFAULT 1,
            tranche1_ts      TEXT,
            tranche2_ts      TEXT,
            tranche3_ts      TEXT,
            tranche2_trigger TEXT,
            tranche3_trigger TEXT,
            final_confidence INTEGER,
            cio_confidence   INTEGER,
            da_severity      TEXT
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


def get_recent_trades(limit=10):
    return get_trades(limit)


def get_trades(limit=100):
    with _conn() as c:
        rows = c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(zip(["id","ts","symbol","action","asset_type","qty","price","allocation","confidence","rationale"], r)) for r in rows]


def get_snapshots(limit=30):
    with _conn() as c:
        rows = c.execute("SELECT ts, equity, cash FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"ts": r[0], "equity": r[1], "cash": r[2]} for r in rows]


def log_summary(kind: str, body: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO summaries (ts, kind, body) VALUES (?,?,?)",
            (datetime.utcnow().isoformat(), kind, body),
        )


def get_summaries(limit=10):
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, kind, body FROM summaries ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{"ts": r[0], "kind": r[1], "body": r[2]} for r in rows]


def get_today_trades():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM trades WHERE ts LIKE ? ORDER BY id ASC", (f"{today}%",)
        ).fetchall()
    return [dict(zip(["id","ts","symbol","action","asset_type","qty","price","allocation","confidence","rationale"], r)) for r in rows]


def set_tranche(symbol: str, target_pct: float, final_confidence: int,
                cio_confidence: int, da_severity: str):
    """Record a new position entering tranche 1."""
    with _conn() as c:
        c.execute("""
            INSERT INTO position_tranches
                (symbol, target_pct, current_tranche, tranche1_ts,
                 final_confidence, cio_confidence, da_severity)
            VALUES (?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                target_pct=excluded.target_pct,
                current_tranche=1,
                tranche1_ts=excluded.tranche1_ts,
                tranche2_ts=NULL, tranche3_ts=NULL,
                tranche2_trigger=NULL, tranche3_trigger=NULL,
                final_confidence=excluded.final_confidence,
                cio_confidence=excluded.cio_confidence,
                da_severity=excluded.da_severity
        """, (symbol, target_pct, datetime.utcnow().isoformat(),
              final_confidence, cio_confidence, da_severity))


def advance_tranche(symbol: str, trigger: str) -> int:
    """Advance a position to the next tranche. Returns new tranche number (0 if not found)."""
    with _conn() as c:
        row = c.execute(
            "SELECT current_tranche FROM position_tranches WHERE symbol=?", (symbol,)
        ).fetchone()
        if not row:
            return 0
        now = datetime.utcnow().isoformat()
        n = row[0]
        if n == 1:
            c.execute("""UPDATE position_tranches
                         SET current_tranche=2, tranche2_ts=?, tranche2_trigger=?
                         WHERE symbol=?""", (now, trigger, symbol))
            return 2
        if n == 2:
            c.execute("""UPDATE position_tranches
                         SET current_tranche=3, tranche3_ts=?, tranche3_trigger=?
                         WHERE symbol=?""", (now, trigger, symbol))
            return 3
        return n


def get_tranche(symbol: str) -> dict | None:
    """Return tranche record for a symbol, or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT symbol, target_pct, current_tranche, tranche1_ts, tranche2_ts, "
            "tranche3_ts, tranche2_trigger, tranche3_trigger, final_confidence, "
            "cio_confidence, da_severity FROM position_tranches WHERE symbol=?",
            (symbol,)
        ).fetchone()
    if not row:
        return None
    keys = ["symbol","target_pct","current_tranche","tranche1_ts","tranche2_ts",
            "tranche3_ts","tranche2_trigger","tranche3_trigger","final_confidence",
            "cio_confidence","da_severity"]
    return dict(zip(keys, row))


def get_all_tranches() -> list[dict]:
    """Return all active tranche records (tranches not yet complete)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT symbol, target_pct, current_tranche, tranche1_ts, tranche2_ts, "
            "tranche3_ts, tranche2_trigger, tranche3_trigger, final_confidence, "
            "cio_confidence, da_severity FROM position_tranches WHERE current_tranche < 3"
        ).fetchall()
    keys = ["symbol","target_pct","current_tranche","tranche1_ts","tranche2_ts",
            "tranche3_ts","tranche2_trigger","tranche3_trigger","final_confidence",
            "cio_confidence","da_severity"]
    return [dict(zip(keys, r)) for r in rows]


def delete_tranche(symbol: str):
    """Remove tranche record when position is closed."""
    with _conn() as c:
        c.execute("DELETE FROM position_tranches WHERE symbol=?", (symbol,))


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
