"""
Agent learning memory — tracks trade outcomes and signal quality over time.

After each cycle: log decision + key signals that drove it.
After position closes: record outcome (P&L, hold days, whether signals were right).
Before committee call: inject a "what worked recently" context block.

This lets the committee learn from its own track record rather than starting cold each cycle.
"""
import json
import sqlite3
import os
from datetime import datetime, timezone, timedelta

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "trading_agent.db")


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(_DB_PATH)


def init_learning_tables():
    """Create learning tables if they don't exist."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS decision_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                symbol      TEXT    NOT NULL,
                action      TEXT    NOT NULL,
                confidence  INTEGER,
                tier        TEXT,
                rationale   TEXT,
                cio_conf    INTEGER,
                da_severity TEXT,
                quant_dec   TEXT,
                bull_signals TEXT,
                bear_signals TEXT,
                allocation_pct REAL,
                outcome_pct    REAL,    -- filled in when position closes
                outcome_days   INTEGER, -- hold period in trading days
                outcome_ts     TEXT,    -- when position closed
                signal_score   REAL     -- retrospective: did signals predict correctly?
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS signal_performance (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                signal_type TEXT NOT NULL,   -- e.g. "golden_cross", "congress_buy", "earnings_beat"
                tier        TEXT,
                correct     INTEGER,         -- 1 = signal predicted correctly, 0 = wrong
                outcome_pct REAL             -- realized return while signal was active
            )
        """)


def log_decision(symbol: str, decision: dict, signals: dict, config):
    """Called after each committee decision — logs what drove the call."""
    init_learning_tables()
    tech   = signals.get("technical", {})
    bull   = [b for b in signals.get("_bull_signals", [])]  # populated by _build_synthesis
    bear   = [b for b in signals.get("_bear_signals", [])]

    with _conn() as c:
        c.execute("""
            INSERT INTO decision_log
              (ts, symbol, action, confidence, tier, rationale, cio_conf,
               da_severity, quant_dec, bull_signals, bear_signals, allocation_pct)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            symbol,
            decision.get("action", ""),
            decision.get("confidence", 0),
            config.TICKER_TIERS.get(symbol, "mid_growth"),
            decision.get("rationale", "")[:500],
            decision.get("cio_confidence", 0),
            decision.get("da_severity", ""),
            decision.get("quant_decision", ""),
            json.dumps(bull[:8]),
            json.dumps(bear[:8]),
            decision.get("allocation_pct", 0),
        ))


def record_outcome(symbol: str, outcome_pct: float, hold_days: int):
    """
    Called when a position closes. Updates the most recent BUY decision for this symbol
    with actual outcome so the learning system can score signal quality.
    """
    init_learning_tables()
    ts_now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        # Find most recent BUY for this symbol without an outcome yet
        row = c.execute("""
            SELECT id, bull_signals, bear_signals, confidence
            FROM decision_log
            WHERE symbol=? AND action='BUY' AND outcome_pct IS NULL
            ORDER BY ts DESC LIMIT 1
        """, (symbol,)).fetchone()
        if not row:
            return
        dec_id, bull_json, bear_json, confidence = row
        # Simple signal score: outcome > 0 = signals were right
        signal_score = 1.0 if outcome_pct > 0 else 0.0
        c.execute("""
            UPDATE decision_log
            SET outcome_pct=?, outcome_days=?, outcome_ts=?, signal_score=?
            WHERE id=?
        """, (outcome_pct, hold_days, ts_now, signal_score, dec_id))

        # Record individual signal performance
        bull_signals = json.loads(bull_json or "[]")
        for sig in bull_signals[:5]:
            sig_type = sig.split(":")[0].strip()[:50]
            c.execute("""
                INSERT INTO signal_performance (ts, signal_type, tier, correct, outcome_pct)
                VALUES (?,?,?,?,?)
            """, (ts_now, sig_type, None, 1 if outcome_pct > 0 else 0, outcome_pct))


def get_learning_context(lookback_days: int = 30) -> str:
    """
    Build a learning context block for the committee prompt.
    Returns a summary of recent outcomes, best/worst signals, and win rates.
    Injected into the committee call so it learns from its own track record.
    """
    init_learning_tables()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()

    with _conn() as c:
        # Recent closed trades with outcomes
        closed = c.execute("""
            SELECT symbol, action, confidence, tier, rationale,
                   outcome_pct, outcome_days, signal_score
            FROM decision_log
            WHERE outcome_pct IS NOT NULL AND ts >= ?
            ORDER BY ts DESC LIMIT 20
        """, (cutoff,)).fetchall()

        # Win rate by tier
        tier_stats = c.execute("""
            SELECT tier,
                   COUNT(*) as total,
                   SUM(CASE WHEN outcome_pct > 0 THEN 1 ELSE 0 END) as wins,
                   AVG(outcome_pct) as avg_return,
                   AVG(outcome_days) as avg_hold_days
            FROM decision_log
            WHERE outcome_pct IS NOT NULL AND ts >= ?
            GROUP BY tier
        """, (cutoff,)).fetchall()

        # Best performing signals
        top_signals = c.execute("""
            SELECT signal_type, COUNT(*) as uses,
                   AVG(correct) as hit_rate, AVG(outcome_pct) as avg_return
            FROM signal_performance
            WHERE ts >= ?
            GROUP BY signal_type
            HAVING uses >= 3
            ORDER BY avg_return DESC LIMIT 8
        """, (cutoff,)).fetchall()

        # Worst performing signals
        worst_signals = c.execute("""
            SELECT signal_type, COUNT(*) as uses,
                   AVG(correct) as hit_rate, AVG(outcome_pct) as avg_return
            FROM signal_performance
            WHERE ts >= ?
            GROUP BY signal_type
            HAVING uses >= 3
            ORDER BY avg_return ASC LIMIT 5
        """, (cutoff,)).fetchall()

    if not closed and not tier_stats:
        return ""

    lines = [f"=== AGENT LEARNING CONTEXT (last {lookback_days} days) ==="]

    if tier_stats:
        lines.append("\nWin rate by tier:")
        for tier, total, wins, avg_ret, avg_days in tier_stats:
            win_pct = wins / total * 100 if total else 0
            lines.append(
                f"  {tier:<14} {wins}/{total} wins ({win_pct:.0f}%) | "
                f"avg return {avg_ret:+.1f}% | avg hold {avg_days:.0f}d")

    if closed:
        lines.append(f"\nRecent closed trades ({len(closed)}):")
        for sym, action, conf, tier, rationale, outcome, days, score in closed[:10]:
            arrow = "▲" if (outcome or 0) > 0 else "▼"
            lines.append(
                f"  {arrow} {sym:<6} {action} conf={conf} [{tier}] "
                f"{outcome:+.1f}% in {days}d — {(rationale or '')[:60]}")

    if top_signals:
        lines.append("\nHighest-return signals recently:")
        for sig, uses, hit_rate, avg_ret in top_signals:
            lines.append(
                f"  + {sig:<40} {uses}x used | "
                f"{hit_rate*100:.0f}% hit rate | avg {avg_ret:+.1f}%")

    if worst_signals:
        lines.append("\nLowest-return signals (treat with caution):")
        for sig, uses, hit_rate, avg_ret in worst_signals:
            lines.append(
                f"  - {sig:<40} {uses}x used | "
                f"{hit_rate*100:.0f}% hit rate | avg {avg_ret:+.1f}%")

    lines.append("=" * 50)
    return "\n".join(lines)
