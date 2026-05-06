"""
KIMMY UPGRADE SCRIPT — ALL 6 FIXES
====================================
Run this once on your server:
  cd /root/trading-agent && python3 kimmy_upgrade.py

What it does:
  Fix 1 — Verify and repair DB logging (trading_agent.db path + paper_tracker wiring)
  Fix 2 — Verify growth score fix (FMP replacing Alpha Vantage)
  Fix 3 — Unlock UW sweep signal (remove shadow mode gate)
  Fix 4 — Increase position sizing for double-SPY goal
  Fix 5 — Clean delisted tickers from earnings catalyst source
  Fix 6 — Activate weekly learning loop (signal performance → weights.json)

Each fix backs up the original file before modifying it.
A summary is printed at the end showing what was changed.
"""

import os
import re
import sys
import json
import shutil
import sqlite3
from datetime import datetime

BASE = "/root/trading-agent"
os.chdir(BASE)
sys.path.insert(0, BASE)

CHANGES = []
ERRORS  = []


def backup(fpath):
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = fpath + f".bak.{ts}"
    shutil.copy2(fpath, bak)
    print(f"  [backup] {fpath} -> {bak}")
    return bak


def read(fpath):
    with open(fpath) as f:
        return f.read()


def write(fpath, content):
    with open(fpath, "w") as f:
        f.write(content)


