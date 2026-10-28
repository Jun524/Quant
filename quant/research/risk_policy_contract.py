"""Fail-closed consistency checks for the capital-preservation policy artifacts."""

from __future__ import annotations

from re import fullmatch
from typing import Any


REQUIRED_POLICY_ID = "CAPITAL_PRESERVATION_RISK_POLICY_V1"


def validate_risk_policy_contract(
    risk_policy: dict[str, Any], capital_objective: dict[str, Any]
) -> dict[str, Any]:
    """Return explicit failures when the human policy's machine artifacts drift.

    This does not authorize orders.  It protects the research boundary by
    making contradictory policy files a failed status rather than silently
    selecting the more permissive value.
    """
    failures: list[str] = []
    if risk_policy.get("policy_id") != REQUIRED_POLICY_ID:
        failures.append("risk policy identifier is not current")
    policy_version = risk_policy.get("policy_version")
    if not isinstance(policy_version, str) or fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", policy_version) is None:
        failures.append("risk policy version must use semantic versioning")
    if risk_policy.get("live_order_authorization") != "CLOSED":
        failures.append("risk policy must keep live order authorization CLOSED")
    if capital_objective.get("live_order_authorization") != "CLOSED":
        failures.append("capital objective must keep live order authorization CLOSED")
    if risk_policy.get("primary_objective") != (
        "Validate positive long-horizon cost-after expectancy while preserving capital; "
        "a monthly return target is not a pass/fail gate."
    ):
        failures.append("risk policy primary objective is inconsistent")
    objective = capital_objective.get("return_objective") or {}
    if objective.get("monthly_return_target") != "NOT_A_GATE":
        failures.append("capital objective must not contain a monthly return gate")
    if objective.get("promotion_requirement") != "positive_long_horizon_cost_after_expectancy":
        failures.append("capital objective promotion requirement is inconsistent")
    requirements = capital_objective.get("capital_preservation_requirements") or {}
    non_negotiables = risk_policy.get("non_negotiables") or {}
    expected = {
        "maximum_cost_after_mtm_drawdown": 0.1,
        "maximum_cost_stress_mtm_drawdown": 0.1,
        "liquidation_exits": 0,
        "rule_violations": 0,
        "maximum_planned_account_risk_per_trade": 0.0025,
        "maximum_planned_leverage": 1.0,
        "maximum_simultaneous_positions": 2,
        "adverse_position_adds": "FORBIDDEN",
        "cost_stress_net_return": "> 0",
        "lookahead_analysis": "PASS",
        "recursive_analysis": "PASS",
        "data_integrity": "PASS",
    }
    for key, value in expected.items():
        if non_negotiables.get(key) != value:
            failures.append(f"risk policy {key} is inconsistent")
    objective_mapping = {
        "maximum_historical_drawdown": "maximum_cost_after_mtm_drawdown",
        "maximum_combined_cost_stress_drawdown": "maximum_cost_stress_mtm_drawdown",
        "liquidation_exits": "liquidation_exits",
        "maximum_planned_account_risk_per_trade": "maximum_planned_account_risk_per_trade",
        "maximum_planned_leverage": "maximum_planned_leverage",
        "maximum_simultaneous_positions": "maximum_simultaneous_positions",
        "adverse_position_adds": "adverse_position_adds",
        "double_fee_plus_modeled_slippage_stress_net_return": "cost_stress_net_return",
        "data_integrity": "data_integrity",
        "lookahead_analysis": "lookahead_analysis",
        "recursive_analysis": "recursive_analysis",
    }
    for objective_key, policy_key in objective_mapping.items():
        if requirements.get(objective_key) != non_negotiables.get(policy_key):
            failures.append(f"capital objective {objective_key} disagrees with risk policy {policy_key}")
    return {
        "status": "PASS" if not failures else "FAIL",
        "policy_id": risk_policy.get("policy_id"),
        "policy_version": risk_policy.get("policy_version"),
        "live_order_authorization": "CLOSED",
        "failures": failures,
    }
