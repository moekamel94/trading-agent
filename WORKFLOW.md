# Kimmy Trading Agent — Full Workflow & Process Guide

**Goal:** 2× SPY annual return. Every decision, report, and role serves this single objective.

---

## THE TEAM (Roles & Responsibilities)

| Role | Who Runs It | When | Purpose |
|------|------------|------|---------|
| **CIO** (Chief Investment Officer) | Claude Opus/Sonnet (committee) | Every cycle | Final conviction score, sector allocation decisions |
| **CRS** (Chief Research Strategist) | Claude Opus/Sonnet (committee) | Every cycle | Growth thesis gate — must prove product + TAM story |
| **CCO** (Chief Compliance Officer) | Claude Opus/Sonnet (committee) | Every cycle | Risk gate — blocks overleveraged or ETF-tracking buys |
| **CRO** (Chief Risk Officer) | Claude Opus/Sonnet (committee) | Every cycle | Valuation risk flag, position sizing input |
| **PM** (Portfolio Manager) | Claude Opus/Sonnet (committee) | Every cycle | Final BUY/SELL/HOLD/TRIM/BUCKET decision |
| **QA** (Quantitative Analyst) | Claude Opus/Sonnet (committee) | Every cycle | Technical signals, momentum confirmation |
| **Report Employee** | Scheduled jobs | Daily/Weekly | Formats all Discord reports (premarket, close, weekly review) |
| **Analytics Employee** | `analytics_employee.py` | Daily close + Sunday | Tracks performance vs 2× SPY, signal accuracy, decision quality |

---

## DAILY CYCLE (Monday–Friday)

### Step 1: Data Collection (no Claude, ~8:30–9:30 ET)

**What happens:**
- `signals/` modules pull fresh data for every stock in the basket
- Sources used: yfinance (free), FMP (paid), Unusual Whales (paid), Finnhub (paid)
- Data saved to `research_cache.json` per ticker

**Data gathered per stock:**
- `fundamentals.py` → P/E, revenue growth, EPS growth, margins
- `technical.py` → RSI, SMA50/200, golden/death cross, 1m/3m return
- `sentiment.py` → news sentiment score
- `financial_data.py` → analyst targets, strong_buy/buy/hold/sell counts (FMP + Finnhub)
- `options_flow.py` → unusual call sweeps, dark pool accumulation (UW)
- `congress.py` → congressional trades last 7 days
- `insider.py` → insider buy/sell activity
- `future_growth.py` → growth score 0–10, narrative stage
- `momentum_news.py` → earnings momentum label

**Output:** `research_cache.json` — one entry per ticker, all signals stored

---

### Step 2: Gap Scanner (8:45 ET, no Claude)

**What happens:** `run_gap_scan()` in `main.py`
- Scans all held positions and basket for overnight gaps >4%
- Flags earnings today/tomorrow
- Checks for spec position milestone catalysts

**Output:** Alerts stored in DB for the 9:35 cycle to consume

---

### Step 3: Pre-market Report (9:00 ET)

**What happens:** `summaries/reporter.py → run_premarket()`
- Pulls NAV, cash %, positions
- Shows SPY 7d/30d vs portfolio performance
- Shows 2× SPY gap (are we ahead or behind target?)
- Shows macro regime (VIX, 10Y yield, CPI, Fed bias)
- Shows upcoming macro events
- Shows each holding: P&L, stop, **price target** (analyst target if no committee target), days held
- Shows committee agenda items (only if < 7 days old)

**Output:** Discord message every morning

---

### Step 4: Main Committee Cycle (9:50 ET + 3:30 PM ET)

This is the core of the system. It runs in 5 phases:

**Phase 1: Signal collection for all candidates**
- Loads combined basket (LT + MT)
- Runs `congress.py` + `uw_pending` to discover new tickers
- Runs regime-driven sector scan (finds new names in winning sectors)
- For each candidate: fetches fresh signals OR uses cached signals

**Phase 2: Preliminary filter (no Claude)**
- Scores each candidate on technicals + fundamentals
- Speculative tier: blocks falling knives (below SMA200 + MACD bearish)
- Non-speculative: needs prelim score ≥ threshold (UW flow adjusts score)
- Candidates that pass go to Claude; failures are SKIP'd

**Phase 3: Claude Committee Review**
The committee (all roles in one prompt) reviews each candidate and returns:
```
{
  "symbol": "NVDA",
  "action": "BUY|HOLD|SELL|TRIM|BUCKET",
  "final_confidence": 8,
  "allocation_pct": 3.0,
  "price_target": 1400,
  "thesis_break_criteria": "price_stop: $900 | revenue growth < 10%",
  "rationale": "...",
  ... (CRS, CCO, CRO, QA outputs)
}
```

