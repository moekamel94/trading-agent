"""
Early Discovery Signal Module — finds stocks before Wall Street does.

Runs weekly (Friday alongside basket review). Scans:
  1. SEC EDGAR insider buying clusters (3+ insiders in 30 days, market cap < $5B)
  2. Google Trends rising search interest before analyst coverage picks up
  3. Patent filings / FDA submissions for binary catalysts not yet priced in
  4. Academic paper citations in AI/biotech/quantum referencing specific companies
  5. Congressional trading in stocks with zero analyst coverage (pure alpha)

Scores each candidate 0–100. Flags any stock where:
  insider buying + rising Google Trends + no analyst coverage → score >= 80

Sends weekly Telegram report: "EARLY SIGNALS — stocks Wall Street hasn't found yet"
"""

import json
import os
import time
from datetime import datetime, date, timedelta, timezone

import requests

import config

import time as _etime
_EARLY_CACHE = {}
_EARLY_CACHE_TTL = 2 * 3600

def _early_cached(url, params, headers=None, timeout=10):
    import requests
    key = url + str(sorted(params.items()))
    now = _etime.time()
    if key in _EARLY_CACHE:
        result, ts = _EARLY_CACHE[key]
        if now - ts < _EARLY_CACHE_TTL:
            return result
    try:
        r = requests.get(url, params=params, headers=headers or {}, timeout=timeout)
        if r.status_code == 200:
            result = r.json()
            _EARLY_CACHE[key] = (result, now)
            return result
    except Exception:
        pass
    return {}


_TIMEOUT = 15
_CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", ".early_signal_cache.json")
_MAX_MARKET_CAP = 5_000_000_000   # $5B cap for early discovery
_MIN_SCORE      = 80               # minimum composite score to flag


# ── 1. SEC EDGAR — insider buying clusters ────────────────────────────────────

