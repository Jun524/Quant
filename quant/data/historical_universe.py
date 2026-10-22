"""Reconstruct a bounded historical USD-M universe from Binance public archives.

The archive listing tells us which symbols have *some* retained data.  This
module deliberately goes one level further: it uses only the 1d candle that
was available on each decision day plus preceding candles to create a
repeatable, day-level liquidity and continuity screen.  It does not invent
historical contract-status, spread, or order-book measurements, so every
output remains diagnostic-only.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from io import BytesIO, TextIOWrapper
import json
import math
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping
from urllib.parse import quote
from zipfile import BadZipFile, ZipFile


MONTHLY_KLINE_ROOT = "https://data.binance.vision/data/futures/um/monthly/klines"
KLINE_1D_HEADER = (
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
)


class HistoricalUniverseError(ValueError):
    """An archive payload or a historical-universe input is unsafe."""


@dataclass(frozen=True)
class HistoricalDailyBar:
    symbol: str
    day: date
    open_time_ms: int
    close_time_ms: int
    quote_volume: float
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    base_volume: float
    trade_count: int


def month_key(value: date) -> str:
    """Return the fixed UTC calendar month key used by Binance archive names."""
    return f"{value.year:04d}-{value.month:02d}"


def monthly_1d_archive_spec(symbol: str, value: date) -> dict[str, str]:
    """Build a publisher URL without accepting a caller-controlled path."""
    if not symbol.isalnum() or symbol != symbol.upper() or not symbol.endswith("USDT"):
        raise HistoricalUniverseError("symbol must be an uppercase alphanumeric USDT contract")
    month = month_key(value)
    filename = f"{symbol}-1d-{month}.zip"
    # Archive listings legitimately include non-ASCII contract symbols.  Keep
    # their original identifier for the publisher checksum and local binding,
    # but percent-encode both URL path components before an HTTP request.
    base = f"{MONTHLY_KLINE_ROOT}/{quote(symbol, safe='')}/1d/{quote(filename, safe='')}"
    return {
        "symbol": symbol,
        "month": month,
        "filename": filename,
        "csv_filename": f"{symbol}-1d-{month}.csv",
        "archive_url": base,
        "checksum_url": f"{base}.CHECKSUM",
    }


def parse_publisher_checksum(payload: bytes, *, expected_filename: str) -> str:
    """Read Binance's one-line SHA-256 sidecar without trusting its filename."""
    try:
        fields = payload.decode("utf-8").strip().split()
    except UnicodeDecodeError as error:
        raise HistoricalUniverseError("publisher checksum is not UTF-8") from error
    if len(fields) != 2 or fields[1] != expected_filename:
        raise HistoricalUniverseError("publisher checksum filename does not match archive")
    digest = fields[0].lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise HistoricalUniverseError("publisher checksum digest is invalid")
    return digest


def validate_monthly_1d_archive(
    archive_payload: bytes,
    *,
    symbol: str,
    month: str,
) -> list[HistoricalDailyBar]:
    """Validate and parse a single, checksum-verified monthly archive.

    The time checks intentionally reject rows outside the archive-named month,
    duplicated days, partial daily candles, and non-finite quote volume.
    """
    if len(month) != 7 or month[4] != "-":
        raise HistoricalUniverseError("month must be YYYY-MM")
    try:
        year, number = (int(part) for part in month.split("-"))
        if not 1 <= number <= 12:
            raise ValueError
    except ValueError as error:
        raise HistoricalUniverseError("month must be a valid YYYY-MM") from error
    expected_name = f"{symbol}-1d-{month}.csv"
    try:
        with ZipFile(BytesIO(archive_payload)) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename != expected_name:
                raise HistoricalUniverseError("monthly archive has an unexpected member inventory")
            member = members[0]
            if member.is_dir() or PurePosixPath(member.filename).name != member.filename:
                raise HistoricalUniverseError("monthly archive member path is unsafe")
            if member.file_size > 20_000_000 or member.compress_size > 20_000_000:
                raise HistoricalUniverseError("monthly archive exceeds fixed safety limit")
            with archive.open(member) as raw:
                reader = csv.reader(TextIOWrapper(raw, encoding="utf-8", newline=""))
                try:
                    header = tuple(next(reader))
                except StopIteration as error:
                    raise HistoricalUniverseError("monthly archive is empty") from error
                if header != KLINE_1D_HEADER:
                    raise HistoricalUniverseError("monthly archive header does not match 1d schema")
                bars: list[HistoricalDailyBar] = []
                seen: set[date] = set()
                previous: date | None = None
                for row in reader:
                    if len(row) != len(KLINE_1D_HEADER):
                        raise HistoricalUniverseError("monthly archive row width is invalid")
                    try:
                        open_time = int(row[0])
                        close_time = int(row[6])
                        open_price = float(row[1])
                        high_price = float(row[2])
                        low_price = float(row[3])
                        close_price = float(row[4])
                        base_volume = float(row[5])
                        quote_volume = float(row[7])
                        trade_count = int(row[8])
                        candle_day = datetime.fromtimestamp(open_time / 1000, tz=timezone.utc).date()
                    except (OverflowError, ValueError, OSError) as error:
                        raise HistoricalUniverseError("monthly archive row has invalid time or volume") from error
                    if candle_day.year != year or candle_day.month != number:
                        raise HistoricalUniverseError("monthly archive contains a day outside its named month")
                    if close_time != open_time + 86_400_000 - 1:
                        raise HistoricalUniverseError("daily candle is not a complete UTC day")
                    prices = (open_price, high_price, low_price, close_price)
                    if (
                        any(not math.isfinite(value) or value <= 0 for value in prices)
                        or high_price < max(open_price, close_price)
                        or low_price > min(open_price, close_price)
                        or low_price > high_price
                    ):
                        raise HistoricalUniverseError("monthly archive OHLC prices are invalid")
                    if (
                        not math.isfinite(base_volume)
                        or not math.isfinite(quote_volume)
                        or base_volume < 0
                        or quote_volume < 0
                        or trade_count < 0
                    ):
                        raise HistoricalUniverseError("monthly archive volume or count is invalid")
                    if candle_day in seen or (previous is not None and candle_day <= previous):
                        raise HistoricalUniverseError("monthly archive days are duplicated or unordered")
                    seen.add(candle_day)
                    previous = candle_day
                    bars.append(
                        HistoricalDailyBar(
                            symbol, candle_day, open_time, close_time, quote_volume,
                            open_price, high_price, low_price, close_price, base_volume, trade_count,
                        )
                    )
    except BadZipFile as error:
        raise HistoricalUniverseError("monthly archive is not a valid zip") from error
    if not bars:
        raise HistoricalUniverseError("monthly archive has no 1d candles")
    return bars


