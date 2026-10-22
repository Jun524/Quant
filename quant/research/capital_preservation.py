"""Mechanical gate for historical capital-preservation diagnostics.

The gate deliberately evaluates Freqtrade's *organic* metrics, which exclude
the artificial force-exit used to close an unfinished backtest.  A profitable
open position at the end of a chosen date range is not evidence that a signal
has a reproducible exit.
"""

from __future__ import annotations

from typing import Any, Mapping


class CapitalPreservationPolicyError(ValueError):
    """The separate capital-preservation objective is incomplete or unsafe."""


def _check(name: str, passed: bool, *, required: Any, actual: Any, kind: str) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "required": required,
        "actual": actual,
        "kind": kind,
    }


def _number(section: Mapping[str, Any], name: str) -> float:
    try:
        return float(section[name])
    except (KeyError, TypeError, ValueError) as error:
        raise CapitalPreservationPolicyError(f"capital-preservation policy missing numeric {name}") from error


def _organic(result: Mapping[str, Any]) -> Mapping[str, Any]:
    organic = result.get("organic")
    if not isinstance(organic, Mapping):
        raise CapitalPreservationPolicyError("result is missing organic metrics")
    return organic


def _adverse_funding_net_return(result: Mapping[str, Any]) -> float | None:
    """Remove favorable funding and double unfavorable funding conservatively.

    Recorded organic net PnL already includes signed funding.  Replacing its
    contribution ``f`` with ``f - abs(f)`` removes a funding subsidy when
    ``f`` is positive and adds one further paid-funding amount when it is
    negative.  Missing or invalid accounting fails closed.
    """
    try:
        starting_balance = float(result["starting_balance"])
        funding = float(result["funding_net"])
        net_return = float(_organic(result)["net_return"])
    except (KeyError, TypeError, ValueError):
        return None
    if starting_balance <= 0:
        return None
    return net_return - abs(funding) / starting_balance


def _combined_cost_drawdown_upper_bound(result: Mapping[str, Any]) -> float | None:
    """Bound stress drawdown when detailed intraday slippage timing is unavailable.

    The Freqtrade stress run supplies the double-fee mark-to-market drawdown.
    Allocating the complete modeled slippage drag to that worst point gives a
    deliberately conservative upper bound rather than pretending to know the
    entry/exit ordering inside candles.
    """
    try:
        drawdown = float(result["max_drawdown"])
        slippage_drag = float((result["cost_drag"] or {})["slippage_drag_pct"])
    except (KeyError, TypeError, ValueError):
        return None
    if drawdown < 0.0 or slippage_drag < 0.0:
        return None
    return drawdown + slippage_drag


