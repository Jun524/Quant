"""Strict, non-promotional diagnostics for cross-sectional alpha signals.

This module measures signal quality before a signal is converted into a
trading strategy.  It deliberately does not estimate fees, funding, fills, or
portfolio profitability and therefore cannot authorize promotion.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from math import floor, sqrt
from pathlib import Path
from typing import Any

import numpy as np


SIGNAL_DIAGNOSTIC_HEADER = [
    "decision_at",
    "forward_start_at",
    "forward_end_at",
    "symbol",
    "signal",
    "forward_return",
]
SIGNAL_DIAGNOSTIC_PROTOCOL_FILES = (
    "quant/research/signal_diagnostics.py",
    "scripts/signal_diagnostics_report.py",
)


@dataclass(frozen=True)
class SignalObservation:
    decision_at: datetime
    forward_start_at: datetime
    forward_end_at: datetime
    symbol: str
    signal: float
    forward_return: float


def _utc(value: str, *, field: str, line_number: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"row {line_number} {field} is not ISO-8601") from error
    if parsed.tzinfo is None:
        raise ValueError(f"row {line_number} {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def load_signal_observations(path: Path) -> list[SignalObservation]:
    """Load a complete ordered signal/forward-return panel.

    Rows must be ordered by decision time and then by symbol.  Every
    cross-section must use one common forward interval, and the interval must
    start strictly after the decision timestamp.  These checks establish the
    declared causal boundary; they cannot prove that the supplied return was
    independently reconstructed from raw prices.
    """

    observations: list[SignalObservation] = []
    seen: set[tuple[datetime, str]] = set()
    previous_key: tuple[datetime, str] | None = None
    group_intervals: dict[datetime, tuple[datetime, datetime]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != SIGNAL_DIAGNOSTIC_HEADER:
            raise ValueError("signal diagnostic CSV header does not match the locked schema")
        for line_number, row in enumerate(reader, start=2):
            decision_at = _utc(str(row["decision_at"]), field="decision_at", line_number=line_number)
            forward_start_at = _utc(
                str(row["forward_start_at"]),
                field="forward_start_at",
                line_number=line_number,
            )
            forward_end_at = _utc(
                str(row["forward_end_at"]),
                field="forward_end_at",
                line_number=line_number,
            )
            symbol = str(row["symbol"]).strip().upper()
            try:
                signal = float(row["signal"])
                forward_return = float(row["forward_return"])
            except (TypeError, ValueError) as error:
                raise ValueError(f"row {line_number} contains a non-numeric value") from error
            if (
                not symbol
                or not np.isfinite(signal)
                or not np.isfinite(forward_return)
                or not decision_at < forward_start_at <= forward_end_at
            ):
                raise ValueError(f"row {line_number} violates the causal signal schema")
            key = (decision_at, symbol)
            if key in seen:
                raise ValueError(f"row {line_number} duplicates a decision/symbol observation")
            if previous_key is not None and key <= previous_key:
                raise ValueError("rows must be ordered by decision_at and symbol")
            interval = (forward_start_at, forward_end_at)
            if decision_at in group_intervals and group_intervals[decision_at] != interval:
                raise ValueError("one decision cross-section contains mixed forward intervals")
            group_intervals[decision_at] = interval
            seen.add(key)
            previous_key = key
            observations.append(
                SignalObservation(
                    decision_at=decision_at,
                    forward_start_at=forward_start_at,
                    forward_end_at=forward_end_at,
                    symbol=symbol,
                    signal=signal,
                    forward_return=forward_return,
                )
            )
    if not observations:
        raise ValueError("signal diagnostic CSV is empty")
    return observations


def _average_ranks(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        ranks[order[start:end]] = (start + end - 1) / 2.0 + 1.0
        start = end
    return ranks


def _correlation(left: np.ndarray, right: np.ndarray) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_centered = left - left.mean()
    right_centered = right - right.mean()
    denominator = float(np.sqrt(np.sum(left_centered**2) * np.sum(right_centered**2)))
    if denominator <= 0:
        return None
    return float(np.sum(left_centered * right_centered) / denominator)


def _spearman(left: np.ndarray, right: np.ndarray) -> float | None:
    return _correlation(_average_ranks(left), _average_ranks(right))


def _overlap_aware_hac_lag(
    periods: list[tuple[datetime, datetime]],
) -> int:
    """Return a conservative Newey-West lag from declared label overlap.

    A decision's forward label can be dependent on an earlier decision only
    while their forward-return intervals overlap.  Rather than infer a lag
    from a user-controlled number, derive the maximum number of preceding
    decision intervals that overlap a current interval.  This is deliberately
    conservative for irregularly spaced decisions and is only a statistical
    diagnostic, never a promotion criterion.
    """

    maximum = 0
    for index, (current_start, _) in enumerate(periods):
        overlapping_predecessors = 0
        for _, previous_end in periods[:index]:
            if previous_end > current_start:
                overlapping_predecessors += 1
        maximum = max(maximum, overlapping_predecessors)
    return maximum


def _newey_west_t(values: np.ndarray, *, lag: int) -> float | None:
    """Mean t-statistic using Bartlett-kernel Newey-West long-run variance."""

    count = len(values)
    if count < 2 or lag < 1:
        return None
    centered = values - values.mean()
    gamma_zero = float(np.dot(centered, centered) / count)
    long_run_variance = gamma_zero
    for offset in range(1, min(lag, count - 1) + 1):
        covariance = float(np.dot(centered[offset:], centered[:-offset]) / count)
        long_run_variance += 2.0 * (1.0 - offset / (lag + 1.0)) * covariance
    if long_run_variance <= 0:
        return None
    return float(values.mean() / sqrt(long_run_variance / count))


def _mean_std_t(
    values: list[float], *, overlap_aware_hac_lag: int = 0
) -> dict[str, float | int | None]:
    if not values:
        return {
            "observations": 0,
            "mean": None,
            "standard_deviation": None,
            "information_ratio": None,
            "t_statistic": None,
            "overlap_aware_hac_lag": overlap_aware_hac_lag,
            "overlap_aware_t_statistic": None,
        }
    array = np.asarray(values, dtype=float)
    mean = float(array.mean())
    if len(array) < 2:
        return {
            "observations": 1,
            "mean": mean,
            "standard_deviation": None,
            "information_ratio": None,
            "t_statistic": None,
            "overlap_aware_hac_lag": overlap_aware_hac_lag,
            "overlap_aware_t_statistic": None,
        }
    standard_deviation = float(array.std(ddof=1))
    t_statistic = (
        mean / (standard_deviation / sqrt(len(array)))
        if standard_deviation > 0
        else None
    )
    return {
        "observations": len(array),
        "mean": mean,
        "standard_deviation": standard_deviation,
        "information_ratio": mean / standard_deviation if standard_deviation > 0 else None,
        "t_statistic": t_statistic,
        "overlap_aware_hac_lag": overlap_aware_hac_lag,
        "overlap_aware_t_statistic": _newey_west_t(
            array, lag=overlap_aware_hac_lag
        ),
    }


def _weights(rows: list[SignalObservation], count: int) -> tuple[dict[str, float], bool]:
    ordered = sorted(rows, key=lambda item: (item.signal, item.symbol))
    low = ordered[:count]
    high = ordered[-count:]
    boundary_tie = (
        count < len(ordered)
        and (
            low[-1].signal == ordered[count].signal
            or high[0].signal == ordered[-count - 1].signal
        )
    )
    weights = {item.symbol: -1.0 / count for item in low}
    for item in high:
        weights[item.symbol] = weights.get(item.symbol, 0.0) + 1.0 / count
    return weights, boundary_tie


def summarize_signal_diagnostics(
    observations: list[SignalObservation],
    *,
    minimum_cross_section: int = 5,
    quantile_fraction: float = 0.20,
) -> dict[str, Any]:
    """Summarize predictive signal quality without creating a promotion gate."""

    if minimum_cross_section < 3:
        raise ValueError("minimum_cross_section must be at least three")
    if not 0 < quantile_fraction <= 0.5:
        raise ValueError("quantile_fraction must be in (0, 0.5]")
    if not observations:
        raise ValueError("signal observations are empty")
    groups: dict[datetime, list[SignalObservation]] = {}
    for observation in observations:
        if (
            not observation.symbol
            or not np.isfinite(observation.signal)
            or not np.isfinite(observation.forward_return)
            or not observation.decision_at
            < observation.forward_start_at
            <= observation.forward_end_at
        ):
            raise ValueError("signal observation violates the causal schema")
        groups.setdefault(observation.decision_at, []).append(observation)
    ordered_groups = sorted(groups.items())
    if any(len(rows) < minimum_cross_section for _, rows in ordered_groups):
        raise ValueError("a decision cross-section is smaller than the declared minimum")
    for _, rows in ordered_groups:
        if len({item.symbol for item in rows}) != len(rows):
            raise ValueError("a decision cross-section contains duplicate symbols")
        intervals = {(item.forward_start_at, item.forward_end_at) for item in rows}
        if len(intervals) != 1:
            raise ValueError("one decision cross-section contains mixed forward intervals")

    rank_ics: list[float] = []
    spreads: list[float] = []
    autocorrelations: list[float] = []
    turnovers: list[float] = []
    periods: list[dict[str, Any]] = []
    previous_rows: list[SignalObservation] | None = None
    previous_weights: dict[str, float] | None = None
    previous_end: datetime | None = None
    overlapping_windows = 0
    constant_signal_periods = 0
    constant_return_periods = 0
    boundary_tie_periods = 0

    for decision_at, rows in ordered_groups:
        signals = np.asarray([item.signal for item in rows], dtype=float)
        returns = np.asarray([item.forward_return for item in rows], dtype=float)
        rank_ic = _spearman(signals, returns)
        if signals.max() == signals.min():
            constant_signal_periods += 1
        if returns.max() == returns.min():
            constant_return_periods += 1
        if rank_ic is not None:
            rank_ics.append(rank_ic)

        selection_count = max(1, floor(len(rows) * quantile_fraction))
        if selection_count * 2 > len(rows):
            raise ValueError("quantile selection overlaps inside a cross-section")
        weights, boundary_tie = _weights(rows, selection_count)
        boundary_tie_periods += int(boundary_tie)
        spread = float(
            sum(weights.get(item.symbol, 0.0) * item.forward_return for item in rows)
        )
        spreads.append(spread)

        turnover = None
        rank_autocorrelation = None
        if previous_weights is not None:
            symbols = set(previous_weights) | set(weights)
            turnover = 0.5 * sum(
                abs(weights.get(symbol, 0.0) - previous_weights.get(symbol, 0.0))
                for symbol in symbols
            )
            turnovers.append(float(turnover))
        if previous_rows is not None:
            previous_signals = {item.symbol: item.signal for item in previous_rows}
            current_signals = {item.symbol: item.signal for item in rows}
            common = sorted(set(previous_signals) & set(current_signals))
            if len(common) >= 2:
                rank_autocorrelation = _spearman(
                    np.asarray([previous_signals[symbol] for symbol in common]),
                    np.asarray([current_signals[symbol] for symbol in common]),
                )
                if rank_autocorrelation is not None:
                    autocorrelations.append(rank_autocorrelation)

        forward_start = rows[0].forward_start_at
        forward_end = rows[0].forward_end_at
        if previous_end is not None and forward_start < previous_end:
            overlapping_windows += 1
        periods.append(
            {
                "decision_at": decision_at.isoformat(),
                "forward_start_at": forward_start.isoformat(),
                "forward_end_at": forward_end.isoformat(),
                "cross_section_size": len(rows),
                "rank_ic": rank_ic,
                "top_bottom_gross_return": spread,
                "one_way_turnover": turnover,
                "signal_rank_autocorrelation": rank_autocorrelation,
                "boundary_tie": boundary_tie,
            }
        )
        previous_rows = rows
        previous_weights = weights
        previous_end = forward_end

    positive_spreads = [value for value in spreads if value > 0]
    positive_concentration = (
        max(positive_spreads) / sum(positive_spreads) if positive_spreads else None
    )
    forward_periods = [
        (rows[0].forward_start_at, rows[0].forward_end_at)
        for _, rows in ordered_groups
    ]
    overlap_aware_hac_lag = _overlap_aware_hac_lag(forward_periods)
    return {
        "schema_version": 1,
        "kind": "CROSS_SECTIONAL_SIGNAL_DIAGNOSTIC",
        "status": "DIAGNOSTIC_COMPLETE",
        "promotion_eligible": False,
        "live_order_authorization": "CLOSED",
        "configuration": {
            "minimum_cross_section": minimum_cross_section,
            "quantile_fraction": quantile_fraction,
            "top_bottom_weighting": "equal_weight_dollar_neutral_before_costs",
        },
        "coverage": {
            "rows": len(observations),
            "periods": len(ordered_groups),
            "first_decision_at": ordered_groups[0][0].isoformat(),
            "last_decision_at": ordered_groups[-1][0].isoformat(),
            "minimum_cross_section_observed": min(len(rows) for _, rows in ordered_groups),
            "maximum_cross_section_observed": max(len(rows) for _, rows in ordered_groups),
            "overlapping_forward_windows": overlapping_windows,
            "overlap_aware_hac_lag": overlap_aware_hac_lag,
        },
        "signal_quality": {
            "rank_ic": _mean_std_t(
                rank_ics, overlap_aware_hac_lag=overlap_aware_hac_lag
            ),
            "rank_ic_positive_fraction": (
                sum(value > 0 for value in rank_ics) / len(rank_ics) if rank_ics else None
            ),
            "top_bottom_gross_return": _mean_std_t(
                spreads, overlap_aware_hac_lag=overlap_aware_hac_lag
            ),
            "positive_spread_single_period_concentration": positive_concentration,
            "signal_rank_autocorrelation": _mean_std_t(autocorrelations),
            "one_way_turnover": _mean_std_t(turnovers),
        },
        "diagnostics": {
            "constant_signal_periods": constant_signal_periods,
            "constant_forward_return_periods": constant_return_periods,
            "quantile_boundary_tie_periods": boundary_tie_periods,
        },
        "periods": periods,
        "limitations": [
            "gross forward returns do not include fees, slippage, funding, or fill uncertainty",
            "timestamp checks enforce declared causality but do not independently reconstruct labels from raw prices",
            "overlap-aware t-statistics use a Bartlett-kernel Newey-West correction derived from declared forward-label overlap; they do not establish independent observations or live profitability",
            "this diagnostic cannot pass a strategy, portfolio, or live-order gate",
        ],
    }
