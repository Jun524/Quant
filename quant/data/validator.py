"""OHLCV integrity validation independent of a particular storage format."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Iterable, Mapping


@dataclass(frozen=True)
class DataIssue:
    code: str
    row: int
    detail: str
    severity: str = "error"


@dataclass(frozen=True)
class ValidationReport:
    rows: int
    issues: tuple[DataIssue, ...]

    @property
    def passed(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> dict:
        return {"rows": self.rows, "passed": self.passed, "issues": [asdict(x) for x in self.issues]}


def validate_ohlcv_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    interval_ms: int | None = None,
    max_price_jump: float = 0.50,
) -> ValidationReport:
    """Validate normalized OHLCV rows and return all discovered problems.

    Rows must expose ``timestamp, open, high, low, close, volume``. Timestamps
    are UTC epoch milliseconds. A missing candle is an error only when an
    expected interval is supplied; large price jumps are warnings because they
    may be real market events that require review rather than silent deletion.
    """
    if interval_ms is not None and interval_ms <= 0:
        raise ValueError("interval_ms must be positive")
    if not 0 < max_price_jump < 100:
        raise ValueError("max_price_jump must be between 0 and 100")

    issues: list[DataIssue] = []
    previous_timestamp: int | None = None
    previous_close: float | None = None
    count = 0
    required = ("timestamp", "open", "high", "low", "close", "volume")

    for row_index, row in enumerate(rows):
        count += 1
        missing = [key for key in required if key not in row]
        if missing:
            issues.append(DataIssue("missing_column", row_index, ", ".join(missing)))
            continue
        try:
            timestamp = int(row["timestamp"])
            opening, high, low, close, volume = (float(row[key]) for key in required[1:])
        except (TypeError, ValueError):
            issues.append(DataIssue("invalid_value", row_index, "timestamp/OHLCV must be numeric"))
            continue

        if not all(isfinite(value) for value in (opening, high, low, close, volume)):
            issues.append(DataIssue("non_finite_value", row_index, "OHLCV contains NaN or infinity"))
        if min(opening, high, low, close) <= 0:
            issues.append(DataIssue("invalid_price", row_index, "prices must be positive"))
        if high < max(opening, close) or low > min(opening, close) or high < low:
            issues.append(DataIssue("invalid_ohlc", row_index, "OHLC relationship is invalid"))
        if volume <= 0:
            issues.append(DataIssue("zero_volume", row_index, "volume must be positive"))

        if previous_timestamp is not None:
            if timestamp == previous_timestamp:
                issues.append(DataIssue("duplicate_timestamp", row_index, str(timestamp)))
            elif timestamp < previous_timestamp:
                issues.append(DataIssue("out_of_order_timestamp", row_index, str(timestamp)))
            elif interval_ms is not None and timestamp - previous_timestamp != interval_ms:
                issues.append(
                    DataIssue("timestamp_gap", row_index, f"expected {interval_ms}ms, got {timestamp - previous_timestamp}ms")
                )
        if previous_close and previous_close > 0:
            jump = abs(close / previous_close - 1)
            if jump > max_price_jump:
                issues.append(DataIssue("extreme_price_jump", row_index, f"{jump:.2%}", severity="warning"))

        previous_timestamp = timestamp
        previous_close = close

    return ValidationReport(rows=count, issues=tuple(issues))
