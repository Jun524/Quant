"""Current Binance symbol-filter feasibility for a fixed-notional order path."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


def _d(value: object) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("exchange filter values must be finite")
    return result


def _floor(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def evaluate_fixed_notional_filter_gate(
    symbol: dict,
    book_ticker: dict,
    *,
    fixed_notional_usdt: object,
) -> dict:
    if symbol.get("symbol") != "BTCUSDT" or book_ticker.get("symbol") != "BTCUSDT":
        raise ValueError("filter gate is locked to BTCUSDT")
    filters = {
        str(item.get("filterType")): item
        for item in symbol.get("filters") or []
        if isinstance(item, dict)
    }
    required = {"PRICE_FILTER", "LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL"}
    if not required.issubset(filters):
        raise ValueError("BTCUSDT exchangeInfo is missing required filters")
    price_filter = filters["PRICE_FILTER"]
    lot = filters["LOT_SIZE"]
    market_lot = filters["MARKET_LOT_SIZE"]
    minimum_notional = _d(filters["MIN_NOTIONAL"]["notional"])
    tick = _d(price_filter["tickSize"])
    step = _d(lot["stepSize"])
    minimum_qty = _d(lot["minQty"])
    maximum_qty = _d(lot["maxQty"])
    market_minimum_qty = _d(market_lot["minQty"])
    notional = _d(fixed_notional_usdt)
    bid = _d(book_ticker["bidPrice"])
    ask = _d(book_ticker["askPrice"])
    if min(tick, step, minimum_qty, minimum_notional, notional, bid, ask) <= 0 or ask <= bid:
        raise ValueError("exchange filters or book ticker are invalid")

    order_types = set(symbol.get("orderTypes") or [])
    time_in_force = set(symbol.get("timeInForce") or [])
    sides = {}
    for side, raw_price, rounding in (
        ("BUY", bid, ROUND_FLOOR),
        ("SELL", ask, ROUND_CEILING),
    ):
        price = (
            (raw_price / tick).to_integral_value(rounding=rounding) * tick
        )
        quantity = _floor(notional / price, step)
        actual_notional = quantity * price
        minimum_executable_qty = _ceil(
            max(minimum_qty, market_minimum_qty, minimum_notional / price), step
        )
        minimum_executable_notional = minimum_executable_qty * price
        sides[side] = {
            "book_price": str(raw_price),
            "filtered_entry_price": str(price),
            "fixed_notional_quantity": str(quantity),
            "fixed_notional_actual": str(actual_notional),
            "minimum_executable_quantity": str(minimum_executable_qty),
            "minimum_executable_notional": str(minimum_executable_notional),
            "passes": bool(
                minimum_qty <= quantity <= maximum_qty
                and quantity >= market_minimum_qty
                and actual_notional >= minimum_notional
            ),
        }
    checks = [
        {
            "name": "symbol_trading",
            "passed": symbol.get("status") == "TRADING",
            "actual": symbol.get("status"),
            "required": "TRADING",
        },
        {
            "name": "required_order_types",
            "passed": {"LIMIT", "MARKET"}.issubset(order_types),
            "actual": sorted(order_types),
            "required": ["LIMIT", "MARKET"],
        },
        {
            "name": "gtx_supported",
            "passed": "GTX" in time_in_force,
            "actual": sorted(time_in_force),
            "required": "GTX",
        },
        {
            "name": "buy_fixed_notional_executable",
            "passed": sides["BUY"]["passes"],
            "actual": sides["BUY"]["fixed_notional_actual"],
            "required_min": str(minimum_notional),
        },
        {
            "name": "sell_fixed_notional_executable",
            "passed": sides["SELL"]["passes"],
            "actual": sides["SELL"]["fixed_notional_actual"],
            "required_min": str(minimum_notional),
        },
    ]
    return {
        "candidate": "ALPHA_007_IMBALANCE_GUIDED_MAKER",
        "status": "PASS" if all(item["passed"] for item in checks) else "FAIL",
        "promotion_eligible": False,
        "fixed_notional_usdt": float(notional),
        "filters": {
            "tick_size": str(tick),
            "lot_step_size": str(step),
            "lot_minimum_qty": str(minimum_qty),
            "market_minimum_qty": str(market_minimum_qty),
            "minimum_notional": str(minimum_notional),
        },
        "sides": sides,
        "checks": checks,
        "interpretation": (
            "A FAIL blocks the current order path. It cannot be repaired by silently "
            "increasing the preregistered 50 USDT order size."
        ),
    }
