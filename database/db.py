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
            symbol                  TEXT PRIMARY KEY,
            target_pct              REAL NOT NULL,
            current_tranche         INTEGER NOT NULL DEFAULT 1,
            tranche1_ts             TEXT,
            tranche2_ts             TEXT,
            tranche3_ts             TEXT,
            tranche2_trigger        TEXT,
            tranche3_trigger        TEXT,
            final_confidence        INTEGER,
            cio_confidence          INTEGER,
            da_severity             TEXT,
            thesis_break_criteria   TEXT,
            bucket                  TEXT DEFAULT 'long_term',
            last_reunderwritten_date TEXT,
            catalyst_note           TEXT,
            expected_holding_weeks  INTEGER
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            symbol      TEXT,
            detail      TEXT
        );

        CREATE TABLE IF NOT EXISTS signal_outcomes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            entry_price     REAL NOT NULL,
            uw_flow         TEXT,
            uw_darkpool     TEXT,
            congress_signal TEXT,
            insider_signal  TEXT,
            sentiment_label TEXT,
            regime_label    TEXT,
            sector_key      TEXT,
            sector_weight   REAL,
            price_7d        REAL,
            price_14d       REAL,
            return_7d       REAL,
            return_14d      REAL,
            outcome_updated TEXT
        );

        CREATE TABLE IF NOT EXISTS signal_weights (
            signal_source   TEXT PRIMARY KEY,
            weight          REAL NOT NULL DEFAULT 1.0,
            hit_rate_30d    REAL,
            hit_rate_90d    REAL,
            sample_count    INTEGER DEFAULT 0,
            last_updated    TEXT
        );

        CREATE TABLE IF NOT EXISTS review_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            period_start    TEXT NOT NULL,
            period_end      TEXT NOT NULL,
            report          TEXT NOT NULL,
            changes_applied TEXT
        );
        """)
        # Migrations: add columns to existing position_tranches tables
        for _col, _defn in [
            ("thesis_break_criteria",    "TEXT"),
            ("bucket",                   "TEXT DEFAULT 'long_term'"),
            ("last_reunderwritten_date", "TEXT"),
            ("catalyst_note",            "TEXT"),
            ("expected_holding_weeks",   "INTEGER"),
            ("uw_bearish_streak",        "INTEGER DEFAULT 0"),
            ("entry_day_low",            "REAL"),
            ("below_entry_low_streak",   "INTEGER DEFAULT 0"),
            ("price_target",             "REAL"),
            ("price_target_basis",       "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE position_tranches ADD COLUMN {_col} {_defn}")
            except Exception:
                pass  # column already exists


def increment_uw_bearish_streak(symbol: str) -> int:
    """Increment consecutive-bearish-flow counter for a held position. Returns new streak count."""
    with _conn() as c:
        c.execute(
            "UPDATE position_tranches SET uw_bearish_streak = COALESCE(uw_bearish_streak,0) + 1 WHERE symbol=?",
            (symbol,)
        )
        row = c.execute(
            "SELECT uw_bearish_streak FROM position_tranches WHERE symbol=?", (symbol,)
        ).fetchone()
        return row[0] if row else 1


def reset_uw_bearish_streak(symbol: str):
    """Reset consecutive bearish flow counter when a clean cycle is seen."""
    with _conn() as c:
        c.execute(
            "UPDATE position_tranches SET uw_bearish_streak = 0 WHERE symbol=?", (symbol,)
        )


def get_uw_bearish_streak(symbol: str) -> int:
    """Return current bearish flow streak for a held position (0 if no record)."""
    with _conn() as c:
        row = c.execute(
            "SELECT uw_bearish_streak FROM position_tranches WHERE symbol=?", (symbol,)
        ).fetchone()
        return row[0] if row and row[0] else 0


def update_price_target(symbol: str, price_target: float, basis: str = ""):
    """Update just the price target for an existing held position (HOLD review)."""
    with _conn() as c:
        c.execute(
            "UPDATE position_tranches SET price_target=?, price_target_basis=? WHERE symbol=?",
            (price_target, basis, symbol)
        )


def set_entry_day_low(symbol: str, low: float):
    """Record the intraday low on the day a position was entered."""
    with _conn() as c:
        c.execute(
            "UPDATE position_tranches SET entry_day_low=?, below_entry_low_streak=0 WHERE symbol=?",
            (low, symbol)
        )


def check_entry_low_breach(symbol: str, current_price: float) -> int:
    """
    If current_price < entry_day_low, increment and return the consecutive-breach streak.
    Otherwise reset streak to 0 and return 0.
    Streak ≥ 2 means the position closed below its entry-day low on 2 consecutive sessions
    — an early warning to re-review the thesis.
    """
    with _conn() as c:
        row = c.execute(
            "SELECT entry_day_low, below_entry_low_streak FROM position_tranches WHERE symbol=?",
            (symbol,)
        ).fetchone()
    if not row or row[0] is None:
        return 0
    entry_low, streak = row
    if current_price < entry_low:
        streak = (streak or 0) + 1
        with _conn() as c:
            c.execute(
                "UPDATE position_tranches SET below_entry_low_streak=? WHERE symbol=?",
                (streak, symbol)
            )
        return streak
    else:
        with _conn() as c:
            c.execute(
                "UPDATE position_tranches SET below_entry_low_streak=0 WHERE symbol=?",
                (symbol,)
            )
        return 0


def log_audit(event_type: str, symbol: str, detail: str):
    """Persist every rule breach, conviction override, stop fire, and thesis criteria record."""
    with _conn() as c:
        c.execute(
            "INSERT INTO audit_log (ts, event_type, symbol, detail) VALUES (?,?,?,?)",
            (datetime.utcnow().isoformat(), event_type, symbol, detail),
        )


def get_audit_log(limit: int = 50, symbol: str = None) -> list[dict]:
    with _conn() as c:
        if symbol:
            rows = c.execute(
                "SELECT ts, event_type, symbol, detail FROM audit_log WHERE symbol=? ORDER BY id DESC LIMIT ?",
                (symbol, limit)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT ts, event_type, symbol, detail FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
    return [{"ts": r[0], "event_type": r[1], "symbol": r[2], "detail": r[3]} for r in rows]


def get_last_buy_date(symbol: str) -> str | None:
    """Return the UTC timestamp of the most recent BUY trade for this symbol."""
    with _conn() as c:
        row = c.execute(
            "SELECT ts FROM trades WHERE symbol=? AND action='BUY' ORDER BY id DESC LIMIT 1",
            (symbol,)
        ).fetchone()
    return row[0] if row else None


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


def get_recent_stop_exits(days: int = 60) -> list[dict]:
    """
    Return SELL trades within the last `days` days whose rationale indicates a
    stop-loss or thesis-break exit. Used by CCO to enforce the 20-day re-entry cooldown.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as c:
        rows = c.execute(
            """SELECT ts, symbol, rationale FROM trades
               WHERE action='SELL' AND ts >= ?
               AND (rationale LIKE '%stop_loss%'
                    OR rationale LIKE '%stop triggered%'
                    OR rationale LIKE '%MidStop%'
                    OR rationale LIKE '%thesis_break%'
                    OR rationale LIKE '%dead_money%'
                    OR rationale LIKE '%trailing_stop%'
                    OR rationale LIKE '%ATR_stop%'
                    OR rationale LIKE '%atr_stop%')
               ORDER BY ts DESC""",
            (cutoff,)
        ).fetchall()

    result = []
    now = datetime.now(timezone.utc)
    for ts_str, symbol, rationale in rows:
        try:
            ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.replace(tzinfo=timezone.utc)
            days_since = (now - ts_dt).days
        except Exception:
            days_since = None
        result.append({
            "symbol":     symbol,
            "ts":         ts_str,
            "days_since": days_since,
            "reason":     (rationale or "")[:80],
        })
    return result


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
                cio_confidence: int, da_severity: str, thesis_break_criteria: str = "",
                bucket: str = "long_term", catalyst_note: str = "",
                expected_holding_weeks: int = None,
                price_target: float = None, price_target_basis: str = ""):
    """Record a new position entering tranche 1."""
    with _conn() as c:
        c.execute("""
            INSERT INTO position_tranches
                (symbol, target_pct, current_tranche, tranche1_ts,
                 final_confidence, cio_confidence, da_severity, thesis_break_criteria,
                 bucket, catalyst_note, expected_holding_weeks,
                 price_target, price_target_basis)
            VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                target_pct=excluded.target_pct,
                current_tranche=1,
                tranche1_ts=excluded.tranche1_ts,
                tranche2_ts=NULL, tranche3_ts=NULL,
                tranche2_trigger=NULL, tranche3_trigger=NULL,
                final_confidence=excluded.final_confidence,
                cio_confidence=excluded.cio_confidence,
                da_severity=excluded.da_severity,
                thesis_break_criteria=excluded.thesis_break_criteria,
                bucket=excluded.bucket,
                catalyst_note=excluded.catalyst_note,
                expected_holding_weeks=excluded.expected_holding_weeks,
                price_target=excluded.price_target,
                price_target_basis=excluded.price_target_basis,
                last_reunderwritten_date=NULL
        """, (symbol, target_pct, datetime.utcnow().isoformat(),
              final_confidence, cio_confidence, da_severity, thesis_break_criteria,
              bucket, catalyst_note, expected_holding_weeks,
              price_target, price_target_basis))


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
            "cio_confidence, da_severity, bucket, last_reunderwritten_date, "
            "catalyst_note, expected_holding_weeks FROM position_tranches WHERE symbol=?",
            (symbol,)
        ).fetchone()
    if not row:
        return None
    keys = ["symbol","target_pct","current_tranche","tranche1_ts","tranche2_ts",
            "tranche3_ts","tranche2_trigger","tranche3_trigger","final_confidence",
            "cio_confidence","da_severity","bucket","last_reunderwritten_date",
            "catalyst_note","expected_holding_weeks"]
    return dict(zip(keys, row))


