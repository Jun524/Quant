"""Fixed causal execution model for preregistered Alpha #11."""

from __future__ import annotations

from dataclasses import dataclass
from math import prod
import random
from typing import Any, Iterable

import numpy as np


WINDOW_MS = 10_000
ENTRY_BUFFER_MS = 1_000
HOLD_MS = 12 * 60 * 60 * 1000


@dataclass
class _Decision:
    boundary_ms: int
    signed_quantity: float = 0.0
    total_quantity: float = 0.0
    entered: bool = False


def _buyer_maker(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).lower() == "true":
        return True
    if str(value).lower() == "false":
        return False
    raise ValueError("is_buyer_maker must be boolean")


def simulate_trade_stream(
    trades: Iterable[dict[str, Any]],
    *,
    boundaries_ms: list[int],
    funding_events: list[tuple[int, float, float]],
    fee_per_side_bps: float,
    slippage_per_side_bps: float,
    stop_pct: float = 0.04,
) -> list[dict[str, Any]]:
    """Stream aggregate trades once; no future observation can affect an entry."""
    boundaries = sorted(boundaries_ms)
    boundary_pointer = 0
    decision: _Decision | None = None
    position: dict[str, Any] | None = None
    results: list[dict[str, Any]] = []
    prior_id: int | None = None
    prior_time: int | None = None

    def close_position(timestamp: int, raw_exit: float, reason: str) -> None:
        nonlocal position
        if position is None:
            return
        direction = int(position["direction"])
        raw_entry = float(position["raw_entry"])
        fee = fee_per_side_bps / 10_000.0
        slippage = slippage_per_side_bps / 10_000.0
        if direction > 0:
            entry_exec = raw_entry * (1.0 + slippage)
            exit_exec = raw_exit * (1.0 - slippage)
            raw_return = raw_exit / raw_entry - 1.0
            execution_return = exit_exec / entry_exec - 1.0
        else:
            entry_exec = raw_entry * (1.0 - slippage)
            exit_exec = raw_exit * (1.0 + slippage)
            raw_return = (raw_entry - raw_exit) / raw_entry
            execution_return = (entry_exec - exit_exec) / entry_exec
        trading_fees = fee + fee * (exit_exec / entry_exec)
        applied_funding: list[float] = []
        entry_time_ms = int(position["entry_time_ms"])
        for event_time, rate, mark_price in funding_events:
            component = -direction * rate * mark_price / entry_exec
            strictly_inside = entry_time_ms < event_time < timestamp
            ambiguous_boundary_cost = event_time in {entry_time_ms, timestamp} and component < 0
            if strictly_inside or ambiguous_boundary_cost:
                applied_funding.append(component)
        funding_return = sum(applied_funding)
        net_return = execution_return - trading_fees + funding_return
        if direction > 0:
            mae = float(position["min_price"]) / raw_entry - 1.0
            mfe = float(position["max_price"]) / raw_entry - 1.0
        else:
            mae = (raw_entry - float(position["max_price"])) / raw_entry
            mfe = (raw_entry - float(position["min_price"])) / raw_entry
        results.append(
            {
                "boundary_ms": int(position["boundary_ms"]),
                "entry_time_ms": int(position["entry_time_ms"]),
                "exit_time_ms": timestamp,
                "direction": "long" if direction > 0 else "short",
                "order_imbalance": float(position["order_imbalance"]),
                "raw_entry": raw_entry,
                "raw_exit": raw_exit,
                "exit_reason": reason,
                "raw_price_return": raw_return,
                "execution_price_return": execution_return,
                "slippage_drag": raw_return - execution_return,
                "trading_fee_drag": trading_fees,
                "funding_return": funding_return,
                "funding_events": len(applied_funding),
                "net_return": net_return,
                "mae": mae,
                "mfe": mfe,
                "holding_hours": (timestamp - int(position["entry_time_ms"])) / 3_600_000.0,
            }
        )
        position = None

    for row in trades:
        trade_id = int(row["agg_trade_id"])
        timestamp = int(row["transact_time"])
        price = float(row["price"])
        quantity = float(row["quantity"])
        if quantity <= 0 or price <= 0:
            raise ValueError("trade price and quantity must be positive")
        if prior_id is not None and trade_id != prior_id + 1:
            raise ValueError("aggregate-trade IDs are not continuous")
        if prior_time is not None and timestamp < prior_time:
            raise ValueError("aggregate-trade timestamps are not monotonic")
        prior_id, prior_time = trade_id, timestamp

        while boundary_pointer < len(boundaries) and boundaries[boundary_pointer] <= timestamp:
            if decision is not None and not decision.entered:
                raise ValueError("a scheduled decision window has no executable post-window trade")
            decision = _Decision(boundaries[boundary_pointer])
            boundary_pointer += 1

        if decision is not None and decision.boundary_ms <= timestamp < decision.boundary_ms + WINDOW_MS:
            signed = -quantity if _buyer_maker(row["is_buyer_maker"]) else quantity
            decision.signed_quantity += signed
            decision.total_quantity += quantity

        if position is not None:
            position["min_price"] = min(float(position["min_price"]), price)
            position["max_price"] = max(float(position["max_price"]), price)
            planned_exit = int(position["planned_exit_ms"])
            direction = int(position["direction"])
            stop_price = float(position["stop_price"])
            if timestamp >= planned_exit:
                close_position(timestamp, price, "time_exit")
            elif direction > 0 and price <= stop_price:
                close_position(timestamp, min(price, stop_price), "protective_stop")
            elif direction < 0 and price >= stop_price:
                close_position(timestamp, max(price, stop_price), "protective_stop")

        if (
            decision is not None
            and not decision.entered
            and timestamp >= decision.boundary_ms + WINDOW_MS + ENTRY_BUFFER_MS
        ):
            decision.entered = True
            if decision.total_quantity > 0 and decision.signed_quantity != 0:
                if position is not None:
                    raise ValueError("maximum concurrent positions would be exceeded")
                imbalance = decision.signed_quantity / decision.total_quantity
                direction = 1 if imbalance > 0 else -1
                position = {
                    "boundary_ms": decision.boundary_ms,
                    "entry_time_ms": timestamp,
                    "planned_exit_ms": decision.boundary_ms + HOLD_MS,
                    "direction": direction,
                    "order_imbalance": imbalance,
                    "raw_entry": price,
                    "min_price": price,
                    "max_price": price,
                    "stop_price": price * (1.0 - stop_pct if direction > 0 else 1.0 + stop_pct),
                }

        if boundary_pointer >= len(boundaries) and decision is not None and decision.entered and position is None:
            break
    return results


def summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    returns = np.array([float(item["net_return"]) for item in trades], dtype=float)
    positive = returns[returns > 0]
    negative = returns[returns < 0]
    wealth = np.concatenate(([1.0], np.cumprod(1.0 + returns))) if len(returns) else np.array([1.0])
    peak = np.maximum.accumulate(wealth)
    long_returns = [float(item["net_return"]) for item in trades if item["direction"] == "long"]
    short_returns = [float(item["net_return"]) for item in trades if item["direction"] == "short"]
    gross_wins = float(positive.sum())
    return {
        "trades": len(trades),
        "wins": int(len(positive)),
        "losses": int(len(negative)),
        "compounded_net_return": float(wealth[-1] - 1.0),
        "sum_net_return": float(returns.sum()) if len(returns) else 0.0,
        "profit_factor": float(positive.sum() / abs(negative.sum())) if len(negative) else None,
        "maximum_drawdown": float(np.max(1.0 - wealth / peak)),
        "top_three_winner_profit_concentration": (
            float(np.sort(positive)[-3:].sum() / gross_wins) if gross_wins > 0 else None
        ),
        "long_trades": len(long_returns),
        "long_expectancy": float(np.mean(long_returns)) if long_returns else None,
        "short_trades": len(short_returns),
        "short_expectancy": float(np.mean(short_returns)) if short_returns else None,
        "price_return_sum": float(sum(float(item["raw_price_return"]) for item in trades)),
        "trading_fee_drag_sum": float(sum(float(item["trading_fee_drag"]) for item in trades)),
        "slippage_drag_sum": float(sum(float(item["slippage_drag"]) for item in trades)),
        "funding_return_sum": float(sum(float(item["funding_return"]) for item in trades)),
        "largest_single_loss": float(returns.min()) if len(returns) else None,
        "mae_mean": float(np.mean([float(item["mae"]) for item in trades])) if trades else None,
        "mae_worst": float(min(float(item["mae"]) for item in trades)) if trades else None,
        "mfe_mean": float(np.mean([float(item["mfe"]) for item in trades])) if trades else None,
        "median_holding_hours": float(np.median([float(item["holding_hours"]) for item in trades]))
        if trades
        else None,
        "stop_exits": sum(item["exit_reason"] == "protective_stop" for item in trades),
        "time_exits": sum(item["exit_reason"] == "time_exit" for item in trades),
    }


