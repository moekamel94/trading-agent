import os, json, requests, math
from datetime import datetime, timezone
import config

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", ".research_engine_cache")
_TIMEOUT = 12

UNIVERSE = {
    "mega_quality":  ["NVDA","MSFT","AAPL","GOOGL","META","AMZN","V","MA","UNH","LLY"],
    "large_growth":  ["AVGO","ORCL","CRM","NOW","ADBE","CDNS","SNPS","KLAC","ANET","MRVL",
                      "TSM","TDG","CSGP","MSCI","SPGI","MCO","ICE","CME","TT","ISRG"],
    "mid_growth":    ["CRDO","ALAB","CAVA","CELH","AXON","DUOL","TTD","FTNT","PANW","ZS",
                      "DDOG","MDB","NET","BILL","GLBE","TMDX","BROS","ONON","GDDY","PAYC"],
    "ai_infra":      ["SMCI","VRT","ETN","DELL","CIEN","LITE","COHR","FN","ONTO","WOLF","ACLS"],
    "power_energy":  ["VST","CEG","NRG","TLN","OKLO","PWR","PRIM","GEV","FSLR","BE","NEE"],
    "defense":       ["GD","LMT","RTX","NOC","BWXT","CACI","SAIC","LDOS","PSN","KTOS","RCAT","HII"],
    "space":         ["RKLB","ASTS","LUNR","VSAT","SPIR"],
    "biotech":       ["DXCM","PODD","INSP","TMDX","RXRX","CRSP","ILMN","VEEV","DOCS","GMED"],
    "robotics":      ["TER","BRKS","CEVA","SERV","ACHR","JOBY","KEYS"],
    "fintech":       ["FIS","FI","GPN","NU","SOFI","AFRM","BILL","FLYW","SSNC"],
    "overlooked":    ["CLS","FIX","PRIM","UFPI","RBA","CSWI","AAON","MGNI","DCBO","ITRI","PLXS"],
}

SURFACE_THRESHOLD = 55
QUALITY_GATE      = 20
FULL_ALLOC_MIN    = 70
HALF_ALLOC_MIN    = 55
BUY_BLOCK_MAX     = 40

TIER_TAM = {
    "mega_quality":9,"large_growth":8,"mid_growth":8,"ai_infra":10,
    "power_energy":9,"defense":8,"space":9,"biotech":9,
    "robotics":9,"fintech":7,"overlooked":7,
}

ALLOC_GUIDE = {
    "mega_quality": {"vh":12.0,"h":6.0,"m":3.0},
    "large_growth": {"vh": 8.0,"h":5.0,"m":2.0},
    "mid_growth":   {"vh": 6.0,"h":4.0,"m":2.0},
    "ai_infra":     {"vh": 6.0,"h":4.0,"m":2.0},
    "power_energy": {"vh": 6.0,"h":4.0,"m":2.0},
    "defense":      {"vh": 6.0,"h":4.0,"m":2.0},
    "space":        {"vh": 4.0,"h":2.0,"m":1.0},
    "biotech":      {"vh": 5.0,"h":3.0,"m":1.5},
    "robotics":     {"vh": 4.0,"h":2.0,"m":1.0},
    "fintech":      {"vh": 5.0,"h":3.0,"m":1.5},
    "overlooked":   {"vh": 5.0,"h":3.0,"m":1.5},
}

def _cache_path(ticker):
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, ticker + "_re.json")

def _load_cache(ticker, max_days=7):
    p = _cache_path(ticker)
    if not os.path.exists(p): return None
    try:
        d = json.load(open(p))
        ts = datetime.fromisoformat(d.get("_ts","2000-01-01")).replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc)-ts).days <= max_days: return d
    except Exception: pass
    return None

def _save_cache(ticker, data):
    data["_ts"] = datetime.now(timezone.utc).isoformat()
    try: json.dump(data, open(_cache_path(ticker),"w"), indent=2)
    except Exception: pass

def _fmp(ep, params=None):
    if not config.FMP_API_KEY: return None
    try:
        p = {"apikey": config.FMP_API_KEY}
        if params: p.update(params)
        r = requests.get("https://financialmodelingprep.com/api/v3/"+ep, params=p, timeout=_TIMEOUT)
        if r.status_code == 200: return r.json()
    except Exception: pass
    return None

