"""Fail-closed promotion gate for a future multi-strategy futures portfolio.

This module does not choose weights or authorize orders.  It verifies that a
pre-registered portfolio and every execution path it can emit have independent
locked-forward and execution-cost evidence before a separate live preflight is
even considered.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any

from quant.research.monthly_target import MonthlyTargetPolicy, evaluate_monthly_target
from quant.research.portfolio_composition import (
    PORTFOLIO_COMPOSITION_PROTOCOL_FILES,
)
from quant.research.portfolio_performance import (
    PORTFOLIO_PERFORMANCE_PROTOCOL_FILES,
)
from quant.research.portfolio_execution import (
    PORTFOLIO_EXECUTION_PROTOCOL_FILES,
)
from quant.research.portfolio_risk import (
    PORTFOLIO_RISK_PROTOCOL_FILES,
    PortfolioRiskPolicy,
    wilson_bounds,
)
from quant.research.selection_bias import (
    SELECTION_BIAS_PROTOCOL_FILES,
    SelectionBiasPolicy,
)


ORDER_ROLES = ("entry", "exit", "protective_stop")
POSITION_SIDES = ("long", "short")
ORDER_TYPES = ("maker_limit", "aggressive_limit", "market")
PROMOTION_PROTOCOL_FILES = (
    "quant/research/monthly_target.py",
    "quant/research/selection_bias.py",
    "scripts/selection_bias_audit.py",
    "quant/research/portfolio_risk.py",
    "scripts/portfolio_risk_audit.py",
    "quant/research/portfolio_composition.py",
    "quant/research/equity.py",
    "quant/research/portfolio_performance.py",
    "quant/research/portfolio_execution.py",
    "scripts/portfolio_execution_audit.py",
    "quant/research/portfolio_promotion.py",
    "scripts/portfolio_promotion_report.py",
    "research/portfolio-promotion-policy.json",
)
PORTFOLIO_MONTHLY_POLICY = MonthlyTargetPolicy(
    # The programme reports 10% as an ambition, but no longer rejects a
    # capital-preserving portfolio solely for missing that return target.
    # A tiny positive threshold avoids calling a flat portfolio an edge.
    target_geometric_monthly_return=1e-12,
    maximum_worst_month_loss=0.05,
    maximum_drawdown=0.10,
    maximum_probability_of_ruin=0.005,
    maximum_probability_terminal_loss=0.05,
    minimum_probability_reaching_annual_target=0.0,
    maximum_bootstrap_drawdown_p95=0.10,
    maximum_leverage=1.0,
    maximum_trade_adverse_excursion=0.20,
    require_slippage_stress_positive=True,
    require_funding_stress_positive=True,
)
REQUIRED_MONTHLY_CHECK_NAMES = frozenset(
    item["name"]
    for item in evaluate_monthly_target(
        {},
        double_cost=None,
        slippage_stress=None,
        funding_stress=None,
        risk=None,
        evidence={},
        policy=PORTFOLIO_MONTHLY_POLICY,
    )["checks"]
)


@dataclass(frozen=True)
class PortfolioPromotionPolicy:
    minimum_sleeves: int = 2
    maximum_sleeves: int = 5
    maximum_single_sleeve_weight: float = 0.70
    maximum_gross_allocation: float = 1.0
    minimum_common_daily_observations: int = 252
    maximum_absolute_pairwise_correlation: float = 0.80
    maximum_single_sleeve_positive_pnl_share: float = 0.75
    maximum_gross_leverage: float = 1.0
    maximum_absolute_net_leverage: float = 1.0
    maximum_portfolio_trade_leverage_adjusted_mae: float = 0.20
    maximum_portfolio_rule_violations: int = 0
    minimum_portfolio_daily_mtm_profit_factor: float = 1.20
    minimum_execution_observations_per_scope: int = 30
    minimum_passive_fills_per_scope: int = 10
    minimum_selection_bias_observations: int = 252
    minimum_deflated_sharpe_probability: float = 0.95
    maximum_probability_backtest_overfitting: float = 0.10
    selection_bias_cscv_slices: int = 8
    minimum_portfolio_risk_daily_returns: int = 365
    portfolio_risk_horizon_days: int = 365
    portfolio_risk_short_block_days: int = 7
    portfolio_risk_long_block_days: int = 28
    portfolio_risk_simulations_per_block: int = 5_000
    portfolio_risk_ruin_equity_fraction: float = 0.50
    portfolio_risk_drawdown_threshold: float = 0.10
    portfolio_risk_annual_target_return: float = 1.10**12 - 1
    portfolio_risk_binomial_confidence_level: float = 0.95
    portfolio_risk_seed: int = 1701
    maximum_portfolio_probability_of_ruin: float = 0.005
    maximum_portfolio_probability_terminal_loss: float = 0.05
    # Kept as a simulated diagnostic at the aspirational 10% monthly target,
    # not a pass/fail requirement.
    minimum_portfolio_probability_reaching_annual_target: float = 0.0
    maximum_portfolio_bootstrap_drawdown_p95: float = 0.10
    maximum_portfolio_historical_drawdown: float = 0.10
    maximum_simultaneous_cost_stress_drawdown: float = 0.10
    portfolio_risk_deterministic_daily_repeats: int = 3
    portfolio_risk_deterministic_weekly_repeats: int = 4
    portfolio_risk_deterministic_four_week_repeats: int = 2
    maximum_portfolio_deterministic_stress_drawdown: float = 0.10


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _sha256_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _action_side(position_side: str, order_role: str) -> str:
    if order_role == "entry":
        return "buy" if position_side == "long" else "sell"
    return "sell" if position_side == "long" else "buy"


def execution_scope_key(scope: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        str(scope.get("candidate") or ""),
        str(scope.get("symbol") or "").upper(),
        str(scope.get("position_side") or "").lower(),
        str(scope.get("order_role") or "").lower(),
        str(scope.get("action_side") or "").lower(),
        str(scope.get("order_type") or "").lower(),
    )


def expected_execution_scopes(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Expand every sleeve into symbol × direction × order-role coverage."""
    scopes: list[dict[str, Any]] = []
    failures: list[str] = []
    sleeves = manifest.get("sleeves")
    if not isinstance(sleeves, list):
        return [], ["portfolio manifest sleeves are missing"]
    seen_candidates: set[str] = set()
    for sleeve in sleeves:
        if not isinstance(sleeve, dict):
            failures.append("portfolio manifest contains an invalid sleeve")
            continue
        candidate = str(sleeve.get("candidate") or "")
        if not candidate or candidate in seen_candidates:
            failures.append("portfolio sleeve candidates must be unique and non-empty")
            continue
        seen_candidates.add(candidate)
        symbols = sleeve.get("symbols")
        sides = sleeve.get("position_sides")
        orders = sleeve.get("orders")
        if (
            not isinstance(symbols, list)
            or not symbols
            or len({str(item).upper() for item in symbols}) != len(symbols)
        ):
            failures.append(f"{candidate} symbols are missing or duplicated")
            continue
        if (
            not isinstance(sides, list)
            or not sides
            or set(str(item).lower() for item in sides) - set(POSITION_SIDES)
            or len({str(item).lower() for item in sides}) != len(sides)
        ):
            failures.append(f"{candidate} position sides are missing, duplicated, or invalid")
            continue
        if not isinstance(orders, dict) or set(orders) != set(ORDER_ROLES):
            failures.append(f"{candidate} must define entry, exit, and protective_stop orders")
            continue
        parsed_orders: dict[str, tuple[str, float, str, float]] = {}
        for role in ORDER_ROLES:
            order = orders.get(role)
            if not isinstance(order, dict):
                failures.append(f"{candidate} {role} order definition is invalid")
                continue
            order_type = str(order.get("order_type") or "").lower()
            budget = order.get("maximum_all_in_drag_bps")
            trigger_kind = str(order.get("trigger_kind") or "")
            fee_bps = order.get("fee_bps")
            if (
                order_type not in ORDER_TYPES
                or not _finite_number(budget)
                or float(budget) <= 0
                or not _finite_number(fee_bps)
                or float(fee_bps) < 0
            ):
                failures.append(
                    f"{candidate} {role} order type, fee, or drag budget is invalid"
                )
                continue
            if not trigger_kind:
                failures.append(f"{candidate} {role} trigger kind is missing")
                continue
            parsed_orders[role] = (
                order_type,
                float(budget),
                trigger_kind,
                float(fee_bps),
            )
        if len(parsed_orders) != len(ORDER_ROLES):
            continue
        for symbol in sorted(str(item).upper() for item in symbols):
            for position_side in sorted(str(item).lower() for item in sides):
                for role in ORDER_ROLES:
                    order_type, budget, trigger_kind, fee_bps = parsed_orders[role]
                    scopes.append(
                        {
                            "candidate": candidate,
                            "symbol": symbol,
                            "position_side": position_side,
                            "order_role": role,
                            "action_side": _action_side(position_side, role),
                            "order_type": order_type,
                            "trigger_kind": trigger_kind,
                            "fee_bps": fee_bps,
                            "maximum_all_in_drag_bps": budget,
                        }
                    )
    keys = [execution_scope_key(item) for item in scopes]
    if len(keys) != len(set(keys)):
        failures.append("expanded portfolio execution scopes are duplicated")
    return scopes, failures


def _check(name: str, passed: bool, actual: Any, required: Any, kind: str) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "actual": actual,
        "required": required,
        "kind": kind,
    }


