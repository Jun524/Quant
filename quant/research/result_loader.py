"""Load Freqtrade exports and expose consistent net-of-cost metrics."""

from __future__ import annotations

import json
import calendar
from datetime import date
from math import prod
from pathlib import Path
from statistics import median
from zipfile import ZipFile


def _summary(values: list[float], starting_balance: float) -> dict[str, float | int | None]:
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = -sum(value for value in values if value < 0)
    net_profit = sum(values)
    return {
        "trades": len(values),
        "net_profit": net_profit,
        "net_return": net_profit / starting_balance if starting_balance else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "wins": sum(value > 0 for value in values),
        "losses": sum(value < 0 for value in values),
        "expectancy": net_profit / len(values) if values else None,
    }


def _short_metrics(trades: list[dict]) -> dict[str, float | int | dict | str | None]:
    """Expose adverse-excursion and concentration diagnostics for shorts.

    Freqtrade exports per-trade extrema but not the intratrade order of every
    high/low.  ``worst_rebound_from_entry`` is therefore an exact full-trade
    adverse excursion from the entry price, not a claim about a rebound after
    the lowest point.  Detailed-candle replay can later add that chronology.
    """
    shorts = [trade for trade in trades if trade.get("is_short")]
    if not shorts:
        return {"trades": 0, "basis": "no organic short trades"}

    def excursion(trade: dict, key: str, adverse: bool) -> float | None:
        open_rate = float(trade.get("open_rate") or 0.0)
        extreme = trade.get(key)
        if open_rate <= 0 or extreme is None:
            return None
        leverage = float(trade.get("leverage") or 1.0)
        move = (float(extreme) / open_rate - 1.0) if adverse else (1.0 - float(extreme) / open_rate)
        return max(0.0, move * leverage)

    mae = [value for trade in shorts if (value := excursion(trade, "max_rate", True)) is not None]
    mfe = [value for trade in shorts if (value := excursion(trade, "min_rate", False)) is not None]
    profits = [float(trade.get("profit_abs") or 0.0) for trade in shorts]
    winners = [profit for profit in profits if profit > 0]
    gross_profit = sum(winners)
    exit_reasons: dict[str, int] = {}
    for trade in shorts:
        reason = str(trade.get("exit_reason") or "unknown")
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
    durations = [float(trade["trade_duration"]) for trade in shorts if trade.get("trade_duration") is not None]
    funding = [float(trade.get("funding_fees") or 0.0) for trade in shorts]
    return {
        "basis": "organic short trades; excursions use Freqtrade min_rate/max_rate and stated leverage",
        "trades": len(shorts),
        "mae_mean": sum(mae) / len(mae) if mae else None,
        "mae_max": max(mae) if mae else None,
        "mfe_mean": sum(mfe) / len(mfe) if mfe else None,
        "mfe_max": max(mfe) if mfe else None,
        "worst_rebound_from_entry": max(mae) if mae else None,
        "largest_single_loss": min(profits) if profits else None,
        "median_holding_minutes": median(durations) if durations else None,
        "funding_pnl": sum(funding),
        "expectancy": sum(profits) / len(profits) if profits else None,
        "largest_winner_profit_concentration": max(winners) / gross_profit if gross_profit else None,
        "exit_reason_distribution": exit_reasons,
    }


def _excursions(trade: dict) -> tuple[float, float] | None:
    """Return (MFE, MAE) as leverage-adjusted fractions for either direction."""
    open_rate = float(trade.get("open_rate") or 0.0)
    min_rate, max_rate = trade.get("min_rate"), trade.get("max_rate")
    if open_rate <= 0 or min_rate is None or max_rate is None:
        return None
    leverage = float(trade.get("leverage") or 1.0)
    if trade.get("is_short"):
        mfe = 1.0 - float(min_rate) / open_rate
        mae = float(max_rate) / open_rate - 1.0
    else:
        mfe = float(max_rate) / open_rate - 1.0
        mae = 1.0 - float(min_rate) / open_rate
    return max(0.0, mfe * leverage), max(0.0, mae * leverage)


