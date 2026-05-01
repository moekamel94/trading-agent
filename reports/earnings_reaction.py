"""
Overnight earnings reaction module.

Runs at 7:30 AM ET (Mon-Fri). Fetches earnings reported after yesterday's
close and any pre-market reports this morning. For each event:

  1. Computes EPS and revenue beat/miss vs consensus + surprise %
  2. Reads guidance change (raised / maintained / lowered / withdrawn)
  3. Measures the pre-market price gap vs yesterday's close
  4. Classifies impact: held_position | basket_candidate | sector_read
  5. Sends full data to committee (Sonnet) for a structured action plan

Output schema (JSON):
  immediate_actions  — held positions: hold / add_tranche / trim / exit
  entry_opportunities — unowned basket tickers: enter now / watch / pass
  sector_reads       — what one company's result implies for peers we hold
  summary            — 2-sentence brief for Discord

Wired into the premarket briefing via reporter.run_premarket() and as
a standalone scheduler job.
"""

import json
import re
import requests
from datetime import datetime, timezone, timedelta

import anthropic
import yfinance as yf

import config
from database import db
from notifications import discord_bot as discord


_FMP_BASE    = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 10


# ── Data collection ───────────────────────────────────────────────────────────

def _fmp_get(path: str, params: dict) -> list | dict | None:
    if not config.FMP_API_KEY:
        return None
    try:
        r = requests.get(
            f"{_FMP_BASE}/{path}",
            params={"apikey": config.FMP_API_KEY, **params},
            timeout=_FMP_TIMEOUT,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _fetch_overnight_earnings() -> list[dict]:
    """
    Return earnings events for:
      - yesterday after-hours (reported after 16:00 ET yesterday)
      - today before market open (pre-market)

    FMP earning-calendar returns: symbol, date, eps, epsEstimated,
    revenue, revenueEstimated, fiscalDateEnding, updatedFromDate.
    """
    today     = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)

    # Fetch a 2-day window — yesterday and today
    data = _fmp_get(
        "earnings-calendar",
        {"from": str(yesterday), "to": str(today)},
    )
    if not data or not isinstance(data, list):
        return []

    results = []
    for item in data:
        sym = item.get("symbol", "")
        if not sym or "/" in sym:
            continue
        results.append({
            "symbol":             sym,
            "date":               item.get("date"),
            "eps_actual":         item.get("epsActual"),
            "eps_estimate":       item.get("epsEstimated"),
            "revenue_actual":     item.get("revenueActual"),
            "revenue_estimate":   item.get("revenueEstimated"),
            "fiscal_date_ending": item.get("fiscalDateEnding"),
        })
    return results


def _compute_surprise(actual, estimate) -> dict:
    """Return beat/miss/in-line label + surprise % for a single metric."""
    if actual is None or estimate is None or estimate == 0:
        return {"result": "unknown", "surprise_pct": None}
    surprise_pct = round((actual - estimate) / abs(estimate) * 100, 1)
    if surprise_pct >= 3:
        result = "beat"
    elif surprise_pct <= -3:
        result = "miss"
    else:
        result = "in_line"
    return {"result": result, "surprise_pct": surprise_pct}


def _fetch_guidance(symbol: str) -> str:
    """
    Pull the most recent earnings press-release headline from FMP to infer
    guidance tone. Returns: raised | maintained | lowered | withdrawn | unknown.
    """
    data = _fmp_get("press-releases", {"symbol": symbol, "limit": 3})
    if not data or not isinstance(data, list):
        return "unknown"

    combined = " ".join((d.get("text") or d.get("title") or "") for d in data[:3]).lower()

    if any(w in combined for w in ["raises guidance", "raises outlook", "increased guidance",
                                   "raised full-year", "raises full year"]):
        return "raised"
    if any(w in combined for w in ["lowers guidance", "reduces guidance", "cuts guidance",
                                   "lowered outlook", "lowered full-year"]):
        return "lowered"
    if any(w in combined for w in ["withdraws guidance", "withdrawn guidance",
                                   "suspending guidance", "suspended guidance"]):
        return "withdrawn"
    if any(w in combined for w in ["reaffirms", "maintains guidance", "reiterates",
                                   "in line with", "consistent with"]):
        return "maintained"
    return "unknown"


