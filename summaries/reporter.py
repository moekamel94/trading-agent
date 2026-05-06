import json,os,sqlite3
from datetime import datetime,timezone,timedelta
import database.db as db
from broker import alpaca
from notifications import discord_bot as discord

def _pl_emoji(pct):
    if pct>=5: return chr(55356)+chr(57122)
    if pct>=0: return chr(55356)+chr(57121)
    if pct>=-5: return chr(55356)+chr(57120)
    return chr(55356)+chr(57119)

def _parse_price_stop(criteria):
    if not criteria: return None
    for part in criteria.split("|"):
        part=part.strip()
        if part.lower().startswith("price_stop"):
            raw=part.split(":",1)[-1].strip().replace("$","").replace(",","")
            try: return float(raw)
            except: return None
    return None

def _load_macro_regime():
    path=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..",".macro_regime.json")
    try: return json.load(open(path))
    except: return {}

def _spy_returns():
    try:
        import yfinance as yf
        hist=yf.Ticker("SPY").history(period="35d")
        if len(hist)<2: return {}
        latest=hist["Close"].iloc[-1]
        r7d=(latest/hist["Close"].iloc[max(0,len(hist)-6)]-1)*100
        r30d=(latest/hist["Close"].iloc[max(0,len(hist)-22)]-1)*100
        return {"spy_7d":round(r7d,1),"spy_30d":round(r30d,1),"spy_price":round(latest,2)}
    except: return {}

def _portfolio_returns(equity):
    try:
        db_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..","trading_agent.db")
        conn=sqlite3.connect(db_path)
        c=conn.cursor()
        now=datetime.now(timezone.utc)
        def _eq(days):
            cutoff=(now-timedelta(days=days)).isoformat()
            row=c.execute("SELECT equity FROM snapshots WHERE ts<=? ORDER BY ts DESC LIMIT 1",(cutoff,)).fetchone()
            return float(row[0]) if row else None
        eq7=_eq(7); eq30=_eq(30)
        conn.close()
        r={}
        if eq7: r["port_7d"]=round((equity/eq7-1)*100,1)
        if eq30: r["port_30d"]=round((equity/eq30-1)*100,1)
        return r
    except: return {}

def _entry_date_map():
    try:
        db_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..","trading_agent.db")
        conn=sqlite3.connect(db_path)
        c=conn.cursor()
        c.execute("SELECT symbol,MAX(ts) FROM trades WHERE action='BUY' GROUP BY symbol")
        result={}
        for sym,ts in c.fetchall():
            try: result[sym]=datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
            except: pass
        conn.close()
        return result
    except: return {}

def _get_last_decisions():
    try:
        db_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..","trading_agent.db")
        conn=sqlite3.connect(db_path)
        c=conn.cursor()
        c.execute("""SELECT symbol,action,confidence,rationale,crs_growth_catalyst,thesis_break_criteria,price_target,ts
                     FROM committee_decisions
                     WHERE (symbol,ts) IN (SELECT symbol,MAX(ts) FROM committee_decisions GROUP BY symbol)""")
        result={}
        for row in c.fetchall():
            result[row[0]]={"action":row[1],"confidence":row[2],"rationale":row[3],"catalyst":row[4],"stop":row[5],"target":row[6],"ts":row[7]}
        conn.close()
        return result
    except: return {}

def _get_commodity_moves():
    try:
        import yfinance as yf
        tickers={"Oil":"CL=F","Brent":"BZ=F","Gold":"GC=F","NatGas":"NG=F","Dollar":"DX-Y.NYB","10Y":"^TNX"}
        moves={}
        for name,sym in tickers.items():
            try:
                hist=yf.Ticker(sym).history(period="2d")
                if len(hist)>=2:
                    today=float(hist["Close"].iloc[-1])
                    prev=float(hist["Close"].iloc[-2])
                    moves[name]={"price":round(today,2),"pct":round((today-prev)/prev*100,2)}
            except: pass
        return moves
    except: return {}

