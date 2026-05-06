"""
signals/geopolitical.py
Geopolitical risk tracker -- scores war escalation vs de-escalation probability.
Monitors Iran/Hormuz situation, computes oil thesis health, fires proactive alerts.
Called every 15 min during market hours via check_macro_shocks().
"""
import os,json,requests
from datetime import datetime,timezone,timedelta
import config

_CACHE = os.path.join(os.path.dirname(__file__),"..",".geo_cache.json")
_TIMEOUT = 10

# Signals that RAISE escalation probability (bad for ceasefire/peace)
ESCALATION_SIGNALS = [
    "shots fired","missiles","attack","strike","explosion","escalat","ceasefire violated",
    "war resumed","blockade tightened","hormuz closed","tanker hit","drone attack",
    "iran fired","us fired","hezbollah","new sanctions",
]

# Signals that LOWER escalation probability (good for peace/oil supply)
DEESCALATION_SIGNALS = [
    "ceasefire holds","negotiations","deal","agreement","hormuz open","ships passing",
    "iran agrees","peace talks","sanctions relief","strait reopened","diplomacy",
    "trump paused","talks progress","witkoff","pakistan mediating",
]

def score_headline(text):
    """Score a headline -1 (escalation) to +1 (de-escalation)."""
    text_low = text.lower()
    esc  = sum(1 for s in ESCALATION_SIGNALS   if s in text_low)
    desc = sum(1 for s in DEESCALATION_SIGNALS if s in text_low)
    if esc == 0 and desc == 0: return 0
    return round((desc - esc) / (desc + esc), 2)

def get_geo_news():
    """Fetch latest Iran/Hormuz headlines via Finnhub news."""
    headlines = []
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/news",
            params={"category":"general","token":config.FINNHUB_API_KEY},
            timeout=_TIMEOUT
        )
        if r.status_code == 200:
            for item in r.json()[:30]:
                h = item.get("headline","")
                if any(k in h.lower() for k in ["iran","hormuz","ceasefire","oil","opec","strait","middle east"]):
                    headlines.append({"headline":h,"ts":item.get("datetime",0)})
    except Exception: pass
    return headlines[:10]

def compute_geo_risk():
    """
    Compute current geopolitical risk score for oil thesis.
    Returns dict with:
      peace_probability: 0-100 (higher = more likely peace/Hormuz open)
      escalation_risk: 0-100 (higher = more likely war escalation)
      oil_thesis: intact / weakening / broken
      action_signal: hold / reduce / exit / accumulate
      key_headlines: list of relevant headlines
      thesis_note: one-line assessment
    """
    # Load cached result if fresh (< 15 min)
    try:
        cached = json.load(open(_CACHE))
        ts = datetime.fromisoformat(cached.get("_ts","2000-01-01")).replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc)-ts).total_seconds() < 900:
            return cached
    except Exception: pass

    headlines = get_geo_news()
    scores = [score_headline(h["headline"]) for h in headlines]
    avg_score = round(sum(scores)/len(scores),2) if scores else 0

    # Current situation as of May 2026:
    # - Ceasefire holds but fragile
    # - US/Iran trading shots in Hormuz
    # - Trump paused Project Freedom for deal
    # - 1550 ships stranded, only 2 US ships passed
    # - Full Hormuz reopening NOT imminent
    # This is the BASELINE embedded in the scoring
    BASELINE_PEACE_PROB = 35  # ceasefire fragile, Hormuz not truly open

    # Adjust from news sentiment
    news_adj = avg_score * 30  # -30 to +30 adjustment
    peace_prob = max(5, min(95, round(BASELINE_PEACE_PROB + news_adj)))
    escalation_risk = 100 - peace_prob

    # Oil thesis assessment
    if peace_prob >= 65:
        oil_thesis = "breaking"
        action = "EXIT -- peace deal would collapse oil war premium, -20-30% downside"
    elif peace_prob >= 50:
        oil_thesis = "weakening"
        action = "REDUCE -- peace probability rising, trim to half position"
    elif peace_prob >= 35:
        oil_thesis = "uncertain"
        action = "HOLD with stop -- ceasefire fragile, binary outcome either way"
    else:
        oil_thesis = "intact"
        action = "HOLD -- escalation risk supports war premium, supply disruption continues"

    # One-line thesis note
    if peace_prob >= 50:
        note = ("Hormuz reopening becoming more likely (peace_prob="+str(peace_prob)+"%). "
               +"Oil war premium at risk. Energy positions exposed.")
    elif peace_prob >= 35:
        note = ("Ceasefire fragile, outcome binary (peace_prob="+str(peace_prob)+"%). "
               +"US/Iran still trading shots in Hormuz. Supply disruption continues.")
    else:
        note = ("Escalation risk high (peace_prob="+str(peace_prob)+"%). "
               +"War premium supported. Energy thesis intact.")

    result = {
        "peace_probability": peace_prob,
        "escalation_risk": escalation_risk,
        "oil_thesis": oil_thesis,
        "action_signal": action,
        "key_headlines": [h["headline"][:100] for h in headlines[:5]],
        "thesis_note": note,
        "avg_news_score": avg_score,
        "_ts": datetime.now(timezone.utc).isoformat(),
    }
    try: json.dump(result, open(_CACHE,"w"), indent=2)
    except Exception: pass
    return result