def build_historical_daily_universe(
    bars: Iterable[HistoricalDailyBar],
    *,
    catalog_sha256: str,
    min_listing_age_days: int = 30,
    min_continuous_days: int = 30,
    min_quote_volume: float = 10_000_000.0,
) -> dict:
    """Build a historical, point-in-time *diagnostic* membership record.

    A symbol is selected only using its bar on the decision date and bars at
    or before that date.  Missing historic order-book fields prevent this
    artifact from claiming equivalence to the live universe protocol.
    """
    if len(catalog_sha256) != 64:
        raise HistoricalUniverseError("catalogue digest must be a SHA-256 hex string")
    if min_listing_age_days < 1 or min_continuous_days < 1 or min_quote_volume <= 0:
        raise HistoricalUniverseError("historical universe policy is invalid")
    indexed: dict[str, list[HistoricalDailyBar]] = {}
    for bar in bars:
        if not isinstance(bar, HistoricalDailyBar):
            raise HistoricalUniverseError("historical daily bar type is invalid")
        indexed.setdefault(bar.symbol, []).append(bar)
    for symbol, rows in indexed.items():
        rows.sort(key=lambda item: item.day)
        if len({item.day for item in rows}) != len(rows):
            raise HistoricalUniverseError(f"duplicate day for {symbol}")
    all_days = sorted({item.day for rows in indexed.values() for item in rows})
    memberships: dict[str, dict] = {}
    for decision_day in all_days:
        selected: list[str] = []
        eligible_observed = 0
        for symbol, rows in sorted(indexed.items()):
            prior = [item for item in rows if item.day <= decision_day]
            if not prior or prior[-1].day != decision_day:
                continue
            eligible_observed += 1
            first_day = prior[0].day
            continuous = len(prior) >= min_continuous_days and all(
                (prior[-1].day - prior[-(offset + 1)].day).days == offset
                for offset in range(1, min_continuous_days)
            )
            age_ok = (decision_day - first_day).days >= min_listing_age_days
            if age_ok and continuous and prior[-1].quote_volume >= min_quote_volume:
                selected.append(symbol)
        memberships[decision_day.isoformat()] = {
            "selected": selected,
            "observed_contract_count": eligible_observed,
        }
    body = {
        "schema_version": 1,
        "status": "HISTORICAL_DAY_MEMBERSHIP_RECONSTRUCTED_DIAGNOSTIC",
        "source_catalog_sha256": catalog_sha256,
        "policy": {
            "min_listing_age_days": min_listing_age_days,
            "min_continuous_days": min_continuous_days,
            "min_quote_volume_1d": min_quote_volume,
            "decision_timestamp": "daily candle close; no later candle is consulted",
        },
        "membership": memberships,
        "limitations": [
            "archive candles do not prove historical exchange contract status",
            "archive candles do not contain historical spread or orderbook depth",
            "incomplete acquisition coverage must not be treated as a complete market universe",
            "diagnostic reconstruction is not live-trading or promotion evidence",
        ],
        "promotion_capability": "DIAGNOSTIC_ONLY",
        "live_order_authorization": "CLOSED",
    }
    digest = sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {**body, "universe_sha256": digest}


