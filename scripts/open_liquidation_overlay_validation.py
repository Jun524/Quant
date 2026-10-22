#!/usr/bin/env python3
"""Open liquidation-overlay validation only after a source-bound development PASS."""

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

from quant.research.liquidation_telemetry import protocol_digests


DEVELOPMENT_REPORT = PROJECT_ROOT / "reports" / "liquidation-overlay-development.json"
LOCK_PATH = PROJECT_ROOT / "research" / "liquidation_telemetry" / "validation-lock.json"


def build_validation_lock(
    development: dict,
    *,
    development_sha256: str,
    current_protocol: dict[str, str],
    now: datetime,
) -> dict:
    now = now.astimezone(timezone.utc)
    selected = development.get("selected_development_days") or []
    if (
        development.get("status") != "DEVELOPMENT_PASS_REQUIRES_EXPLICIT_LOCK_OPEN"
        or development.get("performance_calculated") is not True
        or development.get("locked_validation_opened") is not False
        or development.get("protocol_sha256") != current_protocol
        or len(selected) != int(development.get("required_complete_utc_days") or -1)
        or len(selected) != 60
        or selected != sorted(set(selected))
        or development.get("latest_opened_utc_day") != selected[-1]
    ):
        raise ValueError("development report is not eligible to open locked validation")
    validation_start = datetime.combine(now.date() + timedelta(days=1), time.min, timezone.utc)
    return {
        "schema_version": 1,
        "status": "OPEN",
        "candidate": development["candidate"],
        "opened_at_utc": now.isoformat(),
        "validation_start_utc": validation_start.isoformat(),
        "required_complete_utc_days": 30,
        "development_report": str(DEVELOPMENT_REPORT.relative_to(PROJECT_ROOT)),
        "development_report_sha256": development_sha256,
        "development_days": selected,
        "development_latest_opened_utc_day": selected[-1],
        "protocol_sha256": current_protocol,
        "immutable": True,
        "promotion_eligible": False,
    }


def main() -> None:
    current_protocol = protocol_digests(PROJECT_ROOT)
    development_bytes = DEVELOPMENT_REPORT.read_bytes()
    development = json.loads(development_bytes)
    development_digest = sha256(development_bytes).hexdigest()
    if LOCK_PATH.is_file():
        existing = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        if (
            existing.get("status") != "OPEN"
            or existing.get("immutable") is not True
            or existing.get("protocol_sha256") != current_protocol
            or existing.get("development_report_sha256") != development_digest
        ):
            raise SystemExit("validation lock already exists but is stale or invalid")
        print(json.dumps({"status": "ALREADY_OPEN", "receipt": existing}, indent=2))
        return
    try:
        receipt = build_validation_lock(
            development,
            development_sha256=development_digest,
            current_protocol=current_protocol,
            now=datetime.now(timezone.utc),
        )
    except ValueError as exc:
        raise SystemExit(f"FAIL: {exc}") from exc
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(LOCK_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit("validation lock was created concurrently; rerun to verify it")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(receipt, handle, indent=2)
        handle.write("\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
