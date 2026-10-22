"""Pure, fail-closed sizing for exchange-filtered futures orders.

This module is intentionally an execution *admission* calculation, not an
order builder.  It exposes when Binance's quantity increments force an order
above the research or live risk cap, so a caller cannot silently increase a
precommitted stake just to satisfy exchange filters.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any


def _decimal(value: object, *, field: str) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field} must be a positive finite decimal")
    return parsed


def _ceil(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _price(value: Decimal, tick: Decimal, *, side: str) -> Decimal:
    rounding = ROUND_FLOOR if side == "BUY" else ROUND_CEILING
    return (value / tick).to_integral_value(rounding=rounding) * tick


def evaluate_bounded_executable_notional(
    symbol: dict[str, Any],
    book_ticker: dict[str, Any],
    *,
    desired_notional_usdt: object,
    maximum_notional_usdt: object,
) -> dict[str, Any]:
    """Evaluate required minimum size against an explicit hard notional cap.

    A ``WITHIN_RISK_CAP`` result is only sizing feasibility.  It does not
    authorize a strategy, submit an order, or override a frozen experiment.
    """

    symbol_name = str(symbol.get("symbol") or "")
    if not symbol_name or book_ticker.get("symbol") != symbol_name:
        raise ValueError("symbol and book ticker must identify the same market")
    filters = {
        str(item.get("filterType")): item
        for item in symbol.get("filters") or []
        if isinstance(item, dict)
    }
    required = {"PRICE_FILTER", "LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL"}
    if not required.issubset(filters):
        raise ValueError(f"{symbol_name} exchange filters are incomplete")
    desired = _decimal(desired_notional_usdt, field="desired_notional_usdt")
    maximum = _decimal(maximum_notional_usdt, field="maximum_notional_usdt")
    if desired > maximum:
        raise ValueError("desired_notional_usdt cannot exceed maximum_notional_usdt")
    tick = _decimal(filters["PRICE_FILTER"]["tickSize"], field="tickSize")
    lot = filters["LOT_SIZE"]
    market_lot = filters["MARKET_LOT_SIZE"]
    step = _decimal(lot["stepSize"], field="stepSize")
    minimum_qty = max(
        _decimal(lot["minQty"], field="lot minQty"),
        _decimal(market_lot["minQty"], field="market lot minQty"),
    )
    maximum_qty = min(
        _decimal(lot["maxQty"], field="lot maxQty"),
        _decimal(market_lot["maxQty"], field="market lot maxQty"),
    )
    minimum_notional = _decimal(filters["MIN_NOTIONAL"]["notional"], field="minimum notional")
    bid = _decimal(book_ticker["bidPrice"], field="bidPrice")
    ask = _decimal(book_ticker["askPrice"], field="askPrice")
    if ask <= bid:
        raise ValueError("book ticker must have askPrice above bidPrice")

    sides: dict[str, dict[str, object]] = {}
    for side, raw_price in (("BUY", bid), ("SELL", ask)):
        price = _price(raw_price, tick, side=side)
        required_qty = _ceil(max(minimum_qty, minimum_notional / price), step)
        desired_qty = _ceil(desired / price, step)
        executable_qty = max(required_qty, desired_qty)
        executable_notional = executable_qty * price
        fits_exchange = executable_qty <= maximum_qty
        fits_risk_cap = executable_notional <= maximum
        sides[side] = {
            "filtered_price": str(price),
            "desired_quantity_rounded_up": str(desired_qty),
            "minimum_executable_quantity": str(required_qty),
            "executable_quantity": str(executable_qty),
            "executable_notional_usdt": str(executable_notional),
            "increment_over_desired_usdt": str(max(Decimal("0"), executable_notional - desired)),
            "fits_exchange_filters": fits_exchange,
            "fits_risk_cap": fits_risk_cap,
            "admitted": fits_exchange and fits_risk_cap,
        }
    status = "WITHIN_RISK_CAP" if symbol.get("status") == "TRADING" and all(
        bool(side["admitted"]) for side in sides.values()
    ) else "RISK_CAP_VETO"
    return {
        "schema_version": 1,
        "kind": "BOUNDED_EXECUTABLE_NOTIONAL_DIAGNOSTIC",
        "status": status,
        "promotion_eligible": False,
        "live_order_authorization": "CLOSED",
        "symbol": symbol_name,
        "desired_notional_usdt": str(desired),
        "maximum_notional_usdt": str(maximum),
        "sides": sides,
        "interpretation": (
            "This is a sizing diagnostic only. A risk-cap veto must reject the "
            "trade; it must not be repaired by silently increasing the stake."
        ),
    }
