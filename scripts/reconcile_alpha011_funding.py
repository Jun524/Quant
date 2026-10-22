#!/usr/bin/env python3
"""Reconcile sealed Alpha #11 REST funding against Binance monthly archives."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import io
import json
import os
from pathlib import Path
import sys
from typing import Any
from zipfile import BadZipFile, ZipFile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.download_binance_aggtrades import (
    DownloadError,
    download_file,
    fetch_bytes,
    file_sha256,
    inspect_funding_file,
    parse_checksum,
    protocol_digests,
)


MONTHLY_BASE_URL = "https://data.binance.vision/data/futures/um/monthly/fundingRate"
MONTHLY_COLUMNS = ("calc_time", "funding_interval_hours", "last_funding_rate")


def month_bounds(month: str) -> tuple[date, date]:
    try:
        start = date.fromisoformat(f"{month}-01")
    except ValueError as error:
        raise ValueError("month must be YYYY-MM") from error
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start, end


def inspect_monthly_archive(
    path: Path, *, expected_digest: str, expected_csv_name: str, month: str
) -> list[tuple[int, Decimal]]:
    if file_sha256(path) != expected_digest:
        raise DownloadError("monthly funding archive SHA-256 does not match publisher checksum")
    start, end = month_bounds(month)
    start_ms = int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
    rows: list[tuple[int, Decimal]] = []
    try:
        with ZipFile(path) as archive:
            if archive.namelist() != [expected_csv_name]:
                raise DownloadError("monthly funding archive does not contain exactly the expected CSV")
            with archive.open(expected_csv_name) as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
                if tuple(reader.fieldnames or ()) != MONTHLY_COLUMNS:
                    raise DownloadError("monthly funding CSV header does not match the fixed schema")
                prior_time: int | None = None
                for row in reader:
                    timestamp = int(row["calc_time"])
                    rate = Decimal(row["last_funding_rate"])
                    interval = int(row["funding_interval_hours"])
                    if not start_ms <= timestamp < end_ms:
                        raise DownloadError("monthly funding timestamp falls outside the named month")
                    if prior_time is not None and timestamp <= prior_time:
                        raise DownloadError("monthly funding timestamps are not unique and increasing")
                    if not rate.is_finite() or interval <= 0 or interval > 24:
                        raise DownloadError("monthly funding row contains an invalid rate or interval")
                    rows.append((timestamp, rate))
                    prior_time = timestamp
    except (BadZipFile, UnicodeDecodeError, csv.Error, ValueError, KeyError, InvalidOperation) as error:
        if isinstance(error, DownloadError):
            raise
        raise DownloadError(f"monthly funding archive validation failed ({type(error).__name__})") from error
    if not rows:
        raise DownloadError("monthly funding archive is empty")
    return rows


def compare_funding_rows(
    rest_rows: list[dict[str, Any]], monthly_rows: list[tuple[int, Decimal]]
) -> dict[str, Any]:
    regular = {
        (int(row["fundingTime"]), Decimal(str(row["fundingRate"])))
        for row in rest_rows
        if row["rateType"] == "Regular"
    }
    special = [row for row in rest_rows if row["rateType"] == "Special"]
    if not regular:
        raise DownloadError("sealed REST funding contains no Regular rows to reconcile")
    times = [timestamp for timestamp, _rate in regular]
    monthly = {
        (timestamp, rate)
        for timestamp, rate in monthly_rows
        if min(times) <= timestamp <= max(times)
    }
    missing_from_monthly = sorted(regular - monthly)
    unexpected_monthly = sorted(monthly - regular)
    return {
        "status": "PASS" if not missing_from_monthly and not unexpected_monthly else "FAIL",
        "rest_regular_events": len(regular),
        "rest_special_events": len(special),
        "monthly_events_in_rest_span": len(monthly),
        "missing_from_monthly": [
            {"fundingTime": timestamp, "fundingRate": str(rate)}
            for timestamp, rate in missing_from_monthly
        ],
        "unexpected_monthly": [
            {"fundingTime": timestamp, "fundingRate": str(rate)}
            for timestamp, rate in unexpected_monthly
        ],
        "special_note": "Special rows are sealed from the official REST response but are not represented by the monthly archive schema.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--month", required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--output-root", type=Path, default=Path("research/alpha011/funding-monthly")
    )
    args = parser.parse_args()
    month_start, month_end = month_bounds(args.month)
    if month_end > datetime.now(timezone.utc).date().replace(day=1):
        raise SystemExit("only completed calendar months can be reconciled")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("status") != "PASS" or manifest.get("symbol") != "BTCUSDT":
        raise SystemExit("manifest is not a passing BTCUSDT Alpha #11 acquisition")
    if manifest.get("protocol_sha256") != protocol_digests():
        raise SystemExit("manifest protocol is stale")
    selected = [
        item
        for item in manifest.get("files") or []
        if month_start <= date.fromisoformat(str(item["date"])) < month_end
    ]
    if not selected:
        raise SystemExit("manifest has no eligible sealed funding days in the requested month")

    name = f"BTCUSDT-fundingRate-{args.month}.zip"
    url = f"{MONTHLY_BASE_URL}/BTCUSDT/{name}"
    checksum_payload = fetch_bytes(f"{url}.CHECKSUM", timeout=args.timeout)
    if checksum_payload is None:
        print(json.dumps({"status": "PENDING_PUBLISHER", "month": args.month}, indent=2))
        return
    expected_digest = parse_checksum(checksum_payload, expected_name=name)
    args.output_root.mkdir(parents=True, exist_ok=True)
    archive_path = args.output_root / name
    checksum_path = args.output_root / f"{name}.CHECKSUM"
    if checksum_path.exists() and checksum_path.read_bytes() != checksum_payload:
        raise DownloadError("monthly publisher checksum changed after sealing")
    if not archive_path.exists():
        download_file(url, archive_path, timeout=args.timeout)
    monthly_rows = inspect_monthly_archive(
        archive_path,
        expected_digest=expected_digest,
        expected_csv_name=name.removesuffix(".zip") + ".csv",
        month=args.month,
    )
    if not checksum_path.exists():
        temporary = checksum_path.with_name(f".{checksum_path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(checksum_payload)
        os.replace(temporary, checksum_path)

    rest_rows: list[dict[str, Any]] = []
    rest_hashes: dict[str, str] = {}
    for item in selected:
        day = date.fromisoformat(str(item["date"]))
        path = Path(str(item["funding_file"]))
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        rows, metadata = inspect_funding_file(path, symbol="BTCUSDT", day=day)
        if metadata["funding_sha256"] != item.get("funding_sha256"):
            raise DownloadError("sealed REST funding differs from acquisition manifest")
        rest_rows.extend(rows)
        rest_hashes[day.isoformat()] = metadata["funding_sha256"]
    comparison = compare_funding_rows(rest_rows, monthly_rows)
    report = {
        "schema_version": 1,
        "candidate": "ALPHA_011_QUARTER_HOUR_FLOW_CONTINUATION",
        "month": args.month,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": comparison["status"],
        "manifest": str(args.manifest),
        "protocol_sha256": protocol_digests(),
        "publisher_archive": str(archive_path),
        "publisher_sha256": expected_digest,
        "sealed_rest_days": len(selected),
        "sealed_rest_sha256": rest_hashes,
        "comparison": comparison,
    }
    report_path = Path("reports") / f"alpha011-funding-reconciliation-{args.month}.json"
    report_path.parent.mkdir(exist_ok=True)
    temporary = report_path.with_name(f".{report_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, report_path)
    print(json.dumps(report, indent=2))
    if report["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
