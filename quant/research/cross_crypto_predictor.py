"""Causal, data-agnostic kernel for a deferred cross-crypto predictor.

This module deliberately contains no data downloader or performance evaluator.
Its regularization rule is selected only from chronological one-step return
MSE, never portfolio PnL.  It can be unit-tested with synthetic data while
Experiment 0015 remains unopened and real return acquisition is barred.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class AdaptiveLassoKernelConfig:
    training_pairs: int = 720
    minimum_symbols: int = 30
    portfolio_buckets: int = 5
    rebalance_minutes: int = 10
    entry_latency_minutes: int = 1
    holding_minutes: int = 10
    diagnostic_markouts_minutes: tuple[int, ...] = (1, 5, 10)
    adaptive_power: float = 1.0
    weight_floor: float = 1e-8
    maximum_iterations: int = 10_000
    convergence_tolerance: float = 1e-10
    inner_training_pairs: int = 600
    penalty_fractions: tuple[float, ...] = (
        1.0,
        0.5,
        0.2,
        0.1,
        0.05,
        0.02,
        0.01,
        0.005,
        0.002,
        0.001,
        0.0,
    )


def _utc_timestamp(value: str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


def execution_window(
    decision_at: str | pd.Timestamp,
    *,
    config: AdaptiveLassoKernelConfig | None = None,
) -> dict[str, Any]:
    """Return the fixed non-overlapping execution window for a decision."""
    config = config or AdaptiveLassoKernelConfig()
    decision = _utc_timestamp(decision_at)
    if (
        decision.second != 0
        or decision.microsecond != 0
        or int(decision.timestamp() // 60) % config.rebalance_minutes != 0
    ):
        raise ValueError("decision timestamp is outside the 10-minute UTC epoch grid")
    if (
        config.rebalance_minutes != 10
        or config.entry_latency_minutes != 1
        or config.holding_minutes != 10
        or config.diagnostic_markouts_minutes != (1, 5, 10)
    ):
        raise ValueError("execution timing differs from the effective Alpha 015 amendment")
    entry = decision + pd.to_timedelta(config.entry_latency_minutes, unit="min")
    exit_at = entry + pd.to_timedelta(config.holding_minutes, unit="min")
    next_decision = decision + pd.to_timedelta(config.rebalance_minutes, unit="min")
    if entry.date() != decision.date() or exit_at.date() != decision.date():
        raise ValueError("execution window crosses its UTC evaluation-day boundary")
    return {
        "decision_at": decision.isoformat(),
        "earliest_entry_at": entry.isoformat(),
        "exit_at": exit_at.isoformat(),
        "next_decision_at": next_decision.isoformat(),
        "overlap": False,
        "gate_horizon_minutes": config.holding_minutes,
        "diagnostic_markouts_minutes": list(config.diagnostic_markouts_minutes),
        "daily_boundary_policy": "entry_and_exit_same_utc_date_as_decision",
    }


def _validate_past_panel(
    returns: pd.DataFrame,
    *,
    decision_at: pd.Timestamp,
    config: AdaptiveLassoKernelConfig,
) -> pd.DataFrame:
    """Validate and return only information available at the decision time."""
    if not isinstance(returns.index, pd.DatetimeIndex):
        raise ValueError("returns must use a DatetimeIndex")
    if returns.index.tz is None:
        raise ValueError("returns index must be timezone-aware")
    past = returns.loc[returns.index <= decision_at].copy()
    if decision_at not in past.index:
        raise ValueError("decision timestamp is missing")
    if past.index.has_duplicates or not past.index.is_monotonic_increasing:
        raise ValueError("returns timestamps must be unique and chronological")
    if len(set(map(str, past.columns))) != len(past.columns):
        raise ValueError("return symbols must be unique")
    if len(past.columns) < config.minimum_symbols:
        raise ValueError("cross-section is smaller than the activation minimum")
    required_rows = config.training_pairs + 1
    if len(past) < required_rows:
        raise ValueError("insufficient causal rows for the rolling training window")
    relevant = past.iloc[-required_rows:]
    expected = pd.date_range(relevant.index[0], decision_at, freq="1min")
    if len(relevant) != required_rows or len(relevant.index.symmetric_difference(expected)):
        raise ValueError("rolling training window contains a missing or irregular minute")
    values = relevant.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("rolling training window contains non-finite returns")
    return relevant


def causal_training_matrices(
    returns: pd.DataFrame,
    *,
    decision_at: str | pd.Timestamp,
    target: str,
    config: AdaptiveLassoKernelConfig | None = None,
) -> dict[str, Any]:
    """Build X_t -> y_(t+1) pairs ending at y_t and a feature row at t."""
    config = config or AdaptiveLassoKernelConfig()
    decision = _utc_timestamp(decision_at)
    relevant = _validate_past_panel(returns, decision_at=decision, config=config)
    if target not in relevant.columns:
        raise ValueError(f"unknown target symbol: {target}")
    # For decision t, training features end at t-1 and training targets end at
    # t.  The only feature used for the live forecast is the row at t.
    raw_x = relevant.iloc[:-1].to_numpy(dtype=float)
    raw_y = relevant[target].iloc[1:].to_numpy(dtype=float)
    feature_now = relevant.iloc[-1].to_numpy(dtype=float)
    means = raw_x.mean(axis=0)
    scales = raw_x.std(axis=0, ddof=0)
    if (scales <= 0).any() or not np.isfinite(scales).all():
        raise ValueError("at least one predictor has zero rolling variance")
    standardized_x = (raw_x - means) / scales
    standardized_now = (feature_now - means) / scales
    return {
        "x": standardized_x,
        "y": raw_y,
        "x_now": standardized_now,
        "symbols": tuple(map(str, relevant.columns)),
        "feature_start_at": relevant.index[0].isoformat(),
        "feature_end_at": relevant.index[-2].isoformat(),
        "target_start_at": relevant.index[1].isoformat(),
        "target_end_at": relevant.index[-1].isoformat(),
        "decision_at": decision.isoformat(),
        "future_rows_read": 0,
    }


def _soft_threshold(value: float, threshold: float) -> float:
    if value > threshold:
        return value - threshold
    if value < -threshold:
        return value + threshold
    return 0.0


def fit_adaptive_lasso(
    x: np.ndarray,
    y: np.ndarray,
    *,
    penalty: float,
    config: AdaptiveLassoKernelConfig | None = None,
) -> dict[str, Any]:
    """Fit weighted L1 regression by deterministic coordinate descent.

    The caller must supply a preregistered penalty.  This function never picks
    the value from trading performance.
    """
    config = config or AdaptiveLassoKernelConfig()
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if x.ndim != 2 or y.ndim != 1 or len(x) != len(y):
        raise ValueError("x and y shapes are incompatible")
    if penalty < 0 or not np.isfinite(penalty):
        raise ValueError("penalty must be finite and non-negative")
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("adaptive LASSO inputs must be finite")
    intercept = float(y.mean())
    centered_y = y - intercept
    initial, *_ = np.linalg.lstsq(x, centered_y, rcond=None)
    weights = 1.0 / np.maximum(np.abs(initial), config.weight_floor) ** config.adaptive_power
    coefficients = np.zeros(x.shape[1], dtype=float)
    norms = np.mean(x * x, axis=0)
    if (norms <= 0).any():
        raise ValueError("adaptive LASSO predictor norm is zero")
    residual = centered_y.copy()
    converged = False
    iterations = 0
    for iteration in range(1, config.maximum_iterations + 1):
        maximum_change = 0.0
        for column in range(x.shape[1]):
            old = coefficients[column]
            residual += x[:, column] * old
            rho = float(np.mean(x[:, column] * residual))
            new = _soft_threshold(rho, penalty * weights[column]) / norms[column]
            coefficients[column] = new
            residual -= x[:, column] * new
            maximum_change = max(maximum_change, abs(new - old))
        iterations = iteration
        if maximum_change <= config.convergence_tolerance:
            converged = True
            break
    if not converged:
        raise RuntimeError("adaptive LASSO coordinate descent did not converge")
    return {
        "intercept": intercept,
        "coefficients": coefficients,
        "adaptive_weights": weights,
        "selected_predictors": int(np.count_nonzero(coefficients)),
        "iterations": iterations,
        "converged": converged,
        "penalty": float(penalty),
    }


def select_penalty_by_forward_mse(
    x: np.ndarray,
    y: np.ndarray,
    *,
    config: AdaptiveLassoKernelConfig | None = None,
) -> dict[str, Any]:
    """Select lambda on a chronological inner holdout, never on portfolio PnL."""
    config = config or AdaptiveLassoKernelConfig()
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    split = int(config.inner_training_pairs)
    if x.ndim != 2 or y.ndim != 1 or len(x) != len(y):
        raise ValueError("x and y shapes are incompatible")
    if split < 2 or split >= len(x):
        raise ValueError("inner training split must leave a chronological holdout")
    train_x_raw, validation_x_raw = x[:split], x[split:]
    train_y, validation_y = y[:split], y[split:]
    means = train_x_raw.mean(axis=0)
    scales = train_x_raw.std(axis=0, ddof=0)
    if (scales <= 0).any() or not np.isfinite(scales).all():
        raise ValueError("inner training predictor has zero variance")
    train_x = (train_x_raw - means) / scales
    validation_x = (validation_x_raw - means) / scales
    centered_y = train_y - train_y.mean()
    initial, *_ = np.linalg.lstsq(train_x, centered_y, rcond=None)
    weights = 1.0 / np.maximum(np.abs(initial), config.weight_floor) ** config.adaptive_power
    gradients = np.abs(np.mean(train_x * centered_y[:, None], axis=0))
    maximum_penalty = float(np.max(gradients / weights))
    candidates = sorted(
        {maximum_penalty * float(fraction) for fraction in config.penalty_fractions},
        reverse=True,
    )
    evaluations: list[dict[str, float | int]] = []
    for penalty in candidates:
        fitted = fit_adaptive_lasso(train_x, train_y, penalty=penalty, config=config)
        prediction = fitted["intercept"] + validation_x @ fitted["coefficients"]
        mse = float(np.mean((validation_y - prediction) ** 2))
        evaluations.append(
            {
                "penalty": penalty,
                "validation_mse": mse,
                "selected_predictors": int(fitted["selected_predictors"]),
            }
        )
    # Prefer the sparser/larger penalty when floating-point MSE ties exactly.
    best = min(evaluations, key=lambda item: (item["validation_mse"], -item["penalty"]))
    return {
        "penalty": float(best["penalty"]),
        "validation_mse": float(best["validation_mse"]),
        "inner_training_pairs": split,
        "inner_validation_pairs": len(x) - split,
        "selection_objective": "one_step_return_mse_not_portfolio_pnl",
        "evaluations": evaluations,
    }


def forecast_cross_section(
    returns: pd.DataFrame,
    *,
    decision_at: str | pd.Timestamp,
    penalty: float | None,
    config: AdaptiveLassoKernelConfig | None = None,
) -> dict[str, Any]:
    """Produce one-minute-ahead forecasts using no row after decision_at."""
    config = config or AdaptiveLassoKernelConfig()
    timing = execution_window(decision_at, config=config)
    forecasts: dict[str, float] = {}
    selected: dict[str, int] = {}
    penalties: dict[str, float] = {}
    penalty_receipts: dict[str, dict[str, Any]] = {}
    receipt: dict[str, Any] | None = None
    for target in map(str, returns.columns):
        matrices = causal_training_matrices(
            returns, decision_at=decision_at, target=target, config=config
        )
        if penalty is not None:
            chosen_penalty = float(penalty)
        else:
            selection = select_penalty_by_forward_mse(
                matrices["x"], matrices["y"], config=config
            )
            chosen_penalty = float(selection["penalty"])
            penalty_receipts[target] = {
                key: selection[key]
                for key in (
                    "penalty",
                    "validation_mse",
                    "inner_training_pairs",
                    "inner_validation_pairs",
                    "selection_objective",
                )
            }
        model = fit_adaptive_lasso(
            matrices["x"], matrices["y"], penalty=chosen_penalty, config=config
        )
        forecasts[target] = float(
            model["intercept"] + matrices["x_now"] @ model["coefficients"]
        )
        selected[target] = int(model["selected_predictors"])
        penalties[target] = chosen_penalty
        if receipt is None:
            receipt = {
                key: matrices[key]
                for key in (
                    "feature_start_at",
                    "feature_end_at",
                    "target_start_at",
                    "target_end_at",
                    "decision_at",
                    "future_rows_read",
                )
            }
    return {
        "forecasts": dict(sorted(forecasts.items())),
        "selected_predictors": dict(sorted(selected.items())),
        "penalties_by_target": dict(sorted(penalties.items())),
        "penalty_selection_receipts": dict(sorted(penalty_receipts.items())),
        "receipt": receipt,
        "configuration": config.__dict__,
        "penalty_selection": (
            "fixed_external" if penalty is not None else "chronological_inner_mse"
        ),
        "execution_window": timing,
    }


def quintile_target_weights(
    forecasts: Mapping[str, float],
    *,
    config: AdaptiveLassoKernelConfig | None = None,
) -> dict[str, float]:
    """Return the preregistered gross-one, net-zero quintile portfolio."""
    config = config or AdaptiveLassoKernelConfig()
    clean = {str(symbol): float(value) for symbol, value in forecasts.items()}
    if len(clean) < config.minimum_symbols or not all(np.isfinite(value) for value in clean.values()):
        raise ValueError("forecast cross-section is incomplete or non-finite")
    count = len(clean) // config.portfolio_buckets
    if count < 1:
        raise ValueError("portfolio bucket contains no instrument")
    ordered = sorted(clean.items(), key=lambda item: (item[1], item[0]))
    shorts = [symbol for symbol, _ in ordered[:count]]
    # Taking the opposite end of the same total ordering prevents overlap even
    # when every forecast ties.  Exact ties are resolved alphabetically.
    longs = [symbol for symbol, _ in ordered[-count:]]
    if set(longs) & set(shorts):
        raise ValueError("long and short portfolio buckets overlap")
    weights = {symbol: 0.5 / count for symbol in longs}
    weights.update({symbol: -0.5 / count for symbol in shorts})
    return dict(sorted(weights.items()))


def decile_target_weights(
    forecasts: Mapping[str, float],
    *,
    config: AdaptiveLassoKernelConfig | None = None,
) -> dict[str, float]:
    """Backward-compatible name; the effective amendment now uses quintiles.

    The function delegates to the effective configuration and must not be used
    to resurrect the superseded decile variant as an unregistered trial.
    """
    return quintile_target_weights(forecasts, config=config)


def realized_turnover(
    previous_weights: Mapping[str, float],
    target_weights: Mapping[str, float],
) -> float:
    symbols = set(previous_weights) | set(target_weights)
    turnover = sum(
        abs(float(target_weights.get(symbol, 0.0)) - float(previous_weights.get(symbol, 0.0)))
        for symbol in symbols
    )
    if not np.isfinite(turnover):
        raise ValueError("portfolio turnover is non-finite")
    return float(turnover)


def target_schedule_turnover_floor(
    targets: list[Mapping[str, float]],
) -> dict[str, Any]:
    """Return the no-price-drift floor for a target schedule.

    The full simulator must use marked pretrade weights.  This helper is only
    a deterministic lower-bound diagnostic for synthetic or static prices.
    """
    if not targets:
        raise ValueError("daily target sequence is empty")
    previous: Mapping[str, float] = {}
    transitions: list[float] = []
    for target in targets:
        transitions.append(realized_turnover(previous, target))
        previous = target
    final_exit = realized_turnover(previous, {})
    return {
        "entry_and_rebalance_turnovers": transitions,
        "final_exit_turnover": final_exit,
        "total_turnover": float(sum(transitions) + final_exit),
        "intermediate_policy": "target_schedule_floor_full_replay_uses_marked_weights",
    }


def execution_cost_fraction(
    previous_weights: Mapping[str, float],
    target_weights: Mapping[str, float],
    *,
    fee_per_fill: float,
    slippage_per_fill: float,
    impact_per_fill: float = 0.0,
) -> dict[str, float]:
    components = (fee_per_fill, slippage_per_fill, impact_per_fill)
    if any(value < 0 or not np.isfinite(value) for value in components):
        raise ValueError("execution cost inputs must be finite and non-negative")
    turnover = realized_turnover(previous_weights, target_weights)
    return {
        "turnover": turnover,
        "fee": turnover * fee_per_fill,
        "slippage": turnover * slippage_per_fill,
        "impact": turnover * impact_per_fill,
        "total": turnover * sum(components),
    }


def funding_pnl_fraction(
    weights: Mapping[str, float],
    funding_events: Mapping[str, list[Mapping[str, Any]]],
    *,
    entry_at: str | pd.Timestamp,
    exit_at: str | pd.Timestamp,
) -> dict[str, Any]:
    """Apply every funding transfer while the fixed portfolio is open."""
    entry = _utc_timestamp(entry_at)
    exit_timestamp = _utc_timestamp(exit_at)
    if exit_timestamp <= entry:
        raise ValueError("funding interval must have positive duration")
    contributions: list[dict[str, Any]] = []
    total = 0.0
    for raw_symbol, raw_weight in sorted(weights.items()):
        symbol = str(raw_symbol)
        weight = float(raw_weight)
        if not np.isfinite(weight):
            raise ValueError("funding weight is non-finite")
        for event in funding_events.get(symbol, []):
            try:
                event_at = pd.to_datetime(
                    int(event["fundingTime"]), unit="ms", utc=True
                )
                rate = float(event["fundingRate"])
            except (KeyError, TypeError, ValueError, OverflowError) as error:
                raise ValueError("funding event is invalid") from error
            if not np.isfinite(rate):
                raise ValueError("funding rate is non-finite")
            if entry <= event_at < exit_timestamp:
                contribution = -weight * rate
                total += contribution
                contributions.append(
                    {
                        "symbol": symbol,
                        "funding_time": event_at.isoformat(),
                        "weight": weight,
                        "funding_rate": rate,
                        "pnl_fraction": contribution,
                    }
                )
    return {
        "funding_pnl_fraction": float(total),
        "events_applied": len(contributions),
        "contributions": contributions,
        "interval": {
            "entry_at": entry.isoformat(),
            "exit_at_exclusive": exit_timestamp.isoformat(),
        },
    }
