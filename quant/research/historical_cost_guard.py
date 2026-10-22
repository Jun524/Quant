"""Validate the fixed cost contract for historical diagnostic studies."""

from __future__ import annotations

from typing import Any, Mapping


class HistoricalCostPolicyError(ValueError):
    """Historical cost policy is incomplete or would understate execution cost."""


def _check(name: str, passed: bool, *, required: Any, actual: Any) -> dict[str, Any]:
    return {"name": name, "passed": passed, "required": required, "actual": actual}


def evaluate_historical_cost_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Validate economics without evaluating a strategy or reading returns."""
    if not isinstance(policy, Mapping):
        raise HistoricalCostPolicyError("historical cost policy must be an object")
    execution = policy.get("execution")
    funding = policy.get("funding")
    risk = policy.get("risk")
    if not all(isinstance(item, Mapping) for item in (execution, funding, risk)):
        raise HistoricalCostPolicyError("historical cost policy sections are missing")
    try:
        base_fee = float(execution["base_fee_per_fill"])
        stress_fee = float(execution["stress_fee_per_fill"])
        base_slippage = float(execution["base_slippage_per_fill"])
        stress_slippage = float(execution["stress_slippage_per_fill"])
        leverage = float(risk["maximum_gross_leverage"])
    except (KeyError, TypeError, ValueError) as error:
        raise HistoricalCostPolicyError("historical cost numeric fields are invalid") from error
    checks = [
        _check("schema_version", policy.get("schema_version") == 1, required=1, actual=policy.get("schema_version")),
        _check("preregistered", policy.get("status") == "PREREGISTERED", required="PREREGISTERED", actual=policy.get("status")),
        _check("live_closed", policy.get("live_order_authorization") == "CLOSED", required="CLOSED", actual=policy.get("live_order_authorization")),
        _check("next_open_entry", execution.get("entry_price") == "next available UTC daily open", required="next available UTC daily open", actual=execution.get("entry_price")),
        _check("next_open_exit", execution.get("exit_price") == "next available UTC daily open", required="next available UTC daily open", actual=execution.get("exit_price")),
        _check("round_trip_has_two_fills", execution.get("fills_per_round_trip") == 2, required=2, actual=execution.get("fills_per_round_trip")),
        _check("positive_base_costs", base_fee > 0 and base_slippage > 0, required="> 0", actual={"fee": base_fee, "slippage": base_slippage}),
        _check("stress_not_lower_than_base", stress_fee >= base_fee and stress_slippage >= base_slippage, required=">= base", actual={"base_fee": base_fee, "stress_fee": stress_fee, "base_slippage": base_slippage, "stress_slippage": stress_slippage}),
        _check("funding_checksum_source", "CHECKSUM" in str(funding.get("source") or ""), required="publisher CHECKSUM", actual=funding.get("source")),
        _check("missing_funding_rejects", funding.get("missing_data") == "REJECT_EVALUATION_DAY", required="REJECT_EVALUATION_DAY", actual=funding.get("missing_data")),
        _check("zero_funding_forbidden", funding.get("zero_funding_assumption") == "FORBIDDEN", required="FORBIDDEN", actual=funding.get("zero_funding_assumption")),
        _check("no_daily_ohlc_liquidation_claim", risk.get("liquidation_claim") == "FORBIDDEN_FROM_DAILY_OHLC_ONLY", required="FORBIDDEN_FROM_DAILY_OHLC_ONLY", actual=risk.get("liquidation_claim")),
        _check("gross_leverage_bounded", 0 < leverage <= 1.0, required="0 < leverage <= 1", actual=leverage),
        _check("diagnostic_only", risk.get("promotion_capability") == "DIAGNOSTIC_ONLY", required="DIAGNOSTIC_ONLY", actual=risk.get("promotion_capability")),
    ]
    return {
        "schema_version": 1,
        "status": "PASS" if all(item["passed"] for item in checks) else "FAIL",
        "kind": "HISTORICAL_COST_GOVERNANCE_ONLY",
        "checks": checks,
        "live_order_authorization": "CLOSED",
        "warning": "A PASS validates the accounting contract, not a strategy return or promotion decision.",
    }
