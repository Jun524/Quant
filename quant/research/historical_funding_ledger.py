"""Source-bound funding ledger for archival daily-futures trials.

Only completed, checksum-verified monthly funding archives are admissible.
The ledger deliberately has no zero-funding fallback: a missing symbol-month
or an unsealed manifest rejects the requested calculation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

from quant.research.alpha015_data_guard import (
    Alpha015DataError,
    monthly_funding_archive_spec,
    validate_monthly_funding_archive,
)


class HistoricalFundingLedgerError(ValueError):
    """The source manifest or a requested funding interval is unsafe."""


def _manifest_body(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        raise HistoricalFundingLedgerError("funding manifest must be an object")
    digest = manifest.get("manifest_sha256")
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    actual = sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    if not isinstance(digest, str) or digest != actual:
        raise HistoricalFundingLedgerError("funding manifest digest mismatch")
    if manifest.get("status") not in {"COMPLETE", "COMPLETE_WITH_MISSING_ARCHIVES"}:
        raise HistoricalFundingLedgerError("funding acquisition is not complete")
    if manifest.get("promotion_capability") != "DIAGNOSTIC_ONLY" or manifest.get("live_order_authorization") != "CLOSED":
        raise HistoricalFundingLedgerError("funding manifest has unsafe promotion fields")
    return body


def verify_historical_funding_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Verify a sealed acquisition manifest without loading a strategy or returns."""
    body = _manifest_body(manifest)
    records = body.get("records")
    missing = body.get("not_found")
    if not isinstance(records, list) or not isinstance(missing, list):
        raise HistoricalFundingLedgerError("funding manifest inventory is malformed")
    identities = [(str(item.get("symbol")), str(item.get("month"))) for item in records]
    absent = [(str(item.get("symbol")), str(item.get("month"))) for item in missing]
    if len(identities) != len(set(identities)) or len(absent) != len(set(absent)) or set(identities) & set(absent):
        raise HistoricalFundingLedgerError("funding manifest has duplicate or conflicting inventory")
    if not all(len(str(item.get("sha256") or "")) == 64 and int(item.get("events") or 0) > 0 for item in records):
        raise HistoricalFundingLedgerError("funding manifest record is incomplete")
    return {"universe_sha256": body.get("universe_sha256"), "records": len(records), "not_found": len(missing), "status": body.get("status")}


def _month_starts_between(entry_ms: int, exit_ms: int) -> list[datetime]:
    if entry_ms >= exit_ms:
        raise HistoricalFundingLedgerError("funding interval must have positive duration")
    entry = datetime.fromtimestamp(entry_ms / 1000, tz=timezone.utc)
    exit_exclusive = datetime.fromtimestamp((exit_ms - 1) / 1000, tz=timezone.utc)
    cursor = datetime(entry.year, entry.month, 1, tzinfo=timezone.utc)
    final = datetime(exit_exclusive.year, exit_exclusive.month, 1, tzinfo=timezone.utc)
    months: list[datetime] = []
    while cursor <= final:
        months.append(cursor)
        cursor = datetime(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1, tzinfo=timezone.utc)
    return months


def funding_return_for_interval(
    manifest: Mapping[str, Any],
    *,
    archive_root: Path,
    symbol: str,
    entry_time_ms: int,
    exit_time_ms: int,
    side: str,
) -> dict[str, Any]:
    """Compute signed funding return for one unit-notional perpetual position.

    Positive funding is paid by a long and received by a short.  Events at the
    entry timestamp are charged; events at the exit timestamp are excluded,
    matching the frozen historical cost-accounting contract ``[entry, exit)``.
    """
    body = _manifest_body(manifest)
    if side not in {"long", "short"}:
        raise HistoricalFundingLedgerError("side must be long or short")
    records = body.get("records")
    missing = body.get("not_found")
    if not isinstance(records, list) or not isinstance(missing, list):
        raise HistoricalFundingLedgerError("funding manifest inventory is malformed")
    index = {(str(item.get("symbol")), str(item.get("month"))): item for item in records}
    unavailable = {(str(item.get("symbol")), str(item.get("month"))) for item in missing}
    events: list[dict[str, Any]] = []
    for month_dt in _month_starts_between(entry_time_ms, exit_time_ms):
        month_day = month_dt.date()
        month = month_day.isoformat()
        key = (symbol, month)
        if key in unavailable or key not in index:
            raise HistoricalFundingLedgerError(f"funding archive is unavailable: {symbol} {month}")
        spec = monthly_funding_archive_spec(symbol, month_day)
        archive_path = archive_root / symbol / spec["month"] / spec["archive_name"]
        checksum_path = archive_path.with_suffix(archive_path.suffix + ".CHECKSUM")
        if not archive_path.is_file() or not checksum_path.is_file():
            raise HistoricalFundingLedgerError(f"funding archive file is missing: {symbol} {month}")
        try:
            verified = validate_monthly_funding_archive(
                archive_path.read_bytes(), checksum_path.read_bytes(), symbol=symbol, month=month_day
            )
        except (OSError, Alpha015DataError) as error:
            raise HistoricalFundingLedgerError(f"funding archive validation failed: {symbol} {month}") from error
        if verified["sha256"] != index[key].get("sha256"):
            raise HistoricalFundingLedgerError(f"funding archive does not match sealed manifest: {symbol} {month}")
        events.extend(event for event in verified["events"] if entry_time_ms <= int(event["fundingTime"]) < exit_time_ms)
    raw_rate_sum = sum(float(event["fundingRate"]) for event in events)
    funding_return = -raw_rate_sum if side == "long" else raw_rate_sum
    return {
        "symbol": symbol,
        "side": side,
        "entry_time_ms": entry_time_ms,
        "exit_time_ms": exit_time_ms,
        "events": events,
        "funding_rate_sum": raw_rate_sum,
        "funding_return": funding_return,
        "source": "checksum-verified Binance USD-M monthly fundingRate archives",
    }
