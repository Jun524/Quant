#!/usr/bin/env python3
"""Seal one complete UTC day of Alpha #13 Binance public inputs."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO, StringIO
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

ARCHIVE_BASE = "https://data.binance.vision/data/futures/um/daily"
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")
ARCHIVE_KINDS = {
    "klines": "klines",
    "mark_price_klines": "markPriceKlines",
}
EXPECTED_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
)
MAX_ARCHIVE_BYTES = 2_000_000
MAX_FUNDING_BYTES = 1_000_000
PROTOCOL_PATHS = (
    Path("research/alpha013-preregistration.json"),
    Path("scripts/download_alpha013_day.py"),
    Path("quant/research/funding_dispersion_reversal.py"),
    Path("scripts/analyze_alpha013_forward.py"),
    Path("scripts/reconcile_alpha013_funding.py"),
)


class Alpha013DownloadError(RuntimeError):
    pass


def protocol_digests() -> dict[str, str]:
    return {
        str(path): sha256((PROJECT_ROOT / path).read_bytes()).hexdigest()
        for path in PROTOCOL_PATHS
    }


def _fetch(url: str, *, maximum_bytes: int, timeout: float = 20.0, attempts: int = 3) -> bytes:
    last_error: Exception | None = None
    request = Request(url, headers={"User-Agent": "quant-alpha013-forward/1.0"})
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = response.read(maximum_bytes + 1)
            if len(payload) > maximum_bytes:
                raise Alpha013DownloadError("response exceeded the fixed byte limit")
            return payload
        except HTTPError as error:
            if error.code in {418, 429}:
                raise Alpha013DownloadError(
                    f"publisher returned HTTP {error.code}; retry is disabled"
                ) from error
            last_error = error
            if error.code not in {500, 502, 503, 504} or attempt + 1 == attempts:
                break
        except (URLError, TimeoutError) as error:
            last_error = error
            if attempt + 1 == attempts:
                break
        time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise Alpha013DownloadError(f"download failed: {type(last_error).__name__}: {last_error}") from last_error


def parse_checksum(payload: bytes, *, expected_name: str) -> str:
    try:
        fields = payload.decode("ascii").strip().split()
    except UnicodeDecodeError as error:
        raise Alpha013DownloadError("checksum is not ASCII") from error
    if len(fields) != 2 or fields[1].lstrip("*") != expected_name:
        raise Alpha013DownloadError("checksum does not bind the expected archive name")
    digest = fields[0].lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise Alpha013DownloadError("checksum digest is not valid SHA-256")
    return digest


def _day_bounds_ms(day: date) -> tuple[int, int]:
    start = datetime.combine(day, datetime_time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def archive_name(symbol: str, day: date) -> str:
    return f"{symbol}-1h-{day.isoformat()}.zip"


def archive_url(kind: str, symbol: str, day: date) -> str:
    directory = ARCHIVE_KINDS[kind]
    return f"{ARCHIVE_BASE}/{directory}/{symbol}/1h/{archive_name(symbol, day)}"


def validate_hourly_archive(
    payload: bytes,
    *,
    expected_digest: str,
    symbol: str,
    day: date,
) -> dict[str, Any]:
    actual_digest = sha256(payload).hexdigest()
    if actual_digest != expected_digest:
        raise Alpha013DownloadError("archive SHA-256 does not match publisher checksum")
    expected_csv = archive_name(symbol, day).removesuffix(".zip") + ".csv"
    try:
        with ZipFile(BytesIO(payload)) as archive:
            if archive.namelist() != [expected_csv]:
                raise Alpha013DownloadError("archive must contain exactly the expected CSV")
            raw = archive.read(expected_csv).decode("utf-8")
    except (BadZipFile, UnicodeDecodeError) as error:
        raise Alpha013DownloadError("archive is not a valid UTF-8 ZIP/CSV") from error
    reader = csv.DictReader(StringIO(raw))
    if tuple(reader.fieldnames or ()) != EXPECTED_COLUMNS:
        raise Alpha013DownloadError("hourly CSV header mismatch")
    rows = list(reader)
    if len(rows) != 24:
        raise Alpha013DownloadError(f"complete UTC day must contain 24 hourly rows, got {len(rows)}")
    start_ms, end_ms = _day_bounds_ms(day)
    expected_open_times = [start_ms + hour * 3_600_000 for hour in range(24)]
    observed_open_times: list[int] = []
    for row in rows:
        try:
            open_time = int(row["open_time"])
            close_time = int(row["close_time"])
            open_price = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
        except (TypeError, ValueError) as error:
            raise Alpha013DownloadError("hourly CSV contains invalid numeric fields") from error
        if not all(math.isfinite(value) and value > 0 for value in (open_price, high, low, close)):
            raise Alpha013DownloadError("OHLC prices must be positive and finite")
        if high < max(open_price, close) or low > min(open_price, close) or high < low:
            raise Alpha013DownloadError("OHLC ordering is invalid")
        if close_time != open_time + 3_600_000 - 1:
            raise Alpha013DownloadError("hourly close_time is not the end of its candle")
        observed_open_times.append(open_time)
    if observed_open_times != expected_open_times or observed_open_times[-1] >= end_ms:
        raise Alpha013DownloadError("hourly candles are not a continuous exact UTC day")
    return {
        "sha256": actual_digest,
        "bytes": len(payload),
        "csv": expected_csv,
        "rows": len(rows),
        "first_open_time": observed_open_times[0],
        "last_open_time": observed_open_times[-1],
    }


def funding_rows(symbol: str, day: date, *, timeout: float = 20.0) -> tuple[str, list[dict[str, str | int]]]:
    start_ms, end_ms = _day_bounds_ms(day)
    params = {
        "symbol": symbol,
        "startTime": start_ms,
        "endTime": end_ms - 1,
        "limit": 1000,
    }
    url = f"{FUNDING_URL}?{urlencode(params)}"
    payload = _fetch(url, maximum_bytes=MAX_FUNDING_BYTES, timeout=timeout)
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise Alpha013DownloadError("funding response is not valid JSON") from error
    if not isinstance(decoded, list):
        raise Alpha013DownloadError("funding response must be a list")
    rows: list[dict[str, str | int]] = []
    seen: set[int] = set()
    for item in decoded:
        if not isinstance(item, dict) or item.get("symbol") != symbol:
            raise Alpha013DownloadError("funding row symbol mismatch")
        try:
            timestamp = int(item["fundingTime"])
            rate = Decimal(str(item["fundingRate"]))
            mark_price = Decimal(str(item["markPrice"]))
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise Alpha013DownloadError("funding row has invalid fields") from error
        if timestamp < start_ms or timestamp >= end_ms or timestamp in seen:
            raise Alpha013DownloadError("funding timestamps are duplicate or outside the UTC day")
        if not rate.is_finite() or not mark_price.is_finite() or mark_price <= 0:
            raise Alpha013DownloadError("funding rate/mark price is invalid")
        seen.add(timestamp)
        rows.append(
            {
                "symbol": symbol,
                "fundingTime": timestamp,
                "fundingRate": str(rate),
                "markPrice": str(mark_price),
            }
        )
    rows.sort(key=lambda item: int(item["fundingTime"]))
    return url, rows


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _file_inventory(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": str(path.relative_to(root)),
            "sha256": sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    ]


def manifest_inventory_digest(items: list[dict[str, str]]) -> str:
    """Hash the exact ordered day/manifest identity used by a forward report."""
    canonical = json.dumps(
        items,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def validate_existing_day(day_dir: Path, *, current_protocol: bool = True) -> dict[str, Any]:
    manifest_path = day_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Alpha013DownloadError(f"invalid day manifest: {manifest_path}") from error
    if manifest.get("status") != "PASS" or manifest.get("schema_version") != 1:
        raise Alpha013DownloadError("day manifest is not PASS schema v1")
    if current_protocol and manifest.get("protocol_sha256") != protocol_digests():
        raise Alpha013DownloadError("day manifest belongs to a different protocol")
    expected = manifest.get("files")
    actual = _file_inventory(day_dir)
    if expected != actual:
        raise Alpha013DownloadError("day file inventory/hash mismatch")
    return manifest


def download_day(day: date, output_root: Path, *, timeout: float = 20.0) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    if day >= today:
        raise Alpha013DownloadError("only complete UTC days before today may be sealed")
    target = output_root / day.isoformat()
    if target.exists():
        return validate_existing_day(target)
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{day.isoformat()}-", dir=output_root))
    try:
        archives: list[dict[str, Any]] = []
        funding_summary: list[dict[str, Any]] = []
        for symbol in SYMBOLS:
            symbol_dir = temporary / symbol
            for label in ARCHIVE_KINDS:
                name = archive_name(symbol, day)
                url = archive_url(label, symbol, day)
                checksum_payload = _fetch(
                    f"{url}.CHECKSUM", maximum_bytes=1024, timeout=timeout
                )
                expected_digest = parse_checksum(checksum_payload, expected_name=name)
                archive_payload = _fetch(url, maximum_bytes=MAX_ARCHIVE_BYTES, timeout=timeout)
                verified = validate_hourly_archive(
                    archive_payload,
                    expected_digest=expected_digest,
                    symbol=symbol,
                    day=day,
                )
                archive_path = symbol_dir / f"{label}.zip"
                checksum_path = symbol_dir / f"{label}.zip.CHECKSUM"
                _write_bytes(archive_path, archive_payload)
                _write_bytes(checksum_path, checksum_payload)
                archives.append({"symbol": symbol, "kind": label, "source": url, **verified})
            source_url, rows = funding_rows(symbol, day, timeout=timeout)
            funding_path = symbol_dir / "funding.json"
            _write_json(
                funding_path,
                {
                    "schema_version": 1,
                    "status": "PROVISIONAL_UNTIL_MONTHLY_RECONCILIATION",
                    "symbol": symbol,
                    "day": day.isoformat(),
                    "source": source_url,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "rows": rows,
                },
            )
            funding_summary.append({"symbol": symbol, "rows": len(rows)})
        manifest = {
            "schema_version": 1,
            "status": "PASS",
            "candidate": "ALPHA_013_FUNDING_DISPERSION_REVERSAL",
            "day": day.isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "protocol_sha256": protocol_digests(),
            "archives": archives,
            "funding": {
                "status": "PROVISIONAL_UNTIL_MONTHLY_RECONCILIATION",
                "symbols": funding_summary,
            },
            "files": _file_inventory(temporary),
        }
        _write_json(temporary / "manifest.json", manifest)
        os.replace(temporary, target)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", type=date.fromisoformat, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("research/alpha013/raw"))
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    if args.timeout <= 0 or args.timeout > 120:
        raise SystemExit("timeout must be in (0, 120]")
    manifest = download_day(args.day, args.output_root, timeout=args.timeout)
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "day": manifest["day"],
                "archives": len(manifest["archives"]),
                "funding_rows": sum(item["rows"] for item in manifest["funding"]["symbols"]),
                "output": str(args.output_root / args.day.isoformat()),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
