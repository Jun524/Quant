#!/usr/bin/env python3
"""Report Alpha #11 forward archive coverage without calculating performance."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from hashlib import sha256
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.download_binance_aggtrades import (
    funding_query_url,
    inspect_funding_file,
    parse_checksum,
    protocol_digests,
)
from scripts.sync_alpha011_registry import receipt_health as development_receipt_health
from scripts.analyze_alpha011_validation import (
    LOCK_PATH as VALIDATION_LOCK_PATH,
    OUTPUT as VALIDATION_REPORT_PATH,
    validate_lock_chain,
)
from scripts.sync_alpha011_validation import receipt_health as validation_receipt_health


ELIGIBLE_START = date(2026, 10, 9)
DEVELOPMENT_DAYS = 60
VALIDATION_DAYS = 60
PUBLISHER_GRACE = timedelta(hours=24)
SCHEDULER_STATUS_PATH = PROJECT_ROOT / "research" / "alpha011" / "update-scheduler-status.json"
PREREGISTRATION_PATH = PROJECT_ROOT / "research" / "alpha011-preregistration.json"
RISK_POLICY_PATH = PROJECT_ROOT / "research" / "risk-policy.json"


def policy_scope(
    *,
    preregistration_path: Path = PREREGISTRATION_PATH,
    risk_policy_path: Path = RISK_POLICY_PATH,
) -> dict:
    """Describe why Alpha #11 remains research-only under the current policy.

    Alpha #11's forward sample began before the capital-preservation policy.
    Its frozen 20% drawdown gate cannot be silently tightened or retroactively
    treated as a new-policy 10% gate, even if the final realized drawdown is
    small. Preserve the experiment for its original question, but make its
    ineligibility as promotion evidence explicit.
    """
    try:
        preregistration = json.loads(preregistration_path.read_text(encoding="utf-8"))
        risk_policy = json.loads(risk_policy_path.read_text(encoding="utf-8"))
        frozen_drawdown = float(
            (preregistration.get("development_gate") or {}).get(
                "maximum_drawdown_maximum"
            )
        )
        current_drawdown = float(
            (risk_policy.get("non_negotiables") or {}).get(
                "maximum_cost_after_mtm_drawdown"
            )
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "cohort": "POLICY_SCOPE_UNVERIFIABLE",
            "current_capital_preservation_policy_eligibility": False,
            "reason": "Alpha #11 preregistration or active Risk Policy is unreadable.",
        }
    return {
        "cohort": "FROZEN_PRE_POLICY_FORWARD_COHORT",
        "current_capital_preservation_policy_eligibility": False,
        "reason": (
            "Alpha #11 began before the active capital-preservation policy and its "
            "sealed development gate permits a larger drawdown than the current policy. "
            "It remains research-only and cannot supply current-policy promotion evidence."
        ),
        "preregistration_registered_at_utc": preregistration.get("registered_at_utc"),
        "current_policy_id": risk_policy.get("policy_id"),
        "current_policy_effective_at_utc": risk_policy.get("effective_at_utc"),
        "frozen_maximum_drawdown": frozen_drawdown,
        "current_maximum_drawdown": current_drawdown,
    }


def _scheduler_process_running(pid: int) -> bool:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return completed.returncode == 0 and "run_alpha011_scheduler.py run" in completed.stdout


def scheduler_health(*, now: datetime, path: Path = SCHEDULER_STATUS_PATH) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        heartbeat = datetime.fromisoformat(str(payload["heartbeat_at"]))
        interval = int(payload["interval_seconds"])
        state = str(payload["state"])
        last_exit_code = payload.get("last_exit_code")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return {"health": "MISSING_OR_INVALID", "running": False}
    if heartbeat.tzinfo is None:
        return {"health": "INVALID_HEARTBEAT", "running": False, "pid": pid}
    running = _scheduler_process_running(pid)
    age_seconds = (now - heartbeat.astimezone(timezone.utc)).total_seconds()
    fresh = -60 <= age_seconds <= interval + 1_800
    allowed_state = state in {"UPDATING", "SLEEPING", "RETRY_PENDING"}
    if running and fresh and allowed_state:
        health = "RUNNING_RETRY_PENDING" if state == "RETRY_PENDING" else "RUNNING_HEALTHY"
    elif not running:
        health = "PROCESS_DOWN"
    elif not fresh:
        health = "HEARTBEAT_STALE"
    else:
        health = "STATE_INVALID"
    return {
        "health": health,
        "running": running,
        "pid": pid,
        "state": state,
        "heartbeat_at": heartbeat.isoformat(),
        "heartbeat_age_seconds": age_seconds,
        "last_exit_code": last_exit_code,
        "next_run_at": payload.get("next_run_at"),
        "interval_seconds": interval,
    }


def _resolve_manifest_file(value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else PROJECT_ROOT / path


def validation_phase_health(
    *,
    lock_path: Path = VALIDATION_LOCK_PATH,
    report_path: Path = VALIDATION_REPORT_PATH,
) -> dict:
    if not lock_path.is_file():
        return {"opened": False, "valid": None, "report_status": None}
    try:
        lock_bytes = lock_path.read_bytes()
        lock = json.loads(lock_bytes)
        audit = validate_lock_chain(lock, lock_bytes=lock_bytes)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        return {
            "opened": True,
            "valid": False,
            "report_status": None,
            "error": str(error),
        }
    start = audit["validation_start"]
    result = {
        "opened": True,
        "valid": True,
        "validation_start_utc": start.isoformat(),
        "required_last_archive_day": (start.date() + timedelta(days=VALIDATION_DAYS)).isoformat(),
        "lock_sha256": audit["lock_sha256"],
        "report_status": None,
    }
    if not report_path.is_file():
        return result
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {**result, "valid": False, "error": "validation report is unreadable"}
    report_status = report.get("status")
    if (
        report_status not in {"ACCUMULATING_LOCKED_VALIDATION", "PASS", "FAIL"}
        or report.get("lock_sha256") != audit["lock_sha256"]
        or report.get("validation_protocol_sha256")
        != audit["validation_protocol_sha256"]
        or report.get("promotion_eligible") is not False
        or (report_status in {"PASS", "FAIL"} and report.get("performance_calculated") is not True)
    ):
        return {**result, "valid": False, "error": "validation report binding is invalid"}
    receipt = validation_receipt_health(report_path=report_path, lock_path=lock_path)
    if report_status in {"PASS", "FAIL"} and receipt.get("valid") is not True:
        return {
            **result,
            "valid": False,
            "report_status": report_status,
            "error": "terminal validation receipt is absent or invalid",
            "receipt": receipt,
        }
    return {
        **result,
        "report_status": report_status,
        "performance_calculated": report.get("performance_calculated") is True,
        "report_sha256": sha256(report_path.read_bytes()).hexdigest(),
        "receipt": receipt,
    }


def _inspect_manifest(payload: dict, *, root: Path, current_protocol: dict[str, str]) -> tuple[set[date], list[str]]:
    """Perform a cheap fail-closed audit; full ZIP hashing remains analysis-time work."""

    failures: list[str] = []
    sealed: set[date] = set()
    try:
        start = date.fromisoformat(str(payload["start"]))
        end = date.fromisoformat(str(payload["end_exclusive"]))
        files = payload["files"]
    except (KeyError, TypeError, ValueError):
        return sealed, ["manifest_structure_invalid"]
    if not isinstance(files, list):
        return sealed, ["manifest_files_invalid"]
    if payload.get("status") != "PASS":
        failures.append("manifest_status_not_pass")
    if payload.get("symbol") != "BTCUSDT":
        failures.append("manifest_symbol_mismatch")
    if start != ELIGIBLE_START:
        failures.append("manifest_start_mismatch")
    if end <= start or len(files) != (end - start).days:
        failures.append("manifest_date_span_mismatch")
    if payload.get("verified_days") != len(files):
        failures.append("manifest_verified_days_mismatch")
    if payload.get("protocol_sha256") != current_protocol:
        failures.append("manifest_protocol_stale")

    resolved_root = root.resolve()
    prior_last_id: int | None = None
    for offset, item in enumerate(files):
        if not isinstance(item, dict):
            failures.append(f"file_{offset}_metadata_invalid")
            continue
        expected_day = start + timedelta(days=offset)
        expected_name = f"BTCUSDT-aggTrades-{expected_day.isoformat()}.zip"
        try:
            item_day = date.fromisoformat(str(item["date"]))
            archive = _resolve_manifest_file(item["archive"])
            checksum = _resolve_manifest_file(item["checksum"])
            funding_file = _resolve_manifest_file(item["funding_file"])
            declared_digest = str(item["sha256"])
            declared_size = int(item["compressed_bytes"])
            declared_mtime_ns = int(item["archive_mtime_ns"])
            first_id = int(item["first_agg_trade_id"])
            last_id = int(item["last_agg_trade_id"])
        except (KeyError, TypeError, ValueError):
            failures.append(f"file_{offset}_metadata_invalid")
            continue
        if item_day != expected_day:
            failures.append(f"file_{offset}_date_not_contiguous")
        try:
            archive.resolve().relative_to(resolved_root)
            checksum.resolve().relative_to(resolved_root)
            funding_file.resolve().relative_to(resolved_root)
        except ValueError:
            failures.append(f"file_{offset}_path_outside_root")
        if archive.name != expected_name or checksum.name != f"{expected_name}.CHECKSUM":
            failures.append(f"file_{offset}_name_mismatch")
        expected_funding_name = f"BTCUSDT-funding-{expected_day.isoformat()}.json"
        if funding_file.name != expected_funding_name:
            failures.append(f"file_{offset}_funding_name_mismatch")
        if item.get("funding_query") != funding_query_url(
            symbol="BTCUSDT", day=expected_day
        ):
            failures.append(f"file_{offset}_funding_query_mismatch")
        if (
            not archive.is_file()
            or archive.stat().st_size != declared_size
            or archive.stat().st_mtime_ns != declared_mtime_ns
        ):
            failures.append(f"file_{offset}_archive_metadata_mismatch")
        if not checksum.is_file():
            failures.append(f"file_{offset}_checksum_missing")
        else:
            try:
                publisher_digest = parse_checksum(
                    checksum.read_bytes(), expected_name=expected_name
                )
            except (OSError, RuntimeError):
                failures.append(f"file_{offset}_checksum_invalid")
            else:
                if publisher_digest != declared_digest:
                    failures.append(f"file_{offset}_checksum_digest_mismatch")
        try:
            _funding_rows, funding_metadata = inspect_funding_file(
                funding_file, symbol="BTCUSDT", day=expected_day
            )
        except RuntimeError:
            failures.append(f"file_{offset}_funding_invalid")
        else:
            for key, value in funding_metadata.items():
                if item.get(key) != value:
                    failures.append(f"file_{offset}_funding_{key}_mismatch")
        if first_id > last_id:
            failures.append(f"file_{offset}_id_range_invalid")
        if prior_last_id is not None and first_id != prior_last_id + 1:
            failures.append(f"file_{offset}_cross_day_id_gap")
        prior_last_id = last_id
        if not failures:
            sealed.add(item_day)
    if failures:
        sealed.clear()
    return sealed, sorted(set(failures))


def status_payload(
    *,
    now: datetime | None = None,
    root: Path = Path("research/alpha011/raw/BTCUSDT"),
    scheduler_path: Path = SCHEDULER_STATUS_PATH,
    validation_lock_path: Path | None = None,
    validation_report_path: Path | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    last_complete_day = now.date() - timedelta(days=1)
    development_receipt = development_receipt_health()
    performance_calculated = development_receipt.get("valid") is True
    terminal_report_status = development_receipt.get("report_status")
    default_raw_root = (PROJECT_ROOT / "research" / "alpha011" / "raw" / "BTCUSDT").resolve()
    if validation_lock_path is None:
        validation_lock_path = (
            VALIDATION_LOCK_PATH
            if root.resolve() == default_raw_root
            else root / "validation-lock.json"
        )
    if validation_report_path is None:
        validation_report_path = (
            VALIDATION_REPORT_PATH
            if root.resolve() == default_raw_root
            else root / "alpha011-validation.json"
        )
    validation_phase = validation_phase_health(
        lock_path=validation_lock_path,
        report_path=validation_report_path,
    )
    validation_start = None
    required_last_archive_day = None
    if validation_phase.get("valid") is True:
        validation_start = date.fromisoformat(
            str(validation_phase["validation_start_utc"])[:10]
        )
        required_last_archive_day = validation_start + timedelta(days=VALIDATION_DAYS)
    if terminal_report_status == "FAIL_DEVELOPMENT":
        acquisition_last_day = min(last_complete_day, ELIGIBLE_START + timedelta(days=DEVELOPMENT_DAYS))
    elif validation_start is not None:
        acquisition_last_day = min(last_complete_day, required_last_archive_day)
    elif performance_calculated:
        acquisition_last_day = min(last_complete_day, ELIGIBLE_START + timedelta(days=DEVELOPMENT_DAYS))
    else:
        acquisition_last_day = last_complete_day
    if acquisition_last_day < ELIGIBLE_START:
        expected: list[date] = []
    else:
        expected = [
            ELIGIBLE_START + timedelta(days=offset)
            for offset in range((acquisition_last_day - ELIGIBLE_START).days + 1)
        ]
    current_protocol = protocol_digests()
    valid_manifests: list[tuple[date, Path, dict, set[date]]] = []
    invalid_manifests: list[dict[str, object]] = []
    for path in root.glob("manifest-*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            end = date.fromisoformat(str(payload["end_exclusive"]))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            invalid_manifests.append({"path": str(path), "failures": ["manifest_unreadable"]})
            continue
        sealed_days, failures = _inspect_manifest(
            payload, root=root, current_protocol=current_protocol
        )
        if failures:
            invalid_manifests.append({"path": str(path), "failures": failures})
        else:
            valid_manifests.append((end, path, payload, sealed_days))
    selected_manifest = max(valid_manifests, default=None, key=lambda item: item[0])
    sealed: set[date] = set(selected_manifest[3]) if selected_manifest is not None else set()
    missing_days = [day for day in expected if day not in sealed]
    missing = [day.isoformat() for day in missing_days]
    overdue_missing = [
        day.isoformat()
        for day in missing_days
        if now >= datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone.utc)
        + PUBLISHER_GRACE
    ]
    pending_publication = [
        day.isoformat() for day in missing_days if day.isoformat() not in overdue_missing
    ]
    eligible_sealed = sorted(day for day in sealed if ELIGIBLE_START <= day <= last_complete_day)
    contiguous = 0
    for day in expected:
        if day not in sealed:
            break
        contiguous += 1
    closed_development_days = max(0, contiguous - 1)
    validation_archives = 0
    if validation_start is not None and required_last_archive_day is not None:
        cursor = validation_start
        while cursor <= required_last_archive_day and cursor in sealed:
            validation_archives += 1
            cursor += timedelta(days=1)
    closed_validation_days = min(VALIDATION_DAYS, max(0, validation_archives - 1))
    validation_report_status = validation_phase.get("report_status")
    if validation_phase.get("opened") and validation_phase.get("valid") is False:
        status = "VALIDATION_LOCK_OR_REPORT_INVALID"
    elif performance_calculated and terminal_report_status == "FAIL_DEVELOPMENT":
        status = "DEVELOPMENT_FAIL"
    elif validation_report_status == "PASS":
        status = "VALIDATION_PASS_REVIEW"
    elif validation_report_status == "FAIL":
        status = "VALIDATION_FAIL"
    elif not expected:
        status = "WAITING_FIRST_COMPLETE_UTC_DAY"
    elif overdue_missing:
        status = "DOWNLOAD_REQUIRED"
    elif (
        performance_calculated
        and terminal_report_status == "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED"
        and validation_phase.get("opened") is not True
    ):
        status = "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED"
    elif validation_phase.get("valid") is True and closed_validation_days >= VALIDATION_DAYS:
        status = "VALIDATION_DATA_READY_ANALYSIS_NOT_RUN"
    elif validation_phase.get("valid") is True and pending_publication:
        status = "WAITING_PUBLISHER_ARCHIVE"
    elif validation_phase.get("valid") is True:
        status = "ACCUMULATING_LOCKED_VALIDATION"
    elif closed_development_days >= DEVELOPMENT_DAYS:
        status = "DEVELOPMENT_DATA_READY_ANALYSIS_NOT_RUN"
    elif pending_publication:
        status = "WAITING_PUBLISHER_ARCHIVE"
    elif closed_development_days < DEVELOPMENT_DAYS:
        status = "ACCUMULATING_DEVELOPMENT"
    report = {
        "schema_version": 1,
        "observed_at": now.isoformat(),
        "candidate": "ALPHA_011_QUARTER_HOUR_FLOW_CONTINUATION",
        "policy_scope": policy_scope(),
        "status": status,
        "eligible_start": ELIGIBLE_START.isoformat(),
        "last_complete_utc_day": last_complete_day.isoformat(),
        "eligible_days_elapsed": len(expected),
        "sealed_eligible_archives": len(eligible_sealed),
        "contiguous_sealed_days": contiguous,
        "development_days_required": DEVELOPMENT_DAYS,
        "development_archives_required_for_final_exit": DEVELOPMENT_DAYS + 1,
        "closed_development_days": closed_development_days,
        "development_days_remaining": max(0, DEVELOPMENT_DAYS - closed_development_days),
        "locked_validation_days_required": VALIDATION_DAYS,
        "locked_validation_opened": validation_phase.get("opened") is True,
        "validation_lock_valid": validation_phase.get("valid"),
        "validation_start_utc": validation_phase.get("validation_start_utc"),
        "validation_archives_sealed": validation_archives,
        "closed_validation_days": closed_validation_days,
        "validation_days_remaining": max(0, VALIDATION_DAYS - closed_validation_days),
        "validation_report_status": validation_report_status,
        "validation_phase": validation_phase,
        "missing_archive_days": missing,
        "publisher_grace_hours": PUBLISHER_GRACE.total_seconds() / 3600,
        "pending_publication_days": pending_publication,
        "overdue_missing_archive_days": overdue_missing,
        "performance_calculated": performance_calculated,
        "development_report_status": terminal_report_status,
        "development_receipt": development_receipt,
        "current_protocol_manifest": str(selected_manifest[1]) if selected_manifest else None,
        "current_protocol_manifest_present": selected_manifest is not None,
        "invalid_manifest_count": len(invalid_manifests),
        "invalid_manifests": invalid_manifests,
        "scheduler": scheduler_health(now=now, path=scheduler_path),
    }
    return report


def main() -> None:
    report = status_payload()
    Path("reports").mkdir(exist_ok=True)
    Path("reports/alpha011-status.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
