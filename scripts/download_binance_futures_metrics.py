#!/usr/bin/env python3
"""Download checksum-verified Binance USD-M daily metrics archives."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile


BASE_URL = "https://data.binance.vision/data/futures/um/daily/metrics"
REQUIRED_COLUMNS = {
    "create_time",
    "symbol",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
}
MAX_ARCHIVE_BYTES = 2_000_000
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATHS = (
    Path("research/alpha010-preregistration.json"),
    Path("scripts/download_binance_futures_metrics.py"),
    Path("quant/research/leverage_crowding.py"),
    Path("scripts/analyze_leverage_crowding.py"),
)


class DownloadError(RuntimeError):
    pass


def date_range(start: date, end_exclusive: date) -> list[date]:
    if end_exclusive <= start:
        raise ValueError("end date must be after start date")
    return [start + timedelta(days=offset) for offset in range((end_exclusive - start).days)]


def archive_name(symbol: str, day: date) -> str:
    return f"{symbol}-metrics-{day.isoformat()}.zip"


def archive_url(symbol: str, day: date) -> str:
    name = archive_name(symbol, day)
    return f"{BASE_URL}/{symbol}/{name}"


def _fetch(url: str, *, timeout: float, attempts: int = 4) -> bytes | None:
    for attempt in range(attempts):
        request = Request(url, headers={"User-Agent": "quant-research-checksum-client/1"}, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = response.read(MAX_ARCHIVE_BYTES + 1)
            if len(payload) > MAX_ARCHIVE_BYTES:
                raise DownloadError("response exceeded fixed archive size limit")
            return payload
        except HTTPError as error:
            if error.code == 404:
                return None
            if error.code not in {418, 429, 500, 502, 503, 504} or attempt + 1 == attempts:
                raise DownloadError(f"publisher returned HTTP {error.code}") from error
        except (URLError, TimeoutError) as error:
            if attempt + 1 == attempts:
                raise DownloadError(f"publisher request failed ({type(error).__name__})") from error
        time.sleep(0.5 * (2**attempt))
    raise DownloadError("publisher request retry loop exhausted")


def parse_checksum(payload: bytes, *, expected_name: str) -> str:
    try:
        fields = payload.decode("ascii").strip().split()
    except UnicodeDecodeError as error:
        raise DownloadError("checksum file is not ASCII") from error
    if len(fields) != 2 or fields[1].lstrip("*") != expected_name:
        raise DownloadError("checksum file does not bind the expected archive name")
    digest = fields[0].lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise DownloadError("checksum file does not contain a SHA-256 digest")
    return digest


def verify_archive(payload: bytes, *, expected_digest: str, expected_csv_name: str) -> dict[str, Any]:
    digest = sha256(payload).hexdigest()
    if digest != expected_digest:
        raise DownloadError("archive SHA-256 does not match publisher checksum")
    try:
        from io import BytesIO

        with ZipFile(BytesIO(payload)) as archive:
            names = archive.namelist()
            if names != [expected_csv_name]:
                raise DownloadError("archive does not contain exactly the expected CSV")
            with archive.open(expected_csv_name) as handle:
                header = handle.readline().decode("utf-8").strip().split(",")
    except (BadZipFile, UnicodeDecodeError) as error:
        raise DownloadError("archive is not a valid UTF-8 CSV zip") from error
    missing = sorted(REQUIRED_COLUMNS - set(header))
    if missing:
        raise DownloadError("archive CSV is missing required columns: " + ", ".join(missing))
    return {"sha256": digest, "bytes": len(payload), "csv": expected_csv_name}


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def download_day(symbol: str, day: date, output_dir: Path, *, timeout: float) -> dict[str, Any]:
    name = archive_name(symbol, day)
    csv_name = name.removesuffix(".zip") + ".csv"
    zip_path = output_dir / name
    checksum_path = output_dir / f"{name}.CHECKSUM"
    checksum_payload = _fetch(f"{archive_url(symbol, day)}.CHECKSUM", timeout=timeout)
    if checksum_payload is None:
        return {"date": day.isoformat(), "status": "MISSING"}
    if checksum_path.is_file() and checksum_path.read_bytes() != checksum_payload:
        raise DownloadError("publisher checksum changed after the local archive was sealed")
    expected_digest = parse_checksum(checksum_payload, expected_name=name)

    if zip_path.is_file():
        archive_payload = zip_path.read_bytes()
    else:
        archive_payload = _fetch(archive_url(symbol, day), timeout=timeout)
        if archive_payload is None:
            return {"date": day.isoformat(), "status": "MISSING"}
    verified = verify_archive(
        archive_payload,
        expected_digest=expected_digest,
        expected_csv_name=csv_name,
    )
    if not zip_path.is_file():
        _atomic_write(zip_path, archive_payload)
    if not checksum_path.is_file():
        _atomic_write(checksum_path, checksum_payload)
    return {
        "date": day.isoformat(),
        "status": "VERIFIED",
        "archive": str(zip_path),
        "checksum": str(checksum_path),
        **verified,
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def protocol_digests() -> dict[str, str]:
    return {
        str(path): sha256((PROJECT_ROOT / path).read_bytes()).hexdigest()
        for path in PROTOCOL_PATHS
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end-exclusive", type=date.fromisoformat, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("research/alpha010/raw"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    if not args.symbol.isalnum() or not args.symbol.isupper():
        raise SystemExit("symbol must be uppercase alphanumeric")
    if not 1 <= args.workers <= 16:
        raise SystemExit("workers must be between 1 and 16")
    days = date_range(args.start, args.end_exclusive)
    output_dir = args.output_root / args.symbol
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(download_day, args.symbol, day, output_dir, timeout=args.timeout): day
            for day in days
        }
        for index, future in enumerate(as_completed(futures), start=1):
            day = futures[future]
            try:
                results.append(future.result())
            except Exception as error:
                results.append(
                    {
                        "date": day.isoformat(),
                        "status": "ERROR",
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
            if index % 100 == 0 or index == len(days):
                print(f"Processed {index}/{len(days)} days", flush=True)
    results.sort(key=lambda item: item["date"])
    verified = [item for item in results if item["status"] == "VERIFIED"]
    missing = [item["date"] for item in results if item["status"] == "MISSING"]
    errors = [item for item in results if item["status"] == "ERROR"]
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if len(verified) == len(days) and not missing and not errors else "FAIL",
        "source": BASE_URL,
        "symbol": args.symbol,
        "start": args.start.isoformat(),
        "end_exclusive": args.end_exclusive.isoformat(),
        "expected_days": len(days),
        "verified_days": len(verified),
        "missing_dates": missing,
        "errors": errors,
        "protocol_sha256": protocol_digests(),
        "files": verified,
    }
    manifest_path = output_dir / f"manifest-{args.start.isoformat()}-{args.end_exclusive.isoformat()}.json"
    atomic_write_json(manifest_path, manifest)
    print(json.dumps({key: value for key, value in manifest.items() if key != "files"}, indent=2))
    print(f"Manifest: {manifest_path}")
    if manifest["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
