"""Freeze a chronological release train for archival research windows.

Historical archives can remove the wait for an initial diagnostic, but they
must not let a researcher repeatedly inspect later periods and tune against
them.  This guard treats each later period as a withheld release: it becomes
available only after a candidate has been independently preregistered.  The
guard is deliberately non-promotional because the underlying archive universe
still lacks contemporaneous contract-status and order-book evidence.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping


class HistoricalReleaseTrainError(ValueError):
    """A release schedule is incomplete, mutable, or unsafe."""


_BASELINE = "SEALED_BASELINE"
_WITHHELD = "WITHHELD_UNTOUCHED"


def _check(name: str, passed: bool, *, required: Any, actual: Any) -> dict[str, Any]:
    return {"name": name, "passed": passed, "required": required, "actual": actual}


def _parse_boundary(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise HistoricalReleaseTrainError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise HistoricalReleaseTrainError(f"{name} must be an ISO date") from error


def evaluate_historical_release_train(
    policy: Mapping[str, Any],
    *,
    baseline_universe_sha256: str | None = None,
    baseline_audit_status: str | None = None,
) -> dict[str, Any]:
    """Validate chronology and the immutable baseline binding.

    The function never opens a withheld partition or evaluates returns.  It
    only emits a governance receipt showing which single period may be used
    now and which periods must remain unseen.
    """
    if not isinstance(policy, Mapping):
        raise HistoricalReleaseTrainError("historical release train must be an object")
    partitions = policy.get("partitions")
    if not isinstance(partitions, list) or len(partitions) < 2:
        raise HistoricalReleaseTrainError("release train needs one baseline and one withheld partition")

    ids: set[str] = set()
    prior_end: date | None = None
    normalized: list[dict[str, Any]] = []
    for index, partition in enumerate(partitions):
        if not isinstance(partition, Mapping):
            raise HistoricalReleaseTrainError("release partition must be an object")
        partition_id = partition.get("id")
        if not isinstance(partition_id, str) or not partition_id or partition_id in ids:
            raise HistoricalReleaseTrainError("release partition ids must be unique nonempty strings")
        ids.add(partition_id)
        start = _parse_boundary(partition.get("start"), "partition start")
        end_exclusive = _parse_boundary(partition.get("end_exclusive"), "partition end_exclusive")
        if start >= end_exclusive:
            raise HistoricalReleaseTrainError("release partition must have a positive date range")
        if prior_end is not None and start < prior_end:
            raise HistoricalReleaseTrainError("release partitions must not overlap")
        prior_end = end_exclusive
        state = partition.get("state")
        required_state = _BASELINE if index == 0 else _WITHHELD
        normalized.append({
            "id": partition_id,
            "start": start.isoformat(),
            "end_exclusive": end_exclusive.isoformat(),
            "state": state,
            "state_valid": state == required_state,
            "universe_sha256": partition.get("universe_sha256"),
        })

    baseline = normalized[0]
    withheld = normalized[1:]
    rules = policy.get("release_rules")
    if not isinstance(rules, Mapping):
        raise HistoricalReleaseTrainError("release train needs release_rules")
    checks = [
        _check("schema_version", policy.get("schema_version") == 1, required=1, actual=policy.get("schema_version")),
        _check("preregistered", policy.get("status") == "PREREGISTERED", required="PREREGISTERED", actual=policy.get("status")),
        _check("live_order_authorization_closed", policy.get("live_order_authorization") == "CLOSED", required="CLOSED", actual=policy.get("live_order_authorization")),
        _check("promotion_capability", policy.get("promotion_capability") == "DIAGNOSTIC_ONLY", required="DIAGNOSTIC_ONLY", actual=policy.get("promotion_capability")),
        _check("partition_states", all(item["state_valid"] for item in normalized), required=[_BASELINE, _WITHHELD], actual=[item["state"] for item in normalized]),
        _check("baseline_audit_pass", baseline_audit_status == "PASS", required="PASS", actual=baseline_audit_status),
        _check("baseline_digest_bound", isinstance(baseline["universe_sha256"], str) and baseline["universe_sha256"] == baseline_universe_sha256, required=baseline["universe_sha256"], actual=baseline_universe_sha256),
        _check("candidate_preregistration_before_release", rules.get("candidate_preregistration") == "REQUIRED_BEFORE_WITHHELD_PARTITION_ACCESS", required="REQUIRED_BEFORE_WITHHELD_PARTITION_ACCESS", actual=rules.get("candidate_preregistration")),
        _check("one_partition_per_evaluation", rules.get("evaluation_scope") == "EXACTLY_ONE_RELEASED_PARTITION", required="EXACTLY_ONE_RELEASED_PARTITION", actual=rules.get("evaluation_scope")),
        _check("no_retuning_after_release", rules.get("post_release_retuning") == "FORBIDDEN", required="FORBIDDEN", actual=rules.get("post_release_retuning")),
    ]
    return {
        "schema_version": 1,
        "status": "PASS" if all(item["passed"] for item in checks) else "FAIL",
        "kind": "HISTORICAL_RELEASE_GOVERNANCE_ONLY",
        "checks": checks,
        "baseline": baseline,
        "next_withheld_partition": withheld[0],
        "remaining_withheld_partitions": [item["id"] for item in withheld],
        "promotion_capability": "DIAGNOSTIC_ONLY",
        "live_order_authorization": "CLOSED",
        "warning": "This receipt controls historical access order. It is not a strategy result or a live-trading approval.",
    }
