import os
from dotenv import load_dotenv

load_dotenv()

# --- API Keys ---
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
NEWDATA_API_KEY   = os.getenv("NEWDATA_API_KEY", "")
TAVILY_API_KEY    = os.getenv("TAVILY_API_KEY", "")
EXA_API_KEY       = os.getenv("EXA_API_KEY", "")
SERPER_API_KEY    = os.getenv("SERPER_API_KEY", "")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")
FINNHUB_API_KEY   = os.getenv("FINNHUB_API_KEY", "")
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY", "")
TWELVE_DATA_KEY   = os.getenv("TWELVE_DATA_KEY", "")
FMP_API_KEY       = os.getenv("FMP_API_KEY", "")
POLYGON_API_KEY         = os.getenv("POLYGON_API_KEY", "")
UNUSUAL_WHALES_API_KEY  = os.getenv("UNUSUAL_WHALES_API_KEY", "")

# Unusual Whales shadow mode: True for first 90 days — signal is logged but +0.5 bonus NOT applied.
# Set to False after shadow validation (hit rate ≥55% at 20 days on bullish sweeps in our universe).
# Bearish sweep penalty (-0.5) is ALWAYS live regardless of this flag.
UNUSUAL_WHALES_SHADOW_MODE = True

# Shadow mode auto-graduation thresholds
UW_SHADOW_MIN_SIGNALS   = 20    # need ≥20 bullish sweep signals tracked before graduating
UW_SHADOW_MIN_HIT_RATE  = 55.0  # hit rate on 20-day forward returns must be ≥55%

# Consecutive bearish sweep escalation: after N cycles in a row → escalate to SELL
UW_CONSECUTIVE_BEARISH_EXIT = 3

# --- Cache Auto-Warmup (daily scan) ---
AUTO_WARMUP_MAX   = 8   # max symbols to warm per daily cycle (~32s added latency at 4s/symbol)
CACHE_STALE_DAYS  = 14  # re-warm if cache entry is older than this many days

# --- Medium-Term Basket ---
MT_BASKET_MAX              = 20   # hard cap on MT basket size
MT_BASKET_CONGRESS_DAYS    = 45   # congress buy stays in MT for this many days
MT_BASKET_UW_DAYS          = 30   # UW discovery stays in MT for this many days
MT_BASKET_EARNINGS_WEEKS_MIN = 3  # catalyst must be ≥3 weeks out to add
MT_BASKET_EARNINGS_WEEKS_MAX = 8  # catalyst must be ≤8 weeks out to add
MT_BASKET_MAX_CONGRESS     = 8    # max congress buy slots in MT basket
MT_BASKET_MAX_EARNINGS     = 15   # max earnings catalyst slots (raised for broader sourcing)
MT_BASKET_MAX_SECTOR_ROT   = 8    # max sector rotation slots
MT_BASKET_MAX_UW           = 5    # max UW discovery slots
MT_BASKET_WEEKLY_MAX_ADDS  = 3    # steady-state weekly cap (overridden when basket is sparse — see run_mt_weekly)
MT_BASKET_BOOTSTRAP_ADDS   = 15   # bootstrap cap when basket is below 50% capacity


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

# --- Risk Parameters ---
MAX_POSITION_PCT     = 8.0   # hard cap per position (long-term); medium-term capped at 6%
MAX_POSITIONS        = 20    # total max (15 long-term + 5 medium-term)
MAX_POSITIONS_LONG_TERM   = 15   # long-term sleeve capacity
MAX_POSITIONS_MEDIUM_TERM = 5    # medium-term sleeve capacity
TAKE_PROFIT_PCT      = 0     # no fixed take-profit — let winners run
MAX_OPTIONS_PCT      = 20.0  # max % of portfolio in options
MIN_CONFIDENCE       = 6     # minimum Claude confidence (1-10) to trade
TRADE_CUTOFF_HOUR    = 15    # no new trades after 3 PM ET
TRADE_CUTOFF_MINUTE  = 30
MAX_SECTOR_PCT       = 25.0  # max % of portfolio in any single sector
MAX_SPECULATIVE_POSITIONS = 5
MAX_SPECULATIVE_PCT  = 10.0  # max % in speculative/moonshot tier
MIN_SPEC_CONFIDENCE  = 7     # minimum confidence to enter a speculative position (no conf-6 entries)

