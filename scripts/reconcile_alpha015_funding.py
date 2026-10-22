#!/usr/bin/env python3
"""Seal and reconcile every Alpha 015 funding event to publisher monthly ZIPs."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.alpha015_data_guard import (
    MAX_COMPRESSED_BYTES,
    Alpha015DataError,
    load_reverified_funding_events,
    monthly_funding_archive_spec,
    reconcile_funding_events,
    validate_monthly_funding_archive,
)
from quant.research.alpha015_preregistration import canonical_sha256
from quant.research.alpha015_replay import Alpha015ReplayError, prepare_alpha015_phase_bundle
from scripts.open_alpha015_validation import validate_validation_unlock


PREREGISTRATION_PATH = PROJECT_ROOT / "research" / "alpha015-preregistration.json"
ACTIVATION_LOCK_PATH = PROJECT_ROOT / "research" / "alpha015_activation" / "activation-lock.json"
DEVELOPMENT_REPORT_PATH = PROJECT_ROOT / "research" / "alpha015_development" / "report.json"
UNLOCK_PATH = PROJECT_ROOT / "research" / "alpha015_development" / "validation-unlock.json"
DEVELOPMENT_ROOT = PROJECT_ROOT / "research" / "alpha015_data" / "development"
VALIDATION_ROOT = PROJECT_ROOT / "research" / "alpha015_data" / "locked_validation"
OUTPUT_ROOT = PROJECT_ROOT / "research" / "alpha015_funding_reconciliation"
REPORT_PATH = OUTPUT_ROOT / "report.json"


class Alpha015FundingReconciliationError(RuntimeError):
    pass


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise Alpha015FundingReconciliationError(
            f"required Alpha 015 artifact is unavailable: {path.name}"
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise Alpha015FundingReconciliationError(f"invalid Alpha 015 artifact: {path.name}") from error
    if not isinstance(value, dict):
        raise Alpha015FundingReconciliationError(f"Alpha 015 artifact is not an object: {path.name}")
    return value


def _fetch(url: str, *, maximum_bytes: int = MAX_COMPRESSED_BYTES) -> bytes:
    request = Request(url, headers={"User-Agent": "quant-alpha015-funding-reconciler/1"})
    with urlopen(request, timeout=60) as response:
        payload = response.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise Alpha015FundingReconciliationError("publisher monthly funding response exceeded limit")
    return payload


def _write_once_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise Alpha015FundingReconciliationError(f"sealed publisher artifact differs: {path.name}")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _month_start(value: str) -> date:
    day = date.fromisoformat(value)
    return day.replace(day=1)


def _monthly_record(
    *, symbol: str, month: date, archive_payload: bytes, checksum_payload: bytes
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    verified = validate_monthly_funding_archive(
        archive_payload, checksum_payload, symbol=symbol, month=month
    )
    return (
        {
            "symbol": symbol,
            "month": month.isoformat(),
            "archive_name": verified["archive_name"],
            "sha256": verified["sha256"],
            "checksum_sha256": sha256(checksum_payload).hexdigest(),
            "events": len(verified["events"]),
        },
        list(verified["events"]),
    )


def build_reconciliation_receipt(
    *,
    candidate: str,
    preregistration_sha256: str,
    development_report_sha256: str,
    activation_lock_sha256: str,
    development_manifest_sha256: str,
    validation_manifest_sha256: str,
    authorized_symbol_days: list[tuple[str, str]],
    daily_events: list[dict[str, Any]],
    monthly_records: list[dict[str, Any]],
    monthly_events: list[dict[str, Any]],
) -> dict[str, Any]:
    reconciliation = reconcile_funding_events(
        daily_events,
        monthly_events,
        authorized_symbol_days=authorized_symbol_days,
    )
    if len({day for _, day in authorized_symbol_days}) != 180:
        raise Alpha015FundingReconciliationError("funding reconciliation must cover exactly 180 evaluation days")
    body = {
        "schema_version": 1,
        "candidate": candidate,
        "status": "PASS",
        "preregistration_sha256": preregistration_sha256,
        "development_report_sha256": development_report_sha256,
        "activation_lock_sha256": activation_lock_sha256,
        "development_manifest_sha256": development_manifest_sha256,
        "validation_manifest_sha256": validation_manifest_sha256,
        "authorized_evaluation_days": 180,
        "authorized_symbol_days": len(authorized_symbol_days),
        "all_daily_funding_events_reconciled": True,
        "reconciliation": reconciliation,
        "monthly_archives": sorted(monthly_records, key=lambda item: (item["symbol"], item["month"])),
        "live_order_authorization": "CLOSED",
    }
    return {**body, "source_sha256": canonical_sha256(body)}


def verify_reconciliation_receipt(receipt: dict[str, Any]) -> bool:
    """Replay the immutable receipt shape before another stage trusts it."""
    try:
        digest = str(receipt["source_sha256"])
        body = {key: value for key, value in receipt.items() if key != "source_sha256"}
        records = receipt["monthly_archives"]
        reconciliation = receipt["reconciliation"]
        identities = [(str(item["symbol"]), str(item["month"])) for item in records]
    except (KeyError, TypeError):
        return False
    return (
        len(digest) == 64
        and digest == canonical_sha256(body)
        and receipt.get("status") == "PASS"
        and receipt.get("all_daily_funding_events_reconciled") is True
        and isinstance(records, list)
        and identities == sorted(identities)
        and len(identities) == len(set(identities))
        and all(
            len(str(item.get("sha256") or "")) == 64
            and len(str(item.get("checksum_sha256") or "")) == 64
            and int(item.get("events") or 0) > 0
            for item in records
        )
        and reconciliation.get("status") == "PASS"
        and int(reconciliation.get("authorized_symbol_days") or 0) > 0
        and int(reconciliation.get("events_reconciled") or 0) > 0
    )


def _phase_funding_events(bundle: dict[str, Any], *, archive_root: Path) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    authorized: list[tuple[str, str]] = []
    daily_events: list[dict[str, Any]] = []
    for item in (bundle.get("records") or {}).values():
        if item.get("dataset") != "funding":
            continue
        symbol, day = str(item["symbol"]), str(item["date"])
        authorized.append((symbol, day))
        daily_events.extend(load_reverified_funding_events(item, archive_root=archive_root))
    if not authorized:
        raise Alpha015FundingReconciliationError("phase has no reverified funding files")
    return authorized, daily_events


def _download_or_reverify_monthly(
    *, symbol: str, month: date, fetch: Callable[[str], bytes]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    spec = monthly_funding_archive_spec(symbol, month)
    directory = OUTPUT_ROOT / "monthly" / symbol / month.isoformat()
    archive_path = directory / spec["archive_name"]
    checksum_path = directory / f"{spec['archive_name']}.CHECKSUM"
    if archive_path.is_file() and checksum_path.is_file():
        archive_payload, checksum_payload = archive_path.read_bytes(), checksum_path.read_bytes()
    else:
        checksum_payload = fetch(f"{spec['url']}.CHECKSUM")
        archive_payload = fetch(spec["url"])
        # Validate before a single publisher byte is sealed on disk.
        validate_monthly_funding_archive(
            archive_payload, checksum_payload, symbol=symbol, month=month
        )
        _write_once_bytes(checksum_path, checksum_payload)
        _write_once_bytes(archive_path, archive_payload)
    return _monthly_record(
        symbol=symbol,
        month=month,
        archive_payload=archive_payload,
        checksum_payload=checksum_payload,
    )


def _write_once_json(path: Path, payload: dict[str, Any]) -> None:
    _write_once_bytes(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def main() -> None:
    preregistration = _load(PREREGISTRATION_PATH)
    activation_lock = _load(ACTIVATION_LOCK_PATH)
    development_bytes = DEVELOPMENT_REPORT_PATH.read_bytes()
    development = _load(DEVELOPMENT_REPORT_PATH)
    development_sha256 = sha256(development_bytes).hexdigest()
    unlock = _load(UNLOCK_PATH)
    if not validate_validation_unlock(
        unlock,
        report=development,
        preregistration=preregistration,
        activation_lock=activation_lock,
        report_sha256=development_sha256,
    ):
        raise Alpha015FundingReconciliationError("locked-validation unlock receipt is invalid")
    development_manifest = _load(DEVELOPMENT_ROOT / "manifest.json")
    validation_manifest = _load(VALIDATION_ROOT / "manifest.json")
    try:
        development_bundle = prepare_alpha015_phase_bundle(
            manifest=development_manifest,
            activation_lock=activation_lock,
            archive_root=DEVELOPMENT_ROOT,
            phase="development",
            expected_preregistration_sha256=str(preregistration["preregistration_sha256"]),
        )
        validation_bundle = prepare_alpha015_phase_bundle(
            manifest=validation_manifest,
            activation_lock=activation_lock,
            archive_root=VALIDATION_ROOT,
            phase="locked_validation",
            expected_preregistration_sha256=str(preregistration["preregistration_sha256"]),
            validation_unlock=unlock,
            expected_development_report_sha256=development_sha256,
        )
        development_authorized, development_events = _phase_funding_events(
            development_bundle, archive_root=DEVELOPMENT_ROOT
        )
        validation_authorized, validation_events = _phase_funding_events(
            validation_bundle, archive_root=VALIDATION_ROOT
        )
    except (Alpha015DataError, Alpha015ReplayError) as error:
        raise Alpha015FundingReconciliationError(f"daily funding source replay failed: {error}") from error
    authorized = development_authorized + validation_authorized
    requested_months = sorted({(symbol, _month_start(day)) for symbol, day in authorized})
    records: list[dict[str, Any]] = []
    monthly_events: list[dict[str, Any]] = []
    for symbol, month in requested_months:
        record, events = _download_or_reverify_monthly(symbol=symbol, month=month, fetch=_fetch)
        records.append(record)
        monthly_events.extend(events)
    receipt = build_reconciliation_receipt(
        candidate=str(preregistration["candidate"]),
        preregistration_sha256=str(preregistration["preregistration_sha256"]),
        development_report_sha256=development_sha256,
        activation_lock_sha256=str(activation_lock["lock_sha256"]),
        development_manifest_sha256=sha256((DEVELOPMENT_ROOT / "manifest.json").read_bytes()).hexdigest(),
        validation_manifest_sha256=sha256((VALIDATION_ROOT / "manifest.json").read_bytes()).hexdigest(),
        authorized_symbol_days=authorized,
        daily_events=development_events + validation_events,
        monthly_records=records,
        monthly_events=monthly_events,
    )
    _write_once_json(REPORT_PATH, receipt)
    print(json.dumps({"status": receipt["status"], "report": str(REPORT_PATH)}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (Alpha015FundingReconciliationError, Alpha015DataError, Alpha015ReplayError, OSError) as error:
        raise SystemExit(f"Alpha 015 funding reconciliation blocked: {error}") from error