def _trade_risk(trades: list[dict]) -> dict:
    """Expose futures failure modes which an account-level curve can hide."""
    excursions = [value for trade in trades if (value := _excursions(trade)) is not None]
    leverages = [float(trade.get("leverage") or 1.0) for trade in trades]
    liquidation_exits = [
        trade
        for trade in trades
        if "liquidat" in str(trade.get("exit_reason") or "").lower()
    ]
    return {
        "basis": "organic trades; MAE is leverage-adjusted from exported min_rate/max_rate",
        "trades": len(trades),
        "excursion_coverage_ratio": len(excursions) / len(trades) if trades else 0.0,
        "maximum_leverage": max(leverages) if leverages else None,
        "maximum_leverage_adjusted_mae": max((value[1] for value in excursions), default=None),
        "liquidation_exits": len(liquidation_exits),
    }


def _entry_regime(trade: dict) -> str:
    tag = str(trade.get("enter_tag") or "")
    for regime in ("bull", "bear", "range", "transition"):
        if tag.endswith(f"_{regime}"):
            return regime.upper()
    return "UNSPECIFIED"


def _detail_timing(trades: list[dict], detail_data_dir: Path | None) -> dict[str, float | int | str | None]:
    """Replay exported trades against 5m candles to timestamp MFE and MAE.

    This is diagnostic only: Freqtrade remains the execution simulator. Missing
    pair files or candles lower coverage instead of silently inventing timing.
    """
    if detail_data_dir is None:
        return {"basis": "not requested", "covered_trades": 0, "requested_trades": len(trades)}
    try:
        import pandas as pd
    except ImportError:
        return {"basis": "pandas unavailable", "covered_trades": 0, "requested_trades": len(trades)}

    cache: dict[str, object] = {}
    mfe_minutes: list[float] = []
    mae_minutes: list[float] = []
    for trade in trades:
        pair = str(trade.get("pair") or "")
        if pair not in cache:
            filename = pair.replace("/", "_").replace(":", "_") + "-5m-futures.feather"
            path = detail_data_dir / filename
            if not path.exists():
                cache[pair] = None
            else:
                try:
                    cache[pair] = pd.read_feather(path)
                except ImportError:
                    return {
                        "basis": "5m timing unavailable: this Python environment lacks the Feather reader",
                        "requested_trades": len(trades),
                        "covered_trades": 0,
                    }
        data = cache[pair]
        if data is None or not trade.get("open_date") or not trade.get("close_date"):
            continue
        opened = pd.Timestamp(trade["open_date"])
        closed = pd.Timestamp(trade["close_date"])
        candles = data[(data["date"] >= opened) & (data["date"] <= closed)]
        if candles.empty:
            continue
        if trade.get("is_short"):
            mfe_at = candles.loc[candles["low"].idxmin(), "date"]
            mae_at = candles.loc[candles["high"].idxmax(), "date"]
        else:
            mfe_at = candles.loc[candles["high"].idxmax(), "date"]
            mae_at = candles.loc[candles["low"].idxmin(), "date"]
        mfe_minutes.append((mfe_at - opened).total_seconds() / 60)
        mae_minutes.append((mae_at - opened).total_seconds() / 60)
    return {
        "basis": "5m OHLC replay between exported open/close timestamps; same-candle high/low ordering remains unknowable",
        "requested_trades": len(trades),
        "covered_trades": len(mfe_minutes),
        "coverage_ratio": len(mfe_minutes) / len(trades) if trades else 0.0,
        "median_time_to_mfe_minutes": median(mfe_minutes) if mfe_minutes else None,
        "median_time_to_mae_minutes": median(mae_minutes) if mae_minutes else None,
    }


