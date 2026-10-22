#!/usr/bin/env python3
"""Fetch every complete eligible Alpha #11 UTC day through yesterday."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.alpha011_status import status_payload
from scripts.download_binance_aggtrades import BASE_URL, fetch_bytes, parse_checksum


ELIGIBLE_START = date(2026, 10, 9)


def publisher_has_day(day: date, *, timeout: float = 30.0) -> bool:
    name = f"BTCUSDT-aggTrades-{day.isoformat()}.zip"
    payload = fetch_bytes(f"{BASE_URL}/BTCUSDT/{name}.CHECKSUM", timeout=timeout)
    if payload is None:
        return False
    parse_checksum(payload, expected_name=name)
    return True


def publisher_ready_prefix(
    days: list[date], *, timeout: float = 30.0
) -> list[date]:
    """Return only the oldest contiguous missing days already published."""
    ready: list[date] = []
    for day in sorted(days):
        if not publisher_has_day(day, timeout=timeout):
            break
        ready.append(day)
    return ready


def completed_eligible_months(now: datetime) -> list[str]:
    current_month = now.date().replace(day=1)
    cursor = ELIGIBLE_START.replace(day=1)
    output: list[str] = []
    while True:
        next_month = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        if next_month > current_month:
            break
        output.append(cursor.strftime("%Y-%m"))
        cursor = next_month
    return output


def reconcile_completed_months(manifest: Path, *, now: datetime) -> int:
    for month in completed_eligible_months(now):
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "reconcile_alpha011_funding.py"),
                "--manifest",
                str(manifest),
                "--month",
                month,
            ],
            cwd=PROJECT_ROOT,
            check=False,
        )
        if completed.returncode != 0:
            return completed.returncode
    return 0


def analyze_and_sync(manifest: Path) -> int:
    analyzed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "analyze_quarter_hour_flow.py"),
            "--manifest",
            str(manifest),
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if analyzed.returncode != 0:
        return analyzed.returncode
    synchronized = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "sync_alpha011_registry.py")],
        cwd=PROJECT_ROOT,
        check=False,
    )
    return synchronized.returncode


def open_validation() -> int:
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "open_alpha011_validation.py")],
        cwd=PROJECT_ROOT,
        check=False,
    )
    return completed.returncode


def analyze_validation(manifest: Path, *, now: datetime) -> int:
    reconciliation_code = reconcile_completed_months(manifest, now=now)
    if reconciliation_code:
        return reconciliation_code
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "analyze_alpha011_validation.py"),
            "--manifest",
            str(manifest),
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if completed.returncode != 0:
        return completed.returncode
    synchronized = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "sync_alpha011_validation.py")],
        cwd=PROJECT_ROOT,
        check=False,
    )
    return synchronized.returncode


def finish_ready_development(status: dict, *, now: datetime) -> int | None:
    if (
        status.get("performance_calculated") is True
        and status.get("development_report_status") == "FAIL_DEVELOPMENT"
    ):
        return 0
    if status.get("performance_calculated") is True:
        return None
    if int(status.get("closed_development_days") or 0) < 60:
        return None
    manifest_value = status.get("current_protocol_manifest")
    if not manifest_value:
        raise RuntimeError("Alpha #11 development is ready without a current manifest")
    manifest = Path(str(manifest_value))
    reconciliation_code = reconcile_completed_months(manifest, now=now)
    if reconciliation_code:
        return reconciliation_code
    return analyze_and_sync(manifest)


def main() -> None:
    now = datetime.now(timezone.utc)
    end_exclusive = now.date()
    if end_exclusive <= ELIGIBLE_START:
        print(
            f"Alpha #11 is waiting: first eligible complete UTC day is {ELIGIBLE_START.isoformat()}"
        )
        return
    status = status_payload(now=now)
    ready_code = finish_ready_development(status, now=now)
    if ready_code is not None:
        if ready_code:
            raise SystemExit(ready_code)
        print("Alpha #11 development is terminal or has been analyzed and sealed")
        return
    if status["status"] == "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED":
        open_code = open_validation()
        if open_code:
            raise SystemExit(open_code)
        status = status_payload(now=now)
    if status["status"] in {"DEVELOPMENT_FAIL", "VALIDATION_FAIL", "VALIDATION_PASS_REVIEW"}:
        print(f"Alpha #11 is terminal: {status['status']}")
        return
    if status["status"] == "VALIDATION_LOCK_OR_REPORT_INVALID":
        raise SystemExit("Alpha #11 validation evidence is invalid; refusing collection")
    if status["status"] == "VALIDATION_DATA_READY_ANALYSIS_NOT_RUN":
        manifest_value = status.get("current_protocol_manifest")
        if not manifest_value:
            raise SystemExit("Alpha #11 validation is ready without a current manifest")
        validation_code = analyze_validation(Path(str(manifest_value)), now=now)
        if validation_code:
            raise SystemExit(validation_code)
        print("Alpha #11 locked validation has been evaluated")
        return
    if status["current_protocol_manifest_present"] and not status["missing_archive_days"]:
        reconciliation_code = reconcile_completed_months(
            Path(str(status["current_protocol_manifest"])), now=now
        )
        if reconciliation_code:
            raise SystemExit(reconciliation_code)
        print(f"Alpha #11 is already current through {status['last_complete_utc_day']}")
        return
    missing_days = [date.fromisoformat(item) for item in status["missing_archive_days"]]
    ready_days = publisher_ready_prefix(missing_days)
    if not ready_days:
        next_missing = missing_days[0]
        print(
            "Alpha #11 publisher archive is not available yet for "
            f"{next_missing.isoformat()}; a later scheduled run will retry"
        )
        return
    download_end_exclusive = ready_days[-1] + timedelta(days=1)
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "download_binance_aggtrades.py"),
        "--symbol",
        "BTCUSDT",
        "--start",
        ELIGIBLE_START.isoformat(),
        "--end-exclusive",
        download_end_exclusive.isoformat(),
    ]
    completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    manifest = (
        PROJECT_ROOT
        / "research"
        / "alpha011"
        / "raw"
        / "BTCUSDT"
        / f"manifest-{ELIGIBLE_START.isoformat()}-{download_end_exclusive.isoformat()}.json"
    )
    reconciliation_code = reconcile_completed_months(manifest, now=now)
    if reconciliation_code:
        raise SystemExit(reconciliation_code)
    refreshed = status_payload(now=now)
    ready_code = finish_ready_development(refreshed, now=now)
    if ready_code:
        raise SystemExit(ready_code)
    if refreshed["status"] == "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED":
        open_code = open_validation()
        if open_code:
            raise SystemExit(open_code)
        refreshed = status_payload(now=now)
    if refreshed["status"] == "VALIDATION_DATA_READY_ANALYSIS_NOT_RUN":
        validation_code = analyze_validation(manifest, now=now)
        if validation_code:
            raise SystemExit(validation_code)


if __name__ == "__main__":
    main()
