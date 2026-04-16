from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOptionContractsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, ContractType
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest, StockLatestNewsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta, timezone
import config


_trading = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=True)
_stock_data = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
_crypto_data = CryptoHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)


def get_portfolio():
    account = _trading.get_account()
    return {
        "equity":       float(account.equity),
        "cash":         float(account.cash),
        "buying_power": float(account.buying_power),
        "portfolio_value": float(account.portfolio_value),
    }


def get_positions():
    positions = _trading.get_all_positions()
    return [
        {
            "symbol":       p.symbol,
            "qty":          float(p.qty),
            "avg_entry":    float(p.avg_entry_price),
            "current_price":float(p.current_price),
            "unrealized_pl":float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc) * 100,
            "asset_class":  p.asset_class,
        }
        for p in positions
    ]


def get_stock_bars(symbol: str, days: int = 60):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end)
    bars = _stock_data.get_stock_bars(req)
    return bars[symbol].df if symbol in bars else None


def get_crypto_bars(symbol: str, days: int = 60):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end)
    bars = _crypto_data.get_crypto_bars(req)
    return bars[symbol].df if symbol in bars else None


def get_news(symbol: str, limit: int = 10):
    req = StockLatestNewsRequest(symbols=[symbol.replace("/", "")], limit=limit)
    news = _stock_data.get_news(req)
    return news.get(symbol.replace("/", ""), [])


def place_market_order(symbol: str, qty: float, side: str):
    order_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL
    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
    )
    return _trading.submit_order(req)


def get_option_chain(symbol: str, days_out: int = 30):
    target_date = (datetime.now() + timedelta(days=days_out)).date()
    req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        expiration_date_gte=str(target_date - timedelta(days=7)),
        expiration_date_lte=str(target_date + timedelta(days=7)),
    )
    contracts = _trading.get_option_contracts(req)
    return contracts.option_contracts if contracts else []


def place_option_order(contract_symbol: str, qty: int, side: str):
    order_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL
    req = MarketOrderRequest(
        symbol=contract_symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
    )
    return _trading.submit_order(req)
