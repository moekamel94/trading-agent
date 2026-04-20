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

# --- Watchlist ---
STOCK_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
    "META", "TSLA", "JPM", "SPY", "QQQ",
]

CRYPTO_WATCHLIST = [
    "BTC/USD",
]

# --- Risk Parameters ---
MAX_POSITION_PCT     = 8.0   # max % of portfolio per position (was 5%)
MAX_POSITIONS        = 15    # max open positions (was 20, fewer/higher quality)
TAKE_PROFIT_PCT      = 0     # no fixed take-profit — let winners run
MAX_OPTIONS_PCT      = 20.0  # max % of portfolio in options
MAX_CRYPTO_PCT       = 20.0  # max % of portfolio in crypto
MIN_CONFIDENCE       = 7     # minimum Claude confidence (1-10) to trade
TRADE_CUTOFF_HOUR    = 15    # no new trades after 3 PM ET
TRADE_CUTOFF_MINUTE  = 30

# --- Scheduler ---
RUN_HOUR   = 9
RUN_MINUTE = 30  # 9:30 AM ET, at market open

# Summary schedule (ET)
PREMARKET_SUMMARY_HOUR   = 9
PREMARKET_SUMMARY_MINUTE = 0   # 9:00 AM — 30 min before open
CLOSE_SUMMARY_HOUR       = 16
CLOSE_SUMMARY_MINUTE     = 5   # 4:05 PM — just after close

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

# --- Position Sizing (updated) ---
CONF_ALLOC = {7: 4.0, 8: 5.0, 9: 6.0, 10: 8.0}
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
BASKET_REFRESH_MINUTE = 0   # Monday 8:00 AM ET

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
