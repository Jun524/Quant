"""Measure whether a maker-seeking policy survives real order execution.

Backtests generally assume that a touched limit order fills.  Real post-only
orders can instead wait, partially fill, or be cancelled immediately before a
favourable move.  This module keeps those effects separate from alpha PnL.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from math import isfinite


REQUIRED_FIELDS = {
    "signal_id",
    "side",
    "submitted_qty",
    "reference_mid",
    "filled_qty",
    "average_fill_price",
    "maker_filled_qty",
    "taker_filled_qty",
    "fee",
}


def _number(row: Mapping, key: str, *, allow_blank: bool = False) -> float | None:
    value = row.get(key)
    if value in (None, "") and allow_blank:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be numeric for signal {row.get('signal_id')!r}") from exc
    if not isfinite(number):
        raise ValueError(f"{key} must be finite for signal {row.get('signal_id')!r}")
    return number


def summarize_maker_execution(
    rows: Iterable[Mapping],
    *,
    min_orders: int = 100,
    min_filled_orders: int = 30,
    min_maker_share: float = 0.70,
    max_effective_fee_bps: float = 2.8,
    gross_edge_budget_bps: float | None = None,
) -> dict:
    """Summarize one final record per submitted order.

    ``reference_mid`` is the mid price at submission.  ``mid_after_timeout``
    measures the unfilled opportunity at cancellation/timeout, while
    ``mid_after_5m`` measures adverse selection five minutes after a fill.
    Signed costs are positive when execution hurt the strategy and negative
    when it helped.
    """
    records = list(rows)
    if not records:
        raise ValueError("at least one execution row is required")

    submitted_qty = filled_qty = maker_qty = taker_qty = 0.0
    filled_notional = fees = 0.0
    slippage_cost = 0.0
    missed_cost = missed_weight = 0.0
    markout_cost = markout_weight = 0.0
    filled_orders = partial_orders = unfilled_orders = 0
    orders_with_fills = filled_markout_observations = 0
    orders_with_unfilled_qty = unfilled_opportunity_observations = 0

    seen: set[str] = set()
    for row in records:
        missing = REQUIRED_FIELDS.difference(row)
        if missing:
            raise ValueError(f"missing fields {sorted(missing)} for signal {row.get('signal_id')!r}")
        signal_id = str(row["signal_id"])
        if not signal_id or signal_id in seen:
            raise ValueError(f"signal_id must be non-empty and unique: {signal_id!r}")
        seen.add(signal_id)
        side = str(row["side"]).lower()
        if side not in {"buy", "sell"}:
            raise ValueError(f"side must be buy or sell for signal {signal_id!r}")
        sign = 1.0 if side == "buy" else -1.0
        submitted = _number(row, "submitted_qty")
        filled = _number(row, "filled_qty")
        reference = _number(row, "reference_mid")
        fill_price = _number(row, "average_fill_price")
        maker = _number(row, "maker_filled_qty")
        taker = _number(row, "taker_filled_qty")
        fee = _number(row, "fee")
        assert None not in (submitted, filled, reference, fill_price, maker, taker, fee)
        if submitted <= 0 or reference <= 0:
            raise ValueError(f"submitted_qty and reference_mid must be positive for signal {signal_id!r}")
        if min(filled, maker, taker, fee) < 0 or filled > submitted + 1e-12:
            raise ValueError(f"quantities and fee must be non-negative and filled_qty <= submitted_qty for {signal_id!r}")
        if abs(maker + taker - filled) > max(1e-9, filled * 1e-6):
            raise ValueError(f"maker_filled_qty + taker_filled_qty must equal filled_qty for {signal_id!r}")
        if filled > 0 and fill_price <= 0:
            raise ValueError(f"average_fill_price must be positive for filled signal {signal_id!r}")

        submitted_qty += submitted
        filled_qty += filled
        maker_qty += maker
        taker_qty += taker
        unfilled = submitted - filled
        if filled <= 0:
            unfilled_orders += 1
        elif filled < submitted - 1e-12:
            partial_orders += 1
        else:
            filled_orders += 1

        if filled > 0:
            orders_with_fills += 1
            notional = filled * fill_price
            filled_notional += notional
            fees += fee
            slippage_cost += sign * (fill_price - reference) / reference * 10_000 * notional
            mid_after = _number(row, "mid_after_5m", allow_blank=True)
            if mid_after is not None and mid_after > 0:
                filled_markout_observations += 1
                adverse_bps = sign * (fill_price - mid_after) / fill_price * 10_000
                markout_cost += adverse_bps * notional
                markout_weight += notional

        mid_after_timeout = _number(row, "mid_after_timeout", allow_blank=True)
        if unfilled > 0:
            orders_with_unfilled_qty += 1
        if unfilled > 0 and mid_after_timeout is not None and mid_after_timeout > 0:
            unfilled_opportunity_observations += 1
            opportunity_bps = sign * (mid_after_timeout - reference) / reference * 10_000
            missed_notional = unfilled * reference
            missed_cost += opportunity_bps * missed_notional
            missed_weight += missed_notional

    maker_share = maker_qty / filled_qty if filled_qty else 0.0
    fill_rate = filled_qty / submitted_qty
    fee_bps = fees / filled_notional * 10_000 if filled_notional else None
    slippage_bps = slippage_cost / filled_notional if filled_notional else None
    execution_cost_bps = (fee_bps + slippage_bps) if fee_bps is not None and slippage_bps is not None else None
    missed_opportunity_bps = missed_cost / missed_weight if missed_weight else None
    adverse_markout_bps = markout_cost / markout_weight if markout_weight else None
    filled_markout_coverage = (
        filled_markout_observations / orders_with_fills if orders_with_fills else 0.0
    )
    unfilled_opportunity_coverage = (
        unfilled_opportunity_observations / orders_with_unfilled_qty
        if orders_with_unfilled_qty
        else 1.0
    )
    all_in_drag_bps = None
    if execution_cost_bps is not None and adverse_markout_bps is not None:
        all_in_drag_bps = (
            fill_rate * (max(0.0, execution_cost_bps) + max(0.0, adverse_markout_bps))
            + (1.0 - fill_rate) * max(0.0, missed_opportunity_bps or 0.0)
        )
    checks = [
        {"name": "minimum_order_sample", "passed": len(records) >= min_orders, "actual": len(records), "required": min_orders},
        {
            "name": "minimum_filled_order_sample",
            "passed": orders_with_fills >= min_filled_orders,
            "actual": orders_with_fills,
            "required": min_filled_orders,
        },
        {"name": "maker_share", "passed": maker_share >= min_maker_share, "actual": maker_share, "required": min_maker_share},
        {
            "name": "effective_fee_bps",
            "passed": fee_bps is not None and fee_bps <= max_effective_fee_bps,
            "actual": fee_bps,
            "required_max": max_effective_fee_bps,
        },
        {
            "name": "filled_markout_coverage",
            "passed": filled_markout_coverage == 1.0,
            "actual": filled_markout_coverage,
            "required": 1.0,
        },
        {
            "name": "unfilled_opportunity_coverage",
            "passed": unfilled_opportunity_coverage == 1.0,
            "actual": unfilled_opportunity_coverage,
            "required": 1.0,
        },
    ]
    if gross_edge_budget_bps is not None:
        checks.append(
            {
                "name": "all_in_execution_drag_within_gross_edge",
                "passed": all_in_drag_bps is not None and all_in_drag_bps < gross_edge_budget_bps,
                "actual": all_in_drag_bps,
                "required_max_exclusive": gross_edge_budget_bps,
            }
        )
    return {
        "basis": "one final row per submitted order; quantity-weighted fill and liquidity metrics",
        "orders": len(records),
        "fully_filled_orders": filled_orders,
        "partially_filled_orders": partial_orders,
        "unfilled_orders": unfilled_orders,
        "submitted_qty": submitted_qty,
        "filled_qty": filled_qty,
        "fill_rate_by_qty": fill_rate,
        "maker_share_by_filled_qty": maker_share,
        "taker_share_by_filled_qty": taker_qty / filled_qty if filled_qty else 0.0,
        "filled_notional": filled_notional,
        "orders_with_fills": orders_with_fills,
        "filled_markout_observations": filled_markout_observations,
        "filled_markout_coverage_ratio": filled_markout_coverage,
        "orders_with_unfilled_qty": orders_with_unfilled_qty,
        "unfilled_opportunity_observations": unfilled_opportunity_observations,
        "unfilled_opportunity_coverage_ratio": unfilled_opportunity_coverage,
        "fees": fees,
        "effective_fee_bps": fee_bps,
        "signed_slippage_bps": slippage_bps,
        "fee_plus_signed_slippage_bps": execution_cost_bps,
        "unfilled_opportunity_cost_bps": missed_opportunity_bps,
        "post_fill_adverse_markout_5m_bps": adverse_markout_bps,
        "conservative_all_in_execution_drag_bps": all_in_drag_bps,
        "checks": checks,
        "status": "PASS" if all(check["passed"] for check in checks) else "REVIEW",
    }
