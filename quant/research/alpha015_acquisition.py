"""Authorization, planning, and resumability for Alpha 015 inputs."""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable

from quant.research.alpha015_data_guard import (
    MAX_COMPRESSED_BYTES,
    Alpha015DataError,
    archive_spec,
    authorized_archive_days,
    funding_spec,
    required_phase_archive_specs,
)


class Alpha015AcquisitionError(RuntimeError):
    pass


def canonical_sha256(value: Any) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _self_digest(payload: dict[str, Any], field: str) -> str:
    return canonical_sha256({key: value for key, value in payload.items() if key != field})


def build_phase_acquisition_plan(
    *,
    readiness: dict[str, Any],
    preregistration: dict[str, Any],
    activation_lock: dict[str, Any],
    phase: str,
    validation_unlock: dict[str, Any] | None = None,
    expected_development_report_sha256: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Authorize one exact sealed phase without accessing source bytes.

    Development is authorized only by readiness.  Locked validation has no
    alternate path: its calendar access is delegated to the receipt validator
    in ``authorized_archive_days`` and is therefore bound to the development
    report hash as well as the preregistration and prior-day calendar.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if phase not in {"development", "locked_validation"}:
        raise Alpha015AcquisitionError("phase must be development or locked_validation")
    if phase == "development":
        if not (
            readiness.get("status") == "READY_FOR_DATA_ACQUISITION"
            and readiness.get("experiment_registered") is True
            and readiness.get("preregistration_authorized") is False
            and readiness.get("return_data_authorized") is True
            and not readiness.get("unresolved_methodology")
            and all((readiness.get("checks") or {}).values())
        ):
            raise Alpha015AcquisitionError(
                "source-bound development acquisition is not authorized"
            )
    if not (
        preregistration.get("status") == "PREREGISTERED_SOURCE_ACQUISITION_LOCKED"
        and preregistration.get("return_data_acquired") is False
        and preregistration.get("locked_validation_open") is False
        and preregistration.get("live_order_authorization") == "CLOSED"
        and preregistration.get("preregistration_sha256")
        == _self_digest(preregistration, "preregistration_sha256")
    ):
        raise Alpha015AcquisitionError("preregistration receipt is invalid")
    if not (
        activation_lock.get("status") == "LOCKED_FOR_PREREGISTRATION_ONLY"
        and activation_lock.get("validation_open") is False
        and activation_lock.get("lock_sha256")
        == _self_digest(activation_lock, "lock_sha256")
        and activation_lock.get("lock_sha256")
        == (preregistration.get("activation_binding") or {}).get("lock_sha256")
        and activation_lock.get("split_sha256")
        == (preregistration.get("activation_binding") or {}).get("split_sha256")
    ):
        raise Alpha015AcquisitionError("activation lock binding is invalid")

    if phase == "locked_validation":
        try:
            # This call is the authorization decision.  Do it before planning
            # any URL or directory, so an invalid receipt cannot reveal a
            # single locked archive target.
            authorized_archive_days(
                activation_lock,
                phase=phase,
                dataset="aggTrades",
                validation_unlock=validation_unlock,
                expected_preregistration_sha256=preregistration["preregistration_sha256"],
                expected_development_report_sha256=expected_development_report_sha256,
            )
        except (Alpha015DataError, KeyError, ValueError) as error:
            raise Alpha015AcquisitionError("locked validation remains inaccessible") from error

    specifications = required_phase_archive_specs(activation_lock, phase=phase)
    planned: list[dict[str, Any]] = []
    for item in specifications:
        day = date.fromisoformat(item["date"])
        if day >= now.date():
            raise Alpha015AcquisitionError(
                "phase plan contains an incomplete or future UTC day"
            )
        dataset = item["dataset"]
        symbol = item["symbol"]
        try:
            authorized = authorized_archive_days(
                activation_lock,
                phase=phase,
                dataset=dataset,
                validation_unlock=validation_unlock,
                expected_preregistration_sha256=preregistration["preregistration_sha256"],
                expected_development_report_sha256=expected_development_report_sha256,
            )
        except (Alpha015DataError, KeyError, ValueError) as error:
            raise Alpha015AcquisitionError("phase authorization failed") from error
        if item["date"] not in authorized:
            raise Alpha015AcquisitionError("planned archive falls outside the authorized phase")
        if dataset == "funding":
            source = funding_spec(symbol, day)
            relative_file = str(
                Path(dataset) / symbol / item["date"] / source["file_name"]
            )
            planned.append(
                {
                    **item,
                    "source_url": source["url"],
                    "file": relative_file,
                }
            )
        else:
            source = archive_spec(dataset, symbol, day)
            directory = Path(dataset) / symbol / item["date"]
            planned.append(
                {
                    **item,
                    "source_url": source["url"],
                    "checksum_url": f"{source['url']}.CHECKSUM",
                    "archive": str(directory / source["archive_name"]),
                    "checksum": str(
                        directory / f"{source['archive_name']}.CHECKSUM"
                    ),
                }
            )
    body = {
        "schema_version": 1,
        "candidate": preregistration["candidate"],
        "phase": phase,
        "status": "AUTHORIZED_PLAN_NO_BYTES_ACQUIRED",
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "activation_lock_sha256": activation_lock["lock_sha256"],
        "split_sha256": activation_lock["split_sha256"],
        "evaluation_start": activation_lock[phase][0],
        "evaluation_end": activation_lock[phase][-1],
        "locked_validation_start": activation_lock["locked_validation"][0],
        "locked_validation_open": phase == "locked_validation",
        "files": planned,
        "file_count": len(planned),
        "live_order_authorization": "CLOSED",
    }
    if phase == "locked_validation":
        body["development_report_sha256"] = expected_development_report_sha256
        body["validation_unlock_sha256"] = canonical_sha256(validation_unlock or {})
    return {**body, "plan_sha256": canonical_sha256(body)}


def build_development_acquisition_plan(
    *,
    readiness: dict[str, Any],
    preregistration: dict[str, Any],
    activation_lock: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Backward-compatible development-only acquisition entry point."""
    return build_phase_acquisition_plan(
        readiness=readiness,
        preregistration=preregistration,
        activation_lock=activation_lock,
        phase="development",
        now=now,
    )


def validate_phase_acquisition_plan(
    plan: dict[str, Any],
    *,
    readiness: dict[str, Any],
    preregistration: dict[str, Any],
    activation_lock: dict[str, Any],
    phase: str,
    validation_unlock: dict[str, Any] | None = None,
    expected_development_report_sha256: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Replay a stored plan; no mutable receipt or wider phase is accepted."""
    try:
        expected = build_phase_acquisition_plan(
            readiness=readiness,
            preregistration=preregistration,
            activation_lock=activation_lock,
            phase=phase,
            validation_unlock=validation_unlock,
            expected_development_report_sha256=expected_development_report_sha256,
            now=now,
        )
    except (Alpha015AcquisitionError, KeyError, TypeError, ValueError):
        return False
    return plan == expected


def validate_development_acquisition_plan(
    plan: dict[str, Any],
    *,
    readiness: dict[str, Any],
    preregistration: dict[str, Any],
    activation_lock: dict[str, Any],
    now: datetime | None = None,
) -> bool:
    return validate_phase_acquisition_plan(
        plan,
        readiness=readiness,
        preregistration=preregistration,
        activation_lock=activation_lock,
        phase="development",
        now=now,
    )


def assess_storage_budget(
    plan: dict[str, Any],
    *,
    archive_content_lengths: dict[tuple[str, str, str], int],
    free_bytes: int,
    reserve_bytes: int = 25 * 1024**3,
    safety_multiplier: float = 1.25,
    funding_allowance_bytes_each: int = 1_000_000,
) -> dict[str, Any]:
    """Require exact archive sizes and preserve a fixed free-space reserve."""
    if free_bytes < 0 or reserve_bytes < 0 or safety_multiplier < 1:
        raise Alpha015AcquisitionError("storage budget inputs are invalid")
    expected_archives = {
        (item["dataset"], item["symbol"], item["date"])
        for item in plan.get("files") or []
        if item.get("dataset") != "funding"
    }
    if set(archive_content_lengths) != expected_archives:
        raise Alpha015AcquisitionError("archive size inventory is not exact")
    for identity, length in archive_content_lengths.items():
        if length <= 0 or length > MAX_COMPRESSED_BYTES:
            raise Alpha015AcquisitionError(
                f"archive size is invalid: {'|'.join(identity)}"
            )
    funding_files = sum(
        item.get("dataset") == "funding" for item in plan.get("files") or []
    )
    estimated = sum(archive_content_lengths.values()) + (
        funding_files * funding_allowance_bytes_each
    )
    required = int(estimated * safety_multiplier) + reserve_bytes
    return {
        "status": "PASS" if free_bytes >= required else "INSUFFICIENT_STORAGE",
        "archive_files": len(expected_archives),
        "funding_files": funding_files,
        "estimated_payload_bytes": estimated,
        "safety_multiplier": safety_multiplier,
        "reserve_bytes": reserve_bytes,
        "required_free_bytes": required,
        "observed_free_bytes": free_bytes,
        "bytes_short": max(0, required - free_bytes),
    }


def build_size_inventory_receipt(
    plan: dict[str, Any],
    measurements: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    expected = {
        (item["dataset"], item["symbol"], item["date"])
        for item in plan.get("files") or []
        if item.get("dataset") != "funding"
    }
    rows = list(measurements)
    actual = [
        (str(item.get("dataset")), str(item.get("symbol")), str(item.get("date")))
        for item in rows
    ]
    if len(actual) != len(set(actual)) or not set(actual).issubset(expected):
        raise Alpha015AcquisitionError(
            "archive size measurements are duplicate or outside the plan"
        )
    for item in rows:
        length = int(item.get("content_length") or 0)
        if length <= 0 or length > MAX_COMPRESSED_BYTES:
            raise Alpha015AcquisitionError("archive content length is invalid")
    ordered = sorted(
        rows,
        key=lambda item: (item["dataset"], item["symbol"], item["date"]),
    )
    body = {
        "schema_version": 1,
        "candidate": plan["candidate"],
        "phase": plan["phase"],
        "status": "COMPLETE" if len(ordered) == len(expected) else "ACCUMULATING",
        "plan_sha256": plan["plan_sha256"],
        "measured_archives": len(ordered),
        "required_archives": len(expected),
        "measurements": ordered,
        "locked_validation_open": bool(plan.get("locked_validation_open")),
        "live_order_authorization": "CLOSED",
    }
    return {**body, "size_inventory_sha256": canonical_sha256(body)}


def verify_size_inventory_receipt(
    receipt: dict[str, Any], plan: dict[str, Any]
) -> bool:
    try:
        expected = build_size_inventory_receipt(
            plan, receipt.get("measurements") or []
        )
    except (Alpha015AcquisitionError, KeyError, TypeError, ValueError):
        return False
    return receipt == expected


def build_progress_receipt(
    plan: dict[str, Any],
    completed_files: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    expected_by_identity = {
        (item["dataset"], item["symbol"], item["date"]): item
        for item in plan.get("files") or []
    }
    expected = set(expected_by_identity)
    files = list(completed_files)
    actual = [
        (str(item.get("dataset")), str(item.get("symbol")), str(item.get("date")))
        for item in files
    ]
    if len(actual) != len(set(actual)) or not set(actual).issubset(expected):
        raise Alpha015AcquisitionError(
            "completed acquisition inventory is duplicate or outside the plan"
        )
    for item, identity in zip(files, actual):
        planned = expected_by_identity[identity]
        digest = str(item.get("sha256") or "")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise Alpha015AcquisitionError("completed file digest is invalid")
        if int(item.get("bytes") or 0) <= 0:
            raise Alpha015AcquisitionError("completed file byte count is invalid")
        if identity[0] == "funding":
            if (
                item.get("status") != "API_VERIFIED_AND_SEALED"
                or item.get("file") != planned.get("file")
            ):
                raise Alpha015AcquisitionError(
                    "completed funding file does not match the plan"
                )
        elif (
            item.get("status") != "VERIFIED_AND_SEALED"
            or item.get("archive") != planned.get("archive")
            or item.get("checksum") != planned.get("checksum")
        ):
            raise Alpha015AcquisitionError(
                "completed publisher archive does not match the plan"
            )
    ordered = sorted(
        files,
        key=lambda item: (item["dataset"], item["symbol"], item["date"]),
    )
    body = {
        "schema_version": 1,
        "candidate": plan["candidate"],
        "phase": plan["phase"],
        "status": "COMPLETE" if len(ordered) == len(expected) else "ACCUMULATING",
        "plan_sha256": plan["plan_sha256"],
        "preregistration_sha256": plan["preregistration_sha256"],
        "activation_lock_sha256": plan["activation_lock_sha256"],
        "completed_files": len(ordered),
        "required_files": len(expected),
        "files": ordered,
        "locked_validation_open": bool(plan.get("locked_validation_open")),
        "live_order_authorization": "CLOSED",
    }
    return {**body, "progress_sha256": canonical_sha256(body)}


def verify_progress_receipt(progress: dict[str, Any], plan: dict[str, Any]) -> bool:
    try:
        expected = build_progress_receipt(plan, progress.get("files") or [])
    except (Alpha015AcquisitionError, KeyError, TypeError, ValueError):
        return False
    return progress == expected
