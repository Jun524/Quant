"""Fail-closed partial-fill economics for the frozen Alpha #7 hypothesis."""

from __future__ import annotations

from collections import defaultdict
from statistics import mean

from quant.research.microstructure import _bootstrap_hourly_lower


def _path_summary(records: list[dict], path: str) -> dict:
    selected = [item for item in records if item["path"] == path]
    available = [item for item in selected if item["execution_available"]]
    realized = [item for item in available if item["fill_fraction"] > 0]
    complete = [item for item in available if item["net_return"] is not None]
    contributions = [
        float(item["fill_fraction"]) * float(item["net_return"])
        if item["net_return"] is not None
        else None
        for item in available
    ]
    valid_contributions = [value for value in contributions if value is not None]
    hourly: dict[str, list[float]] = defaultdict(list)
    for item, value in zip(available, contributions):
        if value is not None:
            hourly[str(item["hour"])].append(value)
    fill_weight = sum(float(item["fill_fraction"]) for item in complete)
    executed_weighted = (
        sum(
            float(item["fill_fraction"]) * float(item["net_return"])
            for item in complete
        )
        / fill_weight
        if fill_weight > 0
        else None
    )
    return {
        "eligible_orders": len(selected),
        "execution_available_orders": len(available),
        "execution_coverage_ratio": len(available) / len(selected) if selected else 0.0,
        "executed_exposures": len(realized),
        "partial_fill_orders": sum(
            0 < float(item["fill_fraction"]) < 1 - 1e-12 for item in available
        ),
        "fully_filled_orders": sum(
            float(item["fill_fraction"]) >= 1 - 1e-12 for item in available
        ),
        "exit_quote_coverage_ratio": len(complete) / len(available) if available else 0.0,
        "mean_submitted_notional_contribution": (
            mean(valid_contributions) if valid_contributions else None
        ),
        "executed_notional_weighted_net_return": executed_weighted,
        "hour_block_bootstrap_95pct_lower_contribution": _bootstrap_hourly_lower(hourly),
    }


