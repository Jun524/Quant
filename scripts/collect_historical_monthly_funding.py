#!/usr/bin/env python3
"""Acquire publisher-checksummed historical funding archives in bounded batches.

The collector reads symbols only from the sealed archival universe.  It has no
strategy inputs and does not evaluate returns; it only prepares the funding
ledger required before a later net-PnL diagnostic can run.
"""

from __future__ import annotations

import argparse
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

from quant.data.historical_universe import verify_historical_daily_universe
from quant.research.alpha015_data_guard import (
    Alpha015DataError,
    monthly_funding_archive_spec,
    validate_monthly_funding_archive,
)


MAX_ARCHIVE_BYTES = 20_000_000
MAX_CHECKSUM_BYTES = 4096


def _month_starts(start: date, end_exclusive: date) -> list[date]:
    if start.day != 1 or end_exclusive.day != 1 or start >= end_exclusive:
        raise ValueError("funding calendar must be nonempty month boundaries")
    values: list[date] = []
    current = start
    while current < end_exclusive:
        values.append(current)
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    return values


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


def _write_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"existing source differs from replay: {path}")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _fetch(url: str, *, maximum: int, timeout: float) -> bytes:
    request = Request(url, headers={"User-Agent": "quant-historical-funding/1"})
    with urlopen(request, timeout=timeout) as response:
        payload = response.read(maximum + 1)
    if len(payload) > maximum:
        raise ValueError("publisher payload exceeds fixed safety limit")
    return payload


def _paths(storage: Path, symbol: str, month: date) -> tuple[Path, Path, Path]:
    spec = monthly_funding_archive_spec(symbol, month)
    archive = storage / symbol / spec["month"] / spec["archive_name"]
    return archive, archive.with_suffix(archive.suffix + ".CHECKSUM"), archive.with_suffix(archive.suffix + ".NOT_FOUND.json")