def get_all_tranches() -> list[dict]:
    """Return all active tranche records (tranches not yet complete)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT symbol, target_pct, current_tranche, tranche1_ts, tranche2_ts, "
            "tranche3_ts, tranche2_trigger, tranche3_trigger, final_confidence, "
            "cio_confidence, da_severity, bucket, last_reunderwritten_date, "
            "catalyst_note, expected_holding_weeks, "
            "thesis_break_criteria, price_target, price_target_basis "
            "FROM position_tranches WHERE current_tranche < 3"
        ).fetchall()
    keys = ["symbol","target_pct","current_tranche","tranche1_ts","tranche2_ts",
            "tranche3_ts","tranche2_trigger","tranche3_trigger","final_confidence",
            "cio_confidence","da_severity","bucket","last_reunderwritten_date",
            "catalyst_note","expected_holding_weeks",
            "thesis_break_criteria","price_target","price_target_basis"]
    return [dict(zip(keys, r)) for r in rows]


def get_bucket_assignments() -> dict[str, str]:
    """Return {symbol: bucket} for all tracked positions."""
    with _conn() as c:
        rows = c.execute(
            "SELECT symbol, bucket FROM position_tranches"
        ).fetchall()
    return {r[0]: (r[1] or "long_term") for r in rows}


def portfolio_allocation_snapshot(positions: list, equity: float) -> dict:
    """
    Compute long-term and medium-term bucket percentages from live positions.
    Defaults untracked positions to long_term.
    """
    buckets = get_bucket_assignments()
    lt_val = mt_val = 0.0
    lt_count = mt_count = 0
    for p in positions:
        sym = p.get("symbol", "")
        val = abs((p.get("qty") or 0) * (p.get("current_price") or 0))
        if buckets.get(sym, "long_term") == "medium_term":
            mt_val   += val
            mt_count += 1
        else:
            lt_val   += val
            lt_count += 1
    if equity <= 0:
        return {"long_term_pct": 0.0, "medium_term_pct": 0.0,
                "long_term_count": lt_count, "medium_term_count": mt_count}
    return {
        "long_term_pct":    round(lt_val / equity * 100, 1),
        "medium_term_pct":  round(mt_val / equity * 100, 1),
        "long_term_count":  lt_count,
        "medium_term_count": mt_count,
    }


def get_positions_for_reunderwriting(window_days: int = 5) -> list[str]:
    """
    Return symbols whose long-term position is approaching the 90-day re-underwriting
    window (tranche1_ts between 85-95 days ago) and have not been re-underwritten recently.
    """
    from datetime import timedelta
    now = datetime.utcnow()
    low  = (now - timedelta(days=90 + window_days)).isoformat()
    high = (now - timedelta(days=90 - window_days)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT symbol FROM position_tranches "
            "WHERE bucket = 'long_term' "
            "AND tranche1_ts BETWEEN ? AND ? "
            "AND (last_reunderwritten_date IS NULL "
            "     OR last_reunderwritten_date < ?)",
            (low, high, (now - timedelta(days=80)).isoformat())
        ).fetchall()
    return [r[0] for r in rows]


def update_reunderwriting_date(symbol: str):
    """Record that a long-term position was re-underwritten today."""
    with _conn() as c:
        c.execute(
            "UPDATE position_tranches SET last_reunderwritten_date=? WHERE symbol=?",
            (datetime.utcnow().isoformat(), symbol)
        )


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


# ── Signal outcome tracking ───────────────────────────────────────────────────

def record_signal_outcome(symbol: str, entry_price: float, signals: dict,
                           regime_label: str, sector_key: str, sector_weight: float):
    """Snapshot signal state at trade entry for later outcome measurement."""
    uw   = signals.get("options_flow", {})
    dp   = (uw.get("darkpool") or {}).get("darkpool_signal", "no_data")
    cong = signals.get("congressional", {}).get("net_signal", "neutral")
    insd = signals.get("insider", {}).get("signal", "neutral")
    sent = signals.get("sentiment", {}).get("label", "neutral")
    with _conn() as c:
        c.execute(
            """INSERT INTO signal_outcomes
               (ts, symbol, entry_price, uw_flow, uw_darkpool, congress_signal,
                insider_signal, sentiment_label, regime_label, sector_key, sector_weight)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.utcnow().isoformat(), symbol, entry_price,
             uw.get("flow_signal", "no_data"), dp, cong, insd, sent,
             regime_label, sector_key, sector_weight),
        )