def evaluate_partial_fill_stress(
    records: list[dict],
    *,
    primary_status: str,
    complete_days: int,
    policy: dict,
) -> dict:
    """Evaluate a fixed veto gate from one normalized row per signal/path."""
    if policy.get("candidate") != "ALPHA_007_IMBALANCE_GUIDED_MAKER":
        raise ValueError("partial-fill policy candidate mismatch")
    accounting = policy.get("position_accounting") or {}
    requirements = policy.get("locked_requirements") or {}
    if (
        float(accounting.get("round_trip_cost_bps") or 0) != 10.0
        or accounting.get("include_every_positive_estimated_fill_quantity") is not True
        or accounting.get("unfilled_orders_contribute_zero") is not True
    ):
        raise ValueError("partial-fill accounting policy is not the locked V1 policy")
    base = _path_summary(records, "base")
    latency = _path_summary(records, "latency_100ms")
    base_rows = [
        item
        for item in records
        if item["path"] == "base" and item["execution_available"]
    ]
    partial_rows = [
        item
        for item in base_rows
        if 0 < float(item["fill_fraction"]) < 1 - 1e-12
        and item["net_return"] is not None
    ]
    partial_contribution = sum(
        float(item["fill_fraction"]) * float(item["net_return"])
        for item in partial_rows
    )

    side_summaries: dict[str, dict] = {}
    for side in ("buy", "sell"):
        rows = [item for item in base_rows if item["side"] == side]
        valid = [
            float(item["fill_fraction"]) * float(item["net_return"])
            for item in rows
            if item["net_return"] is not None
        ]
        side_summaries[side] = {
            "eligible_orders": len(rows),
            "mean_submitted_notional_contribution": mean(valid) if valid else None,
        }

    minimum_side = int(requirements["minimum_eligible_orders_per_side"])
    checks = [
        {
            "name": "primary_alpha007_pass",
            "passed": primary_status == "PASS",
            "actual": primary_status,
            "required": "PASS",
        },
        {
            "name": "complete_days",
            "passed": complete_days >= int(requirements["complete_days_minimum"]),
            "actual": complete_days,
            "required": int(requirements["complete_days_minimum"]),
        },
        {
            "name": "base_execution_coverage",
            "passed": base["execution_coverage_ratio"]
            == float(requirements["base_execution_coverage_ratio"]),
            "actual": base["execution_coverage_ratio"],
            "required": float(requirements["base_execution_coverage_ratio"]),
        },
        {
            "name": "latency_execution_coverage",
            "passed": latency["execution_coverage_ratio"]
            >= float(requirements["latency_execution_coverage_ratio_minimum"]),
            "actual": latency["execution_coverage_ratio"],
            "required_min": float(requirements["latency_execution_coverage_ratio_minimum"]),
        },
        {
            "name": "base_exit_quote_coverage",
            "passed": base["exit_quote_coverage_ratio"] == 1.0,
            "actual": base["exit_quote_coverage_ratio"],
            "required": 1.0,
        },
        {
            "name": "latency_exit_quote_coverage",
            "passed": latency["exit_quote_coverage_ratio"] == 1.0,
            "actual": latency["exit_quote_coverage_ratio"],
            "required": 1.0,
        },
        {
            "name": "executed_exposures",
            "passed": base["executed_exposures"]
            >= int(requirements["executed_exposures_minimum"]),
            "actual": base["executed_exposures"],
            "required": int(requirements["executed_exposures_minimum"]),
        },
        {
            "name": "base_mean_contribution",
            "passed": base["mean_submitted_notional_contribution"] is not None
            and base["mean_submitted_notional_contribution"] > 0,
            "actual": base["mean_submitted_notional_contribution"],
            "required": "> 0",
        },
        {
            "name": "base_hour_block_bootstrap",
            "passed": base["hour_block_bootstrap_95pct_lower_contribution"] is not None
            and base["hour_block_bootstrap_95pct_lower_contribution"] > 0,
            "actual": base["hour_block_bootstrap_95pct_lower_contribution"],
            "required": "> 0",
        },
        {
            "name": "latency_mean_contribution",
            "passed": latency["mean_submitted_notional_contribution"] is not None
            and latency["mean_submitted_notional_contribution"] > 0,
            "actual": latency["mean_submitted_notional_contribution"],
            "required": "> 0",
        },
        {
            "name": "partial_fill_total_contribution",
            "passed": partial_contribution >= 0,
            "actual": partial_contribution,
            "required": ">= 0",
        },
    ]
    for side, label in (("buy", "long"), ("sell", "short")):
        summary = side_summaries[side]
        checks.extend(
            [
                {
                    "name": f"{label}_eligible_orders",
                    "passed": summary["eligible_orders"] >= minimum_side,
                    "actual": summary["eligible_orders"],
                    "required": minimum_side,
                },
                {
                    "name": f"{label}_mean_contribution",
                    "passed": summary["mean_submitted_notional_contribution"] is not None
                    and summary["mean_submitted_notional_contribution"] > 0,
                    "actual": summary["mean_submitted_notional_contribution"],
                    "required": "> 0",
                },
            ]
        )

    status = (
        "ACCUMULATING"
        if complete_days < int(requirements["complete_days_minimum"])
        else ("PASS" if all(item["passed"] for item in checks) else "FAIL")
    )
    return {
        "candidate": policy["candidate"],
        "status": status,
        "promotion_eligible": False,
        "primary_status": primary_status,
        "complete_days": complete_days,
        "base": base,
        "latency_100ms": latency,
        "by_side": side_summaries,
        "partial_fill_total_contribution": partial_contribution,
        "checks": checks,
        "warning": "This auxiliary result can veto but never rescue or promote Alpha #7.",
    }
