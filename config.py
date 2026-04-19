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

# --- Telegram ---
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

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
MAX_POSITION_PCT     = 5.0   # max % of portfolio per position
MAX_POSITIONS        = 20    # max open positions
STOP_LOSS_PCT        = 7.0   # auto-sell if down this %
TAKE_PROFIT_PCT      = 20.0  # auto-sell if up this %
MAX_OPTIONS_PCT      = 25.0  # max % of portfolio in options
MAX_CRYPTO_PCT       = 30.0  # max % of portfolio in crypto
MIN_CONFIDENCE       = 7     # minimum Claude confidence (1-10) to trade
TRADE_CUTOFF_HOUR    = 15    # no new trades after 3 PM ET
TRADE_CUTOFF_MINUTE  = 30

# --- Scheduler ---
RUN_HOUR   = 9
RUN_MINUTE = 35  # 9:35 AM ET, 5 min after market open

# Summary schedule (ET)
PREMARKET_SUMMARY_HOUR   = 9
PREMARKET_SUMMARY_MINUTE = 0   # 9:00 AM — 30 min before open
CLOSE_SUMMARY_HOUR       = 16
CLOSE_SUMMARY_MINUTE     = 5   # 4:05 PM — just after close

# --- Hard Entry Criteria (ALL must pass or trade is blocked) ---
CRITERIA_RSI_MIN        = 40    # RSI floor — not in panic/crash
CRITERIA_RSI_MAX        = 68    # RSI ceiling — not overbought
CRITERIA_PRICE_ABOVE_SMA50 = True   # price must be above 50-day SMA
CRITERIA_MACD_NOT_BEARISH  = True   # MACD must not be in bearish crossover
CRITERIA_EPS_GROWTH_MIN    = 0.05   # EPS growth YoY >= 5%
CRITERIA_REVENUE_GROWTH_MIN= 0.03   # Revenue growth YoY >= 3%
CRITERIA_PROFIT_MARGIN_MIN = 0.08   # Net profit margin >= 8%
CRITERIA_PE_MAX            = 55     # P/E ratio <= 55 (not wildly overvalued)
CRITERIA_SENTIMENT_NOT_NEG = True   # sentiment must not be negative

# --- Hard Exit Criteria (any ONE triggers sell) ---
EXIT_RSI_OVERBOUGHT     = 75    # RSI above this -> trim position
EXIT_MACD_BEARISH_CROSS = True  # MACD bearish crossover on open position -> sell

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
