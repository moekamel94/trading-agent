"""
Basket curation — monthly deep + weekly lightweight.

Monthly (run inside run_monthly_research()):
  1. FMP screener for new high-growth candidates (paid API)
  2. Flag existing stocks with degrading thesis
  3. Claude recommends up to 5 adds and any removes

Weekly (run every Saturday, free-only):
  1. Free signals: yfinance technicals + congress/insider free APIs
  2. Score each ticker against ADD/REMOVE criteria
  3. Claude reviews flags and recommends basket changes
  Cost: ~$0.03/month (one tiny Haiku call)

ADD criteria (need 2+ of):
  - Congress buy last 14 days
  - Price breakout: >20% above SMA50 AND volume >1.5x ADV
  - Analyst upgrade with >25% price target upside (yfinance)
  - Earnings beat >5% (most recent quarter)
  - Revenue growth accelerating 2+ consecutive quarters >25%
  - Future Growth Score ≥ 55 (cached monthly)

REMOVE criteria — hard (immediate, any 1):
  - Market cap < $300M
  - Bankruptcy/delisting indicators

REMOVE criteria — soft (need 2+):
  - Death cross AND revenue declining
  - Price -30% from 52-week high AND death cross
  - Zombie: price ±5% over 12 weeks AND no catalyst in cached research
  - Congress net selling ≥ 3 transactions in 30 days
  - Analyst consensus majority Sell (yfinance)
"""
import json
import requests
import yfinance as yf
import config
import anthropic

_FMP  = "https://financialmodelingprep.com/stable"
_TIMEOUT = 12

# Sectors Kimmy cares about — used for screener filtering
_THESIS_SECTORS = [
    "Technology", "Healthcare", "Energy", "Basic Materials",
    "Industrials", "Consumer Cyclical", "Financial Services",
    "Communication Services",
]

# Hard ethical exclusions — never add back
_ETHICAL_EXCLUSIONS = {"APP"}


