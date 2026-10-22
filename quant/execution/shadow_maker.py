"""Conservative queue-based fill estimates for public-market shadow orders."""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def estimate_shadow_fill(
    *,
    side: str,
    limit_price: float,
    order_qty: float,
    queue_ahead_qty: float,
    aggregate_trades: Iterable[Mapping],
) -> dict[str, float | int | None]:
    """Estimate a post-only fill from aggregate trades after observation.

    At the resting price, same-side displayed depth observed at submission is
    treated as queue ahead.  Cancellations are not credited, which is
    conservative.  A trade through the resting price implies the observed
    level was cleared.  This remains a shadow estimate, not an exchange fill.
    Binance's ``m`` field means "buyer was maker".
    """
    side = side.lower()
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    if limit_price <= 0 or order_qty <= 0 or queue_ahead_qty < 0:
        raise ValueError("limit_price/order_qty must be positive and queue_ahead_qty non-negative")

    queue_remaining = float(queue_ahead_qty)
    fill_remaining = float(order_qty)
    eligible_trade_qty = 0.0
    eligible_trade_count = 0
    first_fill_time_ms: int | None = None
    full_fill_time_ms: int | None = None
    for trade in sorted(aggregate_trades, key=lambda item: (int(item.get("T", 0)), int(item.get("a", 0)))):
        price = float(trade["p"])
        quantity = float(trade["q"])
        buyer_was_maker = bool(trade["m"])
        correct_taker_side = buyer_was_maker if side == "buy" else not buyer_was_maker
        reaches_limit = price <= limit_price if side == "buy" else price >= limit_price
        if not correct_taker_side or not reaches_limit or quantity <= 0:
            continue
        eligible_trade_count += 1
        eligible_trade_qty += quantity
        traded_through = price < limit_price if side == "buy" else price > limit_price
        if traded_through:
            queue_remaining = 0.0
            if fill_remaining > 0 and first_fill_time_ms is None:
                first_fill_time_ms = int(trade.get("T", 0))
            fill_remaining = 0.0
            full_fill_time_ms = int(trade.get("T", 0))
            break
        queue_consumed = min(queue_remaining, quantity)
        queue_remaining -= queue_consumed
        residual = quantity - queue_consumed
        filled_here = min(fill_remaining, residual)
        fill_remaining -= filled_here
        if filled_here > 0 and first_fill_time_ms is None:
            first_fill_time_ms = int(trade.get("T", 0))
        if fill_remaining <= 1e-12:
            fill_remaining = 0.0
            full_fill_time_ms = int(trade.get("T", 0))
            break
    return {
        "filled_qty": order_qty - fill_remaining,
        "unfilled_qty": fill_remaining,
        "queue_ahead_initial_qty": queue_ahead_qty,
        "queue_ahead_remaining_qty": queue_remaining,
        "eligible_trade_qty": eligible_trade_qty,
        "eligible_trade_count": eligible_trade_count,
        "first_fill_time_ms": first_fill_time_ms,
        "full_fill_time_ms": full_fill_time_ms,
    }
