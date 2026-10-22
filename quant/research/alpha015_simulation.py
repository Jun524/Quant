"""Source-agnostic execution and PnL replay for Alpha 015.

The module accepts already verified boundary prices and funding events.  It
does not open files or download data, which keeps it testable before the
candidate is authorized to acquire project returns.
"""

from __future__ import annotations

from math import prod
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from quant.research.cross_crypto_predictor import (
    AdaptiveLassoKernelConfig,
    execution_window,
    funding_pnl_fraction,
    realized_turnover,
)


def returns_from_closed_klines(
    klines_by_symbol: Mapping[str, Iterable[Mapping[str, Any]]],
) -> pd.DataFrame:
    """Build close-to-close returns labelled by their observable UTC boundary.

    A kline whose ``open_time`` is 12:09 is fully known at 12:10, so its close
    is indexed at 12:10.  A decision at 12:10 may therefore use the return
    labelled 12:10 but no later row.
    """
    closes: dict[str, pd.Series] = {}
    for raw_symbol, raw_rows in sorted(klines_by_symbol.items()):
        symbol = str(raw_symbol)
        rows = list(raw_rows)
        if len(rows) < 2:
            raise ValueError(f"kline history is too short: {symbol}")
        timestamps: list[pd.Timestamp] = []
        values: list[float] = []
        for row in rows:
            try:
                open_time = int(row["open_time"])
                close = float(row["close"])
            except (KeyError, TypeError, ValueError, OverflowError) as error:
                raise ValueError(f"kline row is invalid: {symbol}") from error
            if not np.isfinite(close) or close <= 0:
                raise ValueError(f"kline close is not positive and finite: {symbol}")
            timestamps.append(
                pd.to_datetime(open_time + 60_000, unit="ms", utc=True)
            )
            values.append(close)
        index = pd.DatetimeIndex(timestamps)
        if index.has_duplicates or not index.is_monotonic_increasing:
            raise ValueError(f"kline timestamps are not unique and sorted: {symbol}")
        expected = pd.date_range(index[0], index[-1], freq="1min")
        if len(index) != len(expected) or len(index.symmetric_difference(expected)):
            raise ValueError(f"kline timestamps contain a minute gap: {symbol}")
        closes[symbol] = pd.Series(values, index=index, dtype=float)
    panel = pd.DataFrame(closes)
    if panel.isna().any().any():
        raise ValueError("kline symbols do not share an exact minute grid")
    returns = panel.pct_change(fill_method=None).iloc[1:]
    if returns.empty or not np.isfinite(returns.to_numpy(dtype=float)).all():
        raise ValueError("close-to-close return panel is empty or non-finite")
    return returns


def first_trade_price_at_or_after(
    events: Iterable[Mapping[str, Any]],
    *,
    boundary_at: str | pd.Timestamp,
    maximum_delay_ms: int = 60_000,
) -> dict[str, Any]:
    """Select the first verified aggregate trade on or after a boundary."""
    boundary = pd.Timestamp(boundary_at)
    boundary = (
        boundary.tz_localize("UTC")
        if boundary.tzinfo is None
        else boundary.tz_convert("UTC")
    )
    boundary_ms = int(boundary.timestamp() * 1000)
    if maximum_delay_ms < 0:
        raise ValueError("maximum execution delay cannot be negative")
    previous_time: int | None = None
    selected: tuple[int, float, int] | None = None
    for event in events:
        try:
            timestamp = int(event["transact_time"])
            price = float(event["price"])
            trade_id = int(event["agg_trade_id"])
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError("aggregate-trade execution event is invalid") from error
        if previous_time is not None and timestamp < previous_time:
            raise ValueError("aggregate-trade execution events are not chronological")
        previous_time = timestamp
        if not np.isfinite(price) or price <= 0:
            raise ValueError("aggregate-trade execution price is invalid")
        if timestamp >= boundary_ms:
            selected = (timestamp, price, trade_id)
            break
    if selected is None or selected[0] - boundary_ms > maximum_delay_ms:
        raise ValueError("no aggregate trade inside the execution-delay ceiling")
    return {
        "boundary_at": boundary.isoformat(),
        "trade_at": pd.to_datetime(selected[0], unit="ms", utc=True).isoformat(),
        "delay_ms": selected[0] - boundary_ms,
        "price": selected[1],
        "agg_trade_id": selected[2],
    }