def _premarket_gap(symbol: str) -> float | None:
    """
    Compute pre-market gap % = (pre_market_price / prev_close - 1) × 100.
    Uses yfinance fast_info; falls back to prepost history.
    """
    try:
        ticker = yf.Ticker(symbol)
        info   = ticker.fast_info

        pre   = getattr(info, "last_price", None)
        prev  = getattr(info, "previous_close", None)

        # fast_info.last_price reflects pre/post market when outside regular hours
        if pre and prev and prev > 0:
            return round((pre / prev - 1) * 100, 2)

        # Fallback: prepost history
        hist = ticker.history(period="2d", prepost=True)
        if len(hist) >= 2:
            last     = float(hist["Close"].iloc[-1])
            reg_prev = float(hist["Close"].iloc[-2])
            if reg_prev > 0:
                return round((last / reg_prev - 1) * 100, 2)
    except Exception:
        pass
    return None


def _enrich_earnings_events(raw_events: list[dict],
                             held_symbols: set[str],
                             basket_symbols: set[str]) -> list[dict]:
    """
    For each earnings event: add beat/miss, guidance, pre-market gap,
    and classify as held_position | basket_candidate | sector_read | irrelevant.
    Returns only events that are relevant to our portfolio or basket.
    """
    enriched = []
    for ev in raw_events:
        sym     = ev["symbol"]
        sector  = config.SECTOR_MAP.get(sym)

        # Classification
        if sym in held_symbols:
            classification = "held_position"
        elif sym in basket_symbols:
            classification = "basket_candidate"
        elif sector is not None:
            classification = "sector_read"
        else:
            continue  # not relevant to us

        eps_surprise = _compute_surprise(ev.get("eps_actual"), ev.get("eps_estimate"))
        rev_surprise = _compute_surprise(ev.get("revenue_actual"), ev.get("revenue_estimate"))
        guidance     = _fetch_guidance(sym)
        gap_pct      = _premarket_gap(sym)

        enriched.append({
            "symbol":           sym,
            "sector":           sector or "unknown",
            "classification":   classification,
            "eps_actual":       ev.get("eps_actual"),
            "eps_estimate":     ev.get("eps_estimate"),
            "eps_surprise":     eps_surprise,
            "revenue_actual":   ev.get("revenue_actual"),
            "revenue_estimate": ev.get("revenue_estimate"),
            "revenue_surprise": rev_surprise,
            "guidance":         guidance,
            "premarket_gap_pct": gap_pct,
            "fiscal_date":      ev.get("fiscal_date_ending"),
        })

    return enriched


def _build_held_context(held_tranches: list[dict]) -> dict[str, dict]:
    """Return {symbol: {catalyst_note, thesis_break_criteria, price_target, confidence}} for open positions."""
    return {
        t["symbol"]: {
            "catalyst_note":        (t.get("catalyst_note") or "")[:200],
            "thesis_break_criteria": (t.get("thesis_break_criteria") or "")[:200],
            "price_target":         t.get("price_target"),
            "confidence":           t.get("final_confidence"),
        }
        for t in held_tranches
    }


# ── Committee prompt ─────────────────────────────────────────────────────────

def _build_reaction_prompt(events: list[dict], held_context: dict) -> str:
    def fmt(x):
        return json.dumps(x, indent=2)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    held    = [e for e in events if e["classification"] == "held_position"]
    basket  = [e for e in events if e["classification"] == "basket_candidate"]
    sector  = [e for e in events if e["classification"] == "sector_read"]

    held_enriched = []
    for e in held:
        ctx = held_context.get(e["symbol"], {})
        held_enriched.append({**e, "our_thesis": ctx})

    return f"""OVERNIGHT EARNINGS REACTION BRIEF — {date_str}

Pre-market gap = (last pre-market price / yesterday close - 1).
Guidance: raised | maintained | lowered | withdrawn | unknown.
EPS/revenue surprise: beat (≥+3%) | in_line | miss (≤-3%).

═══════════════════════════════════════
HELD POSITIONS THAT REPORTED
═══════════════════════════════════════
{fmt(held_enriched) if held_enriched else "None"}

═══════════════════════════════════════
BASKET CANDIDATES THAT REPORTED (not currently held)
═══════════════════════════════════════
{fmt(basket) if basket else "None"}

═══════════════════════════════════════
SECTOR READS (peers/competitors that reported)
═══════════════════════════════════════
{fmt(sector) if sector else "None"}

═══════════════════════════════════════
COMMITTEE TASK
═══════════════════════════════════════
You are the Kimmy investment committee. React to these earnings results
with the same rigour as an opening-bell decision meeting.

For HELD positions: decide immediately — do not equivocate.
For BASKET candidates: assess whether the result creates an entry setup.
For SECTOR reads: assess what the result implies for our held peers.

Return a SINGLE JSON object:
{{
  "immediate_actions": [
    {{
      "symbol":    "TICKER",
      "action":    "hold | add_tranche | trim | exit",
      "urgency":   "premarket | at_open | wait_for_settle | no_rush",
      "rationale": "1-2 sentences grounded in the actual numbers",
      "condition": "exact price or signal condition, if action is conditional"
    }}
  ],
  "entry_opportunities": [
    {{
      "symbol":       "TICKER",
      "action":       "enter_now | watch | pass",
      "why":          "what in the result makes this attractive or unattractive",
      "entry_setup":  "specific setup — e.g. 'gap and hold above $X on open'",
      "size_guidance": "full | half | small",
      "invalidation": "what pre-market/open behaviour kills the setup"
    }}
  ],
  "sector_reads": [
    {{
      "reporting_company": "TICKER",
      "sector":            "sector_key",
      "implication":       "what this result signals for the sector",
      "affected_holdings": ["TICKER1", "TICKER2"],
      "action":            "increase_conviction | reduce_conviction | no_change",
      "note":              "one concrete suggestion for the held positions"
    }}
  ],
  "summary": "2-sentence brief: what happened overnight and the single most important action"
}}

Rules:
- If EPS beat + guidance raised + gap > 0: lean toward add_tranche or enter_now
- If EPS miss + guidance lowered: check thesis_break_criteria — if met, exit
- If gap > 8% at open: wait_for_settle before entering (chasing gaps is a known loss)
- If gap > 15% miss: assess exit before open for held positions
- urgency=premarket only if the action genuinely cannot wait for regular hours
- All exits of held positions must note whether thesis_break_criteria was explicitly met
"""


