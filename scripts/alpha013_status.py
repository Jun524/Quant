#!/usr/bin/env python3
"""Report forward-only Alpha #13 collection and scheduler health."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.download_alpha013_day import (
    Alpha013DownloadError,
    manifest_inventory_digest,
    protocol_digests,
    validate_existing_day,
)
from scripts.build_alpha013_signal_panel import PANEL_PROTOCOL_FILES
from quant.research.signal_diagnostics import SIGNAL_DIAGNOSTIC_HEADER
from quant.research.funding_decision_availability import (
    audit_decision_availability,
    preregistration_digest,
)


PREREGISTRATION = PROJECT_ROOT / "research" / "alpha013-preregistration.json"
REGISTRY = PROJECT_ROOT / "research" / "registry.json"
RAW_ROOT = PROJECT_ROOT / "research" / "alpha013" / "raw"
SCHEDULER_STATUS = PROJECT_ROOT / "research" / "alpha013" / "update-scheduler-status.json"
FORWARD_REPORT = PROJECT_ROOT / "reports" / "alpha013-forward.json"
ELIGIBLE_DATA_START = date(2026, 10, 10)
PUBLISHER_GRACE = timedelta(hours=24)
VALIDATION_LOCK = PROJECT_ROOT / "research" / "alpha013" / "validation-lock.json"
VALIDATION_REPORT = PROJECT_ROOT / "reports" / "alpha013-validation.json"
SIGNAL_PANEL = PROJECT_ROOT / "research" / "alpha013" / "signal-panel.csv"
SIGNAL_PANEL_RECEIPT = PROJECT_ROOT / "reports" / "alpha013-signal-panel.json"
DECISION_RECEIPTS = PROJECT_ROOT / "research" / "alpha013" / "decision-receipts"


def signal_panel_health(
    *,
    expected_source_inventory_sha256: str,
    panel_path: Path = SIGNAL_PANEL,
    receipt_path: Path = SIGNAL_PANEL_RECEIPT,
    panel_reference: str | None = None,
) -> dict[str, Any]:
    """Verify the non-promotional Alpha #13 signal-panel receipt end to end."""

    if not receipt_path.is_file() or not panel_path.is_file():
        return {"status": "ABSENT", "valid": False}
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        panel_bytes = panel_path.read_bytes()
        with panel_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, csv.Error) as error:
        return {"status": "INVALID", "valid": False, "error": str(error)}
    expected_protocol = {
        relative: sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest()
        for relative in PANEL_PROTOCOL_FILES
    }
    expected_panel_reference = panel_reference or str(panel_path.relative_to(PROJECT_ROOT))
    header = rows[0] if rows else None
    actual_rows = max(0, len(rows) - 1)
    coverage = receipt.get("coverage") or {}
    checks = {
        "kind": receipt.get("kind") == "ALPHA013_SOURCE_BOUND_SIGNAL_PANEL",
        "non_promotional": (
            receipt.get("promotion_eligible") is False
            and receipt.get("live_order_authorization") == "CLOSED"
        ),
        "status": receipt.get("status")
        in {"ACCUMULATING", "READY_FOR_SIGNAL_DIAGNOSTIC"},
        "panel_path": receipt.get("panel", {}).get("path") == expected_panel_reference,
        "panel_sha256": receipt.get("panel", {}).get("sha256") == sha256(panel_bytes).hexdigest(),
        "header": header == SIGNAL_DIAGNOSTIC_HEADER
        and receipt.get("panel", {}).get("header") == SIGNAL_DIAGNOSTIC_HEADER,
        "row_count": coverage.get("rows") == actual_rows,
        "source_inventory": (
            (receipt.get("source_integrity") or {}).get("source_inventory_sha256")
            == expected_source_inventory_sha256
        ),
        "protocol": receipt.get("protocol_sha256") == expected_protocol,
    }
    valid = all(checks.values())
    return {
        "status": receipt.get("status") if valid else "INVALID",
        "valid": valid,
        "rows": actual_rows,
        "source_inventory_sha256": (receipt.get("source_integrity") or {}).get("source_inventory_sha256"),
        "checks": checks,
    }


def _process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    completed = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0 and "run_alpha013_scheduler.py run" in completed.stdout