# --- Macro Regime Routing ---
# Minimum sector weight (from macro_regime.sector_weights) required to open new positions.
# Sectors below this threshold receive BUCKET only — no new BUYs.
REGIME_MIN_SECTOR_WEIGHT = 0.55
# Number of top-weight sectors the system concentrates in per regime cycle.
REGIME_TOP_N_SECTORS = 3

# --- 70/30 Portfolio Structure ---
# Long-term bucket: mega + speculative + large_growth (6+ months)
# Medium-term bucket: catalyst/swing plays from large_growth or mid_growth (3-8 weeks)
LONG_TERM_PCT_CAP         = 72.0  # long-term bucket hard cap with 2% buffer
MEDIUM_TERM_PCT_CAP       = 30.0  # medium-term bucket hard cap — matches the 30% design intent exactly
LONG_TERM_SECTOR_CAP_PCT  = 22.0  # max any sector within long-term sleeve (committee: tightened from 28%)

# Medium-term specific risk parameters
MEDIUM_TERM_MAX_POSITION_PCT   = 6.0   # 30% sleeve / 5 positions
MEDIUM_TERM_STOP_LOSS_PCT      = 12.0  # 10-15% range; tighter than long-term tier stops
MEDIUM_TERM_DEAD_MONEY_DAYS    = 45    # shorter dead money window vs 90d for long-term
MEDIUM_TERM_DEAD_MONEY_MIN_PCT = 5.0   # must be up ≥5% after 45 days

# Catalyst window gates for medium-term entries
MEDIUM_TERM_CATALYST_WEEKS_MIN = 3     # catalyst must be at least 3 weeks out
MEDIUM_TERM_CATALYST_WEEKS_MAX = 8     # catalyst must be within 8 weeks

# Long-term re-underwriting trigger (large_growth positions in long-term bucket)
LONG_TERM_REUNDERWRITE_DAYS   = 90    # day 90 triggers re-underwriting review
LONG_TERM_REUNDERWRITE_WINDOW = 5     # alert fires within ±5 days of trigger

# DA-resolution: long-term entries must articulate explicit alpha above SPY
# CIO must project ≥10% alpha over SPY for the holding period; else BUCKET not BUY
LONG_TERM_ALPHA_MANDATE_PCT   = 10.0

# Portfolio-level drawdown circuit breaker (CRO amendment)
# If portfolio drawdown from peak exceeds this threshold, suspend ALL new medium-term entries
PORTFOLIO_MT_SUSPEND_DRAWDOWN = 15.0  # suspend medium-term new entries when port down >15%

# Re-underwriting: minimum projected alpha gap that must remain to migrate to confirmed long_term
REUNDERWRITE_ALPHA_MIN_PCT    = 8.0   # at day 90, alpha gap must still be ≥8% above SPY

# --- Scheduler ---
# Overnight earnings reaction — 07:30 ET, before pre-market summary
EARNINGS_REACTION_HOUR   = 7
EARNINGS_REACTION_MINUTE = 30

# Gap & catalyst scanner — 08:45 ET, lightweight, no Claude
GAP_SCAN_HOUR   = 8
GAP_SCAN_MINUTE = 45

# Two full committee cycles per day
RUN_HOUR        = 9
RUN_MINUTE      = 50   # 9:50 AM ET — post-auction (was 9:35; let opening settle first)
AFTERNOON_HOUR  = 15
AFTERNOON_MINUTE = 0   # 3:00 PM ET

# Midday risk check — stops only on open positions, no new entries, no Claude
MIDDAY_HOUR   = 12
MIDDAY_MINUTE = 30

# Summary schedule (ET)
PREMARKET_SUMMARY_HOUR   = 9
PREMARKET_SUMMARY_MINUTE = 0   # 9:00 AM — 30 min before open
CLOSE_SUMMARY_HOUR       = 16
CLOSE_SUMMARY_MINUTE     = 5   # 4:05 PM — just after close

