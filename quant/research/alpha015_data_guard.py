"""Fail-closed archive and split guard for the deferred Alpha 015 candidate.

The module can be tested with synthetic bytes, but it must not be used to
acquire real returns until ``alpha015-readiness`` explicitly authorizes data
acquisition.  Development callers receive no path for the locked final 60
days, and every Binance archive is bound to its publisher CHECKSUM before it
is sealed locally.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO, TextIOWrapper
import json
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode
from zipfile import BadZipFile, ZipFile


PUBLISHER_ROOT = "https://data.binance.vision/data/futures/um/daily"
MONTHLY_FUNDING_ROOT = "https://data.binance.vision/data/futures/um/monthly/fundingRate"
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
DATASETS = {"klines_1m", "aggTrades", "funding"}
KLINE_COLUMNS = (
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
AGGTRADE_COLUMNS = (
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
)
MONTHLY_FUNDING_COLUMNS = (
    "calc_time",
    "funding_interval_hours",
    "last_funding_rate",
)
MAX_UNCOMPRESSED_BYTES = 10_000_000_000
MAX_COMPRESSED_BYTES = 2_000_000_000


class Alpha015DataError(RuntimeError):
    pass


def _symbol(value: str) -> str:
    if not value.isalnum() or not value.isupper():
        raise Alpha015DataError("symbol must be uppercase alphanumeric")
    return value


def binance_archive_symbol(universe_symbol: str) -> str:
    """Map the one accepted CCXT USDT-M perpetual form to Binance archives."""
    suffix = "/USDT:USDT"
    if not universe_symbol.endswith(suffix):
        raise Alpha015DataError("universe symbol is not a USDT-M perpetual")
    base = universe_symbol[: -len(suffix)]
    if not base or not base.isalnum() or not base.isupper():
        raise Alpha015DataError("universe base symbol is not uppercase alphanumeric")
    return _symbol(f"{base}USDT")


def required_phase_archive_specs(
    activation_lock: dict[str, Any],
    *,
    phase: str,
) -> list[dict[str, str]]:
    """Build the exact publisher archive inventory from prior-day bindings."""
    if phase == "development":
        raw_evaluation_days = list(
            map(str, activation_lock.get("development") or [])
        )
    elif phase == "locked_validation":
        raw_evaluation_days = list(
            map(str, activation_lock.get("locked_validation") or [])
        )
    else:
        raise Alpha015DataError("phase must be development or locked_validation")
    expected_count = 120 if phase == "development" else 60
    evaluation_days = set(raw_evaluation_days)
    if (
        len(raw_evaluation_days) != expected_count
        or len(evaluation_days) != expected_count
    ):
        raise Alpha015DataError("activation lock phase calendar is incomplete")
    bindings = [
        item
        for item in activation_lock.get("bindings") or []
        if str(item.get("evaluation_date") or "") in evaluation_days
    ]
    binding_days = [str(item.get("evaluation_date") or "") for item in bindings]
    if (
        len(bindings) != expected_count
        or len(set(binding_days)) != expected_count
        or set(binding_days) != evaluation_days
    ):
        raise Alpha015DataError("activation lock phase bindings are incomplete")
    specs: set[tuple[str, str, str]] = set()
    for binding in bindings:
        evaluation_day = date.fromisoformat(str(binding["evaluation_date"]))
        membership_day = date.fromisoformat(str(binding["membership_snapshot_date"]))
        if evaluation_day - membership_day != timedelta(days=1):
            raise Alpha015DataError("membership snapshot is not exactly one day lagged")
        symbols = list(map(str, binding.get("eligible_symbols") or []))
        if len(symbols) < 30 or len(symbols) != len(set(symbols)):
            raise Alpha015DataError("binding has fewer than 30 unique eligible symbols")
        for universe_symbol in symbols:
            symbol = binance_archive_symbol(universe_symbol)
            specs.add(("klines_1m", symbol, membership_day.isoformat()))
            specs.add(("klines_1m", symbol, evaluation_day.isoformat()))
            specs.add(("aggTrades", symbol, evaluation_day.isoformat()))
            specs.add(("funding", symbol, evaluation_day.isoformat()))
    return [
        {"dataset": dataset, "symbol": symbol, "date": day}
        for dataset, symbol, day in sorted(specs)
    ]


def validate_phase_archive_inventory(
    manifest: dict[str, Any],
    activation_lock: dict[str, Any],
    *,
    phase: str,
) -> dict[str, Any]:
    """Reject missing, duplicate, or extra files before opening archive paths."""
    expected = required_phase_archive_specs(activation_lock, phase=phase)
    expected_keys = {
        (item["dataset"], item["symbol"], item["date"]) for item in expected
    }
    files = manifest.get("files") or []
    actual = [
        (
            str(item.get("dataset") or ""),
            str(item.get("symbol") or ""),
            str(item.get("date") or ""),
        )
        for item in files
    ]
    actual_keys = set(actual)
    duplicates = len(actual) - len(actual_keys)
    missing = sorted(expected_keys - actual_keys)
    extra = sorted(actual_keys - expected_keys)
    if duplicates or missing or extra:
        raise Alpha015DataError(
            "archive manifest inventory mismatch: "
            f"duplicates={duplicates}, missing={len(missing)}, extra={len(extra)}"
        )
    digest = sha256(
        "\n".join("|".join(item) for item in sorted(actual_keys)).encode("utf-8")
    ).hexdigest()
    return {
        "status": "PASS",
        "phase": phase,
        "files": len(actual_keys),
        "inventory_sha256": digest,
    }


def validate_aggtrade_day_chain(
    verified_files: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Reject aggregate-trade ID gaps across adjacent included UTC days.

    Daily archive validation already proves continuity inside each file.  This
    second check closes the midnight seam whenever a symbol is present on two
    consecutive evaluation days.  A membership gap is not bridged because no
    archive was authorized for the omitted day.
    """
    by_symbol: dict[str, list[tuple[date, int, int]]] = {}
    for item in verified_files:
        if item.get("dataset") != "aggTrades":
            continue
        try:
            symbol = _symbol(str(item["symbol"]))
            day = date.fromisoformat(str(item["date"]))
            first_id = int(item["first_agg_trade_id"])
            last_id = int(item["last_agg_trade_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise Alpha015DataError(
                "aggregate-trade chain metadata is incomplete"
            ) from error
        if first_id > last_id:
            raise Alpha015DataError("aggregate-trade chain bounds are invalid")
        by_symbol.setdefault(symbol, []).append((day, first_id, last_id))

    seams_checked = 0
    files_checked = 0
    for symbol, rows in sorted(by_symbol.items()):
        rows.sort()
        if len({day for day, _, _ in rows}) != len(rows):
            raise Alpha015DataError(
                f"duplicate aggregate-trade day in chain: {symbol}"
            )
        files_checked += len(rows)
        for previous, current in zip(rows, rows[1:]):
            previous_day, _, previous_last = previous
            current_day, current_first, _ = current
            if current_day - previous_day != timedelta(days=1):
                continue
            seams_checked += 1
            if current_first != previous_last + 1:
                raise Alpha015DataError(
                    "aggregate-trade IDs are not continuous across UTC days: "
                    f"{symbol} {previous_day.isoformat()}->{current_day.isoformat()}"
                )
    return {
        "status": "PASS",
        "aggtrade_files_checked": files_checked,
        "adjacent_day_seams_checked": seams_checked,
    }


def archive_spec(dataset: str, symbol: str, day: date) -> dict[str, str]:
    symbol = _symbol(symbol)
    if dataset == "klines_1m":
        name = f"{symbol}-1m-{day.isoformat()}.zip"
        relative = f"klines/{symbol}/1m/{name}"
    elif dataset == "aggTrades":
        name = f"{symbol}-aggTrades-{day.isoformat()}.zip"
        relative = f"aggTrades/{symbol}/{name}"
    else:
        raise Alpha015DataError(f"unsupported dataset: {dataset}")
    return {
        "dataset": dataset,
        "symbol": symbol,
        "date": day.isoformat(),
        "archive_name": name,
        "csv_name": name.removesuffix(".zip") + ".csv",
        "url": f"{PUBLISHER_ROOT}/{relative}",
    }


def funding_spec(symbol: str, day: date) -> dict[str, str]:
    symbol = _symbol(symbol)
    start_ms = int(
        datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()
        * 1000
    )
    query = urlencode(
        {
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": start_ms + 86_400_000 - 1,
            "limit": 1000,
        }
    )
    return {
        "dataset": "funding",
        "symbol": symbol,
        "date": day.isoformat(),
        "file_name": f"{symbol}-funding-{day.isoformat()}.json",
        "url": f"{FUNDING_URL}?{query}",
    }


def monthly_funding_archive_spec(symbol: str, month: date) -> dict[str, str]:
    symbol = _symbol(symbol)
    if month.day != 1:
        raise Alpha015DataError("monthly funding archive date must be month start")
    label = month.strftime("%Y-%m")
    name = f"{symbol}-fundingRate-{label}.zip"
    return {
        "dataset": "funding_monthly_reconciliation",
        "symbol": symbol,
        "month": label,
        "archive_name": name,
        "csv_name": name.removesuffix(".zip") + ".csv",
        "url": f"{MONTHLY_FUNDING_ROOT}/{symbol}/{name}",
    }


def validate_monthly_funding_archive(
    archive_payload: bytes,
    checksum_payload: bytes,
    *,
    symbol: str,
    month: date,
) -> dict[str, Any]:
    """Verify and parse a Binance monthly fundingRate publisher archive."""
    spec = monthly_funding_archive_spec(symbol, month)
    expected_digest = parse_publisher_checksum(
        checksum_payload, expected_name=spec["archive_name"]
    )
    digest = sha256(archive_payload).hexdigest()
    if digest != expected_digest:
        raise Alpha015DataError(
            "monthly funding archive SHA-256 does not match publisher checksum"
        )
    next_month = (
        date(month.year + 1, 1, 1)
        if month.month == 12
        else date(month.year, month.month + 1, 1)
    )
    start_ms = int(
        datetime.combine(month, datetime.min.time(), tzinfo=timezone.utc).timestamp()
        * 1000
    )
    end_ms = int(
        datetime.combine(
            next_month, datetime.min.time(), tzinfo=timezone.utc
        ).timestamp()
        * 1000
    )
    try:
        with ZipFile(BytesIO(archive_payload)) as archive:
            if archive.namelist() != [spec["csv_name"]]:
                raise Alpha015DataError(
                    "monthly funding archive does not contain the expected CSV"
                )
            info = archive.getinfo(spec["csv_name"])
            if info.is_dir() or info.file_size <= 0 or info.file_size > MAX_UNCOMPRESSED_BYTES:
                raise Alpha015DataError("monthly funding CSV size is invalid")
            reader = csv.DictReader(
                TextIOWrapper(archive.open(info), encoding="utf-8", newline="")
            )
            if tuple(reader.fieldnames or ()) != MONTHLY_FUNDING_COLUMNS:
                raise Alpha015DataError("monthly funding CSV schema mismatch")
            events: list[dict[str, Any]] = []
            previous_time: int | None = None
            for row in reader:
                event_time = int(row["calc_time"])
                interval_hours = int(row["funding_interval_hours"])
                rate = Decimal(row["last_funding_rate"])
                if not start_ms <= event_time < end_ms:
                    raise Alpha015DataError(
                        "monthly funding event is outside the named month"
                    )
                if previous_time is not None and event_time <= previous_time:
                    raise Alpha015DataError(
                        "monthly funding event times are not unique and chronological"
                    )
                if interval_hours <= 0 or interval_hours > 24:
                    raise Alpha015DataError("monthly funding interval is invalid")
                if not rate.is_finite():
                    raise Alpha015DataError("monthly funding rate is non-finite")
                events.append(
                    {
                        "symbol": spec["symbol"],
                        "fundingTime": event_time,
                        "fundingRate": str(row["last_funding_rate"]),
                        "fundingIntervalHours": interval_hours,
                    }
                )
                previous_time = event_time
    except (BadZipFile, UnicodeDecodeError, csv.Error, ValueError, InvalidOperation) as error:
        if isinstance(error, Alpha015DataError):
            raise
        raise Alpha015DataError(
            f"monthly funding archive validation failed ({type(error).__name__})"
        ) from error
    if not events:
        raise Alpha015DataError("monthly funding archive has no events")
    return {
        **spec,
        "sha256": digest,
        "bytes": len(archive_payload),
        "csv_bytes": info.file_size,
        "events": events,
        "status": "PUBLISHER_VERIFIED",
    }


def validate_funding_payload(
    payload: bytes,
    *,
    symbol: str,
    day: date,
) -> tuple[bytes, dict[str, Any]]:
    """Canonicalize one complete public funding-history response."""
    symbol = _symbol(symbol)
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Alpha015DataError("funding response is not valid UTF-8 JSON") from error
    if not isinstance(decoded, list) or not decoded:
        raise Alpha015DataError("funding response must be a non-empty list")
    start_ms = int(
        datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()
        * 1000
    )
    end_ms = start_ms + 86_400_000
    normalized: list[dict[str, Any]] = []
    previous_time: int | None = None
    seen: set[tuple[int, str]] = set()
    for item in decoded:
        if not isinstance(item, dict):
            raise Alpha015DataError("funding response row is not an object")
        try:
            event_symbol = _symbol(str(item["symbol"]))
            funding_time = int(item["fundingTime"])
            rate = Decimal(str(item["fundingRate"]))
            mark_price = Decimal(str(item["markPrice"]))
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise Alpha015DataError("funding response row has invalid fields") from error
        rate_type = str(item.get("rateType") or "NOT_PROVIDED")
        if event_symbol != symbol:
            raise Alpha015DataError("funding response contains a different symbol")
        if not start_ms <= funding_time < end_ms:
            raise Alpha015DataError("funding timestamp is outside its UTC day")
        if previous_time is not None and funding_time < previous_time:
            raise Alpha015DataError("funding timestamps are not monotonic")
        if not rate.is_finite() or not mark_price.is_finite() or mark_price <= 0:
            raise Alpha015DataError("funding rate or mark price is invalid")
        identity = (funding_time, rate_type)
        if identity in seen:
            raise Alpha015DataError("funding response contains a duplicate event")
        seen.add(identity)
        previous_time = funding_time
        normalized.append(
            {
                "fundingRate": str(item["fundingRate"]),
                "fundingTime": funding_time,
                "markPrice": str(item["markPrice"]),
                "rateType": rate_type,
                "symbol": symbol,
            }
        )
    canonical = (
        json.dumps(normalized, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return canonical, {
        "events": len(normalized),
        "first_funding_time_ms": int(normalized[0]["fundingTime"]),
        "last_funding_time_ms": int(normalized[-1]["fundingTime"]),
        "rate_types": sorted({str(item["rateType"]) for item in normalized}),
    }


def reconcile_funding_events(
    daily_events: Iterable[dict[str, Any]],
    monthly_events: Iterable[dict[str, Any]],
    *,
    authorized_symbol_days: Iterable[tuple[str, str]],
) -> dict[str, Any]:
    """Match authorized daily API funding rows to publisher monthly rows."""
    authorized = {
        (_symbol(str(symbol)), date.fromisoformat(str(day)).isoformat())
        for symbol, day in authorized_symbol_days
    }
    if not authorized:
        raise Alpha015DataError("funding reconciliation authorization is empty")

    def identity(item: dict[str, Any]) -> tuple[str, int, str]:
        try:
            symbol = _symbol(str(item["symbol"]))
            timestamp = int(item["fundingTime"])
            rate = Decimal(str(item["fundingRate"]))
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise Alpha015DataError(
                "funding reconciliation event is invalid"
            ) from error
        if not rate.is_finite():
            raise Alpha015DataError("funding reconciliation rate is non-finite")
        day = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).date().isoformat()
        if (symbol, day) not in authorized:
            raise Alpha015DataError(
                "daily funding event is outside the authorized symbol-day set"
            )
        return symbol, timestamp, str(rate.normalize())

    daily = [identity(item) for item in daily_events]
    if len(daily) != len(set(daily)):
        raise Alpha015DataError("daily funding reconciliation contains duplicates")

    publisher: list[tuple[str, int, str]] = []
    for item in monthly_events:
        try:
            symbol = _symbol(str(item["symbol"]))
            timestamp = int(item["fundingTime"])
            rate = Decimal(str(item["fundingRate"]))
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise Alpha015DataError(
                "publisher funding reconciliation event is invalid"
            ) from error
        day = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).date().isoformat()
        if (symbol, day) not in authorized:
            continue
        if not rate.is_finite():
            raise Alpha015DataError("publisher funding rate is non-finite")
        publisher.append((symbol, timestamp, str(rate.normalize())))
    if len(publisher) != len(set(publisher)):
        raise Alpha015DataError("publisher funding reconciliation contains duplicates")
    missing = sorted(set(publisher) - set(daily))
    extra = sorted(set(daily) - set(publisher))
    if missing or extra or len(daily) != len(publisher):
        raise Alpha015DataError(
            "daily funding does not match publisher monthly archives: "
            f"missing={len(missing)}, extra={len(extra)}"
        )
    digest = sha256(
        "\n".join(
            f"{symbol}|{timestamp}|{rate}"
            for symbol, timestamp, rate in sorted(daily)
        ).encode("utf-8")
    ).hexdigest()
    return {
        "status": "PASS",
        "authorized_symbol_days": len(authorized),
        "events_reconciled": len(daily),
        "events_sha256": digest,
    }


def parse_publisher_checksum(payload: bytes, *, expected_name: str) -> str:
    try:
        fields = payload.decode("ascii").strip().split()
    except UnicodeDecodeError as error:
        raise Alpha015DataError("publisher checksum is not ASCII") from error
    if len(fields) != 2 or fields[1].lstrip("*") != expected_name:
        raise Alpha015DataError("publisher checksum does not bind the expected archive")
    digest = fields[0].lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise Alpha015DataError("publisher checksum is not a SHA-256 digest")
    return digest


def verify_publisher_archive(
    archive_payload: bytes,
    checksum_payload: bytes,
    *,
    expected_name: str,
    expected_csv_name: str,
    dataset: str | None = None,
    day: date | None = None,
) -> dict[str, Any]:
    expected_digest = parse_publisher_checksum(
        checksum_payload, expected_name=expected_name
    )
    digest = sha256(archive_payload).hexdigest()
    if digest != expected_digest:
        raise Alpha015DataError("archive SHA-256 does not match publisher checksum")
    try:
        with ZipFile(BytesIO(archive_payload)) as archive:
            if archive.namelist() != [expected_csv_name]:
                raise Alpha015DataError(
                    "archive does not contain exactly the expected CSV"
                )
            info = archive.getinfo(expected_csv_name)
            if info.is_dir() or info.file_size <= 0 or info.file_size > MAX_UNCOMPRESSED_BYTES:
                raise Alpha015DataError("publisher CSV is empty or not a file")
            with archive.open(info) as raw:
                if (dataset is None) != (day is None):
                    raise Alpha015DataError("dataset and day must be validated together")
                details = (
                    _validate_daily_csv(raw, dataset=dataset, day=day)
                    if dataset is not None and day is not None
                    else _drain(raw)
                )
    except (BadZipFile, UnicodeDecodeError, csv.Error, ValueError, InvalidOperation) as error:
        if isinstance(error, Alpha015DataError):
            raise
        raise Alpha015DataError(
            f"publisher archive validation failed ({type(error).__name__})"
        ) from error
    return {
        "sha256": digest,
        "bytes": len(archive_payload),
        "csv": expected_csv_name,
        "csv_bytes": info.file_size,
        **details,
    }


def _path_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_publisher_archive_file(
    archive_path: Path,
    checksum_payload: bytes,
    *,
    expected_name: str,
    expected_csv_name: str,
    dataset: str,
    day: date,
) -> dict[str, Any]:
    """Stream-hash and inspect an on-disk archive without loading it into RAM."""
    expected_digest = parse_publisher_checksum(
        checksum_payload, expected_name=expected_name
    )
    try:
        compressed_bytes = archive_path.stat().st_size
    except OSError as error:
        raise Alpha015DataError("publisher archive file is unavailable") from error
    if compressed_bytes <= 0 or compressed_bytes > MAX_COMPRESSED_BYTES:
        raise Alpha015DataError("publisher archive compressed size is invalid")
    digest = _path_sha256(archive_path)
    if digest != expected_digest:
        raise Alpha015DataError("archive SHA-256 does not match publisher checksum")
    try:
        with ZipFile(archive_path) as archive:
            if archive.namelist() != [expected_csv_name]:
                raise Alpha015DataError(
                    "archive does not contain exactly the expected CSV"
                )
            info = archive.getinfo(expected_csv_name)
            if info.is_dir() or info.file_size <= 0 or info.file_size > MAX_UNCOMPRESSED_BYTES:
                raise Alpha015DataError("publisher CSV is empty or not a file")
            with archive.open(info) as raw:
                details = _validate_daily_csv(raw, dataset=dataset, day=day)
    except (BadZipFile, UnicodeDecodeError, csv.Error, ValueError, InvalidOperation) as error:
        if isinstance(error, Alpha015DataError):
            raise
        raise Alpha015DataError(
            f"publisher archive validation failed ({type(error).__name__})"
        ) from error
    return {
        "sha256": digest,
        "bytes": compressed_bytes,
        "csv": expected_csv_name,
        "csv_bytes": info.file_size,
        **details,
    }


def seal_downloaded_archive_file(
    temporary_path: Path,
    checksum_payload: bytes,
    *,
    dataset: str,
    symbol: str,
    day: date,
    output_dir: Path,
) -> dict[str, Any]:
    """Validate a temporary download and atomically publish it once."""
    spec = archive_spec(dataset, symbol, day)
    verified = verify_publisher_archive_file(
        temporary_path,
        checksum_payload,
        expected_name=spec["archive_name"],
        expected_csv_name=spec["csv_name"],
        dataset=dataset,
        day=day,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / spec["archive_name"]
    checksum_path = output_dir / f"{spec['archive_name']}.CHECKSUM"
    if archive_path.exists():
        existing = verify_publisher_archive_file(
            archive_path,
            checksum_payload,
            expected_name=spec["archive_name"],
            expected_csv_name=spec["csv_name"],
            dataset=dataset,
            day=day,
        )
        if existing != verified:
            raise Alpha015DataError(
                "existing sealed archive differs from the verified download"
            )
    else:
        os.replace(temporary_path, archive_path)
    if checksum_path.exists():
        if checksum_path.read_bytes() != checksum_payload:
            raise Alpha015DataError("publisher checksum changed after local sealing")
    else:
        _atomic_write(checksum_path, checksum_payload)
    return {
        **spec,
        **verified,
        "archive": str(archive_path),
        "checksum": str(checksum_path),
        "status": "VERIFIED_AND_SEALED",
    }


def _drain(raw: Any) -> dict[str, Any]:
    while raw.read(1024 * 1024):
        pass
    return {}


def _validate_daily_csv(raw: Any, *, dataset: str, day: date) -> dict[str, Any]:
    reader = csv.DictReader(TextIOWrapper(raw, encoding="utf-8", newline=""))
    start_ms = int(
        datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()
        * 1000
    )
    end_ms = start_ms + 86_400_000
    if dataset == "klines_1m":
        if tuple(reader.fieldnames or ()) != KLINE_COLUMNS:
            raise Alpha015DataError("one-minute kline CSV schema mismatch")
        rows = 0
        for rows, row in enumerate(reader, start=1):
            open_time = int(row["open_time"])
            close_time = int(row["close_time"])
            if open_time != start_ms + (rows - 1) * 60_000 or close_time != open_time + 59_999:
                raise Alpha015DataError("one-minute klines are not a complete UTC grid")
            open_price, high, low, close = (
                Decimal(row[name]) for name in ("open", "high", "low", "close")
            )
            volume = Decimal(row["volume"])
            if (
                not all(value.is_finite() and value > 0 for value in (open_price, high, low, close))
                or high < max(open_price, close)
                or low > min(open_price, close)
                or low > high
                or not volume.is_finite()
                or volume < 0
            ):
                raise Alpha015DataError("one-minute kline contains invalid OHLCV")
            int(row["count"])
        if rows != 1440:
            raise Alpha015DataError("one-minute kline archive must contain 1440 rows")
        return {"rows": rows, "first_timestamp_ms": start_ms, "last_timestamp_ms": end_ms - 60_000}
    if dataset == "aggTrades":
        if tuple(reader.fieldnames or ()) != AGGTRADE_COLUMNS:
            raise Alpha015DataError("aggregate-trade CSV schema mismatch")
        rows = 0
        previous_id: int | None = None
        previous_time: int | None = None
        first_id: int | None = None
        for rows, row in enumerate(reader, start=1):
            trade_id = int(row["agg_trade_id"])
            timestamp = int(row["transact_time"])
            if not start_ms <= timestamp < end_ms:
                raise Alpha015DataError("aggregate-trade timestamp is outside its UTC day")
            if previous_id is not None and trade_id != previous_id + 1:
                raise Alpha015DataError("aggregate-trade IDs are not continuous")
            if previous_time is not None and timestamp < previous_time:
                raise Alpha015DataError("aggregate-trade timestamps are not monotonic")
            price = Decimal(row["price"])
            quantity = Decimal(row["quantity"])
            if not price.is_finite() or price <= 0 or not quantity.is_finite() or quantity <= 0:
                raise Alpha015DataError("aggregate trade has invalid price or quantity")
            int(row["first_trade_id"])
            int(row["last_trade_id"])
            if row["is_buyer_maker"] not in {"True", "False", "true", "false"}:
                raise Alpha015DataError("aggregate-trade maker flag is not boolean")
            first_id = trade_id if first_id is None else first_id
            previous_id, previous_time = trade_id, timestamp
        if rows == 0:
            raise Alpha015DataError("aggregate-trade archive is empty")
        return {"rows": rows, "first_agg_trade_id": first_id, "last_agg_trade_id": previous_id}
    raise Alpha015DataError(f"unsupported dataset: {dataset}")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def download_and_seal_archive(
    *,
    dataset: str,
    symbol: str,
    day: date,
    output_dir: Path,
    fetch: Callable[[str], bytes],
) -> dict[str, Any]:
    """Download CHECKSUM first, verify bytes, then atomically seal both files."""
    spec = archive_spec(dataset, symbol, day)
    archive_path = output_dir / spec["archive_name"]
    checksum_path = output_dir / f"{spec['archive_name']}.CHECKSUM"
    checksum_payload = fetch(f"{spec['url']}.CHECKSUM")
    if checksum_path.exists() and checksum_path.read_bytes() != checksum_payload:
        raise Alpha015DataError("publisher checksum changed after local sealing")
    archive_payload = archive_path.read_bytes() if archive_path.exists() else fetch(spec["url"])
    verified = verify_publisher_archive(
        archive_payload,
        checksum_payload,
        expected_name=spec["archive_name"],
        expected_csv_name=spec["csv_name"],
        dataset=dataset,
        day=day,
    )
    if not archive_path.exists():
        _atomic_write(archive_path, archive_payload)
    if not checksum_path.exists():
        _atomic_write(checksum_path, checksum_payload)
    return {
        **spec,
        **verified,
        "archive": str(archive_path),
        "checksum": str(checksum_path),
        "status": "VERIFIED_AND_SEALED",
    }


def download_and_seal_funding(
    *,
    symbol: str,
    day: date,
    output_dir: Path,
    fetch: Callable[[str], bytes],
) -> dict[str, Any]:
    """Fetch one authorized UTC day from the public funding-history API."""
    spec = funding_spec(symbol, day)
    path = output_dir / spec["file_name"]
    if path.exists():
        canonical, details = validate_funding_payload(
            path.read_bytes(), symbol=spec["symbol"], day=day
        )
        if path.read_bytes() != canonical:
            raise Alpha015DataError("sealed funding file is not canonical")
    else:
        canonical, details = validate_funding_payload(
            fetch(spec["url"]), symbol=spec["symbol"], day=day
        )
        _atomic_write(path, canonical)
    return {
        **spec,
        **details,
        "sha256": sha256(canonical).hexdigest(),
        "bytes": len(canonical),
        "file": str(path),
        "status": "API_VERIFIED_AND_SEALED",
        "monthly_publisher_reconciliation": "PENDING",
    }


def build_locked_calendar(
    days: Iterable[date],
    *,
    development_days: int = 120,
    validation_days: int = 60,
) -> dict[str, Any]:
    ordered = sorted(days)
    required = development_days + validation_days
    if len(ordered) != required or len(set(ordered)) != required:
        raise Alpha015DataError(f"calendar must contain exactly {required} unique days")
    if any(right - left != timedelta(days=1) for left, right in zip(ordered, ordered[1:])):
        raise Alpha015DataError("calendar days must be consecutive")
    development = [item.isoformat() for item in ordered[:development_days]]
    validation = [item.isoformat() for item in ordered[development_days:]]
    split_digest = sha256(
        ("development\n" + "\n".join(development) + "\nlocked_validation\n" + "\n".join(validation)).encode()
    ).hexdigest()
    return {
        "development": development,
        "locked_validation": validation,
        "split_sha256": split_digest,
        "validation_open": False,
    }


def authorized_days(
    calendar: dict[str, Any],
    *,
    phase: str,
    validation_unlock: dict[str, Any] | None = None,
    expected_preregistration_sha256: str | None = None,
    expected_development_report_sha256: str | None = None,
) -> frozenset[str]:
    if phase == "development":
        return frozenset(map(str, calendar.get("development") or []))
    if phase != "locked_validation":
        raise Alpha015DataError("phase must be development or locked_validation")
    receipt = validation_unlock or {}
    preregistration_digest = str(receipt.get("preregistration_sha256") or "")
    development_digest = str(receipt.get("development_report_sha256") or "")
    if not (
        receipt.get("status") == "AUTHORIZED"
        and receipt.get("development_gates_pass") is True
        and expected_preregistration_sha256 is not None
        and preregistration_digest == expected_preregistration_sha256
        and expected_development_report_sha256 is not None
        and development_digest == expected_development_report_sha256
        and receipt.get("calendar_sha256") == calendar.get("split_sha256")
    ):
        raise Alpha015DataError("locked validation remains inaccessible")
    return frozenset(map(str, calendar.get("locked_validation") or []))


def authorized_archive_days(
    calendar: dict[str, Any],
    *,
    phase: str,
    dataset: str,
    validation_unlock: dict[str, Any] | None = None,
    expected_preregistration_sha256: str | None = None,
    expected_development_report_sha256: str | None = None,
) -> frozenset[str]:
    """Return archive dates without widening evaluation-day authorization.

    Klines may include exactly one preceding UTC day to construct the first
    720-minute causal training window.  aggTrades are execution evidence and
    remain restricted to evaluation days only.
    """
    evaluation = authorized_days(
        calendar,
        phase=phase,
        validation_unlock=validation_unlock,
        expected_preregistration_sha256=expected_preregistration_sha256,
        expected_development_report_sha256=expected_development_report_sha256,
    )
    if dataset in {"aggTrades", "funding"}:
        return evaluation
    if dataset != "klines_1m":
        raise Alpha015DataError(f"unsupported dataset: {dataset}")
    if not evaluation:
        raise Alpha015DataError("phase has no evaluation days")
    ordered = sorted(date.fromisoformat(value) for value in evaluation)
    lookback = ordered[0] - timedelta(days=1)
    return frozenset({*evaluation, lookback.isoformat()})


def assert_manifest_phase_access(
    manifest: dict[str, Any],
    calendar: dict[str, Any],
    *,
    archive_root: Path,
    phase: str,
    validation_unlock: dict[str, Any] | None = None,
    expected_preregistration_sha256: str | None = None,
    expected_development_report_sha256: str | None = None,
) -> list[dict[str, Any]]:
    """Reject forbidden dates before a caller is given any archive path."""
    files = manifest.get("files") or []
    if not files:
        raise Alpha015DataError("archive manifest has no files")
    root = archive_root.resolve()
    verified_files: list[dict[str, Any]] = []
    for item in files:
        if item.get("dataset") not in DATASETS:
            raise Alpha015DataError("archive manifest contains an unsupported dataset")
        allowed = authorized_archive_days(
            calendar,
            phase=phase,
            dataset=str(item["dataset"]),
            validation_unlock=validation_unlock,
            expected_preregistration_sha256=expected_preregistration_sha256,
            expected_development_report_sha256=expected_development_report_sha256,
        )
        if str(item.get("date") or "") not in allowed:
            raise Alpha015DataError("archive manifest crosses the locked phase boundary")
        try:
            day = date.fromisoformat(str(item["date"]))
        except (KeyError, ValueError) as error:
            raise Alpha015DataError("archive manifest date is invalid") from error
        dataset = str(item["dataset"])
        if dataset == "funding":
            if item.get("status") != "API_VERIFIED_AND_SEALED":
                raise Alpha015DataError("funding manifest contains an unsealed file")
            spec = funding_spec(str(item.get("symbol") or ""), day)
            funding_path = Path(str(item.get("file") or ""))
            funding_path = (
                funding_path if funding_path.is_absolute() else root / funding_path
            )
            try:
                funding_path.resolve().relative_to(root)
            except ValueError as error:
                raise Alpha015DataError(
                    "funding manifest path escapes the sealed root"
                ) from error
            if (
                funding_path.name != spec["file_name"]
                or not funding_path.is_file()
            ):
                raise Alpha015DataError("funding manifest path is missing or misnamed")
            payload = funding_path.read_bytes()
            canonical, details = validate_funding_payload(
                payload,
                symbol=spec["symbol"],
                day=day,
            )
            if payload != canonical or item.get("sha256") != sha256(payload).hexdigest():
                raise Alpha015DataError(
                    "funding manifest digest or canonical bytes do not match"
                )
            verified_files.append({**item, **details})
            continue
        if item.get("status") != "VERIFIED_AND_SEALED":
            raise Alpha015DataError("archive manifest contains an unsealed file")
        spec = archive_spec(dataset, str(item.get("symbol") or ""), day)
        archive_path = Path(str(item.get("archive") or ""))
        checksum_path = Path(str(item.get("checksum") or ""))
        archive_path = archive_path if archive_path.is_absolute() else root / archive_path
        checksum_path = checksum_path if checksum_path.is_absolute() else root / checksum_path
        try:
            archive_path.resolve().relative_to(root)
            checksum_path.resolve().relative_to(root)
        except ValueError as error:
            raise Alpha015DataError("archive manifest path escapes the sealed root") from error
        if (
            archive_path.name != spec["archive_name"]
            or checksum_path.name != f"{spec['archive_name']}.CHECKSUM"
            or not archive_path.is_file()
            or not checksum_path.is_file()
        ):
            raise Alpha015DataError("archive manifest paths are missing or misnamed")
        verified = verify_publisher_archive_file(
            archive_path,
            checksum_path.read_bytes(),
            expected_name=spec["archive_name"],
            expected_csv_name=spec["csv_name"],
            dataset=spec["dataset"],
            day=day,
        )
        if item.get("sha256") != verified["sha256"]:
            raise Alpha015DataError("archive manifest digest does not match sealed bytes")
        verified_files.append({**item, **verified})
    return verified_files


def iter_reverified_archive_rows(
    item: Mapping[str, Any], *, archive_root: Path
) -> Iterator[dict[str, str]]:
    """Stream rows from one already-authorized sealed archive.

    The caller must first obtain ``item`` from ``assert_manifest_phase_access``
    or ``validate_phase_archive_bundle`` for its authorized phase.  This helper
    replays the archive checksum, filename, and full daily schema before
    reopening the CSV, so a changed local file cannot be consumed after phase
    authorization.  It deliberately yields rows rather than materialising an
    aggTrades day in memory.
    """
    dataset = str(item.get("dataset") or "")
    if dataset not in {"klines_1m", "aggTrades"}:
        raise Alpha015DataError("row streaming only supports sealed ZIP datasets")
    try:
        day = date.fromisoformat(str(item["date"]))
        spec = archive_spec(dataset, str(item["symbol"]), day)
    except (KeyError, ValueError) as error:
        raise Alpha015DataError("sealed archive row request is invalid") from error
    root = archive_root.resolve()
    archive_path = Path(str(item.get("archive") or ""))
    checksum_path = Path(str(item.get("checksum") or ""))
    archive_path = archive_path if archive_path.is_absolute() else root / archive_path
    checksum_path = checksum_path if checksum_path.is_absolute() else root / checksum_path
    try:
        archive_path.resolve().relative_to(root)
        checksum_path.resolve().relative_to(root)
    except ValueError as error:
        raise Alpha015DataError("sealed archive row path escapes the root") from error
    if not archive_path.is_file() or not checksum_path.is_file():
        raise Alpha015DataError("sealed archive row path is unavailable")
    verified = verify_publisher_archive_file(
        archive_path,
        checksum_path.read_bytes(),
        expected_name=spec["archive_name"],
        expected_csv_name=spec["csv_name"],
        dataset=dataset,
        day=day,
    )
    if item.get("sha256") != verified["sha256"]:
        raise Alpha015DataError("sealed archive row digest does not match manifest")
    try:
        with ZipFile(archive_path) as archive:
            with archive.open(spec["csv_name"]) as raw:
                reader = csv.DictReader(TextIOWrapper(raw, encoding="utf-8", newline=""))
                for row in reader:
                    yield dict(row)
    except (BadZipFile, UnicodeDecodeError, csv.Error) as error:
        raise Alpha015DataError(
            f"sealed archive row streaming failed ({type(error).__name__})"
        ) from error


def load_reverified_funding_events(
    item: Mapping[str, Any], *, archive_root: Path
) -> list[dict[str, Any]]:
    """Return canonical funding events from one already-authorized sealed file."""
    if item.get("dataset") != "funding":
        raise Alpha015DataError("funding row request does not name a funding file")
    try:
        day = date.fromisoformat(str(item["date"]))
        spec = funding_spec(str(item["symbol"]), day)
    except (KeyError, ValueError) as error:
        raise Alpha015DataError("sealed funding row request is invalid") from error
    root = archive_root.resolve()
    path = Path(str(item.get("file") or ""))
    path = path if path.is_absolute() else root / path
    try:
        path.resolve().relative_to(root)
    except ValueError as error:
        raise Alpha015DataError("sealed funding row path escapes the root") from error
    if not path.is_file() or path.name != spec["file_name"]:
        raise Alpha015DataError("sealed funding row path is unavailable or misnamed")
    payload = path.read_bytes()
    canonical, details = validate_funding_payload(
        payload, symbol=spec["symbol"], day=day
    )
    if payload != canonical or item.get("sha256") != sha256(payload).hexdigest():
        raise Alpha015DataError("sealed funding row digest or canonical bytes mismatch")
    try:
        events = json.loads(canonical)
    except json.JSONDecodeError as error:  # canonical bytes were just validated
        raise Alpha015DataError("sealed funding canonical payload is unreadable") from error
    if not isinstance(events, list) or len(events) != int(details["events"]):
        raise Alpha015DataError("sealed funding canonical event count mismatch")
    return events


def validate_phase_archive_bundle(
    manifest: dict[str, Any],
    activation_lock: dict[str, Any],
    calendar: dict[str, Any],
    *,
    archive_root: Path,
    phase: str,
    validation_unlock: dict[str, Any] | None = None,
    expected_preregistration_sha256: str | None = None,
    expected_development_report_sha256: str | None = None,
) -> dict[str, Any]:
    """Perform the complete fail-closed inventory, byte, and seam replay."""
    inventory = validate_phase_archive_inventory(
        manifest,
        activation_lock,
        phase=phase,
    )
    verified = assert_manifest_phase_access(
        manifest,
        calendar,
        archive_root=archive_root,
        phase=phase,
        validation_unlock=validation_unlock,
        expected_preregistration_sha256=expected_preregistration_sha256,
        expected_development_report_sha256=expected_development_report_sha256,
    )
    chain = validate_aggtrade_day_chain(verified)
    return {
        "status": "PASS",
        "phase": phase,
        "inventory": inventory,
        "publisher_files_reverified": len(verified),
        "aggtrade_chain": chain,
    }
