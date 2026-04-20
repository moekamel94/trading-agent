import os
from dotenv import load_dotenv

load_dotenv()

# --- API Keys ---
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
NEWDATA_API_KEY   = os.getenv("NEWDATA_API_KEY", "")
SERPAPI_KEY       = os.getenv("SERPAPI_KEY", "")
TAVILY_API_KEY    = os.getenv("TAVILY_API_KEY", "")
EXA_API_KEY       = os.getenv("EXA_API_KEY", "")
SERPER_API_KEY    = os.getenv("SERPER_API_KEY", "")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")
FINNHUB_API_KEY   = os.getenv("FINNHUB_API_KEY", "")
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY", "")
TWELVE_DATA_KEY   = os.getenv("TWELVE_DATA_KEY", "")
FMP_API_KEY       = os.getenv("FMP_API_KEY", "")
POLYGON_API_KEY   = os.getenv("POLYGON_API_KEY", "")


# --- Discord ---
DISCORD_TOKEN            = os.getenv("DISCORD_TOKEN", "")
_discord_ch = os.getenv("DISCORD_ALERT_CHANNEL_ID", "0")
DISCORD_ALERT_CHANNEL_ID = int(_discord_ch) if _discord_ch.isdigit() else 0
DISCORD_WEBHOOK_URL      = os.getenv("DISCORD_WEBHOOK_URL", "")  # for standalone cycles

# --- Watchlist ---
STOCK_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
    "META", "TSLA", "JPM", "SPY", "QQQ",
]

CRYPTO_WATCHLIST = [
    "BTC/USD",
]

# --- Risk Parameters ---
MAX_POSITION_PCT     = 8.0   # hard cap per position
MAX_POSITIONS        = 20    # max open positions
TAKE_PROFIT_PCT      = 0     # no fixed take-profit — let winners run
MAX_OPTIONS_PCT      = 20.0  # max % of portfolio in options
MAX_CRYPTO_PCT       = 20.0  # max % of portfolio in crypto
MIN_CONFIDENCE       = 6     # minimum Claude confidence (1-10) to trade
TRADE_CUTOFF_HOUR    = 15    # no new trades after 3 PM ET
TRADE_CUTOFF_MINUTE  = 30
MAX_SECTOR_PCT       = 25.0  # max % of portfolio in any single sector
MAX_SPECULATIVE_POSITIONS = 5
MAX_SPECULATIVE_PCT  = 10.0  # max % in speculative/moonshot tier

# --- Scheduler ---
# Two cycles per day: open (catch overnight news/gaps) + near-close (daily bars nearly complete)
RUN_HOUR        = 9
RUN_MINUTE      = 35   # 9:35 AM ET — market open
AFTERNOON_HOUR  = 15
AFTERNOON_MINUTE = 30  # 3:30 PM ET — daily bars ~97% complete, best signal quality

# Summary schedule (ET)
PREMARKET_SUMMARY_HOUR   = 9
PREMARKET_SUMMARY_MINUTE = 0   # 9:00 AM — 30 min before open
CLOSE_SUMMARY_HOUR       = 16
CLOSE_SUMMARY_MINUTE     = 5   # 4:05 PM — just after close

# --- Tier-specific entry criteria ---
MID_GROWTH_FUNDAMENTALS_NEEDED   = 2      # relaxed from 3
MID_GROWTH_MOMENTUM_NEEDED       = 2
MID_GROWTH_TECHNICAL_NEEDED      = 1      # just avoid confirmed death+downtrend
MID_GROWTH_PE_MAX                = 200    # high-growth stocks can carry high P/E
MID_GROWTH_MARGIN_MIN            = -5.0   # thin/slightly negative margin OK
MID_GROWTH_PRELIM_MIN            = 1      # lower prelim gate threshold
MID_GROWTH_DEAD_MONEY_REV_EXEMPT = 0.25  # skip dead money if revenue growing >25%

SPEC_GROWTH_SCORE_MIN         = 35    # minimum future_growth score for speculative
SPEC_ANALYST_UPSIDE_MIN       = 20    # % upside to analyst target required
SPEC_REVENUE_GROWTH_MIN       = 0.10  # 10% revenue growth min (or N/A = pass)
SPEC_THESIS_SIGNALS_NEEDED    = 2     # need 2 of 5 thesis signals
SPEC_THESIS_HOLD_MONTHS       = 18    # flag for re-evaluation after this many months
SPEC_TRAILING_STOP_MIN_GAIN   = 40.0  # trailing stop activates only above +40%
SPEC_TRAILING_STOP_1M_DROP    = -15.0 # trigger only on -15% 1M reversal (not -8%)

MID_TRAILING_STOP_MIN_GAIN    = 25.0
MID_TRAILING_STOP_1M_DROP     = -10.0