**Phase 3b: Tranche scale-in**
- Checks if any existing T1 positions have hit T2 triggers (earnings beat, breakout above SMA50)
- Auto-adds T2/T3 allocation if triggered

**Phase 4: Options advisory**
- Never auto-executes options
- Proposes high-conviction options plays to Discord for human approval

**Phase 5: Options monitoring**
- Monitors live options positions for sell signals

**Output:** Discord summary of every BUY/SELL/HOLD + near-misses + exit watch

---

### Step 5: Close Report (4:05 PM ET)

**What happens:** `summaries/reporter.py → run_close()`
- Lists all trades executed today
- Shows NAV, cash %
- Calls `analytics_employee.run_daily_analytics()`:
  - If 30d portfolio is >5pp behind 2× SPY target → sends alert

**Output:** Discord close recap + analytics alert if underperforming

---

## WEEKLY CYCLE

### Friday 4:30 PM ET — Basket Review

**What happens:** `run_weekly_basket_review()` in `main.py`
- Reviews LT basket: adds/removes up to 3 tickers based on tier criteria
- Reviews MT basket: congress buys, earnings catalysts, sector rotation, UW discoveries
- Runs full research onboarding for any new tickers with no cache

**Output:** Discord message with basket changes

---

### Sunday 6:00 PM ET — Weekly Portfolio Review

**What happens:** `summaries/weekly_review.py → run()`

1. Pulls all open positions + technicals + cached fundamentals
2. Loads recent committee decisions (last 14 days) → **DECISION LOCK**
3. Checks macro regime → injects into prompt
4. Claude (Sonnet) reviews every position and recommends: HOLD/TRIM/EXIT/ADD
5. **Decision lock enforced:** If committee made a BUY within 14 days → EXIT blocked
6. Only executes EXIT if: stop loss hit (<-15% P&L) OR dead money (>90 days, <3% return)
7. Adds **Horizon Scan** section: macro catalysts next 4 weeks, next sector to inflect, what to accumulate

After weekly review, runs **Analytics Employee**:
- 7d/14d/30d performance vs 2× SPY target
- Signal accuracy from learning weights
- Decision quality (win rate, avg confidence)

**Output:** Discord weekly review + horizon scan + analytics digest

---

### Every Other Saturday 8:00 PM ET — Biweekly Deep Research

**What happens:** `reports/monthly_deep_dive.py → run()` (now biweekly)

This is the most thorough analysis. Top-down structure:

**Section A: Economy & Macro**
- Current economic phase (expansion/peak/contraction/trough)
- GDP trajectory, growth drivers
- Inflation/deflation forces — 3–6 month outlook
- Fed posture and likely next moves
- Credit conditions, consumer & corporate health

**Section B: Sector Rotation**
- Top 3 sectors right now and WHY (specific structural drivers)
- 2 weakening sectors and WHY
- Which sector is about to inflect (turning point)
- Our portfolio vs the rotation — are we correctly positioned?

**Section C: Sub-sector Opportunities**
- For each top 3 sectors: 2 hottest sub-themes
- Specific companies with upcoming catalysts
- New opportunities not yet in portfolio

**Section D: Thesis Reviews**
- Every held position: thesis intact/weakening/broken
- Action recommendation: ADD/HOLD/TRIM/EXIT
- 6-month price target
- Bull points, key risks, catalyst watch

**Section E: Portfolio Construction**
- Cash verdict (deploy/hold/raise more)
- Missing sector/factor exposures given current macro
- Rebalancing needed

**Output:** 5 Discord messages (one per section)

---

### Every Other Saturday 6:00 PM ET — Biweekly Committee Performance Review

**What happens:** `reports/biweekly_review.py → run_biweekly_review()`
- Reviews last 14 days of trade outcomes vs SPY
- Checks signal accuracy (did each signal predict correctly?)
- Reviews regime accuracy (did macro call lead to right sector bets?)
- Auto-applies parameter changes within safe bounds (max 20% delta per cycle)
- Flags ticker removals for human approval

**Output:** Discord performance report + auto-applied config changes

---

## HOW DECISIONS FLOW (The Chain of Custody)

```
MACRO REGIME
     ↓
Sector weights (which sectors to concentrate/avoid)
     ↓
Basket curation (which tickers to include/remove)
     ↓
Daily signal collection (per-ticker data)
     ↓
Preliminary filter (no Claude — fast pass/fail)
     ↓
Claude Committee (BUY/SELL/HOLD/BUCKET decisions)
     ↓
Risk Manager validation (position limits, stops)
     ↓
Trade execution (Alpaca)
     ↓
Decision logged to DB (decisions_log table)
     ↓
Learning tracker (7d/14d outcome check)
     ↓
Adaptive weights updated (which signals were right)
     ↓
Biweekly review (process improvement)
```

