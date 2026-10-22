#!/usr/bin/env python3
"""Authorize Alpha 015's sealed validation data only after a full dev PASS.

This is deliberately a one-way, write-once receipt.  It does not acquire a
single validation byte and it never grants live trading authority.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.alpha015_preregistration import canonical_sha256


PREREGISTRATION_PATH = PROJECT_ROOT / "research" / "alpha015-preregistration.json"
ACTIVATION_LOCK_PATH = PROJECT_ROOT / "research" / "alpha015_activation" / "activation-lock.json"
REPORT_PATH = PROJECT_ROOT / "research" / "alpha015_development" / "report.json"
UNLOCK_PATH = PROJECT_ROOT / "research" / "alpha015_development" / "validation-unlock.json"


class Alpha015ValidationUnlockError(RuntimeError):
    pass


def _self_digest(payload: dict[str, Any], field: str) -> str:
    return canonical_sha256({key: value for key, value in payload.items() if key != field})


def _all_passed_checks(report: dict[str, Any]) -> bool:
    checks = report.get("checks")
    return isinstance(checks, list) and bool(checks) and all(
        isinstance(item, dict) and item.get("passed") is True for item in checks
    )


def build_validation_unlock(
    report: dict[str, Any],
    preregistration: dict[str, Any],
    activation_lock: dict[str, Any],
    *,
    report_sha256: str,
    now: datetime,
) -> dict[str, Any]:
    """Create the exact receipt accepted by the locked validation loader."""
    source = report.get("source") or {}
    preregistration_sha256 = str(preregistration.get("preregistration_sha256") or "")
    lock_sha256 = str(activation_lock.get("lock_sha256") or "")
    if not (
        report.get("candidate") == preregistration.get("candidate")
        and report.get("stage") == "development"
        and report.get("status") == "PASS"
        and report.get("days") == 120
        and report.get("locked_validation_open") is False
        and report.get("live_order_authorization") == "CLOSED"
        and _all_passed_checks(report)
        and preregistration.get("status") == "PREREGISTERED_SOURCE_ACQUISITION_LOCKED"
        and preregistration.get("return_data_acquired") is False
        and preregistration.get("locked_validation_open") is False
        and preregistration.get("live_order_authorization") == "CLOSED"
        and preregistration_sha256 == _self_digest(preregistration, "preregistration_sha256")
        and activation_lock.get("status") == "LOCKED_FOR_PREREGISTRATION_ONLY"
        and activation_lock.get("validation_open") is False
        and lock_sha256 == _self_digest(activation_lock, "lock_sha256")
        and lock_sha256 == (preregistration.get("activation_binding") or {}).get("lock_sha256")
        and activation_lock.get("split_sha256")
        == (preregistration.get("activation_binding") or {}).get("split_sha256")
        and source.get("preregistration_sha256") == preregistration_sha256
        and source.get("activation_lock_sha256") == lock_sha256
        and isinstance(source.get("development_manifest_sha256"), str)
        and len(source["development_manifest_sha256"]) == 64
        and isinstance(source.get("replay_engine_sha256"), str)
        and len(source["replay_engine_sha256"]) == 64
        and len(report_sha256) == 64
    ):
        raise Alpha015ValidationUnlockError(
            "development report is not an eligible source-bound Alpha 015 PASS"
        )
    now = now.astimezone(timezone.utc)
    return {
        "schema_version": 1,
        "candidate": preregistration["candidate"],
        "status": "AUTHORIZED",
        "authorized_phase": "locked_validation",
        "opened_at_utc": now.isoformat(),
        "development_gates_pass": True,
        "development_report": str(REPORT_PATH.relative_to(PROJECT_ROOT)),
        "development_report_sha256": report_sha256,
        "preregistration_path": str(PREREGISTRATION_PATH.relative_to(PROJECT_ROOT)),
        "preregistration_sha256": preregistration_sha256,
        "activation_lock_path": str(ACTIVATION_LOCK_PATH.relative_to(PROJECT_ROOT)),
        "activation_lock_sha256": lock_sha256,
        "calendar_sha256": activation_lock["split_sha256"],
        "development_manifest_sha256": source["development_manifest_sha256"],
        "replay_engine_sha256": source["replay_engine_sha256"],
        "immutable": True,
        "live_order_authorization": "CLOSED",
    }


def validate_validation_unlock(
    receipt: dict[str, Any],
    *,
    report: dict[str, Any],
    preregistration: dict[str, Any],
    activation_lock: dict[str, Any],
    report_sha256: str,
) -> bool:
    """Validate an existing receipt without rebuilding its historical timestamp."""
    try:
        expected = build_validation_unlock(
            report,
            preregistration,
            activation_lock,
            report_sha256=report_sha256,
            now=datetime.fromisoformat(str(receipt["opened_at_utc"])),
        )
    except (Alpha015ValidationUnlockError, KeyError, TypeError, ValueError):
        return False
    return receipt == expected


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Alpha015ValidationUnlockError(f"invalid Alpha 015 artifact: {path.name}") from error
    if not isinstance(payload, dict):
        raise Alpha015ValidationUnlockError(f"Alpha 015 artifact is not an object: {path.name}")
    return payload


def _write_once(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != encoded:
            raise Alpha015ValidationUnlockError("existing validation unlock differs")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def main() -> None:
    report_bytes = REPORT_PATH.read_bytes()
    report = _load(REPORT_PATH)
    preregistration = _load(PREREGISTRATION_PATH)
    activation_lock = _load(ACTIVATION_LOCK_PATH)
    report_sha256 = sha256(report_bytes).hexdigest()
    if UNLOCK_PATH.exists():
        existing = _load(UNLOCK_PATH)
        if not validate_validation_unlock(
            existing,
            report=report,
            preregistration=preregistration,
            activation_lock=activation_lock,
            report_sha256=report_sha256,
        ):
            raise Alpha015ValidationUnlockError("existing validation unlock is stale or invalid")
        print(json.dumps({"status": "ALREADY_AUTHORIZED", "receipt": existing}, indent=2))
        return
    receipt = build_validation_unlock(
        report,
        preregistration,
        activation_lock,
        report_sha256=report_sha256,
        now=datetime.now(timezone.utc),
    )
    _write_once(UNLOCK_PATH, receipt)
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (Alpha015ValidationUnlockError, OSError) as error:
        raise SystemExit(f"Alpha 015 locked validation remains closed: {error}") from error