def _finnhub(ep, params=None):
    if not config.FINNHUB_API_KEY: return None
    try:
        p = {"token": config.FINNHUB_API_KEY}
        if params: p.update(params)
        r = requests.get("https://finnhub.io/api/v1/"+ep, params=p, timeout=_TIMEOUT)
        if r.status_code == 200: return r.json()
    except Exception: pass
    return None

def _get_financials(ticker):
    f = {}
    inc = _fmp("income-statement/"+ticker, {"limit":8,"period":"quarter"})
    if inc and len(inc) >= 5:
        rn=inc[0].get("revenue",0); ra=inc[4].get("revenue",0)
        if ra and ra>0: f["revenue_growth_yoy"] = round((rn-ra)/ra*100,1)
        f["gross_margin"] = round((inc[0].get("grossProfitRatio",0) or 0)*100,1)
        f["net_margin"]   = round((inc[0].get("netIncomeRatio",0) or 0)*100,1)
        if len(inc)>=6:
            rn2=inc[1].get("revenue",0); ra2=inc[5].get("revenue",0)
            if ra2 and ra2>0:
                g2=(rn2-ra2)/ra2*100
                f["revenue_growth_prev"]=round(g2,1)
                f["revenue_accelerating"]=(f.get("revenue_growth_yoy",0)>g2>0)
        if len(inc)>=7:
            rn3=inc[2].get("revenue",0); ra3=inc[6].get("revenue",0)
            if ra3 and ra3>0:
                g3=(rn3-ra3)/ra3*100
                g1=f.get("revenue_growth_yoy",0); g2x=f.get("revenue_growth_prev",0)
                f["revenue_3q_accel"]=(g1>g2x>g3>0)
        neg=sum(1 for i in range(min(4,len(inc)-4))
                if inc[i+4].get("revenue",0)>0 and inc[i].get("revenue",0)<inc[i+4].get("revenue",0))
        f["rev_decline_qtrs"]=neg
    inc_a=_fmp("income-statement/"+ticker,{"limit":6,"period":"annual"})
    if inc_a and len(inc_a)>=5:
        r5n=inc_a[0].get("revenue",0); r5a=inc_a[4].get("revenue",0)
        if r5a and r5a>0 and r5n>0:
            f["revenue_cagr_5y"]=round((math.pow(r5n/r5a,0.2)-1)*100,1)
    m=_fmp("key-metrics-ttm/"+ticker)
    if m and isinstance(m,list) and m:
        mx=m[0]
        f["pe"]=mx.get("peRatioTTM"); f["peg"]=mx.get("pegRatioTTM")
        f["fcf_yield"]=round((mx.get("freeCashFlowYieldTTM") or 0)*100,2)
        f["roic"]=round((mx.get("roicTTM") or 0)*100,1)
        f["debt_eq"]=mx.get("debtToEquityTTM")
    dcf=_fmp("discounted-cash-flow/"+ticker)
    if dcf and isinstance(dcf,list) and dcf: f["dcf_value"]=dcf[0].get("dcf")
    elif dcf and isinstance(dcf,dict): f["dcf_value"]=dcf.get("dcf")
    recs=_finnhub("stock/recommendation",{"symbol":ticker})
    if recs and isinstance(recs,list) and recs:
        rx=recs[0]
        total=sum([rx.get("strongBuy",0),rx.get("buy",0),rx.get("hold",0),rx.get("sell",0),rx.get("strongSell",0)])
        f["analyst_n"]=total
        f["analyst_buy_pct"]=round((rx.get("strongBuy",0)+rx.get("buy",0))/total*100,1) if total>0 else 0
    pt=_finnhub("stock/price-target",{"symbol":ticker})
    if pt: f["pt_mean"]=pt.get("targetMean"); f["pt_high"]=pt.get("targetHigh")
    return f

