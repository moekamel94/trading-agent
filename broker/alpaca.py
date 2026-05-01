from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, GetOptionContractsRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, ContractType
from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, OptionLatestQuoteRequest
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta, timezone, date as _date
import pandas as pd
import config


_trading = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=True)
_stock_data = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
_option_data = OptionHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)


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


def get_stock_bars(symbol: str, days: int = 300):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end, feed=DataFeed.IEX)
    bars = _stock_data.get_stock_bars(req)
    if symbol not in bars.data or not bars.data[symbol]:
        return None
    return pd.DataFrame([b.model_dump() for b in bars.data[symbol]])


def place_market_order(symbol: str, qty: float, side: str):
    order_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL
    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
    )
    return _trading.submit_order(req)


def cancel_order(order_id: str) -> bool:
    """Cancel a pending order by ID. Returns True if cancelled, False if already filled."""
    try:
        _trading.cancel_order_by_id(order_id)
        return True
    except Exception as e:
        if "422" in str(e) or "cannot be cancelled" in str(e).lower() or "filled" in str(e).lower():
            return False
        raise


def get_open_orders():
    """Return list of all open/pending orders."""
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    orders = _trading.get_orders(filter=req)
    return [{"id": str(o.id), "symbol": o.symbol, "side": str(o.side), "status": str(o.status)} for o in orders]


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


def place_option_limit_order(contract_symbol: str, qty: int, side: str, limit_price: float):
    """Place a limit order for an option contract."""
    order_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL
    req = LimitOrderRequest(
        symbol=contract_symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
        limit_price=round(limit_price, 2),
    )
    return _trading.submit_order(req)


def build_occ_symbol(symbol: str, expiry: "_date", direction: str, strike: float) -> str:
    """
    Build the OCC option contract symbol.
    Format: [SYMBOL][YYMMDD][C/P][strike*1000 zero-padded to 8 digits]
    Example: SPY May 1 2026 Call $711 → SPY260501C00711000
    """
    yy = expiry.strftime("%y")
    mm = expiry.strftime("%m")
    dd = expiry.strftime("%d")
    cp = "C" if direction.lower() == "call" else "P"
    strike_int = int(round(strike * 1000))
    return f"{symbol.upper()}{yy}{mm}{dd}{cp}{strike_int:08d}"


def get_option_last_price(contract_symbol: str) -> float | None:
    """Return the latest mid-price for an option contract, or None on failure."""
    try:
        req = OptionLatestQuoteRequest(symbol_or_symbols=[contract_symbol])
        quotes = _option_data.get_option_latest_quote(req)
        q = quotes.get(contract_symbol)
        if q is None:
            return None
        bid = float(q.bid_price or 0)
        ask = float(q.ask_price or 0)
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 4)
        return bid or ask or None
    except Exception:
        return None
