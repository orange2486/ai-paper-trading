"""升降單位、滑價、手續費、證交稅（PLAN.md 第 4 節）。"""

from __future__ import annotations

import math

from . import config


def tick_size(price: float, code: str) -> float:
    """證交所升降單位。ETF：未滿 50 元 0.01、50 元以上 0.05。"""
    if config.is_etf(code):
        return 0.01 if price < 50 else 0.05
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def _round(x: float) -> float:
    return round(x + 0.0, 4)


def floor_to_tick(price: float, code: str) -> float:
    t = tick_size(price, code)
    return _round(math.floor(price / t + 1e-9) * t)


def ceil_to_tick(price: float, code: str) -> float:
    t = tick_size(price, code)
    return _round(math.ceil(price / t - 1e-9) * t)


def limit_prices(ref: float, code: str) -> tuple[float, float]:
    """漲停、跌停（參考價 ±10%，漲停向下、跌停向上取到升降單位）。近似。"""
    return floor_to_tick(ref * 1.10, code), ceil_to_tick(ref * 0.90, code)


def buy_fill_price(open_price: float, code: str) -> float:
    return _round(open_price + config.SLIPPAGE_TICKS * tick_size(open_price, code))


def sell_fill_price(open_price: float, code: str) -> float:
    return _round(open_price - config.SLIPPAGE_TICKS * tick_size(open_price, code))


def fee(amount: float) -> float:
    return round(amount * config.FEE_RATE, 2)


def tax(amount: float, code: str) -> float:
    rate = config.TAX_ETF if config.is_etf(code) else config.TAX_STOCK
    return round(amount * rate, 2)


def buy_cost(price: float, shares: int) -> float:
    """買進總付出＝價金＋手續費。"""
    amount = price * shares
    return round(amount + fee(amount), 2)


def sell_proceeds(price: float, shares: int, code: str) -> float:
    """賣出淨收入＝價金－手續費－證交稅。"""
    amount = price * shares
    return round(amount - fee(amount) - tax(amount, code), 2)


def max_shares(budget: float, price: float) -> int:
    """budget 內最多買幾股（含手續費）。"""
    if price <= 0 or budget <= 0:
        return 0
    n = int(budget // (price * (1 + config.FEE_RATE)))
    while n > 0 and buy_cost(price, n) > budget:
        n -= 1
    return n
