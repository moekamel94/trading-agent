"""
signals/proactive_regime.py
Proactive regime logic -- anticipates market moves BEFORE they happen.
Monitors leading indicators and fires pre-emptive alerts:
  - Ceasefire probability rising -> reduce energy before oil drops
  - Sector ETF flows rotating -> position ahead of price
  - Earnings revision trends -> anticipate analyst upgrades/downgrades
  - Fed repricing signals -> position ahead of rate moves
"""
import os,json,requests
from datetime import datetime,timezone
import config

_TIMEOUT = 10

def get_sector_etf_flows():
    """
    Get 5-day price momentum for key sector ETFs.
    Rotating INTO a sector = institutional accumulation BEFORE individual stocks move.
    This is the leading indicator -- sector ETF moves lead stock moves by 1-3 days.
    """
    try:
        import yfinance as yf
        ETFs = {
            "XLE":"Energy","XLK":"Technology","XLF":"Financials",
            "XLV":"Healthcare","XLI":"Industrials","XLC":"Communication",
            "XLY":"Cons.Disc","XLP":"Cons.Staples","XLU":"Utilities",
            "XLB":"Materials","XLRE":"Real Estate","XBI":"Biotech",
            "SMH":"Semis","ARKK":"Innovation","ITA":"Defense",
        }
        flows = {}
        for etf,name in ETFs.items():
            try:
                hist = yf.Ticker(etf).history(period="10d")
                if len(hist) >= 5:
                    r5d = round((float(hist["Close"].iloc[-1])/float(hist["Close"].iloc[-5])-1)*100,1)
                    r1d = round((float(hist["Close"].iloc[-1])/float(hist["Close"].iloc[-2])-1)*100,2)
                    vol_ratio = round(float(hist["Volume"].iloc[-1])/float(hist["Volume"].mean()),2)
                    flows[etf] = {"name":name,"5d":r5d,"1d":r1d,"vol_ratio":vol_ratio}
            except Exception: pass
        return flows
    except Exception: return {}

def get_options_market_signals():
    """
    Get VIX term structure and options market signals.
    VIX term structure inversion = institutional fear = sell signal.
    Put/call ratio spikes = hedging = potential reversal.
    """
    try:
        import yfinance as yf
        signals = {}
        # VIX vs VIX3M (term structure)
        for sym,key in [("^VIX","vix"),("^VIX3M","vix3m")]:
            try:
                h = yf.Ticker(sym).history(period="2d")
                if not h.empty: signals[key] = round(float(h["Close"].iloc[-1]),2)
            except Exception: pass
        if "vix" in signals and "vix3m" in signals:
            signals["inverted"] = signals["vix"] > signals["vix3m"]
            signals["spread"] = round(signals["vix"]-signals["vix3m"],2)
        return signals
    except Exception: return {}

def get_earnings_revision_signals(basket_tickers):
    """
    Detect tickers with analyst estimate revisions in last 7 days.
    Rising estimates BEFORE earnings = strong buy signal.
    Falling estimates = exit signal.
    Uses Finnhub earnings estimates.
    """
    revisions = []
    if not config.FINNHUB_API_KEY: return revisions
    for ticker in basket_tickers[:20]:  # limit API calls
        try:
            r = requests.get(
                "https://finnhub.io/api/v1/stock/earnings",
                params={"symbol":ticker,"token":config.FINNHUB_API_KEY},
                timeout=_TIMEOUT
            )
            if r.status_code != 200: continue
            data = r.json()
            if not data: continue
            latest = data[0]
            actual = latest.get("actual",0) or 0
            est    = latest.get("estimate",0) or 0
            if est and est != 0:
                surprise = round((actual-est)/abs(est)*100,1)
                if abs(surprise) >= 10:
                    revisions.append({
                        "ticker":ticker,"surprise_pct":surprise,
                        "actual":actual,"estimate":est,
                        "signal":"bullish" if surprise>0 else "bearish"
                    })
        except Exception: pass
    return sorted(revisions,key=lambda x:-abs(x["surprise_pct"]))[:10]

def compute_proactive_signals(basket_tickers=None, positions=None):
    """
    Main function -- computes all proactive leading indicators.
    Returns structured dict with:
      rotation_signals: sectors gaining/losing institutional money
      fear_signals: VIX term structure, put/call
      earnings_revisions: surprise beats/misses
      pre_position_alerts: specific actions to take BEFORE price moves
    """
    if basket_tickers is None: basket_tickers = []
    if positions is None: positions = []
    held = {p["symbol"]:p for p in positions}

    etf_flows = get_sector_etf_flows()
    options   = get_options_market_signals()
    revisions = get_earnings_revision_signals(basket_tickers)

    alerts = []
    pre_position = []

    # Sector rotation alerts
    gainers = [(etf,d) for etf,d in etf_flows.items() if d["5d"]>=3]
    losers  = [(etf,d) for etf,d in etf_flows.items() if d["5d"]<=-3]
    gainers.sort(key=lambda x:-x[1]["5d"])
    losers.sort(key=lambda x:x[1]["5d"])

    if gainers:
        top = gainers[0]
        alerts.append("SECTOR INFLOW: "+top[1]["name"]+" ("+top[0]+") +"+str(top[1]["5d"])+"% 5d"
                       +" vol=x"+str(top[1]["vol_ratio"])+" -- institutional accumulation")
        pre_position.append("Pre-position: "+top[1]["name"]+" sector leaders before price follows ETF")

    if losers:
        bot = losers[0]
        alerts.append("SECTOR OUTFLOW: "+bot[1]["name"]+" ("+bot[0]+") "+str(bot[1]["5d"])+"% 5d"
                       +" -- institutional distribution")
        # Check if we hold stocks in this sector
        xle_losers = [s for s in held if bot[0]=="XLE" and s in ["COP","OXY","PBR","FANG","GLNG","ET"]]
        if xle_losers:
            pre_position.append("URGENT: "+bot[1]["name"]+" ETF selling -- held positions at risk: "+", ".join(xle_losers))

    # VIX term structure
    if options.get("inverted"):
        alerts.append("VIX INVERSION: spot="+str(options["vix"])+" > 3M="+str(options["vix3m"])
                       +" (spread="+str(options["spread"])+") -- acute fear, reduce gross exposure 25%")

    # Earnings revision alerts
    for rev in revisions[:5]:
        sym = rev["ticker"]
        pct = rev["surprise_pct"]
        sig = "BEAT" if pct>0 else "MISS"
        msg = "EARNINGS "+sig+": "+sym+" "+("+" if pct>0 else "")+str(pct)+"% vs estimate"
        alerts.append(msg)
        if pct > 15 and sym not in held:
            pre_position.append("T+2 ENTRY: "+sym+" beat +"+str(pct)+"% -- watch for gap hold at T+2")
        elif pct < -15 and sym in held:
            pre_position.append("REVIEW EXIT: "+sym+" miss "+str(pct)+"% -- thesis may be broken")

    return {
        "etf_flows":      etf_flows,
        "rotation_alerts":alerts,
        "pre_position":   pre_position,
        "options":        options,
        "revisions":      revisions,
    }