"""
Trade Learner — analyses closed trade outcomes and distills actionable lessons.

How it works:
1. After each trade closes, log_trade_exit() is called in db.py
2. At end-of-day (--close-summary), distill_lessons() runs and:
   - Groups outcomes by RSI range, setup type, volume ratio, time-of-day, bias
   - Computes win rates for each group
   - Writes top lessons back to the `lessons` table
3. claude_agent.decide() loads active lessons and injects them into the prompt
   so every future decision benefits from real trade history.
"""
import json
from collections import defaultdict
from datetime import datetime

import anthropic
import config
import database.db as db

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


# ── Distillation ─────────────────────────────────────────────────────────────

def distill_lessons():
    """
    Pull all closed outcomes, analyse patterns, ask Claude to distill lessons,
    save them back to DB. Called once per day at EOD.
    """
    outcomes = db.get_closed_outcomes(limit=500)
    if len(outcomes) < 3:
        print("  [Learner] Not enough closed trades to distill lessons yet.")
        return

    stats = _compute_stats(outcomes)
    lessons = _ask_claude_for_lessons(outcomes, stats)
    if lessons:
        db.save_lessons(lessons)
        print(f"  [Learner] Saved {len(lessons)} lessons from {len(outcomes)} trades.")
    return lessons


def _compute_stats(outcomes: list) -> dict:
    """Compute empirical win rates across key dimensions."""
    def win(o): return 1 if (o.get("pnl_r") or 0) > 0 else 0

    by_setup   = defaultdict(list)
    by_rsi     = defaultdict(list)
    by_bias    = defaultdict(list)
    by_volratio= defaultdict(list)
    by_hour    = defaultdict(list)

    for o in outcomes:
        setup = o.get("setup") or "UNKNOWN"
        by_setup[setup].append(win(o))

        rsi = o.get("rsi_at_entry")
        if rsi is not None:
            bucket = f"RSI {int(rsi//10)*10}-{int(rsi//10)*10+10}"
            by_rsi[bucket].append(win(o))

        bias = o.get("market_bias") or "UNKNOWN"
        by_bias[bias].append(win(o))

        vr = o.get("volume_ratio")
        if vr is not None:
            vbucket = "vol<1x" if vr < 1 else ("vol 1-1.5x" if vr < 1.5 else "vol>1.5x")
            by_volratio[vbucket].append(win(o))

        ts = o.get("ts_entry", "")
        try:
            hour = datetime.fromisoformat(ts).hour
            by_hour[f"hour_{hour:02d}"].append(win(o))
        except Exception:
            pass

    def summarise(d):
        return {
            k: {"win_rate": round(sum(v)/len(v), 2), "n": len(v)}
            for k, v in d.items() if len(v) >= 2
        }

    return {
        "by_setup":    summarise(by_setup),
        "by_rsi":      summarise(by_rsi),
        "by_bias":     summarise(by_bias),
        "by_volratio": summarise(by_volratio),
        "by_hour":     summarise(by_hour),
        "total_trades": len(outcomes),
        "overall_win_rate": round(sum(win(o) for o in outcomes) / len(outcomes), 2),
    }


def _ask_claude_for_lessons(outcomes: list, stats: dict) -> list:
    """Use Claude to synthesise the stats into actionable trading rules."""
    prompt = f"""
You are a trading performance analyst. Below are statistics from {stats['total_trades']} real trades.
Overall win rate: {stats['overall_win_rate']:.0%}

EMPIRICAL STATS:
{json.dumps(stats, indent=2)}

LAST 20 TRADE OUTCOMES (most recent first):
{json.dumps(outcomes[:20], indent=2, default=str)}

Your task: distill 5–12 specific, actionable lessons that should improve future trade decisions.
Each lesson must be concrete (e.g. "Avoid PULLBACK entries when RSI > 68 — win rate only 22%").
Focus on patterns with sample_size ≥ 3 and clear win rate differences (>15% gap).

Return ONLY valid JSON — no prose, no markdown:
{{
  "lessons": [
    {{
      "category": "RSI" | "SETUP" | "TIMING" | "BIAS" | "VOLUME" | "RISK" | "GENERAL",
      "lesson": "<specific actionable rule>",
      "win_rate": <float 0-1 empirical win rate for this pattern>,
      "sample_size": <int>
    }}
  ]
}}
"""
    try:
        resp = _client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        parsed = json.loads(raw)
        return parsed.get("lessons", [])
    except Exception as e:
        print(f"  [Learner] Claude lesson distillation failed: {e}")
        return []


# ── Inject lessons into prompt ────────────────────────────────────────────────

def format_lessons_for_prompt() -> str:
    """
    Returns a formatted string of active lessons to inject into the trade prompt.
    Empty string if no lessons yet.
    """
    lessons = db.get_active_lessons(limit=12)
    if not lessons:
        return ""

    lines = ["LESSONS FROM PAST TRADES (apply these rules):"]
    for i, l in enumerate(lessons, 1):
        wr = f" (win rate: {l['win_rate']:.0%}, n={l['sample_size']})" \
             if l.get("win_rate") is not None else ""
        lines.append(f"  {i}. [{l['category']}] {l['lesson']}{wr}")
    return "\n".join(lines)


# ── Record open trade for learning ───────────────────────────────────────────

def record_entry(symbol: str, decision: dict, market_bias: str,
                 rsi: float = None, volume_ratio: float = None,
                 spy_rsi: float = None) -> int:
    """
    Call immediately after a trade is placed.
    Returns outcome_id to store for later exit recording.
    """
    return db.log_trade_entry(
        symbol      = symbol,
        action      = decision.get("action", "BUY"),
        setup       = decision.get("setup", "UNKNOWN"),
        entry_price = decision.get("entry", 0),
        stop_price  = decision.get("stop", 0),
        tp_price    = decision.get("tp", 0),
        rsi_at_entry= rsi,
        volume_ratio= volume_ratio,
        market_bias = market_bias,
        confidence  = decision.get("confidence"),
        spy_rsi     = spy_rsi,
    )


def record_exit(symbol: str, exit_price: float, exit_reason: str, notes: str = ""):
    """
    Call when a position closes (stop hit, TP hit, EOD close).
    Automatically finds the open outcome_id for the symbol.
    """
    outcome_id = db.get_open_outcome_id(symbol)
    if outcome_id:
        db.log_trade_exit(outcome_id, exit_price, exit_reason, notes)
        print(f"  [Learner] Exit recorded: {symbol} @ {exit_price} | {exit_reason}")
    else:
        print(f"  [Learner] No open outcome found for {symbol} — skipping exit record.")
