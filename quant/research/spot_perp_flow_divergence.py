"""Causal, simulation-only test of BTC spot/perpetual flow divergence.

The module deliberately accepts normalized aggregate-trade rows rather than an
exchange client.  Acquisition and archive validation are a separate boundary:
this engine must never submit an order or infer a missing observation.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import random
from typing import Any, Iterable

import numpy as np


MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS


@dataclass(frozen=True)
class SpotPerpFlowConfig:
    observation_window_ms: int = 10 * MINUTE_MS
    decision_interval_ms: int = 4 * HOUR_MS
    entry_delay_ms: int = MINUTE_MS
    holding_ms: int = 4 * HOUR_MS
    maximum_reference_delay_ms: int = MINUTE_MS
    maximum_intertrade_gap_ms: int = MINUTE_MS
    perp_imbalance_minimum: float = 0.25
    spot_imbalance_maximum: float = -0.10
    fee_per_side_bps: float = 10.0
    slippage_per_side_bps: float = 3.0


def _buyer_maker(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).lower() == "true":
        return True
    if str(value).lower() == "false":
        return False
    raise ValueError("is_buyer_maker must be boolean")


def _normalize_stream(rows: Iterable[dict[str, Any]], *, market: str) -> list[dict[str, Any]]:
    """Validate a complete, already-normalized aggregate-trade stream.

    ``is_buyer_maker == false`` means the buyer initiated the trade, hence it
    is positive aggressive flow.  The input order is intentionally preserved
    and validated so a caller cannot conceal missing time or ID ordering by
    sorting the data after the fact.
    """
    output: list[dict[str, Any]] = []
    previous_id: int | None = None
    previous_time: int | None = None
    for raw in rows:
        try:
            trade_id = int(raw["agg_trade_id"])
            timestamp = int(raw["transact_time"])
            price = float(raw["price"])
            quantity = float(raw["quantity"])
            buyer_maker = _buyer_maker(raw["is_buyer_maker"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"{market} aggregate trade has invalid schema") from error
        if trade_id < 0 or timestamp < 0 or price <= 0 or quantity <= 0:
            raise ValueError(f"{market} aggregate trade has invalid values")
        if previous_id is not None and trade_id != previous_id + 1:
            raise ValueError(f"{market} aggregate-trade IDs are not continuous")
        if previous_time is not None and timestamp < previous_time:
            raise ValueError(f"{market} aggregate-trade timestamps are not monotonic")
        output.append(
            {
                "agg_trade_id": trade_id,
                "transact_time": timestamp,
                "price": price,
                "quantity": quantity,
                "is_buyer_maker": buyer_maker,
            }
        )
        previous_id, previous_time = trade_id, timestamp
    return output


def _imbalance(rows: list[dict[str, Any]], timestamps: list[int], *, start_ms: int, end_ms: int) -> float | None:
    start = bisect_left(timestamps, start_ms)
    end = bisect_left(timestamps, end_ms)
    selected = rows[start:end]
    total = sum(float(item["quantity"]) for item in selected)
    if total <= 0:
        return None
    signed = sum(
        (-1.0 if item["is_buyer_maker"] else 1.0) * float(item["quantity"])
        for item in selected
    )
    return signed / total


def _first_at_or_after(
    rows: list[dict[str, Any]], timestamps: list[int], when_ms: int
) -> dict[str, Any] | None:
    index = bisect_left(timestamps, when_ms)
    return rows[index] if index < len(rows) else None


def validate_trade_stream_coverage(
    rows: Iterable[dict[str, Any]],
    *,
    market: str,
    start_ms: int,
    end_ms: int,
    maximum_intertrade_gap_ms: int = MINUTE_MS,
) -> dict[str, int | str]:
    """Fail closed when a source stream cannot support every instant in a range.

    Aggregate-trade ID continuity alone does not detect an archive truncated at
    a time boundary.  BTC should not have a one-minute no-trade interval; such
    an interval is therefore treated as missing coverage rather than a price
    that can be carried forward to a virtual fill.
    """
    if start_ms < 0 or end_ms <= start_ms or maximum_intertrade_gap_ms <= 0:
        raise ValueError("coverage interval or maximum gap is invalid")
    normalized = _normalize_stream(rows, market=market)
    if not normalized:
        raise ValueError(f"{market} aggregate-trade stream is empty")
    timestamps = [int(item["transact_time"]) for item in normalized]
    previous = start_ms
    start = bisect_left(timestamps, start_ms)
    end = bisect_left(timestamps, end_ms)
    for timestamp in timestamps[start:end]:
        if timestamp - previous > maximum_intertrade_gap_ms:
            raise ValueError(f"{market} aggregate-trade coverage gap exceeds maximum")
        previous = timestamp
    if end_ms - previous > maximum_intertrade_gap_ms:
        raise ValueError(f"{market} aggregate-trade coverage gap exceeds maximum")
    return {
        "status": "PASS",
        "market": market,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "maximum_intertrade_gap_ms": maximum_intertrade_gap_ms,
    }


def _reprice_one(item: dict[str, Any], *, fee_per_side_bps: float, slippage_per_side_bps: float) -> dict[str, Any]:
    if fee_per_side_bps < 0 or slippage_per_side_bps < 0:
        raise ValueError("fees and slippage must be non-negative")
    result = dict(item)
    raw_entry = float(item["raw_entry"])
    raw_exit = float(item["raw_exit"])
    slippage = slippage_per_side_bps / 10_000.0
    fee = fee_per_side_bps / 10_000.0
    entry_execution = raw_entry * (1.0 + slippage)
    exit_execution = raw_exit * (1.0 - slippage)
    raw_price_return = raw_exit / raw_entry - 1.0
    execution_price_return = exit_execution / entry_execution - 1.0
    trading_fee_drag = fee + fee * exit_execution / entry_execution
    result.update(
        {
            "fee_per_side_bps": fee_per_side_bps,
            "slippage_per_side_bps": slippage_per_side_bps,
            "raw_price_return": raw_price_return,
            "execution_price_return": execution_price_return,
            "trading_fee_drag": trading_fee_drag,
            "slippage_drag": raw_price_return - execution_price_return,
            "funding_return": 0.0,
            "net_return": execution_price_return - trading_fee_drag,
        }
    )
    return result


def simulate_flow_divergence(
    spot_rows: Iterable[dict[str, Any]],
    perpetual_rows: Iterable[dict[str, Any]],
    *,
    boundaries_ms: list[int],
    config: SpotPerpFlowConfig = SpotPerpFlowConfig(),
) -> list[dict[str, Any]]:
    """Return non-overlapping virtual spot-long trades from fixed decision times.

    The feature window ends exactly at the decision boundary.  The entry is the
    first *spot* aggregate trade available at least one minute later; the exit
    uses the first spot trade at least four hours after that actual entry.  No
    perpetual position is created and no funding transfer is credited.
    """
    if not boundaries_ms:
        return []
    boundaries = sorted(boundaries_ms)
    if boundaries != list(dict.fromkeys(boundaries)):
        raise ValueError("decision boundaries must be unique")
    if any(boundary < 0 or boundary % config.decision_interval_ms for boundary in boundaries):
        raise ValueError("decision boundaries must be aligned to the fixed four-hour UTC grid")
    if config.observation_window_ms <= 0 or config.entry_delay_ms < 0 or config.holding_ms <= 0:
        raise ValueError("time parameters are invalid")
    if config.maximum_reference_delay_ms <= 0 or config.maximum_intertrade_gap_ms <= 0:
        raise ValueError("maximum data gaps must be positive")

    spot = _normalize_stream(spot_rows, market="spot")
    perpetual = _normalize_stream(perpetual_rows, market="perpetual")
    if not spot or not perpetual:
        raise ValueError("both spot and perpetual streams must be non-empty")
    spot_times = [int(item["transact_time"]) for item in spot]
    perp_times = [int(item["transact_time"]) for item in perpetual]
    output: list[dict[str, Any]] = []
    occupied_until = -1

    for boundary in boundaries:
        if boundary < occupied_until:
            continue
        window_start = boundary - config.observation_window_ms
        if window_start < 0:
            raise ValueError("decision boundary lacks the full observation window")
        perp_imbalance = _imbalance(
            perpetual, perp_times, start_ms=window_start, end_ms=boundary
        )
        spot_imbalance = _imbalance(spot, spot_times, start_ms=window_start, end_ms=boundary)
        if perp_imbalance is None or spot_imbalance is None:
            raise ValueError("flow window is incomplete or contains no trades")
        if not (
            perp_imbalance >= config.perp_imbalance_minimum
            and spot_imbalance <= config.spot_imbalance_maximum
        ):
            continue
        expected_entry = boundary + config.entry_delay_ms
        entry = _first_at_or_after(spot, spot_times, expected_entry)
        if entry is None:
            raise ValueError("spot entry reference price is unavailable")
        if int(entry["transact_time"]) - expected_entry > config.maximum_reference_delay_ms:
            raise ValueError("spot entry reference delay exceeds maximum")
        expected_exit = int(entry["transact_time"]) + config.holding_ms
        exit_row = _first_at_or_after(
            spot, spot_times, expected_exit
        )
        if exit_row is None:
            raise ValueError("spot exit reference price is unavailable")
        if int(exit_row["transact_time"]) - expected_exit > config.maximum_reference_delay_ms:
            raise ValueError("spot exit reference delay exceeds maximum")
        raw = {
            "signal_at_ms": boundary,
            "observation_start_ms": window_start,
            "observation_end_ms": boundary,
            "perp_imbalance": perp_imbalance,
            "spot_imbalance": spot_imbalance,
            "direction": "simulated_spot_long",
            "entry_time_ms": int(entry["transact_time"]),
            "exit_time_ms": int(exit_row["transact_time"]),
            "raw_entry": float(entry["price"]),
            "raw_exit": float(exit_row["price"]),
            "holding_hours": (int(exit_row["transact_time"]) - int(entry["transact_time"])) / HOUR_MS,
            "exit_reason": "fixed_holding_period",
        }
        output.append(
            _reprice_one(
                raw,
                fee_per_side_bps=config.fee_per_side_bps,
                slippage_per_side_bps=config.slippage_per_side_bps,
            )
        )
        occupied_until = int(exit_row["transact_time"])
    return output


def reprice_trades(
    trades: list[dict[str, Any]], *, fee_per_side_bps: float, slippage_per_side_bps: float
) -> list[dict[str, Any]]:
    """Reprice sealed virtual entries/exits under a cost-only stress scenario."""
    return [
        _reprice_one(
            item,
            fee_per_side_bps=fee_per_side_bps,
            slippage_per_side_bps=slippage_per_side_bps,
        )
        for item in trades
    ]


def summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(item["net_return"]) for item in trades]
    positive = [item for item in returns if item > 0]
    negative = [item for item in returns if item < 0]
    wealth = [1.0]
    for value in returns:
        wealth.append(wealth[-1] * (1.0 + value))
    peak = 1.0
    maximum_drawdown = 0.0
    for value in wealth:
        peak = max(peak, value)
        maximum_drawdown = max(maximum_drawdown, 1.0 - value / peak)
    gross_wins = sum(positive)
    return {
        "trades": len(trades),
        "wins": len(positive),
        "losses": len(negative),
        "sum_net_return": sum(returns),
        "compounded_net_return": wealth[-1] - 1.0,
        "expectancy": sum(returns) / len(returns) if returns else None,
        "profit_factor": gross_wins / -sum(negative) if negative else None,
        "maximum_drawdown": maximum_drawdown,
        "gross_price_return": sum(float(item["raw_price_return"]) for item in trades),
        "trading_fee_drag": sum(float(item["trading_fee_drag"]) for item in trades),
        "slippage_drag": sum(float(item["slippage_drag"]) for item in trades),
        "funding_return": 0.0,
        "largest_single_loss": min(returns) if returns else None,
        "largest_winner_profit_concentration": (
            max(positive) / gross_wins if gross_wins else None
        ),
    }


def matched_random_baseline(
    spot_rows: Iterable[dict[str, Any]],
    *,
    boundaries_ms: list[int],
    signal_trades: list[dict[str, Any]],
    config: SpotPerpFlowConfig = SpotPerpFlowConfig(),
    seed: int = 16,
) -> list[dict[str, Any]]:
    """Draw one no-replacement control hold per signal from the same UTC day.

    A control is a spot-only four-hour hold from a different eligible grid time.
    It is descriptive evidence, not a separately tradable portfolio, and fails
    closed if a signal day has too few same-day alternatives.  Controls are
    deliberately not labelled "no signal": the baseline is a random hold-time
    comparison, not a second feature-conditioned strategy.
    """
    spot = _normalize_stream(spot_rows, market="spot")
    if not spot:
        raise ValueError("spot stream must be non-empty")
    times = [int(item["transact_time"]) for item in spot]
    by_day: dict[str, list[dict[str, Any]]] = {}
    signal_boundaries = {int(item["signal_at_ms"]) for item in signal_trades}
    for boundary in sorted(boundaries_ms):
        if boundary in signal_boundaries:
            continue
        expected_entry = boundary + config.entry_delay_ms
        entry = _first_at_or_after(spot, times, expected_entry)
        if entry is None:
            raise ValueError("random-control entry reference price is unavailable")
        if int(entry["transact_time"]) - expected_entry > config.maximum_reference_delay_ms:
            raise ValueError("random-control entry reference delay exceeds maximum")
        expected_exit = int(entry["transact_time"]) + config.holding_ms
        exit_row = _first_at_or_after(spot, times, expected_exit)
        if exit_row is None:
            raise ValueError("random-control exit reference price is unavailable")
        if int(exit_row["transact_time"]) - expected_exit > config.maximum_reference_delay_ms:
            raise ValueError("random-control exit reference delay exceeds maximum")
        day = datetime.fromtimestamp(boundary / 1000, tz=timezone.utc).date().isoformat()
        by_day.setdefault(day, []).append(
            {
                "signal_at_ms": boundary,
                "entry_time_ms": int(entry["transact_time"]),
                "exit_time_ms": int(exit_row["transact_time"]),
                "raw_entry": float(entry["price"]),
                "raw_exit": float(exit_row["price"]),
                "direction": "random_spot_long_control",
                "holding_hours": (int(exit_row["transact_time"]) - int(entry["transact_time"])) / HOUR_MS,
                "exit_reason": "fixed_holding_period",
            }
        )
    generator = random.Random(seed)
    selected: list[dict[str, Any]] = []
    for signal in signal_trades:
        day = datetime.fromtimestamp(int(signal["signal_at_ms"]) / 1000, tz=timezone.utc).date().isoformat()
        options = by_day.get(day, [])
        if not options:
            raise ValueError("no same-day eligible random control exists for a signal")
        choice = options.pop(generator.randrange(len(options)))
        selected.append(
            _reprice_one(
                choice,
                fee_per_side_bps=config.fee_per_side_bps,
                slippage_per_side_bps=config.slippage_per_side_bps,
            )
        )
    return selected


def daily_block_bootstrap_excess_return(
    signal_trades: list[dict[str, Any]],
    controls: list[dict[str, Any]],
    *,
    calendar_start_utc: date,
    calendar_end_exclusive_utc: date,
    block_days: int = 7,
    paths: int = 5_000,
    seed: int = 16,
) -> dict[str, float | int | None]:
    """Estimate a 95% CI for daily signal-minus-control return by block bootstrap."""
    if len(signal_trades) != len(controls) or not signal_trades:
        raise ValueError("signal and control samples must be non-empty and paired")
    if block_days <= 0 or paths <= 0:
        raise ValueError("bootstrap parameters must be positive")
    if calendar_end_exclusive_utc <= calendar_start_utc:
        raise ValueError("calendar bounds are invalid")
    grouped: dict[date, float] = {}
    for signal, control in zip(signal_trades, controls):
        day = datetime.fromtimestamp(int(signal["signal_at_ms"]) / 1000, tz=timezone.utc).date()
        if not calendar_start_utc <= day < calendar_end_exclusive_utc:
            raise ValueError("signal observation falls outside the locked calendar")
        grouped[day] = grouped.get(day, 0.0) + float(signal["net_return"]) - float(control["net_return"])
    days = (calendar_end_exclusive_utc - calendar_start_utc).days
    daily = [
        grouped.get(calendar_start_utc + timedelta(days=offset), 0.0)
        for offset in range(days)
    ]
    if len(daily) < block_days:
        return {"days": len(daily), "signal_days": len(grouped), "block_days": block_days, "ci95_lower": None, "ci95_upper": None}
    blocks = [daily[index : index + block_days] for index in range(len(daily) - block_days + 1)]
    generator = random.Random(seed)
    samples: list[float] = []
    for _ in range(paths):
        sample: list[float] = []
        while len(sample) < len(daily):
            sample.extend(generator.choice(blocks))
        samples.append(sum(sample[: len(daily)]))
    return {
        "days": len(daily),
        "signal_days": len(grouped),
        "block_days": block_days,
        "observed_excess_return": sum(daily),
        "ci95_lower": float(np.percentile(samples, 2.5)),
        "ci95_upper": float(np.percentile(samples, 97.5)),
    }