# --- Tier-specific entry criteria ---
MID_GROWTH_FUNDAMENTALS_NEEDED   = 1      # committee decides — gate blocks only junk
MID_GROWTH_MOMENTUM_NEEDED       = 1      # one positive signal sufficient; committee has full picture
MID_GROWTH_TECHNICAL_NEEDED      = 1      # just avoid confirmed death+downtrend
MID_GROWTH_PE_MAX                = 200    # high-growth stocks can carry high P/E
MID_GROWTH_MARGIN_MIN            = -5.0   # thin/slightly negative margin OK
MID_GROWTH_PRELIM_MIN            = 0      # only block if actively negative (death cross + all signals bad)
MID_GROWTH_DEAD_MONEY_REV_EXEMPT = 0.25  # skip dead money if revenue growing >25%

SPEC_GROWTH_SCORE_MIN         = 35    # minimum future_growth score for speculative
SPEC_ANALYST_UPSIDE_MIN       = 20    # % upside to analyst target required
SPEC_REVENUE_GROWTH_MIN       = 0.40  # 40% revenue growth YoY min (or binary catalyst within 18mo)
SPEC_MIN_MARKET_CAP           = 2e9   # $2B minimum market cap
SPEC_MAX_MARKET_CAP           = 25e9  # $25B maximum market cap (already-discovered excluded)
SPEC_MIN_INST_OWNERSHIP       = 0.15  # 15% institutional ownership minimum
SPEC_MIN_ADV                  = 50e6  # $50M average daily volume minimum
SPEC_THESIS_SIGNALS_NEEDED    = 2     # need 2 of 5 thesis signals
SPEC_THESIS_HOLD_MONTHS       = 36    # 3-year thesis windows, renewable (was 18)
SPEC_TRAILING_STOP_MIN_GAIN   = 50.0  # trailing stop activates only above +50% (was +40%)
SPEC_TRAILING_STOP_1M_DROP    = -25.0 # trigger on -25% reversal while winning (was -15%)
SPEC_TIER_DRAWDOWN_KILL       = 40.0  # if spec tier draws down >40% from peak: halve sizes
SPEC_TIER_HALT_DAYS           = 90    # freeze new spec entries for this many days after kill switch

MID_TRAILING_STOP_MIN_GAIN    = 25.0
MID_TRAILING_STOP_1M_DROP     = -10.0

# Medium-term trailing stop: tighter — protect gains faster in catalyst plays
MEDIUM_TERM_TRAILING_STOP_MIN_GAIN = 15.0  # activate after +15%
MEDIUM_TERM_TRAILING_STOP_1M_DROP  = -5.0  # exit if 10-day close drops -5% while winning (committee: tightened from -6%)

# --- Entry Criteria: Hard Blocks (absolute stops, very few) ---
CRITERIA_RSI_MIN         = 25   # true panic extreme only
CRITERIA_RSI_MAX         = 78   # true overbought extreme only
CRITERIA_FG_PANIC        = 15   # Fear & Greed below this = no new buys
CRITERIA_EARNINGS_DAYS   = 3    # skip if earnings within this many days

# --- Entry Criteria: Scoring Thresholds ---
CRITERIA_FUNDAMENTALS_NEEDED  = 3    # need 3 of 5 fundamental checks
CRITERIA_MOMENTUM_NEEDED      = 1    # need 1 of 4 — lets healthy pullbacks through; committee decides
CRITERIA_TECHNICAL_NEEDED     = 1    # need 1 of 3 — avoid confirmed breakdowns, not normal weakness
CRITERIA_EPS_GROWTH_MIN       = 0.0  # any positive EPS growth
CRITERIA_REVENUE_GROWTH_MIN   = 0.0  # any positive revenue growth
CRITERIA_PROFIT_MARGIN_MIN    = 0.05 # 5% margin
CRITERIA_PE_MAX               = 80   # covers growth stocks

