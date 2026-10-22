"""Audit whether a sealed daily universe has next-open execution coverage.

This audit deliberately measures *data availability*, not strategy returns.
Every historical daily candidate must observe a fully closed decision candle
and enter no earlier than the following UTC daily open.  A missing next-day
candle therefore rejects that symbol-day from a future trial rather than
silently fabricating an execution price.
"""

from __future__ import annotations

from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from quant.data.historical_universe import (
    HistoricalUniverseError,
    month_key,
    monthly_1d_archive_spec,
    parse_publisher_checksum,
    validate_monthly_1d_archive,
    verify_historical_daily_universe,
)


class HistoricalExecutionCoverageError(ValueError):
    """The coverage inputs cannot prove next-open availability."""


def _periods(policy: Mapping[str, Any]) -> list[tuple[str, date, date]]:
    calendar = policy.get("calendar")
    if not isinstance(calendar, Mapping):
        raise HistoricalExecutionCoverageError("historical universe policy lacks a calendar")
    keys = (
        ("development", "development_start", "development_end"),
        ("locked_historical_validation", "locked_historical_validation_start", "locked_historical_validation_end"),
    )
    periods: list[tuple[str, date, date]] = []
    for label, start_key, end_key in keys:
        try:
            start, end = date.fromisoformat(str(calendar[start_key])), date.fromisoformat(str(calendar[end_key]))
        except (KeyError, ValueError) as error:
            raise HistoricalExecutionCoverageError(f"invalid {label} calendar") from error
        if start > end:
            raise HistoricalExecutionCoverageError(f"invalid {label} range")
        periods.append((label, start, end))
    return periods


def _bars_for_month(archive_root: Path, symbol: str, value: date):
    spec = monthly_1d_archive_spec(symbol, value)
    path = archive_root / symbol / spec["month"] / spec["filename"]
    checksum_path = path.with_suffix(path.suffix + ".CHECKSUM")
    if not path.is_file() or not checksum_path.is_file():
        raise HistoricalExecutionCoverageError(f"execution archive is missing: {symbol} {spec['month']}")
    payload = path.read_bytes()
    expected = parse_publisher_checksum(checksum_path.read_bytes(), expected_filename=spec["filename"])
    if sha256(payload).hexdigest() != expected:
        raise HistoricalExecutionCoverageError(f"execution archive checksum mismatch: {symbol} {spec['month']}")
    return {bar.day: bar for bar in validate_monthly_1d_archive(payload, symbol=symbol, month=spec["month"])}


def audit_historical_next_open_coverage(
    artifact: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    archive_root: Path,
) -> dict[str, Any]:
    """Return a source-bound coverage receipt for each fixed evaluation period."""
    receipt = verify_historical_daily_universe(artifact)
    membership = artifact["membership"]
    assert isinstance(membership, Mapping)
    cache: dict[tuple[str, str], dict[date, Any]] = {}

    def has_next_open(symbol: str, decision_day: date) -> bool:
        execution_day = decision_day + timedelta(days=1)
        key = (symbol, month_key(execution_day))
        if key not in cache:
            cache[key] = _bars_for_month(archive_root, symbol, execution_day)
        bar = cache[key].get(execution_day)
        return bar is not None and bar.open_price > 0

    reports: list[dict[str, Any]] = []
    all_rows = 0
    all_covered = 0
    for label, start, end in _periods(policy):
        required = 0
        covered = 0
        missing: list[dict[str, str]] = []
        for raw_day, entry in membership.items():
            decision_day = date.fromisoformat(str(raw_day))
            if not start <= decision_day <= end:
                continue
            assert isinstance(entry, Mapping)
            selected = entry["selected"]
            assert isinstance(selected, list)
            for symbol in selected:
                required += 1
                if has_next_open(str(symbol), decision_day):
                    covered += 1
                elif len(missing) < 20:
                    missing.append({"decision_day": decision_day.isoformat(), "symbol": str(symbol)})
        all_rows += required
        all_covered += covered
        reports.append({
            "period": label,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "selected_symbol_days": required,
            "next_open_covered_symbol_days": covered,
            "missing_next_open_symbol_days": required - covered,
            "coverage_ratio": covered / required if required else None,
            "sample_missing": missing,
            "status": "PASS" if required > 0 and covered == required else "FAIL",
        })
    return {
        "schema_version": 1,
        "status": "PASS" if all(item["status"] == "PASS" for item in reports) else "FAIL",
        "kind": "HISTORICAL_NEXT_OPEN_DATA_COVERAGE_ONLY",
        "universe_sha256": receipt["universe_sha256"],
        "periods": reports,
        "total_selected_symbol_days": all_rows,
        "total_next_open_covered_symbol_days": all_covered,
        "coverage_ratio": all_covered / all_rows if all_rows else None,
        "funding_coverage": "NOT_EVALUATED; a future trial must obtain checksum-verified funding archives or reject its affected holding days",
        "promotion_capability": "DIAGNOSTIC_ONLY",
        "live_order_authorization": "CLOSED",
        "warning": "This receipt proves archive availability for next-open simulation only; it contains no strategy signal or return claim.",
    }