# ── Main entry point ─────────────────────────────────────────────────────────

def _has_relevant_earnings_today(held_symbols: set[str], basket_symbols: set[str]) -> bool:
    """
    Lightweight calendar pre-check. Returns True only if a held or basket symbol
    is scheduled to report overnight (yesterday AH or today pre-market).
    Avoids fetching enrichment data on days with nothing relevant.
    """
    today     = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    relevant  = held_symbols | basket_symbols
    data = _fmp_get("earnings-calendar", {"from": str(yesterday), "to": str(today)})
    if not data or not isinstance(data, list):
        return False
    return any(item.get("symbol") in relevant for item in data)


def run_earnings_reaction(dry_run: bool = False) -> dict:
    """
    Fetch overnight earnings, enrich, send to committee, return reaction plan.
    Only runs when a held position or basket candidate has reported — silent otherwise.
    """
    # Current portfolio state (needed for pre-check)
    held_tranches  = db.get_all_tranches()
    held_symbols   = {t["symbol"] for t in held_tranches}
    basket_symbols = set(config.TICKER_TIERS.keys())

    # Fast exit: don't fetch enrichment data if nothing we care about reported
    if not _has_relevant_earnings_today(held_symbols, basket_symbols):
        print("  [Earnings] No held/basket symbols reporting today — skipping.")
        return {"summary": "No relevant earnings.", "immediate_actions": [], "entry_opportunities": [], "sector_reads": []}

    print("\n" + "="*60)
    print("OVERNIGHT EARNINGS REACTION")
    print("="*60)

    held_context   = _build_held_context(held_tranches)

    # Fetch and enrich earnings
    raw    = _fetch_overnight_earnings()
    events = _enrich_earnings_events(raw, held_symbols, basket_symbols)

    if not events:
        print("  No relevant earnings overnight — skipping.")
        return {"summary": "No relevant earnings overnight.", "immediate_actions": [], "entry_opportunities": [], "sector_reads": []}

    held_events   = [e for e in events if e["classification"] == "held_position"]
    basket_events = [e for e in events if e["classification"] == "basket_candidate"]
    sector_events = [e for e in events if e["classification"] == "sector_read"]

    print(f"  {len(events)} relevant events: "
          f"{len(held_events)} held, "
          f"{len(basket_events)} basket, "
          f"{len(sector_events)} sector reads")

    # Only run the committee if there's something actionable:
    # - a held position reported (always act), OR
    # - a basket candidate with a significant beat/miss or ±3%+ gap (entry opportunity)
    # Sector reads alone are not enough to trigger a Sonnet call or Discord alert.
    significant_basket = [e for e in basket_events
                          if abs(e.get("premarket_gap_pct") or 0) >= 3
                          or e.get("eps_surprise", {}).get("result") in ("beat", "miss")]
    if not held_events and not significant_basket:
        print(f"  No actionable events ({len(basket_events)} basket, {len(sector_events)} sector reads) — skipping.")
        return {"summary": "No actionable earnings.", "immediate_actions": [], "entry_opportunities": [], "sector_reads": []}

    # Limit basket events to significant ones to keep the prompt focused
    active_events = held_events + significant_basket + sector_events[:3]
    events = active_events

    prompt = _build_reaction_prompt(events, held_context)

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    resp   = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        system=(
            "You are the Kimmy investment committee — CIO, Chief Research Strategist, "
            "Chief Risk Officer, Quantitative Analyst, Data Analyst, and Portfolio Manager — "
            "reacting to overnight earnings before market open. "
            "Be direct and decisive. Every held position that reported needs a clear action. "
            "Do not hedge. Do not say 'monitor'. Say what to do and when."
        ),
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = resp.content[0].text.strip()
    report = None
    start = raw_text.find('{')
    end = raw_text.rfind('}') + 1
    if start != -1 and end > start:
        candidate = raw_text[start:end]
        candidate = re.sub(r',\s*([}\]])', r'\1', candidate)
        try:
            report = json.loads(candidate)
        except json.JSONDecodeError:
            pass
    if report is None:
        print("  [Earnings] Failed to parse committee response")
        print("  Raw response:\n", raw_text[:500])
        report = {"raw": raw_text, "parse_error": True,
                  "immediate_actions": [], "entry_opportunities": [], "sector_reads": []}

    _print_reaction(report, events)

    if not dry_run:
        _send_discord_alert(report, events)

    return report