def update_signal_outcomes(symbol: str, price_7d: float | None, price_14d: float | None):
    """Fill in retrospective price outcomes for a symbol's most recent unresolved entry."""
    now = datetime.utcnow().isoformat()
    with _conn() as c:
        row = c.execute(
            """SELECT id, entry_price FROM signal_outcomes
               WHERE symbol=? AND outcome_updated IS NULL
               ORDER BY id DESC LIMIT 1""",
            (symbol,)
        ).fetchone()
        if not row:
            return
        rec_id, entry = row
        r7  = round((price_7d  - entry) / entry * 100, 2) if price_7d  and entry else None
        r14 = round((price_14d - entry) / entry * 100, 2) if price_14d and entry else None
        c.execute(
            """UPDATE signal_outcomes
               SET price_7d=?, price_14d=?, return_7d=?, return_14d=?, outcome_updated=?
               WHERE id=?""",
            (price_7d, price_14d, r7, r14, now, rec_id),
        )


def get_outcomes_for_review(days: int = 30) -> list[dict]:
    """Fetch resolved outcomes from the last N days for learning analysis."""
    since = datetime.utcnow().strftime(f"%Y-%m-%d")
    with _conn() as c:
        rows = c.execute(
            """SELECT ts, symbol, entry_price, uw_flow, uw_darkpool, congress_signal,
                      insider_signal, sentiment_label, regime_label, sector_key,
                      return_7d, return_14d
               FROM signal_outcomes
               WHERE outcome_updated IS NOT NULL
                 AND ts >= date('now', ?)
               ORDER BY ts DESC""",
            (f"-{days} days",)
        ).fetchall()
    cols = ["ts","symbol","entry_price","uw_flow","uw_darkpool","congress_signal",
            "insider_signal","sentiment_label","regime_label","sector_key",
            "return_7d","return_14d"]
    return [dict(zip(cols, r)) for r in rows]