def _selection_bias_failures(
    audit: Any,
    policy: PortfolioPromotionPolicy,
    *,
    expected_observations: int | None = None,
    expected_selected_return_sha256: str | None = None,
    expected_first_timestamp: Any = None,
    expected_last_timestamp: Any = None,
    expected_family_id: Any = None,
    expected_inventory_id: Any = None,
) -> list[str]:
    """Validate a sealed DSR/PBO receipt instead of trusting its top-level status."""
    if not isinstance(audit, dict):
        return ["selection-bias audit is missing"]
    failures: list[str] = []
    expected_policy = asdict(
        SelectionBiasPolicy(
            minimum_observations=policy.minimum_selection_bias_observations,
            minimum_deflated_sharpe_probability=policy.minimum_deflated_sharpe_probability,
            maximum_probability_backtest_overfitting=policy.maximum_probability_backtest_overfitting,
            cscv_slices=policy.selection_bias_cscv_slices,
        )
    )
    if audit.get("status") != "PASS":
        failures.append("selection-bias audit status is not PASS")
    if audit.get("policy") != expected_policy:
        failures.append("selection-bias audit policy does not match the promotion policy")
    if audit.get("protocol_current") is not True:
        failures.append("selection-bias audit protocol is not current")
    protocol_sha256 = audit.get("protocol_sha256")
    if (
        not isinstance(protocol_sha256, dict)
        or set(protocol_sha256) != set(SELECTION_BIAS_PROTOCOL_FILES)
        or not all(_sha256_text(value) for value in protocol_sha256.values())
    ):
        failures.append("selection-bias protocol digests are missing or invalid")
    if not _sha256_text(audit.get("trial_return_matrix_sha256")):
        failures.append("trial return matrix digest is missing")
    if not _sha256_text(audit.get("selected_return_sha256")):
        failures.append("selected return series digest is missing")
    elif (
        expected_selected_return_sha256 is not None
        and audit.get("selected_return_sha256")
        != expected_selected_return_sha256
    ):
        failures.append("selected trial returns do not match the promoted return series")
    source = audit.get("source")
    if expected_first_timestamp is not None or expected_last_timestamp is not None:
        if (
            not isinstance(source, dict)
            or _parse_utc(source.get("first_timestamp"))
            != _parse_utc(expected_first_timestamp)
            or _parse_utc(source.get("last_timestamp"))
            != _parse_utc(expected_last_timestamp)
            or (
                expected_observations is not None
                and source.get("rows") != expected_observations
            )
        ):
            failures.append("selection-bias return window does not match the promoted sample")
    trial_inventory_source = audit.get("trial_inventory")
    if not isinstance(trial_inventory_source, dict):
        failures.append("sealed trial inventory source is missing")
        inventory_validation: dict[str, Any] = {}
    else:
        inventory_validation = trial_inventory_source.get("validation") or {}
        if (
            not isinstance(trial_inventory_source.get("path"), str)
            or not trial_inventory_source.get("path")
            or not _sha256_text(trial_inventory_source.get("sha256"))
        ):
            failures.append("sealed trial inventory path or digest is invalid")
        if not isinstance(inventory_validation, dict) or inventory_validation.get("status") != "PASS":
            failures.append("sealed trial inventory validation is not PASS")
    if not isinstance(expected_family_id, str) or not expected_family_id:
        failures.append("preregistered selection-bias family ID is missing")
    elif inventory_validation.get("family_id") != expected_family_id:
        failures.append("sealed trial inventory family does not match the manifest")
    if not isinstance(expected_inventory_id, str) or not expected_inventory_id:
        failures.append("preregistered selection-bias inventory ID is missing")
    elif inventory_validation.get("inventory_id") != expected_inventory_id:
        failures.append("sealed trial inventory ID does not match the manifest")
    attempted_trials = audit.get("attempted_trials")
    if (
        not isinstance(attempted_trials, int)
        or isinstance(attempted_trials, bool)
        or attempted_trials < 1
    ):
        failures.append("attempted trial count is invalid")
        attempted_trials = 0
    trial_ids = audit.get("trial_ids")
    if (
        not isinstance(trial_ids, list)
        or len(trial_ids) != attempted_trials
        or len(set(str(item) for item in trial_ids)) != len(trial_ids)
        or not all(isinstance(item, str) and item for item in trial_ids)
    ):
        failures.append("trial inventory is not exact and unique")
    elif audit.get("selected_trial_id") not in trial_ids:
        failures.append("selected trial is absent from the inventory")
    if audit.get("trial_inventory_complete") is not True:
        failures.append("attempted trial inventory is incomplete")
    if isinstance(inventory_validation, dict) and (
        inventory_validation.get("attempted_trials") != attempted_trials
        or inventory_validation.get("selected_trial_id") != audit.get("selected_trial_id")
        or inventory_validation.get("pre_registered_single_trial")
        is not audit.get("pre_registered_single_trial")
    ):
        failures.append("sealed trial inventory does not match the statistical audit")

    dsr = audit.get("deflated_sharpe")
    observations = None
    if not isinstance(dsr, dict):
        failures.append("Deflated Sharpe evidence is missing")
    else:
        observations = dsr.get("observations")
        probability = dsr.get("probability")
        if not isinstance(observations, int) or observations < policy.minimum_selection_bias_observations:
            failures.append("Deflated Sharpe observation count is insufficient")
        if dsr.get("trials") != attempted_trials:
            failures.append("Deflated Sharpe trial count does not match the inventory")
        if not _finite_number(probability) or float(probability) < policy.minimum_deflated_sharpe_probability:
            failures.append("Deflated Sharpe probability is below threshold")
    if expected_observations is not None and observations != expected_observations:
        failures.append("selection-bias observations do not match the reported return sample")

    pbo = audit.get("pbo")
    if not isinstance(pbo, dict):
        failures.append("PBO evidence is missing")
    elif attempted_trials == 1:
        if (
            audit.get("pre_registered_single_trial") is not True
            or pbo.get("status") != "NOT_APPLICABLE_SINGLE_PREREGISTERED_TRIAL"
            or pbo.get("trials") != 1
            or pbo.get("observations") != observations
        ):
            failures.append("single-trial PBO exemption is not valid")
    else:
        probability = pbo.get("probability")
        if (
            pbo.get("status") != "CALCULATED"
            or pbo.get("trials") != attempted_trials
            or pbo.get("observations") != observations
            or pbo.get("slices") != policy.selection_bias_cscv_slices
            or not _finite_number(probability)
            or float(probability) > policy.maximum_probability_backtest_overfitting
        ):
            failures.append("PBO evidence is missing, inconsistent, or above threshold")

    expected_checks = {
        "trial_inventory_complete",
        "minimum_observations",
        "deflated_sharpe_probability",
        "probability_backtest_overfitting",
    }
    checks = audit.get("checks")
    check_names = [item.get("name") for item in checks or [] if isinstance(item, dict)]
    if (
        not isinstance(checks, list)
        or set(check_names) != expected_checks
        or len(check_names) != len(expected_checks)
        or not all(item.get("passed") is True for item in checks if isinstance(item, dict))
    ):
        failures.append("selection-bias check inventory is incomplete or not all PASS")
    return failures


