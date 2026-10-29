"""Reproducible source binding for liquidation-telemetry incident records."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping


def _inside(root: Path, relative: object) -> Path | None:
    if not isinstance(relative, str) or not relative:
        return None
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    return path


def _matching_gap(
    records: object, *, expected_symbols: set[str], expected_gap_ms: int
) -> bool:
    if not isinstance(records, list):
        return False
    symbols = {
        str(record.get("symbol") or "").upper()
        for record in records
        if isinstance(record, Mapping)
        and int(record.get("gap_ms") or -1) == expected_gap_ms
    }
    return symbols == expected_symbols


def verify_mark_gap_incident(
    incident: Mapping[str, Any], *, project_root: Path, coverage: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind an excluded telemetry-day incident to immutable source and replay.

    The incident is evidence of an exclusion, never permission to repair the
    day or reopen a result.  A later clean sample can progress independently,
    but the recorded source and its excluded day must remain reproducible.
    """
    failures: list[str] = []
    source = incident.get("immutable_source")
    affected = incident.get("affected_range")
    if not isinstance(source, Mapping) or not isinstance(affected, Mapping):
        return {"status": "FAIL", "failures": ["incident_schema_invalid"]}
    for name, path_key, digest_key in (
        ("manifest", "manifest", "manifest_sha256"),
        ("telemetry", "telemetry", "telemetry_sha256"),
    ):
        path = _inside(project_root, source.get(path_key))
        expected = source.get(digest_key)
        if path is None:
            failures.append(f"{name}_path_invalid")
        elif not path.is_file():
            failures.append(f"{name}_missing")
        elif not isinstance(expected, str) or sha256(path.read_bytes()).hexdigest() != expected:
            failures.append(f"{name}_sha256_mismatch")
    day = affected.get("utc_day")
    try:
        expected_gap_ms = int(affected.get("gap_ms"))
    except (TypeError, ValueError):
        expected_gap_ms = -1
        failures.append("affected_gap_invalid")
    expected_symbols = {str(item).upper() for item in (affected.get("symbols") or [])}
    if not isinstance(day, str) or not expected_symbols:
        failures.append("affected_range_invalid")
    continuity = coverage.get("mark_price_continuity")
    gap_failures = (
        continuity.get("gap_failures") if isinstance(continuity, Mapping) else None
    )
    actual_records = gap_failures.get(day) if isinstance(gap_failures, Mapping) else None
    if not _matching_gap(
        actual_records, expected_symbols=expected_symbols, expected_gap_ms=expected_gap_ms
    ):
        failures.append("coverage_gap_does_not_match_incident")
    if day in set(coverage.get("complete_utc_days") or []):
        failures.append("affected_day_still_counted_complete")
    daily = next(
        (
            item
            for item in (coverage.get("daily_coverage") or [])
            if isinstance(item, Mapping) and item.get("date") == day
        ),
        None,
    )
    if not isinstance(daily, Mapping) or daily.get("complete") is not False:
        failures.append("affected_day_exclusion_not_replayed")
    return {
        "status": "PASS" if not failures else "FAIL",
        "incident_id": incident.get("incident_id"),
        "affected_day": day,
        "failures": failures,
        "promotion_eligible": False,
    }
