"""Causal feature and execution model for preregistered Alpha #10."""

from __future__ import annotations

from math import sqrt
from typing import Any

import numpy as np
import pandas as pd


FIVE_MINUTES = pd.Timedelta(minutes=5)
HOLDING_PERIOD = pd.Timedelta(hours=8)


def _utc_index(frame: pd.DataFrame, column: str = "date") -> pd.DataFrame:
    result = frame.copy()
    if column in result.columns:
        result[column] = pd.to_datetime(result[column], utc=True, errors="raise")
        result = result.set_index(column)
    elif not isinstance(result.index, pd.DatetimeIndex):
        raise ValueError("data must have a datetime column or DatetimeIndex")
    if result.index.tz is None:
        result.index = result.index.tz_localize("UTC")
    else:
        result.index = result.index.tz_convert("UTC")
    result.index = result.index.as_unit("ns")
    if result.index.has_duplicates or not result.index.is_monotonic_increasing:
        raise ValueError("timestamps must be unique and increasing")
    return result


def require_regular_five_minute_data(frame: pd.DataFrame, *, label: str) -> None:
    if len(frame) < 2:
        raise ValueError(f"{label} has too few observations")
    deltas = frame.index.to_series().diff().dropna()
    bad = deltas[deltas != FIVE_MINUTES]
    if not bad.empty:
        raise ValueError(f"{label} has {len(bad)} non-5m timestamp gaps")


def _funding_sums(funding: pd.DataFrame, decisions: pd.DatetimeIndex) -> pd.Series:
    rates = funding["funding_rate"].astype(float)
    times = rates.index.as_unit("ns").asi8
    values = rates.to_numpy(dtype=float)
    cumulative = np.concatenate(([0.0], np.cumsum(values)))
    output = []
    for timestamp in decisions:
        timestamp = timestamp.as_unit("ns")
        right = np.searchsorted(times, timestamp.value, side="right")
        left = np.searchsorted(times, timestamp.value - 86_400_000_000_000, side="right")
        output.append(float(cumulative[right] - cumulative[left]))
    return pd.Series(output, index=decisions, dtype=float)


def build_decision_frame(
    metrics: pd.DataFrame,
    candles: pd.DataFrame,
    funding: pd.DataFrame,
) -> pd.DataFrame:
    """Build features using only observations available by each delayed entry."""
    metrics = _utc_index(metrics, "create_time" if "create_time" in metrics.columns else "date")
    candles = _utc_index(candles)
    funding = _utc_index(funding)
    require_regular_five_minute_data(metrics, label="metrics")
    require_regular_five_minute_data(candles, label="candles")
    if "sum_open_interest" not in metrics or "close" not in candles or "open" not in candles:
        raise ValueError("metrics or candle columns are incomplete")
    if "funding_rate" not in funding:
        raise ValueError("funding_rate column is missing")

    oi = pd.to_numeric(metrics["sum_open_interest"], errors="raise")
    close = pd.to_numeric(candles["close"], errors="raise")
    log_return = np.log(close).diff()
    realized_volatility = log_return.pow(2).rolling(288, min_periods=288).sum().pow(0.5)
    decision_index = metrics.index[
        (metrics.index.minute == 0) & metrics.index.hour.isin([0, 8, 16])
    ]
    frame = pd.DataFrame(index=decision_index)
    frame.index.name = "decision_time"
    frame["open_interest_change_24h"] = (oi / oi.shift(288) - 1.0).reindex(decision_index)
    frame["price_return_24h"] = (close / close.shift(288) - 1.0).reindex(decision_index)
    frame["realized_volatility_24h"] = realized_volatility.reindex(decision_index)
    frame["open_interest_threshold"] = (
        frame["open_interest_change_24h"]
        .rolling(1095, min_periods=540)
        .quantile(0.90)
        .shift(1)
    )
    frame["realized_volatility_threshold"] = (
        frame["realized_volatility_24h"].rolling(90, min_periods=90).median().shift(1)
    )
    frame["funding_sum_24h"] = _funding_sums(funding, decision_index)
    entry_index = decision_index + FIVE_MINUTES
    planned_exit_index = entry_index + HOLDING_PERIOD
    frame["entry_time"] = entry_index
    frame["planned_exit_time"] = planned_exit_index
    frame["entry_open"] = candles["open"].reindex(entry_index).to_numpy()
    frame["planned_exit_open"] = candles["open"].reindex(planned_exit_index).to_numpy()
    frame["forward_return_8h"] = frame["planned_exit_open"] / frame["entry_open"] - 1.0
    feature_columns = [
        "open_interest_change_24h",
        "price_return_24h",
        "realized_volatility_24h",
        "open_interest_threshold",
        "realized_volatility_threshold",
        "funding_sum_24h",
        "entry_open",
    ]
    frame["features_complete"] = frame[feature_columns].notna().all(axis=1)
    frame["signal"] = (
        frame["features_complete"]
        & (frame["open_interest_change_24h"] > frame["open_interest_threshold"])
        & (frame["price_return_24h"] > 0)
        & (frame["funding_sum_24h"] > 0)
        & (frame["realized_volatility_24h"] > frame["realized_volatility_threshold"])
    )
    return frame