def _validate_target(
    universe_symbols: Iterable[str],
    weights: Mapping[str, float],
    *,
    config: AdaptiveLassoKernelConfig,
) -> dict[str, float]:
    universe = tuple(map(str, universe_symbols))
    if len(universe) < config.minimum_symbols or len(set(universe)) != len(universe):
        raise ValueError("decision universe is smaller than 30 or contains duplicates")
    clean = {str(symbol): float(weight) for symbol, weight in weights.items()}
    if not clean or not set(clean).issubset(universe):
        raise ValueError("target weights are empty or outside the decision universe")
    if not all(np.isfinite(weight) and weight != 0 for weight in clean.values()):
        raise ValueError("target weights must be finite and non-zero")
    tail_count = len(universe) // config.portfolio_buckets
    longs = [weight for weight in clean.values() if weight > 0]
    shorts = [weight for weight in clean.values() if weight < 0]
    if len(longs) != tail_count or len(shorts) != tail_count:
        raise ValueError("target does not contain the fixed top and bottom quintiles")
    expected = 0.5 / tail_count
    if any(not np.isclose(weight, expected, atol=1e-12) for weight in longs):
        raise ValueError("long quintile weights are not equal-weighted")
    if any(not np.isclose(weight, -expected, atol=1e-12) for weight in shorts):
        raise ValueError("short quintile weights are not equal-weighted")
    if not np.isclose(sum(abs(weight) for weight in clean.values()), 1.0, atol=1e-12):
        raise ValueError("target gross exposure is not one")
    if not np.isclose(sum(clean.values()), 0.0, atol=1e-12):
        raise ValueError("target net exposure is not zero")
    return dict(sorted(clean.items()))


def _prices(
    raw: Mapping[str, float],
    required: Iterable[str],
    *,
    label: str,
) -> dict[str, float]:
    required_set = set(required)
    if set(map(str, raw)) != required_set:
        raise ValueError(f"{label} price inventory does not exactly match target symbols")
    prices = {str(symbol): float(value) for symbol, value in raw.items()}
    if not all(np.isfinite(value) and value > 0 for value in prices.values()):
        raise ValueError(f"{label} prices must be positive and finite")
    return prices