def _not_found_receipt(spec: dict[str, str]) -> bytes:
    body = {
        "schema_version": 1,
        "kind": "PUBLISHER_FUNDING_ARCHIVE_NOT_FOUND",
        "http_status": 404,
        "url": spec["url"],
        "warning": "A trial needing funding in this symbol-month must reject the affected holding.",
    }
    return (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _verified_not_found(path: Path, spec: dict[str, str]) -> bool:
    try:
        return path.read_bytes() == _not_found_receipt(spec)
    except OSError:
        return False


def _acquire(symbol: str, month: date, *, storage: Path, timeout: float) -> str:
    spec = monthly_funding_archive_spec(symbol, month)
    archive_path, checksum_path, missing_path = _paths(storage, symbol, month)
    if archive_path.is_file() and checksum_path.is_file():
        if missing_path.exists():
            raise ValueError("conflicting present and missing funding records")
        validate_monthly_funding_archive(archive_path.read_bytes(), checksum_path.read_bytes(), symbol=symbol, month=month)
        return "cached"
    if missing_path.exists():
        if not _verified_not_found(missing_path, spec):
            raise ValueError("funding missing receipt does not match request")
        return "cached_missing"
    if archive_path.exists() or checksum_path.exists():
        raise ValueError("partial funding archive inventory is unsafe")
    try:
        checksum = _fetch(f"{spec['url']}.CHECKSUM", maximum=MAX_CHECKSUM_BYTES, timeout=timeout)
    except HTTPError as error:
        if error.code != 404:
            raise
        _write_once(missing_path, _not_found_receipt(spec))
        return "not_found"
    try:
        archive = _fetch(spec["url"], maximum=MAX_ARCHIVE_BYTES, timeout=timeout)
    except HTTPError as error:
        if error.code == 404:
            raise ValueError("funding checksum exists but archive is missing") from error
        raise
    validate_monthly_funding_archive(archive, checksum, symbol=symbol, month=month)
    _write_once(checksum_path, checksum)
    _write_once(archive_path, archive)
    return "downloaded"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=Path("research/historical-funding-acquisition-policy.json"))
    parser.add_argument("--universe", type=Path, default=Path("research/historical_universe/daily-membership-2024H1.json"))
    parser.add_argument("--storage", type=Path, default=Path("research/historical_funding/monthly"))
    parser.add_argument("--progress", type=Path, default=Path("reports/historical-funding-acquisition.json"))
    parser.add_argument("--manifest", type=Path, default=Path("research/historical_funding/manifest-2024H1.json"))
    parser.add_argument("--task-offset", type=int, default=0)
    parser.add_argument("--archive-limit", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    if args.task_offset < 0 or args.archive_limit < 0 or args.timeout <= 0:
        raise SystemExit("invalid task bounds or timeout")
    resolve = lambda path: path if path.is_absolute() else PROJECT_ROOT / path
    policy = json.loads(resolve(args.policy).read_text(encoding="utf-8"))
    universe = json.loads(resolve(args.universe).read_text(encoding="utf-8"))
    receipt = verify_historical_daily_universe(universe)
    if (
        policy.get("status") != "PREREGISTERED"
        or policy.get("promotion_capability") != "DIAGNOSTIC_ONLY"
        or policy.get("live_order_authorization") != "CLOSED"
        or policy.get("universe_sha256") != receipt["universe_sha256"]
    ):
        raise SystemExit("funding acquisition policy is not safely bound to the sealed universe")
    calendar = policy.get("months") or {}
    months = _month_starts(date.fromisoformat(str(calendar["start"])), date.fromisoformat(str(calendar["end_exclusive"])))
    membership = universe["membership"]
    assert isinstance(membership, dict)
    symbols = sorted({str(symbol) for row in membership.values() for symbol in row["selected"]})
    tasks = [(symbol, month) for symbol in symbols for month in months]
    storage, progress_path, manifest_path = resolve(args.storage), resolve(args.progress), resolve(args.manifest)

    def resolved(task: tuple[str, date]) -> bool:
        archive, checksum, missing = _paths(storage, *task)
        spec = monthly_funding_archive_spec(*task)
        return (archive.is_file() and checksum.is_file()) or _verified_not_found(missing, spec)

    if args.resume:
        selected = [task for task in tasks if not resolved(task)][:args.archive_limit]
    else:
        selected = tasks[args.task_offset:args.task_offset + args.archive_limit] if args.archive_limit else []
    counts = {"downloaded": 0, "not_found": 0, "cached": 0, "cached_missing": 0}
    for symbol, month in selected:
        counts[_acquire(symbol, month, storage=storage, timeout=args.timeout)] += 1
    present = [task for task in tasks if _paths(storage, *task)[0].is_file() and _paths(storage, *task)[1].is_file()]
    missing = [task for task in tasks if _verified_not_found(_paths(storage, *task)[2], monthly_funding_archive_spec(*task))]
    if len(present) + len(missing) != len(set(present + missing)):
        raise SystemExit("funding inventory conflicts")
    report = {
        "schema_version": 1,
        "status": "ACQUISITION_COMPLETE" if len(present) + len(missing) == len(tasks) else "ACQUISITION_IN_PROGRESS",
        "universe_sha256": receipt["universe_sha256"],
        "expected_archives": len(tasks), "verified_present_archives": len(present),
        "verified_not_found_archives": len(missing), "resolved_archives": len(present) + len(missing),
        "remaining_archives": len(tasks) - len(present) - len(missing), "batch": counts,
        "promotion_capability": "DIAGNOSTIC_ONLY", "live_order_authorization": "CLOSED",
    }
    _atomic_json(progress_path, report)
    if args.finalize:
        if report["status"] != "ACQUISITION_COMPLETE":
            raise SystemExit("cannot finalize incomplete funding acquisition")
        records = []
        for symbol, month in present:
            archive, checksum, _ = _paths(storage, symbol, month)
            verified = validate_monthly_funding_archive(archive.read_bytes(), checksum.read_bytes(), symbol=symbol, month=month)
            records.append({"symbol": symbol, "month": month.isoformat(), "sha256": verified["sha256"], "events": len(verified["events"])})
        body = {"schema_version": 1, "status": "COMPLETE" if not missing else "COMPLETE_WITH_MISSING_ARCHIVES", "universe_sha256": receipt["universe_sha256"], "records": records, "not_found": [{"symbol": s, "month": m.isoformat()} for s, m in missing], "promotion_capability": "DIAGNOSTIC_ONLY", "live_order_authorization": "CLOSED"}
        body["manifest_sha256"] = sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        _atomic_json(manifest_path, body)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (Alpha015DataError, OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"historical funding acquisition blocked: {error}") from error