def _breakout_metrics(trades: list[dict], detail_data_dir: Path | None) -> dict:
    """Evaluate continuation quality, direction, and entry-time regime tags."""
    excursions = [value for trade in trades if (value := _excursions(trade)) is not None]
    mfe = [value[0] for value in excursions]
    mae = [value[1] for value in excursions]
    profits = [float(trade.get("profit_abs") or 0.0) for trade in trades]
    winners = [profit for profit in profits if profit > 0]
    gross_profit = sum(winners)
    long_values = [float(trade.get("profit_abs") or 0.0) for trade in trades if not trade.get("is_short")]
    short_values = [float(trade.get("profit_abs") or 0.0) for trade in trades if trade.get("is_short")]
    holding_minutes = [
        float(trade["trade_duration"])
        for trade in trades
        if trade.get("trade_duration") is not None
    ]
    by_regime: dict[str, dict] = {}
    for regime in ("BULL", "BEAR", "RANGE", "TRANSITION", "UNSPECIFIED"):
        values = [float(trade.get("profit_abs") or 0.0) for trade in trades if _entry_regime(trade) == regime]
        by_regime[regime] = _summary(values, 1.0) if values else {"trades": 0, "net_profit": 0.0, "profit_factor": None, "expectancy": None}
    failures = sum(str(trade.get("exit_reason") or "") == "volbo_return_to_range" for trade in trades)
    return {
        "basis": "breakout success is a net-profitable natural trade; false breakout is an explicit return-to-range exit",
        "trades": len(trades),
        "breakout_success_rate": sum(profit > 0 for profit in profits) / len(profits) if profits else None,
        "false_breakout_rate": failures / len(trades) if trades else None,
        "mfe_mean": sum(mfe) / len(mfe) if mfe else None,
        "mae_mean": sum(mae) / len(mae) if mae else None,
        "mfe_mae_ratio": (sum(mfe) / len(mfe)) / (sum(mae) / len(mae)) if mfe and mae and sum(mae) else None,
        "median_holding_minutes": median(holding_minutes) if holding_minutes else None,
        "long_expectancy": sum(long_values) / len(long_values) if long_values else None,
        "short_expectancy": sum(short_values) / len(short_values) if short_values else None,
        "expectancy": sum(profits) / len(profits) if profits else None,
        "largest_winner_profit_concentration": max(winners) / gross_profit if gross_profit else None,
        "pnl_by_regime": by_regime,
        "exit_reason_distribution": {
            reason: sum(str(trade.get("exit_reason") or "unknown") == reason for trade in trades)
            for reason in sorted({str(trade.get("exit_reason") or "unknown") for trade in trades})
        },
        "detail_timing": _detail_timing(trades, detail_data_dir),
    }


def _modeled_slippage_cost(trades: list[dict], slippage_bps: float) -> float:
    return sum(
        sum(float(order.get("cost") or 0.0) for order in trade.get("orders", [])) * slippage_bps / 10_000
        for trade in trades
    )


def _organic_daily_profit(
    trades: list[dict], *, modeled_slippage_bps: float
) -> list[list[str | float]] | None:
    """Return cost-adjusted realized PnL grouped by organic trade close day.

    Freqtrade's top-level ``daily_profit`` may include a terminal ``force_exit``
    while the research gates intentionally exclude it.  It also cannot include
    a slippage stress added only by this loader.  Rebuild the series from the
    same organic trade PnL used by ``organic`` whenever each close timestamp is
    available; otherwise callers must retain the source series and disclose
    that the two accounting bases cannot be reconciled.
    """
    grouped: dict[date, float] = {}
    for trade in trades:
        closed = trade.get("close_date")
        if not closed:
            return None
        try:
            closed_day = date.fromisoformat(str(closed)[:10])
        except ValueError:
            return None
        profit = float(trade.get("profit_abs") or 0.0)
        profit -= _modeled_slippage_cost([trade], modeled_slippage_bps)
        grouped[closed_day] = grouped.get(closed_day, 0.0) + profit
    return [[observed.isoformat(), value] for observed, value in sorted(grouped.items())]