# --- Tier Classification (all basket tickers) ---
TICKER_TIERS = {
    # ── Mega caps ───────────────────────────────────────────────────────────
    "AAPL": "mega", "MSFT": "mega", "GOOGL": "mega", "GOOG": "mega", "META": "mega",
    "AMZN": "mega", "NVDA": "mega", "TSLA": "mega",
    # ── Large growth (established thesis leaders) ────────────────────────
    "AMD": "large_growth", "AVGO": "large_growth", "ARM": "large_growth",
    "MRVL": "large_growth", "TSM": "large_growth", "ASML": "large_growth",
    "PLTR": "large_growth", "CRM": "large_growth", "NOW": "large_growth",
    "ORCL": "large_growth", "AI": "large_growth",
    "CRWD": "large_growth", "PANW": "large_growth", "ZS": "large_growth",
    "LMT": "large_growth", "RTX": "large_growth", "NOC": "large_growth",
    "GD": "large_growth", "AXON": "large_growth",
    "ISRG": "large_growth", "ETN": "large_growth",
    "COIN": "large_growth", "ANET": "large_growth",
    "LLY": "large_growth", "PWR": "large_growth",
    "SHOP": "large_growth", "UBER": "large_growth",
    "MA": "large_growth", "MSCI": "large_growth",
    "FANG": "large_growth", "FCX": "large_growth",
    "WMB": "large_growth", "RGLD": "large_growth",
    "COP": "large_growth",
    "SNPS": "large_growth", "GE": "large_growth", "APH": "large_growth",
    "GEV": "large_growth", "VST": "large_growth",        # power + nuclear grid
    "TDG": "large_growth",                                # aerospace aftermarket
    "SE":  "large_growth", "VEEV": "large_growth",       # SEA ecommerce, healthcare SaaS
    # ── Mid growth (high-conviction, earlier in curve) ───────────────────
    "AMAT": "mid_growth", "LRCX": "mid_growth", "KLAC": "mid_growth",
    "MU": "mid_growth",
    "RGTI": "mid_growth",
    "CCJ": "mid_growth", "CEG": "mid_growth",
    "BWXT": "mid_growth",
    "ABB": "mid_growth", "VRT": "large_growth",           # promoted: AI infra scale
    "HOOD": "mid_growth",
    "MELI": "mid_growth", "NU": "mid_growth",
    "RKLB": "mid_growth",
    "DXCM": "mid_growth",
    "KEYS": "mid_growth", "CACI": "mid_growth",
    "TLN":  "mid_growth", "KTOS": "mid_growth",           # nuclear/data center, defense drones
    "GRAB": "mid_growth",                                  # SEA fintech
    # ── Speculative / Moonshots (real revenue + asymmetric future, 3-year thesis) ─
    "IONQ": "speculative",            # quantum computing — $130M revenue +200% YoY, DARPA/NVIDIA
    "MP":   "speculative",            # only US rare earth magnet producer — DoD + Apple contracts
    "SOUN": "speculative",            # AI voice platform — $84M revenue +90% YoY, NVIDIA partner
    "LUNR": "speculative",            # NASA lunar infrastructure — $200M+ govt contracts, Artemis
    "RXRX": "speculative",            # AI drug discovery — $12B Roche milestones, 23PB data moat
    "ASTS": "speculative",            # satellite-to-phone internet — binary moonshot slot (1% max)
}

# Allocation by tier × confidence (risk manager enforces, not Claude)
TIER_ALLOC = {
    "mega":         {6: 4.0, 7: 5.0, 8: 6.0,  9: 7.0,  10: 8.0},
    "large_growth": {6: 3.0, 7: 4.0, 8: 5.0,  9: 6.0,  10: 7.0},
    "mid_growth":   {6: 2.0, 7: 3.0, 8: 4.0,  9: 4.5,  10: 5.0},
    "speculative":  {7: 1.5, 8: 2.0,  9: 2.5,  10: 3.0},  # min conf 7 — no conf-6 entries
}

# Medium-term bucket allocation (capped at 6% regardless of tier)
# Used whenever a position is assigned to the medium-term bucket by the committee
MEDIUM_TERM_TIER_ALLOC = {6: 3.0, 7: 4.0, 8: 5.0, 9: 6.0, 10: 6.0}

# Stop-loss by tier (speculative needs wider stop — don't shake out on vol)
STOP_LOSS_BY_TIER = {
    "mega":         6.0,
    "large_growth": 8.0,
    "mid_growth":   10.0,
    "speculative":  20.0,  # wider — 15% whipsaws out on normal vol (was 15%)
}
STOP_LOSS_PCT = 8.0  # fallback for tickers not in TICKER_TIERS
STOP_EMERGENCY_MULT = 2.0  # auto-sell (no committee) if loss exceeds stop × this multiple

# --- Portfolio-Level Drawdown Rule ---
PORTFOLIO_DRAWDOWN_LIMIT    = 12.0   # % from portfolio peak — trigger risk-reduction mode
BEAR_MARKET_SIZE_FACTOR     = 0.50   # allocation multiplier when market is in fear/extreme_fear

