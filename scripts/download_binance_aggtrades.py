#!/usr/bin/env python3
"""Download and seal Binance USD-M daily aggregate-trade archives for Alpha #11."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile


BASE_URL = "https://data.binance.vision/data/futures/um/daily/aggTrades"
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
REQUIRED_COLUMNS = (
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
)
MAX_COMPRESSED_BYTES = 2_000_000_000
MAX_UNCOMPRESSED_BYTES = 10_000_000_000
MAX_FUNDING_RESPONSE_BYTES = 2_000_000
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATHS = (
    Path("research/alpha011-preregistration.json"),
    Path("scripts/download_binance_aggtrades.py"),
    Path("quant/research/quarter_hour_flow.py"),
    Path("scripts/analyze_quarter_hour_flow.py"),
)


class DownloadError(RuntimeError):
    pass


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


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_archive(
    path: Path, *, expected_digest: str, expected_csv_name: str, day: date
) -> dict[str, Any]:
    digest = file_sha256(path)
    if digest != expected_digest:
        raise DownloadError("archive SHA-256 does not match publisher checksum")
    try:
        with ZipFile(path) as archive:
            names = archive.namelist()
            if names != [expected_csv_name]:
                raise DownloadError("archive does not contain exactly the expected CSV")
            info = archive.getinfo(expected_csv_name)
            if info.file_size <= 0 or info.file_size > MAX_UNCOMPRESSED_BYTES:
                raise DownloadError("archive CSV has an invalid uncompressed size")
            with archive.open(expected_csv_name) as raw:
                import io

                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
                if tuple(reader.fieldnames or ()) != REQUIRED_COLUMNS:
                    raise DownloadError("aggregate-trade CSV header does not match the fixed schema")
                start_ms = int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
                end_ms = start_ms + 86_400_000
                rows = 0
                first_id = None
                last_id = None
                first_time_ms = None
                last_time_ms = None
                prior_id = None
                prior_time = None
                for row in reader:
                    trade_id = int(row["agg_trade_id"])
                    timestamp = int(row["transact_time"])
                    if not start_ms <= timestamp < end_ms:
                        raise DownloadError("aggregate-trade timestamp falls outside the named UTC day")
                    if prior_id is not None and trade_id != prior_id + 1:
                        raise DownloadError("aggregate-trade IDs are not exactly continuous within the day")
                    if prior_time is not None and timestamp < prior_time:
                        raise DownloadError("aggregate-trade timestamps are not monotonic")
                    float(row["price"])
                    float(row["quantity"])
                    int(row["first_trade_id"])
                    int(row["last_trade_id"])
                    if row["is_buyer_maker"] not in {"True", "False", "true", "false"}:
                        raise DownloadError("is_buyer_maker is not boolean")
                    first_id = trade_id if first_id is None else first_id
                    first_time_ms = timestamp if first_time_ms is None else first_time_ms
                    last_id = trade_id
                    last_time_ms = timestamp
                    prior_id = trade_id
                    prior_time = timestamp
                    rows += 1
    except (BadZipFile, UnicodeDecodeError, csv.Error, ValueError, KeyError) as error:
        if isinstance(error, DownloadError):
            raise
        raise DownloadError(f"archive validation failed ({type(error).__name__})") from error
    if rows == 0:
        raise DownloadError("aggregate-trade CSV is empty")
    return {
        "sha256": digest,
        "compressed_bytes": path.stat().st_size,
        "archive_mtime_ns": path.stat().st_mtime_ns,
        "uncompressed_bytes": info.file_size,
        "rows": rows,
        "first_agg_trade_id": first_id,
        "last_agg_trade_id": last_id,
        "first_transact_time_ms": first_time_ms,
        "last_transact_time_ms": last_time_ms,
    }


def fetch_bytes(url: str, *, timeout: float, attempts: int = 4) -> bytes | None:
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": "quant-alpha011-checksum-client/1"})
            with urlopen(request, timeout=timeout) as response:
                return response.read(4096)
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


def fetch_funding_bytes(url: str, *, timeout: float, attempts: int = 4) -> bytes:
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": "quant-alpha011-funding-client/1"})
            with urlopen(request, timeout=timeout) as response:
                payload = response.read(MAX_FUNDING_RESPONSE_BYTES + 1)
            if len(payload) > MAX_FUNDING_RESPONSE_BYTES:
                raise DownloadError("funding response exceeds the fixed size limit")
            return payload
        except HTTPError as error:
            if error.code not in {418, 429, 500, 502, 503, 504} or attempt + 1 == attempts:
                raise DownloadError(f"funding endpoint returned HTTP {error.code}") from error
        except (URLError, TimeoutError) as error:
            if attempt + 1 == attempts:
                raise DownloadError(f"funding endpoint request failed ({type(error).__name__})") from error
        time.sleep(0.5 * (2**attempt))
    raise DownloadError("funding endpoint retry loop exhausted")


def validate_funding_payload(
    payload: bytes, *, symbol: str, day: date
) -> tuple[bytes, list[dict[str, Any]], dict[str, Any]]:
    try:
        rows = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DownloadError("funding response is not valid UTF-8 JSON") from error
    if not isinstance(rows, list) or not rows:
        raise DownloadError("funding response must be a non-empty list")
    start_ms = int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = start_ms + 86_400_000
    prior_time: int | None = None
    identities: set[tuple[int, str]] = set()
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise DownloadError("funding response row is not an object")
        try:
            event_symbol = str(row["symbol"])
            funding_time = int(row["fundingTime"])
            funding_rate = Decimal(str(row["fundingRate"]))
            mark_price = Decimal(str(row["markPrice"]))
            rate_type = str(row["rateType"])
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise DownloadError("funding response row has an invalid schema") from error
        if event_symbol != symbol:
            raise DownloadError("funding response contains a different symbol")
        if not start_ms <= funding_time < end_ms:
            raise DownloadError("funding timestamp falls outside the requested UTC day")
        if prior_time is not None and funding_time < prior_time:
            raise DownloadError("funding timestamps are not monotonic")
        if not funding_rate.is_finite() or not mark_price.is_finite() or mark_price <= 0:
            raise DownloadError("funding rate or mark price is invalid")
        if rate_type not in {"Regular", "Special"}:
            raise DownloadError("funding rateType is not Regular or Special")
        identity = (funding_time, rate_type)
        if identity in identities:
            raise DownloadError("funding response contains a duplicate time and rateType")
        identities.add(identity)
        prior_time = funding_time
        normalized.append(
            {
                "fundingRate": str(row["fundingRate"]),
                "fundingTime": funding_time,
                "markPrice": str(row["markPrice"]),
                "rateType": rate_type,
                "symbol": event_symbol,
            }
        )
    canonical = (json.dumps(normalized, sort_keys=True, separators=(",", ":")) + "\n").encode()
    metadata = {
        "funding_sha256": sha256(canonical).hexdigest(),
        "funding_events": len(normalized),
        "first_funding_time_ms": int(normalized[0]["fundingTime"]),
        "last_funding_time_ms": int(normalized[-1]["fundingTime"]),
        "funding_rate_types": sorted({str(row["rateType"]) for row in normalized}),
    }
    return canonical, normalized, metadata


def inspect_funding_file(path: Path, *, symbol: str, day: date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise DownloadError("funding file cannot be read") from error
    canonical, rows, metadata = validate_funding_payload(payload, symbol=symbol, day=day)
    if payload != canonical:
        raise DownloadError("funding file is not in sealed canonical form")
    return rows, metadata


def funding_query_url(*, symbol: str, day: date) -> str:
    start_ms = int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
    query = urlencode(
        {
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": start_ms + 86_400_000 - 1,
            "limit": 1000,
        }
    )
    return f"{FUNDING_URL}?{query}"


def download_file(url: str, destination: Path, *, timeout: float, attempts: int = 4) -> None:
    for attempt in range(attempts):
        temporary_handle = tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=f".{destination.name}.", delete=False
        )
        temporary = Path(temporary_handle.name)
        try:
            with temporary_handle:
                request = Request(url, headers={"User-Agent": "quant-alpha011-archive-client/1"})
                with urlopen(request, timeout=timeout) as response:
                    shutil.copyfileobj(response, temporary_handle, length=1024 * 1024)
            if temporary.stat().st_size <= 0 or temporary.stat().st_size > MAX_COMPRESSED_BYTES:
                raise DownloadError("archive has an invalid compressed size")
            os.replace(temporary, destination)
            return
        except HTTPError as error:
            if error.code not in {418, 429, 500, 502, 503, 504} or attempt + 1 == attempts:
                raise DownloadError(f"publisher returned HTTP {error.code}") from error
        except (URLError, TimeoutError) as error:
            if attempt + 1 == attempts:
                raise DownloadError(f"publisher archive request failed ({type(error).__name__})") from error
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        time.sleep(0.5 * (2**attempt))
    raise DownloadError("publisher archive retry loop exhausted")


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
    parser.add_argument("--output-root", type=Path, default=Path("research/alpha011/raw"))
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    if args.symbol != "BTCUSDT":
        raise SystemExit("Alpha #11 is fixed to BTCUSDT")
    if args.start < date(2026, 10, 9):
        raise SystemExit("Alpha #11 cannot download pre-registration dates")
    if args.end_exclusive <= args.start:
        raise SystemExit("end-exclusive must be after start")
    if args.end_exclusive > datetime.now(timezone.utc).date():
        raise SystemExit("end-exclusive cannot include the current or a future UTC day")

    output_dir = args.output_root / args.symbol
    output_dir.mkdir(parents=True, exist_ok=True)
    funding_dir = output_dir / "funding"
    funding_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for offset in range((args.end_exclusive - args.start).days):
        day = args.start + timedelta(days=offset)
        name = f"{args.symbol}-aggTrades-{day.isoformat()}.zip"
        checksum_name = f"{name}.CHECKSUM"
        url = f"{BASE_URL}/{args.symbol}/{name}"
        checksum_payload = fetch_bytes(f"{url}.CHECKSUM", timeout=args.timeout)
        if checksum_payload is None:
            raise DownloadError(f"publisher checksum is unavailable for {day}")
        expected = parse_checksum(checksum_payload, expected_name=name)
        archive_path = output_dir / name
        checksum_path = output_dir / checksum_name
        if checksum_path.exists() and checksum_path.read_bytes() != checksum_payload:
            raise DownloadError(f"publisher checksum changed after sealing {day}")
        if not archive_path.exists():
            download_file(url, archive_path, timeout=args.timeout)
        csv_name = name.removesuffix(".zip") + ".csv"
        inspected = inspect_archive(
            archive_path,
            expected_digest=expected,
            expected_csv_name=csv_name,
            day=day,
        )
        if not checksum_path.exists():
            temporary = checksum_path.with_name(f".{checksum_path.name}.{os.getpid()}.tmp")
            temporary.write_bytes(checksum_payload)
            os.replace(temporary, checksum_path)
        funding_path = funding_dir / f"{args.symbol}-funding-{day.isoformat()}.json"
        funding_payload = fetch_funding_bytes(
            funding_query_url(symbol=args.symbol, day=day), timeout=args.timeout
        )
        canonical_funding, _funding_rows, funding_metadata = validate_funding_payload(
            funding_payload, symbol=args.symbol, day=day
        )
        if funding_path.exists():
            if funding_path.read_bytes() != canonical_funding:
                raise DownloadError(f"funding response changed after sealing {day}")
        else:
            temporary = funding_path.with_name(f".{funding_path.name}.{os.getpid()}.tmp")
            temporary.write_bytes(canonical_funding)
            os.replace(temporary, funding_path)
        files.append(
            {
                "date": day.isoformat(),
                "archive": str(archive_path),
                "checksum": str(checksum_path),
                "funding_file": str(funding_path),
                "funding_query": funding_query_url(symbol=args.symbol, day=day),
                **funding_metadata,
                **inspected,
            }
        )
        print(
            f"Verified {day} ({inspected['rows']} aggregate trades, "
            f"{funding_metadata['funding_events']} funding events)",
            flush=True,
        )

    for previous, current in zip(files, files[1:]):
        if int(current["first_agg_trade_id"]) != int(previous["last_agg_trade_id"]) + 1:
            raise DownloadError("aggregate-trade IDs are discontinuous across daily archives")
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS",
        "source": {"aggregate_trades": BASE_URL, "funding": FUNDING_URL},
        "symbol": args.symbol,
        "start": args.start.isoformat(),
        "end_exclusive": args.end_exclusive.isoformat(),
        "verified_days": len(files),
        "protocol_sha256": protocol_digests(),
        "files": files,
    }
    manifest_path = output_dir / f"manifest-{args.start.isoformat()}-{args.end_exclusive.isoformat()}.json"
    temporary = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, manifest_path)
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