def _daily_distribution(daily_profit: list, starting_balance: float) -> dict:
    """Summarize Freqtrade's day series without allowing one outlier to hide."""
    dates = [str(item[0])[:10] for item in daily_profit]
    amounts = [float(item[1]) for item in daily_profit]
    if not amounts or starting_balance <= 0:
        return {"basis": "unavailable", "days": 0}
    equity = starting_balance
    minimum_equity = equity
    returns: list[float] = []
    for amount in amounts:
        daily_return = amount / equity if equity else 0.0
        returns.append(daily_return)
        equity += amount
        minimum_equity = min(minimum_equity, equity)
    ordered = sorted(returns)

    def historical_var(level: float) -> float:
        index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * (1 - level))))
        return max(0.0, -ordered[index])

    longest_losing_streak = 0
    current_streak = 0
    for daily_return in returns:
        current_streak = current_streak + 1 if daily_return < 0 else 0
        longest_losing_streak = max(longest_losing_streak, current_streak)
    return {
        "basis": "Freqtrade backtest daily_profit; not live mark-to-market equity",
        "days": len(returns),
        "mean_daily_return": sum(returns) / len(returns),
        "median_daily_return": median(returns),
        "geometric_daily_return": prod(1 + value for value in returns) ** (1 / len(returns)) - 1,
        "positive_day_ratio": sum(value > 0 for value in returns) / len(returns),
        "negative_day_ratio": sum(value < 0 for value in returns) / len(returns),
        "best_daily_return": max(returns),
        "worst_daily_return": min(returns),
        "historical_var_95": historical_var(0.95),
        "historical_var_99": historical_var(0.99),
        "max_consecutive_losing_days": longest_losing_streak,
        "minimum_equity": minimum_equity,
        "series": [
            {"date": observed, "return": daily_return, "profit": amount}
            for observed, daily_return, amount in zip(dates, returns, amounts)
        ],
    }