def _score(ticker, tier, f):
    flags=[]
    if f.get("rev_decline_qtrs",0)>=3: flags.append("Revenue declining 3+ consecutive quarters")
    if f.get("gross_margin",100)<0: flags.append("Negative gross margin")
    deq=f.get("debt_eq",0) or 0; fcf=f.get("fcf_yield",0) or 0
    if deq>5 and fcf<=0: flags.append("Excessive debt with no FCF")
    if flags: return {"composite":0,"rejected":True,"flags":flags,"sa":0,"sb":0,"sc":0,"scores":{},"conviction":"low"}
    sc={}
    rev=f.get("revenue_growth_yoy",0) or 0
    gm=f.get("gross_margin",0) or 0
    nm=f.get("net_margin",0) or 0
    roic=f.get("roic",0) or 0
    peg=f.get("peg",99) or 99
    an=f.get("analyst_n",10) or 10
    buy_pct=f.get("analyst_buy_pct",50) or 50
    accel=f.get("revenue_accelerating",False)
    ac3=f.get("revenue_3q_accel",False)
    cagr5=f.get("revenue_cagr_5y",0) or 0
    a1=(10 if rev>=50 else 9 if rev>=30 else 7 if rev>=20 else 5 if rev>=10 else 3 if rev>=5 else 1 if rev>=0 else 0)
    if cagr5>=20: a1=min(10,a1+1)
    sc["a1_revenue"]=a1
    a2=(4 if gm>=70 else 3 if gm>=50 else 2 if gm>=30 else 1 if gm>0 else 0)
    a2+=(3 if nm>=25 else 2 if nm>=12 else 1 if nm>=0 else 0)
    a2+=(3 if roic>=25 else 2 if roic>=12 else 1 if roic>0 else 0)
    sc["a2_profitability"]=min(10,a2)
    a3=(4 if gm>=70 else 3 if gm>=50 else 2 if gm>=30 else 1 if gm>0 else 0)
    a3+=(4 if buy_pct>=75 else 3 if buy_pct>=60 else 2 if buy_pct>=45 else 1 if buy_pct>=30 else 0)
    a3+=(2 if an<=5 else 1 if an<=12 else 0)
    sc["a3_moat"]=min(10,a3)
    a4=(5 if fcf>=7 else 4 if fcf>=4 else 2 if fcf>=1 else 0)
    a4+=(5 if deq<=0.3 else 4 if deq<=0.8 else 2 if deq<=2.0 else 0)
    sc["a4_balance_sheet"]=min(10,a4)
    sa=sc["a1_revenue"]+sc["a2_profitability"]+sc["a3_moat"]+sc["a4_balance_sheet"]
    if sa<QUALITY_GATE:
        return {"composite":sa,"rejected":True,"flags":["Quality gate: "+str(sa)+"/40"],"sa":sa,"sb":0,"sc":0,"scores":sc,"conviction":"low"}
    sc["b1_peg"]=(15 if peg<=0 else 15 if peg<=0.5 else 13 if peg<=1.0 else 10 if peg<=1.5 else 7 if peg<=2.0 else 4 if peg<=2.5 else 2 if peg<=3.5 else 0)
    sc["b2_fcf"]=(10 if fcf>=8 else 8 if fcf>=5 else 6 if fcf>=3 else 4 if fcf>=1.5 else 2 if fcf>=0 else 0)
    b3=(3 if buy_pct>=75 else 2 if buy_pct>=55 else 1)
    if f.get("dcf_value",0): b3=min(5,b3+2)
    sc["b3_conviction"]=b3
    sb=sc["b1_peg"]+sc["b2_fcf"]+sc["b3_conviction"]
    sc["c1_tam"]=TIER_TAM.get(tier,7)
    c2=TIER_TAM.get(tier,7)
    if rev>=30: c2=min(10,c2+2)
    elif rev>=15: c2=min(10,c2+1)
    sc["c2_tailwind"]=c2
    c3=(10 if ac3 else 8 if accel else 7 if rev>=40 else 5 if rev>=25 else 3 if rev>=12 else 1 if rev>=0 else 0)
    sc["c3_acceleration"]=c3
    scc=sc["c1_tam"]+sc["c2_tailwind"]+sc["c3_acceleration"]
    comp=sa+sb+scc
    if ac3 and comp>=70: comp=min(100,comp+3)
    conv=("very_high" if comp>=85 else "high" if comp>=70 else "medium" if comp>=55 else "low")
    return {"composite":comp,"rejected":False,"flags":[],"sa":sa,"sb":sb,"sc":scc,"scores":sc,"conviction":conv}

