"""
Monthly basket curation — runs inside run_monthly_research().

Steps:
  1. Screen market for new high-growth candidates (FMP stock screener)
  2. Flag existing basket stocks with degrading thesis
  3. Ask Claude to recommend up to 5 adds and any removes
  4. Apply changes and notify Discord
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
