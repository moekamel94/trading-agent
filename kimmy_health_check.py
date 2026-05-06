# -*- coding: utf-8 -*-
import os, sys, json, sqlite3, subprocess, py_compile
from datetime import datetime, timezone, timedelta
sys.path.insert(0, '/root/trading-agent')
os.chdir('/root/trading-agent')

PASS=[]; FAIL=[]; WARN=[]
def ok(msg):   PASS.append(msg); print("  OK   "+str(msg))
def fail(msg): FAIL.append(msg); print("  FAIL "+str(msg))
def warn(msg): WARN.append(msg); print("  WARN "+str(msg))
def section(t): print("\n"+"="*58+"\n  "+t+"\n"+"="*58)

section("1. SYNTAX - ALL PYTHON FILES")
ALL_FILES = [
    "main.py","config.py","agent/claude_agent.py","agent/learner.py",
    "signals/insider.py","signals/options_flow.py","signals/future_growth.py",
    "signals/financial_data.py","signals/macro_regime.py","signals/technical.py",
    "signals/early_discovery.py","signals/sentiment.py","signals/congress.py",
    "signals/market_context.py","signals/momentum_news.py","signals/gap_scanner.py",
    "signals/research.py","signals/social.py","signals/fundamentals.py",
    "signals/uw_scanner.py","basket/curation.py","basket/manager.py",
    "basket/tier_criteria.py","basket/pending_removals.py",
    "risk/manager.py","learning/tracker.py","database/db.py",
    "database/research_cache.py","database/learning.py","database/paper_tracker.py",
    "database/decisions_log.py","summaries/reporter.py","summaries/weekly_review.py",
    "reports/biweekly_review.py","reports/monthly_deep_dive.py",
    "reports/earnings_reaction.py","notifications/telegram_bot.py",
    "notifications/discord_bot.py","broker/alpaca.py",
    "monitoring/health.py","monitoring/self_healer.py","monitoring/watchdog.py",
]
for f in ALL_FILES:
    if not os.path.exists(f): fail("MISSING: "+f); continue
    try: py_compile.compile(f, doraise=True); ok("Syntax: "+f)
    except Exception as e: fail("Syntax ERR "+f+": "+str(e)[:60])

section("2. CONFIG VALUES")
import config
cfg = [
    ("MIN_CONFIDENCE == 7",          config.MIN_CONFIDENCE == 7),
    ("MAX_POSITION_PCT == 12.0",     config.MAX_POSITION_PCT == 12.0),
    ("MAX_POSITIONS == 20",          config.MAX_POSITIONS == 20),
    ("UW shadow disabled",           not config.UNUSUAL_WHALES_SHADOW_MODE),
    ("CRYPTO_WATCHLIST empty",       config.CRYPTO_WATCHLIST == []),
    ("TELEGRAM_BOT_TOKEN set",       bool(config.TELEGRAM_BOT_TOKEN)),
    ("TELEGRAM_CHAT_ID set",         bool(config.TELEGRAM_CHAT_ID)),
    ("ANTHROPIC_API_KEY set",        bool(config.ANTHROPIC_API_KEY)),
    ("ALPACA_API_KEY set",           bool(config.ALPACA_API_KEY)),
    ("FMP_API_KEY set",              bool(config.FMP_API_KEY)),
    ("FINNHUB_API_KEY set",          bool(config.FINNHUB_API_KEY)),
    ("UW_API_KEY set",               bool(config.UNUSUAL_WHALES_API_KEY)),
    ("RUN_HOUR == 9",                config.RUN_HOUR == 9),
    ("RUN_MINUTE == 50",             config.RUN_MINUTE == 50),
    ("AFTERNOON_HOUR == 15",         config.AFTERNOON_HOUR == 15),
    ("CLOSE_SUMMARY_HOUR == 16",     config.CLOSE_SUMMARY_HOUR == 16),
    ("MIDDAY_HOUR == 12",            config.MIDDAY_HOUR == 12),
    ("CACHE_STALE_DAYS <= 14",       config.CACHE_STALE_DAYS <= 14),
    ("MIN_SPEC_CONFIDENCE == 7",     config.MIN_SPEC_CONFIDENCE == 7),
    ("MAX_SECTOR_PCT == 25.0",       config.MAX_SECTOR_PCT == 25.0),
]
for msg, result in cfg:
    ok(msg) if result else fail(msg)