def get_tier(ticker):
    for tier,tickers in UNIVERSE.items():
        if ticker in tickers: return tier
    return "mid_growth"

def analyze_ticker(ticker, tier=None):
    if tier is None: tier=get_tier(ticker)
    cached=_load_cache(ticker)
    if cached: return cached
    f=_get_financials(ticker); s=_score(ticker,tier,f)
    rev=f.get("revenue_growth_yoy","na")
    cagr5=f.get("revenue_cagr_5y","na")
    gm=f.get("gross_margin","na")
    roic=f.get("roic","na")
    peg=f.get("peg","na")
    fcf=f.get("fcf_yield","na")
    an=f.get("analyst_n","na")
    ac3=f.get("revenue_3q_accel",False)
    accel=f.get("revenue_accelerating",False)
    parts=[]
    if str(rev) not in ["None","na"]:
        tag=" 3Q-ACCEL" if ac3 else (" ACCEL" if accel else "")
        parts.append("Rev+"+str(rev)+"%"+tag)
    if str(cagr5) not in ["None","na"]:
        try:
            if float(str(cagr5))>0: parts.append("5yr+"+str(cagr5)+"%")
        except: pass
    if str(gm) not in ["None","na"]: parts.append("GM="+str(gm)+"%")
    if str(roic) not in ["None","na"]: parts.append("ROIC="+str(roic)+"%")
    if str(peg) not in ["None","na"]:
        try:
            pf=float(str(peg))
            if pf<=2.0: parts.append("PEG="+str(round(pf,1)))
        except: pass
    if str(fcf) not in ["None","na"]:
        try:
            if float(str(fcf))>0: parts.append("FCF="+str(fcf)+"%")
        except: pass
    if str(an) not in ["None","na"]:
        try:
            if int(str(an))<=10: parts.append(str(an)+" analysts=UNDERFOLLOWED")
        except: pass
    thesis=" | ".join(parts) if parts else "Insufficient financial data"
    conv=s.get("conviction","low")
    ag=ALLOC_GUIDE.get(tier,{"vh":4.0,"h":2.0,"m":1.0})
    alloc=(ag["vh"] if conv=="very_high" else ag["h"] if conv=="high" else ag["m"] if conv=="medium" else 0.0)
    result={"ticker":ticker,"tier":tier,"composite":s.get("composite",0),"conviction":conv,
            "surfaced":s.get("composite",0)>=SURFACE_THRESHOLD and not s.get("rejected"),
            "rejected":s.get("rejected",False),"flags":s.get("flags",[]),
            "sa":s.get("sa",0),"sb":s.get("sb",0),"sc":s.get("sc",0),
            "scores":s.get("scores",{}),"financials":f,"thesis":thesis,"max_alloc":alloc}
    _save_cache(ticker,result)
    return result

def get_synthesis_for_committee(ticker):
    tier=get_tier(ticker); r=analyze_ticker(ticker,tier)
    if r["rejected"]:
        return "RESEARCH ENGINE FLAG: "+ticker+" -- "+"; ".join(r["flags"])
    if not r["surfaced"]:
        return "RESEARCH ENGINE: "+ticker+" scores "+str(r["composite"])+"/100 (below threshold)"
    f=r["financials"]
    ac3=f.get("revenue_3q_accel",False); accel=f.get("revenue_accelerating",False)
    an=f.get("analyst_n",0) or 0; dcf_v=f.get("dcf_value",0) or 0
    accel_note=("3-QUARTER REVENUE ACCELERATION CONFIRMED." if ac3 else "Revenue accelerating QoQ." if accel else "")
    early_note=(" Only "+str(an)+" analysts -- Wall Street has not found this yet." if 0<an<=8 else "")
    dcf_note=(" DCF fair value $"+str(round(dcf_v,2))+"." if dcf_v>0 else "")
    return (
        "RESEARCH ENGINE: "+ticker+" scores "+str(r["composite"])+"/100 ("
        +r["conviction"].replace("_"," ").upper()+"). "
        "Quality="+str(r["sa"])+"/40 | Valuation="+str(r["sb"])+"/30 | Growth="+str(r["sc"])+"/30. "
        +accel_note+early_note+dcf_note
        +" Thesis: "+r["thesis"]
        +". Max alloc: "+str(r["max_alloc"])+"%. "
        "Weight this score alongside all other signals when sizing.")

