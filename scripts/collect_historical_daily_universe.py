#!/usr/bin/env python3
"""Acquire checksum-bound 1d archives for the preregistered historical universe.

The command intentionally works in small deterministic batches.  It never
emits a universe result from partial acquisition; a complete catalog/month
inventory is required before ``--finalize`` can create a diagnostic membership
artifact.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.data.historical_universe import (
    HistoricalDailyBar,
    HistoricalUniverseError,
    build_historical_daily_universe,
    monthly_1d_archive_spec,
    parse_publisher_checksum,
    validate_monthly_1d_archive,
)


MAX_ARCHIVE_BYTES = 20_000_000
MAX_CHECKSUM_BYTES = 4_096


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from error


def _months(start: date, end_exclusive: date) -> list[date]:
    if start >= end_exclusive:
        raise HistoricalUniverseError("archive calendar must be non-empty")
    current = date(start.year, start.month, 1)
    terminal = date(end_exclusive.year, end_exclusive.month, 1)
    values: list[date] = []
    while current < terminal:
        values.append(current)
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    return values


def _fetch(url: str, *, maximum: int, timeout: float) -> bytes:
    request = Request(url, headers={"User-Agent": "quant-historical-universe/1"})
    with urlopen(request, timeout=timeout) as response:
        payload = response.read(maximum + 1)
    if len(payload) > maximum:
        raise HistoricalUniverseError("publisher payload exceeds fixed safety limit")
    return payload


def _write_if_identical_or_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise HistoricalUniverseError(f"existing archive differs from publisher replay: {path}")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _load_catalog(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("status") != "SOURCE_CATALOG_SEALED_NOT_DAY_MEMBERSHIP":
        raise HistoricalUniverseError("catalogue status is not a sealed source inventory")
    symbols = payload.get("symbols")
    digest = payload.get("catalog_sha256")
    if not isinstance(symbols, list) or not symbols or not isinstance(digest, str) or len(digest) != 64:
        raise HistoricalUniverseError("catalogue fields are invalid")
    return payload


def _archive_path(root: Path, spec: dict[str, str]) -> Path:
    return root / spec["symbol"] / spec["month"] / spec["filename"]


def _not_found_path(archive_path: Path) -> Path:
    return archive_path.with_suffix(archive_path.suffix + ".NOT_FOUND.json")


def _not_found_receipt(spec: dict[str, str]) -> bytes:
    body = {
        "schema_version": 1,
        "kind": "PUBLISHER_ARCHIVE_NOT_FOUND",
        "http_status": 404,
        "archive_url": spec["archive_url"],
        "checksum_url": spec["checksum_url"],
        "warning": "No archive is not proof of contract inactivity or delisting.",
    }
    return (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _is_verified_not_found(path: Path, spec: dict[str, str]) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload == json.loads(_not_found_receipt(spec).decode("utf-8"))


def _load_verified_cached_bar(path: Path, spec: dict[str, str]) -> list[HistoricalDailyBar]:
    archive = path.read_bytes()
    checksum_path = path.with_suffix(path.suffix + ".CHECKSUM")
    expected = parse_publisher_checksum(checksum_path.read_bytes(), expected_filename=spec["filename"])
    if sha256(archive).hexdigest() != expected:
        raise HistoricalUniverseError(f"cached archive checksum mismatch: {path}")
    return validate_monthly_1d_archive(archive, symbol=spec["symbol"], month=spec["month"])


def _acquire_one(spec: dict[str, str], *, storage: Path, timeout: float) -> str:
    """Acquire one independent archive atomically; return its observed state."""
    archive_path = _archive_path(storage, spec)
    checksum_path = archive_path.with_suffix(archive_path.suffix + ".CHECKSUM")
    not_found_path = _not_found_path(archive_path)
    if archive_path.exists() and checksum_path.exists():
        if not_found_path.exists():
            raise HistoricalUniverseError("archive has conflicting present and not-found records")
        _load_verified_cached_bar(archive_path, spec)
        return "cached"
    if not_found_path.exists():
        if not _is_verified_not_found(not_found_path, spec):
            raise HistoricalUniverseError("not-found receipt does not match requested archive")
        return "cached"
    if archive_path.exists() or checksum_path.exists():
        raise HistoricalUniverseError("partial cached archive inventory is unsafe")
    try:
        checksum = _fetch(spec["checksum_url"], maximum=MAX_CHECKSUM_BYTES, timeout=timeout)
    except HTTPError as error:
        if error.code != 404:
            raise
        _write_if_identical_or_new(not_found_path, _not_found_receipt(spec))
        return "not_found"
    expected = parse_publisher_checksum(checksum, expected_filename=spec["filename"])
    try:
        archive = _fetch(spec["archive_url"], maximum=MAX_ARCHIVE_BYTES, timeout=timeout)
    except HTTPError as error:
        if error.code == 404:
            raise HistoricalUniverseError("publisher checksum exists but its archive is missing") from error
        raise
    if sha256(archive).hexdigest() != expected:
        raise HistoricalUniverseError(f"publisher checksum mismatch: {spec['archive_url']}")
    validate_monthly_1d_archive(archive, symbol=spec["symbol"], month=spec["month"])
    _write_if_identical_or_new(archive_path, archive)
    _write_if_identical_or_new(checksum_path, checksum)
    return "downloaded"


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path("research/historical_universe/source-symbol-catalog.json"))
    parser.add_argument("--policy", type=Path, default=Path("research/historical-universe-reconstruction-policy.json"))
    parser.add_argument("--storage", type=Path, default=Path("research/historical_universe/monthly_1d"))
    parser.add_argument("--progress", type=Path, default=Path("reports/historical-universe-acquisition.json"))
    parser.add_argument("--output", type=Path, default=Path("research/historical_universe/daily-membership-2024H1.json"))
    parser.add_argument("--task-offset", type=int, default=0)
    parser.add_argument("--archive-limit", type=int, default=0, help="positive bounded batch size; 0 only with --finalize")
    parser.add_argument("--resume", action="store_true", help="select the next unresolved archive tasks instead of an absolute offset")
    parser.add_argument("--timeout", type=float, default=20.0)
    # The public archive intermittently throttles parallel requests.  Keep the
    # default serial and let an operator opt in to a small parallel probe.
    parser.add_argument("--workers", type=int, default=1, help="bounded publisher requests (default: serial)")
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    if args.task_offset < 0 or args.archive_limit < 0 or args.timeout <= 0 or not 1 <= args.workers <= 8:
        raise SystemExit("invalid task bounds, timeout, or --workers (must be 1..8)")
    catalog_path = args.catalog if args.catalog.is_absolute() else PROJECT_ROOT / args.catalog
    policy_path = args.policy if args.policy.is_absolute() else PROJECT_ROOT / args.policy
    storage = args.storage if args.storage.is_absolute() else PROJECT_ROOT / args.storage
    progress_path = args.progress if args.progress.is_absolute() else PROJECT_ROOT / args.progress
    output_path = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    catalog = _load_catalog(catalog_path)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if policy.get("status") != "PREREGISTERED" or policy.get("promotion_capability") != "DIAGNOSTIC_ONLY":
        raise HistoricalUniverseError("historical universe policy is not a preregistered diagnostic protocol")
    calendar = policy.get("calendar", {})
    start = _parse_day(calendar["archive_start"])
    end_exclusive = _parse_day(calendar["archive_end_exclusive"])
    months = _months(start, end_exclusive)
    tasks = [
        monthly_1d_archive_spec(symbol, month)
        for symbol in catalog["symbols"]
        for month in months
    ]
    def resolved(spec: dict[str, str]) -> bool:
        archive_path = _archive_path(storage, spec)
        return (
            archive_path.exists()
            and archive_path.with_suffix(archive_path.suffix + ".CHECKSUM").exists()
        ) or _is_verified_not_found(_not_found_path(archive_path), spec)

    if args.resume:
        selected = [spec for spec in tasks if not resolved(spec)][: args.archive_limit]
    else:
        selected = tasks[args.task_offset : args.task_offset + args.archive_limit] if args.archive_limit else []
    downloaded = 0
    observed_not_found = 0
    verified_cached = 0
    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="historic-archive") as executor:
        futures = [executor.submit(_acquire_one, spec, storage=storage, timeout=args.timeout) for spec in selected]
        for future in as_completed(futures):
            result = future.result()
            if result == "downloaded":
                downloaded += 1
            elif result == "not_found":
                observed_not_found += 1
            else:
                verified_cached += 1

    present_specs = [
        spec for spec in tasks
        if _archive_path(storage, spec).exists()
        and _archive_path(storage, spec).with_suffix(".zip.CHECKSUM").exists()
    ]
    absent_specs = [spec for spec in tasks if _is_verified_not_found(_not_found_path(_archive_path(storage, spec)), spec)]
    if len(present_specs) + len(absent_specs) != len(set((item["symbol"], item["month"]) for item in present_specs + absent_specs)):
        raise HistoricalUniverseError("archive inventory contains conflicting or duplicate observations")
    resolved_archives = len(present_specs) + len(absent_specs)
    report = {
        "schema_version": 1,
        "status": "ACQUISITION_COMPLETE" if resolved_archives == len(tasks) else "ACQUISITION_IN_PROGRESS",
        "source_catalog_sha256": catalog["catalog_sha256"],
        "calendar": {"start": start.isoformat(), "end_exclusive": end_exclusive.isoformat(), "months": [item.isoformat() for item in months]},
        "expected_archives": len(tasks),
        "verified_present_archives": len(present_specs),
        "verified_not_found_archives": len(absent_specs),
        "resolved_archives": resolved_archives,
        "remaining_archives": len(tasks) - resolved_archives,
        "batch": {"task_offset": args.task_offset, "archive_limit": args.archive_limit, "resume": args.resume, "workers": args.workers, "downloaded": downloaded, "not_found": observed_not_found, "verified_cached": verified_cached},
        "promotion_capability": "DIAGNOSTIC_ONLY",
        "live_order_authorization": "CLOSED",
    }
    _atomic_json(progress_path, report)

    if args.finalize:
        if resolved_archives != len(tasks):
            raise SystemExit("cannot finalize: historical archive acquisition is incomplete")
        bars: list[HistoricalDailyBar] = []
        for spec in present_specs:
            bars.extend(_load_verified_cached_bar(_archive_path(storage, spec), spec))
        selection = policy["selection"]
        artifact = build_historical_daily_universe(
            bars,
            catalog_sha256=catalog["catalog_sha256"],
            min_listing_age_days=int(selection["min_listing_age_days"]),
            min_continuous_days=int(selection["min_continuous_days"]),
            min_quote_volume=float(selection["min_quote_volume_1d"]),
        )
        _atomic_json(output_path, artifact)
        report["membership_output"] = str(output_path.relative_to(PROJECT_ROOT))
        report["membership_sha256"] = artifact["universe_sha256"]
        _atomic_json(progress_path, report)

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
