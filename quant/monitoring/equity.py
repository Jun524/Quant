"""Causal conversion of frequent account-equity observations to UTC days."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observation timestamps must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def select_causal_daily_boundaries(observations: list[dict], *, maximum_age_seconds: int = 120) -> list[dict]:
    """Select the last observation at or before each UTC midnight.

    The selected value can never see past the boundary. Missing/stale boundary
    evidence fails closed rather than being interpolated.
    """
    if maximum_age_seconds < 1:
        raise ValueError("maximum_age_seconds must be positive")
    parsed = sorted(
        [(_timestamp(str(item["observed_at"])), float(item["equity"])) for item in observations],
        key=lambda item: item[0],
    )
    if not parsed:
        return []
    for (previous, _), (current, _) in zip(parsed, parsed[1:]):
        if current <= previous:
            raise ValueError("observation timestamps must be unique")

    first = parsed[0][0]
    boundary = datetime.combine(first.date(), time.min, tzinfo=timezone.utc)
    if first > boundary:
        boundary += timedelta(days=1)
    final_boundary = datetime.combine(parsed[-1][0].date(), time.min, tzinfo=timezone.utc)
    result: list[dict] = []
    index = -1
    while boundary <= final_boundary:
        while index + 1 < len(parsed) and parsed[index + 1][0] <= boundary:
            index += 1
        if index < 0:
            raise ValueError(f"no causal equity observation for {boundary.isoformat()}")
        observed_at, equity = parsed[index]
        age = (boundary - observed_at).total_seconds()
        if age > maximum_age_seconds:
            raise ValueError(
                f"stale equity observation at {boundary.isoformat()}: {age:.0f}s > {maximum_age_seconds}s"
            )
        result.append(
            {
                "timestamp": boundary.isoformat().replace("+00:00", "Z"),
                "equity": equity,
                "equity_type": "mark_to_market",
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "boundary_lag_seconds": age,
            }
        )
        boundary += timedelta(days=1)
    return result
