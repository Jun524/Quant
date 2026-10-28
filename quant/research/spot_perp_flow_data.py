"""Publisher-verified Binance spot and USD-M aggregate-trade archives for Alpha 016."""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from hashlib import sha256
from io import TextIOWrapper
from math import isfinite
from pathlib import Path
from typing import Any, Iterable, Iterator
from zipfile import BadZipFile, ZipFile


MARKET_ROOTS = {
    "spot": "https://data.binance.vision/data/spot/daily/aggTrades",
    "usd_m_perpetual": "https://data.binance.vision/data/futures/um/daily/aggTrades",
}
FUTURES_COLUMNS = (
    "agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id",
    "transact_time", "is_buyer_maker",
)
SPOT_COLUMNS = (*FUTURES_COLUMNS, "is_best_match")
MAX_COMPRESSED_BYTES = 2_000_000_000
MAX_UNCOMPRESSED_BYTES = 10_000_000_000


class SpotPerpFlowDataError(RuntimeError):
    pass


def _assert_market(market: str) -> None:
    if market not in MARKET_ROOTS:
        raise SpotPerpFlowDataError("unsupported aggregate-trade market")


def archive_spec(*, market: str, symbol: str, day: date) -> dict[str, str]:
    _assert_market(market)
    if symbol != "BTCUSDT":
        raise SpotPerpFlowDataError("Alpha 016 is fixed to BTCUSDT")
    name = f"{symbol}-aggTrades-{day.isoformat()}.zip"
    return {
        "market": market,
        "symbol": symbol,
        "date": day.isoformat(),
        "archive_name": name,
        "csv_name": name.removesuffix(".zip") + ".csv",
        "url": f"{MARKET_ROOTS[market]}/{symbol}/{name}",
    }


def parse_checksum(payload: bytes, *, expected_name: str) -> str:
    try:
        parts = payload.decode("ascii").strip().split()
    except UnicodeDecodeError as error:
        raise SpotPerpFlowDataError("publisher checksum is not ASCII") from error
    if len(parts) != 2 or parts[1].lstrip("*") != expected_name:
        raise SpotPerpFlowDataError("publisher checksum does not bind expected archive")
    digest = parts[0].lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise SpotPerpFlowDataError("publisher checksum is not SHA-256")
    return digest


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_columns(market: str) -> tuple[str, ...]:
    return SPOT_COLUMNS if market == "spot" else FUTURES_COLUMNS


def _csv_rows(binary: Any, *, market: str) -> Iterator[dict[str, str]]:
    """Normalize Binance's headered USD-M and headerless spot CSV variants."""
    columns = _expected_columns(market)
    reader = csv.reader(TextIOWrapper(binary, encoding="utf-8", newline=""))
    first = next(reader, None)
    if first is None:
        return
    if tuple(first) != columns:
        if len(first) != len(columns):
            raise SpotPerpFlowDataError("aggregate-trade CSV schema mismatch")
        yield dict(zip(columns, first, strict=True))
    for values in reader:
        if len(values) != len(columns):
            raise SpotPerpFlowDataError("aggregate-trade CSV row has invalid column count")
        yield dict(zip(columns, values, strict=True))