def _utc_timestamp(value: str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return (
        timestamp.tz_localize("UTC")
        if timestamp.tzinfo is None
        else timestamp.tz_convert("UTC")
    )


def _maximum_drawdown(equity: list[float]) -> float:
    peak = -np.inf
    maximum = 0.0
    for value in equity:
        if not np.isfinite(value) or value <= 0:
            raise ValueError("mark-to-market equity must remain positive and finite")
        peak = max(peak, value)
        maximum = max(maximum, 1.0 - value / peak)
    return float(maximum)


def intraday_mark_to_market_path(
    decisions: list[dict[str, Any]],
    *,
    funding_events: Mapping[str, list[Mapping[str, Any]]],
    interior_mark_prices: Mapping[str | pd.Timestamp, Mapping[str, float]],
    cost_per_turnover: float,
    config: AdaptiveLassoKernelConfig | None = None,
) -> dict[str, Any]:
    """Replay the conservative minute-boundary equity path for one UTC day.

    Entry, exit, and shared-rebalance prices remain the separately verified
    aggregate-trade prices in ``decisions``.  ``interior_mark_prices`` supplies
    only the nine closed one-minute marks strictly inside each ten-minute
    holding window, which prevents a kline close from silently replacing an
    execution-boundary price.  At a shared boundary the curve emits a
    pre-rebalance point and then a post-cost point at the same timestamp.
    """
    config = config or AdaptiveLassoKernelConfig()
    if len(decisions) != 143:
        raise ValueError("a complete Alpha 015 UTC day must contain 143 decisions")
    if not np.isfinite(cost_per_turnover) or cost_per_turnover < 0:
        raise ValueError("mark-to-market turnover cost is invalid")

    marks: dict[pd.Timestamp, Mapping[str, float]] = {}
    for raw_timestamp, prices in interior_mark_prices.items():
        timestamp = _utc_timestamp(raw_timestamp)
        if timestamp in marks:
            raise ValueError("minute mark timestamps are duplicated")
        marks[timestamp] = prices

    expected_marks: set[pd.Timestamp] = set()
    timings: list[dict[str, Any]] = []
    for decision in decisions:
        timing = execution_window(decision["decision_at"], config=config)
        timings.append(timing)
        entry = _utc_timestamp(timing["earliest_entry_at"])
        expected_marks.update(
            entry + pd.to_timedelta(offset, unit="min")
            for offset in range(1, config.holding_minutes)
        )
    if set(marks) != expected_marks:
        missing = len(expected_marks - set(marks))
        extra = len(set(marks) - expected_marks)
        raise ValueError(
            f"interior minute mark inventory is not exact: missing={missing}, extra={extra}"
        )

    equity = 1.0
    previous_pretrade_weights: dict[str, float] = {}
    previous_exit_at: pd.Timestamp | None = None
    previous_exit_prices: dict[str, float] | None = None
    curve: list[dict[str, Any]] = []
    evaluation_day = None

    for sequence, (decision, timing) in enumerate(zip(decisions, timings)):
        entry_at = _utc_timestamp(timing["earliest_entry_at"])
        exit_at = _utc_timestamp(timing["exit_at"])
        if evaluation_day is None:
            evaluation_day = entry_at.date()
        if entry_at.date() != evaluation_day or exit_at.date() != evaluation_day:
            raise ValueError("daily mark-to-market path crosses its UTC evaluation day")
        if previous_exit_at is not None and entry_at != previous_exit_at:
            raise ValueError("mark-to-market decision windows are not contiguous")
        weights = _validate_target(
            decision["universe_symbols"], decision["target_weights"], config=config
        )
        entry_prices = _prices(decision["entry_prices"], weights, label="entry")
        exit_prices = _prices(decision["exit_prices"], weights, label="exit")
        if previous_exit_prices is not None:
            overlap = set(previous_pretrade_weights) & set(weights)
            if any(
                previous_exit_prices[symbol] != entry_prices[symbol]
                for symbol in overlap
            ):
                raise ValueError("shared rebalance boundary uses inconsistent prices")

        turnover = realized_turnover(previous_pretrade_weights, weights)
        cost = turnover * cost_per_turnover
        funding = funding_pnl_fraction(
            weights, funding_events, entry_at=entry_at, exit_at=exit_at
        )
        funding_by_timestamp: dict[pd.Timestamp, float] = {}
        for contribution in funding["contributions"]:
            timestamp = _utc_timestamp(contribution["funding_time"])
            funding_by_timestamp[timestamp] = (
                funding_by_timestamp.get(timestamp, 0.0)
                + float(contribution["pnl_fraction"])
            )

        def equity_at(
            timestamp: pd.Timestamp, prices: Mapping[str, float]
        ) -> float:
            valid_prices = _prices(prices, weights, label="mark")
            price_return = sum(
                weight * (valid_prices[symbol] / entry_prices[symbol] - 1.0)
                for symbol, weight in weights.items()
            )
            funding_return = sum(
                amount
                for funded_at, amount in funding_by_timestamp.items()
                if entry_at <= funded_at <= timestamp and funded_at < exit_at
            )
            value = equity * (1.0 - cost + price_return + funding_return)
            if not np.isfinite(value) or value <= 0:
                raise ValueError("minute mark-to-market path reaches non-positive equity")
            return float(value)

        curve.append(
            {
                "at": entry_at.isoformat(),
                "sequence": sequence,
                "stage": "entry_from_cash" if sequence == 0 else "post_rebalance",
                "equity": equity_at(entry_at, entry_prices),
            }
        )
        for offset in range(1, config.holding_minutes):
            mark_at = entry_at + pd.to_timedelta(offset, unit="min")
            curve.append(
                {
                    "at": mark_at.isoformat(),
                    "sequence": sequence,
                    "stage": "interior_closed_minute_mark",
                    "equity": equity_at(mark_at, marks[mark_at]),
                }
            )
        interval_equity = equity_at(exit_at, exit_prices)
        curve.append(
            {
                "at": exit_at.isoformat(),
                "sequence": sequence,
                "stage": "pre_rebalance" if sequence + 1 < len(decisions) else "pre_final_exit",
                "equity": interval_equity,
            }
        )
        price_pnl = float(
            sum(
                weight * (exit_prices[symbol] / entry_prices[symbol] - 1.0)
                for symbol, weight in weights.items()
            )
        )
        pre_cost_factor = 1.0 + price_pnl + float(funding["funding_pnl_fraction"])
        if pre_cost_factor <= 0:
            raise ValueError("pre-cost interval path reaches non-positive equity")
        previous_pretrade_weights = {
            symbol: weight * (exit_prices[symbol] / entry_prices[symbol]) / pre_cost_factor
            for symbol, weight in weights.items()
        }
        equity = interval_equity
        previous_exit_at = exit_at
        previous_exit_prices = exit_prices

    final_turnover = realized_turnover(previous_pretrade_weights, {})
    final_cost = final_turnover * cost_per_turnover
    final_equity = equity * (1.0 - final_cost)
    if not np.isfinite(final_equity) or final_equity <= 0:
        raise ValueError("final mark-to-market liquidation reaches non-positive equity")
    curve.append(
        {
            "at": previous_exit_at.isoformat(),
            "sequence": len(decisions) - 1,
            "stage": "post_final_exit",
            "equity": float(final_equity),
        }
    )
    values = [float(item["equity"]) for item in curve]
    return {
        "status": "PASS",
        "evaluation_date": evaluation_day.isoformat(),
        "cost_per_turnover": cost_per_turnover,
        "points": curve,
        "maximum_drawdown": _maximum_drawdown(values),
        "final_equity": float(final_equity),
        "final_return": float(final_equity - 1.0),
        "boundary_price_policy": "verified_aggtrade",
        "interior_mark_policy": "closed_one_minute_mark",
    }


def simulate_daily_path(
    decisions: list[dict[str, Any]],
    *,
    funding_events: Mapping[str, list[Mapping[str, Any]]],
    base_cost_per_turnover: float = 0.0005,
    stress_cost_per_turnover: float = 0.001,
    interior_mark_prices: Mapping[str | pd.Timestamp, Mapping[str, float]] | None = None,
    config: AdaptiveLassoKernelConfig | None = None,
) -> dict[str, Any]:
    """Replay one complete UTC day under both frozen cost floors."""
    config = config or AdaptiveLassoKernelConfig()
    if len(decisions) != 143:
        raise ValueError("a complete Alpha 015 UTC day must contain 143 decisions")
    if (
        base_cost_per_turnover < 0
        or stress_cost_per_turnover < base_cost_per_turnover
        or not np.isfinite([base_cost_per_turnover, stress_cost_per_turnover]).all()
    ):
        raise ValueError("base and stress costs are invalid")

    previous_pretrade_weights: dict[str, float] = {}
    previous_target_weights: dict[str, float] = {}
    previous_exit_at: pd.Timestamp | None = None
    previous_exit_prices: dict[str, float] | None = None
    interval_rows: list[dict[str, Any]] = []
    base_factors: list[float] = []
    stress_factors: list[float] = []
    gross_price_factors: list[float] = []
    total_turnover = 0.0
    evaluation_day = None

    for index, decision in enumerate(decisions):
        timing = execution_window(decision["decision_at"], config=config)
        entry_at = pd.Timestamp(timing["earliest_entry_at"])
        exit_at = pd.Timestamp(timing["exit_at"])
        if evaluation_day is None:
            evaluation_day = entry_at.date()
        if entry_at.date() != evaluation_day or exit_at.date() != evaluation_day:
            raise ValueError("daily simulation crosses its UTC evaluation day")
        if previous_exit_at is not None and entry_at != previous_exit_at:
            raise ValueError("decision windows are not contiguous")
        weights = _validate_target(
            decision["universe_symbols"],
            decision["target_weights"],
            config=config,
        )
        entry_prices = _prices(
            decision["entry_prices"], weights, label="entry"
        )
        exit_prices = _prices(decision["exit_prices"], weights, label="exit")
        if previous_exit_prices is not None:
            overlap = set(previous_target_weights) & set(weights)
            if any(
                previous_exit_prices[symbol] != entry_prices[symbol]
                for symbol in overlap
            ):
                raise ValueError("shared rebalance boundary uses inconsistent prices")

        turnover = realized_turnover(previous_pretrade_weights, weights)
        total_turnover += turnover
        price_pnl = float(
            sum(
                weight * (exit_prices[symbol] / entry_prices[symbol] - 1.0)
                for symbol, weight in weights.items()
            )
        )
        funding = funding_pnl_fraction(
            weights,
            funding_events,
            entry_at=entry_at,
            exit_at=exit_at,
        )
        funding_pnl = float(funding["funding_pnl_fraction"])
        base_cost = turnover * base_cost_per_turnover
        stress_cost = turnover * stress_cost_per_turnover
        base_net = price_pnl + funding_pnl - base_cost
        stress_net = price_pnl + funding_pnl - stress_cost
        if min(1.0 + price_pnl, 1.0 + base_net, 1.0 + stress_net) <= 0:
            raise ValueError("daily interval path reaches non-positive equity")
        gross_price_factors.append(1.0 + price_pnl)
        base_factors.append(1.0 + base_net)
        stress_factors.append(1.0 + stress_net)
        interval_rows.append(
            {
                "sequence": index,
                "decision_at": timing["decision_at"],
                "entry_at": timing["earliest_entry_at"],
                "exit_at": timing["exit_at"],
                "universe_size": len(decision["universe_symbols"]),
                "positions": len(weights),
                "turnover": turnover,
                "gross_price_return": price_pnl,
                "funding_return": funding_pnl,
                "funding_events": funding["events_applied"],
                "base_cost": base_cost,
                "stress_cost": stress_cost,
                "base_net_return": base_net,
                "stress_net_return": stress_net,
            }
        )
        pre_cost_factor = 1.0 + price_pnl + funding_pnl
        if pre_cost_factor <= 0:
            raise ValueError("pre-cost interval path reaches non-positive equity")
        previous_pretrade_weights = {
            symbol: weight * (exit_prices[symbol] / entry_prices[symbol]) / pre_cost_factor
            for symbol, weight in weights.items()
        }
        previous_target_weights = weights
        previous_exit_at = exit_at
        previous_exit_prices = exit_prices

    final_exit_turnover = realized_turnover(previous_pretrade_weights, {})
    total_turnover += final_exit_turnover
    base_exit_factor = 1.0 - final_exit_turnover * base_cost_per_turnover
    stress_exit_factor = 1.0 - final_exit_turnover * stress_cost_per_turnover
    if min(base_exit_factor, stress_exit_factor) <= 0:
        raise ValueError("final liquidation cost reaches non-positive equity")
    result = {
        "status": "PASS",
        "evaluation_date": evaluation_day.isoformat(),
        "decisions": len(decisions),
        "intervals": interval_rows,
        "turnover": {
            "entry_and_intermediate": total_turnover - final_exit_turnover,
            "final_exit": final_exit_turnover,
            "total": total_turnover,
            "policy": "direct_net_rebalance_not_forced_flat",
        },
        "gross_price_return": float(prod(gross_price_factors) - 1.0),
        "funding_return_arithmetic": float(
            sum(row["funding_return"] for row in interval_rows)
        ),
        "base_net_return": float(prod(base_factors) * base_exit_factor - 1.0),
        "stress_net_return": float(
            prod(stress_factors) * stress_exit_factor - 1.0
        ),
        "base_final_exit_cost": final_exit_turnover * base_cost_per_turnover,
        "stress_final_exit_cost": final_exit_turnover * stress_cost_per_turnover,
    }
    if interior_mark_prices is not None:
        result["mark_to_market"] = {
            "base": intraday_mark_to_market_path(
                decisions,
                funding_events=funding_events,
                interior_mark_prices=interior_mark_prices,
                cost_per_turnover=base_cost_per_turnover,
                config=config,
            ),
            "stress": intraday_mark_to_market_path(
                decisions,
                funding_events=funding_events,
                interior_mark_prices=interior_mark_prices,
                cost_per_turnover=stress_cost_per_turnover,
                config=config,
            ),
        }
    return result
