"""Fail-closed authorization to preregister a future portfolio sample.

This is deliberately not a promotion gate.  It only prevents a portfolio
sample from being opened before there are enough independent sleeves to make a
combined test meaningful.  The full source-replayed promotion gate remains in
``portfolio_promotion.py``.
"""

from __future__ import annotations

from typing import Any


def _candidate_sleeves(registry: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract only independent, preregistered locked-forward registry PASSes."""
    candidates: list[dict[str, Any]] = []
    for experiment in registry.get("experiments") or []:
        if not isinstance(experiment, dict):
            continue
        result = experiment.get("result") or {}
        gate = result.get("gate") or {}
        accounting = experiment.get("trial_accounting") or {}
        evidence = result.get("portfolio_activation_evidence") or {}
        strategy = str(experiment.get("strategy") or "")
        family = str(accounting.get("family") or "")
        if (
            gate.get("status") == "PASS"
            and accounting.get("independent_alpha") is not False
            and strategy
            and family
            and evidence.get("sample_role") == "locked_forward"
            and evidence.get("pre_registered") is True
            and evidence.get("parameter_changes_after_start") == 0
            and evidence.get("locked_validation_status") == "PASS"
        ):
            candidates.append(
                {
                    "experiment_id": experiment.get("experiment_id"),
                    "strategy": strategy,
                    "family": family,
                    "locked_forward_evidence": evidence,
                }
            )
    return candidates


def evaluate_portfolio_activation(
    policy: dict[str, Any], registry: dict[str, Any]
) -> dict[str, Any]:
    """Assess whether a *new* portfolio preregistration may be written.

    Registry PASS is intentionally only a pre-registration prerequisite: the
    eventual portfolio must still independently prove locked-forward samples,
    costs, execution paths, risk, and the monthly objective.
    """
    requirements = policy.get("activation_requirements") or {}
    minimum_sleeves = int(requirements.get("minimum_independent_sleeves") or 0)
    required_months = int(requirements.get("minimum_complete_locked_forward_months") or 0)
    candidates = _candidate_sleeves(registry)
    distinct_families = sorted({item["family"] for item in candidates})
    contract_checks = {
        "policy_deferred_not_portfolio": policy.get("status") == "DEFERRED_NOT_OPENED",
        "live_order_authorization_closed": policy.get("live_order_authorization") == "CLOSED",
        "minimum_independent_sleeves_valid": minimum_sleeves >= 2,
        "distinct_family_requirement_valid": requirements.get("require_distinct_families") is True,
        "locked_forward_month_requirement_valid": required_months >= 12,
        "registry_schema_valid": registry.get("schema_version") == 1,
    }
    contract_ok = all(contract_checks.values())
    independent_sleeves_ok = len(candidates) >= minimum_sleeves
    family_diversification_ok = len(distinct_families) >= minimum_sleeves
    if not contract_ok:
        status = "ACTIVATION_CONTRACT_BLOCK"
    elif not (independent_sleeves_ok and family_diversification_ok):
        status = "WAITING_INDEPENDENT_SLEEVES"
    else:
        status = "READY_FOR_PORTFOLIO_PREREGISTRATION_ONLY"
    return {
        "schema_version": 1,
        "candidate": policy.get("candidate"),
        "status": status,
        "preregistration_authorized": status == "READY_FOR_PORTFOLIO_PREREGISTRATION_ONLY",
        "portfolio_return_data_authorized": False,
        "live_order_authorization": "CLOSED",
        "eligible_registry_pass_sleeves": candidates,
        "eligible_registry_pass_families": distinct_families,
        "minimum_independent_sleeves": minimum_sleeves,
        "minimum_complete_locked_forward_months": required_months,
        "checks": {
            **contract_checks,
            "independent_sleeve_count": independent_sleeves_ok,
            "distinct_independent_families": family_diversification_ok,
        },
        "next_action": (
            "Write and hash a standalone portfolio manifest before its first UTC-midnight observation; then collect its own locked-forward evidence."
            if status == "READY_FOR_PORTFOLIO_PREREGISTRATION_ONLY"
            else "Continue independent sleeve research; do not open a portfolio sample or submit orders."
        ),
        "warning": "Registry PASS is not promotion evidence. Portfolio promotion remains unavailable until its separately preregistered locked-forward, source-replayed, execution, risk, and selection-bias gates pass.",
    }