def simulate_trades(
    decisions: pd.DataFrame,
    candles: pd.DataFrame,
    funding: pd.DataFrame,
    *,
    start: pd.Timestamp,
    end_exclusive: pd.Timestamp,
    fee_per_side_bps: float,
    slippage_per_side_bps: float,
    stop_above_entry: float = 0.04,
) -> list[dict[str, Any]]:
    candles = _utc_index(candles)
    funding = _utc_index(funding)
    fee = fee_per_side_bps / 10_000.0
    slippage = slippage_per_side_bps / 10_000.0
    selected = decisions[
        decisions["signal"]
        & (decisions.index >= start)
        & (decisions.index < end_exclusive)
        & (decisions["planned_exit_time"] <= end_exclusive)
    ]
    trades: list[dict[str, Any]] = []
    for decision_time, row in selected.iterrows():
        entry_time = pd.Timestamp(row["entry_time"])
        planned_exit_time = pd.Timestamp(row["planned_exit_time"])
        if entry_time not in candles.index or planned_exit_time not in candles.index:
            raise ValueError("trade entry or planned exit candle is missing")
        raw_entry = float(candles.at[entry_time, "open"])
        stop_price = raw_entry * (1.0 + stop_above_entry)
        path = candles.loc[(candles.index >= entry_time) & (candles.index < planned_exit_time)]
        stop_hits = path[pd.to_numeric(path["high"], errors="raise") >= stop_price]
        if stop_hits.empty:
            exit_time = planned_exit_time
            raw_exit = float(candles.at[exit_time, "open"])
            exit_reason = "time_exit"
        else:
            exit_time = stop_hits.index[0]
            raw_exit = max(stop_price, float(candles.at[exit_time, "open"]))
            exit_reason = "protective_stop"
        funding_rate = float(
            funding.loc[(funding.index > entry_time) & (funding.index < exit_time), "funding_rate"].sum()
        )
        entry_exec = raw_entry * (1.0 - slippage)
        exit_exec = raw_exit * (1.0 + slippage)
        raw_price_return = (raw_entry - raw_exit) / raw_entry
        execution_price_return = (entry_exec - exit_exec) / entry_exec
        trading_fees = fee + fee * (exit_exec / entry_exec)
        net_return = execution_price_return - trading_fees + funding_rate
        trades.append(
            {
                "decision_time": decision_time.isoformat(),
                "entry_time": entry_time.isoformat(),
                "exit_time": exit_time.isoformat(),
                "exit_reason": exit_reason,
                "raw_entry": raw_entry,
                "raw_exit": raw_exit,
                "raw_price_return": raw_price_return,
                "execution_price_return": execution_price_return,
                "slippage_drag": raw_price_return - execution_price_return,
                "trading_fee_drag": trading_fees,
                "funding_return": funding_rate,
                "net_return": net_return,
                "three_percent_crash": bool(float(row["forward_return_8h"]) <= -0.03),
            }
        )
    return trades