---

## DECISION CONSISTENCY RULES

To prevent the same stock being bought Monday and sold Sunday:

1. **Decision Lock:** Weekly review checks the last 14 days of committee BUY decisions. It CANNOT recommend EXIT on a symbol bought within 14 days unless:
   - Hard stop loss triggered (P&L < -15%)
   - Dead money: held >90 days with <3% return

2. **Macro Alignment:** Weekly review receives the current macro regime. It cannot recommend EXIT on a defense stock when macro regime is GEOPOLITICALLY_STRESSED.

3. **Committee Authority:** The committee (daily cycle) is the only entity that can make BUY decisions. The weekly review can flag concerns but not override committee buys within the lock window.

4. **Price Targets:** Every held position has a price target shown in pre-market. Targets come from: (1) committee decision, (2) analyst consensus, (3) "No target set" flag.

---

## HOW REPORTS ARE USED

| Report | When | Who reads it | Decision it informs |
|--------|------|-------------|---------------------|
| Pre-market | 9 AM daily | You | Context before market open, see all holdings with targets/stops |
| Committee cycle | 9:50 AM + 3:30 PM | System (auto) | BUY/SELL/HOLD trade execution |
| Close recap | 4:05 PM | You | What was traded today, daily performance |
| Analytics alert | Close (only if behind) | You | Signals when process is underperforming vs target |
| Weekly review | Sunday | You + system | Position health check, horizon planning |
| Analytics digest | Sunday | You | Performance accountability vs 2× SPY |
| Biweekly deep dive | Every other Saturday | You | Strategic repositioning, sector thesis updates |
| Biweekly perf review | Every other Saturday | System (auto) | Process parameter tuning |
| Basket review | Friday 4:30 PM | System (auto) | Ticker additions/removals |

---

## PAID API USAGE (What Each Pays For)

| API | Cost | What it provides | Impact on decisions |
|-----|------|-----------------|---------------------|
| **Financial Modeling Prep (FMP)** | ~$100/mo | Analyst targets, strong_buy/buy/hold/sell counts, screener for new tickers, earnings data | Analyst consensus in committee prompt; basket curation screener |
| **Unusual Whales (UW)** | ~$99/mo | Call sweep detection, dark pool accumulation, IV rank, short interest, sector flow | Preliminary score +2 for strong_accumulation; conviction bonus for validated sweeps |
| **Finnhub** | Free tier | News headlines, earnings dates | Pre-market news feed, earnings alerts |
| **FRED** | Free | CPI, Fed funds rate, yield curve | Macro regime classification |
| **Alpaca** | Brokerage | Trade execution, positions, portfolio | Everything |

**If a paid API is not contributing to decisions:** the biweekly committee review section "needs_work" will flag it. Currently, UW shadow mode requires 10 samples before adjusting conviction — it may not be fully contributing until that threshold is met.

---

## WHERE TO LOOK WHEN SOMETHING IS WRONG

| Symptom | Where to check |
|---------|---------------|
| Wrong decision made (bought then sold) | `basket/committee_agenda.json` for stale directives; check if weekly review respected decision lock |
| Missing price target in pre-market | `summaries/reporter.py` → tranche data or analyst target in research cache |
| Friday basket review not sent | APScheduler logs; check if `weekly_basket_review` job fired |
| Stale committee agenda items showing | `basket/committee_agenda.json` → items older than 7 days are filtered; check `added` dates |
| Contradicting hold/sell recommendation | Weekly review now has decision lock — check `decisions_log` DB table |
| Signal not contributing to decisions | `learning/weights.json` — if weight < 0.9 or still at baseline 1.0 (< 10 samples), signal not calibrated |
| Monthly deep dive not appearing | It runs biweekly (every 14 days) on Saturdays — check if ≥14 days since last run |
| Analytics not tracking | `reports/analytics_employee.py` → needs `snapshots` DB table to have historical NAV data |

---

## WHAT STILL NEEDS HUMAN INPUT

The system is designed to run autonomously, but the following ALWAYS require your approval:

1. **Ticker removals from LT basket** — system flags them to Discord, never auto-removes
2. **Options trades** — system proposes, never executes; you place manually
3. **New speculative tier stocks** — require your confirmation before adding
4. **Override of committee decisions** — if you want to manually buy/sell, use the `--dry-run` flag first to see what the committee thinks

---

*Document version: 2026-05-04*
