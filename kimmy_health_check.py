import os, sys, json, sqlite3, subprocess, py_compile
from datetime import datetime, timezone, timedelta
sys.path.insert(0, "/root/trading-agent")
os.chdir("/root/trading-agent")

PASS = []; FAIL = []; WARN = []
def ok(msg):   PASS.append(msg); print("  OK  " + msg)
def fail(msg): FAIL.append(msg); print("  FAIL " + msg)
def warn(msg): WARN.append(msg); print("  WARN " + msg)
def section(t): print("\n" + "="*55 + "\n  " + t + "\n" + "="*55)

section("1. SYNTAX CHECK")
KEY_FILES = ["main.py","config.py","agent/claude_agent.py","signals/insider.py",
    "signals/options_flow.py","signals/future_growth.py","signals/early_discovery.py",
    "signals/technical.py","signals/congress.py","signals/market_context.py",
    "basket/curation.py","risk/manager.py","learning/tracker.py",
    "database/db.py","database/research_cache.py","summaries/reporter.py",
    "summaries/weekly_review.py","notifications/telegram_bot.py","broker/alpaca.py"]
for f in KEY_FILES:
    if not os.path.exists(f): fail("MISSING: "+f); continue
    try: py_compile.compile(f,doraise=True); ok("Syntax OK: "+f)
    except Exception as e: fail("Syntax ERR "+f+": "+str(e)[:50])

section("2. CONFIG VALUES")
import config
cfg_checks = [
    ("MIN_CONFIDENCE==7",        config.MIN_CONFIDENCE==7),
    ("MAX_POSITION_PCT==12.0",   config.MAX_POSITION_PCT==12.0),
    ("UW shadow disabled",       not config.UNUSUAL_WHALES_SHADOW_MODE),
    ("Telegram token set",       bool(config.TELEGRAM_BOT_TOKEN)),
    ("Anthropic key set",        bool(config.ANTHROPIC_API_KEY)),
    ("Alpaca key set",           bool(config.ALPACA_API_KEY)),
    ("FMP key set",              bool(config.FMP_API_KEY)),
    ("Finnhub key set",          bool(config.FINNHUB_API_KEY)),
    ("UW key set",               bool(config.UNUSUAL_WHALES_API_KEY)),
    ("CRYPTO_WATCHLIST empty",   config.CRYPTO_WATCHLIST==[]),
]
for msg, result in cfg_checks:
    ok(msg) if result else fail(msg)

section("3. THE 12 FIXES")
main_c   = open("main.py").read()
agent_c  = open("agent/claude_agent.py").read()
uw_c     = open("signals/options_flow.py").read()
track_c  = open("learning/tracker.py").read()
insider_c= open("signals/insider.py").read()
fixes = [
    ("Fix1 Hard cap 12%",               "Hard cap: 12% per position" in agent_c),
    ("Fix2 Growth scores mega tiers",   "growth_data   = future_growth.compute(symbol)" in main_c),
    ("Fix3 Insider buy/sell",           "buy_count" in insider_c and "sell_count" in insider_c),
    ("Fix4 UW reads config fresh",      "import config as _cfg" in uw_c),
    ("Fix5 Midpoint outcomes",          "record_midpoint_outcomes" in track_c),
    ("Fix5b Midpoint wired",            "record_midpoint_outcomes" in main_c),
    ("Fix6 Stale cache alert",          "STALE CACHE" in main_c or "CacheAlert" in main_c),
    ("Fix7 Candidate ranking",          "_rank_map" in main_c),
    ("Fix8 Early discovery module",     os.path.exists("signals/early_discovery.py")),
    ("Fix9 ATR stop guidance",          "ATR-based stops" in agent_c),
    ("Fix10 High prob setups",          "HIGH-PROBABILITY SETUPS" in agent_c),
    ("Fix11 Learning report",           "send_weekly_learning_report" in track_c),
    ("Fix11b Report wired",             "send_weekly_learning_report" in main_c),
    ("Fix12 BUCKET 5 cycles",           ">5 consecutive cycles" in agent_c),
]
for msg, result in fixes:
    ok(msg) if result else fail(msg)

section("4. DATABASE")
try:
    conn = sqlite3.connect("trading_agent.db")
    cur  = conn.cursor()
    for table in ["trades","snapshots","audit_log","signal_performance"]:
        cur.execute("SELECT COUNT(*) FROM "+table)
        ok("Table "+table+": "+str(cur.fetchone()[0])+" rows")
    cur.execute("SELECT equity, ts FROM snapshots ORDER BY rowid DESC LIMIT 1")
    row = cur.fetchone()
    if row:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row[1][:19]).replace(tzinfo=timezone.utc)).days
        ok("Latest snapshot: $"+str(int(row[0]))+", "+str(age)+"d ago")
    conn.close()
except Exception as e: fail("DB error: "+str(e))

section("5. SPY BASELINE")
try:
    b = json.load(open("spy_baseline.json"))
    ok("SPY baseline: $"+str(b["spy_price"])+" on "+str(b["date"]))
except Exception as e: fail("SPY baseline: "+str(e))

section("6. LEARNING WEIGHTS")
try:
    w = json.load(open("learning/weights.json"))
    for k,v in w.items():
        if not k.startswith("_"): ok("Weight "+k+": "+str(v))
except Exception as e: fail("Weights: "+str(e))

section("7. TELEGRAM")
try:
    from notifications import telegram_bot as tg
    ok("Token: "+str(bool(tg._TOKEN)))
    ok("ChatID: "+str(bool(tg._CHAT_ID)))
except Exception as e: fail("Telegram: "+str(e))

section("8. KIMMY SERVICE")
r = subprocess.getoutput("systemctl is-active kimmy")
ok("Service active") if r.strip()=="active" else fail("Service: "+r)
n = subprocess.getoutput("journalctl -u kimmy --since '1 hour ago' --no-pager | grep -c 'Scheduled restart'")
restarts = int(n.strip()) if n.strip().isdigit() else 0
ok("No crash loops: "+str(restarts)+" restarts") if restarts<3 else fail("CRASH LOOP: "+str(restarts)+" restarts!")

section("9. SIGNAL IMPORTS")
mods = [("signals.future_growth","compute"),("signals.options_flow","compute"),
        ("signals.early_discovery","compute"),("signals.technical","compute"),
        ("learning.tracker","record_midpoint_outcomes"),
        ("learning.tracker","send_weekly_learning_report")]
for mod,func in mods:
    try:
        m=__import__(mod,fromlist=[func])
        ok(mod+"."+func) if hasattr(m,func) else fail(mod+"."+func+" MISSING")
    except Exception as e: fail(mod+": "+str(e)[:50])

print("\n" + "="*55)
print("  HEALTH CHECK COMPLETE")
print("="*55)
print("  OK:   "+str(len(PASS)))
print("  WARN: "+str(len(WARN)))
print("  FAIL: "+str(len(FAIL)))
if FAIL:
    print("\n  FAILED:")
    for f in FAIL: print("    FAIL "+f)
if WARN:
    print("\n  WARNINGS:")
    for w in WARN: print("    WARN "+w)
total = len(PASS)+len(FAIL)+len(WARN)*0.5
grade = round((len(PASS)/total)*10,1) if total>0 else 0
print("\n  GRADE: "+str(grade)+"/10")
print("="*55)