def _sec_insider_clusters(lookback_days: int = 30) -> list[dict]:
    """
    Query SEC EDGAR Form 4 filings for clusters of 3+ insiders buying in 30 days
    on stocks with market cap < $5B that are NOT already in the basket.
    Returns [{symbol, insider_count, total_value_usd, latest_date}]
    """
    from_date = (date.today() - timedelta(days=lookback_days)).isoformat()
    basket_syms = set()
    try:
        from basket.manager import load as _load_basket
        basket_syms = set(_load_basket())
    except Exception:
        pass

    results: list[dict] = []
    try:
        r = requests.get(
            "https://efts.sec.gov/LATEST/search-index?q=%22form+4%22"
            f"&dateRange=custom&startdt={from_date}&forms=4",
            headers={"User-Agent": "Kimmy Trading Bot research@kimmy.ai"},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return results

        hits = r.json().get("hits", {}).get("hits", [])
        filings_by_cik: dict[str, list[dict]] = {}
        for hit in hits[:500]:
            src = hit.get("_source", {})
            ticker = src.get("file-num", "")
            cik = src.get("entity_id", "")
            filed = src.get("file_date", "")
            if cik:
                if cik not in filings_by_cik:
                    filings_by_cik[cik] = []
                filings_by_cik[cik].append({"ticker": ticker, "filed": filed})

        # Find CIKs with 3+ distinct filings (proxy for 3+ insiders)
        for cik, filings in filings_by_cik.items():
            if len(filings) < 3:
                continue
            # Try to resolve ticker via EDGAR company facts
            try:
                facts_r = requests.get(
                    f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json",
                    headers={"User-Agent": "Kimmy Trading Bot research@kimmy.ai"},
                    timeout=_TIMEOUT,
                )
                if facts_r.status_code != 200:
                    continue
                company = facts_r.json()
                tickers_list = company.get("tickers", [])
                if not tickers_list:
                    continue
                sym = tickers_list[0].upper()
                if sym in basket_syms or not sym.isalpha() or len(sym) > 5:
                    continue

                # Market cap filter
                try:
                    import yfinance as yf
                    info = yf.Ticker(sym).info
                    mktcap = info.get("marketCap", 0) or 0
                    if mktcap <= 0 or mktcap > _MAX_MARKET_CAP:
                        continue
                    analyst_count = (info.get("numberOfAnalystOpinions") or 0)
                except Exception:
                    analyst_count = 0
                    mktcap = 0

                results.append({
                    "symbol":         sym,
                    "insider_count":  len(filings),
                    "market_cap_m":   int(mktcap / 1e6),
                    "analyst_count":  analyst_count,
                    "latest_filing":  max(f["filed"] for f in filings),
                    "source":         "sec_insider_cluster",
                })
            except Exception:
                continue
    except Exception as e:
        print(f"  [EarlySignal] SEC EDGAR error: {e}")

    return results


# ── 2. Google Trends — rising search interest ─────────────────────────────────

def _google_trends_rising(symbols: list[str]) -> dict[str, float]:
    """
    Check Google Trends for rising interest in company names.
    Returns {symbol: trend_score} where score > 1.0 means rising interest.
    Uses pytrends if available, else skips gracefully.
    """
    scores: dict[str, float] = {}
    try:
        from pytrends.request import TrendReq
        pt = TrendReq(hl="en-US", tz=360, timeout=(10, 25))
        for sym in symbols[:10]:  # batch limit
            try:
                pt.build_payload([sym], timeframe="today 3-m")
                df = pt.interest_over_time()
                if df.empty or sym not in df.columns:
                    continue
                vals = df[sym].tolist()
                if len(vals) < 8:
                    continue
                recent_avg = sum(vals[-4:]) / 4
                older_avg  = sum(vals[-12:-4]) / 8 if len(vals) >= 12 else sum(vals[:4]) / 4
                if older_avg > 0:
                    scores[sym] = round(recent_avg / older_avg, 2)
                time.sleep(1)  # respect rate limits
            except Exception:
                continue
    except ImportError:
        pass  # pytrends not installed — skip gracefully
    return scores


# ── 3. USPTO patent approvals ─────────────────────────────────────────────────

def _uspto_recent_grants(sectors: list[str] | None = None) -> list[dict]:
    """
    Fetch recent USPTO patent grants for AI/biotech/quantum/defense companies.
    Returns [{assignee, patent_title, grant_date, sector_guess}]
    """
    results: list[dict] = []
    if sectors is None:
        sectors = ["artificial intelligence", "quantum computing",
                   "gene therapy", "mRNA", "autonomous", "space"]
    try:
        for kw in sectors[:4]:
            r = requests.get(
                "https://developer.uspto.gov/ibd-api/v1/application/grants",
                params={"searchText": kw, "dateRangeData.startDate":
                        (date.today() - timedelta(days=30)).isoformat(),
                        "rows": 10},
                timeout=_TIMEOUT,
            )
            if r.status_code != 200:
                continue
            data = r.json().get("results", {}).get("patentFileWrapperDataBag", [])
            for item in data[:5]:
                assignee = item.get("applicantNameBag", [""])[0] if item.get("applicantNameBag") else ""
                title    = item.get("inventionTitle", "")
                date_str = item.get("grantDate", "")
                if assignee and title:
                    results.append({
                        "assignee":    assignee,
                        "title":       title[:120],
                        "grant_date":  date_str,
                        "keyword":     kw,
                        "source":      "uspto_patent",
                    })
    except Exception as e:
        print(f"  [EarlySignal] USPTO error: {e}")
    return results[:15]


# ── 3b. USPTO Patent API — R&D acceleration signals ─────────────────────────

def _uspto_patent_signals(ticker: str, days: int = 60) -> dict:
    """
    Check USPTO patent API for R&D acceleration signals.
    Flags companies filing 3+ patents in same tech area within 60 days.
    Free API: https://api.patentsview.org/patents/query
    """
    try:
        from datetime import date, timedelta
        import requests, json
        from_date = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        payload = {
            "q": {"_and": [
                {"_text_any": {"patent_abstract": ticker}},
                {"_gte": {"patent_date": from_date}}
            ]},
            "f": ["patent_number", "patent_date", "patent_title", "assignee_organization"],
            "o": {"per_page": 25}
        }
        r = requests.post(
            "https://api.patentsview.org/patents/query",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            count = data.get("total_patent_count", 0)
            patents = data.get("patents", [])
            return {
                "patent_count_60d": count,
                "acceleration": count >= 3,  # 3+ patents = R&D acceleration signal
                "recent_titles": [p.get("patent_title", "")[:80] for p in patents[:3]]
            }
    except Exception:
        pass
    return {}


# ── 4. FDA submissions / DARPA / NIH grants ──────────────────────────────────

def _fda_recent_submissions() -> list[dict]:
    """
    Scan FDA drug submissions (IND/NDA/BLA) for binary catalyst events.
    Uses FDA openFDA API — free, no key required.
    """
    results: list[dict] = []
    try:
        r = requests.get(
            "https://api.fda.gov/drug/drugsfda.json",
            params={
                "search": f"submissions.submission_type:NDA+OR+submissions.submission_type:BLA",
                "limit": 20,
                "sort": "openfda.brand_name:desc",
            },
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return results
        items = r.json().get("results", [])
        for item in items[:10]:
            sponsor = item.get("sponsor_name", "")
            app_num = item.get("application_number", "")
            subs    = item.get("submissions", [])
            recent  = sorted(subs, key=lambda x: x.get("submission_status_date", ""), reverse=True)
            if recent and sponsor:
                results.append({
                    "sponsor":   sponsor,
                    "app_num":   app_num,
                    "sub_type":  recent[0].get("submission_type", ""),
                    "sub_date":  recent[0].get("submission_status_date", ""),
                    "source":    "fda_submission",
                })
    except Exception as e:
        print(f"  [EarlySignal] FDA API error: {e}")
    return results


# ── 5. Congressional trades with zero analyst coverage ───────────────────────

def _congress_zero_analyst() -> list[dict]:
    """
    Find congressional stock trades (last 30 days) in companies with < 3 analyst ratings.
    These are pure alpha — no institutional coverage yet.
    Uses quiverquant public API (free tier) or Unusual Whales congress endpoint.
    """
    results: list[dict] = []
    from_dt = (date.today() - timedelta(days=30)).isoformat()
    try:
        # Unusual Whales congressional endpoint
        if config.UNUSUAL_WHALES_API_KEY:
            r = requests.get(
                "https://api.unusualwhales.com/api/congress/recent-trades",
                headers={"Authorization": f"Bearer {config.UNUSUAL_WHALES_API_KEY}"},
                timeout=_TIMEOUT,
            )
            if r.status_code == 200:
                trades = r.json().get("data", [])
                for trade in trades[:50]:
                    sym      = (trade.get("ticker") or "").upper()
                    txn_type = (trade.get("transaction_type") or "").lower()
                    txn_date = trade.get("transaction_date", "")
                    if not sym or "buy" not in txn_type or not sym.isalpha():
                        continue
                    if txn_date < from_dt:
                        continue
                    # Check analyst coverage
                    try:
                        import yfinance as yf
                        info = yf.Ticker(sym).info
                        n_analysts = info.get("numberOfAnalystOpinions") or 0
                        if n_analysts < 3:
                            results.append({
                                "symbol":      sym,
                                "analyst_count": n_analysts,
                                "txn_date":    txn_date,
                                "member":      trade.get("representative", ""),
                                "source":      "congress_zero_analyst",
                            })
                    except Exception:
                        continue
    except Exception as e:
        print(f"  [EarlySignal] Congress zero-analyst scan error: {e}")
    return results[:10]


# ── Composite scorer ──────────────────────────────────────────────────────────

def _score_candidate(
    symbol: str,
    insider_data: dict | None,
    trend_score: float,
    analyst_count: int,
    extra_signals: list[str],
) -> int:
    """
    Score a potential early-discovery candidate 0–100.
    UNDISCOVERED target: insider buying + rising trends + no analyst coverage.
    """
    score = 0

    # Insider buying cluster (max 40 pts)
    if insider_data:
        n = insider_data.get("insider_count", 0)
        if n >= 5:
            score += 40
        elif n >= 3:
            score += 30
        elif n >= 1:
            score += 15

    # Google Trends rising (max 25 pts)
    if trend_score >= 2.0:
        score += 25
    elif trend_score >= 1.5:
        score += 18
    elif trend_score >= 1.2:
        score += 10

    # Zero/low analyst coverage (max 20 pts — inverse scoring)
    if analyst_count == 0:
        score += 20
    elif analyst_count <= 2:
        score += 15
    elif analyst_count <= 5:
        score += 8

    # Extra signals bonus (max 15 pts)
    if "patent_grant" in extra_signals:
        score += 5
    if "fda_submission" in extra_signals:
        score += 7
    if "congress_buy" in extra_signals:
        score += 8
    if "darpa_contract" in extra_signals:
        score += 10

    return min(100, score)


# ── Narrative stage classifier ────────────────────────────────────────────────

def classify_narrative_stage(symbol: str, analyst_count: int | None = None,
                              social_volume: str | None = None,
                              has_insider_buying: bool = False) -> dict:
    """
    Classify a stock's narrative stage:
      UNDISCOVERED: <5 analyst ratings, low social volume, insider buying
      EARLY:        5-15 analysts, growing social, institutional accumulation
      CONSENSUS:    15-30 analysts, high social, crowded trade
      LATE:         30+ analysts, all bullish, maximum coverage

    Returns {stage, label, weight_modifier, description}
    Weight modifier: UNDISCOVERED=1.3, EARLY=1.1, CONSENSUS=1.0, LATE=0.7
    """
    if analyst_count is None:
        try:
            import yfinance as yf
            info = yf.Ticker(symbol).info
            analyst_count = info.get("numberOfAnalystOpinions") or 0
        except Exception:
            analyst_count = 0

    if analyst_count < 5 and has_insider_buying:
        stage, modifier = "UNDISCOVERED", 1.3
        desc = "< 5 analysts, insider buying — best entry asymmetry"
    elif analyst_count < 5:
        stage, modifier = "UNDISCOVERED", 1.2
        desc = "< 5 analysts, low coverage — early discovery candidate"
    elif analyst_count <= 15:
        stage, modifier = "EARLY", 1.1
        desc = "5-15 analysts, story building — growing institutional interest"
    elif analyst_count <= 30:
        stage, modifier = "CONSENSUS", 1.0
        desc = "15-30 analysts, well-known story — standard entry criteria"
    else:
        stage, modifier = "LATE", 0.7
        desc = "30+ analysts, crowded trade — reduce or avoid new entry"

    return {
        "stage":           stage,
        "analyst_count":   analyst_count,
        "weight_modifier": modifier,
        "description":     desc,
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def run_weekly_scan() -> list[dict]:
    """
    Run the weekly early-discovery scan. Returns list of flagged candidates.
    Call from basket curation on Friday alongside the weekly basket review.
    """
    print("\n  [EarlySignal] Starting weekly early-discovery scan...")

    flagged: list[dict] = []

    # Step 1: SEC insider clusters
    print("  [EarlySignal] Scanning SEC EDGAR insider clusters...")
    insider_clusters = _sec_insider_clusters(lookback_days=30)
    insider_map = {c["symbol"]: c for c in insider_clusters}
    print(f"  [EarlySignal] Found {len(insider_clusters)} insider clusters")

    # Step 2: Google Trends for cluster symbols
    trend_scores: dict[str, float] = {}
    if insider_clusters:
        syms = [c["symbol"] for c in insider_clusters[:15]]
        print(f"  [EarlySignal] Checking Google Trends for {len(syms)} symbols...")
        trend_scores = _google_trends_rising(syms)

    # Step 3: Congress zero-analyst trades
    print("  [EarlySignal] Scanning congressional trades (zero analyst coverage)...")
    congress_zero = _congress_zero_analyst()
    congress_syms = {c["symbol"] for c in congress_zero}

    # Step 4: Score all candidates
    all_syms = set(insider_map.keys()) | congress_syms
    for sym in all_syms:
        ins_data      = insider_map.get(sym)
        t_score       = trend_scores.get(sym, 1.0)
        analyst_count = (ins_data or {}).get("analyst_count", 0)
        if sym in congress_syms and analyst_count == 0:
            analyst_count = 0  # confirmed zero-analyst
        extra_sigs = []
        if sym in congress_syms:
            extra_sigs.append("congress_buy")

        score = _score_candidate(sym, ins_data, t_score, analyst_count, extra_sigs)
        if score >= _MIN_SCORE:
            stage_info = classify_narrative_stage(
                sym, analyst_count=analyst_count,
                has_insider_buying=ins_data is not None,
            )
            flagged.append({
                "symbol":         sym,
                "early_score":    score,
                "insider_count":  (ins_data or {}).get("insider_count", 0),
                "market_cap_m":   (ins_data or {}).get("market_cap_m", 0),
                "analyst_count":  analyst_count,
                "trend_score":    t_score,
                "extra_signals":  extra_sigs,
                "narrative_stage": stage_info["stage"],
                "source":         "early_signal",
            })

    flagged.sort(key=lambda x: x["early_score"], reverse=True)
    print(f"  [EarlySignal] {len(flagged)} candidates scored >= {_MIN_SCORE}")
    return flagged[:20]


def send_weekly_report(flagged: list[dict]) -> None:
    """Send the weekly EARLY SIGNALS report to Telegram/Discord."""
    try:
        from notifications import discord_bot as discord
    except Exception:
        return

    if not flagged:
        discord.send("📡 **EARLY SIGNALS** — No new undiscovered stocks flagged this week.")
        return

    now_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📡 **EARLY SIGNALS** — {now_str}",
        f"Stocks Wall Street hasn't found yet",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for i, c in enumerate(flagged[:10], 1):
        sym     = c["symbol"]
        score   = c["early_score"]
        ins     = c["insider_count"]
        ana     = c["analyst_count"]
        trend   = c["trend_score"]
        mktcap  = c["market_cap_m"]
        stage   = c["narrative_stage"]
        extra   = ", ".join(c.get("extra_signals", []))

        lines.append(f"\n**{i}. {sym}** — Early Score: {score}/100 | Stage: {stage}")
        lines.append(f"  👥 Insiders: {ins} buying | 📊 Analysts: {ana} | 🔍 Trends: {trend:.1f}×")
        if mktcap:
            lines.append(f"  💰 Market Cap: ${mktcap:,}M")
        if extra:
            lines.append(f"  📌 Signals: {extra}")

    lines.append(
        "\n_UNDISCOVERED stage = best entry point. "
        "These names are flagged for research — not automatic buys. "
        "Run full committee review before entering._"
    )
    discord.send("\n".join(lines))