def _monthly_distribution(
    daily_profit: list,
    starting_balance: float,
    target: float = 0.10,
    *,
    coverage_start: date | None = None,
    coverage_end: date | None = None,
) -> dict:
    """Compound closed-profit records into calendar-month returns.

    Freqtrade's ``daily_profit`` begins on the first recorded trade day and can
    end on the last recorded trade day.  Use the backtest's half-open coverage
    interval when available so inactive days and a quiet final month are not
    incorrectly discarded from the monthly objective.
    """
    if not daily_profit or starting_balance <= 0:
        return {"basis": "unavailable", "complete_months": 0, "series": []}
    rows = sorted((date.fromisoformat(str(item[0])[:10]), float(item[1])) for item in daily_profit)
    profits_by_month: dict[str, float] = {}
    observed_bounds: dict[str, list[date]] = {}
    for observed, amount in rows:
        key = observed.strftime("%Y-%m")
        profits_by_month[key] = profits_by_month.get(key, 0.0) + amount
        observed_bounds.setdefault(key, [observed, observed])[1] = observed

    def next_month(value: date) -> date:
        return date(value.year + (value.month == 12), value.month % 12 + 1, 1)

    if coverage_start is not None and coverage_end is not None and coverage_start < coverage_end:
        month_cursor = coverage_start.replace(day=1)
        months: list[tuple[date, bool]] = []
        while month_cursor < coverage_end:
            following = next_month(month_cursor)
            months.append((month_cursor, coverage_start <= month_cursor and coverage_end >= following))
            month_cursor = following
    else:
        months = []
        month_cursor = rows[0][0].replace(day=1)
        final_month = rows[-1][0].replace(day=1)
        while month_cursor <= final_month:
            key = month_cursor.strftime("%Y-%m")
            bounds = observed_bounds.get(key)
            complete_observation = bool(
                bounds
                and bounds[0].day == 1
                and bounds[1].day == calendar.monthrange(month_cursor.year, month_cursor.month)[1]
            )
            months.append((month_cursor, complete_observation))
            month_cursor = next_month(month_cursor)

    equity = starting_balance
    grouped: list[dict] = []
    for month_cursor, is_complete in months:
        key = month_cursor.strftime("%Y-%m")
        month_profit = profits_by_month.get(key, 0.0)
        grouped.append(
            {
                "month": key,
                "return": month_profit / equity if equity else 0.0,
                "profit": month_profit,
                "start_equity": equity,
                "complete": is_complete,
            }
        )
        equity += month_profit
    complete = [float(item["return"]) for item in grouped if item["complete"]]
    if not complete:
        return {"basis": "calendar months from Freqtrade daily_profit; no complete months", "complete_months": 0, "series": grouped}
    ordered = sorted(complete)
    location = (len(ordered) - 1) * 0.10
    low = int(location)
    high = min(low + 1, len(ordered) - 1)
    p10 = ordered[low] + (ordered[high] - ordered[low]) * (location - low)
    losing_streak = current_streak = 0
    for value in complete:
        current_streak = current_streak + 1 if value < 0 else 0
        losing_streak = max(losing_streak, current_streak)
    return {
        "basis": "complete calendar months from Freqtrade closed daily_profit; not mark-to-market equity",
        "complete_months": len(complete),
        "geometric_monthly_return": prod(1 + value for value in complete) ** (1 / len(complete)) - 1,
        "arithmetic_mean_monthly_return": sum(complete) / len(complete),
        "median_monthly_return": median(complete),
        "positive_month_ratio": sum(value > 0 for value in complete) / len(complete),
        "target_month_ratio": sum(value >= target for value in complete) / len(complete),
        "target": target,
        "best_month": max(complete),
        "worst_month": min(complete),
        "p10_monthly_return": p10,
        "max_consecutive_losing_months": losing_streak,
        "series": grouped,
    }