def check_buy_quality_gate(ticker, proposed_alloc_pct):
    try:
        r=analyze_ticker(ticker,get_tier(ticker)); score=r.get("composite",55)
        if r.get("rejected"):       return False,0,"BLOCKED: "+"; ".join(r["flags"])
        if score<BUY_BLOCK_MAX:     return False,0,"BLOCKED: score "+str(score)+"/100"
        if score<HALF_ALLOC_MIN:    return True,round(proposed_alloc_pct*0.5,1),"HALF ALLOC: score "+str(score)
        if score<FULL_ALLOC_MIN:    return True,round(proposed_alloc_pct*0.75,1),"75pct ALLOC: score "+str(score)
        return True,proposed_alloc_pct,"FULL ALLOC: score "+str(score)+"/100"
    except Exception as e:
        return True,proposed_alloc_pct,"GATE SKIPPED: "+str(e)

def run_full_scan(notify=True):
    print("[ResearchEngine] Full universe scan...")
    surfaced=[]; rejected=[]; all_r=[]
    for tier,tickers in UNIVERSE.items():
        print("  Tier: "+tier)
        for ticker in tickers:
            try:
                r=analyze_ticker(ticker,tier); all_r.append(r)
                if r["surfaced"]:
                    surfaced.append(r)
                    ac=" 3Q-ACCEL" if r["financials"].get("revenue_3q_accel") else ""
                    print("    SURFACE "+ticker+": "+str(r["composite"])+"/100 ("+r["conviction"]+")"+ac)
                elif r["rejected"]: rejected.append(r)
            except Exception as e: print("    ERROR "+ticker+": "+str(e))
    surfaced.sort(key=lambda x:-x["composite"])
    if notify and surfaced: _send_scan_report(surfaced,rejected,len(all_r))
    return {"surfaced":surfaced,"rejected":rejected,"total_scanned":len(all_r),"total_surfaced":len(surfaced)}