# --- Re-entry Rule ---
REENTRY_MIN_TRADING_DAYS    = 20     # minimum trading days after a stop-loss exit before re-entry

# --- Valuation Risk Tiers (P/E based, non-high-growth stocks) ---
VALUATION_PE_ELEVATED       = 60.0   # P/E above this → Elevated risk flag
VALUATION_PE_EXTREME        = 120.0  # P/E above this → Extreme risk flag

# --- ATR-Based Stop Multipliers (replaces fixed % stops) ---
ATR_STOP_MULT_MEGA_LARGE    = 2.5    # Mega/Large growth: stop = entry - 2.5×ATR(20)
ATR_STOP_MULT_MID_SPEC      = 3.5    # Mid/Speculative: stop = entry - 3.5×ATR(20)

# --- Portfolio Constraints ---
PORTFOLIO_BETA_CAP          = 1.6    # max portfolio beta vs SPY (monitored, not hard-enforced)
FACTOR_CLUSTER_CAP          = 0.40   # max 40% of portfolio in any single factor cluster

# --- Trim Rule ---
TRIM_TRIGGER_MULTIPLE       = 1.4    # trim when position > 1.4× its original target weight
TRIM_FAST_GAIN_PCT          = 50.0   # also trim when position gains >50% in <30 days
TRIM_SIZE_PCT               = 0.33   # trim 33% of position back toward target weight

# --- Winner Protection ---
# A position still showing upward momentum is exempt from mechanical size/gain trims.
# Trim is deferred until momentum breaks — the trailing stop and technical exits then handle exit.
WINNER_CAP_EXEMPT_GAIN  = 30.0   # unrealized % above which the wider winner hard-cap applies
WINNER_POSITION_CAP_PCT = 15.0   # hard-cap for confirmed winner positions (vs 8% standard cap)

# --- Add Cadence ---
ADD_CADENCE_DAYS            = 10     # max 1 add per position per 10 trading days

# --- Strength-Add Gates ---
STRENGTH_ADD_MIN_CONVICTION = 9      # min conviction required for adds on strength (not pullbacks)
STRENGTH_ADD_MAX_WEIGHT     = 1.3    # post-add position ≤ 1.3× target weight

# --- Factor Clusters (correlated theme groups for concentration management) ---
FACTOR_CLUSTERS = {
    "ai_tech":        {"MSFT", "GOOGL", "META", "AMZN", "NVDA", "AAPL", "TSLA", "PLTR", "CRM",
                       "NOW", "ORCL", "AI", "ANET", "SNOW", "PSTG"},
    "semis":          {"AMD", "AVGO", "ARM", "MRVL", "TSM", "AMAT", "LRCX", "KLAC",
                       "MU", "SNPS", "KEYS", "APH"},
    "cyber":          {"CRWD", "PANW", "ZS"},
    "defense":        {"LMT", "RTX", "NOC", "GD", "AXON", "BWXT", "GE", "CACI", "KTOS", "TDG"},
    "nuclear_energy": {"CCJ", "CEG", "GEV", "VST", "TLN", "OKLO", "SMR"},
    "space":          {"RKLB", "ASTS", "LUNR"},
    "voice_ai":       {"SOUN"},
    "quantum":        {"IONQ", "RGTI"},
    "robotics_infra": {"ABB", "ETN", "ISRG", "VRT", "PWR"},
    "fintech":        {"HOOD", "COIN", "MELI", "NU", "MA", "MSCI"},
    "ecommerce":      {"SHOP", "UBER", "SE", "GRAB", "MELI"},
    "healthcare":     {"LLY", "DXCM", "RXRX", "VEEV"},
    "energy_comm":    {"FANG", "COP", "FCX", "WMB", "RGLD", "MP"},
}