def reprice_trades(
    trades: list[dict[str, Any]], *, fee_per_side_bps: float, slippage_per_side_bps: float
) -> list[dict[str, Any]]:
    """Replay only fixed execution costs; signals, entries, exits, and funding stay sealed."""
    fee = fee_per_side_bps / 10_000.0
    slippage = slippage_per_side_bps / 10_000.0
    output: list[dict[str, Any]] = []
    for item in trades:
        result = dict(item)
        raw_entry = float(item["raw_entry"])
        raw_exit = float(item["raw_exit"])
        if item["direction"] == "long":
            entry_exec = raw_entry * (1.0 + slippage)
            exit_exec = raw_exit * (1.0 - slippage)
            raw_return = raw_exit / raw_entry - 1.0
            execution_return = exit_exec / entry_exec - 1.0
        elif item["direction"] == "short":
            entry_exec = raw_entry * (1.0 - slippage)
            exit_exec = raw_exit * (1.0 + slippage)
            raw_return = (raw_entry - raw_exit) / raw_entry
            execution_return = (entry_exec - exit_exec) / entry_exec
        else:
            raise ValueError("trade direction must be long or short")
        trading_fees = fee + fee * (exit_exec / entry_exec)
        result.update(
            {
                "raw_price_return": raw_return,
                "execution_price_return": execution_return,
                "slippage_drag": raw_return - execution_return,
                "trading_fee_drag": trading_fees,
                "net_return": execution_return
                - trading_fees
                + float(item["funding_return"]),
            }
        )
        output.append(result)
    return output


def block_bootstrap_loss_probability(
    trades: list[dict[str, Any]], *, block_trades: int = 16, paths: int = 5000, seed: int = 11
) -> float | None:
    returns = [float(item["net_return"]) for item in trades]
    if len(returns) < block_trades:
        return None
    blocks = [returns[index : index + block_trades] for index in range(len(returns) - block_trades + 1)]
    generator = random.Random(seed)
    losses = 0
    for _ in range(paths):
        sample: list[float] = []
        while len(sample) < len(returns):
            sample.extend(generator.choice(blocks))
        compounded = prod(1.0 + value for value in sample[: len(returns)]) - 1.0
        losses += compounded <= 0
    return losses / paths


def development_gate(
    base: dict[str, Any],
    double_fee: dict[str, Any],
    slippage_stress: dict[str, Any],
    *,
    complete_days: int,
    bootstrap_loss_probability: float | None,
) -> dict[str, bool]:
    return {
        "complete_utc_days_minimum": complete_days >= 60,
        "natural_closed_trades_minimum": int(base["trades"]) >= 100,
        "net_return_positive": float(base["compounded_net_return"]) > 0,
        "profit_factor_minimum": base["profit_factor"] is not None and float(base["profit_factor"]) >= 1.15,
        "double_fee_net_positive": float(double_fee["compounded_net_return"]) > 0,
        "five_bps_per_side_slippage_net_positive": float(slippage_stress["compounded_net_return"]) > 0,
        "maximum_drawdown_maximum": float(base["maximum_drawdown"]) <= 0.20,
        "top_three_winner_profit_concentration_maximum": base[
            "top_three_winner_profit_concentration"
        ]
        is not None
        and float(base["top_three_winner_profit_concentration"]) <= 0.50,
        "long_expectancy_positive": base["long_expectancy"] is not None
        and float(base["long_expectancy"]) > 0,
        "short_expectancy_positive": base["short_expectancy"] is not None
        and float(base["short_expectancy"]) > 0,
        "eight_day_block_bootstrap_probability_of_loss_maximum": bootstrap_loss_probability
        is not None
        and bootstrap_loss_probability <= 0.10,
    }
