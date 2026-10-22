"""Non-promotional integrity checks for an in-progress public-L2 campaign."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping


def summarize_shadow_campaign_progress(
    rows: Iterable[Mapping[str, Any]],
    *,
    symbols: Iterable[str],
    orders_per_symbol: int,
    campaign_status: str,
) -> dict[str, Any]:
    """Validate observed rows without estimating alpha or execution suitability.

    Rows are deliberately inspected only for identity, causal queue evidence and
    completion fields.  This is safe to run while a campaign is accumulating;
    it cannot produce a cost-policy update or any promotion decision.
    """
    symbol_list = [str(symbol).upper() for symbol in symbols]
    if not symbol_list or len(set(symbol_list)) != len(symbol_list):
        raise ValueError("symbols must be a non-empty unique list")
    if orders_per_symbol < 1:
        raise ValueError("orders_per_symbol must be positive")

    counts: Counter[str] = Counter()
    seen_ids: set[str] = set()
    failures: list[str] = []
    fills = 0
    rows_seen = 0
    for index, row in enumerate(rows):
        rows_seen += 1
        prefix = f"row[{index}]"
        symbol = str(row.get("symbol") or "").upper()
        signal_id = str(row.get("signal_id") or "")
        if symbol not in symbol_list:
            failures.append(f"{prefix}:unknown_symbol")
            continue
        counts[symbol] += 1
        if counts[symbol] > orders_per_symbol:
            failures.append(f"{prefix}:symbol_order_limit_exceeded")
        if not signal_id:
            failures.append(f"{prefix}:missing_signal_id")
        elif signal_id in seen_ids:
            failures.append(f"{prefix}:duplicate_signal_id")
        else:
            seen_ids.add(signal_id)
        if row.get("probe_kind") != "public_queue_estimate_no_order_submitted":
            failures.append(f"{prefix}:unexpected_probe_kind")
        if row.get("trigger_kind") != "periodic_calibration":
            failures.append(f"{prefix}:unexpected_trigger_kind")
        if row.get("side") != "buy":
            failures.append(f"{prefix}:unexpected_side")
        if row.get("aggregate_trade_ids_continuous") is not True:
            failures.append(f"{prefix}:aggregate_trade_ids_not_continuous")
        try:
            submitted = float(row["submitted_qty"])
            filled = float(row["filled_qty"])
        except (KeyError, TypeError, ValueError):
            failures.append(f"{prefix}:invalid_quantity")
            continue
        if submitted <= 0 or filled < 0 or filled > submitted:
            failures.append(f"{prefix}:invalid_fill_quantity")
            continue
        if filled > 0:
            fills += 1
            if not row.get("mid_after_5m"):
                failures.append(f"{prefix}:filled_row_missing_markout")
        elif not row.get("mid_after_timeout"):
            failures.append(f"{prefix}:unfilled_row_missing_timeout_mid")

    total_target = len(symbol_list) * orders_per_symbol
    # A final row can be written just before the supervisor seals its aggregate
    # source and manifest.  Do not call that narrow interval "complete": the
    # sealed supervisor state, not a row count, is the authority for running a
    # final cost report.
    completed = str(campaign_status) == "COMPLETE" and rows_seen == total_target
    return {
        "schema_version": 1,
        "kind": "SHADOW_ENTRY_CAMPAIGN_PROGRESS_DIAGNOSTIC",
        "campaign_status": str(campaign_status),
        "source_scope": "PUBLIC_L2_PERIODIC_CALIBRATION_ROWS_ONLY",
        "rows_seen": rows_seen,
        "target_rows": total_target,
        "filled_rows": fills,
        "per_symbol": {
            symbol: {"rows": counts[symbol], "target": orders_per_symbol}
            for symbol in symbol_list
        },
        "integrity_status": "PASS" if not failures else "FAIL",
        "integrity_failures": failures,
        "campaign_complete": completed,
        "cost_suitability_determined": False,
        "performance_determined": False,
        "promotion_capability": "DIAGNOSTIC_ONLY",
        "live_order_authorization": "CLOSED",
        "warning": (
            "This is an in-progress integrity diagnostic. It does not estimate "
            "alpha, approve execution costs, or authorize promotion."
        ),
    }