# Sector map — used for concentration limits (MAX_SECTOR_PCT)
SECTOR_MAP = {
    "MSFT": "ai_software", "GOOGL": "ai_software", "GOOG": "ai_software", "META": "ai_software",
    "AMZN": "ai_software", "ORCL": "ai_software", "PLTR": "ai_software",
    "CRM": "ai_software",  "NOW": "ai_software",  "AI": "ai_software",
    "NVDA": "semis", "AMD": "semis",  "AVGO": "semis", "AMAT": "semis",
    "LRCX": "semis", "KLAC": "semis", "MU": "semis",   "ARM": "semis",
    "MRVL": "semis", "TSM": "semis",  "ASML": "semis", "SNPS": "semis", "KEYS": "semis",
    "IONQ": "quantum", "RGTI": "quantum",
    "CRWD": "cyber", "PANW": "cyber", "ZS": "cyber",
    "RKLB": "space", "ASTS": "space", "LUNR": "space",
    "SOUN": "voice_ai",
    "CCJ": "nuclear", "CEG": "nuclear",
    "GEV": "nuclear", "VST": "nuclear", "TLN": "nuclear",
    "LMT": "defense", "RTX": "defense", "NOC": "defense", "GD": "defense",
    "AXON": "defense", "BWXT": "defense", "GE": "defense", "CACI": "defense",
    "KTOS": "defense", "TDG": "defense",
    "ABB": "robotics", "ETN": "robotics", "ISRG": "robotics",
    "ANET": "ai_infra", "VRT": "ai_infra", "PWR": "ai_infra",
    "PSTG": "ai_infra", "SNOW": "ai_infra", "APH": "ai_infra",
    "HOOD": "fintech", "COIN": "fintech", "MELI": "ecommerce", "NU": "fintech",
    "MA": "fintech", "MSCI": "fintech",
    "SHOP": "ecommerce", "UBER": "ecommerce", "SE": "ecommerce", "GRAB": "ecommerce",
    "RXRX": "biotech", "LLY": "healthcare", "DXCM": "healthcare", "VEEV": "healthcare",
    "FANG": "energy_oil", "COP": "energy_oil",
    "FCX": "commodities_metals", "MP": "commodities_metals",
    "AAPL": "mega_tech", "TSLA": "mega_tech",
}

# --- Position Sizing ---
CONGRESS_BONUS_PCT   = 2.0
INSIDER_BONUS_PCT    = 1.0

# --- 6-Agent Confidence Modifiers ---
# Applied to CIO base confidence before PM sizes the position
CONF_MOD_CRO_CAUTION      = -1   # CRO = Caution
CONF_MOD_DA_HIGH          = -2   # Devil's Advocate severity = High
CONF_MOD_DA_MEDIUM        = -1   # Devil's Advocate severity = Medium
CONF_MOD_QUANT_BEARISH    = -1   # QUANT = Bearish
CONF_MOD_QUANT_STRONG_BUY =  1   # QUANT = Strongly Bullish

# --- Tranche / Scale-In ---
# Tranche 1 = 50% of target on entry
# Tranche 2 = +25% after earnings beat OR price breaks prior high on volume
# Tranche 3 = +25% after second independent confirmation
TRANCHE_1_PCT   = 0.50   # fraction of target size for initial entry
TRANCHE_2_PCT   = 0.25   # fraction added on first confirmation
TRANCHE_3_PCT   = 0.25   # fraction added on second confirmation
# Minimum confidence delta required to replace a position (new must beat weakest by this)
REPLACEMENT_CONF_DELTA = 2

# --- Liquidity / ADV ---
# Position size in dollars must not exceed this fraction of 30-day avg daily dollar volume
ADV_POSITION_PCT_MAX = 0.05   # 5% of ADV

# --- Basket Reviews ---
BASKET_WEEKLY_REVIEW_HOUR   = 16
BASKET_WEEKLY_REVIEW_MINUTE = 30  # Friday 4:30 PM ET (moved from Saturday — deploys Monday open)

# Bi-weekly speculative research — Wednesday alternating weeks, full paid APIs, spec tier only
SPEC_REFRESH_HOUR   = 18
SPEC_REFRESH_MINUTE = 0   # Wednesday 6:00 PM ET

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

# --- Basket ---
BASKET_REFRESH_HOUR   = 8
BASKET_REFRESH_MINUTE = 0   # First Monday of each month, 8:00 AM ET

# --- Options ---
OPTION_DAYS_TO_EXPIRY = 30  # target ~30 days out (used for auto-execution, kept for reference)

# --- Options Advisor (inform-only proposals) ---
OPTIONS_PROPOSAL_ACTIVE_DAYS = 42   # monitor proposals for up to 6 weeks
OPTIONS_DTE_WARNING_WEEKS    = 2.5  # alert when this many weeks remain
OPTIONS_DTE_EXIT_WEEKS       = 1.0  # force-close alert when < 1 week remains

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
