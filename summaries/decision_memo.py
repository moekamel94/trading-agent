"""
summaries/decision_memo.py
Generates CIO-grade decision memos after every trading cycle.
Answers: what happened, why it matters, which holdings affected,
did thesis improve or weaken, what action is recommended.
This replaces the activity log with a structured decision document.
"""
import os,json,sqlite3
from datetime import datetime,timezone,timedelta
import database.db as db
from broker import alpaca
from notifications import discord_bot as discord

def _load_macro():
    p=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..",".macro_regime.json")
    try: return json.load(open(p))
    except: return {}

def _get_decisions_today():
    try:
        db_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..","trading_agent.db")
        conn=sqlite3.connect(db_path)
        c=conn.cursor()
        today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
        c.execute("""SELECT symbol,action,confidence,allocation_pct,rationale,
                     crs_market_outlook,crs_growth_catalyst,thesis_break_criteria,
                     price_target,da_severity,da_bear_case,ts
                     FROM committee_decisions WHERE ts>=? ORDER BY ts DESC""",(today+"%",))
        rows=c.fetchall()
        conn.close()
        return rows
    except: return []

def _get_bucket_decisions_today():
    try:
        rows=_get_decisions_today()
        return [r for r in rows if r[1] in ["BUCKET","HOLD"]]
    except: return []

