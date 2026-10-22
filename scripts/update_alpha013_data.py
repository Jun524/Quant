#!/usr/bin/env python3
"""Seal every currently available complete Alpha #13 UTC day."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.alpha013_status import ELIGIBLE_DATA_START, RAW_ROOT, status_payload
from scripts.download_alpha013_day import (
    Alpha013DownloadError,
    _fetch,
    archive_name,
    archive_url,
    download_day,
    parse_checksum,
)


TERMINAL_STATUSES = frozenset(
    {
        "CAUSAL_AVAILABILITY_VETO",
        "DEVELOPMENT_FAIL",
        "VALIDATION_FAIL",
        "VALIDATION_PASS_REVIEW",
    }
)


def is_terminal_status(status: str) -> bool:
    """Return whether Alpha #13 must never collect another forward sample."""

    return status in TERMINAL_STATUSES


def completed_eligible_months(now: datetime) -> list[str]:
    current_month = now.date().replace(day=1)
    cursor = ELIGIBLE_DATA_START.replace(day=1)
    output: list[str] = []
    while cursor < current_month:
        output.append(cursor.strftime("%Y-%m"))
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return output


def refresh_report_and_reconcile(now: datetime, *, validation: bool = False) -> int:
    for month in completed_eligible_months(now):
        month_days = list(RAW_ROOT.glob(f"{month}-??"))
        if not month_days:
            continue
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "reconcile_alpha013_funding.py"),
                "--month",
                month,
            ],
            cwd=PROJECT_ROOT,
            check=False,
        )
        if completed.returncode != 0:
            return completed.returncode
    analyzer = (
        "analyze_alpha013_validation.py"
        if validation
        else "analyze_alpha013_forward.py"
    )
    synchronizer = (
        "sync_alpha013_validation.py"
        if validation
        else "sync_alpha013_registry.py"
    )
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / analyzer)],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if completed.returncode != 0:
        return completed.returncode
    synchronized = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / synchronizer)],
        cwd=PROJECT_ROOT,
        check=False,
    )
    return synchronized.returncode


def publisher_has_day(day: date, *, timeout: float = 20.0) -> bool:
    symbol = "BTCUSDT"
    name = archive_name(symbol, day)
    url = archive_url("klines", symbol, day)
    try:
        payload = _fetch(f"{url}.CHECKSUM", maximum_bytes=1024, timeout=timeout, attempts=1)
    except Alpha013DownloadError as error:
        if isinstance(error.__cause__, HTTPError) and error.__cause__.code == 404:
            return False
        raise
    parse_checksum(payload, expected_name=name)
    return True


def publisher_ready_prefix(
    days: list[date], *, timeout: float = 20.0
) -> list[date]:
    """Return only the oldest contiguous missing days already published."""
    ready: list[date] = []
    for day in sorted(days):
        if not publisher_has_day(day, timeout=timeout):
            break
        ready.append(day)
    return ready


def main() -> None:
    now = datetime.now(timezone.utc)
    if now.date() <= ELIGIBLE_DATA_START:
        print(
            "Alpha #13 is waiting: first eligible complete UTC day is "
            f"{ELIGIBLE_DATA_START.isoformat()}"
        )
        raise SystemExit(refresh_report_and_reconcile(now))
    status = status_payload(now=now)
    if status["invalid_days"] or status["extra_days"]:
        raise SystemExit("FAIL: Alpha #13 existing data integrity is invalid")
    if is_terminal_status(str(status["status"])):
        print(f"Alpha #13 is terminal: {status['status']}")
        return
    if status["status"] == "VALIDATION_LOCK_OR_REPORT_INVALID":
        raise SystemExit("FAIL: Alpha #13 validation evidence is invalid")
    if status["status"] == "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED":
        opened = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "open_alpha013_validation.py")],
            cwd=PROJECT_ROOT,
            check=False,
        )
        if opened.returncode != 0:
            raise SystemExit(opened.returncode)
        status = status_payload(now=now)
    validation = status.get("locked_validation_opened") is True
    missing = [date.fromisoformat(item) for item in status["missing_complete_days"]]
    ready_days = publisher_ready_prefix(missing)
    for day in ready_days:
        manifest = download_day(day, RAW_ROOT)
        print(
            f"Alpha #13 sealed {day.isoformat()}: "
            f"{len(manifest['archives'])} archives, "
            f"{sum(item['rows'] for item in manifest['funding']['symbols'])} funding rows"
        )
    if not missing:
        latest = now.date() - timedelta(days=1)
        print(f"Alpha #13 is already current through {latest.isoformat()}")
    elif not ready_days:
        print(
            "Alpha #13 publisher archive is not available yet for "
            f"{missing[0].isoformat()}; a later scheduled run will retry"
        )
    elif len(ready_days) < len(missing):
        print(
            "Alpha #13 caught up through "
            f"{ready_days[-1].isoformat()} and is waiting for "
            f"{missing[len(ready_days)].isoformat()}"
        )
    raise SystemExit(refresh_report_and_reconcile(now, validation=validation))


if __name__ == "__main__":
    main()