# --- Entry Criteria: Hard Blocks (absolute stops, very few) ---
CRITERIA_RSI_MIN         = 25   # true panic extreme only
CRITERIA_RSI_MAX         = 78   # true overbought extreme only
CRITERIA_FG_PANIC        = 15   # Fear & Greed below this = no new buys
CRITERIA_EARNINGS_DAYS   = 3    # skip if earnings within this many days

# --- Entry Criteria: Scoring Thresholds ---
CRITERIA_FUNDAMENTALS_NEEDED  = 3    # need 3 of 5 fundamental checks
CRITERIA_MOMENTUM_NEEDED      = 2    # need 2 of 4 momentum checks
CRITERIA_TECHNICAL_NEEDED     = 2    # need 2 of 3 technical checks
CRITERIA_EPS_GROWTH_MIN       = 0.0  # any positive EPS growth
CRITERIA_REVENUE_GROWTH_MIN   = 0.0  # any positive revenue growth
CRITERIA_PROFIT_MARGIN_MIN    = 0.05 # 5% margin
CRITERIA_PE_MAX               = 80   # covers growth stocks

# --- Tier Classification (all basket tickers) ---
TICKER_TIERS = {
    # ── Mega caps ───────────────────────────────────────────────────────────
    "AAPL": "mega", "MSFT": "mega", "GOOGL": "mega", "META": "mega",
    "AMZN": "mega", "NVDA": "mega", "TSLA": "mega",
    # ── Large growth (established thesis leaders) ────────────────────────
    "AMD": "large_growth", "AVGO": "large_growth", "ARM": "large_growth",
    "MRVL": "large_growth", "TSM": "large_growth",
    "PLTR": "large_growth", "CRM": "large_growth", "NOW": "large_growth",
    "ORCL": "large_growth", "AI": "large_growth",
    "CRWD": "large_growth", "PANW": "large_growth", "ZS": "large_growth",
    "FTNT": "large_growth", "S": "large_growth",
    "LMT": "large_growth", "RTX": "large_growth", "NOC": "large_growth",
    "GD": "large_growth", "AXON": "large_growth",
    "ISRG": "large_growth", "ETN": "large_growth",
    "COIN": "large_growth", "ANET": "large_growth",
    # ── Mid growth (high-conviction, earlier in curve) ───────────────────
    "AMAT": "mid_growth", "LRCX": "mid_growth", "KLAC": "mid_growth",
    "MU": "mid_growth", "IBM": "mid_growth",
    "RGTI": "mid_growth",
    "CCJ": "mid_growth", "CEG": "mid_growth",
    "BWXT": "mid_growth", "KTOS": "mid_growth",
    "ENPH": "mid_growth", "FSLR": "mid_growth",
    "ABB": "mid_growth", "VRT": "mid_growth",
    "HOOD": "mid_growth", "DUOL": "mid_growth",
    "MELI": "mid_growth", "NU": "mid_growth",
    "SYM": "mid_growth", "CELH": "mid_growth", "CAVA": "mid_growth",
    "RKLB": "mid_growth", "RDDT": "mid_growth",
    # ── Speculative / Moonshots (asymmetric 10-year bets) ────────────────
    "ASTS": "speculative",            # satellite-to-phone internet (2.8B existing subscribers)
    "OKLO": "speculative",            # nuclear microreactors for AI data centers (Sam Altman)
    "SMR": "speculative",             # small modular reactors
    "JOBY": "speculative",            # eVTOL air taxi — 80% through FAA Stage 4, Toyota backed
    "RXRX": "speculative",            # AI drug discovery — $12B Roche milestones, NVIDIA platform
    "IONQ": "speculative",            # quantum computing — networked qubits, $130M revenue +200% YoY
}

# Allocation by tier × confidence (risk manager enforces, not Claude)
TIER_ALLOC = {
    "mega":         {6: 4.0, 7: 5.0, 8: 6.0,  9: 7.0,  10: 8.0},
    "large_growth": {6: 3.0, 7: 4.0, 8: 5.0,  9: 6.0,  10: 7.0},
    "mid_growth":   {6: 2.0, 7: 3.0, 8: 4.0,  9: 4.5,  10: 5.0},
    "speculative":  {6: 1.0, 7: 1.5, 8: 2.0,  9: 2.5,  10: 3.0},
}

# Stop-loss by tier (speculative needs wider stop — don't shake out on vol)
STOP_LOSS_BY_TIER = {
    "mega":         6.0,
    "large_growth": 8.0,
    "mid_growth":   10.0,
    "speculative":  15.0,
}
STOP_LOSS_PCT = 8.0  # fallback for tickers not in TICKER_TIERS