def get_pending_outcomes() -> list[dict]:
    """Return entries that still need outcome prices filled in."""
    with _conn() as c:
        rows = c.execute(
            """SELECT id, ts, symbol, entry_price
               FROM signal_outcomes WHERE outcome_updated IS NULL
               ORDER BY ts ASC"""
        ).fetchall()
    return [{"id": r[0], "ts": r[1], "symbol": r[2], "entry_price": r[3]} for r in rows]


# ── Adaptive signal weights ───────────────────────────────────────────────────

def upsert_signal_weight(source: str, weight: float,
                          hit_rate_30d: float | None, hit_rate_90d: float | None,
                          sample_count: int):
    with _conn() as c:
        c.execute(
            """INSERT INTO signal_weights
               (signal_source, weight, hit_rate_30d, hit_rate_90d, sample_count, last_updated)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(signal_source) DO UPDATE SET
                 weight=excluded.weight, hit_rate_30d=excluded.hit_rate_30d,
                 hit_rate_90d=excluded.hit_rate_90d, sample_count=excluded.sample_count,
                 last_updated=excluded.last_updated""",
            (source, weight, hit_rate_30d, hit_rate_90d, sample_count,
             datetime.utcnow().isoformat()),
        )


def get_signal_weights() -> dict[str, float]:
    """Return {signal_source: weight} — defaults to 1.0 for unknown sources."""
    with _conn() as c:
        rows = c.execute(
            "SELECT signal_source, weight FROM signal_weights"
        ).fetchall()
    return {r[0]: r[1] for r in rows}


