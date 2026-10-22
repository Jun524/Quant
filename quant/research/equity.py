"""Strict ingestion of daily mark-to-market account-equity evidence."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
import json
from math import isfinite, prod
from pathlib import Path
from statistics import median


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("equity timestamps must include an explicit UTC offset")
    parsed = parsed.astimezone(timezone.utc)
    if parsed.time() != datetime.min.time():
        raise ValueError("equity snapshots must be taken at 00:00:00 UTC")
    return parsed


def _read_rows(path: Path) -> list[dict]:
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    location = (len(ordered) - 1) * probability
    lower = int(location)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (location - lower)


def _daily_metrics(points: list[tuple[datetime, float]]) -> dict:
    returns = [current / previous - 1 for (_, previous), (_, current) in zip(points, points[1:])]
    dates = [timestamp.date().isoformat() for timestamp, _ in points[1:]]
    ordered = sorted(returns)
    losing_streak = current_streak = 0
    for value in returns:
        current_streak = current_streak + 1 if value < 0 else 0
        losing_streak = max(losing_streak, current_streak)
    return {
        "basis": "daily 00:00 UTC mark-to-market account equity",
        "days": len(returns),
        "mean_daily_return": sum(returns) / len(returns),
        "median_daily_return": median(returns),
        "geometric_daily_return": prod(1 + value for value in returns) ** (1 / len(returns)) - 1,
        "positive_day_ratio": sum(value > 0 for value in returns) / len(returns),
        "negative_day_ratio": sum(value < 0 for value in returns) / len(returns),
        "best_daily_return": max(returns),
        "worst_daily_return": min(returns),
        "historical_var_95": max(0.0, -ordered[max(0, int((len(ordered) - 1) * 0.05))]),
        "historical_var_99": max(0.0, -ordered[max(0, int((len(ordered) - 1) * 0.01))]),
        "max_consecutive_losing_days": losing_streak,
        "series": [
            {"date": observed, "return": value, "profit": None}
            for observed, value in zip(dates, returns)
        ],
    }


def _monthly_metrics(points: list[tuple[datetime, float]], target: float) -> dict:
    by_date = {timestamp.date(): equity for timestamp, equity in points}
    series: list[dict] = []
    for observed, start_equity in by_date.items():
        if observed.day != 1:
            continue
        if observed.month == 12:
            following = date(observed.year + 1, 1, 1)
        else:
            following = date(observed.year, observed.month + 1, 1)
        if following not in by_date:
            continue
        value = by_date[following] / start_equity - 1
        series.append(
            {
                "month": observed.strftime("%Y-%m"),
                "return": value,
                "profit": by_date[following] - start_equity,
                "start_equity": start_equity,
                "complete": True,
            }
        )
    values = [float(item["return"]) for item in series]
    if not values:
        return {"basis": "mark-to-market month boundaries; no complete months", "complete_months": 0, "series": []}
    losing_streak = current_streak = 0
    for value in values:
        current_streak = current_streak + 1 if value < 0 else 0
        losing_streak = max(losing_streak, current_streak)
    return {
        "basis": "complete calendar months between 00:00 UTC mark-to-market snapshots",
        "complete_months": len(values),
        "geometric_monthly_return": prod(1 + value for value in values) ** (1 / len(values)) - 1,
        "arithmetic_mean_monthly_return": sum(values) / len(values),
        "median_monthly_return": median(values),
        "positive_month_ratio": sum(value > 0 for value in values) / len(values),
        "target_month_ratio": sum(value >= target for value in values) / len(values),
        "target": target,
        "best_month": max(values),
        "worst_month": min(values),
        "p10_monthly_return": _quantile(values, 0.10),
        "max_consecutive_losing_months": losing_streak,
        "series": series,
    }


def load_mark_to_market_equity(
    path: Path,
    *,
    expected_start: str | None = None,
    expected_end: str | None = None,
    target: float = 0.10,
) -> dict:
    """Load an unbroken UTC-midnight equity curve aligned to one test run.

    ``equity`` must mean wallet balance plus unrealized PnL after fees and
    funding. Requiring the literal ``equity_type=mark_to_market`` makes that
    definition an explicit piece of evidence rather than an assumption.
    """
    rows = _read_rows(path)
    if len(rows) < 2:
        raise ValueError("equity curve needs at least two daily boundary snapshots")
    points: list[tuple[datetime, float]] = []
    for row in rows:
        if str(row.get("equity_type") or "").strip().lower() != "mark_to_market":
            raise ValueError("every equity row must declare equity_type=mark_to_market")
        timestamp = _parse_timestamp(str(row.get("timestamp") or ""))
        equity = float(row.get("equity") or 0.0)
        if not isfinite(equity) or equity <= 0:
            raise ValueError("equity must be finite and strictly positive")
        points.append((timestamp, equity))
    for previous, current in zip(points, points[1:]):
        if current[0] <= previous[0]:
            raise ValueError("equity timestamps must be strictly increasing and unique")
        if current[0] - previous[0] != timedelta(days=1):
            raise ValueError("equity curve must contain every consecutive UTC day")
    first, last = points[0][0].date(), points[-1][0].date()
    if expected_start and first != date.fromisoformat(str(expected_start)[:10]):
        raise ValueError(f"equity start {first} does not match result start {str(expected_start)[:10]}")
    if expected_end and last != date.fromisoformat(str(expected_end)[:10]):
        raise ValueError(f"equity end {last} does not match result end {str(expected_end)[:10]}")
    peak = points[0][1]
    maximum_drawdown = 0.0
    for _, equity in points:
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, 1 - equity / peak)
    return {
        "source": str(path),
        "equity_basis": "mark_to_market",
        "definition": "account equity including unrealized PnL after fees and funding",
        "coverage_start": first.isoformat(),
        "coverage_end": last.isoformat(),
        "snapshots": len(points),
        "initial_equity": points[0][1],
        "final_equity": points[-1][1],
        "daily": _daily_metrics(points),
        "monthly": _monthly_metrics(points, target),
        "max_drawdown": maximum_drawdown,
    }


def apply_mark_to_market_equity(base: dict, curve: dict) -> dict:
    """Return result metrics with risk/return distributions replaced by MTM."""
    updated = dict(base)
    updated.update(
        {
            "daily": curve["daily"],
            "monthly": curve["monthly"],
            "max_drawdown": curve["max_drawdown"],
            "equity_basis": curve["equity_basis"],
            "equity_evidence": {
                key: value for key, value in curve.items() if key not in {"daily", "monthly", "max_drawdown"}
            },
        }
    )
    return updated
