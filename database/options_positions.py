"""
Tracks options proposals sent to Mohammed for manual execution.
He buys manually; the committee monitors and sends sell alerts.
Status lifecycle: proposed → closed
"""
import sqlite3
import os
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trading_agent.db")


def _conn():
    return sqlite3.connect(DB_PATH)


def init():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS options_proposals (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                  TEXT NOT NULL,
                symbol              TEXT NOT NULL,
                direction           TEXT NOT NULL,
                confidence          INTEGER NOT NULL,
                expiry_weeks        INTEGER,
                strike_strategy     TEXT,
                iv_rank             REAL,
                implied_move_pct    REAL,
                flow_signal         TEXT,
                sweep_count         INTEGER,
                price_at_proposal   REAL,
                entry_price_low     REAL,
                entry_price_high    REAL,
                target_price        REAL,
                stop_price          REAL,
                bull_case           TEXT,
                bear_case           TEXT,
                thesis_break        TEXT,
                rationale           TEXT,
                sell_trigger        TEXT,
                status              TEXT DEFAULT 'proposed',
                closed_ts           TEXT,
                close_reason        TEXT
            )
        """)


def log_proposal(
    symbol: str,
    direction: str,
    confidence: int,
    expiry_weeks: int,
    strike_strategy: str,
    iv_rank,
    implied_move_pct,
    flow_signal: str,
    sweep_count: int,
    price_at_proposal: float,
    bull_case: str,
    bear_case: str,
    thesis_break: str,
    rationale: str,
    sell_trigger: str,
    entry_price_low: float = 0.0,
    entry_price_high: float = 0.0,
    target_price: float = 0.0,
    stop_price: float = 0.0,
) -> int:
    init()
    # Migrate existing table to add new columns if needed
    with _conn() as c:
        for col, defn in [("entry_price_low", "REAL"), ("entry_price_high", "REAL"),
                          ("target_price", "REAL"), ("stop_price", "REAL")]:
            try:
                c.execute(f"ALTER TABLE options_proposals ADD COLUMN {col} {defn}")
            except Exception:
                pass
        cur = c.execute(
            """INSERT INTO options_proposals
               (ts, symbol, direction, confidence, expiry_weeks, strike_strategy,
                iv_rank, implied_move_pct, flow_signal, sweep_count, price_at_proposal,
                entry_price_low, entry_price_high, target_price, stop_price,
                bull_case, bear_case, thesis_break, rationale, sell_trigger)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                symbol, direction, confidence, expiry_weeks, strike_strategy,
                iv_rank, implied_move_pct, flow_signal, sweep_count, price_at_proposal,
                entry_price_low, entry_price_high, target_price, stop_price,
                bull_case, bear_case, thesis_break, rationale, sell_trigger,
            ),
        )
        return cur.lastrowid


def get_active_proposals(max_age_days: int = 42) -> list:
    init()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM options_proposals WHERE status='proposed' AND ts >= ? ORDER BY ts DESC",
            (cutoff,),
        ).fetchall()
        cols = [d[1] for d in c.execute("PRAGMA table_info(options_proposals)").fetchall()]
    return [dict(zip(cols, r)) for r in rows]


def close_proposal(proposal_id: int, reason: str):
    init()
    with _conn() as c:
        c.execute(
            "UPDATE options_proposals SET status='closed', closed_ts=?, close_reason=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), reason, proposal_id),
        )


def was_recently_proposed(symbol: str, direction: str, hours: int = 72) -> bool:
    """Prevent duplicate proposals for the same ticker+direction within N hours."""
    init()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT id FROM options_proposals "
            "WHERE symbol=? AND direction=? AND ts>=? AND status='proposed' LIMIT 1",
            (symbol, direction, cutoff),
        ).fetchone()
    return row is not None
