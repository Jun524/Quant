"""Frozen development-stage statistics for Alpha 015.

This module only evaluates already sealed daily portfolio returns.  It does
not load archives, fit parameters, or create strategy variants.  The smaller
minimum sample is deliberately limited to Alpha 015's 120-day *development*
screen; portfolio promotion continues to use its independent 252-day policy.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from math import prod
from typing import Any, Iterable, Mapping

import numpy as np

from quant.research.selection_bias import SelectionBiasPolicy, evaluate_selection_bias


ALPHA015_DEVELOPMENT_DAYS = 120
ALPHA015_LOCKED_VALIDATION_DAYS = 60
ALPHA015_MINIMUM_COMPLETE_DAILY_OBSERVATIONS = 100
ALPHA015_NEWEY_WEST_MAXIMUM_LAG_DAYS = 5
ALPHA015_DEVELOPMENT_SELECTION_POLICY = SelectionBiasPolicy(
    minimum_observations=ALPHA015_MINIMUM_COMPLETE_DAILY_OBSERVATIONS,
    minimum_deflated_sharpe_probability=0.95,
    maximum_probability_backtest_overfitting=0.10,
    cscv_slices=8,
)


def _daily_returns(values: Any, *, minimum_observations: int = ALPHA015_MINIMUM_COMPLETE_DAILY_OBSERVATIONS) -> np.ndarray:
    returns = np.asarray(values, dtype=float)
    if returns.ndim != 1 or len(returns) < minimum_observations:
        raise ValueError(f"Alpha 015 requires at least {minimum_observations} complete daily returns")
    if not np.isfinite(returns).all():
        raise ValueError("Alpha 015 daily returns must be finite")
    return returns


def alpha015_newey_west_mean_t_statistic(
    values: Any, *, minimum_observations: int = ALPHA015_MINIMUM_COMPLETE_DAILY_OBSERVATIONS
) -> dict[str, float | int]:
    """Return the fixed five-lag Bartlett-HAC t-statistic for daily returns."""
    returns = _daily_returns(values, minimum_observations=minimum_observations)
    centered = returns - returns.mean()
    lag = ALPHA015_NEWEY_WEST_MAXIMUM_LAG_DAYS
    long_run_variance = float(np.mean(centered * centered))
    for offset in range(1, lag + 1):
        covariance = float(np.mean(centered[offset:] * centered[:-offset]))
        long_run_variance += 2.0 * (1.0 - offset / (lag + 1.0)) * covariance
    if not np.isfinite(long_run_variance) or long_run_variance <= 0:
        raise ValueError("Alpha 015 Newey-West long-run variance is not positive")
    standard_error = float(np.sqrt(long_run_variance / len(returns)))
    return {
        "observations": len(returns),
        "minimum_observations": minimum_observations,
        "maximum_lag_days": lag,
        "mean_daily_return": float(returns.mean()),
        "long_run_variance": long_run_variance,
        "standard_error": standard_error,
        "t_statistic": float(returns.mean() / standard_error),
        "kernel": "Bartlett",
    }


def evaluate_alpha015_development_selection_bias(
    selected_daily_returns: Any,
    trial_daily_return_matrix: Any,
    *,
    trial_ids: list[str],
    selected_trial_id: str,
    trial_inventory_complete: bool,
    pre_registered_single_trial: bool,
) -> dict[str, Any]:
    """Evaluate the frozen, development-only selection-bias policy."""
    selected = _daily_returns(selected_daily_returns)
    matrix = np.asarray(trial_daily_return_matrix, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] != len(selected):
        raise ValueError("Alpha 015 trial matrix must match complete daily returns")
    return evaluate_selection_bias(
        selected,
        matrix,
        attempted_trials=matrix.shape[1],
        trial_ids=trial_ids,
        selected_trial_id=selected_trial_id,
        trial_inventory_complete=trial_inventory_complete,
        pre_registered_single_trial=pre_registered_single_trial,
        policy=ALPHA015_DEVELOPMENT_SELECTION_POLICY,
    )


def _profit_factor(values: np.ndarray) -> float | None:
    gains = float(values[values > 0].sum())
    losses = float(-values[values < 0].sum())
    return gains / losses if gains > 0 and losses > 0 else None


def _maximum_drawdown(equity: Iterable[float]) -> float:
    peak = 0.0
    maximum = 0.0
    for raw in equity:
        value = float(raw)
        if not np.isfinite(value) or value <= 0:
            raise ValueError("Alpha 015 MTM equity must be positive and finite")
        peak = max(peak, value)
        maximum = max(maximum, 1.0 - value / peak)
    return float(maximum)


def _complete_month_returns(
    dates: list[date], returns: np.ndarray
) -> list[dict[str, Any]]:
    values_by_date = dict(zip(dates, returns))
    months = sorted({(item.year, item.month) for item in dates})
    result: list[dict[str, Any]] = []
    for year, month in months:
        days = [date(year, month, offset) for offset in range(1, monthrange(year, month)[1] + 1)]
        if not all(item in values_by_date for item in days):
            continue
        monthly = np.asarray([values_by_date[item] for item in days], dtype=float)
        result.append(
            {
                "month": f"{year:04d}-{month:02d}",
                "return": float(prod(1.0 + monthly) - 1.0),
                "days": len(days),
            }
        )
    return result


def _combined_mtm_drawdown(replays: list[Mapping[str, Any]], branch: str) -> float:
    equity = [1.0]
    starting_equity = 1.0
    for replay in replays:
        try:
            path = replay["simulation"]["mark_to_market"][branch]
            points = path["points"]
            final_equity = float(path["final_equity"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Alpha 015 minute MTM path is missing") from error
        if not isinstance(points, list) or not points:
            raise ValueError("Alpha 015 minute MTM points are missing")
        equity.extend(starting_equity * float(point["equity"]) for point in points)
        starting_equity *= final_equity
    return _maximum_drawdown(equity)


def evaluate_alpha015_development(
    daily_replays: Iterable[Mapping[str, Any]],
    *,
    candidate: str = "ALPHA_015_MINUTE_CROSS_CRYPTO_PREDICTABILITY",
    lookahead_analysis_passed: bool = False,
    future_row_invariance_passed: bool = False,
) -> dict[str, Any]:
    """Apply Alpha 015's development gates to exactly 120 source-bound days.

    Lookahead and future-row invariance are explicit inputs rather than assumed
    from an apparently causal implementation.  A report therefore fails closed
    until a separate replayed analysis has supplied both receipts.
    """
    replays = list(daily_replays)
    dates: list[date] = []
    base_returns: list[float] = []
    stress_returns: list[float] = []
    gross_returns: list[float] = []
    source_integrity = True
    for replay in replays:
        try:
            day = date.fromisoformat(str(replay["evaluation_date"]))
            simulation = replay["simulation"]
            receipts = replay["decision_receipts"]
            source = replay["source_validation"]
            base = float(simulation["base_net_return"])
            stress = float(simulation["stress_net_return"])
            gross = float(simulation["gross_price_return"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Alpha 015 daily replay has an invalid result schema") from error
        if (
            replay.get("status") != "PASS"
            or simulation.get("status") != "PASS"
            or len(receipts) != 143
            or any(int(item.get("future_rows_read", -1)) != 0 for item in receipts)
            or (source.get("inventory") or {}).get("status") != "PASS"
            or (source.get("aggtrade_chain") or {}).get("status") != "PASS"
            or not np.isfinite([base, stress, gross]).all()
            or min(1.0 + base, 1.0 + stress, 1.0 + gross) <= 0
        ):
            source_integrity = False
        dates.append(day)
        base_returns.append(base)
        stress_returns.append(stress)
        gross_returns.append(gross)
    if len(replays) != ALPHA015_DEVELOPMENT_DAYS:
        raise ValueError("Alpha 015 development must contain exactly 120 complete days")
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise ValueError("Alpha 015 development days must be unique and chronological")
    base_array = _daily_returns(base_returns)
    stress_array = _daily_returns(stress_returns)
    gross_array = _daily_returns(gross_returns)
    selection = evaluate_alpha015_development_selection_bias(
        base_array,
        base_array.reshape(-1, 1),
        trial_ids=[candidate],
        selected_trial_id=candidate,
        trial_inventory_complete=True,
        pre_registered_single_trial=True,
    )
    nw = alpha015_newey_west_mean_t_statistic(base_array)
    complete_months = _complete_month_returns(dates, base_array)
    positive_month_fraction = (
        sum(item["return"] > 0 for item in complete_months) / len(complete_months)
        if complete_months
        else 0.0
    )
    positive = base_array[base_array > 0]
    largest_positive_day_share = (
        float(positive.max() / positive.sum()) if len(positive) and positive.sum() > 0 else None
    )
    base_mtm_dd = _combined_mtm_drawdown(replays, "base")
    stress_mtm_dd = _combined_mtm_drawdown(replays, "stress")
    base_pf = _profit_factor(base_array)
    stress_pf = _profit_factor(stress_array)
    checks = [
        {"name": "source_integrity", "passed": source_integrity, "actual": source_integrity, "required": True},
        {"name": "gross_price_pnl_positive", "passed": prod(1.0 + gross_array) > 1.0, "actual": float(prod(1.0 + gross_array) - 1.0), "required": "> 0"},
        {"name": "base_net_pnl_positive", "passed": prod(1.0 + base_array) > 1.0, "actual": float(prod(1.0 + base_array) - 1.0), "required": "> 0"},
        {"name": "stress_net_pnl_positive", "passed": prod(1.0 + stress_array) > 1.0, "actual": float(prod(1.0 + stress_array) - 1.0), "required": "> 0"},
        {"name": "base_profit_factor", "passed": base_pf is not None and base_pf >= 1.2, "actual": base_pf, "required": ">= 1.2"},
        {"name": "stress_profit_factor", "passed": stress_pf is not None and stress_pf >= 1.0, "actual": stress_pf, "required": ">= 1.0"},
        {"name": "newey_west_t_stat_base", "passed": nw["t_statistic"] >= 2.0, "actual": nw["t_statistic"], "required": ">= 2.0"},
        {"name": "base_minute_mark_to_market_drawdown", "passed": base_mtm_dd <= 0.15, "actual": base_mtm_dd, "required": "<= 0.15"},
        {"name": "stress_minute_mark_to_market_drawdown", "passed": stress_mtm_dd <= 0.15, "actual": stress_mtm_dd, "required": "<= 0.15"},
        {"name": "positive_complete_calendar_month_fraction", "passed": positive_month_fraction >= 0.6, "actual": positive_month_fraction, "required": ">= 0.6"},
        {"name": "largest_positive_day_share", "passed": largest_positive_day_share is not None and largest_positive_day_share <= 0.2, "actual": largest_positive_day_share, "required": "<= 0.2"},
        {"name": "deflated_sharpe_probability", "passed": selection["checks"][2]["passed"], "actual": selection["deflated_sharpe"]["probability"], "required": ">= 0.95"},
        {"name": "single_trial_pbo_contract", "passed": selection["pbo"]["status"] == "NOT_APPLICABLE_SINGLE_PREREGISTERED_TRIAL", "actual": selection["pbo"]["status"], "required": "NOT_APPLICABLE_SINGLE_PREREGISTERED_TRIAL"},
        {"name": "lookahead_analysis", "passed": lookahead_analysis_passed is True, "actual": lookahead_analysis_passed, "required": True},
        {"name": "future_row_invariance", "passed": future_row_invariance_passed is True, "actual": future_row_invariance_passed, "required": True},
    ]
    return {
        "schema_version": 1,
        "candidate": candidate,
        "stage": "development",
        "status": "PASS" if all(item["passed"] for item in checks) else "FAIL",
        "days": len(replays),
        "daily_returns": {"base": base_returns, "stress": stress_returns, "gross_price": gross_returns},
        "profit_factor": {"base": base_pf, "stress": stress_pf},
        "newey_west": nw,
        "minute_mark_to_market_drawdown": {"base": base_mtm_dd, "stress": stress_mtm_dd},
        "complete_months": complete_months,
        "positive_complete_calendar_month_fraction": positive_month_fraction,
        "largest_positive_day_share": largest_positive_day_share,
        "selection_bias_audit": selection,
        "checks": checks,
        "locked_validation_open": False,
        "live_order_authorization": "CLOSED",
    }


def evaluate_alpha015_locked_validation(
    daily_replays: Iterable[Mapping[str, Any]],
    *,
    candidate: str = "ALPHA_015_MINUTE_CROSS_CRYPTO_PREDICTABILITY",
    all_funding_reconciled: bool = False,
    no_parameter_or_universe_retuning: bool = False,
    lookahead_analysis_passed: bool = False,
    future_row_invariance_passed: bool = False,
) -> dict[str, Any]:
    """Judge the untouched 60-day holdout, never a live-promotion decision."""
    replays = list(daily_replays)
    dates: list[date] = []
    base_returns: list[float] = []
    stress_returns: list[float] = []
    source_integrity = True
    for replay in replays:
        try:
            day = date.fromisoformat(str(replay["evaluation_date"]))
            simulation = replay["simulation"]
            receipts = replay["decision_receipts"]
            source = replay["source_validation"]
            base = float(simulation["base_net_return"])
            stress = float(simulation["stress_net_return"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Alpha 015 locked validation replay has an invalid result schema") from error
        if (
            replay.get("status") != "PASS"
            or simulation.get("status") != "PASS"
            or len(receipts) != 143
            or any(int(item.get("future_rows_read", -1)) != 0 for item in receipts)
            or (source.get("inventory") or {}).get("status") != "PASS"
            or (source.get("aggtrade_chain") or {}).get("status") != "PASS"
            or not np.isfinite([base, stress]).all()
            or min(1.0 + base, 1.0 + stress) <= 0
        ):
            source_integrity = False
        dates.append(day)
        base_returns.append(base)
        stress_returns.append(stress)
    if len(replays) != ALPHA015_LOCKED_VALIDATION_DAYS:
        raise ValueError("Alpha 015 locked validation must contain exactly 60 complete days")
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise ValueError("Alpha 015 locked validation days must be unique and chronological")
    base_array = _daily_returns(base_returns, minimum_observations=ALPHA015_LOCKED_VALIDATION_DAYS)
    stress_array = _daily_returns(stress_returns, minimum_observations=ALPHA015_LOCKED_VALIDATION_DAYS)
    nw = alpha015_newey_west_mean_t_statistic(
        base_array, minimum_observations=ALPHA015_LOCKED_VALIDATION_DAYS
    )
    complete_months = _complete_month_returns(dates, base_array)
    positive_month_fraction = (
        sum(item["return"] > 0 for item in complete_months) / len(complete_months)
        if complete_months
        else 0.0
    )
    positive = base_array[base_array > 0]
    largest_positive_day_share = (
        float(positive.max() / positive.sum()) if len(positive) and positive.sum() > 0 else None
    )
    base_mtm_dd = _combined_mtm_drawdown(replays, "base")
    stress_mtm_dd = _combined_mtm_drawdown(replays, "stress")
    base_pf = _profit_factor(base_array)
    stress_pf = _profit_factor(stress_array)
    checks = [
        {"name": "source_integrity", "passed": source_integrity, "actual": source_integrity, "required": True},
        {"name": "all_funding_reconciled", "passed": all_funding_reconciled is True, "actual": all_funding_reconciled, "required": True},
        {"name": "base_net_pnl_positive", "passed": prod(1.0 + base_array) > 1.0, "actual": float(prod(1.0 + base_array) - 1.0), "required": "> 0"},
        {"name": "stress_net_pnl_positive", "passed": prod(1.0 + stress_array) > 1.0, "actual": float(prod(1.0 + stress_array) - 1.0), "required": "> 0"},
        {"name": "base_profit_factor", "passed": base_pf is not None and base_pf >= 1.2, "actual": base_pf, "required": ">= 1.2"},
        {"name": "stress_profit_factor", "passed": stress_pf is not None and stress_pf >= 1.0, "actual": stress_pf, "required": ">= 1.0"},
        {"name": "newey_west_t_stat_base", "passed": nw["t_statistic"] >= 2.0, "actual": nw["t_statistic"], "required": ">= 2.0"},
        {"name": "base_minute_mark_to_market_drawdown", "passed": base_mtm_dd <= 0.15, "actual": base_mtm_dd, "required": "<= 0.15"},
        {"name": "stress_minute_mark_to_market_drawdown", "passed": stress_mtm_dd <= 0.15, "actual": stress_mtm_dd, "required": "<= 0.15"},
        {"name": "positive_complete_calendar_month_fraction", "passed": positive_month_fraction >= 0.5, "actual": positive_month_fraction, "required": ">= 0.5"},
        {"name": "largest_positive_day_share", "passed": largest_positive_day_share is not None and largest_positive_day_share <= 0.2, "actual": largest_positive_day_share, "required": "<= 0.2"},
        {"name": "no_parameter_or_universe_retuning", "passed": no_parameter_or_universe_retuning is True, "actual": no_parameter_or_universe_retuning, "required": True},
        {"name": "lookahead_analysis", "passed": lookahead_analysis_passed is True, "actual": lookahead_analysis_passed, "required": True},
        {"name": "future_row_invariance", "passed": future_row_invariance_passed is True, "actual": future_row_invariance_passed, "required": True},
    ]
    return {
        "schema_version": 1,
        "candidate": candidate,
        "stage": "locked_validation",
        "status": "REVIEW" if all(item["passed"] for item in checks) else "FAIL",
        "days": len(replays),
        "daily_returns": {"base": base_returns, "stress": stress_returns},
        "profit_factor": {"base": base_pf, "stress": stress_pf},
        "newey_west": nw,
        "minute_mark_to_market_drawdown": {"base": base_mtm_dd, "stress": stress_mtm_dd},
        "complete_months": complete_months,
        "positive_complete_calendar_month_fraction": positive_month_fraction,
        "largest_positive_day_share": largest_positive_day_share,
        "checks": checks,
        "live_order_authorization": "CLOSED",
        "promotion_eligible": False,
    }