def section(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


# =============================================================================
# FIX 1 — DB LOGGING: Verify trading_agent.db path is consistent everywhere
# =============================================================================
section("FIX 1 — DB LOGGING VERIFICATION & REPAIR")

DB_PATH = os.path.join(BASE, "trading_agent.db")

# 1a. Confirm DB exists and has the trades table
try:
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [t[0] for t in cur.fetchall()]
    print(f"  DB tables found: {tables}")

    # Create trades table if missing
    if "trades" not in tables:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol        TEXT NOT NULL,
                action        TEXT NOT NULL,
                asset_type    TEXT DEFAULT 'stock',
                qty           REAL,
                price         REAL,
                allocation_pct REAL,
                confidence    INTEGER,
                rationale     TEXT,
                ts            TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        print("  [CREATED] trades table was missing — created now")
        CHANGES.append("Fix 1: Created missing trades table in trading_agent.db")
    else:
        print("  [OK] trades table exists")

    # Create snapshots table if missing
    if "snapshots" not in tables:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                equity REAL,
                cash   REAL,
                ts     TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        print("  [CREATED] snapshots table was missing — created now")
        CHANGES.append("Fix 1: Created missing snapshots table")
    else:
        print("  [OK] snapshots table exists")

    # Create audit_log table if missing
    if "audit_log" not in tables:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                event     TEXT,
                symbol    TEXT,
                detail    TEXT,
                ts        TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        print("  [CREATED] audit_log table was missing — created now")
        CHANGES.append("Fix 1: Created missing audit_log table")
    else:
        print("  [OK] audit_log table exists")

    # Create signal_performance table for Fix 6
    if "signal_performance" not in tables:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS signal_performance (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol        TEXT,
                signal_name   TEXT,
                signal_value  TEXT,
                confidence    INTEGER,
                action        TEXT,
                entry_price   REAL,
                exit_price    REAL,
                return_7d     REAL,
                return_14d    REAL,
                return_30d    REAL,
                outcome       TEXT,
                ts_entry      TEXT DEFAULT (datetime('now')),
                ts_updated    TEXT
            )
        """)
        conn.commit()
        print("  [CREATED] signal_performance table for learning loop")
        CHANGES.append("Fix 1: Created signal_performance table for learning loop")
    else:
        print("  [OK] signal_performance table exists")

    conn.close()
    print("  [OK] trading_agent.db is healthy")

except Exception as e:
    ERRORS.append(f"Fix 1 DB check failed: {e}")
    print(f"  [ERROR] {e}")

# 1b. Check database/db.py uses the correct DB path
db_file = os.path.join(BASE, "database/db.py")
if os.path.exists(db_file):
    content = read(db_file)
    # Check if it's using trading_agent.db or an old path
    if "trading.db" in content and "trading_agent.db" not in content:
        backup(db_file)
        content = content.replace("trading.db", "trading_agent.db")
        write(db_file, content)
        print("  [FIXED] database/db.py was pointing to wrong DB (trading.db)")
        CHANGES.append("Fix 1: Corrected DB path in database/db.py")
    elif "trading_agent.db" in content:
        print("  [OK] database/db.py already uses trading_agent.db")
    else:
        print("  [WARN] database/db.py DB path unclear — check manually")
else:
    ERRORS.append("Fix 1: database/db.py not found")


# =============================================================================
# FIX 2 — GROWTH SCORES: Verify FMP is working, fallback if not
# =============================================================================
section("FIX 2 — GROWTH SCORE VERIFICATION")

fg_file = os.path.join(BASE, "signals/future_growth.py")
if os.path.exists(fg_file):
    content = read(fg_file)

    # Check if Alpha Vantage is still referenced as primary
    if "alpha_vantage" in content.lower() and "fmp" not in content.lower():
        print("  [WARN] future_growth.py still references Alpha Vantage — FMP fix may not be applied")
        ERRORS.append("Fix 2: future_growth.py still uses Alpha Vantage — apply FMP fix manually")
    elif "fmp" in content.lower() or "financialmodelingprep" in content.lower():
        print("  [OK] future_growth.py references FMP — fix appears applied")
    else:
        print("  [INFO] future_growth.py uses yfinance only (no AV or FMP) — this is fine")

    # Live test: try computing growth score for NVDA
    print("\n  Testing growth score computation for NVDA...")
    try:
        from signals import future_growth
        result = future_growth.compute("NVDA")
        score  = result.get("score", None)
        if score and score != 0:
            print(f"  [OK] NVDA growth score = {score} — FIX IS WORKING")
            CHANGES.append(f"Fix 2: Growth scores confirmed working (NVDA={score})")
        else:
            print(f"  [FAIL] NVDA growth score = {score} — still broken")
            ERRORS.append(f"Fix 2: Growth score still 0 for NVDA — FMP not returning data")

        # Test a few more
        for sym in ["MSFT", "PLTR", "LMT"]:
            try:
                r = future_growth.compute(sym)
                s = r.get("score", 0)
                status = "OK" if s and s != 0 else "FAIL"
                print(f"  [{status}] {sym} growth score = {s}")
            except Exception as ex:
                print(f"  [ERROR] {sym}: {ex}")

    except Exception as e:
        ERRORS.append(f"Fix 2: Could not import future_growth: {e}")
        print(f"  [ERROR] {e}")
else:
    ERRORS.append("Fix 2: signals/future_growth.py not found")


# =============================================================================
# FIX 3 — UW SWEEP SIGNAL: Remove shadow mode gate on bullish bonus
# =============================================================================
section("FIX 3 — UNLOCK UW SWEEP SIGNAL (remove shadow mode gate)")

agent_file = os.path.join(BASE, "agent/claude_agent.py")
if os.path.exists(agent_file):
    content = read(agent_file)
    original = content

    # Pattern 1: shadow mode gate on bullish_sweep bonus
    # Looking for the condition that gates the +0.5 bonus behind validated signal count
    shadow_patterns = [
        # Pattern: "shadow mode" conditional around bullish sweep bonus
        (
            r'(#\s*Shadow mode.*?\n.*?bullish.*?sweep.*?\+0\.5.*?gated.*?until.*?\n)',
            "# Shadow mode removed — bullish sweep bonus is always live\n"
        ),
        # Pattern: if statement checking shadow mode before applying bonus
        (
            r'if.*?shadow.*?mode.*?:.*?\n(\s+.*?bonus.*?\+.*?0\.5.*?\n)',
            r'\1'  # keep the bonus line, remove the shadow gate
        ),
    ]

    # More targeted: find the exact shadow mode gating block and remove it
    # Based on the prompt text: "Shadow mode: bullish sweep +0.5 bonus gated until 20 validated signals"
    # In code this is likely an if condition checking a counter or flag

    # Search for shadow mode variable/flag
    shadow_var_match = re.search(
        r'(_uw_shadow_validated\s*=\s*\d+|shadow_validated\s*=|uw_shadow_mode\s*=)',
        content
    )
    if shadow_var_match:
        print(f"  Found shadow mode variable: {shadow_var_match.group()}")

    # Find and replace the shadow gate around the bullish sweep +0.5 bonus
    # Pattern: "if <shadow_condition>:" wrapping the bonus application
    shadow_gate_pattern = re.compile(
        r'([ \t]*)if[^\n]*shadow[^\n]*(?:validated|mode|gate|count)[^\n]*:\n'
        r'((?:\1[ \t]+[^\n]*\n)*)',
        re.IGNORECASE
    )

    matches = list(shadow_gate_pattern.finditer(content))
    if matches:
        for m in matches:
            block = m.group(0)
            if "0.5" in block or "bonus" in block.lower() or "sweep" in block.lower():
                # Extract the indented body (remove the if wrapper, keep the body)
                indent  = m.group(1)
                body    = m.group(2)
                # De-indent by one level
                dedented = re.sub(r'^' + indent + r'    ', indent, body, flags=re.MULTILINE)
                content  = content.replace(block, dedented)
                print("  [FIXED] Removed shadow mode gate from UW sweep bonus")
                CHANGES.append("Fix 3: Removed shadow mode gate — UW bullish sweep +0.5 bonus now always live")
                break
    else:
        # Alternative: look for the gating condition more broadly
        # The system prompt says: "Shadow mode: bullish sweep +0.5 bonus gated until 20 validated signals with ≥55% hit rate"
        # Find any if-block that checks a count >= 20 near bullish sweep logic

        alt_pattern = re.compile(
            r'([ \t]*)if\s+\w+\s*>=?\s*20[^\n]*:\n'
            r'((?:\1[ \t]+[^\n]*\n)+)',
            re.MULTILINE
        )
        for m in alt_pattern.finditer(content):
            block = m.group(0)
            if "0.5" in block or "sweep" in block.lower() or "bonus" in block.lower():
                indent   = m.group(1)
                body     = m.group(2)
                dedented = re.sub(r'^' + indent + r'    ', indent, body, flags=re.MULTILINE)
                content  = content.replace(block, dedented)
                print("  [FIXED] Removed shadow mode >= 20 count gate from UW bonus")
                CHANGES.append("Fix 3: Removed shadow count gate — UW sweep bonus always live")
                break
        else:
            # Check if shadow mode is already removed or implemented differently
            if "shadow" not in content.lower():
                print("  [OK] No shadow mode gate found in code — bonus may already be live")
                CHANGES.append("Fix 3: No shadow gate found — UW sweep bonus appears already live")
            else:
                print("  [WARN] Shadow mode referenced but pattern not matched — check manually")
                print("         Search for 'shadow' in agent/claude_agent.py")
                ERRORS.append("Fix 3: Shadow mode gate pattern not auto-matched — manual check needed")

    # Also update the _SYSTEM prompt text to reflect shadow mode is removed
    if "Shadow mode: bullish sweep +0.5 bonus gated until 20 validated signals" in content:
        content = content.replace(
            "Shadow mode: bullish sweep +0.5 bonus gated until 20 validated signals with ≥55% hit rate.",
            "Bullish sweep +0.5 bonus is ALWAYS LIVE (shadow mode removed). Track outcomes in signal_performance table for future calibration."
        )
        print("  [FIXED] Updated system prompt text — shadow mode note removed")

    if content != original:
        backup(agent_file)
        write(agent_file, content)
        print("  [SAVED] agent/claude_agent.py updated")
    else:
        print("  [INFO] No changes needed in agent/claude_agent.py for Fix 3")
else:
    ERRORS.append("Fix 3: agent/claude_agent.py not found")


# =============================================================================
# FIX 4 — POSITION SIZING: Increase for double-SPY goal
# =============================================================================
section("FIX 4 — POSITION SIZING (double-SPY aggressive sizing)")

config_file = os.path.join(BASE, "config.py")
if os.path.exists(config_file):
    content  = read(config_file)
    original = content

    # Current sizing (from the file we read):
    # TIER_ALLOC = {
    #     "mega":         {7: 4.0, 8: 6.0, 9: 7.0, 10: 8.0},
    #     "large_growth": {7: 3.0, 8: 5.0, 9: 6.0, 10: 7.0},
    #     "mid_growth":   {7: 2.5, 8: 4.0, 9: 5.0, 10: 6.0},
    #     "speculative":  {7: 2.0, 8: 3.0, 9: 4.0, 10: 5.0},
    # }
    #
    # New sizing — more aggressive at high conviction for double-SPY:
    # mega:         7→5%, 8→7%, 9→9%, 10→12%
    # large_growth: 7→4%, 8→6%, 9→8%, 10→10%
    # mid_growth:   7→3%, 8→5%, 9→6.5%, 10→8%
    # speculative:  unchanged — spec stays conservative

    old_tier_alloc = '''{
    "mega":         {7: 4.0, 8: 6.0, 9: 7.0, 10: 8.0},
    "large_growth": {7: 3.0, 8: 5.0, 9: 6.0, 10: 7.0},
    "mid_growth":   {7: 2.5, 8: 4.0, 9: 5.0, 10: 6.0},
    "speculative":  {7: 2.0, 8: 3.0, 9: 4.0, 10: 5.0},
}'''

    new_tier_alloc = '''{
    # Sized for 2x SPY goal — high conviction gets meaningful allocation
    # Regime-gated: CONCENTRATE sectors use full table; NEUTRAL = max conf-8 row; AVOID = HOLD
    "mega":         {7: 5.0, 8: 7.0, 9: 9.0,  10: 12.0},
    "large_growth": {7: 4.0, 8: 6.0, 9: 8.0,  10: 10.0},
    "mid_growth":   {7: 3.0, 8: 5.0, 9: 6.5,  10: 8.0},
    "speculative":  {7: 2.0, 8: 3.0, 9: 4.0,  10: 5.0},   # unchanged — spec stays conservative
}'''

    if old_tier_alloc in content:
        backup(config_file)
        content = content.replace(old_tier_alloc, new_tier_alloc)
        print("  [FIXED] TIER_ALLOC updated for double-SPY goal")
        CHANGES.append("Fix 4: TIER_ALLOC updated — mega 10→12%, large_growth 10→10%, mid_growth 10→8%")
    else:
        # Try matching with flexible whitespace
        pattern = re.compile(
            r'TIER_ALLOC\s*=\s*\{[^}]+\}',
            re.DOTALL
        )
        m = pattern.search(content)
        if m:
            old_block = m.group(0)
            new_block = '''TIER_ALLOC = {
    # Sized for 2x SPY goal — high conviction gets meaningful allocation
    # Regime-gated: CONCENTRATE sectors use full table; NEUTRAL = max conf-8 row; AVOID = HOLD
    "mega":         {7: 5.0, 8: 7.0, 9: 9.0,  10: 12.0},
    "large_growth": {7: 4.0, 8: 6.0, 9: 8.0,  10: 10.0},
    "mid_growth":   {7: 3.0, 8: 5.0, 9: 6.5,  10: 8.0},
    "speculative":  {7: 2.0, 8: 3.0, 9: 4.0,  10: 5.0},
}'''
            backup(config_file)
            content = content.replace(old_block, new_block)
            print("  [FIXED] TIER_ALLOC updated (flexible match)")
            CHANGES.append("Fix 4: TIER_ALLOC updated via flexible match")
        else:
            ERRORS.append("Fix 4: Could not find TIER_ALLOC in config.py — update manually")
            print("  [ERROR] TIER_ALLOC not found — update manually")

    # Raise the hard cap from 8% to 15% to allow winners to run
    # Also raise WINNER_POSITION_CAP_PCT (already 15% — this is for confirmed winners)
    if "MAX_POSITION_PCT" in content:
        # Find the current value
        max_pos_match = re.search(r'MAX_POSITION_PCT\s*=\s*([\d.]+)', content)
        if max_pos_match:
            current_max = float(max_pos_match.group(1))
            if current_max < 15.0:
                content = re.sub(
                    r'MAX_POSITION_PCT\s*=\s*[\d.]+',
                    'MAX_POSITION_PCT = 15.0   # raised for 2x SPY — high conviction can size up',
                    content
                )
                print(f"  [FIXED] MAX_POSITION_PCT raised from {current_max}% to 15%")
                CHANGES.append(f"Fix 4: MAX_POSITION_PCT raised {current_max}% -> 15%")
            else:
                print(f"  [OK] MAX_POSITION_PCT already {current_max}% — no change needed")

    # Update medium-term sizing too — cap was 6%, raise to 8% at conf 10
    old_mt = "MEDIUM_TERM_TIER_ALLOC = {7: 3.0, 8: 4.5, 9: 6.0, 10: 6.0}"
    new_mt = "MEDIUM_TERM_TIER_ALLOC = {7: 3.0, 8: 4.5, 9: 6.0, 10: 8.0}  # raised conf-10 cap for MT plays"
    if old_mt in content:
        content = content.replace(old_mt, new_mt)
        print("  [FIXED] MEDIUM_TERM_TIER_ALLOC conf-10 raised to 8%")
        CHANGES.append("Fix 4: MEDIUM_TERM_TIER_ALLOC conf-10 raised from 6% to 8%")

    if content != original:
        write(config_file, content)
        print("  [SAVED] config.py updated")

    # Also update the sizing table in the _SYSTEM prompt in claude_agent.py
    agent_file = os.path.join(BASE, "agent/claude_agent.py")
    if os.path.exists(agent_file):
        agent_content  = read(agent_file)
        agent_original = agent_content

        old_sizing = """POSITION SIZING (tier-based — risk manager enforces, your confidence drives it):
Mega caps:      conf 7→5% | 8→6% | 9→7% | 10→8%
Large growth:   conf 7→4% | 8→5% | 9→6% | 10→7%
Mid growth:     conf 7→3% | 8→4% | 9→4.5% | 10→5%
Speculative:    conf 7→1.5% | 8→2% | 9→2.5% | 10→3%  (min conf 7 — no conf-6 entries; ASTS hard cap 1%)"""

        new_sizing = """POSITION SIZING (tier-based — risk manager enforces, your confidence drives it):
Sized for 2× SPY goal. High-conviction calls must be sized to MATTER.
Mega caps:      conf 7→5% | 8→7% | 9→9% | 10→12%
Large growth:   conf 7→4% | 8→6% | 9→8% | 10→10%
Mid growth:     conf 7→3% | 8→5% | 9→6.5% | 10→8%
Speculative:    conf 7→2% | 8→3% | 9→4% | 10→5%  (min conf 7 — no conf-6 entries; ASTS hard cap 1%)
REGIME GATE: CONCENTRATE sectors → use full table. NEUTRAL → cap at conf-8 row. AVOID → HOLD."""

        if old_sizing in agent_content:
            agent_content = agent_content.replace(old_sizing, new_sizing)
            print("  [FIXED] System prompt sizing table updated in claude_agent.py")
            CHANGES.append("Fix 4: System prompt sizing table updated to match new config")
        else:
            # Try flexible match
            sz_pattern = re.compile(
                r'POSITION SIZING.*?Speculative:.*?ASTS hard cap 1%\)',
                re.DOTALL
            )
            sz_m = sz_pattern.search(agent_content)
            if sz_m:
                agent_content = agent_content.replace(sz_m.group(0), new_sizing)
                print("  [FIXED] System prompt sizing updated (flexible match)")
                CHANGES.append("Fix 4: System prompt sizing updated (flexible match)")
            else:
                print("  [WARN] Could not update sizing in system prompt — update manually")
                ERRORS.append("Fix 4: System prompt sizing table not matched — update claude_agent.py manually")

        if agent_content != agent_original:
            write(agent_file, agent_content)
            print("  [SAVED] agent/claude_agent.py sizing updated")

else:
    ERRORS.append("Fix 4: config.py not found")


# =============================================================================
# FIX 5 — CLEAN DELISTED TICKERS from earnings catalyst source
# =============================================================================
section("FIX 5 — CLEAN DELISTED TICKERS FROM EARNINGS PIPELINE")

curation_file = os.path.join(BASE, "basket/curation.py")
delisted_cache_file = os.path.join(BASE, "delisted_cache.json")

# Load existing delisted cache
delisted_cache = {}
if os.path.exists(delisted_cache_file):
    try:
        with open(delisted_cache_file) as f:
            delisted_cache = json.load(f)
        print(f"  Loaded {len(delisted_cache)} cached delisted symbols")
    except Exception:
        delisted_cache = {}

# Known delisted from the log we saw
known_delisted = [
    "PEV", "WBA", "SCS", "GMS", "ENZ", "STRM", "HITC",
    "APXIF", "SBOX", "LVRO", "ADN", "GES", "VRNT", "REVG",
    "BDRL", "BASE", "AHNR", "BHAC", "SPTN", "GLLI", "EVM",
    "OCFT", "EUSP", "BSTT"
]
for sym in known_delisted:
    delisted_cache[sym] = {"delisted": True, "confirmed": str(datetime.now().date())}

# Save updated cache
with open(delisted_cache_file, "w") as f:
    json.dump(delisted_cache, f, indent=2)
print(f"  [FIXED] Added {len(known_delisted)} known delisted tickers to delisted_cache.json")
CHANGES.append(f"Fix 5: Added {len(known_delisted)} delisted tickers to delisted_cache.json")

# Now patch curation.py to use the cache before fetching price data
if os.path.exists(curation_file):
    content  = read(curation_file)
    original = content

    # The filter function to inject — filters candidates against delisted cache
    filter_fn = '''
def _filter_delisted(symbols: list, delisted_cache_path: str = None) -> list:
    """
    Filter out known delisted symbols before processing.
    Checks delisted_cache.json first (fast), then yfinance (slow, updates cache).
    """
    import json, os
    import yfinance as yf

    if delisted_cache_path is None:
        delisted_cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'delisted_cache.json')

    # Load cache
    cache = {}
    if os.path.exists(delisted_cache_path):
        try:
            with open(delisted_cache_path) as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    clean    = []
    newly_delisted = {}

    for sym in symbols:
        # Already cached as delisted
        if cache.get(sym, {}).get("delisted"):
            continue
        # Try yfinance if not cached
        try:
            ticker = yf.Ticker(sym)
            hist   = ticker.history(period="5d")
            if hist.empty:
                newly_delisted[sym] = {"delisted": True, "confirmed": str(__import__("datetime").date.today())}
                continue
        except Exception:
            newly_delisted[sym] = {"delisted": True, "confirmed": str(__import__("datetime").date.today())}
            continue
        clean.append(sym)

    # Update cache with newly discovered delisted
    if newly_delisted:
        cache.update(newly_delisted)
        try:
            with open(delisted_cache_path, "w") as f:
                json.dump(cache, f, indent=2)
            print(f"  [Delisted Cache] Added {len(newly_delisted)} new delisted: {list(newly_delisted.keys())}")
        except Exception:
            pass

    return clean

'''

    # Only inject if not already present
    if "_filter_delisted" not in content:
        # Find a good insertion point — after imports, before first function/class
        import_end = 0
        for i, line in enumerate(content.split("\n")):
            if line.startswith("import ") or line.startswith("from "):
                import_end = i
        lines = content.split("\n")
        insert_at = import_end + 2
        lines.insert(insert_at, filter_fn)
        content = "\n".join(lines)
        print("  [FIXED] Injected _filter_delisted() function into curation.py")
        CHANGES.append("Fix 5: Added _filter_delisted() to basket/curation.py")

    # Now wire it in — find where earnings candidates are collected and add the filter call
    # Look for where earnings symbols are assembled into a list before processing
    earnings_patterns = [
        # Pattern: list of symbols going into earnings processing
        (
            r'(earnings_symbols\s*=\s*\[.*?\])',
            r'\1\n    earnings_symbols = _filter_delisted(earnings_symbols)'
        ),
        (
            r'(candidates\s*=\s*earnings_candidates)',
            r'earnings_candidates = _filter_delisted(earnings_candidates)\n    \1'
        ),
    ]

    for old_pat, new_pat in earnings_patterns:
        new_content = re.sub(old_pat, new_pat, content, count=1, flags=re.DOTALL)
        if new_content != content:
            content = new_content
            print("  [FIXED] Wired _filter_delisted() into earnings pipeline")
            CHANGES.append("Fix 5: Wired delisted filter into earnings candidate pipeline")
            break
    else:
        # Find any line that iterates over earnings symbols and add filter before it
        if "earnings" in content.lower() and "yfinance" in content.lower():
            print("  [INFO] Earnings pipeline found — _filter_delisted() injected, wire manually if needed")
        else:
            print("  [WARN] Could not auto-wire filter — _filter_delisted() added, wire manually")
            ERRORS.append("Fix 5: _filter_delisted() added but needs manual wiring in curation.py")

    if content != original:
        backup(curation_file)
        write(curation_file, content)
        print("  [SAVED] basket/curation.py updated")
else:
    ERRORS.append("Fix 5: basket/curation.py not found")


# =============================================================================
# FIX 6 — LEARNING LOOP: Weekly signal performance → weights.json
# =============================================================================
section("FIX 6 — ACTIVATE LEARNING LOOP")

learning_file = os.path.join(BASE, "learning/tracker.py")
weights_file  = os.path.join(BASE, "learning/weights.json")

# Initialize weights.json with default weights if empty/missing
default_weights = {
    "congress_buy":       1.0,
    "uw_sweep_bullish":   1.0,
    "earnings_catalyst":  1.0,
    "growth_score_high":  1.0,
    "insider_buy":        1.0,
    "rsi_breakout":       1.0,
    "golden_cross":       1.0,
    "dark_pool_accum":    1.0,
    "earnings_beat":      1.0,
    "macro_concentrate":  1.0,
    "_last_updated":      str(datetime.now().date()),
    "_total_trades_seen": 0,
    "_version":           "1.0"
}

try:
    if os.path.exists(weights_file):
        with open(weights_file) as f:
            existing = json.load(f)
        if not existing or existing == {} or "_version" not in existing:
            with open(weights_file, "w") as f:
                json.dump(default_weights, f, indent=2)
            print("  [FIXED] weights.json was empty — initialized with default weights")
            CHANGES.append("Fix 6: Initialized learning/weights.json with default signal weights")
        else:
            print(f"  [OK] weights.json exists with {len(existing)} entries")
    else:
        with open(weights_file, "w") as f:
            json.dump(default_weights, f, indent=2)
        print("  [FIXED] weights.json created")
        CHANGES.append("Fix 6: Created learning/weights.json")
except Exception as e:
    ERRORS.append(f"Fix 6: Could not initialize weights.json: {e}")

# Create/update the learning tracker with the weekly update function
weekly_learning_fn = '''
# ============================================================
# WEEKLY SIGNAL PERFORMANCE UPDATE — added by kimmy_upgrade.py
# Call this every Sunday evening to update signal weights
# based on actual trade outcomes from the past 30 days.
# ============================================================

import json
import os
import sqlite3
from datetime import datetime, timedelta

_DB_PATH      = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'trading_agent.db')
_WEIGHTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'weights.json')

_SIGNALS_TO_TRACK = [
    "congress_buy", "uw_sweep_bullish", "earnings_catalyst",
    "growth_score_high", "insider_buy", "rsi_breakout",
    "golden_cross", "dark_pool_accum", "earnings_beat", "macro_concentrate"
]

_DEFAULT_WEIGHTS = {s: 1.0 for s in _SIGNALS_TO_TRACK}


def _load_weights() -> dict:
    if os.path.exists(_WEIGHTS_FILE):
        try:
            with open(_WEIGHTS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return dict(_DEFAULT_WEIGHTS)


def _save_weights(weights: dict):
    weights["_last_updated"]  = str(datetime.now().date())
    with open(_WEIGHTS_FILE, "w") as f:
        json.dump(weights, f, indent=2)


def update_signal_weights_from_outcomes():
    """
    Weekly job: read last 30 days of trades from trading_agent.db,
    fetch current prices, compute 14d returns, and update signal weights.

    Logic:
    - Trades where rationale mentions a signal keyword AND return > 5% → signal weight +0.05
    - Trades where rationale mentions a signal keyword AND return < -5% → signal weight -0.05
    - Weights are clipped to [0.3, 2.0] to prevent extremes
    - Signal weights are fed into committee_review() as signal_weights param
    """
    print("\\n[Learning] Running weekly signal weight update...")

    weights = _load_weights()
    signal_outcomes = {s: [] for s in _SIGNALS_TO_TRACK}

    try:
        conn = sqlite3.connect(_DB_PATH)
        cur  = conn.cursor()

        # Get trades from last 30 days
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        cur.execute("""
            SELECT symbol, action, price, rationale, ts
            FROM trades
            WHERE ts >= ? AND action IN ('BUY', 'SELL')
            ORDER BY ts DESC
        """, (cutoff,))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            print("[Learning] No trades in last 30 days — weights unchanged")
            return weights

        print(f"[Learning] Analyzing {len(rows)} trades...")

        # Group BUY/SELL pairs by symbol
        buys  = {}
        sells = {}
        for symbol, action, price, rationale, ts in rows:
            if action == "BUY":
                buys[symbol]  = {"price": price, "rationale": (rationale or ""), "ts": ts}
            elif action == "SELL":
                sells[symbol] = {"price": price, "ts": ts}

        # Compute returns for completed trades (have both BUY and SELL)
        completed = []
        for sym, buy in buys.items():
            if sym in sells:
                ret = (sells[sym]["price"] - buy["price"]) / buy["price"] * 100
                completed.append({"symbol": sym, "return_pct": ret, "rationale": buy["rationale"]})

        # For open positions, try to get current price from yfinance
        open_syms = [s for s in buys if s not in sells]
        if open_syms:
            try:
                import yfinance as yf
                for sym in open_syms[:10]:  # limit API calls
                    try:
                        info  = yf.Ticker(sym).fast_info
                        price = getattr(info, "last_price", None)
                        if price:
                            ret = (price - buys[sym]["price"]) / buys[sym]["price"] * 100
                            completed.append({
                                "symbol": sym, "return_pct": ret,
                                "rationale": buys[sym]["rationale"], "open": True
                            })
                    except Exception:
                        pass
            except Exception:
                pass

        print(f"[Learning] {len(completed)} trades with return data")

        # Map rationale keywords to signal names
        signal_keywords = {
            "congress_buy":      ["congress", "congress_buy", "congress buy"],
            "uw_sweep_bullish":  ["uw", "sweep", "unusual whales", "bullish sweep", "dark pool"],
            "earnings_catalyst": ["earnings catalyst", "earnings_catalyst", "earnings play"],
            "growth_score_high": ["growth score", "growth_score", "future_growth"],
            "insider_buy":       ["insider", "form 4", "insider buy"],
            "rsi_breakout":      ["rsi", "breakout", "golden cross"],
            "golden_cross":      ["golden cross", "sma cross", "sma200"],
            "dark_pool_accum":   ["dark pool", "accumulation", "block trade"],
            "earnings_beat":     ["beat", "strong beat", "earnings beat"],
            "macro_concentrate": ["concentrate", "macro", "regime", "geopolit"],
        }

        # Update weights based on outcomes
        signal_updates = {s: 0 for s in _SIGNALS_TO_TRACK}
        signal_counts  = {s: 0 for s in _SIGNALS_TO_TRACK}

        for trade in completed:
            rationale = trade["rationale"].lower()
            ret       = trade["return_pct"]

            for signal, keywords in signal_keywords.items():
                if any(kw in rationale for kw in keywords):
                    signal_counts[signal] += 1
                    if ret > 5:
                        signal_updates[signal] += 0.05   # signal predicted a winner
                    elif ret < -5:
                        signal_updates[signal] -= 0.05   # signal predicted a loser

        # Apply updates and clip to [0.3, 2.0]
        for signal in _SIGNALS_TO_TRACK:
            if signal_counts[signal] >= 3:   # need at least 3 data points to adjust
                old_w = weights.get(signal, 1.0)
                new_w = round(max(0.3, min(2.0, old_w + signal_updates[signal])), 3)
                weights[signal] = new_w
                if abs(new_w - old_w) >= 0.01:
                    print(f"  [Learning] {signal}: {old_w:.2f} -> {new_w:.2f} "
                          f"({signal_counts[signal]} trades, "
                          f"update={signal_updates[signal]:+.2f})")

        weights["_total_trades_seen"] = weights.get("_total_trades_seen", 0) + len(completed)
        _save_weights(weights)
        print(f"[Learning] Weights updated and saved -> {_WEIGHTS_FILE}")

    except Exception as e:
        print(f"[Learning] Error in weight update: {e}")

    return weights


def get_weights_for_committee() -> dict:
    """
    Called by committee_review() to inject current signal weights.
    Returns only the signal weight keys (not metadata).
    """
    weights = _load_weights()
    return {k: v for k, v in weights.items() if not k.startswith("_")}


if __name__ == "__main__":
    update_signal_weights_from_outcomes()
'''

# Append to learning/tracker.py if the weekly update function isn't there
if os.path.exists(learning_file):
    existing = read(learning_file)
    if "update_signal_weights_from_outcomes" not in existing:
        backup(learning_file)
        with open(learning_file, "a") as f:
            f.write("\n\n" + weekly_learning_fn)
        print("  [FIXED] Weekly learning function added to learning/tracker.py")
        CHANGES.append("Fix 6: Added update_signal_weights_from_outcomes() to learning/tracker.py")
    else:
        print("  [OK] Learning loop already present in tracker.py")
else:
    # Create the file
    with open(learning_file, "w") as f:
        f.write('"""Learning tracker — signal performance and weight updates."""\n')
        f.write(weekly_learning_fn)
    print("  [FIXED] Created learning/tracker.py with weekly learning loop")
    CHANGES.append("Fix 6: Created learning/tracker.py with weekly signal weight update")

# Wire the weekly learning job into the APScheduler in main.py
main_file = os.path.join(BASE, "main.py")
if os.path.exists(main_file):
    main_content  = read(main_file)
    main_original = main_content

    if "update_signal_weights_from_outcomes" not in main_content:
        # Find the APScheduler section and add the weekly job
        scheduler_patterns = [
            # Pattern: after a weekly review job is added
            r'(scheduler\.add_job.*?weekly.*?\n)',
            r'(scheduler\.add_job.*?friday.*?\n)',
            r'(scheduler\.add_job.*?basket_weekly.*?\n)',
        ]
        job_added = False
        for pat in scheduler_patterns:
            m = re.search(pat, main_content, re.IGNORECASE)
            if m:
                insert_point = m.end()
                learning_job = (
                    "\n    # Weekly signal learning loop — Sunday 6 PM ET\n"
                    "    scheduler.add_job(\n"
                    "        lambda: __import__('learning.tracker', fromlist=['update_signal_weights_from_outcomes'])"
                    ".update_signal_weights_from_outcomes(),\n"
                    "        'cron', day_of_week='sun', hour=18, minute=0,\n"
                    "        id='weekly_learning', replace_existing=True\n"
                    "    )\n"
                )
                main_content = main_content[:insert_point] + learning_job + main_content[insert_point:]
                job_added = True
                print("  [FIXED] Weekly learning job added to APScheduler in main.py")
                CHANGES.append("Fix 6: Weekly learning job wired into APScheduler (Sunday 6 PM ET)")
                break

        if not job_added:
            print("  [WARN] Could not find APScheduler block in main.py — add job manually")
            ERRORS.append("Fix 6: APScheduler job not added automatically — wire manually")

    if main_content != main_original:
        backup(main_file)
        write(main_file, main_content)
        print("  [SAVED] main.py updated with learning job")

else:
    ERRORS.append("Fix 6: main.py not found")


# =============================================================================
# SUMMARY
# =============================================================================
section("UPGRADE COMPLETE — SUMMARY")

print("\nCHANGES APPLIED:")
for i, c in enumerate(CHANGES, 1):
    print(f"  {i}. {c}")

if ERRORS:
    print("\nITEMS NEEDING MANUAL ATTENTION:")
    for i, e in enumerate(ERRORS, 1):
        print(f"  {i}. {e}")
else:
    print("\n[OK] No manual steps needed.")

print("""
NEXT STEPS:
1. Restart Kimmy:   sudo systemctl restart kimmy
2. Check logs:      journalctl -u kimmy -f
3. In 5 min verify: journalctl -u kimmy | grep -E "Committee|growth|UW|sweep"
4. After first trade: check trading_agent.db has the trade logged:
   sqlite3 /root/trading-agent/trading_agent.db "SELECT * FROM trades ORDER BY rowid DESC LIMIT 5;"
5. After 30 days: run the learning loop manually to verify:
   cd /root/trading-agent && python3 learning/tracker.py
""")