def _thesis_status(sym,upl_pct,macro,cache_data,last_dec):
    ENERGY=["COP","OXY","PBR","FANG","GLNG","CVX","XOM","ET","DVN"]
    NUCLEAR=["VST","CEG","TLN","CCJ","OKLO","SMR","BWXT"]
    DEFENSE=["GD","LMT","RTX","NOC","BWXT","KTOS"]
    sw=macro.get("sector_weights",{})
    status="intact"
    reasons=[]
    action="HOLD"
    dec=last_dec.get(sym,{})
    if sym in ENERGY:
        oil_w=sw.get("energy_oil",0.5)
        if oil_w<0.35:
            reasons.append("Energy sector routing AVOID ("+str(round(oil_w,2))+") -- macro headwind")
            status="weakening"; action="TRIM or EXIT"
        if upl_pct<-5:
            reasons.append("Down "+str(round(upl_pct,1))+"% -- war premium unwinding as ceasefire probability rises")
            status="weakening"
            if action=="HOLD": action="REVIEW EXIT"
    elif sym in NUCLEAR:
        nuke_w=sw.get("nuclear_power",0.5)
        if nuke_w>=0.7: reasons.append("Nuclear sector CONCENTRATE -- AI power demand thesis intact")
        else: reasons.append("Nuclear weight "+str(nuke_w)+" -- holding but watch AI capex data")
    elif sym in DEFENSE:
        def_w=sw.get("defense",0.5)
        if def_w>=0.7: reasons.append("Defense sector CONCENTRATE -- NATO spending cycle intact")
    if not reasons:
        if upl_pct>5: reasons.append("Outperforming -- thesis intact")
        elif upl_pct>-3: reasons.append("Within normal range -- monitor")
        else: reasons.append("Underperforming "+str(round(upl_pct,1))+"% -- review thesis"); status="weakening"
    return {"status":status,"reasons":reasons,"action":action}

def _macro_insights(cm,macro):
    alerts=[]; insights=[]
    oil=cm.get("Oil",{}).get("pct",0)
    gold=cm.get("Gold",{}).get("pct",0)
    dxy=cm.get("Dollar",{}).get("pct",0)
    y10=cm.get("10Y",{}).get("pct",0)
    if oil<=-3:
        alerts.append("OIL SHOCK: -"+str(abs(round(oil,1)))+"% -- ceasefire/peace premium unwinding")
        insights.append("Energy thesis WEAKENING: COP/OXY/FANG/PBR/GLNG at risk. War premium reversing.")
        insights.append("Rotation: Airlines, travel, growth tech benefit from lower fuel costs.")
        insights.append("Watch: Hormuz reopening confirmation = another -8-12% for energy names.")
    elif oil>=3:
        alerts.append("OIL SURGE: +"+str(round(oil,1))+"% -- supply disruption or escalation")
        insights.append("Energy holdings benefit short-term but inflation risk rises for growth.")
    if gold>=2: insights.append("Gold +"+str(round(gold,1))+"% -- flight to safety, risk-off signal")
    if gold<=-2: insights.append("Gold "+str(round(gold,1))+"% -- risk appetite improving, growth stocks benefit")
    if dxy>=0.5: insights.append("Dollar +"+str(round(dxy,1))+"% -- headwind for TSM/PBR/GLNG/international")
    if dxy<=-0.5: insights.append("Dollar "+str(round(dxy,1))+"% -- tailwind for international and commodities")
    if y10>=4: insights.append("10Y yield rising -- pressure on high-multiple growth (NVDA/CRDO/IONQ)")
    if y10<=-4: insights.append("10Y yield falling -- tailwind for growth and tech multiples")
    return alerts,insights