def load_freqtrade_result(
    path: Path,
    strategy_name: str,
    *,
    detail_data_dir: Path | None = None,
    modeled_slippage_bps: float = 0.0,
) -> dict:
    """Load one strategy from a Freqtrade result zip.

    Artificial end-of-test force exits are kept separately and excluded from
    organic closed-trade metrics. Fee and funding figures are recorded from the
    exported trades; they are never inferred as zero.
    """
    with ZipFile(path) as archive:
        result_file = next(
            name for name in archive.namelist() if name.endswith(".json") and not name.endswith("_config.json")
        )
        payload = json.loads(archive.read(result_file))
    try:
        strategy = payload["strategy"][strategy_name]
    except KeyError as exc:
        raise ValueError(f"strategy {strategy_name!r} not found in {path}") from exc

    trades = strategy["trades"]
    organic = [trade for trade in trades if trade.get("exit_reason") != "force_exit"]
    forced = [trade for trade in trades if trade.get("exit_reason") == "force_exit"]
    starting_balance = float(strategy["starting_balance"])
    estimated_fees = sum(
        sum(
            float(order.get("cost") or 0.0)
            * (float(trade.get("fee_open") or 0.0) if order.get("ft_is_entry") else float(trade.get("fee_close") or 0.0))
            for order in trade.get("orders", [])
        )
        for trade in organic
    )
    funding = sum(float(trade.get("funding_fees") or 0.0) for trade in organic)
    days = max(int(strategy.get("backtest_days") or 1), 1)
    modeled_slippage = _modeled_slippage_cost(organic, modeled_slippage_bps)
    organic_summary = _summary(
        [float(trade["profit_abs"]) - _modeled_slippage_cost([trade], modeled_slippage_bps) for trade in organic],
        starting_balance,
    )
    net_return = float(organic_summary["net_return"])
    organic_daily_profit = _organic_daily_profit(
        organic, modeled_slippage_bps=modeled_slippage_bps
    )
    if organic_daily_profit is None:
        daily_profit = strategy.get("daily_profit") or []
        daily_accounting = {
            "status": "UNRECONCILED_SOURCE_FALLBACK",
            "basis": "Freqtrade daily_profit; one or more organic trades lack close_date, so force-exit exclusion and modeled slippage cannot be reconciled by close day.",
        }
    else:
        daily_profit = organic_daily_profit
        daily_accounting = {
            "status": "RECONCILED",
            "basis": "organic trade profit_abs grouped by UTC close date, less the same modeled slippage used by organic net PnL; force exits excluded.",
        }
    daily = _daily_distribution(daily_profit, starting_balance)
    coverage_start = date.fromisoformat(str(strategy["backtest_start"])[:10]) if strategy.get("backtest_start") else None
    coverage_end = date.fromisoformat(str(strategy["backtest_end"])[:10]) if strategy.get("backtest_end") else None
    monthly = _monthly_distribution(
        daily_profit,
        starting_balance,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
    )
    net_pnl = float(organic_summary["net_profit"])
    # Funding is signed: positive means it added to recorded net PnL.  This is
    # a reconciliation estimate because exchange exports can round per-order
    # values differently from aggregate trade PnL.
    price_pnl_estimate = net_pnl + estimated_fees - funding + modeled_slippage
    return {
        "source": str(path),
        "strategy": strategy_name,
        "timerange": strategy.get("timerange"),
        "backtest_start": strategy.get("backtest_start"),
        "backtest_end": strategy.get("backtest_end"),
        "starting_balance": starting_balance,
        "organic": organic_summary,
        "forced_exit": _summary([float(trade["profit_abs"]) for trade in forced], starting_balance),
        "max_drawdown": float(strategy.get("max_drawdown_account") or 0.0),
        "sharpe": strategy.get("sharpe"),
        "sortino": strategy.get("sortino"),
        "calmar": strategy.get("calmar"),
        "expectancy": strategy.get("expectancy"),
        "daily": daily,
        "equity_basis": "organic_closed_trade_daily_profit_not_mark_to_market",
        "daily_accounting": daily_accounting,
        "monthly": monthly,
        "positive_day_ratio": daily.get("positive_day_ratio", float(strategy.get("winning_days") or 0) / days),
        "daily_geometric_return": daily.get("geometric_daily_return", (1 + net_return) ** (1 / days) - 1 if net_return > -1 else -1.0),
        "turnover": float(strategy.get("total_volume") or 0.0) / starting_balance if starting_balance else 0.0,
        "estimated_fee_cost": estimated_fees,
        "modeled_slippage_bps": modeled_slippage_bps,
        "funding_net": funding,
        "funding_paid": -sum(min(float(trade.get("funding_fees") or 0.0), 0.0) for trade in organic),
        "funding_received": sum(max(float(trade.get("funding_fees") or 0.0), 0.0) for trade in organic),
        "pnl_components": {
            "basis": "estimated price PnL + trading fees + funding PnL - modeled slippage = net PnL (rounding may differ)",
            "price_pnl_estimate": price_pnl_estimate,
            "trading_fees": -estimated_fees,
            "funding_pnl": funding,
            "modeled_slippage": -modeled_slippage,
            "net_pnl": net_pnl,
        },
        "cost_drag": {
            "basis": "fractions of starting balance; positive funding_drag means funding reduced PnL",
            "fee_drag_pct": estimated_fees / starting_balance if starting_balance else 0.0,
            "funding_drag_pct": -funding / starting_balance if starting_balance else 0.0,
            "slippage_drag_pct": modeled_slippage / starting_balance if starting_balance else 0.0,
        },
        "short_metrics": _short_metrics(organic),
        "breakout_metrics": _breakout_metrics(organic, detail_data_dir),
        "trade_risk": _trade_risk(organic),
    }