def _portfolio_risk_failures(
    audit: Any,
    policy: PortfolioPromotionPolicy,
    *,
    expected_observations: int,
) -> list[str]:
    if not isinstance(audit, dict):
        return ["portfolio risk audit is missing"]
    failures: list[str] = []
    expected_policy = asdict(
        PortfolioRiskPolicy(
            minimum_daily_returns=policy.minimum_portfolio_risk_daily_returns,
            horizon_days=policy.portfolio_risk_horizon_days,
            short_block_days=policy.portfolio_risk_short_block_days,
            long_block_days=policy.portfolio_risk_long_block_days,
            simulations_per_block=policy.portfolio_risk_simulations_per_block,
            ruin_equity_fraction=policy.portfolio_risk_ruin_equity_fraction,
            drawdown_threshold=policy.portfolio_risk_drawdown_threshold,
            annual_target_return=policy.portfolio_risk_annual_target_return,
            binomial_confidence_level=policy.portfolio_risk_binomial_confidence_level,
            seed=policy.portfolio_risk_seed,
            maximum_probability_of_ruin=policy.maximum_portfolio_probability_of_ruin,
            maximum_probability_terminal_loss=policy.maximum_portfolio_probability_terminal_loss,
            minimum_probability_reaching_annual_target=policy.minimum_portfolio_probability_reaching_annual_target,
            maximum_bootstrap_drawdown_p95=policy.maximum_portfolio_bootstrap_drawdown_p95,
            maximum_historical_drawdown=policy.maximum_portfolio_historical_drawdown,
            deterministic_daily_repeats=policy.portfolio_risk_deterministic_daily_repeats,
            deterministic_weekly_repeats=policy.portfolio_risk_deterministic_weekly_repeats,
            deterministic_four_week_repeats=policy.portfolio_risk_deterministic_four_week_repeats,
            maximum_deterministic_stress_drawdown=policy.maximum_portfolio_deterministic_stress_drawdown,
        )
    )
    if audit.get("status") != "PASS":
        failures.append("portfolio risk audit status is not PASS")
    if audit.get("policy") != expected_policy:
        failures.append("portfolio risk audit policy does not match promotion policy")
    if audit.get("protocol_current") is not True:
        failures.append("portfolio risk protocol is not current")
    protocol = audit.get("protocol_sha256")
    if (
        not isinstance(protocol, dict)
        or set(protocol) != set(PORTFOLIO_RISK_PROTOCOL_FILES)
        or not all(_sha256_text(value) for value in protocol.values())
    ):
        failures.append("portfolio risk protocol digests are missing or invalid")
    source = audit.get("source")
    if (
        not isinstance(source, dict)
        or not isinstance(source.get("path"), str)
        or not source.get("path")
        or not _sha256_text(source.get("sha256"))
    ):
        failures.append("portfolio risk source path or digest is invalid")
    observations = audit.get("observations")
    equity = audit.get("equity") or {}
    if (
        not isinstance(equity, dict)
        or not isinstance(observations, int)
        or observations < policy.minimum_portfolio_risk_daily_returns
        or observations != expected_observations
        or equity.get("snapshots") != observations + 1
        or not _sha256_text(equity.get("daily_returns_sha256"))
    ):
        failures.append("portfolio risk observation coverage or return digest is invalid")
    historical_drawdown = equity.get("historical_maximum_drawdown")
    if (
        not _finite_number(historical_drawdown)
        or float(historical_drawdown) < 0.0
        or float(historical_drawdown) > policy.maximum_portfolio_historical_drawdown
    ):
        failures.append("portfolio historical maximum drawdown does not pass")
    scenarios = audit.get("scenarios")
    expected_blocks = {
        policy.portfolio_risk_short_block_days,
        policy.portfolio_risk_long_block_days,
    }
    if (
        not isinstance(scenarios, list)
        or len(scenarios) != 2
        or not all(isinstance(item, dict) for item in scenarios)
        or {item.get("block_days") for item in scenarios} != expected_blocks
        or not all(
            item.get("simulations") == policy.portfolio_risk_simulations_per_block
            for item in scenarios
        )
    ):
        failures.append("weekly/four-week portfolio risk scenarios are incomplete")
        valid_scenarios: list[dict[str, Any]] = []
    else:
        valid_scenarios = scenarios
    probability_fields = (
        "probability_of_ruin",
        "probability_drawdown_exceeds_threshold",
        "probability_terminal_loss",
        "probability_reaching_annual_target",
    )
    scenario_metric_fields = probability_fields + (
        "probability_of_ruin_upper_confidence",
        "probability_terminal_loss_upper_confidence",
        "probability_reaching_annual_target_lower_confidence",
        "terminal_return_p05",
        "maximum_drawdown_p95",
    )
    if valid_scenarios and any(
        not all(_finite_number(item.get(name)) for name in scenario_metric_fields)
        or any(not 0.0 <= float(item[name]) <= 1.0 for name in probability_fields)
        or not 0.0 <= float(item["maximum_drawdown_p95"]) <= 1.0
        for item in valid_scenarios
    ):
        failures.append("portfolio risk scenario metrics are missing or invalid")
        valid_scenarios = []
    event_probability_fields = {
        "ruin": "probability_of_ruin",
        "drawdown_exceeds_threshold": "probability_drawdown_exceeds_threshold",
        "terminal_loss": "probability_terminal_loss",
        "reaches_annual_target": "probability_reaching_annual_target",
    }
    if valid_scenarios:
        for item in valid_scenarios:
            counts = item.get("event_counts")
            simulations = item["simulations"]
            if (
                item.get("binomial_confidence_level")
                != policy.portfolio_risk_binomial_confidence_level
                or not isinstance(counts, dict)
                or set(counts) != set(event_probability_fields)
                or any(
                    isinstance(count, bool)
                    or not isinstance(count, int)
                    or not 0 <= count <= simulations
                    for count in counts.values()
                )
                or any(
                    not math.isclose(
                        float(item[field]),
                        counts[event] / simulations,
                        rel_tol=1e-12,
                        abs_tol=1e-15,
                    )
                    for event, field in event_probability_fields.items()
                )
            ):
                failures.append("portfolio risk event counts or probabilities are inconsistent")
                valid_scenarios = []
                break
            _, ruin_upper = wilson_bounds(
                counts["ruin"],
                simulations,
                policy.portfolio_risk_binomial_confidence_level,
            )
            _, loss_upper = wilson_bounds(
                counts["terminal_loss"],
                simulations,
                policy.portfolio_risk_binomial_confidence_level,
            )
            target_lower, _ = wilson_bounds(
                counts["reaches_annual_target"],
                simulations,
                policy.portfolio_risk_binomial_confidence_level,
            )
            expected_bounds = {
                "probability_of_ruin_upper_confidence": ruin_upper,
                "probability_terminal_loss_upper_confidence": loss_upper,
                "probability_reaching_annual_target_lower_confidence": target_lower,
            }
            if any(
                not math.isclose(
                    float(item[name]), value, rel_tol=1e-12, abs_tol=1e-15
                )
                for name, value in expected_bounds.items()
            ):
                failures.append("portfolio risk Wilson confidence bounds are inconsistent")
                valid_scenarios = []
                break
    aggregate = audit.get("aggregate") or {}
    if not isinstance(aggregate, dict):
        aggregate = {}
        failures.append("portfolio risk aggregate is invalid")
    if valid_scenarios:
        expected_aggregate = {
            "probability_of_ruin": max(
                float(item["probability_of_ruin"]) for item in valid_scenarios
            ),
            "probability_of_ruin_upper_confidence": max(
                float(item["probability_of_ruin_upper_confidence"])
                for item in valid_scenarios
            ),
            "probability_drawdown_exceeds_threshold": max(
                float(item["probability_drawdown_exceeds_threshold"])
                for item in valid_scenarios
            ),
            "probability_terminal_loss": max(
                float(item["probability_terminal_loss"]) for item in valid_scenarios
            ),
            "probability_terminal_loss_upper_confidence": max(
                float(item["probability_terminal_loss_upper_confidence"])
                for item in valid_scenarios
            ),
            "probability_reaching_annual_target": min(
                float(item["probability_reaching_annual_target"])
                for item in valid_scenarios
            ),
            "probability_reaching_annual_target_lower_confidence": min(
                float(item["probability_reaching_annual_target_lower_confidence"])
                for item in valid_scenarios
            ),
            "terminal_return_p05": min(
                float(item["terminal_return_p05"]) for item in valid_scenarios
            ),
            "maximum_drawdown_p95": max(
                float(item["maximum_drawdown_p95"]) for item in valid_scenarios
            ),
        }
        if any(
            not _finite_number(aggregate.get(name))
            or not math.isclose(
                float(aggregate[name]), value, rel_tol=1e-12, abs_tol=1e-15
            )
            for name, value in expected_aggregate.items()
        ):
            failures.append("portfolio risk aggregate is not the conservative scenario envelope")
    thresholds = (
        (
            "probability_of_ruin_upper_confidence",
            policy.maximum_portfolio_probability_of_ruin,
            "maximum",
        ),
        (
            "probability_terminal_loss_upper_confidence",
            policy.maximum_portfolio_probability_terminal_loss,
            "maximum",
        ),
        (
            "probability_reaching_annual_target_lower_confidence",
            policy.minimum_portfolio_probability_reaching_annual_target,
            "minimum",
        ),
        (
            "maximum_drawdown_p95",
            policy.maximum_portfolio_bootstrap_drawdown_p95,
            "maximum",
        ),
    )
    for name, threshold, direction in thresholds:
        value = aggregate.get(name)
        if not _finite_number(value) or (
            direction == "maximum" and float(value) > threshold
        ) or (direction == "minimum" and float(value) < threshold):
            failures.append(f"portfolio risk {name} does not pass")
    deterministic_stress = audit.get("deterministic_stress")
    expected_stress_specs = {
        f"worst_day_x{policy.portfolio_risk_deterministic_daily_repeats}": (
            1,
            policy.portfolio_risk_deterministic_daily_repeats,
        ),
        f"worst_7d_x{policy.portfolio_risk_deterministic_weekly_repeats}": (
            7,
            policy.portfolio_risk_deterministic_weekly_repeats,
        ),
        f"worst_28d_x{policy.portfolio_risk_deterministic_four_week_repeats}": (
            28,
            policy.portfolio_risk_deterministic_four_week_repeats,
        ),
    }
    stress_scenarios = (
        deterministic_stress.get("scenarios")
        if isinstance(deterministic_stress, dict)
        else None
    )
    if (
        not isinstance(stress_scenarios, list)
        or len(stress_scenarios) != len(expected_stress_specs)
        or not all(isinstance(item, dict) for item in stress_scenarios)
        or {item.get("name") for item in stress_scenarios} != set(expected_stress_specs)
    ):
        failures.append("deterministic portfolio tail-stress scenarios are incomplete")
        valid_stress_scenarios: list[dict[str, Any]] = []
    else:
        valid_stress_scenarios = stress_scenarios
    if valid_stress_scenarios and any(
        (
            item.get("source_window_days"),
            item.get("repeats"),
        )
        != expected_stress_specs[item["name"]]
        or item.get("stressed_days")
        != item.get("source_window_days") * item.get("repeats")
        or isinstance(item.get("source_start_return_index"), bool)
        or not isinstance(item.get("source_start_return_index"), int)
        or item.get("source_start_return_index") < 0
        or not all(
            _finite_number(item.get(name))
            for name in ("source_block_return", "terminal_return", "maximum_drawdown")
        )
        or not 0.0 <= float(item["maximum_drawdown"]) <= 1.0
        for item in valid_stress_scenarios
    ):
        failures.append("deterministic portfolio tail-stress metrics are invalid")
        valid_stress_scenarios = []
    deterministic_drawdown = (
        deterministic_stress.get("maximum_drawdown")
        if isinstance(deterministic_stress, dict)
        else None
    )
    deterministic_terminal = (
        deterministic_stress.get("worst_terminal_return")
        if isinstance(deterministic_stress, dict)
        else None
    )
    if valid_stress_scenarios:
        expected_stress_drawdown = max(
            float(item["maximum_drawdown"]) for item in valid_stress_scenarios
        )
        expected_stress_terminal = min(
            float(item["terminal_return"]) for item in valid_stress_scenarios
        )
        if (
            not _finite_number(deterministic_drawdown)
            or not math.isclose(
                float(deterministic_drawdown),
                expected_stress_drawdown,
                rel_tol=1e-12,
                abs_tol=1e-15,
            )
            or not _finite_number(deterministic_terminal)
            or not math.isclose(
                float(deterministic_terminal),
                expected_stress_terminal,
                rel_tol=1e-12,
                abs_tol=1e-15,
            )
        ):
            failures.append("deterministic portfolio tail-stress aggregate is inconsistent")
    if (
        not _finite_number(deterministic_drawdown)
        or float(deterministic_drawdown)
        > policy.maximum_portfolio_deterministic_stress_drawdown
    ):
        failures.append("deterministic portfolio tail-stress drawdown does not pass")
    expected_checks = {
        "minimum_daily_returns",
        "historical_maximum_drawdown",
        "probability_of_ruin",
        "probability_terminal_loss",
        "probability_reaching_annual_target",
        "bootstrap_maximum_drawdown_p95",
        "deterministic_tail_stress_drawdown",
    }
    checks = audit.get("checks")
    names = [item.get("name") for item in checks or [] if isinstance(item, dict)]
    expected_check_values = {
        "minimum_daily_returns": (
            observations,
            policy.minimum_portfolio_risk_daily_returns,
        ),
        "historical_maximum_drawdown": (
            historical_drawdown,
            policy.maximum_portfolio_historical_drawdown,
        ),
        "probability_of_ruin": (
            aggregate.get("probability_of_ruin_upper_confidence"),
            policy.maximum_portfolio_probability_of_ruin,
        ),
        "probability_terminal_loss": (
            aggregate.get("probability_terminal_loss_upper_confidence"),
            policy.maximum_portfolio_probability_terminal_loss,
        ),
        "probability_reaching_annual_target": (
            aggregate.get("probability_reaching_annual_target_lower_confidence"),
            policy.minimum_portfolio_probability_reaching_annual_target,
        ),
        "bootstrap_maximum_drawdown_p95": (
            aggregate.get("maximum_drawdown_p95"),
            policy.maximum_portfolio_bootstrap_drawdown_p95,
        ),
        "deterministic_tail_stress_drawdown": (
            deterministic_drawdown,
            policy.maximum_portfolio_deterministic_stress_drawdown,
        ),
    }
    if (
        not isinstance(checks, list)
        or set(names) != expected_checks
        or len(names) != len(expected_checks)
        or not all(isinstance(item, dict) and item.get("passed") is True for item in checks)
        or any(
            item.get("actual") != expected_check_values[item["name"]][0]
            or item.get("required") != expected_check_values[item["name"]][1]
            for item in checks
            if isinstance(item, dict) and item.get("name") in expected_check_values
        )
    ):
        failures.append("portfolio risk check inventory is incomplete or not all PASS")
    return failures