def _tomorrow_setup(cm,macro,positions):
    lines=["TOMORROW SETUP:"]
    oil=cm.get("Oil",{}).get("pct",0)
    sw=macro.get("sector_weights",{})
    regime=macro.get("regime_label","")
    if oil<=-5:
        lines.append("  Oil selloff may continue: watch crude support at 5. Break = more energy downside.")
        lines.append("  Rotation signal: Airlines/travel/consumer benefit from lower fuel inflation.")
        lines.append("  If Hormuz officially reopens: energy -8-12% more. Position to reduce energy exposure.")
        lines.append("  Lower oil = lower CPI = Fed more likely to cut = growth multiple expansion opportunity.")
    ai=sw.get("ai_software",0)+sw.get("semis",0)
    if ai>1.5:
        lines.append("  AI/semis regime strong ("+str(round(ai,2))+") -- earnings season = key test for momentum.")
        lines.append("  Watch: hyperscaler capex commentary for AI infrastructure confirmation.")
    if regime=="growth_driven":
        lines.append("  Growth regime intact -- favor quality compounders over commodity plays.")
    events=macro.get("upcoming_events",[])[:2]
    for ev in events:
        lines.append("  Key event: "+ev.get("event","")+" -- "+ev.get("impact","watch carefully")[:60])
    return lines

def run_premarket(dry_run=False):
    now=datetime.now(timezone.utc)
    label=now.strftime("%a %d %b %Y")
    try:
        portfolio=alpaca.get_portfolio()
        positions=alpaca.get_positions()
    except Exception as e:
        discord.send("Pre-market: portfolio fetch failed -- "+str(e))
        return
    equity=portfolio.get("equity",0)
    cash=portfolio.get("cash",0)
    cash_pct=cash/equity*100 if equity else 0
    spy=_spy_returns(); port_ret=_portfolio_returns(equity)
    entry_dt=_entry_date_map(); macro=_load_macro_regime()
    last_dec=_get_last_decisions(); cm=_get_commodity_moves()
    p7=port_ret.get("port_7d"); s7=spy.get("spy_7d")
    p30=port_ret.get("port_30d"); s30=spy.get("spy_30d")
    gap=round(p30-(s30*2),1) if p30 and s30 else None
    out=["PRE-MARKET -- "+label,"NAV $"+str(f"{equity:,.0f}")+"  Cash "+str(round(cash_pct,0))+"% ($"+str(f"{cash:,.0f}")+")  "+str(len(positions))+" positions"]
    if p7 and s7: out.append("7d: Kimmy "+str(p7)+"% vs SPY "+str(s7)+"%")
    if gap is not None: out.append("Mandate: "+("+" if gap>=0 else "")+str(gap)+"pp vs 2xSPY")
    alerts,insights=_macro_insights(cm,macro)
    if alerts:
        out.append("")
        out.append("MACRO SHOCK ALERTS:")
        for a_ in alerts: out.append("  ALERT: "+a_)
    out.append("")
    out.append("COMMODITIES:")
    for name,data in cm.items():
        pct=data.get("pct",0); px=data.get("price",0)
        flag=" <-- SHOCK" if abs(pct)>=3 else ""
        out.append("  "+name+": $"+str(px)+" ("+("+" if pct>=0 else "")+str(pct)+"%)"+flag)
    sw=macro.get("sector_weights",{})
    regime=macro.get("regime_label","?")
    top=sorted(sw.items(),key=lambda x:-x[1])[:3]
    out.append("")
    out.append("MACRO REGIME: "+regime.upper().replace("_"," "))
    out.append("Top sectors: "+" | ".join(s+" "+str(round(w,2)) for s,w in top))
    fc=macro.get("regime_forecast",{})
    if fc.get("likely_next_regime") and fc.get("transition_probability",0)>=25:
        out.append("REGIME SHIFT: "+str(fc["transition_probability"])+"% -> "+fc["likely_next_regime"].upper()+" in "+str(fc.get("horizon_weeks","?"))+"w")
    if insights:
        out.append("")
        out.append("MARKET INTELLIGENCE:")
        for i in insights: out.append("  "+i)
    cache_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..","research_cache.json")
    cache_data={}
    try: cache_data=json.load(open(cache_path))
    except: pass
    out.append("")
    out.append("POSITIONS + THESIS (sorted by P&L):")
    sorted_pos=sorted(positions,key=lambda x:float(x.get("unrealized_plpc",0) or 0),reverse=True)
    for p in sorted_pos:
        sym=p["symbol"]
        cur=float(p.get("current_price",0) or 0)
        upl_pct=float(p.get("unrealized_plpc",0) or 0)
        upl_usd=float(p.get("unrealized_pl",0) or 0)
        edt=entry_dt.get(sym)
        dh=" ["+str((now-edt).days)+"d]" if edt else ""
        icon=("UP" if upl_pct>=5 else "OK" if upl_pct>=0 else "WATCH" if upl_pct>=-5 else "ALERT")
        out.append(icon+" "+sym+" $"+str(round(cur,2))+" "+("+" if upl_pct>=0 else "")+str(round(upl_pct,1))+"% ($"+str(round(upl_usd,0))+")"+dh)
        th=_thesis_status(sym,upl_pct,macro,cache_data,last_dec)
        out.append("  Thesis: "+th["status"].upper()+" | Action: "+th["action"]+" | "+th["reasons"][0])
        dec=last_dec.get(sym,{})
        if dec.get("target"): out.append("  Target: $"+str(dec["target"])+" | Catalyst: "+(dec.get("catalyst","") or "")[:70])
        cd=cache_data.get(sym,{})
        ed=(cd.get("earnings_data") or {})
        earn_date=ed.get("earnings_date","")
        if earn_date:
            try:
                _ed=datetime.strptime(earn_date,"%Y-%m-%d").replace(tzinfo=timezone.utc)
                dte=(_ed.date()-now.date()).days
                if 0<=dte<=14: out.append("  EARNINGS IN "+str(dte)+" DAYS -- binary event risk")
            except: pass
    out.append("")
    out+=_tomorrow_setup(cm,macro,positions)
    msg=chr(10).join(out)
    print(chr(10)+msg)
    db.log_summary("premarket",msg)
    if not dry_run: discord.send(msg)