def generate_cycle_memo(cycle_label, decisions, positions, portfolio, macro, dry_run=False):
    """
    Generate and send a decision memo after each trading cycle.
    decisions: list of committee decision dicts from the cycle
    """
    equity = portfolio.get("equity",0)
    cash   = portfolio.get("cash",0)
    now    = datetime.now(timezone.utc)
    sw     = macro.get("sector_weights",{})
    regime = macro.get("regime_label","?").upper().replace("_"," ")

    out = ["CYCLE MEMO -- "+cycle_label]
    out.append("NAV $"+str(f"{equity:,.0f}")+"  Cash "+str(round(cash/equity*100,1) if equity else 0)+"%  Regime: "+regime)

    # Section 1: What happened this cycle
    buys  = [d for d in decisions if d.get("action")=="BUY"]
    sells = [d for d in decisions if d.get("action")=="SELL"]
    holds = [d for d in decisions if d.get("action")=="HOLD"]
    buckets=[d for d in decisions if d.get("action")=="BUCKET"]
    trims = [d for d in decisions if d.get("action")=="TRIM"]

    out.append("")
    out.append("WHAT HAPPENED THIS CYCLE:")
    if buys:
        out.append("  BOUGHT ("+str(len(buys))+"):")
        for d in buys:
            out.append("  "+d["symbol"]+" at "+str(d.get("allocation_pct",0))+"% conf="+str(d.get("confidence","?"))+"/10")
            if d.get("rationale"): out.append("    Thesis: "+str(d["rationale"])[:120])
    if sells:
        out.append("  SOLD ("+str(len(sells))+"):")
        for d in sells:
            out.append("  "+d["symbol"]+" | "+str(d.get("rationale",""))[:100])
    if trims:
        out.append("  TRIMMED ("+str(len(trims))+"):")
        for d in trims:
            out.append("  "+d["symbol"]+" | "+str(d.get("rationale",""))[:100])
    if not buys and not sells and not trims:
        out.append("  No trades -- market conditions did not meet conviction threshold")
        out.append("  (Conviction gate requires >=7/10. Best candidate: "+
                   (buckets[0]["symbol"] if buckets else "none")+
                   (" conf="+str(buckets[0].get("confidence","?"))+"/10" if buckets else "")+")")

    # Section 2: Why it matters
    out.append("")
    out.append("WHY IT MATTERS:")
    # Geopolitical context for energy
    try:
        from signals import geopolitical as geo_mod
        geo = geo_mod.compute_geo_risk()
        peace = geo["peace_probability"]
        out.append("  Geopolitical: "+geo["thesis_note"])
        # Energy positions
        energy_held = [p["symbol"] for p in positions if p["symbol"] in ["COP","OXY","PBR","FANG","GLNG","ET","DVN"]]
        if energy_held:
            decisions_e,_ = geo_mod.get_oil_position_decisions(positions, equity)
            for d in decisions_e:
                out.append("  "+d["symbol"]+" "+str(d["upl_pct"])+"% | Action: "+d["action"])
                out.append("    "+d["reason"])
    except Exception as e: out.append("  [Geo] error: "+str(e)[:60])

    # Proactive signals
    try:
        from signals import proactive_regime as pr
        from basket import manager as bm
        basket = bm.load_combined()
        proactive = pr.compute_proactive_signals(basket, positions)
        if proactive["pre_position"]:
            out.append("")
            out.append("PRE-POSITIONING ALERTS (act before price moves):")
            for alert in proactive["pre_position"][:5]:
                out.append("  "+alert)
        if proactive["rotation_alerts"]:
            out.append("")
            out.append("SECTOR ROTATION SIGNALS:")
            for alert in proactive["rotation_alerts"][:5]:
                out.append("  "+alert)
    except Exception as e: out.append("  [Proactive] error: "+str(e)[:60])

    # Section 3: Holdings thesis update
    out.append("")
    out.append("HOLDINGS THESIS UPDATE:")
    pos_map = {p["symbol"]:p for p in positions}
    # Show positions where thesis changed this cycle
    for d in decisions[:8]:
        sym = d.get("symbol","")
        if not sym: continue
        p = pos_map.get(sym,{})
        upl = round(float(p.get("unrealized_plpc",0) or 0),1) if p else 0
        action = d.get("action","")
        conf   = d.get("confidence",0)
        rationale = str(d.get("rationale",""))[:120]
        catalyst  = str(d.get("crs_growth_catalyst") or "")[:80]
        target    = d.get("price_target",0)
        stop      = d.get("thesis_break_criteria","")
        icon = ("BUY" if action=="BUY" else "SELL" if action=="SELL"
                else "WATCH" if action=="BUCKET" else "HOLD")
        line = "  "+icon+" "+sym+" ("+str(upl)+"%) conf="+str(conf)+"/10"
        if target: line += " target=$"+str(target)
        out.append(line)
        if rationale: out.append("    "+rationale)
        if catalyst: out.append("    Catalyst: "+catalyst)
        if stop and action=="BUY": out.append("    Exit if: "+str(stop)[:80])

    # Section 4: What to watch tomorrow
    out.append("")
    out.append("WATCH TOMORROW:")
    # Scale-in opportunities
    scale_ins = [d for d in decisions if d.get("action")=="HOLD" and d.get("confidence",0)>=7
                 and d.get("symbol") in pos_map]
    if scale_ins:
        out.append("  Scale-in candidates (tranche 2/3):")
        for d in scale_ins[:5]:
            sym = d["symbol"]
            out.append("  "+sym+" conf="+str(d.get("confidence",0))+"/10 | "+str(d.get("crs_growth_catalyst") or "")[:70])
    # Bucket watch
    if buckets:
        out.append("  Bucket -- needs: "+", ".join([b["symbol"] for b in buckets[:5]]))
    # Regime regime triggers
    fc = macro.get("regime_forecast",{})
    if fc.get("transition_probability",0)>=25:
        nxt = fc.get("likely_next_regime","").upper().replace("_"," ")
        out.append("  Regime shift: "+str(fc["transition_probability"])+"% -> "+nxt)
        pre = fc.get("pre_position_sectors",[])
        if pre: out.append("  Pre-position sectors: "+", ".join(pre[:4]))

    memo = chr(10).join(out)
    print(chr(10)+memo)
    db.log_summary("cycle_memo",memo)
    if not dry_run: discord.send(memo)
    return memo