def _fmp_screener(min_mktcap: int = 500_000_000,
                  min_rev_growth: float = 0.15) -> list[dict]:
    """Pull high-growth mid/large-cap candidates from FMP screener."""
    if not config.FMP_API_KEY:
        return []
    try:
        resp = requests.get(
            f"{_FMP}/stock-screener",
            params={
                "marketCapMoreThan": min_mktcap,
                "revenueGrowthMoreThan": min_rev_growth,
                "country": "US",
                "exchange": "NASDAQ,NYSE",
                "limit": 80,
                "apikey": config.FMP_API_KEY,
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        return resp.json() or []
    except Exception as e:
        print(f"  [Curation] Screener error: {e}")
        return []


def _quick_fundamentals(symbol: str) -> dict:
    """Fast yfinance snapshot for a candidate."""
    try:
        t = yf.Ticker(symbol)
        info = t.info or {}
        return {
            "symbol":         symbol,
            "name":           info.get("longName", symbol),
            "sector":         info.get("sector", ""),
            "market_cap":     info.get("marketCap", 0),
            "revenue_growth": info.get("revenueGrowth"),
            "eps_growth":     info.get("earningsGrowth"),
            "pe_ratio":       info.get("trailingPE"),
            "profit_margin":  info.get("profitMargins"),
            "price":          info.get("currentPrice") or info.get("regularMarketPrice"),
        }
    except Exception:
        return {"symbol": symbol}


def _flag_weak_existing(existing: list[str],
                        cached_research: dict) -> list[dict]:
    """
    Identify current basket stocks showing thesis degradation.
    Flags if: revenue declining AND negative EPS growth AND price below SMA200.
    """
    flagged = []
    for sym in existing:
        cached = cached_research.get(sym, {})
        if not cached:
            continue
        fund = cached.get("fundamentals") or cached.get("financial_data") or {}
        fin  = (fund.get("fmp") or {}) if isinstance(fund, dict) else {}

        rev_growth  = fund.get("revenue_growth") or (fin.get("revenueGrowth") if fin else None)
        eps_growth  = fund.get("eps_growth_yoy") or (fin.get("epsgrowth") if fin else None)

        # Check technicals via yfinance
        try:
            tk   = yf.Ticker(sym)
            hist = tk.history(period="1y")
            if len(hist) < 50:
                continue
            price  = float(hist["Close"].iloc[-1])
            sma200 = float(hist["Close"].tail(200).mean()) if len(hist) >= 200 else None
            below_sma200 = sma200 and price < sma200
        except Exception:
            below_sma200 = False

        rev_neg = rev_growth is not None and rev_growth < -0.05
        eps_neg = eps_growth is not None and eps_growth < -0.10

        if rev_neg and eps_neg and below_sma200:
            flagged.append({
                "symbol":       sym,
                "rev_growth":   rev_growth,
                "eps_growth":   eps_growth,
                "below_sma200": below_sma200,
                "reason":       f"revenue {rev_growth:.1%}, EPS {eps_growth:.1%}, below SMA200",
            })

    return flagged


def _claude_curate(candidates: list[dict],
                   flagged_existing: list[dict],
                   current_basket: list[str]) -> dict:
    """
    Ask Claude to recommend basket changes.
    Returns {"add": [...], "remove": [...], "reasoning": "..."}
    """
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    prompt = f"""You are Kimmy's investment committee doing a monthly basket review.

Current basket ({len(current_basket)} stocks): {current_basket}

NEW CANDIDATES from market screener (revenue growth >15%, mktcap >$500M, not already in basket):
{json.dumps(candidates[:30], indent=2, default=str)}

EXISTING STOCKS FLAGGED FOR WEAKNESS (declining revenue + EPS + below SMA200):
{json.dumps(flagged_existing, indent=2, default=str)}

Kimmy's thesis: AI infrastructure, semiconductors, quantum computing, cybersecurity,
nuclear energy, defense tech, healthcare AI, e-commerce, financial infrastructure,
energy/commodities (copper, rare earths, oil FCF machines), robotics, space, clean energy.
Target: 25% annual return. Ethical: no surveillance/predatory ad platforms.

TASK:
1. From the new candidates, recommend up to 5 to ADD to the basket. Only add if they
   clearly fit the thesis and have a compelling growth story. Quality over quantity.
2. From the flagged existing stocks, recommend any to REMOVE. Only remove if the thesis
   is genuinely broken — not just a bad quarter.
3. You may also recommend removing non-flagged stocks if you see a clearly broken thesis.

Respond in this exact JSON format:
{{
  "add": ["TICK1", "TICK2"],
  "remove": ["TICK3"],
  "reasoning": "brief explanation of each add and remove"
}}"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return json.loads(raw)
    except Exception as e:
        print(f"  [Curation] Claude error: {e}")
        return {"add": [], "remove": [], "reasoning": "Claude call failed"}


def run(existing_basket: list[str],
        cached_research: dict,
        config_module) -> tuple[list[str], list[str]]:
    """
    Main entry point — call from run_monthly_research().

    Returns (to_add, to_remove) lists. Caller is responsible for
    actually updating basket.json and config.TICKER_TIERS.
    """
    print("\n  [Curation] Screening market for new candidates...")
    raw_candidates = _fmp_screener()

    # Filter out existing basket, ethical exclusions, and non-US
    candidates = []
    for c in raw_candidates:
        sym = (c.get("symbol") or "").upper()
        if (sym and sym not in existing_basket
                and sym not in _ETHICAL_EXCLUSIONS
                and sym.isalpha()
                and len(sym) <= 5):
            fundamentals = _quick_fundamentals(sym)
            if fundamentals.get("sector") in _THESIS_SECTORS:
                candidates.append(fundamentals)

    print(f"  [Curation] {len(candidates)} new candidates in thesis sectors")

    print("  [Curation] Checking existing basket for thesis degradation...")
    flagged = _flag_weak_existing(existing_basket, cached_research)
    if flagged:
        print(f"  [Curation] Flagged for review: {[f['symbol'] for f in flagged]}")
    else:
        print("  [Curation] No existing stocks flagged for removal")

    print("  [Curation] Asking Claude to curate adds/removes...")
    result = _claude_curate(candidates, flagged, existing_basket)

    to_add    = [s for s in result.get("add", [])
                 if s not in existing_basket and s not in _ETHICAL_EXCLUSIONS]
    to_remove = [s for s in result.get("remove", [])
                 if s in existing_basket]
    reasoning = result.get("reasoning", "")

    print(f"  [Curation] ADD: {to_add}")
    print(f"  [Curation] REMOVE: {to_remove}")
    print(f"  [Curation] Reasoning: {reasoning[:200]}")

    return to_add, to_remove, reasoning


# ── Weekly basket review (free-only signals) ──────────────────────────────────

def _weekly_score_ticker(sym: str, cached_research: dict) -> dict:
    """
    Score one basket ticker against ADD/REMOVE criteria using only free data.
    Returns a scoring dict for the Claude weekly prompt.
    """
    result = {"symbol": sym, "add_signals": [], "remove_hard": [], "remove_soft": []}
    try:
        tk   = yf.Ticker(sym)
        info = tk.info or {}
        hist = tk.history(period="6mo")
        if len(hist) < 20:
            return result

        price      = float(hist["Close"].iloc[-1])
        mktcap     = info.get("marketCap", 0) or 0
        rev_growth = info.get("revenueGrowth")     # trailing 12m
        analyst_tgt= info.get("targetMeanPrice")
        rec_key    = info.get("recommendationKey", "").lower()
        hi_52w     = info.get("fiftyTwoWeekHigh") or price

        close   = hist["Close"]
        volume  = hist["Volume"]
        sma50   = float(close.tail(50).mean()) if len(close) >= 50 else None
        sma200  = float(close.tail(200).mean()) if len(close) >= 200 else None
        avg_vol = float(volume.tail(20).mean()) if len(volume) >= 20 else None
        avg_5d  = float(volume.tail(5).mean())  if len(volume) >= 5  else None
        vol_ratio = (avg_5d / avg_vol) if (avg_vol and avg_vol > 0) else None
        death_cross = sma50 and sma200 and sma50 < sma200

        # ── Hard remove ───────────────────────────────────────────────────────
        if mktcap and mktcap < 300_000_000:
            result["remove_hard"].append(f"market cap ${mktcap/1e6:.0f}M < $300M threshold")
        if info.get("financialCurrency") is None and mktcap == 0:
            result["remove_hard"].append("no financial data — possible delisting/OTC")

        # ── Soft remove ───────────────────────────────────────────────────────
        if death_cross and rev_growth is not None and rev_growth < -0.05:
            result["remove_soft"].append(f"death cross + revenue declining {rev_growth:.1%}")
        if hi_52w and price < hi_52w * 0.70 and death_cross:
            result["remove_soft"].append(f"price {(price/hi_52w - 1):.1%} from 52w high + death cross")
        if rec_key in ("sell", "strong_sell", "underperform"):
            result["remove_soft"].append(f"analyst consensus: {rec_key}")
        # 12-week zombie check
        if len(close) >= 60:
            p_12w = float(close.iloc[-60])
            pct_change = abs((price - p_12w) / p_12w) if p_12w > 0 else 1
            if pct_change < 0.05:
                cached = cached_research.get(sym, {})
                has_catalyst = bool((cached.get("research") or {}).get("snippets"))
                if not has_catalyst:
                    result["remove_soft"].append(f"zombie: ±{pct_change:.1%} over 12 weeks, no catalyst in research cache")

        # ── Add signals ───────────────────────────────────────────────────────
        if sma50 and price > sma50 * 1.20 and vol_ratio and vol_ratio > 1.5:
            result["add_signals"].append(f"breakout: {(price/sma50 - 1):.1%} above SMA50, vol {vol_ratio:.1f}x")
        if analyst_tgt and price > 0:
            upside = (analyst_tgt - price) / price
            if upside > 0.25:
                result["add_signals"].append(f"analyst target ${analyst_tgt:.0f} = +{upside:.1%} upside")
        if rev_growth and rev_growth > 0.25:
            result["add_signals"].append(f"revenue growth {rev_growth:.1%} > 25% threshold")

        # Earnings beat from cached research
        cached = cached_research.get(sym, {})
        em = cached.get("earnings_momentum") or {}
        if em.get("label") in ("strong_bullish", "bullish") and sym not in _ETHICAL_EXCLUSIONS:
            result["add_signals"].append(f"earnings momentum: {em.get('label')}")

    except Exception as e:
        result["error"] = str(e)

    return result


def _score_congress_weekly(existing_basket: list[str]) -> dict:
    """
    Lightweight congress check for weekly review.
    Returns {sym: {"buys": n, "sells": n, "signal": "bullish"|"bearish"|"neutral"}}
    using the same free House/Senate Stock Watcher APIs as signals/congress.py.
    """
    try:
        from signals import congress as cong_mod
        out = {}
        for sym in existing_basket:
            try:
                result = cong_mod.fetch(sym)
                out[sym] = result
            except Exception:
                pass
        return out
    except Exception:
        return {}


def _claude_weekly(scores: list[dict], congress_map: dict,
                   current_basket: list[str], cached_research: dict) -> dict:
    """Ask Claude (Haiku) to make weekly basket recommendations from free-signal scores."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # Summarise the scored tickers
    flagged_remove = []
    flagged_add    = []
    for s in scores:
        sym = s["symbol"]
        if s.get("remove_hard"):
            flagged_remove.append({"symbol": sym, "reason": "HARD: " + "; ".join(s["remove_hard"])})
        elif len(s.get("remove_soft", [])) >= 2:
            flagged_remove.append({"symbol": sym, "reason": "SOFT: " + "; ".join(s["remove_soft"])})
        if s.get("add_signals") and sym not in current_basket:
            flagged_add.append({"symbol": sym, "add_signals": s["add_signals"]})

    # Congress signals
    cong_buys  = [sym for sym, v in congress_map.items() if v.get("net_signal") == "bullish"]
    cong_sells = [sym for sym, v in congress_map.items() if v.get("net_signal") == "bearish"]

    prompt = f"""You are Kimmy's weekly basket review committee (Saturday lightweight review).
This is a CONSERVATIVE review — only make changes when signals are clear.
Monthly deep research handles major additions. Weekly is for maintenance + fast-moving signals.

Current basket ({len(current_basket)} tickers): {current_basket}

TICKERS FLAGGED FOR REMOVAL (scored against ADD/REMOVE criteria):
{json.dumps(flagged_remove, indent=2)}

CANDIDATES FLAGGED FOR ADDITION (not yet in basket, showing add signals):
{json.dumps(flagged_add[:10], indent=2)}

CONGRESS NET BUYING this week (strong add signal if not already in basket): {cong_buys}
CONGRESS NET SELLING (remove signal if already in basket): {cong_sells}

Kimmy's thesis: AI infrastructure, semiconductors, quantum computing, cybersecurity,
nuclear energy, defense tech, healthcare AI, e-commerce, financial infrastructure,
energy/commodities, robotics, space, clean energy. No surveillance/predatory platforms.

RULES:
- Hard remove flags → always recommend REMOVE
- Soft remove flags (≥2 signals) → recommend REMOVE unless strong counter-thesis exists
- Congress buying a non-basket stock → strong ADD signal (fits thesis? ADD)
- Be conservative: prefer HOLD over premature add/remove
- Max 3 adds per week (quality > quantity)

Respond in this exact JSON:
{{
  "add": ["TICK1", "TICK2"],
  "remove": ["TICK3"],
  "reasoning": "brief explanation of each change"
}}"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return json.loads(raw)
    except Exception as e:
        print(f"  [WeeklyCuration] Claude error: {e}")
        return {"add": [], "remove": [], "reasoning": "Claude call failed"}


def run_weekly(existing_basket: list[str],
               cached_research: dict) -> tuple[list[str], list[str], str]:
    """
    Weekly basket review — free signals only, one small Haiku call.
    Runs every Saturday 8:00 AM ET.
    Returns (to_add, to_remove, reasoning).
    """
    print("\n  [WeeklyCuration] Scoring basket tickers against weekly criteria...")
    scores = [_weekly_score_ticker(sym, cached_research)
              for sym in existing_basket if not sym.startswith("BTC")]
    print(f"  [WeeklyCuration] Scored {len(scores)} tickers")

    print("  [WeeklyCuration] Checking congress activity...")
    congress_map = _score_congress_weekly(existing_basket)

    print("  [WeeklyCuration] Asking Claude for weekly recommendations...")
    result = _claude_weekly(scores, congress_map, existing_basket, cached_research)

    to_add    = [s for s in result.get("add", [])
                 if s not in existing_basket and s not in _ETHICAL_EXCLUSIONS]
    to_remove = [s for s in result.get("remove", [])
                 if s in existing_basket]
    reasoning = result.get("reasoning", "")

    print(f"  [WeeklyCuration] ADD: {to_add}")
    print(f"  [WeeklyCuration] REMOVE: {to_remove}")
    print(f"  [WeeklyCuration] Reasoning: {reasoning[:200]}")

    return to_add, to_remove, reasoning