def inspect_archive(
    path: Path,
    *,
    market: str,
    symbol: str,
    day: date,
    expected_digest: str,
) -> dict[str, Any]:
    """Verify one compressed archive without trusting its filename or contents."""
    spec = archive_spec(market=market, symbol=symbol, day=day)
    if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > MAX_COMPRESSED_BYTES:
        raise SpotPerpFlowDataError("aggregate-trade archive has invalid size")
    digest = file_sha256(path)
    if digest != expected_digest:
        raise SpotPerpFlowDataError("aggregate-trade archive digest mismatch")
    start_ms = int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = start_ms + 86_400_000
    try:
        with ZipFile(path) as archive:
            if archive.namelist() != [spec["csv_name"]]:
                raise SpotPerpFlowDataError("archive does not contain exactly the expected CSV")
            info = archive.getinfo(spec["csv_name"])
            if info.is_dir() or info.file_size <= 0 or info.file_size > MAX_UNCOMPRESSED_BYTES:
                raise SpotPerpFlowDataError("aggregate-trade CSV has invalid size")
            with archive.open(info) as binary:
                previous_id: int | None = None
                previous_time: int | None = None
                rows = 0
                first_id = last_id = first_time = last_time = None
                for row in _csv_rows(binary, market=market):
                    trade_id = int(row["agg_trade_id"])
                    timestamp = int(row["transact_time"])
                    price = float(row["price"])
                    quantity = float(row["quantity"])
                    if not (trade_id >= 0 and start_ms <= timestamp < end_ms and price > 0 and quantity > 0):
                        raise SpotPerpFlowDataError("aggregate-trade row has invalid values")
                    if not isfinite(price) or not isfinite(quantity):
                        raise SpotPerpFlowDataError("aggregate-trade row is non-finite")
                    if row["is_buyer_maker"].lower() not in {"true", "false"}:
                        raise SpotPerpFlowDataError("aggregate-trade buyer-maker flag is invalid")
                    if previous_id is not None and trade_id != previous_id + 1:
                        raise SpotPerpFlowDataError("aggregate-trade IDs are not continuous within day")
                    if previous_time is not None and timestamp < previous_time:
                        raise SpotPerpFlowDataError("aggregate-trade timestamps are not monotonic")
                    first_id = trade_id if first_id is None else first_id
                    first_time = timestamp if first_time is None else first_time
                    last_id, last_time = trade_id, timestamp
                    previous_id, previous_time = trade_id, timestamp
                    rows += 1
    except (BadZipFile, UnicodeDecodeError, csv.Error, KeyError, ValueError) as error:
        if isinstance(error, SpotPerpFlowDataError):
            raise
        raise SpotPerpFlowDataError(f"aggregate-trade archive validation failed ({type(error).__name__})") from error
    if not rows:
        raise SpotPerpFlowDataError("aggregate-trade archive is empty")
    return {
        "market": market,
        "symbol": symbol,
        "date": day.isoformat(),
        "archive": str(path),
        "sha256": digest,
        "compressed_bytes": path.stat().st_size,
        "uncompressed_bytes": info.file_size,
        "rows": rows,
        "first_agg_trade_id": first_id,
        "last_agg_trade_id": last_id,
        "first_transact_time_ms": first_time,
        "last_transact_time_ms": last_time,
    }


def verify_adjacent_day_chains(files: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Verify every adjacent UTC-day ID seam independently for spot and futures."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in files:
        market = str(item.get("market") or "")
        _assert_market(market)
        grouped.setdefault(market, []).append(item)
    seams = 0
    for market, items in grouped.items():
        ordered = sorted(items, key=lambda item: str(item["date"]))
        for previous, current in zip(ordered, ordered[1:]):
            previous_day = date.fromisoformat(str(previous["date"]))
            current_day = date.fromisoformat(str(current["date"]))
            if (current_day - previous_day).days != 1:
                raise SpotPerpFlowDataError(f"{market} archive days are not consecutive")
            if int(current["first_agg_trade_id"]) != int(previous["last_agg_trade_id"]) + 1:
                raise SpotPerpFlowDataError(f"{market} aggregate-trade IDs are discontinuous across days")
            seams += 1
    return {"status": "PASS", "markets": sorted(grouped), "adjacent_day_seams_checked": seams}


def iter_verified_rows(path: Path, *, market: str, symbol: str, day: date) -> Iterator[dict[str, Any]]:
    """Yield normalized rows only after the caller has admitted the archive."""
    spec = archive_spec(market=market, symbol=symbol, day=day)
    try:
        with ZipFile(path) as archive, archive.open(spec["csv_name"]) as binary:
            for row in _csv_rows(binary, market=market):
                yield {
                    "agg_trade_id": int(row["agg_trade_id"]),
                    "transact_time": int(row["transact_time"]),
                    "price": float(row["price"]),
                    "quantity": float(row["quantity"]),
                    "is_buyer_maker": row["is_buyer_maker"].lower() == "true",
                }
    except (BadZipFile, UnicodeDecodeError, csv.Error, KeyError, ValueError) as error:
        if isinstance(error, SpotPerpFlowDataError):
            raise
        raise SpotPerpFlowDataError(f"aggregate-trade replay failed ({type(error).__name__})") from error