def evaluate_capital_preservation(
    development: Mapping[str, Any],
    development_stress: Mapping[str, Any],
    post_development: Mapping[str, Any],
    post_development_stress: Mapping[str, Any],
    *,
    objective: Mapping[str, Any],
    execution_contract: Mapping[str, Any],
    integrity: Mapping[str, Any],
    sample_role: str = "historical_diagnostic",
) -> dict[str, Any]:
    """Evaluate a fixed candidate without granting historical promotion.

    ``post_development`` is intentionally not called OOS here: callers must
    separately prove a locked, unseen partition before making that claim.
    """
    if objective.get("id") != "CAPITAL_PRESERVATION_RESEARCH_OBJECTIVE_V1":
        raise CapitalPreservationPolicyError("unexpected capital-preservation objective")
    requirements = objective.get("capital_preservation_requirements")
    if not isinstance(requirements, Mapping):
        raise CapitalPreservationPolicyError("capital-preservation requirements are missing")
    minimum_trades = int(_number(requirements, "minimum_organic_trades"))
    minimum_pf = _number(requirements, "minimum_profit_factor")
    maximum_drawdown = _number(requirements, "maximum_historical_drawdown")
    maximum_worst_month = _number(requirements, "maximum_worst_complete_month_loss")
    maximum_leverage = _number(requirements, "maximum_planned_leverage")
    maximum_risk = _number(requirements, "maximum_planned_account_risk_per_trade")
    maximum_positions = int(_number(requirements, "maximum_simultaneous_positions"))
    modeled_slippage_bps = _number(requirements, "modeled_slippage_bps")
    maximum_cost_stress_drawdown = _number(requirements, "maximum_combined_cost_stress_drawdown")

    dev = _organic(development)
    dev_stress = _organic(development_stress)
    post = _organic(post_development)
    post_stress = _organic(post_development_stress)
    total_trades = int(dev.get("trades") or 0) + int(post.get("trades") or 0)
    dev_adverse_funding = _adverse_funding_net_return(development_stress)
    post_adverse_funding = _adverse_funding_net_return(post_development_stress)
    dev_cost_drawdown = _combined_cost_drawdown_upper_bound(development_stress)
    post_cost_drawdown = _combined_cost_drawdown_upper_bound(post_development_stress)
    checks = [
        _check("objective_active", objective.get("status") == "ACTIVE_FOR_NEW_HISTORICAL_DIAGNOSTICS", required="ACTIVE_FOR_NEW_HISTORICAL_DIAGNOSTICS", actual=objective.get("status"), kind="governance"),
        _check("live_order_authorization_closed", objective.get("live_order_authorization") == "CLOSED", required="CLOSED", actual=objective.get("live_order_authorization"), kind="governance"),
        _check("development_organic_net_positive", float(dev.get("net_return") or 0.0) > 0.0, required="> 0", actual=dev.get("net_return"), kind="performance"),
        _check("development_profit_factor", dev.get("profit_factor") is not None and float(dev["profit_factor"]) >= minimum_pf, required=f">= {minimum_pf}", actual=dev.get("profit_factor"), kind="performance"),
        _check("development_combined_fee_slippage_model", float(development_stress.get("modeled_slippage_bps") or -1.0) == modeled_slippage_bps, required=modeled_slippage_bps, actual=development_stress.get("modeled_slippage_bps"), kind="cost"),
        _check("development_combined_fee_slippage_net_positive", float(dev_stress.get("net_return") or 0.0) > 0.0, required="> 0", actual=dev_stress.get("net_return"), kind="cost"),
        _check("development_combined_cost_stress_drawdown", dev_cost_drawdown is not None and dev_cost_drawdown <= maximum_cost_stress_drawdown, required=f"<= {maximum_cost_stress_drawdown} conservative upper bound", actual=dev_cost_drawdown, kind="cost"),
        _check("development_adverse_funding_net_positive", dev_adverse_funding is not None and dev_adverse_funding > 0.0, required="> 0 after removing favorable funding and doubling paid funding", actual=dev_adverse_funding, kind="cost"),
        _check("post_development_organic_net_positive", float(post.get("net_return") or 0.0) > 0.0, required="> 0", actual=post.get("net_return"), kind="performance"),
        _check("post_development_profit_factor", post.get("profit_factor") is not None and float(post["profit_factor"]) >= minimum_pf, required=f">= {minimum_pf}", actual=post.get("profit_factor"), kind="performance"),
        _check("post_development_combined_fee_slippage_model", float(post_development_stress.get("modeled_slippage_bps") or -1.0) == modeled_slippage_bps, required=modeled_slippage_bps, actual=post_development_stress.get("modeled_slippage_bps"), kind="cost"),
        _check("post_development_combined_fee_slippage_net_positive", float(post_stress.get("net_return") or 0.0) > 0.0, required="> 0", actual=post_stress.get("net_return"), kind="cost"),
        _check("post_development_combined_cost_stress_drawdown", post_cost_drawdown is not None and post_cost_drawdown <= maximum_cost_stress_drawdown, required=f"<= {maximum_cost_stress_drawdown} conservative upper bound", actual=post_cost_drawdown, kind="cost"),
        _check("post_development_adverse_funding_net_positive", post_adverse_funding is not None and post_adverse_funding > 0.0, required="> 0 after removing favorable funding and doubling paid funding", actual=post_adverse_funding, kind="cost"),
        _check("organic_trade_sample", total_trades >= minimum_trades, required=minimum_trades, actual=total_trades, kind="sample"),
        _check("maximum_historical_drawdown", all(float(result.get("max_drawdown") or 1.0) <= maximum_drawdown for result in (development, post_development)), required=f"<= {maximum_drawdown}", actual={"development": development.get("max_drawdown"), "post_development": post_development.get("max_drawdown")}, kind="risk"),
        _check("worst_complete_month_loss", all(result.get("monthly", {}).get("worst_month") is not None and float(result["monthly"]["worst_month"]) >= -maximum_worst_month for result in (development, post_development)), required=f">= {-maximum_worst_month}", actual={"development": (development.get("monthly") or {}).get("worst_month"), "post_development": (post_development.get("monthly") or {}).get("worst_month")}, kind="risk"),
        _check("planned_leverage", float(execution_contract.get("leverage") or 0.0) <= maximum_leverage, required=f"<= {maximum_leverage}", actual=execution_contract.get("leverage"), kind="risk"),
        _check("planned_risk_per_trade", float(execution_contract.get("planned_account_risk_per_trade") or 1.0) <= maximum_risk, required=f"<= {maximum_risk}", actual=execution_contract.get("planned_account_risk_per_trade"), kind="risk"),
        _check("simultaneous_positions", int(execution_contract.get("maximum_simultaneous_positions") or 0) <= maximum_positions, required=f"<= {maximum_positions}", actual=execution_contract.get("maximum_simultaneous_positions"), kind="risk"),
        _check("adverse_position_adds_forbidden", execution_contract.get("adverse_position_adds") is False, required=False, actual=execution_contract.get("adverse_position_adds"), kind="risk"),
        _check("liquidation_exits", all(int((result.get("trade_risk") or {}).get("liquidation_exits") or 0) == 0 for result in (development, post_development)), required=0, actual={"development": (development.get("trade_risk") or {}).get("liquidation_exits"), "post_development": (post_development.get("trade_risk") or {}).get("liquidation_exits")}, kind="risk"),
        _check("lookahead_analysis", integrity.get("lookahead") == "PASS", required="PASS", actual=integrity.get("lookahead"), kind="integrity"),
        _check("recursive_analysis", integrity.get("recursive") == "PASS", required="PASS", actual=integrity.get("recursive"), kind="integrity"),
    ]
    hard_kinds = {"performance", "cost", "risk", "integrity"}
    failures = [item["name"] for item in checks if not item["passed"]]
    hard_failure = any(not item["passed"] for item in checks if item["kind"] in hard_kinds)
    sample_ready = all(item["passed"] for item in checks if item["kind"] == "sample")
    if hard_failure:
        status = "FAIL"
    elif not sample_ready:
        status = "REVIEW"
    else:
        status = "DIAGNOSTIC_PASS_NOT_PROMOTABLE"
    return {
        "status": status,
        "promotion_eligible": False,
        "sample_role": sample_role,
        "checks": checks,
        "failures": failures,
        "aspirational_monthly_target": (objective.get("return_objective") or {}).get("aspirational_net_geometric_monthly_return"),
        "warning": "A historical diagnostic cannot authorize live trading or replace separately locked forward evidence.",
    }