def run_close():
    ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    now=datetime.now(timezone.utc)
    out=["CLOSE MEMO -- "+ts]
    try:
        portfolio=alpaca.get_portfolio()
        positions=alpaca.get_positions()
        equity=portfolio["equity"]; cash=portfolio["cash"]; n_pos=len(positions)
        out.append("NAV: $"+str(f"{equity:,.0f}")+"  Cash: $"+str(f"{cash:,.0f}")+" ("+str(round(cash/equity*100,0))+"%)  "+str(n_pos)+" positions")
    except Exception as e:
        positions=[]; equity=0
        out.append("[Portfolio error: "+str(e)+"]")
    spy=_spy_returns(); port_ret=_portfolio_returns(equity)
    cm=_get_commodity_moves(); macro=_load_macro_regime()
    last_dec=_get_last_decisions()
    p7=port_ret.get("port_7d"); s7=spy.get("spy_7d")
    if p7 and s7:
        alpha=round(p7-s7,1)
        out.append("7d: Kimmy "+str(p7)+"% vs SPY "+str(s7)+"% (alpha: "+("+" if alpha>=0 else "")+str(alpha)+"pp)")
    out.append("")
    out.append("WHAT HAPPENED TODAY:")
    alerts,insights=_macro_insights(cm,macro)
    for a_ in alerts: out.append("  "+a_)
    oil=cm.get("Oil",{}).get("pct",0)
    if abs(oil)>=1: out.append("  Oil WTI: "+("+" if oil>=0 else "")+str(round(oil,1))+"% today")
    for i in insights: out.append("  "+i)
    if insights or alerts:
        out.append("")
        out.append("WHY IT MATTERS FOR YOUR PORTFOLIO:")
        cache_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..","research_cache.json")
        cache_data={}
        try: cache_data=json.load(open(cache_path))
        except: pass
        energy_pos=[p["symbol"] for p in positions if p["symbol"] in ["COP","OXY","PBR","FANG","GLNG","ET","DVN"]]
        if energy_pos and oil<=-3:
            out.append("  Energy holdings: "+", ".join(energy_pos))
            out.append("  Oil -"+str(abs(round(oil,1)))+"% = war premium unwinding = thesis weakening")
            out.append("  Action: Committee evaluates trim/exit next cycle. Do NOT average down.")
    trades=db.get_today_trades()
    out.append("")
    if not trades:
        out.append("TRADES: None today -- conditions did not meet conviction threshold")
    else:
        buys=[t for t in trades if t["action"]=="BUY"]
        sells=[t for t in trades if t["action"]=="SELL"]
        if buys:
            out.append("BOUGHT ("+str(len(buys))+"):")
            for t in buys:
                out.append("  "+t["symbol"]+" "+str(t.get("allocation",0))+"% @$"+str(t["price"])+" conf="+str(t.get("confidence","?"))+"/10")
                if t.get("rationale"): out.append("  Thesis: "+t["rationale"][:150])
        if sells:
            out.append("SOLD ("+str(len(sells))+"):")
            for t in sells:
                out.append("  "+t["symbol"]+" @$"+str(t["price"]))
                if t.get("rationale"): out.append("  Reason: "+t["rationale"][:150])
    cache_path2=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..","research_cache.json")
    cache_data2={}
    try: cache_data2=json.load(open(cache_path2))
    except: pass
    attention=[]
    for p in positions:
        sym=p["symbol"]; upl_pct=float(p.get("unrealized_plpc",0) or 0)
        th=_thesis_status(sym,upl_pct,macro,cache_data2,last_dec)
        if th["status"] in ["weakening","broken"]: attention.append((sym,upl_pct,th))
    if attention:
        out.append("")
        out.append("POSITIONS NEEDING ATTENTION:")
        for sym,upl_pct,th in attention:
            out.append("  "+sym+" "+("+" if upl_pct>=0 else "")+str(round(upl_pct,1))+"% | "+th["status"].upper()+" | "+th["action"])
            for r in th["reasons"]: out.append("  -- "+r)
    out.append("")
    out+=_tomorrow_setup(cm,macro,positions)
    body=chr(10).join(out)
    print(chr(10)+body)
    db.log_summary("close",body)
    discord.send(body)
    try:
        from monitoring import health
        health.send_eod_digest()
    except Exception as e:
        print("  [Health] EOD digest failed: "+str(e))

