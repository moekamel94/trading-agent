import os
from dotenv import load_dotenv

load_dotenv()

# --- API Keys ---
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# --- Watchlist ---
STOCK_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
    "META", "TSLA", "JPM", "SPY", "QQQ",
]

CRYPTO_WATCHLIST = [
    "BTC/USD", "ETH/USD", "SOL/USD",
]

# --- Risk Parameters ---
MAX_POSITION_PCT     = 5.0   # max % of portfolio per position
MAX_POSITIONS        = 20    # max open positions
STOP_LOSS_PCT        = 7.0   # auto-sell if down this %
TAKE_PROFIT_PCT      = 20.0  # auto-sell if up this %
MAX_OPTIONS_PCT      = 25.0  # max % of portfolio in options
MAX_CRYPTO_PCT       = 30.0  # max % of portfolio in crypto
MIN_CONFIDENCE       = 6     # minimum Claude confidence (1-10) to trade
TRADE_CUTOFF_HOUR    = 15    # no new trades after 3 PM ET
TRADE_CUTOFF_MINUTE  = 30

# --- Scheduler ---
RUN_HOUR   = 9
RUN_MINUTE = 35  # 9:35 AM ET, 5 min after market open

# --- Options ---
OPTION_DAYS_TO_EXPIRY = 30  # target ~30 days out