def get_oil_position_decisions(positions, portfolio_equity):
    """
    For each oil position, generate a specific hold/trim/exit recommendation
    based on geopolitical risk score, P&L, and current thesis health.
    Returns list of decision dicts.
    """
    OIL_NAMES = {
        "COP":  "ConocoPhillips -- US shale, war premium + LNG exposure",
        "OXY":  "Occidental -- US shale, Buffett-backed, war premium play",
        "PBR":  "Petrobras -- Brazilian deepwater, EM exposure + war premium",
        "FANG": "Diamondback Energy -- Permian pure-play, US shale",
        "GLNG": "Golar LNG -- floating LNG, Hormuz disruption beneficiary",
        "ET":   "Energy Transfer -- US pipelines, less war-premium sensitive",
        "DVN":  "Devon Energy -- US shale, war premium play",
    }
    geo = compute_geo_risk()
    decisions = []
    for p in positions:
        sym = p["symbol"]
        if sym not in OIL_NAMES: continue
        upl_pct = float(p.get("unrealized_plpc",0) or 0)
        upl_usd = float(p.get("unrealized_pl",0) or 0)
        qty     = float(p.get("qty",0) or 0)
        price   = float(p.get("current_price",0) or 0)
        mkt_val = qty * price
        alloc   = mkt_val / portfolio_equity * 100 if portfolio_equity else 0
        peace   = geo["peace_probability"]
        # Determine specific action
        if peace >= 60 or upl_pct < -10:
            action = "EXIT"
            reason = ("Peace probability "+str(peace)+"% -- war premium collapsing. "
                     +"Loss "+str(round(upl_pct,1))+"% -- stop threshold breached.")
        elif peace >= 45 or upl_pct < -7:
            action = "TRIM 50%"
            reason = ("Peace probability "+str(peace)+"% rising. "
                     +"Reduce to half position, keep stop tight.")
        elif peace >= 35 and upl_pct < -5:
            action = "TRIM 25%"
            reason = ("Ceasefire fragile but position losing "+str(round(upl_pct,1))+"%. "
                     +"Reduce slightly, watch Hormuz developments.")
        else:
            action = "HOLD"
            reason = "War premium intact, escalation risk supports position."
        # Special case: GLNG and ET less war-premium sensitive
        if sym in ["ET","GLNG"] and action == "EXIT":
            action = "TRIM 50%"
            reason = sym+" less war-premium sensitive -- trim rather than full exit."
        decisions.append({
            "symbol": sym,
            "description": OIL_NAMES[sym],
            "upl_pct": round(upl_pct,1),
            "upl_usd": round(upl_usd,0),
            "alloc_pct": round(alloc,1),
            "action": action,
            "reason": reason,
            "peace_prob": peace,
        })
    return decisions, geo