section("3. THE 12 DEEP DIVE FIXES")
main_c    = open("main.py").read()
agent_c   = open("agent/claude_agent.py").read()
uw_c      = open("signals/options_flow.py").read()
track_c   = open("learning/tracker.py").read()
insider_c = open("signals/insider.py").read()
fixes = [
    ("Fix1  Hard cap 12%",             "Hard cap: 12% per position" in agent_c),
    ("Fix2  Growth scores mega",       "growth_data   = future_growth.compute(symbol)" in main_c),
    ("Fix3  Insider buy/sell",         "buy_count" in insider_c and "sell_count" in insider_c),
    ("Fix4  UW reads config fresh",    "import config as _cfg" in uw_c),
    ("Fix5  Midpoint outcomes",        "record_midpoint_outcomes" in track_c),
    ("Fix5b Midpoint wired",           "record_midpoint_outcomes" in main_c),
    ("Fix6  Stale cache alert",        "STALE CACHE" in main_c or "CacheAlert" in main_c),
    ("Fix7  Candidate ranking",        "_rank_map" in main_c),
    ("Fix8  Early discovery module",   os.path.exists("signals/early_discovery.py")),
    ("Fix9  ATR stop guidance",        "ATR-based stops" in agent_c),
    ("Fix10 High-prob setups",         "HIGH-PROBABILITY SETUPS" in agent_c),
    ("Fix11 Learning report",          "send_weekly_learning_report" in track_c),
    ("Fix11b Report wired",            "send_weekly_learning_report" in main_c),
    ("Fix12 BUCKET 5 cycles",          ">5 consecutive cycles" in agent_c),
    ("Fix12 DEFER option",             "DEFER until" in agent_c),
]
for msg, result in fixes:
    ok(msg) if result else fail(msg)

section("4. COMMITTEE PROMPT QUALITY")
prompt_checks = [
    ("CRS growth gate",              "CRS FAIL conditions" in agent_c),
    ("6-agent structure",            "6-AGENT INVESTMENT COMMITTEE" in agent_c),
    ("Conviction formula",           "CONVICTION FORMULA" in agent_c),
    ("Winner rule",                  "WINNER RULE" in agent_c),
    ("Exit signals",                 "EXIT SIGNALS" in agent_c),
    ("UW signals section",           "UNUSUAL WHALES SIGNALS" in agent_c),
    ("Macro signals",                "GLOBAL MACRO" in agent_c),
    ("Earnings momentum",            "EARNINGS MOMENTUM SIGNAL" in agent_c),
    ("Narrative stage",              "NARRATIVE STAGE" in agent_c),
    ("Tranche rule",                 "TRANCHE RULE" in agent_c),
    ("Post-earnings T+2",            "POST-EARNINGS T+2" in agent_c),
    ("Falsification gate",           "FALSIFICATION GATE" in agent_c),
    ("Anti-procrastination",         "ANTI-PROCRASTINATION" in agent_c),
    ("High-prob setups",             "HIGH-PROBABILITY SETUPS" in agent_c),
    ("ATR stops",                    "ATR-based stops" in agent_c),
]
for msg, result in prompt_checks:
    ok(msg) if result else fail(msg)

