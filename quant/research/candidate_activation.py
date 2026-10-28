"""Fail-closed readiness checks for deferred research candidates."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from math import ceil
from pathlib import Path
from typing import Any


# Health reporters observe the programme but do not determine a candidate's
# data, methodology, or activation-lock contents.  Binding one into the frozen
# method artifact set would let an operational monitoring improvement block a
# preregistration without changing the research contract.
_OPERATIONAL_OBSERVER_ARTIFACTS = frozenset({"scripts/research_health.py"})


def verify_methodology_artifacts(
    methodology_audit: dict[str, Any], *, project_root: Path
) -> dict[str, Any]:
    """Recompute every frozen Alpha 015 artifact hash before readiness use."""
    audited = deepcopy(methodology_audit)
    transfer = audited.get("frozen_transfer_choices") or {}
    activation = audited.get("activation_policy") or {}
    activation_lock = audited.get("activation_lock") or {}
    primary_source = audited.get("primary_source") or {}
    preregistration_pipeline = audited.get("preregistration_pipeline") or {}
    references = [
        audited.get("causal_kernel") or {},
        audited.get("data_guard") or {},
        activation,
        {
            "path": transfer.get("execution_calibration_path"),
            "sha256": transfer.get("execution_calibration_sha256"),
        },
    ]
    references.extend(activation.get("amendments") or [])
    references.extend(activation_lock.get("artifacts") or [])
    references.extend(primary_source.get("artifacts") or [])
    references.extend(preregistration_pipeline.get("artifacts") or [])
    root = project_root.resolve()
    failures: list[str] = []
    for offset, reference in enumerate(references):
        relative = str(reference.get("path") or "")
        expected = str(reference.get("sha256") or "")
        normalized_relative = relative.replace("\\", "/")
        if normalized_relative in _OPERATIONAL_OBSERVER_ARTIFACTS:
            failures.append(f"artifact_{offset}_operational_observer_disallowed")
            continue
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            failures.append(f"artifact_{offset}_path_escape")
            continue
        if not path.is_file():
            failures.append(f"artifact_{offset}_missing")
        elif len(expected) != 64 or sha256(path.read_bytes()).hexdigest() != expected:
            failures.append(f"artifact_{offset}_sha256_mismatch")
    audited.setdefault("resolution_status", {})["protocol_artifacts_current"] = not failures
    audited["artifact_integrity"] = {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
    }
    return audited


def verify_alpha015_contract_semantics(
    methodology_audit: dict[str, Any], *, project_root: Path
) -> dict[str, Any]:
    """Verify that the frozen Alpha 015 documents describe one contract.

    Hash checks prove that each referenced file is unchanged, but cannot prove
    that a newer amendment, the preregistration template, and the immutable
    calibration receipt agree on cadence and costs.  This is intentionally a
    pre-return, fail-closed check.
    """
    root = project_root.resolve()
    failures: list[str] = []
    amendments = (
        (methodology_audit.get("activation_policy") or {}).get("amendments") or []
    )
    amendment_ref = next(
        (
            item
            for item in amendments
            if str((item or {}).get("path") or "").endswith(
                "alpha015-contract-amendment-011.json"
            )
        ),
        None,
    )
    preservation_ref = next(
        (
            item
            for item in amendments
            if str((item or {}).get("path") or "").endswith(
                "alpha015-capital-preservation-amendment-012.json"
            )
        ),
        None,
    )
    template_ref = next(
        (
            item
            for item in ((methodology_audit.get("preregistration_pipeline") or {}).get("artifacts") or [])
            if str((item or {}).get("path") or "").endswith(
                "alpha015-preregistration-template.json"
            )
        ),
        None,
    )
    frozen = methodology_audit.get("frozen_transfer_choices") or {}

    def load(ref: object, label: str) -> tuple[dict[str, Any], Path] | None:
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
            failures.append(f"{label}_reference_missing")
            return None
        path = (root / ref["path"]).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            failures.append(f"{label}_path_escape")
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failures.append(f"{label}_unreadable")
            return None
        if not isinstance(payload, dict):
            failures.append(f"{label}_not_object")
            return None
        return payload, path

    amendment_loaded = load(amendment_ref, "contract_amendment")
    preservation_loaded = load(preservation_ref, "capital_preservation_amendment")
    template_loaded = load(template_ref, "preregistration_template")
    if (
        amendment_loaded is None
        or preservation_loaded is None
        or template_loaded is None
    ):
        return {"status": "FAIL", "failures": failures}
    amendment, amendment_path = amendment_loaded
    preservation, preservation_path = preservation_loaded
    template, _ = template_loaded
    contract = amendment.get("authoritative_contract") or {}
    cadence = contract.get("cadence") or {}
    costs = contract.get("costs") or {}
    calibration = contract.get("execution_calibration") or {}
    portfolio = template.get("portfolio") or {}
    model = template.get("model") or {}
    template_costs = template.get("costs") or {}
    development_gate = template.get("development_gate") or {}
    validation_gate = template.get("locked_validation_gate") or {}

    try:
        grid_minutes = int(cadence["decision_grid_minutes"])
        holding_minutes = int(cadence["holding_minutes"])
        latency_minutes = int(cadence["entry_latency_minutes"])
        base_cost = float(costs["base_all_in_basis_points_per_unit_realized_turnover"])
        stress_cost = float(costs["stress_all_in_basis_points_per_unit_realized_turnover"])
        observed_floor = float(calibration["observed_descriptive_floor_basis_points"])
        calibration_path = str(calibration["path"])
        calibration_sha256 = str(calibration["sha256"])
        normalize_scope = lambda value: str(value).strip().lower().replace(" ", "_")
        expected_scopes = sorted(normalize_scope(value) for value in calibration["measured_scopes"])
        expected_unmeasured_scopes = sorted(
            normalize_scope(value) for value in calibration["required_unmeasured_scopes"]
        )
    except (KeyError, TypeError, ValueError):
        failures.append("contract_amendment_schema_invalid")
        return {"status": "FAIL", "failures": failures}

    if (grid_minutes, holding_minutes, latency_minutes) != (10, 10, 1):
        failures.append("contract_cadence_not_ten_minute_causal_grid")

    def integer_matches(value: object, expected: int, failure: str) -> None:
        try:
            matches = int(value) == expected
        except (TypeError, ValueError):
            matches = False
        if not matches:
            failures.append(failure)

    def numeric_matches(value: object, expected: float, failure: str) -> None:
        try:
            matches = float(value) == expected
        except (TypeError, ValueError):
            matches = False
        if not matches:
            failures.append(failure)

    if portfolio.get("decision_grid") != "10-minute UTC epoch grid":
        failures.append("template_decision_grid_mismatch")
    integer_matches(
        portfolio.get("holding_minutes"),
        holding_minutes,
        "template_holding_minutes_mismatch",
    )
    integer_matches(
        portfolio.get("entry_latency_minutes"),
        latency_minutes,
        "template_latency_minutes_mismatch",
    )
    numeric_matches(
        template_costs.get("base_all_in_basis_points_per_unit_realized_turnover"),
        base_cost,
        "template_base_cost_mismatch",
    )
    numeric_matches(
        template_costs.get("stress_all_in_basis_points_per_unit_realized_turnover"),
        stress_cost,
        "template_stress_cost_mismatch",
    )
    if template_costs.get("cost_contract_amendment") != str(amendment_path.relative_to(root)):
        failures.append("template_contract_amendment_reference_mismatch")
    if base_cost < float(ceil(observed_floor)) or stress_cost != base_cost * 2:
        failures.append("contract_cost_floor_not_conservative")
    if (
        frozen.get("execution_calibration_path") != calibration_path
        or frozen.get("execution_calibration_sha256") != calibration_sha256
    ):
        failures.append("audit_calibration_binding_mismatch")
    receipt_loaded = load({"path": calibration_path}, "execution_calibration")
    if receipt_loaded is not None:
        receipt, receipt_path = receipt_loaded
        actual_sha256 = sha256(receipt_path.read_bytes()).hexdigest()
        if actual_sha256 != calibration_sha256:
            failures.append("execution_calibration_sha256_mismatch")
        actual_floor = ((receipt.get("cost_reference") or {}).get("descriptive_research_stress_floor_bps"))
        try:
            if float(actual_floor) != observed_floor:
                failures.append("execution_calibration_floor_mismatch")
        except (TypeError, ValueError):
            failures.append("execution_calibration_floor_missing")
        if receipt.get("promotion_eligible") is not False:
            failures.append("execution_calibration_must_be_nonpromotion")
        actual_scopes = sorted(
            normalize_scope(value) for value in (receipt.get("measured_scopes") or [])
        )
        if actual_scopes != expected_scopes:
            failures.append("execution_calibration_measured_scope_mismatch")
        actual_unmeasured = sorted(
            normalize_scope(value) for value in (receipt.get("unmeasured_scopes") or [])
        )
        if actual_unmeasured != expected_unmeasured_scopes:
            failures.append("execution_calibration_unmeasured_scope_mismatch")

    preservation_contract = preservation.get("authoritative_risk_contract") or {}
    required_policy = preservation.get("policy_binding") or {}
    candidate_compatible = False
    try:
        base_drawdown = float(
            preservation_contract["maximum_base_minute_mark_to_market_drawdown_fraction"]
        )
        stress_drawdown = float(
            preservation_contract["maximum_stress_minute_mark_to_market_drawdown_fraction"]
        )
    except (KeyError, TypeError, ValueError):
        failures.append("capital_preservation_amendment_schema_invalid")
    else:
        if (
            preservation.get("amendment_id")
            != "ALPHA015_CAPITAL_PRESERVATION_AMENDMENT_012"
            or preservation.get("status") != "PRE_RETURN_POLICY_ALIGNMENT"
            or required_policy.get("risk_policy_id")
            != "CAPITAL_PRESERVATION_RISK_POLICY_V1"
            or required_policy.get("capital_objective_id")
            != "CAPITAL_PRESERVATION_RESEARCH_OBJECTIVE_V2"
        ):
            failures.append("capital_preservation_amendment_identity_invalid")
        if (base_drawdown, stress_drawdown) != (0.10, 0.10):
            failures.append("capital_preservation_drawdown_limit_invalid")
        for name, gate in (
            ("development", development_gate),
            ("locked_validation", validation_gate),
        ):
            checks = gate.get("checks") or {}
            try:
                matches = (
                    float(checks["base_maximum_minute_mark_to_market_drawdown_fraction"])
                    == base_drawdown
                    and float(checks["stress_maximum_minute_mark_to_market_drawdown_fraction"])
                    == stress_drawdown
                )
            except (KeyError, TypeError, ValueError):
                matches = False
            if not matches:
                failures.append(f"template_{name}_capital_preservation_drawdown_mismatch")
        if template.get("capital_preservation_policy_amendment") != str(
            preservation_path.relative_to(root)
        ):
            failures.append("template_capital_preservation_amendment_reference_mismatch")
        if any(
            "monthly" in str(item).lower() and "target" in str(item).lower()
            for item in (template.get("post_validation_requirements") or [])
        ):
            failures.append("template_monthly_return_target_forbidden")
        compatibility = preservation.get("candidate_compatibility") or {}
        try:
            maximum_positions = int(
                preservation_contract["maximum_simultaneous_positions"]
            )
            minimum_symbols = int(model["minimum_symbols"])
            buckets = int(portfolio["buckets"])
            template_positions = 2 * (minimum_symbols // buckets)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            failures.append("capital_preservation_position_limit_schema_invalid")
        else:
            candidate_compatible = template_positions <= maximum_positions
            expected_compatibility_status = (
                "COMPATIBLE" if candidate_compatible else "VETO_MAXIMUM_SIMULTANEOUS_POSITIONS"
            )
            if (
                compatibility.get("status") != expected_compatibility_status
                or int(compatibility.get("policy_limit") or -1) != maximum_positions
                or int(compatibility.get("minimum_concurrent_futures_positions") or -1)
                != template_positions
            ):
                failures.append("capital_preservation_position_veto_invalid")
            if template_positions > maximum_positions:
                failures.append("template_simultaneous_position_limit_breach")
    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "cadence": {
            "decision_grid_minutes": grid_minutes,
            "holding_minutes": holding_minutes,
            "entry_latency_minutes": latency_minutes,
        },
        "costs": {"base_bps": base_cost, "stress_bps": stress_cost},
        "capital_preservation": {
            "amendment_path": str(preservation_path.relative_to(root)),
            "maximum_minute_mark_to_market_drawdown": 0.10,
            "monthly_return_target": "NOT_A_GATE",
            "candidate_compatible": candidate_compatible,
        },
        "calibration_path": calibration_path,
        "calibration_measured_scopes": expected_scopes,
    }


def evaluate_alpha015_activation(
    *,
    policy: dict[str, Any],
    universe_report: dict[str, Any],
    registry: dict[str, Any],
    screening: dict[str, Any],
    methodology_audit: dict[str, Any] | None = None,
    activation_lock_verified: bool,
    preregistration_verified: bool = False,
) -> dict[str, Any]:
    candidate = str(policy.get("candidate") or "")
    requirements = policy.get("activation_requirements") or {}
    expected_days = int(requirements.get("point_in_time_universe_consecutive_days") or 0)
    selected = screening.get("selected_next_step")
    screened_candidate = next(
        (
            item for item in screening.get("candidates", [])
            if item.get("name") == "MINUTE_CROSS_CRYPTO_PREDICTABILITY"
        ),
        {},
    )
    alpha15_records = [
        item for item in registry.get("experiments", [])
        if int(((item.get("trial_accounting") or {}).get("alpha_number") or -1)) == 15
        or str(item.get("strategy") or "").startswith("ALPHA_015")
    ]
    universe_status = str(universe_report.get("status") or "MISSING")
    current_days = int(universe_report.get("consecutive_days_ending_latest") or 0)
    reported_required_days = int(universe_report.get("required_consecutive_days") or 0)
    integrity_ok = universe_status not in {
        "DATA_INTEGRITY_FAIL",
        "ACCUMULATING_WITH_GAPS",
        "MISSING",
    }
    methodology_audit = methodology_audit or {}
    resolution = methodology_audit.get("resolution_status") or {}
    capital_preservation = (
        (methodology_audit.get("contract_semantics") or {}).get(
            "capital_preservation"
        )
        or {}
    )
    capital_preservation_candidate_compatible = (
        capital_preservation.get("candidate_compatible") is not False
    )
    unresolved_methodology = sorted(
        name for name, passed in resolution.items() if passed is not True
    )
    methodology_current = (
        methodology_audit.get("candidate") == candidate
        and methodology_audit.get("experiment_0015_open") is False
        and bool(resolution)
    )
    artifact_integrity_ok = (
        (methodology_audit.get("artifact_integrity") or {}).get("status") == "PASS"
    )
    checks = {
        "candidate_identity_present": candidate == "CANDIDATE_MINUTE_CROSS_CRYPTO_PREDICTABILITY",
        "policy_is_deferred_not_experiment": policy.get("status") == "DEFERRED_NOT_AN_EXPERIMENT",
        "candidate_screened_defer_high_priority": screened_candidate.get("decision") == "DEFER_HIGH_PRIORITY",
        "alpha015_registration_valid_or_absent": not alpha15_records or preregistration_verified,
        "universe_data_integrity": integrity_ok,
        "universe_requirement_matches_policy": reported_required_days == expected_days and expected_days > 0,
        "universe_protocol_ready": universe_status == requirements.get("universe_protocol_status"),
        "point_in_time_days_minimum": current_days >= expected_days,
        "prior_day_activation_lock_verified": activation_lock_verified is True,
        "methodology_audit_current": methodology_current,
        "methodology_artifact_integrity": artifact_integrity_ok,
        "methodology_contract_consistency": resolution.get("contract_terms_consistent") is True,
        "capital_preservation_candidate_compatible": capital_preservation_candidate_compatible,
        "methodology_resolved_before_preregistration": methodology_current and not unresolved_methodology,
        "screening_did_not_select_candidate_for_immediate_test": selected != candidate,
    }
    contract_checks = (
        "candidate_identity_present",
        "policy_is_deferred_not_experiment",
        "candidate_screened_defer_high_priority",
        "alpha015_registration_valid_or_absent",
        "universe_requirement_matches_policy",
        "methodology_audit_current",
        "methodology_artifact_integrity",
        "methodology_contract_consistency",
        "capital_preservation_candidate_compatible",
        "screening_did_not_select_candidate_for_immediate_test",
    )
    activation_contract_ok = all(checks[name] for name in contract_checks)
    if not capital_preservation_candidate_compatible:
        status = "INCOMPATIBLE_WITH_CAPITAL_PRESERVATION_POLICY"
    elif len(alpha15_records) > 1:
        status = "ACTIVATION_CONTRACT_BLOCK"
    elif alpha15_records and not preregistration_verified:
        status = "PREMATURE_EXPERIMENT_REGISTERED"
    elif not integrity_ok:
        status = "DATA_INTEGRITY_BLOCK"
    elif not activation_contract_ok:
        status = "ACTIVATION_CONTRACT_BLOCK"
    elif not (checks["universe_protocol_ready"] and checks["point_in_time_days_minimum"]):
        status = "WAITING_POINT_IN_TIME_UNIVERSE"
    elif not checks["prior_day_activation_lock_verified"]:
        status = "WAITING_ACTIVATION_LOCK"
    elif not checks["methodology_resolved_before_preregistration"]:
        status = "WAITING_METHODOLOGY_LOCK"
    elif alpha15_records:
        status = "READY_FOR_DATA_ACQUISITION"
    else:
        status = "READY_FOR_PREREGISTRATION_ONLY"
    return {
        "schema_version": 1,
        "candidate": candidate,
        "status": status,
        "experiment_registered": bool(alpha15_records),
        "preregistration_authorized": status == "READY_FOR_PREREGISTRATION_ONLY",
        "return_data_authorized": status == "READY_FOR_DATA_ACQUISITION",
        "current_consecutive_universe_days": current_days,
        "required_consecutive_universe_days": expected_days,
        "days_remaining": max(0, expected_days - current_days),
        "unresolved_methodology": unresolved_methodology,
        "checks": checks,
        "next_action": (
            (
                "Acquire only the source-bound development archives; locked validation remains inaccessible."
                if status == "READY_FOR_DATA_ACQUISITION"
                else "Write and hash the exact Experiment 0015 protocol before acquiring minute returns."
            )
            if status in {"READY_FOR_PREREGISTRATION_ONLY", "READY_FOR_DATA_ACQUISITION"}
            else (
                "Resolve every methodology ambiguity without inspecting returns; do not open Experiment 0015."
                if status == "WAITING_METHODOLOGY_LOCK"
                else (
                    "Alpha 015 requires more concurrent futures positions than the policy permits. Do not alter its locked model; screen a new two-position candidate before acquiring any returns."
                    if status == "INCOMPATIBLE_WITH_CAPITAL_PRESERVATION_POLICY"
                    else (
                    "Repair the activation contract and rerun every source check; do not acquire returns."
                    if status == "ACTIVATION_CONTRACT_BLOCK"
                    else (
                        "Create and replay the write-once prior-day universe activation lock; do not acquire returns."
                        if status == "WAITING_ACTIVATION_LOCK"
                        else "Continue sealed daily universe collection; do not open Experiment 0015."
                    )
                    )
                )
            )
        ),
        "warning": "Only a verified preregistration may authorize source-bound development data acquisition; locked validation and live trading remain closed.",
    }