def verify_historical_daily_universe(artifact: Mapping[str, object]) -> dict:
    """Verify a sealed diagnostic universe before exposing any prices.

    This is intentionally stricter than a normal JSON loader: partial
    acquisitions cannot create this artifact, and its digest covers every
    decision-day membership list.
    """
    if not isinstance(artifact, Mapping):
        raise HistoricalUniverseError("historical daily universe must be an object")
    expected = artifact.get("universe_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        raise HistoricalUniverseError("historical daily universe lacks a digest")
    body = {key: value for key, value in artifact.items() if key != "universe_sha256"}
    actual = sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    if actual != expected:
        raise HistoricalUniverseError("historical daily universe digest mismatch")
    if artifact.get("status") != "HISTORICAL_DAY_MEMBERSHIP_RECONSTRUCTED_DIAGNOSTIC":
        raise HistoricalUniverseError("historical daily universe is not a completed diagnostic artifact")
    if artifact.get("promotion_capability") != "DIAGNOSTIC_ONLY" or artifact.get("live_order_authorization") != "CLOSED":
        raise HistoricalUniverseError("historical daily universe promotion boundary is unsafe")
    membership = artifact.get("membership")
    if not isinstance(membership, Mapping) or not membership:
        raise HistoricalUniverseError("historical daily universe membership is empty")
    parsed_days: list[date] = []
    selected_rows = 0
    for raw_day, entry in membership.items():
        if not isinstance(raw_day, str) or not isinstance(entry, Mapping):
            raise HistoricalUniverseError("historical daily universe membership row is invalid")
        try:
            parsed_days.append(date.fromisoformat(raw_day))
        except ValueError as error:
            raise HistoricalUniverseError("historical daily universe day is invalid") from error
        symbols = entry.get("selected")
        if not isinstance(symbols, list) or symbols != sorted(set(symbols)):
            raise HistoricalUniverseError("historical daily universe selected symbols are not ordered unique")
        if not all(isinstance(item, str) and item.isalnum() and item.endswith("USDT") for item in symbols):
            raise HistoricalUniverseError("historical daily universe symbol is invalid")
        selected_rows += len(symbols)
    if parsed_days != sorted(parsed_days) or len(set(parsed_days)) != len(parsed_days):
        raise HistoricalUniverseError("historical daily universe days are not ordered unique")
    return {"days": len(parsed_days), "selected_symbol_days": selected_rows, "universe_sha256": expected}


def load_historical_daily_price_panel(
    artifact: Mapping[str, object],
    *,
    archive_root: Path,
):
    """Load selected OHLC rows from checksum-bound monthly archives only.

    The returned frame has one row for each selected symbol on each decision
    day.  It is diagnostic-only: a caller must still use the *next* daily
    open for entry, as required by the separately frozen cost policy.
    """
    verify_historical_daily_universe(artifact)
    membership = artifact["membership"]
    assert isinstance(membership, Mapping)  # established by verifier
    requests: dict[tuple[str, str], set[date]] = {}
    for raw_day, entry in membership.items():
        decision_day = date.fromisoformat(str(raw_day))
        assert isinstance(entry, Mapping)
        for symbol in entry["selected"]:
            requests.setdefault((str(symbol), month_key(decision_day)), set()).add(decision_day)
    records: list[dict] = []
    for (symbol, month), requested_days in sorted(requests.items()):
        spec = monthly_1d_archive_spec(symbol, date.fromisoformat(f"{month}-01"))
        path = archive_root / symbol / month / spec["filename"]
        checksum_path = path.with_suffix(path.suffix + ".CHECKSUM")
        if not path.is_file() or not checksum_path.is_file():
            raise HistoricalUniverseError(f"selected symbol-day archive is missing: {symbol} {month}")
        archive = path.read_bytes()
        expected = parse_publisher_checksum(checksum_path.read_bytes(), expected_filename=spec["filename"])
        if sha256(archive).hexdigest() != expected:
            raise HistoricalUniverseError(f"selected symbol-day archive checksum mismatch: {symbol} {month}")
        bars = validate_monthly_1d_archive(archive, symbol=symbol, month=month)
        indexed = {bar.day: bar for bar in bars}
        missing = sorted(requested_days - set(indexed))
        if missing:
            raise HistoricalUniverseError(f"selected symbol has missing decision days: {symbol} {missing[0]}")
        for decision_day in sorted(requested_days):
            bar = indexed[decision_day]
            records.append({
                "decision_day": decision_day.isoformat(), "symbol": symbol,
                "open_time_ms": bar.open_time_ms, "close_time_ms": bar.close_time_ms,
                "open": bar.open_price, "high": bar.high_price, "low": bar.low_price,
                "close": bar.close_price, "base_volume": bar.base_volume,
                "quote_volume": bar.quote_volume, "trade_count": bar.trade_count,
            })
    records.sort(key=lambda item: (item["decision_day"], item["symbol"]))
    try:
        import pandas as pd
    except ImportError as error:  # pragma: no cover - runtime dependency is declared for research
        raise HistoricalUniverseError("pandas is required to load the historical price panel") from error
    panel = pd.DataFrame.from_records(records)
    if panel.empty:
        raise HistoricalUniverseError("historical price panel has no selected rows")
    if panel.duplicated(["decision_day", "symbol"]).any():
        raise HistoricalUniverseError("historical price panel has duplicate symbol-days")
    return panel
