#!/usr/bin/env python3
"""Open Alpha #13 validation only from a replayed terminal development PASS."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.analyze_alpha013_validation import (
    LOCK_PATH,
    validate_lock_chain,
    validation_protocol_digests,
)
from scripts.download_alpha013_day import protocol_digests as acquisition_protocol_digests
from scripts.sync_alpha013_registry import RECEIPT_PATH, REPORT_PATH, receipt_health


def next_monday_0400(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    candidate = now.replace(hour=4, minute=0, second=0, microsecond=0)
    candidate += timedelta(days=(-candidate.weekday()) % 7)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def build_validation_lock(
    report: dict,
    receipt_audit: dict,
    *,
    report_sha256: str,
    receipt_sha256: str,
    acquisition_protocol: dict[str, str],
    validation_protocol: dict[str, str],
    now: datetime,
) -> dict:
    gate = report.get("gate") or {}
    if (
        receipt_audit.get("valid") is not True
        or report.get("status") != "PASS_DEVELOPMENT"
        or report.get("locked_validation_opened") is not False
        or (report.get("data_integrity") or {}).get("protocol_sha256")
        != acquisition_protocol
        or not gate
        or not all(value is True for value in gate.values())
    ):
        raise ValueError("Alpha #13 development evidence is not eligible for validation")
    decision = next_monday_0400(now)
    return {
        "schema_version": 1,
        "status": "OPEN",
        "candidate": "ALPHA_013_FUNDING_DISPERSION_REVERSAL",
        "opened_at_utc": now.astimezone(timezone.utc).isoformat(),
        "first_validation_decision_utc": decision.isoformat(),
        "required_eligible_weeks": 26,
        "development_report": str(REPORT_PATH.relative_to(PROJECT_ROOT)),
        "development_report_sha256": report_sha256,
        "development_receipt": str(RECEIPT_PATH.relative_to(PROJECT_ROOT)),
        "development_receipt_sha256": receipt_sha256,
        "development_registry_event_id": receipt_audit["registry_event_id"],
        "development_source_inventory_sha256": receipt_audit["source_inventory_sha256"],
        "acquisition_protocol_sha256": acquisition_protocol,
        "validation_protocol_sha256": validation_protocol,
        "immutable": True,
        "promotion_eligible": False,
    }


def main() -> None:
    if not REPORT_PATH.is_file() or not RECEIPT_PATH.is_file():
        raise SystemExit("FAIL: DEVELOPMENT_NOT_READY - Alpha #13 report and replay receipt are required")
    report_bytes = REPORT_PATH.read_bytes()
    receipt_bytes = RECEIPT_PATH.read_bytes()
    report = json.loads(report_bytes)
    receipt_audit = receipt_health()
    if LOCK_PATH.exists():
        lock_bytes = LOCK_PATH.read_bytes()
        existing = json.loads(lock_bytes)
        try:
            validate_lock_chain(existing, lock_bytes=lock_bytes)
        except ValueError as error:
            raise SystemExit(f"FAIL: existing Alpha #13 validation lock is invalid: {error}") from error
        print(json.dumps({"status": "ALREADY_OPEN", "lock": existing}, indent=2))
        return
    try:
        lock = build_validation_lock(
            report,
            receipt_audit,
            report_sha256=sha256(report_bytes).hexdigest(),
            receipt_sha256=sha256(receipt_bytes).hexdigest(),
            acquisition_protocol=acquisition_protocol_digests(),
            validation_protocol=validation_protocol_digests(),
            now=datetime.now(timezone.utc),
        )
    except ValueError as error:
        raise SystemExit(f"FAIL: {error}") from error
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(LOCK_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit("Alpha #13 validation lock was created concurrently")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(lock, handle, indent=2)
        handle.write("\n")
    print(json.dumps(lock, indent=2))


if __name__ == "__main__":
    main()