def _send_scan_report(surfaced,rejected,total_scanned):
    try:
        from notifications import discord_bot as discord
        t1=[r for r in surfaced if r["composite"]>=85]
        t2=[r for r in surfaced if 70<=r["composite"]<85]
        t3=[r for r in surfaced if 55<=r["composite"]<70]
        lines=["KIMMY RESEARCH ENGINE -- MONTHLY SCAN",
               "Scored "+str(total_scanned)+" | Surfaced: "+str(len(surfaced))+" | Rejected: "+str(len(rejected)),""]
        lines.append("VERY HIGH CONVICTION (85-100):")
        for r in t1[:5]:
            ac="3Q-ACCEL " if r["financials"].get("revenue_3q_accel") else ""
            lines.append("  "+r["ticker"]+": "+str(r["composite"])+"/100 "+ac+"| "+r["thesis"][:70])
        lines.append("HIGH CONVICTION (70-84):")
        for r in t2[:5]:
            lines.append("  "+r["ticker"]+": "+str(r["composite"])+"/100 | "+r["thesis"][:70])
        lines.append("MONITOR (55-69):")
        for r in t3[:5]:
            lines.append("  "+r["ticker"]+": "+str(r["composite"])+"/100 | "+r["thesis"][:60])
        discord.send("
".join(lines))
    except Exception as e: print("[ResearchEngine] Report error: "+str(e))

def _discover_fmp_screener():
    found=[]
    try:
        known=set(t for tl in UNIVERSE.values() for t in tl)
        r=requests.get("https://financialmodelingprep.com/api/v3/stock-screener",
            params={"apikey":config.FMP_API_KEY,"marketCapMoreThan":800000000,
                    "revenueMoreThan":80000000,"country":"US","exchange":"NASDAQ,NYSE",
                    "isEtf":False,"limit":150},timeout=_TIMEOUT)
        if r.status_code==200:
            for s in r.json():
                t=s.get("symbol","")
                if t and t not in known and len(t)<=5:
                    found.append({"ticker":t,"name":s.get("companyName",""),
                                  "sector":s.get("sector",""),"source":"fmp_screener"})
    except Exception as e: print("  [Discovery] FMP screener: "+str(e))
    print("  [Discovery] FMP screener: "+str(len(found))+" new candidates")
    return found

def _discover_congress_buys():
    found=[]
    try:
        known=set(t for tl in UNIVERSE.values() for t in tl)
        if config.UNUSUAL_WHALES_API_KEY:
            r=requests.get("https://api.unusualwhales.com/api/congress/recent-trades",
                headers={"Authorization":"Bearer "+config.UNUSUAL_WHALES_API_KEY},timeout=_TIMEOUT)
            if r.status_code==200:
                for t in r.json().get("data",[]):
                    ticker=t.get("ticker",""); txtype=t.get("transaction_type","")
                    if not ticker or ticker in known or len(ticker)>5: continue
                    if "buy" not in txtype.lower() and "purchase" not in txtype.lower(): continue
                    found.append({"ticker":ticker,"sector":"","source":"congress_buy",
                                  "detail":t.get("representative","")+" bought "+t.get("amount","")})
    except Exception as e: print("  [Discovery] Congress: "+str(e))
    unique={f["ticker"]:f for f in found}
    print("  [Discovery] Congress buys: "+str(len(unique))+" new tickers")
    return list(unique.values())

def _discover_earnings_surprises():
    found=[]
    try:
        known=set(t for tl in UNIVERSE.values() for t in tl)
        r=requests.get("https://financialmodelingprep.com/api/v3/earnings-surprises",
            params={"apikey":config.FMP_API_KEY,"limit":300},timeout=_TIMEOUT)
        if r.status_code==200:
            for s in r.json():
                ticker=s.get("symbol",""); actual=s.get("actualEarningResult",0) or 0
                est=s.get("estimatedEarning",0) or 0
                if not ticker or ticker in known or len(ticker)>5: continue
                if est and est>0 and actual>est*1.15:
                    pct=round((actual-est)/est*100,1)
                    found.append({"ticker":ticker,"sector":"","source":"earnings_surprise",
                                  "detail":"Beat estimate by "+str(pct)+"%"})
    except Exception as e: print("  [Discovery] Earnings surprise: "+str(e))
    unique={f["ticker"]:f for f in found}
    print("  [Discovery] Earnings surprises: "+str(len(unique))+" new tickers")
    return list(unique.values())

def _discover_sector_leaders(top_sectors=None):
    found=[]
    if not top_sectors:
        top_sectors=["Technology","Industrials","Energy","Healthcare","Consumer Cyclical"]
    try:
        known=set(t for tl in UNIVERSE.values() for t in tl)
        for sector in top_sectors:
            r=requests.get("https://financialmodelingprep.com/api/v3/stock-screener",
                params={"apikey":config.FMP_API_KEY,"sector":sector,
                        "marketCapMoreThan":500000000,"country":"US",
                        "exchange":"NASDAQ,NYSE","isEtf":False,"limit":30},timeout=_TIMEOUT)
            if r.status_code==200:
                for s in r.json():
                    t=s.get("symbol","")
                    if t and t not in known and len(t)<=5:
                        found.append({"ticker":t,"name":s.get("companyName",""),
                                      "sector":sector,"source":"sector_leader:"+sector})
    except Exception as e: print("  [Discovery] Sector leaders: "+str(e))
    unique={f["ticker"]:f for f in found}
    print("  [Discovery] Sector leaders: "+str(len(unique))+" new tickers")
    return list(unique.values())

def run_discovery_scan(top_sectors=None, notify=True):
    print("[Discovery] Starting dynamic stock discovery scan...")
    all_candidates=[]
    all_candidates.extend(_discover_fmp_screener())
    all_candidates.extend(_discover_congress_buys())
    all_candidates.extend(_discover_earnings_surprises())
    all_candidates.extend(_discover_sector_leaders(top_sectors))
    seen={}
    for c in all_candidates:
        t=c["ticker"]
        if t not in seen: seen[t]=c
        else: seen[t]["source"]+=" + "+c["source"]
    candidates=list(seen.values())
    print("[Discovery] Unique new candidates: "+str(len(candidates)))
    surfaced=[]; added=[]
    for c in candidates:
        ticker=c["ticker"]
        try:
            sector=c.get("sector","").lower()
            if "tech" in sector:      tier="mid_growth"
            elif "energy" in sector:  tier="power_energy"
            elif "health" in sector:  tier="biotech"
            elif "industr" in sector: tier="overlooked"
            elif "defense" in sector: tier="defense"
            elif "financ" in sector:  tier="fintech"
            else:                     tier="mid_growth"
            r=analyze_ticker(ticker,tier)
            r["discovery_source"]=c.get("source","")
            r["discovery_detail"]=c.get("detail","")
            if r["surfaced"]: surfaced.append(r)
        except Exception as e: print("  ERROR "+ticker+": "+str(e))
    surfaced.sort(key=lambda x:-x["composite"])
    if surfaced:
        try:
            from basket import manager as bm
            existing=set(bm.load_combined())
            for r in surfaced:
                if r["composite"]>=70 and r["ticker"] not in existing:
                    bm.add_to_mt([r["ticker"]]); added.append(r["ticker"])
                    print("  [Discovery] Auto-added: "+r["ticker"]+" ("+str(r["composite"])+"/100)")
        except Exception as e: print("  [Discovery] Basket error: "+str(e))
    if notify and surfaced: _send_discovery_report(surfaced,added,len(candidates))
    return {"candidates":len(candidates),"surfaced":surfaced,"added_to_basket":added}

def _send_discovery_report(surfaced,added,total):
    try:
        from notifications import discord_bot as discord
        lines=["KIMMY DISCOVERY ENGINE",
               "Scanned "+str(total)+" candidates | "+str(len(surfaced))+" surfaced",""]
        for r in surfaced[:12]:
            src=r.get("discovery_source",""); detail=r.get("discovery_detail","")
            ac=" 3Q-ACCEL" if r["financials"].get("revenue_3q_accel") else ""
            lines.append("  "+r["ticker"]+": "+str(r["composite"])+"/100"+ac+" | "+src)
            if detail: lines.append("    "+detail)
            lines.append("    "+r["thesis"][:80])
        if added: lines.append("Auto-added to MT basket: "+", ".join(added))
        discord.send("
".join(lines))
    except Exception as e: print("[Discovery] Report error: "+str(e))

def run_basket_health_check(basket_tickers, notify=True):
    print("[ResearchEngine] Weekly basket health check...")
    scores=[]; warnings=[]
    for ticker in basket_tickers:
        try:
            r=analyze_ticker(ticker,get_tier(ticker))
            scores.append((ticker,r.get("composite",0),r.get("conviction","low")))
            if r.get("rejected"): warnings.append(ticker+" FAILS: "+"; ".join(r.get("flags",[])))
            elif r.get("composite",0)<40: warnings.append(ticker+" LOW score "+str(r.get("composite",0))+"/100")
        except Exception as e: print("  ERROR "+ticker+": "+str(e))
    scores.sort(key=lambda x:-x[1])
    if notify:
        try:
            from notifications import discord_bot as discord
            lines=["BASKET HEALTH CHECK -- WEEKLY RE-SCORE",""]
            lines.append("TOP SCORED:")
            for t,sc,cv in scores[:10]: lines.append("  "+t+": "+str(sc)+"/100 ("+cv+")")
            if warnings:
                lines.append("WARNINGS:")
                for ww in warnings: lines.append("  "+ww)
            else: lines.append("All basket tickers pass quality checks.")
            discord.send("
".join(lines))
        except Exception as e: print("[ResearchEngine] Health check error: "+str(e))
    return {"scores":scores,"warnings":warnings}
