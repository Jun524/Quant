#!/usr/bin/env python3
"""Reconcile Alpha #13 daily REST funding with Binance monthly archives."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.download_alpha013_day import (
    MAX_ARCHIVE_BYTES,
    SYMBOLS,
    _fetch,
    parse_checksum,
    protocol_digests,
    validate_existing_day,
)
from scripts.reconcile_alpha011_funding import inspect_monthly_archive, month_bounds


MONTHLY_BASE_URL = "https://data.binance.vision/data/futures/um/monthly/fundingRate"
RAW_ROOT = PROJECT_ROOT / "research" / "alpha013" / "raw"
ARCHIVE_ROOT = PROJECT_ROOT / "research" / "alpha013" / "funding-monthly"
REPORT_ROOT = PROJECT_ROOT / "reports"


class Alpha013FundingReconciliationError(RuntimeError):
    pass


def compare_rows(
    rest_rows: list[dict[str, Any]], monthly_rows: list[tuple[int, Decimal]]
) -> dict[str, Any]:
    rest = {
        (int(row["fundingTime"]), Decimal(str(row["fundingRate"])))
        for row in rest_rows
    }
    if not rest:
        raise Alpha013FundingReconciliationError("sealed REST funding is empty")
    times = [timestamp for timestamp, _rate in rest]
    monthly = {
        (timestamp, rate)
        for timestamp, rate in monthly_rows
        if min(times) <= timestamp <= max(times)
    }
    missing = sorted(rest - monthly)
    unexpected = sorted(monthly - rest)
    return {
        "status": "PASS" if not missing and not unexpected else "FAIL",
        "rest_events": len(rest),
        "monthly_events_in_rest_span": len(monthly),
        "missing_from_monthly": [
            {"fundingTime": timestamp, "fundingRate": str(rate)}
            for timestamp, rate in missing
        ],
        "unexpected_monthly": [
            {"fundingTime": timestamp, "fundingRate": str(rate)}
            for timestamp, rate in unexpected
        ],
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _day_funding(day_dir: Path, symbol: str) -> tuple[list[dict[str, Any]], str]:
    path = day_dir / symbol / "funding.json"
    payload = path.read_bytes()
    decoded = json.loads(payload)
    if decoded.get("symbol") != symbol or decoded.get("day") != day_dir.name:
        raise Alpha013FundingReconciliationError("sealed funding identity mismatch")
    rows = decoded.get("rows")
    if not isinstance(rows, list):
        raise Alpha013FundingReconciliationError("sealed funding rows are invalid")
    return rows, sha256(payload).hexdigest()


def reconcile_month(
    month: str,
    *,
    raw_root: Path = RAW_ROOT,
    archive_root: Path = ARCHIVE_ROOT,
    report_root: Path = REPORT_ROOT,
    timeout: float = 30.0,
) -> dict[str, Any]:
    month_start, month_end = month_bounds(month)
    current_month = datetime.now(timezone.utc).date().replace(day=1)
    if month_end > current_month:
        raise Alpha013FundingReconciliationError("only completed calendar months may reconcile")
    selected = sorted(
        path for path in raw_root.glob(f"{month}-??")
        if path.is_dir() and month_start <= date.fromisoformat(path.name) < month_end
    )
    if not selected:
        raise Alpha013FundingReconciliationError("no sealed Alpha #13 days exist in month")
    for day_dir in selected:
        validate_existing_day(day_dir)

    symbols: dict[str, Any] = {}
    raw_hashes: dict[str, dict[str, str]] = {path.name: {} for path in selected}
    archive_root.mkdir(parents=True, exist_ok=True)
    for symbol in SYMBOLS:
        name = f"{symbol}-fundingRate-{month}.zip"
        url = f"{MONTHLY_BASE_URL}/{symbol}/{name}"
        checksum_payload = _fetch(
            f"{url}.CHECKSUM", maximum_bytes=1024, timeout=timeout
        )
        expected_digest = parse_checksum(checksum_payload, expected_name=name)
        archive_path = archive_root / name
        checksum_path = archive_root / f"{name}.CHECKSUM"
        if checksum_path.exists() and checksum_path.read_bytes() != checksum_payload:
            raise Alpha013FundingReconciliationError(
                "publisher checksum changed after monthly archive was sealed"
            )
        if archive_path.exists():
            archive_payload = archive_path.read_bytes()
        else:
            archive_payload = _fetch(
                url, maximum_bytes=MAX_ARCHIVE_BYTES, timeout=timeout
            )
            _atomic_write(archive_path, archive_payload)
        if not checksum_path.exists():
            _atomic_write(checksum_path, checksum_payload)
        monthly_rows = inspect_monthly_archive(
            archive_path,
            expected_digest=expected_digest,
            expected_csv_name=name.removesuffix(".zip") + ".csv",
            month=month,
        )
        rest_rows: list[dict[str, Any]] = []
        for day_dir in selected:
            rows, digest = _day_funding(day_dir, symbol)
            rest_rows.extend(rows)
            raw_hashes[day_dir.name][symbol] = digest
        comparison = compare_rows(rest_rows, monthly_rows)
        symbols[symbol] = {
            "publisher_archive": _display_path(archive_path),
            "publisher_sha256": expected_digest,
            "comparison": comparison,
        }
    status = "PASS" if all(
        item["comparison"]["status"] == "PASS" for item in symbols.values()
    ) else "FAIL"
    report = {
        "schema_version": 1,
        "candidate": "ALPHA_013_FUNDING_DISPERSION_REVERSAL",
        "month": month,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "protocol_sha256": protocol_digests(),
        "sealed_days": [path.name for path in selected],
        "sealed_rest_sha256": raw_hashes,
        "symbols": symbols,
    }
    report_path = report_root / f"alpha013-funding-reconciliation-{month}.json"
    _atomic_write(report_path, (json.dumps(report, indent=2) + "\n").encode())
    return report


def reconciled_coverage(
    raw_root: Path,
    *,
    start_day: date,
    end_day: date,
    report_root: Path = REPORT_ROOT,
) -> dict[str, Any]:
    required_days: list[date] = []
    cursor = start_day
    while cursor <= end_day:
        required_days.append(cursor)
        cursor = date.fromordinal(cursor.toordinal() + 1)
    failures: list[str] = []
    covered: list[str] = []
    reports: dict[str, dict[str, Any]] = {}
    for day in required_days:
        month = day.strftime("%Y-%m")
        if month not in reports:
            path = report_root / f"alpha013-funding-reconciliation-{month}.json"
            try:
                reports[month] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                reports[month] = {}
        report = reports[month]
        if report.get("status") != "PASS":
            failures.append(f"{day.isoformat()}: reconciliation receipt missing or not PASS")
            continue
        if report.get("protocol_sha256") != protocol_digests():
            failures.append(f"{day.isoformat()}: reconciliation protocol mismatch")
            continue
        hashes = (report.get("sealed_rest_sha256") or {}).get(day.isoformat())
        if not isinstance(hashes, dict) or set(hashes) != set(SYMBOLS):
            failures.append(f"{day.isoformat()}: receipt does not cover every symbol")
            continue
        day_dir = raw_root / day.isoformat()
        try:
            validate_existing_day(day_dir)
            actual = {
                symbol: sha256((day_dir / symbol / "funding.json").read_bytes()).hexdigest()
                for symbol in SYMBOLS
            }
        except Exception as error:
            failures.append(f"{day.isoformat()}: raw day invalid ({error})")
            continue
        if actual != hashes:
            failures.append(f"{day.isoformat()}: receipt/raw funding hash mismatch")
            continue
        covered.append(day.isoformat())
    return {
        "status": "PASS" if not failures and len(covered) == len(required_days) else "FAIL",
        "required_days": len(required_days),
        "covered_days": covered,
        "failures": failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    report = reconcile_month(args.month, timeout=args.timeout)
    print(json.dumps(report, indent=2))
    if report["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