# ── Output ────────────────────────────────────────────────────────────────────

def _print_reaction(report: dict, events: list[dict]) -> None:
    print(f"\n{'─'*60}")
    print(f"SUMMARY: {report.get('summary', 'No summary')}")
    print(f"{'─'*60}")

    if report.get("immediate_actions"):
        print("\n🚨 HELD POSITIONS — IMMEDIATE ACTIONS:")
        for a in report["immediate_actions"]:
            urgency = a.get("urgency", "").upper()
            action  = a.get("action", "").upper()
            cond    = f"  (if: {a['condition']})" if a.get("condition") else ""
            print(f"  [{urgency}] {a.get('symbol','')} → {action}{cond}")
            print(f"    {a.get('rationale','')}")

    if report.get("entry_opportunities"):
        print("\n🎯 ENTRY OPPORTUNITIES:")
        for e in report["entry_opportunities"]:
            action = e.get("action", "").upper()
            size   = e.get("size_guidance", "")
            print(f"  [{action}] {e.get('symbol','')} ({size} size)")
            print(f"    Setup:  {e.get('entry_setup','')}")
            print(f"    Kills:  {e.get('invalidation','')}")

    if report.get("sector_reads"):
        print("\n📡 SECTOR READS:")
        for s in report["sector_reads"]:
            action    = s.get("action", "").upper()
            affected  = ", ".join(s.get("affected_holdings", []))
            print(f"  {s.get('reporting_company','')} ({s.get('sector','')}) → {action}")
            if affected:
                print(f"    Affects: {affected} — {s.get('note','')}")

    print(f"{'─'*60}\n")


def _send_discord_alert(report: dict, events: list[dict]) -> None:
    lines = ["📊 **OVERNIGHT EARNINGS REACTION**", ""]

    summary = report.get("summary", "")
    if summary:
        lines += [summary, ""]

    immediate = report.get("immediate_actions", [])
    if immediate:
        lines.append("**🚨 Open Positions — Actions:**")
        for a in immediate:
            urgency = a.get("urgency", "")
            action  = a.get("action", "hold").upper()
            cond    = f" _(if {a['condition']})_" if a.get("condition") else ""
            lines.append(f"`{a.get('symbol','')}` → **{action}**{cond} [{urgency}]")
            lines.append(f"  _{a.get('rationale','')}_")
        lines.append("")

    opps = [e for e in report.get("entry_opportunities", []) if e.get("action") == "enter_now"]
    if opps:
        lines.append("**🎯 Entry Setups:**")
        for e in opps:
            lines.append(f"`{e.get('symbol','')}` ({e.get('size_guidance','')}) — {e.get('entry_setup','')}")
        lines.append("")

    sector_alerts = [s for s in report.get("sector_reads", [])
                     if s.get("action") in ("increase_conviction", "reduce_conviction")]
    if sector_alerts:
        lines.append("**📡 Sector Reads:**")
        for s in sector_alerts:
            action   = s.get("action", "").replace("_", " ").upper()
            affected = ", ".join(s.get("affected_holdings", []))
            lines.append(f"{s.get('reporting_company','')} → **{action}**"
                         + (f" (affects {affected})" if affected else ""))

    discord.send("\n".join(lines))