section("5. DATABASE HEALTH")
try:
    conn = sqlite3.connect("trading_agent.db")
    cur  = conn.cursor()
    for t in ["trades","snapshots","audit_log","signal_performance"]:
        cur.execute("SELECT COUNT(*) FROM "+t)
        n = cur.fetchone()[0]
        ok("Table "+t+": "+str(n)+" rows")
    cur.execute("SELECT equity, cash, ts FROM snapshots ORDER BY rowid DESC LIMIT 1")
    row = cur.fetchone()
    if row:
        age = int((datetime.now(timezone.utc) - datetime.fromisoformat(row[2][:19]).replace(tzinfo=timezone.utc)).total_seconds() // 3600)
        ok("Latest snapshot: $"+str(int(row[0]))+", cash=$"+str(int(row[1]))+", "+str(age)+"h ago")
        if age > 25: warn("Snapshot >25h old")
    cur.execute("SELECT COUNT(*) FROM trades WHERE action='BUY'")
    ok("Buy trades logged: "+str(cur.fetchone()[0]))
    conn.close()
except Exception as e: fail("DB error: "+str(e))

section("6. RESEARCH CACHE FRESHNESS")
try:
    from database import research_cache as rc
    cache = rc.load_all() if hasattr(rc,"load_all") else {}
    ok("Cache loaded: "+str(len(cache))+" tickers")
    conn2 = sqlite3.connect("trading_agent.db")
    cur2  = conn2.cursor()
    cur2.execute("SELECT DISTINCT symbol FROM trades WHERE action='BUY'")
    held = [r[0] for r in cur2.fetchall()]
    conn2.close()
    now = datetime.now(timezone.utc)
    stale = []
    for sym in held:
        cd = cache.get(sym, {})
        ts = cd.get("_cached_at") or cd.get("spec_refresh_ts","")
        if not ts: fail("Cache MISSING: "+sym); continue
        try:
            age = (now - datetime.fromisoformat(ts[:19]).replace(tzinfo=timezone.utc)).days
            if age > 7: warn("STALE "+sym+": "+str(age)+"d"); stale.append(sym)
            elif age > 5: warn("Aging "+sym+": "+str(age)+"d")
            else: ok("Fresh "+sym+": "+str(age)+"d")
        except: warn("Date parse err: "+sym)
    if not stale: ok("All held positions cache is fresh")
except Exception as e: fail("Cache error: "+str(e))

section("7. KEY DATA FILES")
data_files = [
    ("spy_baseline.json", True),
    ("learning/weights.json", True),
    ("basket/basket.json", True),
    ("basket/basket_mt.json", True),
    (".macro_regime.json", False),
    ("delisted_cache.json", False),
]
for fname, required in data_files:
    if os.path.exists(fname):
        size = os.path.getsize(fname)
        ok("Exists: "+fname+" ("+str(size)+"b)") if size > 10 else warn("Empty: "+fname)
    elif required: fail("MISSING required: "+fname)
    else: warn("Optional missing: "+fname)
try:
    b = json.load(open("spy_baseline.json"))
    ok("SPY baseline: $"+str(b.get("spy_price"))+" on "+str(b.get("date")))
except Exception as e: fail("SPY baseline: "+str(e))
try:
    w = json.load(open("learning/weights.json"))
    for k,v in w.items():
        if not k.startswith("_"): ok("Weight "+k+": "+str(v))
except Exception as e: fail("Weights: "+str(e))

section("8. BASKET QUALITY")
try:
    lt = json.load(open("basket/basket.json"))
    mt = json.load(open("basket/basket_mt.json"))
    lt_list = lt if isinstance(lt,list) else lt.get("tickers",[])
    mt_list = mt if isinstance(mt,list) else mt.get("tickers",mt.get("symbols",[]))
    ok("LT basket: "+str(len(lt_list))+" tickers")
    ok("MT basket: "+str(len(mt_list))+" tickers")
    if len(lt_list) > 70: warn("LT basket large ("+str(len(lt_list))+") - consider trimming")
    if len(lt_list) < 20: fail("LT basket too small: "+str(len(lt_list)))
except Exception as e: fail("Basket error: "+str(e))

section("9. NOTIFICATIONS")
try:
    from notifications import telegram_bot as tg
    ok("Telegram token: "+str(bool(tg._TOKEN)))
    ok("Telegram chat ID: "+str(bool(tg._CHAT_ID)))
except Exception as e: fail("Telegram: "+str(e))
try:
    from notifications import discord_bot as db2
    ok("Discord bot loads (routes to Telegram)")
    ok("send() present: "+str(hasattr(db2,"send")))
except Exception as e: fail("Discord bot: "+str(e))

section("10. KIMMY SERVICE")
r = subprocess.getoutput("systemctl is-active kimmy")
ok("Service: "+r) if r.strip()=="active" else fail("Service: "+r)
pid = subprocess.getoutput("systemctl show kimmy --property=MainPID --value")
ok("PID: "+pid.strip()) if pid.strip() and pid.strip()!="0" else fail("No PID")
n = subprocess.getoutput("journalctl -u kimmy --since '1 hour ago' --no-pager | grep -c 'Scheduled restart'")
restarts = int(n.strip()) if n.strip().isdigit() else 0
ok("No crash loops: "+str(restarts)) if restarts < 3 else fail("CRASH LOOP: "+str(restarts))
e2 = subprocess.getoutput("journalctl -u kimmy --since '2 hours ago' --no-pager | grep -i 'syntaxerror\|attributeerror\|importerror' | wc -l")
n2 = int(e2.strip()) if e2.strip().isdigit() else 0
ok("No code errors in 2h: "+str(n2)) if n2 == 0 else fail("Code errors in 2h: "+str(n2))

section("11. ALL SIGNAL IMPORTS")
mods = [
    ("signals.future_growth","compute"),
    ("signals.options_flow","compute"),
    ("signals.technical","compute"),
    ("signals.sentiment","compute"),
    ("signals.congress","compute"),
    ("signals.insider","compute"),
    ("signals.fundamentals","compute"),
    ("signals.market_context","compute"),
    ("signals.financial_data","compute"),
    ("signals.macro_regime","compute"),
    ("signals.early_discovery","compute"),
    ("signals.momentum_news","earnings_momentum"),
    ("learning.tracker","outcome_update"),
    ("learning.tracker","record_midpoint_outcomes"),
    ("learning.tracker","send_weekly_learning_report"),
    ("learning.tracker","compute_weights"),
    ("database.db","init"),
    ("database.research_cache","load"),
    ("basket.manager","load_combined"),
    ("risk.manager","check_stops"),
    ("summaries.reporter","run_premarket"),
    ("summaries.reporter","run_close"),
    ("summaries.weekly_review","run"),
]
for mod, func in mods:
    try:
        m = __import__(mod, fromlist=[func])
        ok(mod+"."+func) if hasattr(m,func) else fail(mod+"."+func+" MISSING")
    except Exception as e: fail(mod+": "+str(e)[:60])

section("12. SCHEDULER TIMING")
sched_checks = [
    ("Gap scan 8:45 ET",   config.GAP_SCAN_HOUR==8 and config.GAP_SCAN_MINUTE==45),
    ("Premarket 9:00 ET",  config.PREMARKET_SUMMARY_HOUR==9 and config.PREMARKET_SUMMARY_MINUTE==0),
    ("AM cycle 9:50 ET",   config.RUN_HOUR==9 and config.RUN_MINUTE==50),
    ("Midday 12:30 ET",    config.MIDDAY_HOUR==12 and config.MIDDAY_MINUTE==30),
    ("PM cycle 15:00 ET",  config.AFTERNOON_HOUR==15 and config.AFTERNOON_MINUTE==0),
    ("Close 16:05 ET",     config.CLOSE_SUMMARY_HOUR==16 and config.CLOSE_SUMMARY_MINUTE==5),
]
for msg, result in sched_checks:
    ok(msg) if result else fail(msg)

print("\n"+"="*58)
print("  KIMMY COMPREHENSIVE HEALTH CHECK - COMPLETE")
print("="*58)
print("  OK:   "+str(len(PASS)))
print("  WARN: "+str(len(WARN)))
print("  FAIL: "+str(len(FAIL)))
if FAIL:
    print("\n  FAILED ITEMS:")
    for item in FAIL: print("    FAIL "+item)
if WARN:
    print("\n  WARNINGS:")
    for item in WARN: print("    WARN "+item)
total = len(PASS)+len(FAIL)+len(WARN)*0.5
grade = round((len(PASS)/total)*10,1) if total > 0 else 0
print("\n  GRADE: "+str(grade)+"/10")
print("="*58)