def check_macro_shocks():
    try:
        cm=_get_commodity_moves()
        macro=_load_macro_regime()
        thresholds={"Oil":3.0,"Brent":3.0,"Gold":2.0,"NatGas":5.0,"Dollar":0.8,"10Y":4.0}
        fired=[]
        for name,thresh in thresholds.items():
            pct=cm.get(name,{}).get("pct",0)
            if abs(pct)>=thresh:
                px=cm.get(name,{}).get("price",0)
                fired.append(name+" "+("+" if pct>=0 else "")+str(round(pct,1))+"% ($"+str(px)+")")
        if not fired: return
        alerts,insights=_macro_insights(cm,macro)
        out=["MACRO SHOCK ALERT"]+["  "+f for f in fired]
        if insights:
            out.append("")
            out.append("WHAT IT MEANS:")
            for i in insights: out.append("  "+i)
        try:
            positions=alpaca.get_positions()
            cache_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..","research_cache.json")
            cd={}
            try: cd=json.load(open(cache_path))
            except: pass
            ld=_get_last_decisions()
            affected=[]
            for p in positions:
                sym=p["symbol"]; upl=float(p.get("unrealized_plpc",0) or 0)
                th=_thesis_status(sym,upl,macro,cd,ld)
                if th["status"] in ["weakening","broken"]: affected.append(sym+" ("+th["action"]+")")
            if affected:
                out.append("")
                out.append("HOLDINGS AT RISK: "+", ".join(affected))
                out.append("Committee evaluates at next cycle. Do NOT average down into weakness.")
        except: pass
        discord.send(chr(10).join(out))
    except Exception as e:
        print("  [MacroShock] error: "+str(e))