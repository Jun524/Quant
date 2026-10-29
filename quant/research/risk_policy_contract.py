"""Fail-closed consistency checks for the capital-preservation policy artifacts."""

from __future__ import annotations

from re import fullmatch
from typing import Any


REQUIRED_POLICY_ID = "CAPITAL_PRESERVATION_RISK_POLICY_V1"


def validate_risk_policy_contract(
    risk_policy: dict[str, Any],
    capital_objective: dict[str, Any],
    *,
    portfolio_promotion_policy: dict[str, Any] | None = None,
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
        "non_positive_equity": False,
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
        "rule_violations": "rule_violations",
        "non_positive_equity": "non_positive_equity",
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
    if portfolio_promotion_policy is not None:
        # Import only for the portfolio status path.  Historical diagnostics
        # intentionally do not depend on a future portfolio implementation.
        from quant.research.portfolio_promotion import (
            PORTFOLIO_COST_AFTER_POLICY,
            PortfolioPromotionPolicy,
        )
        from quant.research.portfolio_risk import PortfolioRiskPolicy

        portfolio_machine = portfolio_promotion_policy.get("machine_policy") or {}
        portfolio_requirements = portfolio_promotion_policy.get(
            "portfolio_requirements"
        ) or {}
        if (
            portfolio_promotion_policy.get("policy")
            != "CAPITAL_PRESERVATION_PORTFOLIO_PROMOTION_V20"
        ):
            failures.append("portfolio promotion policy is not the current V19 policy")
        portfolio_expected = {
            "maximum_portfolio_historical_drawdown": non_negotiables.get(
                "maximum_cost_after_mtm_drawdown"
            ),
            "maximum_simultaneous_cost_stress_drawdown": non_negotiables.get(
                "maximum_cost_stress_mtm_drawdown"
            ),
            "maximum_portfolio_rule_violations": non_negotiables.get(
                "rule_violations"
            ),
            "portfolio_risk_annual_target_return": 0.0,
        }
        for key, value in portfolio_expected.items():
            if portfolio_machine.get(key) != value:
                failures.append(f"portfolio promotion policy {key} is inconsistent")
        if portfolio_requirements.get("zero_liquidations") is not True:
            failures.append("portfolio promotion policy must require zero liquidations")
        if portfolio_requirements.get("zero_rule_violations") is not True:
            failures.append("portfolio promotion policy must require zero rule violations")
        if (
            portfolio_requirements.get("combined_cost_after_expectancy_gate")
            != "PASS_WITH_POSITIVE_LONG_HORIZON_COST_AFTER_EXPECTANCY;_NO_MONTHLY_RETURN_TARGET"
        ):
            failures.append(
                "portfolio promotion policy must require cost-after expectancy without a monthly target"
            )
        if "combined_monthly_target_gate" in portfolio_requirements:
            failures.append("portfolio promotion policy must not retain a monthly target gate")
        code_expected = {
            "target_geometric_monthly_return": None,
            "require_geometric_monthly_return": False,
            "require_positive_month_ratio": False,
            "require_positive_cost_after_return": True,
            "maximum_drawdown": non_negotiables.get(
                "maximum_cost_after_mtm_drawdown"
            ),
            "maximum_leverage": non_negotiables.get("maximum_planned_leverage"),
            "maximum_worst_month_loss": 0.05,
        }
        for key, value in code_expected.items():
            if getattr(PORTFOLIO_COST_AFTER_POLICY, key) != value:
                failures.append(
                    f"portfolio promotion code {key} is inconsistent with the preservation objective"
                )
        if PortfolioPromotionPolicy().portfolio_risk_annual_target_return != 0.0:
            failures.append(
                "portfolio promotion code must use zero as the descriptive annual-return boundary"
            )
        if PortfolioRiskPolicy().annual_target_return != 0.0:
            failures.append(
                "portfolio risk code must use zero as the descriptive annual-return boundary"
            )
    return {
        "status": "PASS" if not failures else "FAIL",
        "policy_id": risk_policy.get("policy_id"),
        "policy_version": risk_policy.get("policy_version"),
        "live_order_authorization": "CLOSED",
        "failures": failures,
    }
