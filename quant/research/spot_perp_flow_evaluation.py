"""Streaming, fail-closed observation extraction for Alpha 016.

The official aggregate-trade archives are too large to treat a whole quarter as
an in-memory dataframe.  This module reads an already-admitted chronological
stream once, retaining only fixed decision-window aggregates and virtual
reference fills.  It does not fetch data or submit orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from quant.research.spot_perp_flow_divergence import SpotPerpFlowConfig


@dataclass(frozen=True)
class SpotReference:
    entry_time_ms: int
    entry_price: float
    exit_time_ms: int
    exit_price: float


class SpotPerpFlowEvaluationError(RuntimeError):
    pass


def _validated_row(raw: dict[str, Any], *, market: str) -> tuple[int, int, float, float, bool]:
    try:
        trade_id = int(raw["agg_trade_id"])
        timestamp = int(raw["transact_time"])
        price = float(raw["price"])
        quantity = float(raw["quantity"])
        buyer_maker = raw["is_buyer_maker"]
    except (KeyError, TypeError, ValueError) as error:
        raise SpotPerpFlowEvaluationError(f"{market} row has invalid schema") from error
    if not isinstance(buyer_maker, bool) or trade_id < 0 or timestamp < 0 or price <= 0 or quantity <= 0:
        raise SpotPerpFlowEvaluationError(f"{market} row has invalid values")
    return trade_id, timestamp, price, quantity, buyer_maker


def extract_market_observations(
    rows: Iterable[dict[str, Any]],
    *,
    market: str,
    boundaries_ms: list[int],
    coverage_start_ms: int,
    coverage_end_ms: int,
    config: SpotPerpFlowConfig = SpotPerpFlowConfig(),
    spot_references: bool = False,
) -> dict[str, Any]:
    """Extract completed-window imbalance and, for spot, bounded fill references.

    The caller supplies all phase boundaries before opening data.  Every
    boundary receives a complete ten-minute feature interval; every spot
    boundary receives a first trade no more than one minute after its scheduled
    entry and after its actual-entry-based exit.  A missing trade interval is a
    phase failure, never a forward-filled virtual price.
    """
    if market not in {"spot", "perpetual"}:
        raise SpotPerpFlowEvaluationError("market must be spot or perpetual")
    boundaries = sorted(boundaries_ms)
    if not boundaries or boundaries != list(dict.fromkeys(boundaries)):
        raise SpotPerpFlowEvaluationError("boundaries must be non-empty and unique")
    if coverage_end_ms <= coverage_start_ms:
        raise SpotPerpFlowEvaluationError("coverage interval is invalid")

    totals = {boundary: 0.0 for boundary in boundaries}
    signed = {boundary: 0.0 for boundary in boundaries}
    references: dict[int, dict[str, Any]] = {}
    feature_index = 0
    entry_index = 0
    pending_exits: list[tuple[int, int]] = []  # (scheduled exit, boundary)
    previous_id: int | None = None
    previous_time: int | None = None
    coverage_previous = coverage_start_ms
    saw_coverage_row = False

    for raw in rows:
        trade_id, timestamp, price, quantity, buyer_maker = _validated_row(raw, market=market)
        if previous_id is not None and trade_id != previous_id + 1:
            raise SpotPerpFlowEvaluationError(f"{market} aggregate-trade IDs are not continuous")
        if previous_time is not None and timestamp < previous_time:
            raise SpotPerpFlowEvaluationError(f"{market} aggregate-trade timestamps are not monotonic")
        previous_id, previous_time = trade_id, timestamp

        if coverage_start_ms <= timestamp < coverage_end_ms:
            if timestamp - coverage_previous > config.maximum_intertrade_gap_ms:
                raise SpotPerpFlowEvaluationError(f"{market} aggregate-trade coverage gap exceeds maximum")
            coverage_previous = timestamp
            saw_coverage_row = True

        # The ten-minute windows are separated by four hours.  Advancing this
        # pointer makes extraction O(rows + boundaries), rather than scanning
        # every one of the quarter's boundaries for each aggregate trade.
        while feature_index < len(boundaries) and timestamp >= boundaries[feature_index]:
            feature_index += 1
        if feature_index < len(boundaries):
            boundary = boundaries[feature_index]
            if timestamp >= boundary - config.observation_window_ms:
                totals[boundary] += quantity
                signed[boundary] += (-quantity if buyer_maker else quantity)

        if not spot_references:
            continue

        while entry_index < len(boundaries):
            boundary = boundaries[entry_index]
            scheduled_entry = boundary + config.entry_delay_ms
            if timestamp < scheduled_entry:
                break
            if timestamp - scheduled_entry > config.maximum_reference_delay_ms:
                raise SpotPerpFlowEvaluationError("spot entry reference delay exceeds maximum")
            references[boundary] = {"entry_time_ms": timestamp, "entry_price": price}
            pending_exits.append((timestamp + config.holding_ms, boundary))
            entry_index += 1

        while pending_exits and timestamp >= pending_exits[0][0]:
            scheduled_exit, boundary = pending_exits[0]
            if timestamp - scheduled_exit > config.maximum_reference_delay_ms:
                raise SpotPerpFlowEvaluationError("spot exit reference delay exceeds maximum")
            references[boundary].update({"exit_time_ms": timestamp, "exit_price": price})
            pending_exits.pop(0)

    if not saw_coverage_row or coverage_end_ms - coverage_previous > config.maximum_intertrade_gap_ms:
        raise SpotPerpFlowEvaluationError(f"{market} aggregate-trade coverage gap exceeds maximum")
    incomplete = [boundary for boundary, total in totals.items() if total <= 0]
    if incomplete:
        raise SpotPerpFlowEvaluationError("aggregate-trade feature window is incomplete")
    if spot_references:
        if entry_index != len(boundaries) or pending_exits:
            raise SpotPerpFlowEvaluationError("spot entry or exit reference price is unavailable")
        if any(set(reference) != {"entry_time_ms", "entry_price", "exit_time_ms", "exit_price"} for reference in references.values()):
            raise SpotPerpFlowEvaluationError("spot reference is incomplete")

    return {
        "market": market,
        "coverage": {
            "status": "PASS",
            "start_ms": coverage_start_ms,
            "end_ms": coverage_end_ms,
            "maximum_intertrade_gap_ms": config.maximum_intertrade_gap_ms,
        },
        "imbalances": {boundary: signed[boundary] / totals[boundary] for boundary in boundaries},
        "spot_references": {
            boundary: SpotReference(**reference) for boundary, reference in references.items()
        },
    }