def _portfolio_composition_failures(
    audit: Any,
    *,
    candidates: list[str],
) -> list[str]:
    if not isinstance(audit, dict):
        return ["portfolio composition audit is missing"]
    failures: list[str] = []
    if audit.get("status") != "PASS":
        failures.append("portfolio composition audit status is not PASS")
    if audit.get("protocol_current") is not True:
        failures.append("portfolio composition protocol is not current")
    protocol = audit.get("protocol_sha256")
    if (
        not isinstance(protocol, dict)
        or set(protocol) != set(PORTFOLIO_COMPOSITION_PROTOCOL_FILES)
        or not all(_sha256_text(value) for value in protocol.values())
    ):
        failures.append("portfolio composition protocol digests are missing or invalid")
    source = audit.get("source")
    if (
        not isinstance(source, dict)
        or not isinstance(source.get("path"), str)
        or not source.get("path")
        or not _sha256_text(source.get("sha256"))
    ):
        failures.append("portfolio contribution source path or digest is invalid")
    sleeve_pnl_source = audit.get("sleeve_pnl_source")
    if (
        not isinstance(sleeve_pnl_source, dict)
        or not isinstance(sleeve_pnl_source.get("path"), str)
        or not sleeve_pnl_source.get("path")
        or not _sha256_text(sleeve_pnl_source.get("sha256"))
    ):
        failures.append("portfolio sleeve PnL source path or digest is invalid")
    if audit.get("candidate_order") != candidates:
        failures.append("portfolio contribution candidate order does not match manifest")
    observations = audit.get("observations")
    if not isinstance(observations, int) or observations < 1:
        failures.append("portfolio composition observation count is invalid")
    if not _sha256_text(audit.get("portfolio_returns_sha256")) or not _sha256_text(
        audit.get("equity_daily_returns_sha256")
    ):
        failures.append("portfolio composition return digests are missing")
    reconciliation_error = audit.get("maximum_daily_reconciliation_error")
    if not _finite_number(reconciliation_error) or float(reconciliation_error) > 1e-12:
        failures.append("portfolio sleeve contributions do not reconcile to MTM returns")
    sleeve_pnl_error = audit.get("maximum_daily_sleeve_pnl_reconciliation_error")
    contribution_binding_error = audit.get(
        "maximum_sleeve_contribution_binding_error"
    )
    sleeve_pnl_digests = audit.get("sleeve_net_pnl_fraction_sha256")
    sleeve_return_digests = audit.get("sleeve_daily_return_sha256")
    minimum_sleeve_equity = audit.get("minimum_sleeve_equity_fraction")
    if (
        not _finite_number(sleeve_pnl_error)
        or float(sleeve_pnl_error) > 1e-12
        or not _finite_number(contribution_binding_error)
        or float(contribution_binding_error) > 1e-12
        or not isinstance(sleeve_pnl_digests, dict)
        or set(sleeve_pnl_digests) != set(candidates)
        or not all(_sha256_text(value) for value in sleeve_pnl_digests.values())
        or not isinstance(sleeve_return_digests, dict)
        or set(sleeve_return_digests) != set(candidates)
        or not all(_sha256_text(value) for value in sleeve_return_digests.values())
        or not _finite_number(minimum_sleeve_equity)
        or float(minimum_sleeve_equity) <= 0.0
    ):
        failures.append(
            "portfolio sleeve PnL does not bind every contribution to the MTM path"
        )
    pairwise = audit.get("pairwise_daily_correlations")
    if audit.get("pairwise_daily_correlation_basis") != (
        "self_financing_sleeve_daily_returns"
    ):
        failures.append("portfolio correlation basis is not self-financing sleeve returns")
    required_pairs = len(candidates) * (len(candidates) - 1) // 2
    pair_keys: set[tuple[str, str]] = set()
    if not isinstance(pairwise, list) or len(pairwise) != required_pairs:
        failures.append("portfolio composition pairwise correlation inventory is incomplete")
    else:
        for item in pairwise:
            if not isinstance(item, dict):
                failures.append("portfolio composition correlation row is invalid")
                break
            left, right = sorted(
                (str(item.get("left") or ""), str(item.get("right") or ""))
            )
            key = (left, right)
            value = item.get("correlation")
            if (
                left not in candidates
                or right not in candidates
                or left == right
                or key in pair_keys
                or not _finite_number(value)
                or abs(float(value)) > 1.0
            ):
                failures.append("portfolio composition correlation row is invalid")
                break
            pair_keys.add(key)
    contributions = audit.get("positive_pnl_share_by_sleeve")
    if audit.get("positive_pnl_share_basis") != (
        "source_sleeve_daily_net_pnl_fraction_of_initial_portfolio_equity"
    ):
        failures.append("portfolio positive PnL share basis is not source sleeve net PnL")
    if (
        not isinstance(contributions, dict)
        or set(contributions) != set(candidates)
        or not all(
            _finite_number(value) and 0.0 <= float(value) <= 1.0
            for value in contributions.values()
        )
        or abs(sum(float(value) for value in contributions.values()) - 1.0) > 1e-9
    ):
        failures.append("portfolio composition positive PnL shares are invalid")
    expected_checks = {
        "exact_mtm_date_alignment",
        "daily_mtm_return_reconciliation",
        "sleeve_pnl_exact_mtm_date_alignment",
        "sleeve_pnl_mtm_reconciliation",
        "sleeve_contribution_binding",
        "self_financing_sleeve_return_reconstruction",
        "pairwise_self_financing_return_correlations_defined",
        "source_net_pnl_positive_share_available",
    }
    checks = audit.get("checks")
    names = [item.get("name") for item in checks or [] if isinstance(item, dict)]
    if (
        not isinstance(checks, list)
        or set(names) != expected_checks
        or len(names) != len(expected_checks)
        or not all(
            isinstance(item, dict) and item.get("passed") is True for item in checks
        )
    ):
        failures.append("portfolio composition check inventory is incomplete or not all PASS")
    return failures


