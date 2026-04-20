import os, requests
from dotenv import load_dotenv
load_dotenv()

results = {}

# 1. Alpaca
try:
    base = os.getenv("ALPACA_BASE_URL", "").rstrip("/v2").rstrip("/")
    r = requests.get(
        base + "/v2/account",
        headers={"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY"), "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY")},
        timeout=10,
    )
    if r.status_code == 200:
        d = r.json()
        results["Alpaca"] = "OK equity={:.2f} cash={:.2f}".format(float(d["equity"]), float(d["cash"]))
    else:
        results["Alpaca"] = "FAIL {}: {}".format(r.status_code, r.text[:80])
except Exception as e:
    results["Alpaca"] = "ERROR: {}".format(e)

# 2. Anthropic
try:
    import anthropic
    c = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    c.messages.create(model="claude-haiku-4-5-20251001", max_tokens=5, messages=[{"role": "user", "content": "hi"}])
    results["Anthropic"] = "OK"
except Exception as e:
    results["Anthropic"] = "FAIL: {}".format(e)

# 3. Finnhub
try:
    r = requests.get("https://finnhub.io/api/v1/quote?symbol=AAPL&token={}".format(os.getenv("FINNHUB_API_KEY")), timeout=10)
    d = r.json()
    results["Finnhub"] = "OK AAPL={}".format(d["c"]) if "c" in d and d["c"] else "FAIL: {}".format(d)
except Exception as e:
    results["Finnhub"] = "ERROR: {}".format(e)

# 4. Alpha Vantage
try:
    r = requests.get(
        "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=AAPL&apikey={}".format(os.getenv("ALPHA_VANTAGE_KEY")),
        timeout=10,
    )
    d = r.json()
    if "Global Quote" in d and d["Global Quote"]:
        results["AlphaVantage"] = "OK AAPL={}".format(d["Global Quote"].get("05. price", "?"))
    elif "Note" in d or "Information" in d:
        results["AlphaVantage"] = "RATE_LIMITED"
    else:
        results["AlphaVantage"] = "FAIL: {}".format(str(d)[:80])
except Exception as e:
    results["AlphaVantage"] = "ERROR: {}".format(e)

# 5. Twelve Data
try:
    r = requests.get(
        "https://api.twelvedata.com/price?symbol=AAPL&apikey={}".format(os.getenv("TWELVE_DATA_KEY")),
        timeout=10,
    )
    d = r.json()
    results["TwelveData"] = "OK AAPL={}".format(d["price"]) if "price" in d else "FAIL: {}".format(d)
except Exception as e:
    results["TwelveData"] = "ERROR: {}".format(e)

# 6. FMP
try:
    r = requests.get(
        "https://financialmodelingprep.com/stable/profile?symbol=AAPL&apikey={}".format(os.getenv("FMP_API_KEY")),
        timeout=10,
    )
    d = r.json()
    results["FMP"] = "OK {}".format(d[0]["companyName"]) if d and isinstance(d, list) else "FAIL: {}".format(str(d)[:80])
except Exception as e:
    results["FMP"] = "ERROR: {}".format(e)

# 7. Polygon (free tier: prev close aggregates)
try:
    r = requests.get(
        "https://api.polygon.io/v2/aggs/ticker/AAPL/prev?adjusted=true&apiKey={}".format(os.getenv("POLYGON_API_KEY")),
        timeout=10,
    )
    d = r.json()
    res = d.get("results", [{}])[0] if d.get("resultsCount", 0) > 0 else {}
    results["Polygon"] = "OK AAPL prev_close={}".format(res.get("c", "?")) if d.get("resultsCount", 0) > 0 else "FAIL: {}".format(str(d)[:80])
except Exception as e:
    results["Polygon"] = "ERROR: {}".format(e)

# 8. SerpAPI
try:
    r = requests.get(
        "https://serpapi.com/search.json",
        params={"q": "AAPL stock", "api_key": os.getenv("SERPAPI_KEY"), "num": 1},
        timeout=10,
    )
    d = r.json()
    results["SerpAPI"] = "OK" if "organic_results" in d else "FAIL: {}".format(str(d)[:80])
except Exception as e:
    results["SerpAPI"] = "ERROR: {}".format(e)

# 9. Tavily
try:
    r = requests.post(
        "https://api.tavily.com/search",
        json={"query": "AAPL stock", "max_results": 1},
        headers={"Authorization": "Bearer {}".format(os.getenv("TAVILY_API_KEY"))},
        timeout=10,
    )
    d = r.json()
    results["Tavily"] = "OK" if "results" in d else "FAIL {}: {}".format(r.status_code, str(d)[:80])
except Exception as e:
    results["Tavily"] = "ERROR: {}".format(e)

# 10. Exa
try:
    r = requests.post(
        "https://api.exa.ai/search",
        json={"query": "AAPL stock", "numResults": 1},
        headers={"x-api-key": os.getenv("EXA_API_KEY"), "Content-Type": "application/json"},
        timeout=10,
    )
    d = r.json()
    results["Exa"] = "OK" if "results" in d else "FAIL {}: {}".format(r.status_code, str(d)[:80])
except Exception as e:
    results["Exa"] = "ERROR: {}".format(e)

# 11. Serper
try:
    r = requests.post(
        "https://google.serper.dev/search",
        json={"q": "AAPL stock", "num": 1},
        headers={"X-API-KEY": os.getenv("SERPER_API_KEY"), "Content-Type": "application/json"},
        timeout=10,
    )
    d = r.json()
    results["Serper"] = "OK" if "organic" in d else "FAIL {}: {}".format(r.status_code, str(d)[:80])
except Exception as e:
    results["Serper"] = "ERROR: {}".format(e)

# 12. Firecrawl
try:
    r = requests.post(
        "https://api.firecrawl.dev/v1/scrape",
        json={"url": "https://finance.yahoo.com/quote/AAPL/"},
        headers={"Authorization": "Bearer {}".format(os.getenv("FIRECRAWL_API_KEY")), "Content-Type": "application/json"},
        timeout=15,
    )
    d = r.json()
    results["Firecrawl"] = "OK" if d.get("success") else "FAIL {}: {}".format(r.status_code, str(d)[:100])
except Exception as e:
    results["Firecrawl"] = "ERROR: {}".format(e)

# 13. NewData
try:
    key = os.getenv("NEWDATA_API_KEY", "")
    if not key or key in ("YOUR_KEY_HERE", "placeholder", ""):
        results["NewData"] = "NOT_SET"
    else:
        r = requests.get("https://api.newdata.io/v1/news/search?q=AAPL&token={}".format(key), timeout=10)
        d = r.json()
        results["NewData"] = "OK" if "data" in d else "FAIL {}: {}".format(r.status_code, str(d)[:80])
except Exception as e:
    results["NewData"] = "ERROR: {}".format(e)

print("\n=== API Health Check ===")
ok = warn = fail = 0
for k in sorted(results):
    v = results[k]
    if v.startswith("OK"):
        tag = "[OK  ]"; ok += 1
    elif "RATE_LIMITED" in v or "NOT_SET" in v:
        tag = "[WARN]"; warn += 1
    else:
        tag = "[FAIL]"; fail += 1
    print("{} {:<15} {}".format(tag, k, v))

print("\nSummary: {} OK, {} warnings, {} failed".format(ok, warn, fail))
if fail == 0 and warn == 0:
    print("Agent is READY TO TRADE.")
elif fail == 0:
    print("Agent is ready — warnings are non-blocking.")
else:
    print("Fix the failed APIs before trading.")
