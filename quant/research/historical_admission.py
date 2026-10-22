"""Guard the role of historical evidence in the research programme.

Historical archives are indispensable for quickly rejecting weak ideas, but
they cannot manufacture a forward, point-in-time result.  This small guard
keeps those two roles explicit so a static surviving-symbol diagnostic can
never accidentally become evidence for a live promotion.
"""

from __future__ import annotations

from typing import Any, Mapping


class HistoricalAdmissionError(ValueError):
    """The historical-research policy is malformed or unsafe."""


_LANES = {
    "ARCHIVAL_DIAGNOSTIC",
    "HISTORICAL_POINT_IN_TIME_REPLICATION",
    "FORWARD_CONFIRMATION",
}


def _check(name: str, passed: bool, *, required: Any, actual: Any) -> dict[str, Any]:
    return {"name": name, "passed": passed, "required": required, "actual": actual}


def evaluate_historical_admission(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the fixed three-lane evidence policy without reading returns.

    A policy is valid only when it gives historical diagnostics an explicit
    non-promotion cap, requires a timestamped historical membership source for
    any historical point-in-time claim, and still retains a forward
    confirmation lane.  Thus this report is a governance receipt, never an
    alpha result.
    """
    if not isinstance(policy, Mapping):
        raise HistoricalAdmissionError("historical policy must be an object")
    lanes = policy.get("lanes")
    if not isinstance(lanes, list):
        raise HistoricalAdmissionError("historical policy lanes must be a list")
    indexed: dict[str, Mapping[str, Any]] = {}
    for lane in lanes:
        if not isinstance(lane, Mapping) or not isinstance(lane.get("id"), str):
            raise HistoricalAdmissionError("each historical policy lane needs an id")
        lane_id = lane["id"]
        if lane_id not in _LANES or lane_id in indexed:
            raise HistoricalAdmissionError("historical policy lane inventory is invalid")
        indexed[lane_id] = lane
    if set(indexed) != _LANES:
        raise HistoricalAdmissionError("historical policy must contain exactly three lanes")

    archival = indexed["ARCHIVAL_DIAGNOSTIC"]
    point_in_time = indexed["HISTORICAL_POINT_IN_TIME_REPLICATION"]
    forward = indexed["FORWARD_CONFIRMATION"]
    feature_contract = policy.get("feature_availability_contract")
    historical_oi = (
        feature_contract.get("historical_open_interest")
        if isinstance(feature_contract, Mapping)
        else None
    )
    forward_oi = (
        feature_contract.get("forward_open_interest")
        if isinstance(feature_contract, Mapping)
        else None
    )
    checks = [
        _check("schema_version", policy.get("schema_version") == 1, required=1, actual=policy.get("schema_version")),
        _check("policy_active", policy.get("status") == "ACTIVE", required="ACTIVE", actual=policy.get("status")),
        _check("live_order_authorization_closed", policy.get("live_order_authorization") == "CLOSED", required="CLOSED", actual=policy.get("live_order_authorization")),
        _check("archival_is_non_promotional", archival.get("promotion_capability") == "DIAGNOSTIC_ONLY", required="DIAGNOSTIC_ONLY", actual=archival.get("promotion_capability")),
        _check("archival_requires_publisher_checksums", archival.get("publisher_checksum_required") is True, required=True, actual=archival.get("publisher_checksum_required")),
        _check("archival_requires_preregistered_costs", archival.get("costs_preregistered") is True, required=True, actual=archival.get("costs_preregistered")),
        _check("historical_pit_requires_membership_source", point_in_time.get("membership_source_required") is True, required=True, actual=point_in_time.get("membership_source_required")),
        _check("historical_pit_cannot_fallback_to_current_symbols", point_in_time.get("current_symbol_fallback") == "FORBIDDEN", required="FORBIDDEN", actual=point_in_time.get("current_symbol_fallback")),
        _check(
            "historical_decision_feature_availability_rule",
            isinstance(feature_contract, Mapping)
            and feature_contract.get("decision_feature_rule")
            == "A historical decision feature requires both an event timestamp and evidence that it was available no later than the decision timestamp.",
            required="event timestamp plus no-later-than-decision availability evidence",
            actual=(feature_contract or {}).get("decision_feature_rule")
            if isinstance(feature_contract, Mapping)
            else None,
        ),
        _check(
            "historical_open_interest_blocked_without_publication_time",
            isinstance(historical_oi, Mapping)
            and historical_oi.get("availability_status") == "UNVERIFIED_PUBLICATION_TIME"
            and historical_oi.get("allowed_as_historical_decision_feature") is False,
            required="UNVERIFIED_PUBLICATION_TIME and not eligible as a historical decision feature",
            actual=historical_oi if isinstance(historical_oi, Mapping) else None,
        ),
        _check(
            "forward_open_interest_requires_observed_provenance",
            isinstance(forward_oi, Mapping)
            and forward_oi.get("allowed_as_forward_decision_feature") is True
            and isinstance(forward_oi.get("required_evidence"), list)
            and set(forward_oi["required_evidence"])
            == {
                "source endpoint",
                "local UTC receive timestamp",
                "append-only raw capture",
                "preregistered decision delay",
            },
            required="observed endpoint, receive time, append-only capture, and decision delay",
            actual=forward_oi if isinstance(forward_oi, Mapping) else None,
        ),
        _check("forward_confirmation_retained", forward.get("minimum_consecutive_days") == 180, required=180, actual=forward.get("minimum_consecutive_days")),
        _check("forward_is_non_live", forward.get("promotion_capability") == "REVIEW_ONLY", required="REVIEW_ONLY", actual=forward.get("promotion_capability")),
    ]
    return {
        "schema_version": 1,
        "status": "PASS" if all(item["passed"] for item in checks) else "FAIL",
        "kind": "HISTORICAL_EVIDENCE_GOVERNANCE_ONLY",
        "checks": checks,
        "historical_point_in_time_available_now": point_in_time.get("availability") == "READY",
        "live_order_authorization": "CLOSED",
        "warning": (
            "A PASS validates evidence boundaries only. It does not validate a "
            "strategy, return, or live-trading eligibility."
        ),
    }