def _portfolio_performance_failures(
    audit: Any, *, candidates: list[str]
) -> list[str]:
    if not isinstance(audit, dict):
        return ["portfolio performance audit is missing"]
    failures: list[str] = []
    if audit.get("status") != "PASS":
        failures.append("portfolio performance audit status is not PASS")
    if audit.get("protocol_current") is not True:
        failures.append("portfolio performance protocol is not current")
    protocol = audit.get("protocol_sha256")
    if (
        not isinstance(protocol, dict)
        or set(protocol) != set(PORTFOLIO_PERFORMANCE_PROTOCOL_FILES)
        or not all(_sha256_text(value) for value in protocol.values())
    ):
        failures.append("portfolio performance protocol digests are missing or invalid")
    sources = audit.get("sources")
    if not isinstance(sources, dict) or set(sources) != {
        "trades",
        "exposure",
        "accounting",
    }:
        failures.append("portfolio performance source inventory is incomplete")
    else:
        for name, source in sources.items():
            if (
                not isinstance(source, dict)
                or not isinstance(source.get("path"), str)
                or not source.get("path")
                or not _sha256_text(source.get("sha256"))
            ):
                failures.append(f"portfolio performance {name} source is invalid")
    if audit.get("candidate_order") != candidates:
        failures.append("portfolio performance candidates do not match manifest")
    for name in (
        "trade_rows",
        "organic_trades",
        "accounting_rows",
        "liquidations",
        "rule_violations",
    ):
        if not isinstance(audit.get(name), int) or audit[name] < 0:
            failures.append(f"portfolio performance {name} is invalid")
    rule_violation_evidence = audit.get("rule_violation_evidence")
    expected_rule_violation_categories = {
        "trade_maximum_leverage",
        "trade_leverage_adjusted_mae",
        "daily_gross_leverage",
        "daily_absolute_net_leverage",
        "liquidation_exit",
    }
    if (
        not isinstance(rule_violation_evidence, dict)
        or set(rule_violation_evidence) != expected_rule_violation_categories
        or any(
            not isinstance(values, list)
            or not all(isinstance(value, str) and value for value in values)
            for values in rule_violation_evidence.values()
        )
        or audit.get("rule_violations")
        != sum(len(values) for values in rule_violation_evidence.values())
    ):
        failures.append("portfolio performance rule-violation evidence is invalid")
    for name in (
        "maximum_daily_accounting_reconciliation_error",
        "closed_trade_mtm_total_reconciliation_error",
    ):
        if not _finite_number(audit.get(name)) or float(audit[name]) < 0:
            failures.append(f"portfolio performance {name} is invalid")
    component_errors = audit.get("closed_trade_mtm_component_reconciliation_errors")
    if (
        not isinstance(component_errors, dict)
        or set(component_errors)
        != {
            "gross_price_pnl",
            "trading_fees",
            "funding_pnl",
            "modeled_slippage",
        }
        or any(
            not _finite_number(value) or float(value) < 0.0
            for value in component_errors.values()
        )
    ):
        failures.append("portfolio performance component reconciliation is invalid")
    cost_floor = audit.get("execution_cost_floor")
    expected_cost_floor_fields = {
        "trades_evaluated",
        "total_trades",
        "duplicate_scope_roles",
        "minimum_total_trading_fees",
        "minimum_total_all_in_cost",
        "recorded_total_trading_fees",
        "recorded_total_modeled_slippage",
        "maximum_trade_fee_shortfall",
        "maximum_trade_all_in_shortfall",
    }
    if (
        not isinstance(cost_floor, dict)
        or set(cost_floor) != expected_cost_floor_fields
        or not isinstance(cost_floor.get("trades_evaluated"), int)
        or not isinstance(cost_floor.get("total_trades"), int)
        or cost_floor.get("trades_evaluated") != audit.get("trade_rows")
        or cost_floor.get("total_trades") != audit.get("trade_rows")
        or cost_floor.get("duplicate_scope_roles") is not False
        or any(
            not _finite_number(cost_floor.get(name))
            or float(cost_floor[name]) < 0.0
            for name in (
                "minimum_total_trading_fees",
                "minimum_total_all_in_cost",
                "recorded_total_trading_fees",
                "recorded_total_modeled_slippage",
                "maximum_trade_fee_shortfall",
                "maximum_trade_all_in_shortfall",
            )
        )
        or (
            _finite_number(cost_floor.get("maximum_trade_fee_shortfall"))
            and float(cost_floor["maximum_trade_fee_shortfall"]) > 1e-12
        )
        or (
            _finite_number(cost_floor.get("maximum_trade_all_in_shortfall"))
            and float(cost_floor["maximum_trade_all_in_shortfall"]) > 1e-12
        )
    ):
        failures.append("portfolio performance execution-cost floor is invalid")
    for name in ("maximum_gross_leverage", "maximum_absolute_net_leverage"):
        if not _finite_number(audit.get(name)) or float(audit[name]) < 0:
            failures.append(f"portfolio performance {name} is invalid")
    if (
        not _finite_number(audit.get("maximum_trade_leverage_adjusted_mae"))
        or float(audit["maximum_trade_leverage_adjusted_mae"]) < 0.0
        or float(audit["maximum_trade_leverage_adjusted_mae"])
        > PORTFOLIO_MONTHLY_POLICY.maximum_trade_adverse_excursion
    ):
        failures.append("portfolio performance intratrade leverage-adjusted MAE is invalid")
    daily_pf = audit.get("daily_mtm_profit_factor")
    daily_gross_profit = audit.get("daily_mtm_gross_profit")
    daily_gross_loss = audit.get("daily_mtm_gross_loss")
    if (
        not _finite_number(daily_gross_profit)
        or float(daily_gross_profit) <= 0.0
        or not _finite_number(daily_gross_loss)
        or float(daily_gross_loss) < 0.0
        or (
            float(daily_gross_loss) > 0.0
            and (
                not _finite_number(daily_pf)
                or float(daily_pf) < PORTFOLIO_MONTHLY_POLICY.minimum_profit_factor
            )
        )
        or (float(daily_gross_loss) == 0.0 and daily_pf is not None)
    ):
        failures.append("portfolio performance daily MTM profit factor is invalid")
    if (
        _finite_number(audit.get("maximum_gross_leverage"))
        and _finite_number(audit.get("maximum_absolute_net_leverage"))
        and float(audit["maximum_absolute_net_leverage"])
        > float(audit["maximum_gross_leverage"]) + 1e-12
    ):
        failures.append("portfolio performance net leverage exceeds gross leverage")
    exposure_reconciliation = audit.get("exposure_reconciliation")
    if (
        not isinstance(exposure_reconciliation, dict)
        or set(exposure_reconciliation)
        != {
            "rows_evaluated",
            "reported_rows",
            "reconstructed_maximum_gross_leverage",
            "reconstructed_maximum_absolute_net_leverage",
            "maximum_gross_leverage_understatement",
            "maximum_absolute_net_leverage_understatement",
            "notional_convention",
            "equity_denominator",
        }
        or not isinstance(exposure_reconciliation.get("rows_evaluated"), int)
        or not isinstance(exposure_reconciliation.get("reported_rows"), int)
        or exposure_reconciliation.get("rows_evaluated") != audit.get("accounting_rows")
        or exposure_reconciliation.get("reported_rows") != audit.get("accounting_rows")
        or any(
            not _finite_number(exposure_reconciliation.get(name))
            or float(exposure_reconciliation[name]) < 0.0
            for name in (
                "reconstructed_maximum_gross_leverage",
                "reconstructed_maximum_absolute_net_leverage",
                "maximum_gross_leverage_understatement",
                "maximum_absolute_net_leverage_understatement",
            )
        )
        or (
            _finite_number(
                exposure_reconciliation.get("maximum_gross_leverage_understatement")
            )
            and float(
                exposure_reconciliation["maximum_gross_leverage_understatement"]
            )
            > 1e-12
        )
        or (
            _finite_number(
                exposure_reconciliation.get(
                    "maximum_absolute_net_leverage_understatement"
                )
            )
            and float(
                exposure_reconciliation[
                    "maximum_absolute_net_leverage_understatement"
                ]
            )
            > 1e-12
        )
    ):
        failures.append("portfolio performance intraday exposure reconciliation is invalid")
    boundary_force_exit = audit.get("boundary_force_exit_audit")
    if (
        not isinstance(boundary_force_exit, dict)
        or set(boundary_force_exit)
        != {
            "force_exit_trades",
            "invalid_force_exit_trade_ids",
            "required_close_at_utc",
        }
        or not isinstance(boundary_force_exit.get("force_exit_trades"), int)
        or boundary_force_exit.get("force_exit_trades", -1) < 0
        or boundary_force_exit.get("invalid_force_exit_trade_ids") != []
        or not isinstance(boundary_force_exit.get("required_close_at_utc"), str)
        or _parse_utc(boundary_force_exit.get("required_close_at_utc")) is None
    ):
        failures.append("portfolio performance boundary force-exit evidence is invalid")
    stress = audit.get("stress_returns")
    if (
        not isinstance(stress, dict)
        or set(stress)
        != {"double_fee", "double_slippage", "adverse_funding", "simultaneous"}
        or not all(_finite_number(value) for value in stress.values())
    ):
        failures.append("portfolio performance stress returns are incomplete")
    stress_paths = audit.get("stress_paths")
    expected_stress_paths = {
        "double_fee",
        "double_slippage",
        "adverse_funding",
        "simultaneous",
    }
    if (
        not isinstance(stress_paths, dict)
        or set(stress_paths) != expected_stress_paths
        or any(not isinstance(item, dict) for item in stress_paths.values())
    ):
        failures.append("portfolio performance stress paths are incomplete")
    else:
        for name, item in stress_paths.items():
            if (
                set(item)
                != {
                    "terminal_return",
                    "maximum_drawdown",
                    "minimum_equity_fraction",
                    "ruined",
                }
                or not _finite_number(item.get("terminal_return"))
                or not _finite_number(item.get("maximum_drawdown"))
                or not 0.0 <= float(item["maximum_drawdown"]) <= 1.0
                or not _finite_number(item.get("minimum_equity_fraction"))
                or not isinstance(item.get("ruined"), bool)
                or not isinstance(stress, dict)
                or name not in stress
                or not _finite_number(stress.get(name))
                or bool(item["ruined"])
                != (float(item["minimum_equity_fraction"]) <= 0.0)
                or (
                    bool(item["ruined"])
                    and not math.isclose(
                        float(item["maximum_drawdown"]),
                        1.0,
                        rel_tol=0.0,
                        abs_tol=1e-12,
                    )
                )
                or not math.isclose(
                    float(item["terminal_return"]),
                    float(stress[name]),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                failures.append(f"portfolio performance {name} stress path is invalid")
    expected_checks = {
        "candidate_trade_inventory",
        "daily_exposure_alignment",
        "intraday_exposure_reconciliation",
        "daily_accounting_alignment",
        "daily_accounting_mtm_reconciliation",
        "trade_sample_alignment",
        "boundary_force_exit_integrity",
        "closed_trade_mtm_total_reconciliation",
        "closed_trade_mtm_component_reconciliation",
        "preregistered_execution_cost_floor",
        "capital_preservation_rule_violations",
        "monthly_target_gate",
        "daily_mtm_profit_factor",
        "simultaneous_cost_funding_stress",
        "simultaneous_cost_funding_stress_drawdown",
    }
    checks = audit.get("checks")
    names = [item.get("name") for item in checks or [] if isinstance(item, dict)]
    if (
        not isinstance(checks, list)
        or set(names) != expected_checks
        or len(names) != len(expected_checks)
        or not all(
            isinstance(item, dict) and item.get("passed") is True for item in checks
        )
    ):
        failures.append("portfolio performance check inventory is incomplete or not all PASS")
    return failures


def evaluate_portfolio_promotion(
    manifest: dict[str, Any],
    portfolio_report: dict[str, Any],
    sleeve_reports: dict[str, dict[str, Any]],
    execution_audit: dict[str, Any],
    *,
    manifest_sha256: str,
    policy_sha256: str,
    policy: PortfolioPromotionPolicy | None = None,
) -> dict[str, Any]:
    """Evaluate a combined portfolio without silently inheriting Alpha #1 assumptions."""
    policy = policy or PortfolioPromotionPolicy()
    sleeves = manifest.get("sleeves") if isinstance(manifest.get("sleeves"), list) else []
    expected_scopes, scope_definition_failures = expected_execution_scopes(manifest)
    candidates = [str(item.get("candidate") or "") for item in sleeves if isinstance(item, dict)]
    weights = [item.get("allocation_weight") for item in sleeves if isinstance(item, dict)]
    valid_weights = all(_finite_number(item) and 0 < float(item) <= 1 for item in weights)
    gross_allocation = sum(float(item) for item in weights) if valid_weights else None
    maximum_weight = max((float(item) for item in weights), default=None) if valid_weights else None
    portfolio_evidence = portfolio_report.get("evidence") or {}
    registered_at = _parse_utc(manifest.get("registered_at_utc"))
    sample_started_at = _parse_utc(portfolio_evidence.get("sample_started_at_utc"))
    sample_ended_at = _parse_utc(portfolio_evidence.get("sample_ended_at_utc"))
    performance_audit = portfolio_report.get("portfolio_performance_audit")
    performance_failures = _portfolio_performance_failures(
        performance_audit, candidates=candidates
    )
    monthly_gate = (
        performance_audit.get("monthly_target_gate")
        if isinstance(performance_audit, dict)
        else {}
    ) or {}
    monthly_checks = monthly_gate.get("checks")
    monthly_check_names = [
        str(item.get("name") or "")
        for item in monthly_checks or []
        if isinstance(item, dict)
    ]
    monthly_check_inventory_valid = (
        isinstance(monthly_checks, list)
        and len(monthly_check_names) == len(monthly_checks)
        and len(monthly_check_names) == len(set(monthly_check_names))
        and set(monthly_check_names) == REQUIRED_MONTHLY_CHECK_NAMES
    )
    composition_audit = portfolio_report.get("portfolio_composition_audit")
    composition_failures = _portfolio_composition_failures(
        composition_audit, candidates=candidates
    )
    reported_common_observations = (
        int(composition_audit.get("observations") or 0)
        if isinstance(composition_audit, dict)
        else 0
    )
    portfolio_selection_bias_failures = _selection_bias_failures(
        portfolio_report.get("selection_bias_audit"),
        policy,
        expected_observations=reported_common_observations,
        expected_selected_return_sha256=(
            composition_audit.get("equity_daily_returns_sha256")
            if isinstance(composition_audit, dict)
            else None
        ),
        expected_first_timestamp=(
            composition_audit.get("first_timestamp")
            if isinstance(composition_audit, dict)
            else None
        ),
        expected_last_timestamp=(
            composition_audit.get("last_timestamp")
            if isinstance(composition_audit, dict)
            else None
        ),
        expected_family_id=manifest.get("selection_bias_family_id"),
        expected_inventory_id=manifest.get("selection_bias_inventory_id"),
    )
    portfolio_risk_failures = _portfolio_risk_failures(
        portfolio_report.get("portfolio_risk_audit"),
        policy,
        expected_observations=reported_common_observations,
    )
    portfolio_risk_audit = portfolio_report.get("portfolio_risk_audit")
    risk_equity = (
        portfolio_risk_audit.get("equity")
        if isinstance(portfolio_risk_audit, dict)
        else {}
    ) or {}
    source_started_at = _parse_utc(risk_equity.get("first_timestamp"))
    source_ended_at = _parse_utc(risk_equity.get("last_timestamp"))
    composition_started_at = _parse_utc(
        composition_audit.get("first_timestamp")
        if isinstance(composition_audit, dict)
        else None
    )
    composition_ended_at = _parse_utc(
        composition_audit.get("last_timestamp")
        if isinstance(composition_audit, dict)
        else None
    )
    expected_return_days = (
        (sample_ended_at - sample_started_at).days
        if sample_started_at is not None
        and sample_ended_at is not None
        and sample_started_at < sample_ended_at
        else None
    )
    boundary_close_at = _parse_utc(
        ((performance_audit or {}).get("boundary_force_exit_audit") or {}).get(
            "required_close_at_utc"
        )
        if isinstance(performance_audit, dict)
        else None
    )
    locked_source_window_bound = bool(
        sample_started_at is not None
        and sample_ended_at is not None
        and sample_started_at < sample_ended_at
        and source_started_at == sample_started_at
        and source_ended_at == sample_ended_at
        and composition_started_at == sample_started_at + timedelta(days=1)
        and composition_ended_at == sample_ended_at
        and expected_return_days is not None
        and reported_common_observations == expected_return_days
        and isinstance(performance_audit, dict)
        and performance_audit.get("accounting_rows") == expected_return_days
        and boundary_close_at == sample_ended_at
    )

    checks: list[dict[str, Any]] = [
        _check(
            "portfolio_manifest_schema",
            manifest.get("schema_version") == 1
            and isinstance(manifest.get("candidate"), str)
            and bool(manifest.get("candidate")),
            manifest.get("schema_version"),
            1,
            "evidence",
        ),
        _check(
            "portfolio_manifest_policy_binding",
            manifest.get("portfolio_promotion_policy_sha256") == policy_sha256,
            manifest.get("portfolio_promotion_policy_sha256"),
            policy_sha256,
            "evidence",
        ),
        _check(
            "portfolio_locked_forward_evidence",
            portfolio_evidence.get("sample_role") == "locked_forward"
            and portfolio_evidence.get("pre_registered") is True
            and portfolio_evidence.get("parameter_changes_after_start") == 0
            and portfolio_evidence.get("registered_at_utc") == manifest.get("registered_at_utc")
            and registered_at is not None
            and sample_started_at is not None
            and sample_ended_at is not None
            and registered_at < sample_started_at < sample_ended_at,
            portfolio_evidence,
            "preregistered locked-forward portfolio with zero changes, registration before sample, and an explicit end",
            "evidence",
        ),
        _check(
            "locked_sample_source_window_binding",
            locked_source_window_bound,
            {
                "reported_start": portfolio_evidence.get("sample_started_at_utc"),
                "reported_end": portfolio_evidence.get("sample_ended_at_utc"),
                "mtm_start": risk_equity.get("first_timestamp"),
                "mtm_end": risk_equity.get("last_timestamp"),
                "contribution_start": (
                    composition_audit.get("first_timestamp")
                    if isinstance(composition_audit, dict)
                    else None
                ),
                "contribution_end": (
                    composition_audit.get("last_timestamp")
                    if isinstance(composition_audit, dict)
                    else None
                ),
                "common_daily_observations": reported_common_observations,
                "accounting_rows": (
                    performance_audit.get("accounting_rows")
                    if isinstance(performance_audit, dict)
                    else None
                ),
                "boundary_close": (
                    ((performance_audit or {}).get("boundary_force_exit_audit") or {}).get(
                        "required_close_at_utc"
                    )
                    if isinstance(performance_audit, dict)
                    else None
                ),
            },
            "reported window exactly equals source-replayed MTM boundaries; contributions/accounting cover every return day; boundary exits use the same end",
            "evidence",
        ),
        _check(
            "sleeve_count",
            policy.minimum_sleeves <= len(sleeves) <= policy.maximum_sleeves,
            len(sleeves),
            f"{policy.minimum_sleeves}..{policy.maximum_sleeves}",
            "sample",
        ),
        _check(
            "unique_sleeve_candidates",
            len(candidates) == len(set(candidates)) and all(candidates),
            candidates,
            "unique non-empty candidates",
            "evidence",
        ),
        _check(
            "valid_positive_allocation_weights",
            valid_weights and len(weights) == len(sleeves),
            weights,
            "one finite 0 < weight <= 1 per sleeve",
            "risk",
        ),
        _check(
            "gross_allocation",
            gross_allocation is not None
            and abs(gross_allocation - policy.maximum_gross_allocation) <= 1e-9,
            gross_allocation,
            policy.maximum_gross_allocation,
            "risk",
        ),
        _check(
            "single_sleeve_weight",
            maximum_weight is not None
            and maximum_weight <= policy.maximum_single_sleeve_weight,
            maximum_weight,
            f"<= {policy.maximum_single_sleeve_weight}",
            "risk",
        ),
        _check(
            "execution_scope_definitions",
            not scope_definition_failures and bool(expected_scopes),
            scope_definition_failures,
            "all symbol/direction entry, exit, and protective-stop paths",
            "evidence",
        ),
        _check(
            "portfolio_report_manifest_binding",
            portfolio_report.get("portfolio_manifest_sha256") == manifest_sha256,
            portfolio_report.get("portfolio_manifest_sha256"),
            manifest_sha256,
            "evidence",
        ),
        _check(
            "portfolio_report_policy_binding",
            portfolio_report.get("portfolio_promotion_policy_sha256") == policy_sha256,
            portfolio_report.get("portfolio_promotion_policy_sha256"),
            policy_sha256,
            "evidence",
        ),
        _check(
            "combined_monthly_target_gate",
            monthly_gate.get("status") == "PASS"
            and monthly_gate.get("policy") == asdict(PORTFOLIO_MONTHLY_POLICY)
            and monthly_check_inventory_valid
            and all(item.get("passed") is True for item in monthly_checks),
            {
                "status": monthly_gate.get("status"),
                "policy_matches": monthly_gate.get("policy") == asdict(PORTFOLIO_MONTHLY_POLICY),
                "check_inventory_valid": monthly_check_inventory_valid,
                "check_names": monthly_check_names,
            },
            "exact portfolio monthly policy and check inventory, all PASS",
            "performance",
        ),
        _check(
            "daily_mtm_profit_factor",
            isinstance(performance_audit, dict)
            and _finite_number(performance_audit.get("daily_mtm_gross_profit"))
            and float(performance_audit["daily_mtm_gross_profit"]) > 0.0
            and _finite_number(performance_audit.get("daily_mtm_gross_loss"))
            and float(performance_audit["daily_mtm_gross_loss"]) >= 0.0
            and (
                float(performance_audit["daily_mtm_gross_loss"]) == 0.0
                or (
                    _finite_number(performance_audit.get("daily_mtm_profit_factor"))
                    and float(performance_audit["daily_mtm_profit_factor"])
                    >= policy.minimum_portfolio_daily_mtm_profit_factor
                )
            ),
            {
                "profit_factor": performance_audit.get("daily_mtm_profit_factor"),
                "gross_profit": performance_audit.get("daily_mtm_gross_profit"),
                "gross_loss": performance_audit.get("daily_mtm_gross_loss"),
            }
            if isinstance(performance_audit, dict)
            else None,
            f">= {policy.minimum_portfolio_daily_mtm_profit_factor}, or positive gross profit with zero losing days",
            "performance",
        ),
        _check(
            "portfolio_data_integrity",
            (portfolio_report.get("data_integrity") or {}).get("status") == "PASS",
            (portfolio_report.get("data_integrity") or {}).get("status"),
            "PASS",
            "evidence",
        ),
        _check(
            "portfolio_composition_source_replay",
            not composition_failures,
            composition_failures,
            "source-replayed sleeve contribution matrix exactly reconciles to daily MTM returns",
            "evidence",
        ),
        _check(
            "portfolio_performance_source_replay",
            not performance_failures,
            performance_failures,
            "source-replayed MTM, trade ledger, exposure, costs, funding, and monthly target",
            "evidence",
        ),
        _check(
            "boundary_force_exit_integrity",
            isinstance(performance_audit, dict)
            and isinstance(
                performance_audit.get("boundary_force_exit_audit"), dict
            )
            and performance_audit["boundary_force_exit_audit"].get(
                "invalid_force_exit_trade_ids"
            )
            == [],
            performance_audit.get("boundary_force_exit_audit")
            if isinstance(performance_audit, dict)
            else None,
            "every force_exit closes exactly at the locked MTM sample end",
            "performance",
        ),
        _check(
            "intraday_exposure_reconciliation",
            isinstance(performance_audit, dict)
            and isinstance(performance_audit.get("exposure_reconciliation"), dict)
            and _finite_number(
                performance_audit["exposure_reconciliation"].get(
                    "maximum_gross_leverage_understatement"
                )
            )
            and float(
                performance_audit["exposure_reconciliation"][
                    "maximum_gross_leverage_understatement"
                ]
            )
            <= 1e-12
            and _finite_number(
                performance_audit["exposure_reconciliation"].get(
                    "maximum_absolute_net_leverage_understatement"
                )
            )
            and float(
                performance_audit["exposure_reconciliation"][
                    "maximum_absolute_net_leverage_understatement"
                ]
            )
            <= 1e-12,
            performance_audit.get("exposure_reconciliation")
            if isinstance(performance_audit, dict)
            else None,
            "reported daily maximum exposure covers conservative trade-overlap lower bounds",
            "risk",
        ),
        _check(
            "preregistered_execution_cost_floor",
            isinstance(performance_audit, dict)
            and isinstance(performance_audit.get("execution_cost_floor"), dict)
            and performance_audit["execution_cost_floor"].get("trades_evaluated")
            == performance_audit.get("trade_rows")
            and performance_audit["execution_cost_floor"].get(
                "duplicate_scope_roles"
            )
            is False
            and _finite_number(
                performance_audit["execution_cost_floor"].get(
                    "maximum_trade_fee_shortfall"
                )
            )
            and float(
                performance_audit["execution_cost_floor"][
                    "maximum_trade_fee_shortfall"
                ]
            )
            <= 1e-12
            and _finite_number(
                performance_audit["execution_cost_floor"].get(
                    "maximum_trade_all_in_shortfall"
                )
            )
            and float(
                performance_audit["execution_cost_floor"][
                    "maximum_trade_all_in_shortfall"
                ]
            )
            <= 1e-12,
            performance_audit.get("execution_cost_floor")
            if isinstance(performance_audit, dict)
            else None,
            "every trade charges at least its preregistered execution fee and all-in drag budgets",
            "cost",
        ),
        _check(
            "reported_performance_matches_source",
            isinstance(performance_audit, dict)
            and portfolio_report.get("monthly_target_gate")
            == performance_audit.get("monthly_target_gate")
            and portfolio_report.get("maximum_gross_leverage")
            == performance_audit.get("maximum_gross_leverage")
            and portfolio_report.get("maximum_absolute_net_leverage")
            == performance_audit.get("maximum_absolute_net_leverage")
            and portfolio_report.get("maximum_trade_leverage_adjusted_mae")
            == performance_audit.get("maximum_trade_leverage_adjusted_mae")
            and portfolio_report.get("liquidations")
            == performance_audit.get("liquidations")
            and portfolio_report.get("rule_violations")
            == performance_audit.get("rule_violations")
            and portfolio_report.get("simultaneous_cost_funding_stress_net_return")
            == (performance_audit.get("stress_returns") or {}).get("simultaneous")
            and portfolio_report.get("simultaneous_cost_funding_stress_max_drawdown")
            == ((performance_audit.get("stress_paths") or {}).get("simultaneous") or {}).get(
                "maximum_drawdown"
            ),
            {
                "monthly_target_gate": portfolio_report.get("monthly_target_gate"),
                "maximum_gross_leverage": portfolio_report.get(
                    "maximum_gross_leverage"
                ),
                "maximum_absolute_net_leverage": portfolio_report.get(
                    "maximum_absolute_net_leverage"
                ),
                "maximum_trade_leverage_adjusted_mae": portfolio_report.get(
                    "maximum_trade_leverage_adjusted_mae"
                ),
                "liquidations": portfolio_report.get("liquidations"),
                "rule_violations": portfolio_report.get("rule_violations"),
                "simultaneous_cost_funding_stress_net_return": portfolio_report.get(
                    "simultaneous_cost_funding_stress_net_return"
                ),
                "simultaneous_cost_funding_stress_max_drawdown": portfolio_report.get(
                    "simultaneous_cost_funding_stress_max_drawdown"
                ),
            },
            "exact source-replayed portfolio performance statistics",
            "evidence",
        ),
        _check(
            "reported_composition_matches_source",
            isinstance(composition_audit, dict)
            and portfolio_report.get("common_daily_observations")
            == composition_audit.get("observations")
            and portfolio_report.get("pairwise_daily_correlations")
            == composition_audit.get("pairwise_daily_correlations")
            and portfolio_report.get("positive_pnl_share_by_sleeve")
            == composition_audit.get("positive_pnl_share_by_sleeve"),
            {
                "common_daily_observations": portfolio_report.get(
                    "common_daily_observations"
                ),
                "pairwise_daily_correlations": portfolio_report.get(
                    "pairwise_daily_correlations"
                ),
                "positive_pnl_share_by_sleeve": portfolio_report.get(
                    "positive_pnl_share_by_sleeve"
                ),
            },
            "exact source-replayed composition statistics",
            "evidence",
        ),
        _check(
            "portfolio_selection_bias",
            not portfolio_selection_bias_failures,
            portfolio_selection_bias_failures,
            "complete current trial inventory, DSR >= threshold, and PBO <= threshold",
            "performance",
        ),
        _check(
            "portfolio_multi_block_risk",
            not portfolio_risk_failures,
            portfolio_risk_failures,
            "source-replayed 7/28-day MTM block bootstrap and downside gates PASS",
            "risk",
        ),
        _check(
            "common_daily_observations",
            reported_common_observations >= policy.minimum_common_daily_observations,
            reported_common_observations,
            f">= {policy.minimum_common_daily_observations}",
            "sample",
        ),
        _check(
            "maximum_gross_leverage",
            isinstance(performance_audit, dict)
            and _finite_number(performance_audit.get("maximum_gross_leverage"))
            and float(performance_audit["maximum_gross_leverage"])
            <= policy.maximum_gross_leverage,
            performance_audit.get("maximum_gross_leverage")
            if isinstance(performance_audit, dict)
            else None,
            f"<= {policy.maximum_gross_leverage}",
            "risk",
        ),
        _check(
            "maximum_absolute_net_leverage",
            isinstance(performance_audit, dict)
            and _finite_number(performance_audit.get("maximum_absolute_net_leverage"))
            and float(performance_audit["maximum_absolute_net_leverage"])
            <= policy.maximum_absolute_net_leverage,
            performance_audit.get("maximum_absolute_net_leverage")
            if isinstance(performance_audit, dict)
            else None,
            f"<= {policy.maximum_absolute_net_leverage}",
            "risk",
        ),
        _check(
            "maximum_intratrade_leverage_adjusted_mae",
            isinstance(performance_audit, dict)
            and _finite_number(
                performance_audit.get("maximum_trade_leverage_adjusted_mae")
            )
            and float(performance_audit["maximum_trade_leverage_adjusted_mae"])
            <= policy.maximum_portfolio_trade_leverage_adjusted_mae,
            performance_audit.get("maximum_trade_leverage_adjusted_mae")
            if isinstance(performance_audit, dict)
            else None,
            f"<= {policy.maximum_portfolio_trade_leverage_adjusted_mae}",
            "risk",
        ),
        _check(
            "zero_liquidations",
            isinstance(performance_audit, dict)
            and performance_audit.get("liquidations") == 0,
            performance_audit.get("liquidations")
            if isinstance(performance_audit, dict)
            else None,
            0,
            "risk",
        ),
        _check(
            "zero_rule_violations",
            isinstance(performance_audit, dict)
            and performance_audit.get("rule_violations")
            == policy.maximum_portfolio_rule_violations,
            performance_audit.get("rule_violations")
            if isinstance(performance_audit, dict)
            else None,
            policy.maximum_portfolio_rule_violations,
            "risk",
        ),
        _check(
            "simultaneous_cost_funding_stress",
            isinstance(performance_audit, dict)
            and _finite_number(
                (performance_audit.get("stress_returns") or {}).get("simultaneous")
            )
            and float(performance_audit["stress_returns"]["simultaneous"]) > 0,
            (performance_audit.get("stress_returns") or {}).get("simultaneous")
            if isinstance(performance_audit, dict)
            else None,
            "> 0",
            "cost",
        ),
        _check(
            "simultaneous_cost_funding_stress_drawdown",
            isinstance(performance_audit, dict)
            and _finite_number(
                ((performance_audit.get("stress_paths") or {}).get("simultaneous") or {}).get(
                    "maximum_drawdown"
                )
            )
            and ((performance_audit.get("stress_paths") or {}).get("simultaneous") or {}).get(
                "ruined"
            )
            is False
            and float(
                performance_audit["stress_paths"]["simultaneous"]["maximum_drawdown"]
            )
            <= policy.maximum_simultaneous_cost_stress_drawdown,
            ((performance_audit.get("stress_paths") or {}).get("simultaneous") or {}).get(
                "maximum_drawdown"
            )
            if isinstance(performance_audit, dict)
            else None,
            f"<= {policy.maximum_simultaneous_cost_stress_drawdown} and no interim insolvency",
            "risk",
        ),
    ]

    pairwise = (
        composition_audit.get("pairwise_daily_correlations")
        if isinstance(composition_audit, dict)
        else None
    )
    required_pairs = len(candidates) * (len(candidates) - 1) // 2
    pairwise_valid = isinstance(pairwise, list) and len(pairwise) == required_pairs
    pair_keys: set[tuple[str, str]] = set()
    maximum_correlation: float | None = None
    if pairwise_valid:
        values: list[float] = []
        for item in pairwise:
            if not isinstance(item, dict):
                pairwise_valid = False
                break
            left, right = sorted((str(item.get("left") or ""), str(item.get("right") or "")))
            value = item.get("correlation")
            key = (left, right)
            if (
                left not in candidates
                or right not in candidates
                or left == right
                or key in pair_keys
                or not _finite_number(value)
                or abs(float(value)) > 1
            ):
                pairwise_valid = False
                break
            pair_keys.add(key)
            values.append(abs(float(value)))
        if pairwise_valid:
            maximum_correlation = max(values, default=0.0)
    checks.append(
        _check(
            "pairwise_daily_correlation_coverage",
            pairwise_valid,
            len(pairwise) if isinstance(pairwise, list) else None,
            required_pairs,
            "evidence",
        )
    )
    checks.append(
        _check(
            "maximum_absolute_pairwise_correlation",
            maximum_correlation is not None
            and maximum_correlation <= policy.maximum_absolute_pairwise_correlation,
            maximum_correlation,
            f"<= {policy.maximum_absolute_pairwise_correlation}",
            "risk",
        )
    )

    contributions = (
        composition_audit.get("positive_pnl_share_by_sleeve")
        if isinstance(composition_audit, dict)
        else None
    )
    contributions_valid = (
        isinstance(contributions, dict)
        and set(contributions) == set(candidates)
        and all(_finite_number(value) and 0 <= float(value) <= 1 for value in contributions.values())
        and abs(sum(float(value) for value in contributions.values()) - 1.0) <= 1e-9
    )
    largest_contribution = (
        max((float(value) for value in contributions.values()), default=None)
        if contributions_valid
        else None
    )
    checks.append(
        _check(
            "positive_pnl_contribution_coverage",
            contributions_valid,
            contributions,
            "all sleeves, finite shares summing to 1",
            "evidence",
        )
    )
    checks.append(
        _check(
            "single_sleeve_positive_pnl_concentration",
            largest_contribution is not None
            and largest_contribution <= policy.maximum_single_sleeve_positive_pnl_share,
            largest_contribution,
            f"<= {policy.maximum_single_sleeve_positive_pnl_share}",
            "risk",
        )
    )

    sleeve_failures: dict[str, list[str]] = {}
    bias_audit_failures: dict[str, list[str]] = {}
    selection_bias_failures: dict[str, list[str]] = {}
    sleeve_pnl_digests = (
        composition_audit.get("sleeve_net_pnl_fraction_sha256")
        if isinstance(composition_audit, dict)
        else {}
    ) or {}
    sleeve_return_digests = (
        composition_audit.get("sleeve_daily_return_sha256")
        if isinstance(composition_audit, dict)
        else {}
    ) or {}
    for candidate in candidates:
        report = sleeve_reports.get(candidate)
        manifest_sleeve = next(
            (
                item
                for item in sleeves
                if isinstance(item, dict) and item.get("candidate") == candidate
            ),
            {},
        )
        reasons: list[str] = []
        bias_reasons: list[str] = []
        selection_reasons: list[str] = []
        if not isinstance(report, dict):
            reasons.append("report missing")
            bias_reasons.append("report missing")
            selection_reasons.append("report missing")
        else:
            if report.get("candidate") != candidate:
                reasons.append("candidate mismatch")
            if report.get("status") != "PASS":
                reasons.append("status is not PASS")
            if report.get("protocol_current") is not True:
                reasons.append("protocol is not current")
            if (report.get("data_integrity") or {}).get("status") != "PASS":
                reasons.append("data integrity is not PASS")
            evidence = report.get("evidence") or {}
            if evidence.get("sample_role") != "locked_forward":
                reasons.append("sample is not locked_forward")
            if evidence.get("pre_registered") is not True:
                reasons.append("sample is not preregistered")
            if evidence.get("parameter_changes_after_start") != 0:
                reasons.append("parameters changed after sample start")
            if evidence.get("daily_net_pnl_fraction_sha256") != sleeve_pnl_digests.get(
                candidate
            ):
                reasons.append(
                    "sleeve report is not bound to its portfolio daily net PnL series"
                )
            sleeve_registered_at = _parse_utc(evidence.get("registered_at_utc"))
            sleeve_started_at = _parse_utc(evidence.get("sample_started_at_utc"))
            sleeve_ended_at = _parse_utc(evidence.get("sample_ended_at_utc"))
            if (
                sleeve_registered_at is None
                or sleeve_started_at is None
                or sleeve_ended_at is None
                or sleeve_registered_at >= sleeve_started_at
                or sleeve_started_at >= sleeve_ended_at
            ):
                reasons.append("sleeve preregistration does not precede sample")
            if (
                sleeve_started_at != sample_started_at
                or sleeve_ended_at != sample_ended_at
            ):
                reasons.append("sleeve locked window does not match the portfolio window")
            bias_audit = report.get("bias_audit") or {}
            if bias_audit.get("candidate") != candidate:
                bias_reasons.append("bias audit candidate mismatch")
            if bias_audit.get("status") != "PASS":
                bias_reasons.append("bias audit status is not PASS")
            if bias_audit.get("protocol_current") is not True:
                bias_reasons.append("bias audit is not bound to the current protocol")
            if not isinstance(bias_audit.get("protocol_sha256"), dict) or not bias_audit.get(
                "protocol_sha256"
            ):
                bias_reasons.append("bias audit protocol digests are missing")
            lookahead = bias_audit.get("lookahead") or {}
            if (
                lookahead.get("passed") is not True
                or lookahead.get("biased_entry_signals") != 0
                or lookahead.get("biased_exit_signals") != 0
            ):
                bias_reasons.append("lookahead analysis did not pass cleanly")
            if not _sha256_text(lookahead.get("artifact_sha256")):
                bias_reasons.append("lookahead artifact digest is missing")
            recursive = bias_audit.get("recursive") or {}
            if (
                recursive.get("passed") is not True
                or recursive.get("indicator_lookahead_bias") is not False
            ):
                bias_reasons.append("recursive analysis did not pass cleanly")
            if not _sha256_text(recursive.get("artifact_sha256")):
                bias_reasons.append("recursive artifact digest is missing")
            selection_reasons.extend(
                _selection_bias_failures(
                    report.get("selection_bias_audit"),
                    policy,
                    expected_observations=reported_common_observations,
                    expected_selected_return_sha256=sleeve_return_digests.get(
                        candidate
                    ),
                    expected_first_timestamp=(
                        composition_audit.get("first_timestamp")
                        if isinstance(composition_audit, dict)
                        else None
                    ),
                    expected_last_timestamp=(
                        composition_audit.get("last_timestamp")
                        if isinstance(composition_audit, dict)
                        else None
                    ),
                    expected_family_id=manifest_sleeve.get(
                        "selection_bias_family_id"
                    ),
                    expected_inventory_id=manifest_sleeve.get(
                        "selection_bias_inventory_id"
                    ),
                )
            )
        if reasons:
            sleeve_failures[candidate] = reasons
        if bias_reasons:
            bias_audit_failures[candidate] = bias_reasons
        if selection_reasons:
            selection_bias_failures[candidate] = selection_reasons
    if set(sleeve_reports) != set(candidates):
        sleeve_failures["__inventory__"] = ["sleeve report inventory is not exact"]
        bias_audit_failures["__inventory__"] = [
            "bias-audited sleeve report inventory is not exact"
        ]
        selection_bias_failures["__inventory__"] = [
            "selection-bias-audited sleeve report inventory is not exact"
        ]
    checks.append(
        _check(
            "standalone_sleeve_promotion",
            not sleeve_failures and bool(candidates),
            sleeve_failures,
            "every exact sleeve independently PASS on locked-forward evidence",
            "evidence",
        )
    )
    checks.append(
        _check(
            "current_sleeve_selection_bias_audits",
            not selection_bias_failures and bool(candidates),
            selection_bias_failures,
            "every exact sleeve has a current complete DSR/PBO audit",
            "performance",
        )
    )
    checks.append(
        _check(
            "current_sleeve_bias_audits",
            not bias_audit_failures and bool(candidates),
            bias_audit_failures,
            "every exact sleeve has current clean lookahead and recursive analysis",
            "evidence",
        )
    )

    audited_scopes = execution_audit.get("scopes")
    execution_failures: list[str] = []
    execution_protocol = execution_audit.get("protocol_sha256")
    if execution_audit.get("protocol_current") is not True:
        execution_failures.append("portfolio execution protocol is not current")
    if (
        not isinstance(execution_protocol, dict)
        or set(execution_protocol) != set(PORTFOLIO_EXECUTION_PROTOCOL_FILES)
        or not all(_sha256_text(value) for value in execution_protocol.values())
    ):
        execution_failures.append("portfolio execution protocol digests are invalid")
    execution_sources = execution_audit.get("sources")
    if not isinstance(execution_sources, dict) or set(execution_sources) != {
        "observation_manifest",
        "mark_to_market_equity",
    }:
        execution_failures.append("portfolio execution source inventory is incomplete")
    else:
        for name, source in execution_sources.items():
            if (
                not isinstance(source, dict)
                or not isinstance(source.get("path"), str)
                or not source.get("path")
                or not _sha256_text(source.get("sha256"))
            ):
                execution_failures.append(f"portfolio execution {name} source is invalid")
        manifest_source = execution_sources.get("observation_manifest")
        segments = (
            manifest_source.get("segments")
            if isinstance(manifest_source, dict)
            else None
        )
        if (
            not isinstance(segments, list)
            or not segments
            or any(
                not isinstance(segment, dict)
                or not segment.get("segment_id")
                or not segment.get("path")
                or not _sha256_text(segment.get("sha256"))
                or not isinstance(segment.get("market_data_manifest"), dict)
                or not segment["market_data_manifest"].get("path")
                or not _sha256_text(
                    segment["market_data_manifest"].get("sha256")
                )
                or not isinstance(
                    segment["market_data_manifest"].get("files"), list
                )
                or not segment["market_data_manifest"].get("files")
                or any(
                    not isinstance(source_file, dict)
                    or not source_file.get("path")
                    or not _sha256_text(source_file.get("sha256"))
                    for source_file in segment["market_data_manifest"].get(
                        "files", []
                    )
                )
                for segment in segments
            )
        ):
            execution_failures.append("portfolio execution sealed segment inventory is invalid")
    execution_checks = execution_audit.get("checks")
    execution_check_names = [
        item.get("name") for item in execution_checks or [] if isinstance(item, dict)
    ]
    expected_execution_checks = {
        "exact_execution_scope_inventory",
        "execution_observations_within_mtm_sample",
        "all_execution_scopes_pass",
    }
    if (
        not isinstance(execution_checks, list)
        or set(execution_check_names) != expected_execution_checks
        or len(execution_check_names) != len(expected_execution_checks)
        or not all(item.get("passed") is True for item in execution_checks)
    ):
        execution_failures.append("portfolio execution replay checks are incomplete or not all PASS")
    if (
        not isinstance(execution_audit.get("observation_rows"), int)
        or execution_audit["observation_rows"] <= 0
    ):
        execution_failures.append("portfolio execution observation row count is invalid")
    if execution_audit.get("portfolio_manifest_sha256") != manifest_sha256:
        execution_failures.append("execution audit is not bound to the portfolio manifest")
    if execution_audit.get("portfolio_promotion_policy_sha256") != policy_sha256:
        execution_failures.append("execution audit is not bound to the portfolio policy")
    if execution_audit.get("status") != "PASS":
        execution_failures.append("execution audit status is not PASS")
    if execution_audit.get("evidence_purpose") != "live_promotion_candidate":
        execution_failures.append("execution evidence is not live-promotion candidate evidence")
    audit_by_key: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    if not isinstance(audited_scopes, list):
        execution_failures.append("execution audit scopes are missing")
        audited_scopes = []
    for item in audited_scopes:
        if not isinstance(item, dict):
            execution_failures.append("execution audit contains an invalid scope")
            continue
        key = execution_scope_key(item)
        if key in audit_by_key:
            execution_failures.append(f"duplicate execution scope: {key}")
        audit_by_key[key] = item
    expected_by_key = {execution_scope_key(item): item for item in expected_scopes}
    if set(audit_by_key) != set(expected_by_key):
        execution_failures.append("execution audit scope inventory is not exact")
    for key, expected in expected_by_key.items():
        item = audit_by_key.get(key)
        if item is None:
            continue
        observations = int(item.get("signal_triggered_observations") or 0)
        fills = int(item.get("fills") or 0)
        order_type = expected["order_type"]
        minimum_fills = (
            policy.minimum_passive_fills_per_scope
            if order_type == "maker_limit"
            else policy.minimum_execution_observations_per_scope
        )
        mean_drag = item.get("mean_all_in_drag_bps")
        p95_drag = item.get("p95_all_in_drag_bps")
        budget = float(expected["maximum_all_in_drag_bps"])
        if (
            item.get("status") != "PASS"
            or item.get("trigger_kind") != expected["trigger_kind"]
            or not _finite_number(item.get("fee_bps"))
            or float(item["fee_bps"]) != float(expected["fee_bps"])
            or observations < policy.minimum_execution_observations_per_scope
            or fills < minimum_fills
            or (order_type != "maker_limit" and fills != observations)
            or float(item.get("trigger_coverage_ratio") or 0.0) != 1.0
            or float(item.get("cost_coverage_ratio") or 0.0) != 1.0
            or float(item.get("raw_replay_coverage_ratio") or 0.0) != 1.0
            or not _finite_number(mean_drag)
            or not _finite_number(p95_drag)
            or float(mean_drag) >= budget
            or float(p95_drag) >= budget
        ):
            execution_failures.append(f"execution scope does not pass: {key}")
    checks.append(
        _check(
            "complete_execution_path_evidence",
            not execution_failures and bool(expected_scopes),
            execution_failures,
            "every exact symbol/direction/order-role scope PASS",
            "cost",
        )
    )

    sample_ready = all(item["passed"] for item in checks if item["kind"] == "sample")
    hard_failure = any(
        not item["passed"] for item in checks if item["kind"] in {"performance", "risk", "cost"}
    )
    passed = all(item["passed"] for item in checks)
    return {
        "schema_version": 1,
        "candidate": manifest.get("candidate"),
        "status": "PASS" if passed else ("FAIL" if sample_ready and hard_failure else "REVIEW"),
        "policy": asdict(policy),
        "expected_execution_scopes": expected_scopes,
        "checks": checks,
        "warning": "PASS is a promotion prerequisite, not a return guarantee or an order authorization.",
    }
