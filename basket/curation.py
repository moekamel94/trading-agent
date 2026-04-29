"""
Basket curation — monthly deep (per-tier) + weekly lightweight (tier-aware) + MT weekly.

Monthly (1st Monday):
  - 4 per-tier FMP screener passes with correct thresholds
  - Flags existing stocks with degrading thesis
  - Haiku recommends adds/removes per tier caps
  - Speculative: max 1 add, 0 auto-removes — committee only

Weekly (Friday 16:30 ET):
  - LT basket: Free signals + tier-aware ADD/REMOVE (max 3 adds)
  - MT basket: congress buys + earnings catalysts + sector rotation + UW discoveries
  - MT auto-retirement: expired TTL, death cross, RSI overbought, catalyst passed
"""
import json
import os
from datetime import datetime, date

import requests
import yfinance as yf
import config
import anthropic

from basket.tier_criteria import (
    TIER_CRITERIA, REMOVE_CRITERIA, ADD_CRITERIA,
    COMMITTEE_ROUTING, PROMOTION_FLAGS,
    WEEKLY_MAX_ADDS, WEEKLY_MAX_SPEC_ADDS,
    MONTHLY_SCREENER_CAPS,
    MT_BASKET_SOURCES, MT_BASKET_RETIRE,
)

_FMP     = "https://financialmodelingprep.com/stable"
_TIMEOUT = 12

_THESIS_STATE_PATH = os.path.join(os.path.dirname(__file__), "thesis_state.json")

_THESIS_SECTORS = [
    "Technology", "Healthcare", "Energy", "Basic Materials",
    "Industrials", "Consumer Cyclical", "Financial Services",
    "Communication Services",
]

_ETHICAL_EXCLUSIONS = {"APP"}


# ── Thesis state helpers ──────────────────────────────────────────────────────

