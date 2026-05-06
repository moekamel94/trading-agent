import os, json, requests
from datetime import datetime, timezone
import config

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", ".early_disc_cache")
_TIMEOUT = 10

def _cache_path(symbol):
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, symbol + ".json")

def _load_cache(symbol):
    p = _cache_path(symbol)
    if not os.path.exists(p): return None
    try:
        d = json.load(open(p))
        ts = datetime.fromisoformat(d.get("_ts","2000-01-01")).replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc)-ts).total_seconds() < 86400: return d
    except Exception: pass
    return None

def _save_cache(symbol, data):
    data["_ts"] = datetime.now(timezone.utc).isoformat()
    try: json.dump(data, open(_cache_path(symbol),"w"))
    except Exception: pass

def _get_analyst_count(symbol):
    if not config.FINNHUB_API_KEY: return None
    try:
        r = requests.get("https://finnhub.io/api/v1/stock/recommendation",
            params={"symbol":symbol,"token":config.FINNHUB_API_KEY}, timeout=_TIMEOUT)
        if r.status_code != 200: return None
        recs = r.json()
        if not recs: return None
        l = recs[0]
        return sum([l.get("strongBuy",0),l.get("buy",0),l.get("hold",0),l.get("sell",0),l.get("strongSell",0)])
    except Exception: return None

def _get_revenue_acceleration(symbol):
    if not config.FMP_API_KEY: return {"accelerating":False,"growth_rates":[],"quarters":0}
    try:
        r = requests.get("https://financialmodelingprep.com/api/v3/income-statement/"+symbol,
            params={"apikey":config.FMP_API_KEY,"limit":6,"period":"quarter"}, timeout=_TIMEOUT)
        if r.status_code != 200: return {"accelerating":False,"growth_rates":[],"quarters":0}
        stmts = r.json()
        if len(stmts) < 4: return {"accelerating":False,"growth_rates":[],"quarters":0}
        growth_rates = []
        for i in range(min(3, len(stmts)-4)):
            rn = stmts[i].get("revenue",0); ra = stmts[i+4].get("revenue",0)
            if ra and ra > 0: growth_rates.append(round((rn-ra)/ra*100,1))
        if len(growth_rates) < 2: return {"accelerating":False,"growth_rates":growth_rates,"quarters":len(growth_rates)}
        accel = all(growth_rates[i]>growth_rates[i+1] for i in range(len(growth_rates)-1))
        pos   = all(g>0 for g in growth_rates)
        return {"accelerating":accel and pos,"growth_rates":growth_rates,"quarters":len(growth_rates),"latest_growth":growth_rates[0] if growth_rates else None}
    except Exception: return {"accelerating":False,"growth_rates":[],"quarters":0}

def compute(symbol):
    cached = _load_cache(symbol)
    if cached: return cached
    result = {"analyst_count":None,"analyst_stage":"unknown","revenue_accelerating":False,"revenue_growth_rates":[],"early_stage_score":0,"signals":[]}
    score = 0
    ac = _get_analyst_count(symbol)
    result["analyst_count"] = ac
    if ac is not None:
        if ac <= 5: result["analyst_stage"]="early"; score+=40; result["signals"].append("Only "+str(ac)+" analyst(s) — EARLY stage")
        elif ac <= 15: result["analyst_stage"]="consensus"; score+=20; result["signals"].append(str(ac)+" analysts — CONSENSUS")
        else: result["analyst_stage"]="late"; result["signals"].append(str(ac)+" analysts — LATE stage")
    rv = _get_revenue_acceleration(symbol)
    result["revenue_accelerating"] = rv.get("accelerating",False)
    result["regrowth_rates"] = rv.get("growth_rates",[])
    if rv.get("accelerating"): score+=35; result["signals"].append("Revenue ACCELERATING: "+str(rv.get("growth_rates","")))
    elif rv.get("latest_growth") and rv["latest_growth"]>20: score+=15; result["signals"].append("Revenue +"+str(rv["latest_growth"])+"%")
    if result["analyst_stage"]=="early" and result["revenue_accelerating"]: score+=25; result["signals"].append("EARLY DISCOVERY ALERT: low coverage + accelerating revenue")
    result["early_stage_score"] = min(100, score)
    _save_cache(symbol, result)
    return result

def get_alert(symbol):
    data = compute(symbol)
    if data.get("early_stage_score",0) >= 60:
        return "EARLY DISCOVERY: "+str(data.get("analyst_count","?"))+" analysts ("+data.get("analyst_stage","?")+") score="+str(data["early_stage_score"])+"/100"
    return None