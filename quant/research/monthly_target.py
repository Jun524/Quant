"""Evidence gate for an aggressive net 10% monthly research objective."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone


def _parse_utc(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class MonthlyTargetPolicy:
    target_geometric_monthly_return: float = 0.10
    minimum_complete_months: int = 12
    minimum_organic_trades: int = 100
    minimum_positive_month_ratio: float = 0.60
    maximum_worst_month_loss: float = 0.10
    maximum_drawdown: float = 0.20
    minimum_profit_factor: float = 1.20
    maximum_probability_of_ruin: float = 0.01
    maximum_probability_terminal_loss: float = 0.10
    minimum_probability_reaching_annual_target: float = 0.50
    maximum_bootstrap_drawdown_p95: float = 0.20
    maximum_leverage: float = 3.0
    maximum_trade_adverse_excursion: float = 0.50
    require_mark_to_market_equity: bool = True
    require_zero_liquidations: bool = True
    require_double_cost_positive: bool = True
    require_slippage_stress_positive: bool = False
    require_funding_stress_positive: bool = False
    require_locked_forward_evidence: bool = True


def evaluate_monthly_target(
    base: dict,
    *,
    double_cost: dict | None,
    slippage_stress: dict | None = None,
    funding_stress: dict | None = None,
    risk: dict | None = None,
    evidence: dict | None = None,
    policy: MonthlyTargetPolicy | None = None,
) -> dict:
    policy = policy or MonthlyTargetPolicy()
    monthly = base.get("monthly") or {}
    organic = base.get("organic") or {}
    trade_risk = base.get("trade_risk") or {}
    evidence = evidence or {}
    registered_at = _parse_utc(evidence.get("registered_at_utc"))
    sample_started_at = _parse_utc(evidence.get("sample_started_at_utc"))
    checks = [
        {
            "name": "complete_month_sample",
            "passed": int(monthly.get("complete_months") or 0) >= policy.minimum_complete_months,
            "actual": int(monthly.get("complete_months") or 0),
            "required": policy.minimum_complete_months,
            "kind": "sample",
        },
        {
            "name": "organic_trade_sample",
            "passed": int(organic.get("trades") or 0) >= policy.minimum_organic_trades,
            "actual": int(organic.get("trades") or 0),
            "required": policy.minimum_organic_trades,
            "kind": "sample",
        },
        {
            "name": "geometric_monthly_return",
            "passed": float(monthly.get("geometric_monthly_return") or 0.0) >= policy.target_geometric_monthly_return,
            "actual": monthly.get("geometric_monthly_return"),
            "required": policy.target_geometric_monthly_return,
            "kind": "performance",
        },
        {
            "name": "positive_month_ratio",
            "passed": float(monthly.get("positive_month_ratio") or 0.0) >= policy.minimum_positive_month_ratio,
            "actual": monthly.get("positive_month_ratio"),
            "required": policy.minimum_positive_month_ratio,
            "kind": "risk",
        },
        {
            "name": "worst_month_loss",
            "passed": monthly.get("worst_month") is not None
            and float(monthly["worst_month"]) >= -policy.maximum_worst_month_loss,
            "actual": monthly.get("worst_month"),
            "required": f">= {-policy.maximum_worst_month_loss}",
            "kind": "risk",
        },
        {
            "name": "max_drawdown",
            "passed": base.get("max_drawdown") is not None
            and float(base["max_drawdown"]) <= policy.maximum_drawdown,
            "actual": base.get("max_drawdown"),
            "required": f"<= {policy.maximum_drawdown}",
            "kind": "risk",
        },
        {
            "name": "profit_factor",
            "passed": organic.get("profit_factor") is not None and float(organic["profit_factor"]) >= policy.minimum_profit_factor,
            "actual": organic.get("profit_factor"),
            "required": policy.minimum_profit_factor,
            "kind": "performance",
        },
    ]
    if policy.require_mark_to_market_equity:
        checks.append(
            {
                "name": "mark_to_market_equity_available",
                "passed": base.get("equity_basis") == "mark_to_market",
                "actual": base.get("equity_basis"),
                "required": "mark_to_market",
                "kind": "evidence",
            }
        )
    if policy.require_locked_forward_evidence:
        checks.extend(
            [
                {
                    "name": "locked_forward_sample",
                    "passed": evidence.get("sample_role") == "locked_forward",
                    "actual": evidence.get("sample_role"),
                    "required": "locked_forward",
                    "kind": "evidence",
                },
                {
                    "name": "sample_preregistered",
                    "passed": evidence.get("pre_registered") is True,
                    "actual": evidence.get("pre_registered"),
                    "required": True,
                    "kind": "evidence",
                },
                {
                    "name": "no_parameter_changes_after_sample_start",
                    "passed": evidence.get("parameter_changes_after_start") == 0,
                    "actual": evidence.get("parameter_changes_after_start"),
                    "required": 0,
                    "kind": "evidence",
                },
                {
                    "name": "preregistration_precedes_sample",
                    "passed": registered_at is not None
                    and sample_started_at is not None
                    and registered_at < sample_started_at,
                    "actual": {
                        "registered_at_utc": evidence.get("registered_at_utc"),
                        "sample_started_at_utc": evidence.get("sample_started_at_utc"),
                    },
                    "required": "registered_at_utc < sample_started_at_utc",
                    "kind": "evidence",
                },
            ]
        )
    checks.extend(
        [
            {
                "name": "trade_excursion_evidence_complete",
                "passed": float(trade_risk.get("excursion_coverage_ratio") or 0.0) == 1.0,
                "actual": trade_risk.get("excursion_coverage_ratio"),
                "required": 1.0,
                "kind": "evidence",
            },
            {
                "name": "maximum_leverage",
                "passed": trade_risk.get("maximum_leverage") is not None
                and float(trade_risk["maximum_leverage"]) <= policy.maximum_leverage,
                "actual": trade_risk.get("maximum_leverage"),
                "required": f"<= {policy.maximum_leverage}",
                "kind": "risk",
            },
            {
                "name": "maximum_trade_adverse_excursion",
                "passed": trade_risk.get("maximum_leverage_adjusted_mae") is not None
                and float(trade_risk["maximum_leverage_adjusted_mae"]) <= policy.maximum_trade_adverse_excursion,
                "actual": trade_risk.get("maximum_leverage_adjusted_mae"),
                "required": f"<= {policy.maximum_trade_adverse_excursion}",
                "kind": "risk",
            },
            {
                "name": "bootstrap_probability_of_ruin",
                "passed": risk is not None
                and risk.get("probability_of_ruin") is not None
                and float(risk["probability_of_ruin"]) <= policy.maximum_probability_of_ruin,
                "actual": risk.get("probability_of_ruin") if risk else None,
                "required": f"<= {policy.maximum_probability_of_ruin}",
                "kind": "risk",
            },
            {
                "name": "bootstrap_probability_terminal_loss",
                "passed": risk is not None
                and risk.get("probability_terminal_loss") is not None
                and float(risk["probability_terminal_loss"]) <= policy.maximum_probability_terminal_loss,
                "actual": risk.get("probability_terminal_loss") if risk else None,
                "required": f"<= {policy.maximum_probability_terminal_loss}",
                "kind": "risk",
            },
            {
                "name": "bootstrap_probability_reaching_annual_target",
                "passed": risk is not None
                and risk.get("probability_reaching_annual_target") is not None
                and float(risk["probability_reaching_annual_target"])
                >= policy.minimum_probability_reaching_annual_target,
                "actual": risk.get("probability_reaching_annual_target") if risk else None,
                "required": f">= {policy.minimum_probability_reaching_annual_target}",
                "kind": "performance",
            },
            {
                "name": "bootstrap_maximum_drawdown_p95",
                "passed": risk is not None
                and risk.get("maximum_drawdown_p95") is not None
                and float(risk["maximum_drawdown_p95"]) <= policy.maximum_bootstrap_drawdown_p95,
                "actual": risk.get("maximum_drawdown_p95") if risk else None,
                "required": f"<= {policy.maximum_bootstrap_drawdown_p95}",
                "kind": "risk",
            },
        ]
    )
    if policy.require_zero_liquidations:
        checks.append(
            {
                "name": "liquidation_exits",
                "passed": trade_risk.get("liquidation_exits") == 0,
                "actual": trade_risk.get("liquidation_exits"),
                "required": 0,
                "kind": "risk",
            }
        )
    if policy.require_double_cost_positive:
        checks.append(
            {
                "name": "double_cost_net_positive",
                "passed": double_cost is not None and float((double_cost.get("organic") or {}).get("net_return") or 0.0) > 0,
                "actual": (double_cost.get("organic") or {}).get("net_return") if double_cost else None,
                "required": "> 0",
                "kind": "cost",
            }
        )
    if policy.require_slippage_stress_positive:
        checks.extend(
            [
                {
                    "name": "slippage_evidence_complete",
                    "passed": float(trade_risk.get("slippage_coverage_ratio") or 0.0) == 1.0,
                    "actual": trade_risk.get("slippage_coverage_ratio"),
                    "required": 1.0,
                    "kind": "evidence",
                },
                {
                    "name": "slippage_stress_net_positive",
                    "passed": slippage_stress is not None
                    and float((slippage_stress.get("organic") or {}).get("net_return") or 0.0) > 0,
                    "actual": (slippage_stress.get("organic") or {}).get("net_return")
                    if slippage_stress
                    else None,
                    "required": "> 0",
                    "kind": "cost",
                },
            ]
        )
    if policy.require_funding_stress_positive:
        checks.extend(
            [
                {
                    "name": "funding_evidence_complete",
                    "passed": float(trade_risk.get("funding_coverage_ratio") or 0.0) == 1.0,
                    "actual": trade_risk.get("funding_coverage_ratio"),
                    "required": 1.0,
                    "kind": "evidence",
                },
                {
                    "name": "adverse_funding_stress_net_positive",
                    "passed": funding_stress is not None
                    and float((funding_stress.get("organic") or {}).get("net_return") or 0.0) > 0,
                    "actual": (funding_stress.get("organic") or {}).get("net_return")
                    if funding_stress
                    else None,
                    "required": "> 0 after removing favorable funding and doubling adverse funding",
                    "kind": "cost",
                },
            ]
        )
    sample_ready = all(item["passed"] for item in checks if item["kind"] == "sample")
    hard_failure = any(not item["passed"] for item in checks if item["kind"] in {"performance", "risk", "cost"})
    passed = all(item["passed"] for item in checks)
    return {
        "status": "PASS" if passed else ("FAIL" if sample_ready and hard_failure else "REVIEW"),
        "policy": asdict(policy),
        "checks": checks,
        "warning": (
            "Mark-to-market block bootstrap is a scenario test, not a promise of future returns."
            if base.get("equity_basis") == "mark_to_market"
            else "Closed-trade bootstrap risk is preliminary; PASS requires mark-to-market equity and is not a promise of future returns."
        ),
    }
