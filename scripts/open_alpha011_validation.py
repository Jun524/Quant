#!/usr/bin/env python3
"""Open Alpha #11 locked validation only after a source-replayed development PASS."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.analyze_alpha011_validation import (
    validate_lock_chain,
    validation_protocol_digests,
)
from scripts.download_binance_aggtrades import protocol_digests as acquisition_protocol_digests
from scripts.sync_alpha011_registry import (
    RECEIPT_PATH,
    REPORT_PATH,
    receipt_health,
)


LOCK_PATH = PROJECT_ROOT / "research" / "alpha011" / "validation-lock.json"


def build_validation_lock(
    report: dict,
    receipt_audit: dict,
    *,
    report_sha256: str,
    development_receipt_sha256: str,
    development_protocol: dict[str, str],
    validation_protocol: dict[str, str],
    now: datetime,
) -> dict:
    checks = (report.get("development_gate") or {}).get("checks") or []
    if (
        receipt_audit.get("valid") is not True
        or report.get("status") != "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED"
        or report.get("validation_opened") is not False
        or report.get("protocol_sha256") != development_protocol
        or (report.get("development_gate") or {}).get("status") != "PASS"
        or not checks
        or not all(item.get("passed") is True for item in checks)
    ):
        raise ValueError("Alpha #11 development evidence is not eligible for validation")
    now = now.astimezone(timezone.utc)
    start = datetime.combine(now.date() + timedelta(days=1), time.min, timezone.utc)
    return {
        "schema_version": 1,
        "status": "OPEN",
        "candidate": "ALPHA_011_QUARTER_HOUR_FLOW_CONTINUATION",
        "opened_at_utc": now.isoformat(),
        "validation_start_utc": start.isoformat(),
        "required_complete_utc_days": 60,
        "required_archives_for_final_exit": 61,
        "development_report": str(REPORT_PATH.relative_to(PROJECT_ROOT)),
        "development_report_sha256": report_sha256,
        "development_receipt": str(RECEIPT_PATH.relative_to(PROJECT_ROOT)),
        "development_receipt_sha256": development_receipt_sha256,
        "development_registry_event_id": receipt_audit["registry_event_id"],
        "development_protocol_sha256": development_protocol,
        "validation_protocol_sha256": validation_protocol,
        "immutable": True,
        "promotion_eligible": False,
    }


def main() -> None:
    if not REPORT_PATH.is_file() or not RECEIPT_PATH.is_file():
        raise SystemExit(
            "FAIL: DEVELOPMENT_NOT_READY - source-replayed Alpha #11 development "
            "report and receipt are required"
        )
    report_bytes = REPORT_PATH.read_bytes()
    report = json.loads(report_bytes)
    audit = receipt_health()
    development_protocol = acquisition_protocol_digests()
    validation_protocol = validation_protocol_digests()
    if LOCK_PATH.exists():
        lock_bytes = LOCK_PATH.read_bytes()
        existing = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        try:
            validate_lock_chain(existing, lock_bytes=lock_bytes)
        except ValueError as error:
            raise SystemExit(f"FAIL: Alpha #11 validation lock exists but is stale: {error}") from error
        print(json.dumps({"status": "ALREADY_OPEN", "receipt": existing}, indent=2))
        return
    try:
        lock = build_validation_lock(
            report,
            audit,
            report_sha256=sha256(report_bytes).hexdigest(),
            development_receipt_sha256=sha256(RECEIPT_PATH.read_bytes()).hexdigest(),
            development_protocol=development_protocol,
            validation_protocol=validation_protocol,
            now=datetime.now(timezone.utc),
        )
    except ValueError as error:
        raise SystemExit(f"FAIL: {error}") from error
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(LOCK_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit("Alpha #11 validation lock was created concurrently")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(lock, handle, indent=2)
        handle.write("\n")
    print(json.dumps(lock, indent=2))


if __name__ == "__main__":
    main()
