"""Causal evaluation of the preregistered liquidation-risk overlay."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Iterable


def _series(rows: Iterable[dict], value_key: str) -> dict[str, tuple[list[int], list[float]]]:
    grouped: dict[str, dict[int, float]] = defaultdict(dict)
    for row in rows:
        grouped[str(row["symbol"]).upper()][int(row["event_time_ms"])] = float(row[value_key])
    result = {}
    for symbol, values in grouped.items():
        ordered = sorted(values.items())
        result[symbol] = ([item[0] for item in ordered], [item[1] for item in ordered])
    return result


def _asof(series: tuple[list[int], list[float]] | None, timestamp: int, *, max_age_ms: int | None = None) -> float | None:
    if not series:
        return None
    times, values = series
    index = bisect_right(times, timestamp) - 1
    if index < 0 or (max_age_ms is not None and timestamp - times[index] > max_age_ms):
        return None
    return values[index]


def _maximum_absolute_excursion(
    series: tuple[list[int], list[float]] | None,
    start_ms: int,
    horizon_ms: int,
) -> float | None:
    start_price = _asof(series, start_ms, max_age_ms=2_000)
    if start_price is None or not series:
        return None
    times, values = series
    begin = bisect_left(times, start_ms)
    end = bisect_right(times, start_ms + horizon_ms)
    if begin >= end or times[end - 1] < start_ms + horizon_ms - 2_000:
        return None
    return max(abs(value / start_price - 1.0) for value in values[begin:end])


def qualifying_triggers(
    liquidations: Iterable[dict],
    open_interest: Iterable[dict],
    marks: Iterable[dict],
    *,
    lookback_seconds: int,
    minimum_ratio: float,
    minimum_mark_move: float,
) -> list[dict]:
    """Calculate triggers using only exchange timestamps at or before each event."""

    mark_series = _series(marks, "mark_price")
    oi_series = _series(open_interest, "open_interest_base_qty")
    windows: dict[tuple[str, str], deque[tuple[int, float]]] = defaultdict(deque)
    sums: dict[tuple[str, str], float] = defaultdict(float)
    triggers: list[dict] = []
    lookback_ms = lookback_seconds * 1_000
    for row in sorted(liquidations, key=lambda item: (int(item["event_time_ms"]), str(item["symbol"]))):
        timestamp = int(row["event_time_ms"])
        symbol = str(row["symbol"]).upper()
        side = str(row["side"]).upper()
        key = (symbol, side)
        queue = windows[key]
        notional = float(row["observed_snapshot_notional"])
        queue.append((timestamp, notional))
        sums[key] += notional
        while queue and queue[0][0] < timestamp - lookback_ms:
            _, expired = queue.popleft()
            sums[key] -= expired
        current = _asof(mark_series.get(symbol), timestamp, max_age_ms=2_000)
        prior = _asof(mark_series.get(symbol), timestamp - lookback_ms, max_age_ms=2_000)
        oi_qty = _asof(oi_series.get(symbol), timestamp, max_age_ms=600_000)
        if current is None or prior is None or oi_qty is None or oi_qty <= 0:
            continue
        mark_return = current / prior - 1.0
        ratio = sums[key] / (oi_qty * current)
        direction_confirmed = (
            side == "SELL" and mark_return <= -minimum_mark_move
        ) or (side == "BUY" and mark_return >= minimum_mark_move)
        if ratio >= minimum_ratio and direction_confirmed:
            triggers.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "event_time_ms": timestamp,
                    "observed_snapshot_notional_60s": sums[key],
                    "open_interest_notional": oi_qty * current,
                    "snapshot_to_open_interest_ratio": ratio,
                    "mark_return_60s": mark_return,
                }
            )
    return triggers


def collapse_episodes(triggers: Iterable[dict], *, freeze_minutes: int) -> list[dict]:
    freeze_ms = freeze_minutes * 60_000
    active: dict[str, dict] = {}
    episodes: list[dict] = []
    for trigger in sorted(triggers, key=lambda item: (int(item["event_time_ms"]), str(item["symbol"]))):
        symbol = str(trigger["symbol"])
        timestamp = int(trigger["event_time_ms"])
        current = active.get(symbol)
        if current is not None and timestamp <= int(current["freeze_end_ms"]):
            current["freeze_end_ms"] = max(int(current["freeze_end_ms"]), timestamp + freeze_ms)
            current["qualifying_trigger_count"] += 1
            continue
        episode = {
            **trigger,
            "episode_start_ms": timestamp,
            "freeze_end_ms": timestamp + freeze_ms,
            "qualifying_trigger_count": 1,
        }
        active[symbol] = episode
        episodes.append(episode)
    return episodes


def _inside_freeze(timestamp: int, symbol: str, episodes: list[dict]) -> bool:
    return any(
        item["symbol"] == symbol
        and int(item["episode_start_ms"]) <= timestamp <= int(item["freeze_end_ms"])
        for item in episodes
    )


def evaluate_overlay(
    liquidations: list[dict],
    open_interest: list[dict],
    marks: list[dict],
    *,
    policy: dict,
    complete_days: set[date],
) -> dict:
    trigger_policy = policy["fixed_trigger_for_later_evaluation"]
    triggers = qualifying_triggers(
        liquidations,
        open_interest,
        marks,
        lookback_seconds=int(trigger_policy["lookback_seconds"]),
        minimum_ratio=float(trigger_policy["minimum_observed_snapshot_notional_to_open_interest_notional"]),
        minimum_mark_move=float(trigger_policy["minimum_same_direction_mark_return_abs"]),
    )
    episodes = collapse_episodes(triggers, freeze_minutes=int(trigger_policy["entry_freeze_minutes"]))
    mark_series = _series(marks, "mark_price")
    horizon_ms = int(trigger_policy["entry_freeze_minutes"]) * 60_000
    matched: list[dict] = []
    for episode in episodes:
        timestamp = int(episode["episode_start_ms"])
        symbol = str(episode["symbol"])
        triggered_outcome = _maximum_absolute_excursion(mark_series.get(symbol), timestamp, horizon_ms)
        if triggered_outcome is None:
            continue
        baseline_time = baseline_outcome = None
        for weeks in range(1, 5):
            candidate = timestamp - weeks * 7 * 86_400_000
            candidate_day = datetime.fromtimestamp(candidate / 1_000, timezone.utc).date()
            if candidate_day not in complete_days or _inside_freeze(candidate, symbol, episodes):
                continue
            outcome = _maximum_absolute_excursion(mark_series.get(symbol), candidate, horizon_ms)
            if outcome is not None:
                baseline_time = candidate
                baseline_outcome = outcome
                break
        if baseline_outcome is None:
            continue
        matched.append(
            {
                **episode,
                "triggered_max_abs_excursion_30m": triggered_outcome,
                "baseline_time_ms": baseline_time,
                "baseline_max_abs_excursion_30m": baseline_outcome,
            }
        )

    triggered_values = [float(item["triggered_max_abs_excursion_30m"]) for item in matched]
    baseline_values = [float(item["baseline_max_abs_excursion_30m"]) for item in matched]
    triggered_median = median(triggered_values) if triggered_values else None
    baseline_median = median(baseline_values) if baseline_values else None
    median_multiple = (
        triggered_median / baseline_median
        if triggered_median is not None and baseline_median not in (None, 0)
        else None
    )
    triggered_one_pct = (
        sum(value >= 0.01 for value in triggered_values) / len(triggered_values)
        if triggered_values else None
    )
    baseline_one_pct = (
        sum(value >= 0.01 for value in baseline_values) / len(baseline_values)
        if baseline_values else None
    )
    one_pct_difference = (
        triggered_one_pct - baseline_one_pct
        if triggered_one_pct is not None and baseline_one_pct is not None
        else None
    )
    midpoint = len(matched) // 2
    halves = [matched[:midpoint], matched[midpoint:]] if matched else [[], []]
    half_higher = []
    for half in halves:
        if not half:
            half_higher.append(False)
            continue
        half_higher.append(
            median([item["triggered_max_abs_excursion_30m"] for item in half])
            > median([item["baseline_max_abs_excursion_30m"] for item in half])
        )
    symbol_counts = defaultdict(int)
    for item in matched:
        symbol_counts[str(item["symbol"])] += 1
    concentration = max(symbol_counts.values(), default=0) / len(matched) if matched else None
    gate = policy["development_gate"]
    checks = [
        {"name": "minimum_nonoverlapping_triggers", "passed": len(matched) >= int(gate["minimum_nonoverlapping_triggers"]), "actual": len(matched)},
        {"name": "median_tail_multiple", "passed": median_multiple is not None and median_multiple >= float(gate["triggered_30m_absolute_return_median_at_least_matched_baseline_multiple"]), "actual": median_multiple},
        {"name": "one_percent_tail_rate_difference", "passed": one_pct_difference is not None and one_pct_difference >= float(gate["triggered_30m_one_percent_adverse_move_rate_minus_matched_baseline_minimum"]), "actual": one_pct_difference},
        {"name": "both_halves_higher_tail_risk", "passed": all(half_higher), "actual": half_higher},
        {"name": "single_symbol_concentration", "passed": concentration is not None and concentration <= float(gate["single_symbol_trigger_concentration_maximum"]), "actual": concentration},
    ]
    return {
        "status": "PASS" if all(item["passed"] for item in checks) else "FAIL",
        "qualifying_trigger_rows": len(triggers),
        "nonoverlapping_episodes": len(episodes),
        "matched_episodes": len(matched),
        "triggered_median_max_abs_excursion_30m": triggered_median,
        "baseline_median_max_abs_excursion_30m": baseline_median,
        "median_tail_multiple": median_multiple,
        "triggered_one_percent_tail_rate": triggered_one_pct,
        "baseline_one_percent_tail_rate": baseline_one_pct,
        "one_percent_tail_rate_difference": one_pct_difference,
        "single_symbol_concentration": concentration,
        "matched_symbol_counts": dict(sorted(symbol_counts.items())),
        "checks": checks,
        "episodes": matched,
    }