def summarize_trades(
    trades: list[dict[str, Any]],
    decisions: pd.DataFrame,
    *,
    start: pd.Timestamp,
    end_exclusive: pd.Timestamp,
) -> dict[str, Any]:
    returns = np.array([float(item["net_return"]) for item in trades], dtype=float)
    positive = returns[returns > 0]
    negative = returns[returns < 0]
    equity = np.cumprod(1.0 + returns) if len(returns) else np.array([], dtype=float)
    if len(equity):
        wealth = np.concatenate(([1.0], equity))
        running_peak = np.maximum.accumulate(wealth)
        maximum_drawdown = float(np.max(1.0 - wealth / running_peak))
        compounded_return = float(equity[-1] - 1.0)
    else:
        maximum_drawdown = None
        compounded_return = 0.0
    profit_factor = float(positive.sum() / abs(negative.sum())) if len(negative) else (1_000_000_000.0 if len(positive) else None)
    gross_positive = float(positive.sum())
    concentration = (
        float(np.sort(positive)[-3:].sum() / gross_positive) if gross_positive > 0 else None
    )
    eligible_decisions = decisions[
        (decisions.index >= start)
        & (decisions.index < end_exclusive)
        & decisions["forward_return_8h"].notna()
    ]
    baseline_crash_rate = float((eligible_decisions["forward_return_8h"] <= -0.03).mean()) if len(eligible_decisions) else None
    signal_crash_rate = float(np.mean([item["three_percent_crash"] for item in trades])) if trades else None
    precision_lift = (
        signal_crash_rate / baseline_crash_rate
        if signal_crash_rate is not None and baseline_crash_rate not in (None, 0.0)
        else None
    )
    return {
        "trades": len(trades),
        "wins": int(len(positive)),
        "losses": int(len(negative)),
        "compounded_net_return": compounded_return,
        "sum_net_return": float(returns.sum()) if len(returns) else 0.0,
        "mean_net_return": float(returns.mean()) if len(returns) else None,
        "profit_factor": profit_factor,
        "maximum_drawdown": maximum_drawdown,
        "top_three_winner_profit_concentration": concentration,
        "three_percent_crash_precision": signal_crash_rate,
        "unconditional_three_percent_crash_rate": baseline_crash_rate,
        "three_percent_crash_precision_lift": precision_lift,
        "price_return_sum": float(sum(item["raw_price_return"] for item in trades)),
        "trading_fee_drag_sum": float(sum(item["trading_fee_drag"] for item in trades)),
        "slippage_drag_sum": float(sum(item["slippage_drag"] for item in trades)),
        "funding_return_sum": float(sum(item["funding_return"] for item in trades)),
        "stop_exits": sum(item["exit_reason"] == "protective_stop" for item in trades),
        "time_exits": sum(item["exit_reason"] == "time_exit" for item in trades),
    }


def development_gate(base: dict[str, Any], double_fee: dict[str, Any], slippage_stress: dict[str, Any]) -> dict[str, bool]:
    return {
        "natural_closed_trades_minimum": int(base["trades"]) >= 60,
        "net_return_positive": float(base["compounded_net_return"]) > 0,
        "profit_factor_minimum": base["profit_factor"] is not None and float(base["profit_factor"]) >= 1.15,
        "double_fee_net_positive": float(double_fee["compounded_net_return"]) > 0,
        "five_bps_per_side_slippage_net_positive": float(slippage_stress["compounded_net_return"]) > 0,
        "maximum_drawdown_maximum": base["maximum_drawdown"] is not None and float(base["maximum_drawdown"]) <= 0.20,
        "top_three_winner_profit_concentration_maximum": base["top_three_winner_profit_concentration"] is not None
        and float(base["top_three_winner_profit_concentration"]) <= 0.50,
        "three_percent_crash_precision_lift_minimum": base["three_percent_crash_precision_lift"] is not None
        and float(base["three_percent_crash_precision_lift"]) >= 1.50,
    }