# ── Review log ────────────────────────────────────────────────────────────────

def log_review(period_start: str, period_end: str, report: dict, changes: list):
    with _conn() as c:
        c.execute(
            "INSERT INTO review_log (ts, period_start, period_end, report, changes_applied) VALUES (?,?,?,?,?)",
            (datetime.utcnow().isoformat(), period_start, period_end,
             json.dumps(report), json.dumps(changes)),
        )


def get_last_review_date() -> str | None:
    with _conn() as c:
        row = c.execute(
            "SELECT ts FROM review_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return row[0][:10] if row else None


def get_last_committee_decision(symbol: str) -> dict | None:
    """
    Return the most recent committee decision for a symbol from decision_log.
    Used to give the committee last-cycle context for held positions.
    """
    with _conn() as c:
        row = c.execute(
            """SELECT ts, action, confidence, rationale, cio_conf, quant_dec, da_severity, allocation_pct
               FROM decision_log WHERE symbol=? ORDER BY ts DESC LIMIT 1""",
            (symbol,),
        ).fetchone()
    if not row:
        return None
    ts, action, conf, rationale, cio_conf, quant_dec, da_sev, alloc = row
    return {
        "ts":          ts,
        "action":      action,
        "confidence":  conf,
        "rationale":   rationale or "",
        "cio_conf":    cio_conf,
        "quant_dec":   quant_dec or "",
        "da_severity": da_sev or "",
        "allocation_pct": alloc or 0,
    }
