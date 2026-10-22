"""Non-promotional inventory checks for versioned execution calibrations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any


POLICY = "periodic-public-l2-entry-calibration-v1"
MINIMUM_TEMPORAL_SEPARATION_HOURS = 24.0
REQUIRED_EXECUTION_SCOPES = (
    "long_entry",
    "long_exit",
    "short_entry",
    "short_exit",
    "protective_stop",
)


def _canonical_scopes(payload: dict[str, Any]) -> set[str]:
    """Extract explicitly reported order-path roles from a calibration receipt.

    Older, sealed receipts describe their role in prose.  Keep that narrow
    compatibility path so the inventory can expose its missing paths without
    rewriting historical evidence.
    """
    explicit = payload.get("measured_scopes")
    if isinstance(explicit, list):
        return {
            str(scope).strip().lower().replace(" ", "_")
            for scope in explicit
            if str(scope).strip().lower().replace(" ", "_")
            in REQUIRED_EXECUTION_SCOPES
        }
    scope = str(payload.get("scope") or "").lower()
    measured: set[str] = set()
    if "long entry" in scope:
        measured.add("long_entry")
    if "long exit" in scope:
        measured.add("long_exit")
    if "short entry" in scope:
        measured.add("short_entry")
    if "short exit" in scope:
        measured.add("short_exit")
    if "protective stop" in scope:
        measured.add("protective_stop")
    return measured


def _utc_timestamp(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("calibration timing lacks timezone")
    return parsed.astimezone(timezone.utc)


def _bound_source_path(project_root: Path, value: object, *, label: str) -> Path:
    raw = Path(str(value))
    if raw.is_absolute():
        raise ValueError(f"{label} must be project-relative")
    resolved = (project_root / raw).resolve()
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError as error:
        raise ValueError(f"{label} escapes the project root") from error
    return resolved


def _verify_source_binding(source: dict[str, Any], *, project_root: Path) -> None:
    """Rehash the raw rows and manifest named by a versioned receipt."""
    for label, path_key, digest_key in (
        ("manifest", "manifest_path", "manifest_sha256"),
        ("raw", "raw_path", "raw_sha256"),
    ):
        path = _bound_source_path(project_root, source[path_key], label=label)
        expected = source[digest_key]
        if not isinstance(expected, str) or len(expected) != 64:
            raise ValueError(f"{label} source digest is invalid")
        if not path.is_file():
            raise ValueError(f"{label} source is missing")
        actual = sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"{label} source digest mismatch")


def _temporal_independence(receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """Require a materially separated pair before calling receipts independent.

    Unique files collected minutes apart are separate recordings, but they are
    not evidence from separate liquidity conditions.  This is intentionally a
    descriptive precondition, not a profitability or promotion gate.
    """
    intervals = sorted(
        (
            _utc_timestamp(receipt["first_submitted_at"]),
            _utc_timestamp(receipt["last_observed_at"]),
        )
        for receipt in receipts
    )
    latest_observed_at = max((end for _, end in intervals), default=None)
    next_eligible = (
        latest_observed_at + timedelta(hours=MINIMUM_TEMPORAL_SEPARATION_HOURS)
        if latest_observed_at is not None
        else None
    )
    gaps = [
        (later_start - earlier_end).total_seconds() / 3600
        for (_, earlier_end), (later_start, _) in zip(intervals, intervals[1:])
    ]
    maximum_gap = max(gaps) if gaps else None
    return {
        "minimum_separation_hours": MINIMUM_TEMPORAL_SEPARATION_HOURS,
        "observed_inter_receipt_gaps_hours": gaps,
        "maximum_inter_receipt_gap_hours": maximum_gap,
        "temporally_separated_pair_available": bool(
            maximum_gap is not None
            and maximum_gap >= MINIMUM_TEMPORAL_SEPARATION_HOURS
        ),
        "latest_observed_at": (
            latest_observed_at.isoformat() if latest_observed_at is not None else None
        ),
        "next_eligible_independent_window_start_at": (
            next_eligible.isoformat() if next_eligible is not None else None
        ),
        "warning": "Temporal separation reduces same-window dependence; it does not prove regime coverage or profitability.",
    }


def _descriptive_cost_dispersion(receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """Expose observed cost variation without selecting or changing a cost model."""
    values = [
        float(receipt["conservative_all_in_execution_drag_bps"])
        for receipt in receipts
    ]
    stress_floors = [float(receipt["descriptive_research_stress_floor_bps"]) for receipt in receipts]
    return {
        "receipt_count": len(values),
        "minimum_observed_all_in_entry_drag_bps": min(values) if values else None,
        "maximum_observed_all_in_entry_drag_bps": max(values) if values else None,
        "range_bps": max(values) - min(values) if values else None,
        "maximum_observed_descriptive_stress_floor_bps": (
            max(stress_floors) if stress_floors else None
        ),
        "automatic_cost_model_mutation": False,
        "warning": "Observed receipt dispersion is descriptive only. Do not average, select the minimum, or replace a preregistered cost model from this field.",
    }


def summarize_versioned_calibration_receipts(
    reports_directory: Path, *, minimum_receipts: int = 2
) -> dict[str, Any]:
    """Describe whether separate calibration windows are available to compare.

    This is deliberately an evidence-inventory observer. It does not average
    costs, decide an execution budget, establish profitability, or authorize
    any order path.
    """
    if minimum_receipts < 2:
        raise ValueError("minimum_receipts must be at least two")
    receipts: list[dict[str, Any]] = []
    failures: list[str] = []
    manifests: set[str] = set()
    raw_paths: set[str] = set()
    observed_scopes: set[str] = set()
    project_root = reports_directory.resolve().parent
    for path in sorted(reports_directory.glob("execution-calibration-*.json")):
        # The inventory itself shares the human-facing filename prefix.  It is
        # an observer, not a calibration receipt, and must never turn an
        # otherwise valid receipt inventory into a data-integrity failure.
        if path.name == "execution-calibration-stability.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            source = payload["source"]
            timing = payload["timing"]
            manifest = str(source["manifest_path"])
            raw_path = str(source["raw_path"])
            first_submitted_at = _utc_timestamp(timing["first_submitted_at"])
            last_observed_at = _utc_timestamp(timing["last_observed_at"])
            if not isinstance(source, dict):
                raise ValueError("source binding is invalid")
            _verify_source_binding(source, project_root=project_root)
            summary = payload.get("summary")
            costs = payload.get("cost_reference")
            if not isinstance(summary, dict) or not isinstance(costs, dict):
                raise ValueError("calibration metrics are missing")
            if (
                costs.get("includes_fee") is not True
                or costs.get("do_not_add_trading_fee_twice") is not True
            ):
                raise ValueError("calibration fee-accounting declaration is invalid")
            all_in_drag = float(summary["conservative_all_in_execution_drag_bps"])
            worst_symbol_drag = float(costs["worst_symbol_observed_all_in_drag_bps"])
            stress_floor = float(costs["descriptive_research_stress_floor_bps"])
            fill_rate = float(summary["fill_rate_by_qty"])
            if (
                all_in_drag < 0
                or worst_symbol_drag < 0
                or stress_floor < worst_symbol_drag
                or not 0 <= fill_rate <= 1
            ):
                raise ValueError("calibration metrics are outside valid bounds")
            if (
                payload.get("schema_version") != 1
                or payload.get("policy") != POLICY
                or payload.get("promotion_eligible") is not False
                or not manifest
                or not raw_path
                or not timing.get("first_submitted_at")
                or not timing.get("last_observed_at")
                or last_observed_at < first_submitted_at
                or manifest in manifests
                or raw_path in raw_paths
            ):
                raise ValueError("invalid or duplicated source binding")
            manifests.add(manifest)
            raw_paths.add(raw_path)
            measured_scopes = sorted(_canonical_scopes(payload))
            observed_scopes.update(measured_scopes)
            receipts.append(
                {
                    "path": str(path),
                    "status": payload.get("status"),
                    "manifest_path": manifest,
                    "raw_path": raw_path,
                    "first_submitted_at": first_submitted_at.isoformat(),
                    "last_observed_at": last_observed_at.isoformat(),
                    "orders": summary.get("orders"),
                    "measured_scopes": measured_scopes,
                    "fill_rate_by_qty": fill_rate,
                    "conservative_all_in_execution_drag_bps": all_in_drag,
                    "worst_symbol_observed_all_in_drag_bps": worst_symbol_drag,
                    "descriptive_research_stress_floor_bps": stress_floor,
                }
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            failures.append(f"{path.name}:{type(error).__name__}")
    temporal_independence = _temporal_independence(receipts) if not failures else {
        "minimum_separation_hours": MINIMUM_TEMPORAL_SEPARATION_HOURS,
        "observed_inter_receipt_gaps_hours": [],
        "maximum_inter_receipt_gap_hours": None,
        "temporally_separated_pair_available": False,
        "latest_observed_at": None,
        "next_eligible_independent_window_start_at": None,
        "warning": "No temporal-independence assessment is valid while receipt integrity fails.",
    }
    if failures:
        status = "DATA_INTEGRITY_FAIL"
    elif len(receipts) < minimum_receipts:
        status = "WAITING_INDEPENDENT_CALIBRATION_RECEIPTS"
    elif not temporal_independence["temporally_separated_pair_available"]:
        status = "WAITING_TEMPORALLY_SEPARATED_RECEIPTS"
    else:
        status = "READY_FOR_DESCRIPTIVE_COMPARISON"
    required_scopes = list(REQUIRED_EXECUTION_SCOPES)
    missing_scopes = sorted(set(required_scopes) - observed_scopes)
    cost_dispersion = _descriptive_cost_dispersion(receipts)
    return {
        "schema_version": 1,
        "kind": "VERSIONED_EXECUTION_CALIBRATION_INVENTORY_ONLY",
        "status": status,
        "minimum_receipts": minimum_receipts,
        "valid_receipts": len(receipts),
        "receipts": receipts,
        "failures": failures,
        "temporal_independence": temporal_independence,
        "descriptive_cost_dispersion": cost_dispersion,
        "execution_path_coverage": {
            "required_scopes": required_scopes,
            "observed_scopes": sorted(observed_scopes),
            "missing_scopes": missing_scopes,
            "complete": not missing_scopes,
            "status": "DESCRIPTIVE_ONLY_COMPLETE" if not missing_scopes else "INCOMPLETE",
            "warning": "Public shadow observations do not replace exchange acknowledgements or authorize live orders.",
        },
        "promotion_capability": "DIAGNOSTIC_ONLY",
        "automatic_cost_model_mutation": False,
        "live_order_authorization": "CLOSED",
        "warning": "Multiple short public-L2 windows still cannot establish alpha profitability or complete execution-path evidence.",
    }