def scheduler_health(
    *,
    now: datetime | None = None,
    path: Path = SCHEDULER_STATUS,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"health": "NOT_STARTED", "running": False}
    pid = int(payload.get("pid") or -1)
    running = _process_running(pid)
    heartbeat_raw = payload.get("heartbeat_at")
    try:
        heartbeat = datetime.fromisoformat(str(heartbeat_raw).replace("Z", "+00:00"))
        age = (now - heartbeat.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        age = None
    interval = int(payload.get("interval_seconds") or 21_600)
    fresh = age is not None and -60 <= age <= max(interval + 900, 22_500)
    state = str(payload.get("state") or "UNKNOWN")
    if running and fresh and state in {"SLEEPING", "UPDATING"}:
        health = "RUNNING_HEALTHY"
    elif running and fresh and state == "RETRY_PENDING":
        health = "RUNNING_RETRY_PENDING"
    elif running:
        health = "RUNNING_STALE"
    else:
        health = "STOPPED"
    return {
        "health": health,
        "running": running,
        "pid": pid,
        "state": state,
        "heartbeat_at": heartbeat_raw,
        "heartbeat_age_seconds": age,
        "last_exit_code": payload.get("last_exit_code"),
        "next_run_at": payload.get("next_run_at"),
        "interval_seconds": interval,
    }


def _dates(start: date, end_exclusive: date) -> list[date]:
    if end_exclusive <= start:
        return []
    return [start + timedelta(days=offset) for offset in range((end_exclusive - start).days)]


def validation_phase_health(
    *,
    lock_path: Path = VALIDATION_LOCK,
    report_path: Path = VALIDATION_REPORT,
) -> dict[str, Any]:
    if not lock_path.is_file():
        return {"opened": False, "valid": None, "report_status": None}
    try:
        from scripts.analyze_alpha013_validation import (
            validate_lock_chain,
            validation_protocol_digests,
        )

        lock_bytes = lock_path.read_bytes()
        lock = json.loads(lock_bytes)
        audit = validate_lock_chain(lock, lock_bytes=lock_bytes)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        return {"opened": True, "valid": False, "report_status": None, "error": str(error)}
    result: dict[str, Any] = {
        "opened": True,
        "valid": True,
        "first_validation_decision_utc": audit["first_validation_decision"].isoformat(),
        "lock_sha256": audit["lock_sha256"],
        "report_status": None,
        "eligible_weeks_closed": 0,
    }
    if not report_path.is_file():
        return result
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {**result, "valid": False, "error": "validation report is unreadable"}
    report_status = report.get("status")
    if (
        report_status not in {
            "ACCUMULATING_LOCKED_VALIDATION",
            "WAITING_LOCKED_VALIDATION_FUNDING_RECONCILIATION",
            "PASS",
            "FAIL",
        }
        or report.get("lock_sha256") != audit["lock_sha256"]
        or report.get("validation_protocol_sha256") != validation_protocol_digests()
        or report.get("promotion_eligible") is not False
    ):
        return {**result, "valid": False, "error": "validation report binding is invalid"}
    if report_status in {"PASS", "FAIL"}:
        from scripts.sync_alpha013_validation import receipt_health

        receipt = receipt_health(report_path=report_path, lock_path=lock_path)
        if receipt.get("valid") is not True:
            return {
                **result,
                "valid": False,
                "report_status": report_status,
                "error": "terminal validation receipt is absent or invalid",
                "receipt": receipt,
            }
        result["receipt"] = receipt
    return {
        **result,
        "report_status": report_status,
        "eligible_weeks_closed": int(report.get("eligible_weeks_closed") or 0)
        if report_status in {
            "ACCUMULATING_LOCKED_VALIDATION",
            "WAITING_LOCKED_VALIDATION_FUNDING_RECONCILIATION",
        }
        else int((((report.get("validation") or {}).get("base") or {}).get("eligible_weeks") or 0)),
        "source_inventory_sha256": report.get("source_inventory_sha256")
        or (report.get("data_integrity") or {}).get("source_inventory_sha256"),
    }


def status_payload(
    *,
    now: datetime | None = None,
    raw_root: Path = RAW_ROOT,
    scheduler_path: Path = SCHEDULER_STATUS,
    forward_report_path: Path = FORWARD_REPORT,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    prereg = json.loads(PREREGISTRATION.read_text(encoding="utf-8"))
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    from scripts.sync_alpha013_registry import receipt_health as development_receipt_health

    development_receipt = development_receipt_health()
    development_terminal = development_receipt.get("report_status")
    validation_phase = validation_phase_health()
    try:
        forward_report = json.loads(forward_report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        forward_report = {}
    early_gate = forward_report.get("gate") or {}
    early_funding_only_pending = bool(
        forward_report.get("status") == "FAIL_DEVELOPMENT"
        and early_gate.get("all_funding_rows_reconciled") is False
        and all(
            value is True
            for name, value in early_gate.items()
            if name != "all_funding_rows_reconciled"
        )
    )
    matching = [
        item for item in registry.get("experiments", [])
        if item.get("strategy") == prereg["candidate"]
    ]
    acquisition_end_exclusive = now.date()
    if (
        (development_receipt.get("valid") is True or early_funding_only_pending)
        and validation_phase.get("opened") is not True
    ):
        try:
            last_day = date.fromisoformat(str((forward_report.get("data_integrity") or {})["last_day"]))
            acquisition_end_exclusive = last_day + timedelta(days=1)
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            pass
    if validation_phase.get("report_status") in {
        "PASS",
        "FAIL",
        "WAITING_LOCKED_VALIDATION_FUNDING_RECONCILIATION",
    }:
        try:
            last_day = max(
                date.fromisoformat(path.name)
                for path in raw_root.glob("????-??-??")
                if path.is_dir()
            )
            acquisition_end_exclusive = last_day + timedelta(days=1)
        except ValueError:
            pass
    expected_days = _dates(ELIGIBLE_DATA_START, acquisition_end_exclusive)
    valid_days: list[str] = []
    valid_day_manifests: list[dict[str, str]] = []
    invalid_days: list[dict[str, str]] = []
    for path in sorted(raw_root.glob("????-??-??")) if raw_root.exists() else []:
        if not path.is_dir():
            continue
        try:
            parsed = date.fromisoformat(path.name)
        except ValueError:
            invalid_days.append({"day": path.name, "error": "invalid directory date"})
            continue
        if parsed < ELIGIBLE_DATA_START:
            invalid_days.append({"day": path.name, "error": "predates fresh sample"})
            continue
        try:
            validate_existing_day(path)
            valid_days.append(path.name)
            valid_day_manifests.append(
                {
                    "day": path.name,
                    "manifest_sha256": sha256(
                        (path / "manifest.json").read_bytes()
                    ).hexdigest(),
                }
            )
        except Alpha013DownloadError as error:
            invalid_days.append({"day": path.name, "error": str(error)})
    expected_names = [item.isoformat() for item in expected_days]
    missing_days = sorted(set(expected_names) - set(valid_days))
    overdue_missing_days = [
        item
        for item in missing_days
        if now
        >= datetime.combine(
            date.fromisoformat(item) + timedelta(days=1),
            datetime.min.time(),
            timezone.utc,
        )
        + PUBLISHER_GRACE
    ]
    pending_publication_days = [
        item for item in missing_days if item not in overdue_missing_days
    ]
    extra_days = sorted(set(valid_days) - set(expected_names))
    scheduler = scheduler_health(now=now, path=scheduler_path)
    source_inventory_sha256 = manifest_inventory_digest(valid_day_manifests)
    decision_availability = audit_decision_availability(
        prereg,
        preregistration_sha256=preregistration_digest(PREREGISTRATION),
        receipt_directory=DECISION_RECEIPTS,
        now=now,
    )
    panel_health = signal_panel_health(
        expected_source_inventory_sha256=source_inventory_sha256
    )
    report_integrity = forward_report.get("data_integrity") or {}
    report_current = bool(
        forward_report.get("candidate") == prereg["candidate"]
        and forward_report.get("preregistration_sha256")
        == sha256(PREREGISTRATION.read_bytes()).hexdigest()
        and report_integrity.get("sealed_days") == len(valid_days)
        and report_integrity.get("day_manifests") == valid_day_manifests
        and report_integrity.get("source_inventory_sha256")
        == source_inventory_sha256
        and report_integrity.get("protocol_sha256") == protocol_digests()
    )
    development = (forward_report.get("development") or {}).get("base") or {}
    development_gate = forward_report.get("gate") or {}
    development_funding_only_pending = bool(
        report_current
        and forward_report.get("status") == "FAIL_DEVELOPMENT"
        and development_gate.get("all_funding_rows_reconciled") is False
        and all(
            value is True
            for name, value in development_gate.items()
            if name != "all_funding_rows_reconciled"
        )
    )

    first_decision = prereg["fresh_sample"]["first_eligible_decision"]
    first_decision_at = datetime.fromisoformat(first_decision.replace("Z", "+00:00"))
    validation_status = validation_phase.get("report_status")
    if len(matching) != 1:
        status = "REGISTRY_MISMATCH"
    elif invalid_days or extra_days:
        status = "DATA_INTEGRITY_FAIL"
    elif validation_phase.get("opened") and validation_phase.get("valid") is False:
        status = "VALIDATION_LOCK_OR_REPORT_INVALID"
    elif decision_availability["status"] == "CAUSAL_AVAILABILITY_VETO":
        status = "CAUSAL_AVAILABILITY_VETO"
    elif development_receipt.get("valid") is True and development_terminal == "FAIL_DEVELOPMENT":
        status = "DEVELOPMENT_FAIL"
    elif validation_status == "PASS":
        status = "VALIDATION_PASS_REVIEW"
    elif validation_status == "FAIL":
        status = "VALIDATION_FAIL"
    elif (
        development_receipt.get("valid") is True
        and development_terminal == "PASS_DEVELOPMENT"
        and validation_phase.get("opened") is not True
    ):
        status = "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED"
    elif development_funding_only_pending:
        status = "WAITING_FUNDING_RECONCILIATION"
    elif now.date() <= ELIGIBLE_DATA_START:
        status = "WAITING_FIRST_COMPLETE_UTC_DAY"
    elif overdue_missing_days:
        status = "DOWNLOAD_REQUIRED"
    elif pending_publication_days:
        status = "WAITING_PUBLISHER_ARCHIVE"
    elif now < first_decision_at:
        status = "ACCUMULATING_SIGNAL_LOOKBACK"
    elif validation_phase.get("valid") is True:
        status = "ACCUMULATING_LOCKED_VALIDATION"
    else:
        status = "ACCUMULATING_FORWARD_WEEKS"
    return {
        "schema_version": 1,
        "observed_at": now.isoformat(),
        "candidate": prereg["candidate"],
        "status": status,
        "registered_at_utc": prereg["registered_at_utc"],
        "first_eligible_decision": first_decision,
        "eligible_data_start": ELIGIBLE_DATA_START.isoformat(),
        "experiment_id": matching[0]["experiment_id"] if len(matching) == 1 else None,
        "post_hoc_hypothesis": prereg["provenance"]["post_hoc_hypothesis"],
        "historical_confirmation_allowed": False,
        "valid_complete_days": len(valid_days),
        "valid_days": valid_days,
        "valid_day_manifests": valid_day_manifests,
        "source_inventory_sha256": source_inventory_sha256,
        "decision_availability": decision_availability,
        "signal_panel": panel_health,
        "missing_complete_days": missing_days,
        "publisher_grace_hours": PUBLISHER_GRACE.total_seconds() / 3600,
        "pending_publication_days": pending_publication_days,
        "overdue_missing_days": overdue_missing_days,
        "invalid_days": invalid_days,
        "extra_days": extra_days,
        "forward_report_current": report_current,
        "forward_report_status": forward_report.get("status") if report_current else "STALE_OR_ABSENT",
        "development_weeks_closed": int(development.get("eligible_weeks") or 0) if report_current else 0,
        "closed_legs": int(development.get("closed_legs") or 0) if report_current else 0,
        "net_return": development.get("net_return") if report_current else None,
        "development_weeks_required": prereg["fresh_sample"]["development_weeks"],
        "development_funding_only_pending": development_funding_only_pending,
        "development_receipt": development_receipt,
        "locked_validation_opened": validation_phase.get("opened") is True,
        "validation_lock_valid": validation_phase.get("valid"),
        "validation_first_decision": validation_phase.get("first_validation_decision_utc"),
        "validation_weeks_closed": validation_phase.get("eligible_weeks_closed", 0),
        "validation_weeks_required": prereg["fresh_sample"]["locked_validation_weeks"],
        "validation_report_status": validation_status,
        "validation_phase": validation_phase,
        "scheduler": scheduler,
    }


def main() -> None:
    payload = status_payload()
    print(json.dumps(payload, indent=2))
    healthy = {
        "WAITING_FIRST_COMPLETE_UTC_DAY",
        "WAITING_PUBLISHER_ARCHIVE",
        "ACCUMULATING_SIGNAL_LOOKBACK",
        "ACCUMULATING_FORWARD_WEEKS",
        "WAITING_FUNDING_RECONCILIATION",
        "DEVELOPMENT_FAIL",
        "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED",
        "ACCUMULATING_LOCKED_VALIDATION",
        "VALIDATION_PASS_REVIEW",
        "VALIDATION_FAIL",
    }
    if payload["status"] not in healthy:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