# Sector map — used for concentration limits (MAX_SECTOR_PCT)
SECTOR_MAP = {
    "MSFT": "ai_software", "GOOGL": "ai_software", "META": "ai_software",
    "AMZN": "ai_software", "ORCL": "ai_software", "PLTR": "ai_software",
    "CRM": "ai_software",  "NOW": "ai_software",  "AI": "ai_software",
    "NVDA": "semis", "AMD": "semis",  "AVGO": "semis", "AMAT": "semis",
    "LRCX": "semis", "KLAC": "semis", "MU": "semis",   "ARM": "semis",
    "MRVL": "semis", "TSM": "semis",
    "IONQ": "quantum", "RGTI": "quantum", "IBM": "quantum",
    "CRWD": "cyber", "PANW": "cyber", "ZS": "cyber", "FTNT": "cyber", "S": "cyber",
    "RKLB": "space", "ASTS": "space",
    "CCJ": "nuclear", "OKLO": "nuclear", "SMR": "nuclear", "CEG": "nuclear",
    "LMT": "defense", "RTX": "defense", "NOC": "defense", "GD": "defense",
    "KTOS": "defense", "AXON": "defense", "BWXT": "defense",
    "ENPH": "clean_energy", "FSLR": "clean_energy",
    "ABB": "robotics", "ETN": "robotics", "ISRG": "robotics", "SYM": "robotics",
    "ANET": "ai_infra", "VRT": "ai_infra",
    "HOOD": "fintech", "COIN": "fintech", "MELI": "fintech", "NU": "fintech",
    "DUOL": "consumer_tech", "RDDT": "consumer_tech",
    "JOBY": "evtol",
    "RXRX": "biotech",
    "CELH": "consumer_goods", "CAVA": "consumer_goods",
    "AAPL": "mega_tech", "TSLA": "mega_tech",
}

# --- Position Sizing (legacy bonus modifiers) ---
CONGRESS_BONUS_PCT   = 2.0
INSIDER_BONUS_PCT    = 1.0

# --- Exit Criteria ---
STOP_LOSS_PCT        = 8.0    # wider for position trading (was 7%)
EXIT_RSI_OVERBOUGHT  = 80     # true overbought (was 75)
EXIT_MACD_BEARISH_CROSS = True
DEAD_MONEY_DAYS      = 90     # exit if held this long with < profit below
DEAD_MONEY_MIN_PCT   = 3.0    # minimum profit after DEAD_MONEY_DAYS

# Trailing stop: if a position is up TRAILING_STOP_MIN_GAIN% or more and the
# 1-month return drops below TRAILING_STOP_1M_DROP%, it means the stock has
# reversed — protect the gain.
TRAILING_STOP_MIN_GAIN = 20.0  # only activate trailing stop above this profit
TRAILING_STOP_1M_DROP  = -8.0  # sell if 1M return goes this negative while winning

# --- Quality Filters (Risk Officer) ---
MIN_STOCK_PRICE    = 3.0   # skip stocks below $3 — penny stock / liquidity risk
MIN_VOLUME_RATIO   = 1.0   # require at least average daily volume (was 0.8)

# --- BTC-Specific Criteria ---
BTC_RSI_MIN          = 30
BTC_RSI_MAX          = 75
BTC_STOP_LOSS_PCT    = 12.0   # crypto needs wider stop
BTC_FG_PANIC         = 20     # crypto Fear & Greed (use main F&G as proxy)
BTC_VIX_MAX          = 38     # don't buy BTC during equity market panic

# --- Basket ---
BASKET_REFRESH_HOUR   = 8
BASKET_REFRESH_MINUTE = 0   # First Monday of each month, 8:00 AM ET

# --- Options ---
OPTION_DAYS_TO_EXPIRY = 30  # target ~30 days out

# S&P 500 tickers eligible for options trading.
# Fetched from Wikipedia at startup; falls back to this list if unavailable.
SP500_FALLBACK = {
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","TSLA","BRK.B","UNH",
    "JPM","XOM","LLY","V","AVGO","MA","PG","JNJ","HD","MRK","COST","ABBV",
    "CVX","CRM","BAC","NFLX","AMD","PEP","TMO","ORCL","KO","WMT","CSCO",
    "ACN","MCD","ABT","WFC","DHR","LIN","TXN","PM","ADBE","NEE","CAT","DIS",
    "INTU","IBM","GE","HON","QCOM","RTX","SPGI","UPS","GS","BKNG","ISRG",
    "AMGN","ELV","PLD","LOW","SYK","VRTX","MDT","BLK","AXP","TJX","ADI",
    "C","SCHW","ZTS","MMC","CB","CME","MO","SO","DUK","ITW","BSX","AON",
    "NOC","ETN","PGR","ICE","HCA","FI","CL","SPY","QQQ",
}


def get_sp500_tickers() -> set[str]:
    try:
        import pandas as pd
        tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        tickers = set(tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist())
        return tickers if len(tickers) > 400 else SP500_FALLBACK
    except Exception:
        return SP500_FALLBACK