def _load_thesis_state() -> dict:
    try:
        with open(_THESIS_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _thesis_intact(symbol: str, thesis_state: dict) -> bool:
    """Return True if the speculative thesis is still intact (suppresses soft-removes)."""
    return thesis_state.get(symbol, {}).get("thesis_intact", True)


# ── FMP screener helpers ──────────────────────────────────────────────────────

def _fmp_screener(tier: str) -> list[dict]:
    """Run FMP screener with tier-appropriate thresholds."""
    if not config.FMP_API_KEY:
        return []
    crit = TIER_CRITERIA.get(tier, TIER_CRITERIA["mid_growth"])
    params = {
        "marketCapMoreThan":    int(crit.get("min_mcap", 500e6)),
        "revenueGrowthMoreThan": crit.get("min_rev_growth", 0.15),
        "country":  "US",
        "exchange": "NASDAQ,NYSE",
        "limit":    60,
        "apikey":   config.FMP_API_KEY,
    }
    if tier == "speculative":
        params["marketCapLessThan"] = int(crit.get("max_mcap", 25e9))
    try:
        resp = requests.get(f"{_FMP}/stock-screener", params=params, timeout=_TIMEOUT)
        return resp.json() if resp.status_code == 200 else []
    except Exception as e:
        print(f"  [Curation] Screener error ({tier}): {e}")
        return []


def _quick_fundamentals(symbol: str) -> dict:
    try:
        info = yf.Ticker(symbol).info or {}
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


def _flag_weak_existing(existing: list[str], cached_research: dict) -> list[dict]:
    """Flag basket stocks showing thesis degradation (revenue + EPS declining + below SMA200)."""
    flagged = []
    for sym in existing:
        cached = cached_research.get(sym, {})
        if not cached:
            continue
        fund = cached.get("fundamentals") or {}
        fin  = (cached.get("financial_data") or {}).get("fmp") or {}
        rev_growth = fund.get("revenue_growth") or fin.get("revenueGrowth")
        eps_growth = fund.get("eps_growth_yoy") or fin.get("epsgrowth")
        try:
            hist = yf.Ticker(sym).history(period="1y")
            if len(hist) < 50:
                continue
            price    = float(hist["Close"].iloc[-1])
            sma200   = float(hist["Close"].tail(200).mean()) if len(hist) >= 200 else None
            below200 = sma200 and price < sma200
        except Exception:
            below200 = False
        if (rev_growth is not None and rev_growth < -0.05 and
                eps_growth is not None and eps_growth < -0.10 and below200):
            flagged.append({
                "symbol":     sym,
                "rev_growth": rev_growth,
                "eps_growth": eps_growth,
                "reason":     f"revenue {rev_growth:.1%}, EPS {eps_growth:.1%}, below SMA200",
            })
    return flagged


# ── Monthly curation ──────────────────────────────────────────────────────────

def _claude_curate_tier(tier: str, candidates: list[dict],
                        flagged_existing: list[dict],
                        current_basket: list[str],
                        caps: dict) -> dict:
    """Ask Haiku to recommend basket changes for one tier."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    spec_note = (
        "\nSPECIAL RULE FOR SPECULATIVE: Only add if company has real revenue >$50M "
        "growing >40% YoY AND a Tier-1 strategic partner (NVIDIA/DoD/hyperscaler/Fortune 100). "
        "Do NOT recommend removing speculative tickers — flag them for committee review instead."
        if tier == "speculative" else ""
    )
    prompt = f"""Monthly basket review — {tier.upper()} tier.
Current basket ({len(current_basket)} stocks): {current_basket}
Max adds this month: {caps['max_add']} | Max removes: {caps['max_remove']}

NEW CANDIDATES (screened for {tier} criteria):
{json.dumps(candidates[:20], indent=2, default=str)}

EXISTING STOCKS FLAGGED FOR WEAKNESS:
{json.dumps(flagged_existing, indent=2, default=str)}

Kimmy's thesis: AI infrastructure, semiconductors, quantum computing, cybersecurity,
nuclear energy, defense tech, healthcare AI, e-commerce, financial infrastructure,
energy/commodities, robotics, space.{spec_note}

Respond ONLY in this JSON:
{{"add": ["TICK1"], "remove": ["TICK2"], "committee_flag": ["TICK3"], "reasoning": "brief"}}"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"  [Curation/{tier}] Claude error: {e}")
        return {"add": [], "remove": [], "committee_flag": [], "reasoning": "error"}


def run(existing_basket: list[str], cached_research: dict,
        config_module) -> tuple[list[str], list[str], str]:
    """
    Monthly deep curation — 4 per-tier screener passes.
    Returns (to_add, to_remove, reasoning).
    """
    from basket.manager import SECTOR_LIST, classify_ticker

    print("\n  [Curation] Running per-tier monthly screeners...")
    flagged = _flag_weak_existing(existing_basket, cached_research)
    flagged_syms = {f["symbol"] for f in flagged}

    all_adds:    list[str] = []
    all_removes: list[str] = []
    all_flags:   list[str] = []
    reasoning_parts: list[str] = []

    tiers_to_screen = ["mega", "large_growth", "mid_growth", "speculative"]
    for tier in tiers_to_screen:
        caps = MONTHLY_SCREENER_CAPS[tier]
        raw  = _fmp_screener(tier)

        candidates = []
        for c in raw:
            sym = (c.get("symbol") or "").upper()
            if (sym and sym not in existing_basket
                    and sym not in _ETHICAL_EXCLUSIONS
                    and sym.isalpha() and len(sym) <= 5):
                fund = _quick_fundamentals(sym)
                if fund.get("sector") in _THESIS_SECTORS:
                    candidates.append(fund)

        tier_flagged = [f for f in flagged if classify_ticker(f["symbol"]) == tier]
        print(f"  [Curation/{tier}] {len(candidates)} candidates, {len(tier_flagged)} flagged existing")

        if not candidates and not tier_flagged:
            continue

        result = _claude_curate_tier(tier, candidates, tier_flagged, existing_basket, caps)

        adds    = [s for s in result.get("add", [])
                   if s not in existing_basket and s not in _ETHICAL_EXCLUSIONS][:caps["max_add"]]
        removes = [s for s in result.get("remove", [])
                   if s in existing_basket][:caps["max_remove"]]
        flags   = result.get("committee_flag", [])

        # Speculative never auto-removed — move any removes to committee flags
        if tier == "speculative":
            all_flags.extend(removes)
            removes = []

        all_adds.extend(adds)
        all_removes.extend(removes)
        all_flags.extend(flags)
        if result.get("reasoning"):
            reasoning_parts.append(f"[{tier}] {result['reasoning'][:150]}")

    if all_flags:
        print(f"  [Curation] COMMITTEE FLAGS (require review): {all_flags}")

    reasoning = " | ".join(reasoning_parts)
    print(f"  [Curation] ADD: {all_adds} | REMOVE: {all_removes}")
    return all_adds, all_removes, reasoning


# ── Weekly curation ───────────────────────────────────────────────────────────

def _weekly_score_ticker(sym: str, cached_research: dict,
                         thesis_state: dict) -> dict:
    """Score one basket ticker against tier-aware ADD/REMOVE criteria."""
    from basket.manager import classify_ticker
    tier   = classify_ticker(sym)
    result = {
        "symbol":       sym,
        "tier":         tier,
        "add_signals":  [],
        "remove_hard":  [],
        "remove_soft":  [],
        "thesis_intact": _thesis_intact(sym, thesis_state) if tier == "speculative" else True,
    }
    try:
        tk   = yf.Ticker(sym)
        info = tk.info or {}
        hist = tk.history(period="6mo")
        if len(hist) < 20:
            return result

        price       = float(hist["Close"].iloc[-1])
        mktcap      = info.get("marketCap", 0) or 0
        rev_growth  = info.get("revenueGrowth")
        analyst_tgt = info.get("targetMeanPrice")
        rec_key     = info.get("recommendationKey", "").lower()
        hi_52w      = info.get("fiftyTwoWeekHigh") or price

        close   = hist["Close"]
        volume  = hist["Volume"]
        sma50   = float(close.tail(50).mean()) if len(close) >= 50 else None
        sma200  = float(close.tail(200).mean()) if len(close) >= 200 else None
        avg_vol = float(volume.tail(20).mean()) if len(volume) >= 20 else None
        avg_5d  = float(volume.tail(5).mean())  if len(volume) >= 5  else None
        vol_ratio   = (avg_5d / avg_vol) if (avg_vol and avg_vol > 0) else None
        death_cross = sma50 and sma200 and sma50 < sma200

        remove_crit = REMOVE_CRITERIA.get(tier, REMOVE_CRITERIA["mid_growth"])

        # ── Hard remove checks ────────────────────────────────────────────────
        hard_limits = {
            "mega":         200e9,
            "large_growth": 10e9,
            "mid_growth":   2e9,
            "speculative":  1e9,
        }
        if mktcap and mktcap < hard_limits.get(tier, 2e9):
            result["remove_hard"].append(
                f"market cap ${mktcap/1e9:.1f}B below {tier} hard floor")
        if info.get("financialCurrency") is None and mktcap == 0:
            result["remove_hard"].append("no financial data — possible delisting")

        # ── Soft remove checks (skipped entirely for speculative) ─────────────
        if tier != "speculative":
            if death_cross and rev_growth is not None and rev_growth < -0.05:
                result["remove_soft"].append(
                    f"death cross + revenue declining {rev_growth:.1%}")
            if hi_52w and price < hi_52w * 0.70 and death_cross:
                result["remove_soft"].append(
                    f"price {(price/hi_52w - 1):.1%} from 52w high + death cross")
            if rec_key in ("sell", "strong_sell", "underperform"):
                result["remove_soft"].append(f"analyst consensus: {rec_key}")
            if len(close) >= 60:
                p_12w = float(close.iloc[-60])
                pct   = abs((price - p_12w) / p_12w) if p_12w > 0 else 1
                cached = cached_research.get(sym, {})
                has_catalyst = bool((cached.get("research") or {}).get("snippets"))
                if pct < 0.05 and not has_catalyst:
                    result["remove_soft"].append(
                        f"zombie: ±{pct:.1%} over 12 weeks, no catalyst")

        # ── Add signals ───────────────────────────────────────────────────────
        if sma50 and price > sma50 * 1.20 and vol_ratio and vol_ratio > 1.5:
            result["add_signals"].append(
                f"breakout: {(price/sma50 - 1):.1%} above SMA50, vol {vol_ratio:.1f}x")
        if analyst_tgt and price > 0 and (analyst_tgt - price) / price > 0.25:
            result["add_signals"].append(
                f"analyst target ${analyst_tgt:.0f} = +{(analyst_tgt-price)/price:.1%} upside")
        min_rev = ADD_CRITERIA.get(tier, {}).get("min_rev_growth",
                  TIER_CRITERIA.get(tier, {}).get("min_rev_growth", 0.25))
        if rev_growth and rev_growth > min_rev:
            result["add_signals"].append(f"revenue growth {rev_growth:.1%}")
        cached = cached_research.get(sym, {})
        em = (cached.get("earnings_momentum") or {})
        if em.get("label") in ("strong_bullish", "bullish"):
            result["add_signals"].append(f"earnings momentum: {em['label']}")

    except Exception as e:
        result["error"] = str(e)

    return result


def _score_congress_weekly(existing_basket: list[str]) -> dict:
    try:
        from signals import congress as cong_mod
        return {sym: cong_mod.fetch(sym) for sym in existing_basket}
    except Exception:
        return {}


def _detect_tier_promotions(existing_basket: list[str]) -> list[dict]:
    """
    Quarterly flag — detect tickers that have outgrown their current tier.
    Returns list of promotion candidates for committee review.
    """
    from basket.manager import classify_ticker
    candidates = []
    for sym in existing_basket:
        current_tier = classify_ticker(sym)
        if current_tier not in ("mid_growth", "large_growth"):
            continue
        try:
            mcap = yf.Ticker(sym).info.get("marketCap", 0) or 0
        except Exception:
            continue
        if current_tier == "mid_growth":
            thresh = PROMOTION_FLAGS["mid_to_large"]["min_mcap"]
            if mcap >= thresh:
                candidates.append({
                    "symbol":       sym,
                    "current_tier": current_tier,
                    "target_tier":  "large_growth",
                    "mcap":         mcap,
                    "reason":       f"mcap ${mcap/1e9:.0f}B >= ${thresh/1e9:.0f}B threshold",
                })
        elif current_tier == "large_growth":
            thresh = PROMOTION_FLAGS["large_to_mega"]["min_mcap"]
            if mcap >= thresh:
                candidates.append({
                    "symbol":       sym,
                    "current_tier": current_tier,
                    "target_tier":  "mega",
                    "mcap":         mcap,
                    "reason":       f"mcap ${mcap/1e9:.0f}B >= ${thresh/1e9:.0f}B threshold",
                })
    return candidates


def _claude_weekly(scored: list[dict], congress_map: dict,
                   current_basket: list[str], cached_research: dict,
                   spec_flags: list[dict]) -> dict:
    """Ask Haiku for weekly basket recommendations (non-speculative only)."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    flagged_remove = []
    flagged_add    = []
    for s in scored:
        sym  = s["symbol"]
        tier = s["tier"]
        if tier == "speculative":
            continue  # speculative handled separately via committee
        if s.get("remove_hard"):
            flagged_remove.append({"symbol": sym, "tier": tier,
                                   "reason": "HARD: " + "; ".join(s["remove_hard"])})
        elif len(s.get("remove_soft", [])) >= REMOVE_CRITERIA.get(
                tier, {}).get("soft_count", 2):
            flagged_remove.append({"symbol": sym, "tier": tier,
                                   "reason": "SOFT: " + "; ".join(s["remove_soft"])})
        if s.get("add_signals") and sym not in current_basket:
            flagged_add.append({"symbol": sym, "tier": tier,
                                "add_signals": s["add_signals"]})

    cong_buys  = [s for s, v in congress_map.items() if v.get("net_signal") == "bullish"]
    cong_sells = [s for s, v in congress_map.items() if v.get("net_signal") == "bearish"]

    prompt = f"""Weekly basket review (Friday — tier-aware, conservative).
Only make changes when signals are clear. Speculative names are handled separately.

Current basket ({len(current_basket)} tickers): {current_basket}

FLAGGED FOR REMOVAL (scored by tier):
{json.dumps(flagged_remove, indent=2)}

CANDIDATES FOR ADDITION:
{json.dumps(flagged_add[:8], indent=2)}

CONGRESS NET BUYING: {cong_buys}
CONGRESS NET SELLING: {cong_sells}

Kimmy's thesis: AI infrastructure, semiconductors, quantum, cybersecurity,
nuclear energy, defense tech, healthcare AI, e-commerce, financial infrastructure,
energy/commodities, robotics, space. No surveillance/predatory platforms.

RULES:
- Hard remove flags → always REMOVE
- Soft remove (≥2 signals, tier-appropriate count) → REMOVE unless strong counter-thesis
- Congress buying a non-basket stock in thesis sectors → ADD
- Max {WEEKLY_MAX_ADDS} total adds (speculative excluded — committee handles those)
- Prefer HOLD over premature changes

JSON only: {{"add": ["TICK"], "remove": ["TICK"], "reasoning": "brief"}}"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"  [WeeklyCuration] Claude error: {e}")
        return {"add": [], "remove": [], "reasoning": "error"}


def run_weekly(existing_basket: list[str],
               cached_research: dict) -> tuple[list[str], list[str], str]:
    """
    Weekly basket review — Friday 16:30 ET.
    Tier-aware. Speculative names: flag only, never auto-remove.
    Returns (to_add, to_remove, reasoning).
    """
    from basket.manager import classify_ticker

    print("\n  [WeeklyCuration] Scoring basket tickers (tier-aware)...")
    thesis_state = _load_thesis_state()
    stocks = [s for s in existing_basket if not s.startswith("BTC")]
    scored = [_weekly_score_ticker(s, cached_research, thesis_state) for s in stocks]

    # ── Speculative protection — flag for committee, never auto-remove ────────
    spec_flags: list[dict] = []
    for s in scored:
        if s["tier"] != "speculative":
            continue
        sym = s["symbol"]
        intact = s["thesis_intact"]
        if s.get("remove_hard"):
            spec_flags.append({"symbol": sym, "type": "hard_remove",
                                "reason": "; ".join(s["remove_hard"]),
                                "thesis_intact": intact})
        elif s.get("remove_soft") and not intact:
            spec_flags.append({"symbol": sym, "type": "soft_remove",
                                "reason": "; ".join(s["remove_soft"]),
                                "thesis_intact": intact})
        # If thesis_intact=true → suppress soft signals entirely

    if spec_flags:
        print(f"  [WeeklyCuration] SPECULATIVE FLAGS (committee required): "
              f"{[f['symbol'] for f in spec_flags]}")

    # ── Tier promotion detection ───────────────────────────────────────────────
    promotions = _detect_tier_promotions(existing_basket)
    if promotions:
        print(f"  [WeeklyCuration] TIER PROMOTION CANDIDATES (committee): "
              f"{[p['symbol'] for p in promotions]}")

    # ── Congress signals ───────────────────────────────────────────────────────
    print("  [WeeklyCuration] Checking congress activity...")
    congress_map = _score_congress_weekly(existing_basket)

    # ── Haiku decides non-speculative changes ─────────────────────────────────
    print("  [WeeklyCuration] Asking Claude for weekly recommendations...")
    result = _claude_weekly(scored, congress_map, existing_basket,
                            cached_research, spec_flags)

    to_add    = [s for s in result.get("add", [])
                 if s not in existing_basket and s not in _ETHICAL_EXCLUSIONS
                 ][:WEEKLY_MAX_ADDS]
    to_remove = [s for s in result.get("remove", [])
                 if s in existing_basket
                 and classify_ticker(s) != "speculative"]  # never auto-remove speculative
    reasoning = result.get("reasoning", "")

    # Append committee flag summary to reasoning
    if spec_flags:
        flag_summary = "; ".join(f"{f['symbol']}({f['type']})" for f in spec_flags)
        reasoning += f" | COMMITTEE REQUIRED FOR: {flag_summary}"
    if promotions:
        promo_summary = "; ".join(
            f"{p['symbol']} {p['current_tier']}→{p['target_tier']}" for p in promotions)
        reasoning += f" | TIER PROMOTION CANDIDATES: {promo_summary}"

    print(f"  [WeeklyCuration] ADD: {to_add} | REMOVE: {to_remove}")
    print(f"  [WeeklyCuration] Reasoning: {reasoning[:300]}")
    return to_add, to_remove, reasoning


# ── MT basket weekly curation ─────────────────────────────────────────────────

def _mt_get_congress_buys(lt_basket: list[str]) -> list[dict]:
    """
    Congress buys not already in LT basket — primary MT source.
    Returns [{symbol, note, ttl_days}].
    """
    try:
        from signals import congress as cong_mod
        buys = cong_mod.get_recent_buys(days=config.MT_BASKET_CONGRESS_DAYS)
        result = []
        for sym in buys:
            if sym not in lt_basket and sym not in _ETHICAL_EXCLUSIONS:
                result.append({
                    "symbol":   sym,
                    "source":   "congress_buy",
                    "note":     f"Net congress buying (last {config.MT_BASKET_CONGRESS_DAYS}d)",
                    "ttl_days": config.MT_BASKET_CONGRESS_DAYS,
                })
        return result
    except Exception as e:
        print(f"  [MT Curation] Congress buys error: {e}")
        return []


def _mt_get_earnings_catalysts(lt_basket: list[str]) -> list[dict]:
    """
    Tickers (from LT basket or broader market) with earnings in 3-8 weeks
    + positive relative strength vs SPY + RSI 35-75 + above SMA20.
    Returns [{symbol, source, note, ttl_days, catalyst_date}].
    """
    if not config.FINNHUB_API_KEY:
        return []

    from datetime import timedelta
    today     = date.today()
    min_date  = today + timedelta(weeks=config.MT_BASKET_EARNINGS_WEEKS_MIN)
    max_date  = today + timedelta(weeks=config.MT_BASKET_EARNINGS_WEEKS_MAX)

    # Fetch earnings calendar from Finnhub for the window
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={
                "from":  min_date.isoformat(),
                "to":    max_date.isoformat(),
                "token": config.FINNHUB_API_KEY,
            },
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return []
        items = r.json().get("earningsCalendar", [])
    except Exception as e:
        print(f"  [MT Curation] Finnhub earnings error: {e}")
        return []

    # Broader thesis universe — tickers NOT in LT basket with upcoming catalysts
    lt_set = set(lt_basket)
    candidates = []
    seen = set()
    for e in items:
        sym = (e.get("symbol") or "").upper()
        if not sym or sym in seen or sym in _ETHICAL_EXCLUSIONS:
            continue
        if sym in lt_set:
            continue  # already in LT basket — scanned anyway
        if not sym.isalpha() or len(sym) > 5:
            continue
        seen.add(sym)
        edate_str = e.get("date", "")

        # Technical check: RSI 35-75, above SMA20, positive RS vs SPY
        try:
            tk   = yf.Ticker(sym)
            hist = tk.history(period="3mo")
            if len(hist) < 25:
                continue
            close  = hist["Close"]
            price  = float(close.iloc[-1])
            sma20  = float(close.tail(20).mean())
            if price < sma20:
                continue  # below SMA20 — not in uptrend

            # RSI (14-period)
            delta  = close.diff()
            gain   = delta.clip(lower=0).tail(14).mean()
            loss   = (-delta.clip(upper=0)).tail(14).mean()
            rsi    = 100 - (100 / (1 + gain / loss)) if loss > 0 else 100
            crit   = MT_BASKET_SOURCES["earnings_catalyst"]
            if not (crit["min_rsi"] <= rsi <= crit["max_rsi"]):
                continue

            # Relative strength vs SPY 4 weeks
            spy_hist = yf.Ticker("SPY").history(period="1mo")
            if len(spy_hist) >= 2 and len(hist) >= 20:
                rs_stock = (float(close.iloc[-1]) / float(close.iloc[-20]) - 1)
                rs_spy   = (float(spy_hist["Close"].iloc[-1]) /
                            float(spy_hist["Close"].iloc[0]) - 1)
                if rs_stock < rs_spy:
                    continue  # underperforming SPY — not a rotation leader

            analyst_tgt = tk.info.get("targetMeanPrice")
            upside = ((analyst_tgt - price) / price) if analyst_tgt and price else 0
            if upside < crit["min_upside"]:
                continue

            try:
                earnings_dt = date.fromisoformat(edate_str)
                days_to     = (earnings_dt - today).days
                ttl         = days_to + 14  # expires 14 days after earnings
            except Exception:
                ttl = 45

            candidates.append({
                "symbol":        sym,
                "source":        "earnings_catalyst",
                "note":          f"Earnings {edate_str} | RSI={rsi:.0f} | upside={upside:.1%}",
                "ttl_days":      ttl,
                "catalyst_date": edate_str,
            })
        except Exception:
            continue

    return candidates[:config.MT_BASKET_MAX_EARNINGS]


def _mt_get_sector_rotation(lt_basket: list[str]) -> list[dict]:
    """
    Find thesis sectors outperforming SPY over 4 weeks.
    Returns the top RS ticker per outperforming sector not already in LT basket.
    Returns [{symbol, source, note, ttl_days}].
    """
    # Representative sector tickers (ETFs or leaders) for 4-week RS calculation
    sector_proxies = {
        "semis":    "SOXX",
        "ai_tech":  "IGV",
        "cyber":    "CIBR",
        "defense":  "ITA",
        "nuclear":  "NLR",
        "fintech":  "FINX",
        "biotech":  "XBI",
        "energy":   "XLE",
        "robotics": "BOTZ",
        "quantum":  "QTUM",
    }

    # Thesis sector member lists — ONLY tickers NOT in LT basket
    # (LT tickers are already scanned; MT rotation adds genuinely new names)
    sector_members = {
        "semis":   ["QCOM", "INTC", "ON", "MCHP", "TXN", "SMCI", "SWKS", "WOLF", "MTSI", "SLAB", "MPWR", "ACLS", "ONTO", "COHU"],
        "ai_tech": ["DDOG", "MDB", "NET", "HCP", "GTLB", "CFLT", "BILL", "SNOW", "PLTR", "AI", "BBAI", "SOUN", "RXRX", "AMBA"],
        "cyber":   ["OKTA", "CYBR", "RPD", "QLYS", "TENB", "S", "VRNS", "ZTNO", "PANW", "CRWD"],
        "defense": ["HII", "DRS", "LDOS", "SAIC", "BAH", "MOOG", "TDG", "AXON", "KTOS", "RCAT"],
        "nuclear": ["SMR", "OKLO", "NNE", "UUUU", "LEU", "DNN", "UEC", "CCJ"],
        "fintech": ["SQ", "PYPL", "AFRM", "SOFI", "UPST", "LMND", "HOOD", "COIN", "NU", "DAVE"],
        "energy":  ["XOM", "CVX", "MPC", "PSX", "DVN", "HAL", "SLB", "OXY", "FANG", "AR"],
        "robotics":["PATH", "TER", "RRX", "FORM", "BRZE", "ACMR", "ISRG", "IRBT", "RMBS"],
        "biotech": ["MRNA", "BNTX", "REGN", "VRTX", "ALNY", "INCY", "SGEN", "RCKT", "BEAM", "EDIT"],
        "quantum": ["IONQ", "RGTI", "QUBT", "IBM", "QBTS"],
    }

    lt_set = set(lt_basket)
    candidates = []

    try:
        spy_hist = yf.Ticker("SPY").history(period="1mo")
        if len(spy_hist) < 10:
            return []
        spy_rs = (float(spy_hist["Close"].iloc[-1]) / float(spy_hist["Close"].iloc[0]) - 1)
    except Exception:
        return []

    min_rs_gap = MT_BASKET_SOURCES["sector_rotation"]["min_rs_vs_spy"]

    for sector, proxy in sector_proxies.items():
        try:
            proxy_hist = yf.Ticker(proxy).history(period="1mo")
            if len(proxy_hist) < 10:
                continue
            sector_rs = (float(proxy_hist["Close"].iloc[-1]) /
                         float(proxy_hist["Close"].iloc[0]) - 1)
            if sector_rs - spy_rs < min_rs_gap:
                continue  # sector not outperforming SPY by enough

            # Find the strongest individual ticker in this sector not in LT basket
            members_oot = [s for s in sector_members.get(sector, [])
                           if s not in lt_set and s not in _ETHICAL_EXCLUSIONS]
            if not members_oot:
                continue

            best_sym = None
            best_rs  = -999
            for sym in members_oot[:6]:
                try:
                    h = yf.Ticker(sym).history(period="1mo")
                    if len(h) < 10:
                        continue
                    rs = float(h["Close"].iloc[-1]) / float(h["Close"].iloc[0]) - 1
                    if rs > best_rs:
                        best_rs  = rs
                        best_sym = sym
                except Exception:
                    continue

            if best_sym:
                candidates.append({
                    "symbol":   best_sym,
                    "source":   "sector_rotation",
                    "note":     f"{sector} sector RS={sector_rs:.1%} vs SPY={spy_rs:.1%} | "
                                f"{best_sym} RS={best_rs:.1%}",
                    "ttl_days": MT_BASKET_SOURCES["sector_rotation"]["ttl_days"],
                })
        except Exception:
            continue

    return candidates[:config.MT_BASKET_MAX_SECTOR_ROT]


def _mt_get_uw_discoveries() -> list[dict]:
    """Pull queued UW discoveries from the pending file."""
    try:
        from basket.manager import get_uw_pending, clear_uw_pending
        pending = get_uw_pending()
        result = []
        for d in pending:
            sym = d.get("symbol", "").upper()
            if sym and sym not in _ETHICAL_EXCLUSIONS:
                result.append({
                    "symbol":   sym,
                    "source":   "uw_discovery",
                    "note":     f"UW call sweep ${d.get('premium', 0)/1e6:.1f}M",
                    "ttl_days": config.MT_BASKET_UW_DAYS,
                })
        clear_uw_pending()
        return result[:config.MT_BASKET_MAX_UW]
    except Exception as e:
        print(f"  [MT Curation] UW pending error: {e}")
        return []


def _mt_retire_existing(existing_meta: dict) -> list[str]:
    """
    Check existing MT basket tickers against retirement criteria.
    Returns list of symbols to remove.
    """
    today   = date.today()
    to_remove = []

    for sym, meta in existing_meta.items():
        # TTL expiry
        added_str = meta.get("added", "")
        ttl       = meta.get("ttl_days", 30)
        try:
            added_dt = date.fromisoformat(added_str[:10])
            if (today - added_dt).days > ttl:
                to_remove.append(sym)
                print(f"  [MT Retire] {sym} TTL expired ({ttl}d from {added_str[:10]})")
                continue
        except Exception:
            pass

        # Technical retirement (death cross, RSI overbought)
        try:
            hist = yf.Ticker(sym).history(period="6mo")
            if len(hist) < 50:
                continue
            close  = hist["Close"]
            sma50  = float(close.tail(50).mean())
            sma200 = float(close.tail(200).mean()) if len(close) >= 200 else None
            if sma200 and sma50 < sma200:
                to_remove.append(sym)
                print(f"  [MT Retire] {sym} death cross confirmed")
                continue
            delta = close.diff()
            gain  = delta.clip(lower=0).tail(14).mean()
            loss  = (-delta.clip(upper=0)).tail(14).mean()
            rsi   = 100 - (100 / (1 + gain / loss)) if loss > 0 else 100
            if rsi >= MT_BASKET_RETIRE["rsi_overbought"]:
                to_remove.append(sym)
                print(f"  [MT Retire] {sym} RSI overbought ({rsi:.0f})")
        except Exception:
            pass

        # Catalyst passed check
        catalyst_date_str = meta.get("catalyst_date", "")
        if catalyst_date_str:
            try:
                cat_dt = date.fromisoformat(catalyst_date_str[:10])
                if (today - cat_dt).days > MT_BASKET_RETIRE["catalyst_passed_days"]:
                    to_remove.append(sym)
                    print(f"  [MT Retire] {sym} catalyst passed ({catalyst_date_str})")
            except Exception:
                pass

    return list(set(to_remove))


def _mt_haiku_finalize(current: list[str], candidates: list[dict],
                       to_retire: list[str], max_adds: int | None = None) -> dict:
    """Ask Haiku to finalize the MT basket from the scored candidate pool."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    effective_max = max_adds if max_adds is not None else config.MT_BASKET_WEEKLY_MAX_ADDS

    cand_summary = [
        {"symbol": c["symbol"], "source": c["source"], "note": c["note"]}
        for c in candidates
    ]
    prompt = f"""Medium-term catalyst basket weekly review.

Current MT basket ({len(current)} tickers, max {config.MT_BASKET_MAX}): {current}
Scheduled retirements (TTL/technical): {to_retire}
Max new adds this week: {effective_max}

NEW CANDIDATES (pre-screened):
{json.dumps(cand_summary, indent=2)}

Source caps: congress={config.MT_BASKET_MAX_CONGRESS}, earnings={config.MT_BASKET_MAX_EARNINGS}, \
sector_rotation={config.MT_BASKET_MAX_SECTOR_ROT}, uw_discovery={config.MT_BASKET_MAX_UW}

RULES:
- congress_buy: auto-approve (congressional info-edge, event-driven)
- earnings_catalyst: approve if RSI/RS criteria passed (pre-screened)
- sector_rotation: approve if sector is in Kimmy's thesis universe
- uw_discovery: approve if ethical, thesis-adjacent
- Do NOT exceed source caps or total MT cap of {config.MT_BASKET_MAX}
- Prefer quality over quantity — empty slots are fine

Respond ONLY in JSON: {{"add": ["TICK"], "remove_extra": ["TICK"], "reasoning": "brief"}}"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"  [MT Curation] Haiku error: {e}")
        # Fallback: auto-approve all congress buys, top-N earnings
        auto = [c["symbol"] for c in candidates if c["source"] == "congress_buy"]
        return {"add": auto[:effective_max], "remove_extra": [], "reasoning": "fallback"}


def run_mt_weekly(lt_basket: list[str]) -> tuple[list[str], dict, str]:
    """
    Weekly MT basket curation — runs Friday alongside LT basket review.

    Sources:
      1. Congress buys (within 45 days, not in LT basket) — auto-qualify
      2. Earnings catalyst plays (3-8 weeks out, above SMA20, RSI 35-75)
      3. Sector rotation leaders (top RS vs SPY in thesis sectors)
      4. UW out-of-basket discoveries (from queued pending file)

    Returns (mt_tickers: list, mt_metadata: dict, reasoning: str).
    """
    from basket.manager import load_mt, load_mt_metadata

    print("\n  [MT Curation] Weekly medium-term basket refresh...")

    existing_tickers  = load_mt()
    existing_meta     = load_mt_metadata()

    # Step 1: Retire expired / deteriorated positions
    to_retire = _mt_retire_existing(existing_meta)

    # Step 2: Source new candidates from all four pipelines
    print("  [MT Curation] Sourcing: congress buys...")
    congress_cands = _mt_get_congress_buys(lt_basket)

    print("  [MT Curation] Sourcing: earnings catalysts...")
    earnings_cands = _mt_get_earnings_catalysts(lt_basket)

    print("  [MT Curation] Sourcing: sector rotation...")
    sector_cands   = _mt_get_sector_rotation(lt_basket)

    print("  [MT Curation] Sourcing: UW discoveries...")
    uw_cands       = _mt_get_uw_discoveries()

    all_candidates = congress_cands + earnings_cands + sector_cands + uw_cands
    already_in_mt  = set(existing_tickers)
    new_candidates = [c for c in all_candidates if c["symbol"] not in already_in_mt]

    print(f"  [MT Curation] {len(new_candidates)} new candidates "
          f"(congress={len(congress_cands)} earnings={len(earnings_cands)} "
          f"sector={len(sector_cands)} uw={len(uw_cands)})")

    # Step 3: Haiku finalizes adds
    # Use bootstrap cap when basket is below 50% capacity — fills faster on first runs
    active = [s for s in existing_tickers if s not in to_retire]
    bootstrap = len(active) < config.MT_BASKET_MAX // 2
    max_adds  = config.MT_BASKET_BOOTSTRAP_ADDS if bootstrap else config.MT_BASKET_WEEKLY_MAX_ADDS
    if bootstrap:
        print(f"  [MT Curation] Bootstrap mode — basket at {len(active)}/{config.MT_BASKET_MAX}, allowing up to {max_adds} adds")
    result = _mt_haiku_finalize(active, new_candidates, to_retire, max_adds=max_adds)

    approved_adds  = [c for c in all_candidates
                      if c["symbol"] in result.get("add", [])]
    extra_removes  = result.get("remove_extra", [])
    reasoning      = result.get("reasoning", "")

    # Build final ticker list and metadata
    final_tickers  = [s for s in active if s not in extra_removes]
    final_meta     = {s: existing_meta[s] for s in final_tickers if s in existing_meta}

    today_str = date.today().isoformat()
    for c in approved_adds:
        sym = c["symbol"]
        if sym not in set(final_tickers):
            final_tickers.append(sym)
        exp = date.today()
        from datetime import timedelta as _td
        exp_str = (date.today() + _td(days=c.get("ttl_days", 30))).isoformat()
        final_meta[sym] = {
            "source":        c["source"],
            "added":         today_str,
            "expires":       exp_str,
            "note":          c.get("note", ""),
            "catalyst_date": c.get("catalyst_date", ""),
        }

    # Enforce hard cap
    if len(final_tickers) > config.MT_BASKET_MAX:
        # Trim: keep congress > earnings > sector > uw, shortest TTL last
        def _priority(sym):
            src = final_meta.get(sym, {}).get("source", "uw_discovery")
            return {"congress_buy": 0, "earnings_catalyst": 1,
                    "sector_rotation": 2, "uw_discovery": 3}.get(src, 4)
        final_tickers.sort(key=_priority)
        final_tickers = final_tickers[:config.MT_BASKET_MAX]
        final_meta = {s: final_meta[s] for s in final_tickers if s in final_meta}

    adds_log    = [c["symbol"] for c in approved_adds]
    removes_log = to_retire + extra_removes
    print(f"  [MT Curation] Final: {len(final_tickers)} tickers | "
          f"Added: {adds_log} | Removed: {removes_log}")
    print(f"  [MT Curation] Reasoning: {reasoning[:200]}")

    return final_tickers, final_meta, reasoning
