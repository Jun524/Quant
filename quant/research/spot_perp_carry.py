"""Causal simulator for preregistered same-asset spot/perpetual cash-and-carry."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from math import prod
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np
import pandas as pd


SYMBOLS = ("BTC", "ETH")


@dataclass(frozen=True)
class SpotPerpCarryConfig:
    lookback_days: int = 7
    minimum_funding_score: float = 0.004
    spot_capital_fraction: float = 0.45
    maximum_holding_days: int = 7
    minimum_holding_hours: int = 24
    short_stop_fraction: float = 0.20
    modeled_liquidation_rise: float = 0.80
    spot_fee_per_fill: float = 0.0010
    futures_fee_per_fill: float = 0.0005
    slippage_per_leg_per_fill: float = 0.0002
    starting_equity: float = 1000.0


def same_asset_basis_pnl(
    quantity: float,
    *,
    spot_entry: float,
    spot_exit: float,
    perpetual_entry: float,
    perpetual_exit: float,
) -> float:
    """Price PnL of long spot plus the identical base quantity short perpetual."""
    return quantity * (
        (spot_exit - spot_entry) - (perpetual_exit - perpetual_entry)
    )


def short_funding_pnl(quantity: float, rate: float, mark_price: float) -> float:
    """Positive funding is received by a short; negative funding is paid."""
    return quantity * rate * mark_price


def _load_frame(path: Path, *, required: tuple[str, ...]) -> pd.DataFrame:
    frame = pd.read_feather(path)
    missing = sorted(set(("date",) + required) - set(frame.columns))
    if missing:
        raise ValueError(f"{path.name} is missing columns: {', '.join(missing)}")
    frame = frame.loc[:, ["date", *required]].copy()
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    if frame["date"].isna().any() or frame["date"].duplicated().any():
        raise ValueError(f"{path.name} contains invalid or duplicate timestamps")
    frame = frame.set_index("date").sort_index()
    if not frame.index.is_monotonic_increasing:
        raise ValueError(f"{path.name} is not chronological")
    numeric = frame.loc[:, list(required)].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise ValueError(f"{path.name} contains non-finite values")
    if any(column != "funding_rate" and (numeric[column] <= 0).any() for column in required):
        raise ValueError(f"{path.name} contains non-positive market values")
    return numeric


def load_markets(data_dir: Path) -> dict[str, dict[str, pd.DataFrame]]:
    markets: dict[str, dict[str, pd.DataFrame]] = {}
    spot_dir = data_dir.parent
    for symbol in SYMBOLS:
        spot = _load_frame(
            spot_dir / f"{symbol}_USDT-1h.feather",
            required=("open", "high", "low", "close"),
        )
        prefix = f"{symbol}_USDT_USDT"
        perpetual = _load_frame(
            data_dir / f"{prefix}-1h-futures.feather",
            required=("open", "high", "low", "close"),
        )
        mark = _load_frame(
            data_dir / f"{prefix}-1h-mark.feather",
            required=("open", "high", "low", "close"),
        )
        funding = _load_frame(
            data_dir / f"{prefix}-1h-funding_rate.feather",
            required=("funding_rate",),
        )
        markets[symbol] = {
            "spot": spot,
            "perpetual": perpetual,
            "mark": mark,
            "funding": funding,
        }
    return markets


def _at(frame: pd.DataFrame, timestamp: pd.Timestamp, column: str) -> float:
    if timestamp not in frame.index:
        raise ValueError(f"missing {column} at {timestamp.isoformat()}")
    return float(frame.at[timestamp, column])


def _funding_between(
    market: dict[str, pd.DataFrame],
    *,
    entry_at: pd.Timestamp,
    exit_at: pd.Timestamp,
    quantity: float,
) -> tuple[float, int]:
    funding = market["funding"]
    selected = funding[(funding.index >= entry_at) & (funding.index <= exit_at)]
    total = 0.0
    included = 0
    for timestamp, row in selected.iterrows():
        rate = float(row["funding_rate"])
        # Ordering within an identical exchange timestamp is unknown.  Include
        # an adverse debit at an entry/exit tie but never an ambiguous credit.
        if timestamp in {entry_at, exit_at} and rate >= 0:
            continue
        mark_price = _at(market["mark"], timestamp, "open")
        total += short_funding_pnl(quantity, rate, mark_price)
        included += 1
    return total, included


def _funding_score(market: dict[str, pd.DataFrame], decision_at: pd.Timestamp, days: int) -> float:
    funding = market["funding"]
    selected = funding[
        (funding.index > decision_at - pd.Timedelta(days=days))
        & (funding.index <= decision_at)
    ]
    if selected.empty:
        raise ValueError(f"no funding history ending {decision_at.isoformat()}")
    return float(selected["funding_rate"].sum())


def _choose_exit(
    market: dict[str, pd.DataFrame],
    *,
    entry_at: pd.Timestamp,
    executed_perpetual_entry: float,
    config: SpotPerpCarryConfig,
) -> tuple[pd.Timestamp, str, float, float, bool]:
    planned_exit = entry_at + pd.Timedelta(days=config.maximum_holding_days)
    exit_at = planned_exit
    exit_reason = "maximum_holding_time"

    first_decision = entry_at.normalize() + pd.Timedelta(days=2)
    for decision_at in pd.date_range(first_decision, planned_exit, freq="1D", inclusive="left"):
        selected = market["funding"][
            (market["funding"].index > decision_at - pd.Timedelta(days=1))
            & (market["funding"].index <= decision_at)
        ]
        if selected.empty or float(selected["funding_rate"].sum()) <= 0:
            candidate = decision_at + pd.Timedelta(hours=1)
            if candidate < exit_at:
                exit_at = candidate
                exit_reason = "funding_nonpositive"
            break

    perpetual = market["perpetual"]
    stop_price = executed_perpetual_entry * (1 + config.short_stop_fraction)
    liquidation_price = executed_perpetual_entry * (1 + config.modeled_liquidation_rise)
    window = perpetual[(perpetual.index >= entry_at) & (perpetual.index <= exit_at)]
    breaches = window[window["high"] >= stop_price]
    liquidated = False
    if not breaches.empty:
        stop_at = breaches.index[0]
        if stop_at <= exit_at:
            exit_at = stop_at
            candle_open = float(perpetual.at[exit_at, "open"])
            raw_perpetual_exit = max(stop_price, candle_open)
            liquidated = candle_open >= liquidation_price
            exit_reason = "modeled_liquidation" if liquidated else "short_emergency_stop"
            # An intrabar timestamp is unavailable, so use the spot candle low
            # as the conservative simultaneous long-leg liquidation value.
            raw_spot_exit = float(market["spot"].at[exit_at, "low"])
            return exit_at, exit_reason, raw_spot_exit, raw_perpetual_exit, liquidated

    raw_spot_exit = _at(market["spot"], exit_at, "open")
    raw_perpetual_exit = _at(perpetual, exit_at, "open")
    return exit_at, exit_reason, raw_spot_exit, raw_perpetual_exit, liquidated


def _liquidation_value(
    market: dict[str, pd.DataFrame],
    *,
    timestamp: pd.Timestamp,
    entry_at: pd.Timestamp,
    quantity: float,
    raw_spot_entry: float,
    raw_perpetual_entry: float,
    executed_spot_entry: float,
    executed_perpetual_entry: float,
    config: SpotPerpCarryConfig,
) -> float:
    raw_spot_exit = _at(market["spot"], timestamp, "open")
    raw_perpetual_exit = _at(market["perpetual"], timestamp, "open")
    executed_spot_exit = raw_spot_exit * (1 - config.slippage_per_leg_per_fill)
    executed_perpetual_exit = raw_perpetual_exit * (1 + config.slippage_per_leg_per_fill)
    price_pnl = same_asset_basis_pnl(
        quantity,
        spot_entry=executed_spot_entry,
        spot_exit=executed_spot_exit,
        perpetual_entry=executed_perpetual_entry,
        perpetual_exit=executed_perpetual_exit,
    )
    fees = -config.spot_fee_per_fill * quantity * (executed_spot_entry + executed_spot_exit)
    fees -= config.futures_fee_per_fill * quantity * (
        executed_perpetual_entry + executed_perpetual_exit
    )
    funding, _ = _funding_between(
        market, entry_at=entry_at, exit_at=timestamp, quantity=quantity
    )
    return price_pnl + fees + funding


def _simulate_spread(
    market: dict[str, pd.DataFrame],
    *,
    symbol: str,
    decision_at: pd.Timestamp,
    funding_score: float,
    equity: float,
    config: SpotPerpCarryConfig,
) -> tuple[dict[str, Any], list[tuple[pd.Timestamp, float]]]:
    entry_at = decision_at + pd.Timedelta(hours=1)
    raw_spot_entry = _at(market["spot"], entry_at, "open")
    raw_perpetual_entry = _at(market["perpetual"], entry_at, "open")
    executed_spot_entry = raw_spot_entry * (1 + config.slippage_per_leg_per_fill)
    executed_perpetual_entry = raw_perpetual_entry * (1 - config.slippage_per_leg_per_fill)
    quantity = equity * config.spot_capital_fraction / executed_spot_entry
    spot_capital = quantity * executed_spot_entry
    futures_margin = quantity * executed_perpetual_entry
    entry_fees = config.spot_fee_per_fill * spot_capital
    entry_fees += config.futures_fee_per_fill * futures_margin
    required_capital = spot_capital + futures_margin + entry_fees
    if required_capital > equity:
        raise ValueError("1x spot plus isolated-futures capital exceeds current equity")

    exit_at, exit_reason, raw_spot_exit, raw_perpetual_exit, liquidated = _choose_exit(
        market,
        entry_at=entry_at,
        executed_perpetual_entry=executed_perpetual_entry,
        config=config,
    )
    executed_spot_exit = raw_spot_exit * (1 - config.slippage_per_leg_per_fill)
    executed_perpetual_exit = raw_perpetual_exit * (1 + config.slippage_per_leg_per_fill)
    raw_basis_pnl = same_asset_basis_pnl(
        quantity,
        spot_entry=raw_spot_entry,
        spot_exit=raw_spot_exit,
        perpetual_entry=raw_perpetual_entry,
        perpetual_exit=raw_perpetual_exit,
    )
    executed_price_pnl = same_asset_basis_pnl(
        quantity,
        spot_entry=executed_spot_entry,
        spot_exit=executed_spot_exit,
        perpetual_entry=executed_perpetual_entry,
        perpetual_exit=executed_perpetual_exit,
    )
    modeled_slippage = executed_price_pnl - raw_basis_pnl
    spot_fees = -config.spot_fee_per_fill * quantity * (
        executed_spot_entry + executed_spot_exit
    )
    futures_fees = -config.futures_fee_per_fill * quantity * (
        executed_perpetual_entry + executed_perpetual_exit
    )
    funding_pnl, funding_events = _funding_between(
        market, entry_at=entry_at, exit_at=exit_at, quantity=quantity
    )
    net_pnl = raw_basis_pnl + modeled_slippage + spot_fees + futures_fees + funding_pnl

    mtm: list[tuple[pd.Timestamp, float]] = []
    for timestamp in pd.date_range(entry_at, exit_at, freq="1h"):
        value = equity + _liquidation_value(
            market,
            timestamp=timestamp,
            entry_at=entry_at,
            quantity=quantity,
            raw_spot_entry=raw_spot_entry,
            raw_perpetual_entry=raw_perpetual_entry,
            executed_spot_entry=executed_spot_entry,
            executed_perpetual_entry=executed_perpetual_entry,
            config=config,
        )
        mtm.append((timestamp, value))
    if mtm:
        mtm[-1] = (exit_at, equity + net_pnl)

    return {
        "symbol": symbol,
        "decision_at": decision_at.isoformat(),
        "entry_at": entry_at.isoformat(),
        "exit_at": exit_at.isoformat(),
        "exit_reason": exit_reason,
        "liquidated": liquidated,
        "funding_score": funding_score,
        "entry_basis_fraction": raw_perpetual_entry / raw_spot_entry - 1,
        "quantity": quantity,
        "spot_capital": spot_capital,
        "futures_initial_margin": futures_margin,
        "required_capital": required_capital,
        "raw_spot_entry": raw_spot_entry,
        "raw_spot_exit": raw_spot_exit,
        "raw_perpetual_entry": raw_perpetual_entry,
        "raw_perpetual_exit": raw_perpetual_exit,
        "raw_basis_price_pnl": raw_basis_pnl,
        "modeled_slippage": modeled_slippage,
        "spot_fees": spot_fees,
        "futures_fees": futures_fees,
        "funding_pnl": funding_pnl,
        "funding_events": funding_events,
        "net_pnl": net_pnl,
        "return_on_starting_equity": net_pnl / equity,
        "starting_equity": equity,
        "ending_equity": equity + net_pnl,
    }, mtm


def _profit_factor(values: list[float]) -> float | None:
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = -sum(value for value in values if value < 0)
    return gross_profit / gross_loss if gross_loss else None


def _monthly_metrics(
    observations: list[tuple[pd.Timestamp, float]],
    *,
    start_at: pd.Timestamp,
    end_at: pd.Timestamp,
    starting_equity: float,
) -> dict[str, Any]:
    series = pd.Series(
        [value for _, value in observations],
        index=pd.DatetimeIndex([timestamp for timestamp, _ in observations]),
        dtype=float,
    ).sort_index()
    series = series[~series.index.duplicated(keep="last")]
    # Use the first instant of the following month as the boundary.  Pandas'
    # month-end timestamp is 00:00 on the final calendar day and would omit
    # that day's remaining 24 hours from a mark-to-market month.
    first_boundary = start_at.normalize() + pd.offsets.MonthBegin(1)
    month_boundaries = pd.date_range(first_boundary, end_at, freq="MS", inclusive="both")
    previous = starting_equity
    rows: list[dict[str, Any]] = []
    returns: list[float] = []
    for boundary in month_boundaries:
        eligible = series[series.index < boundary]
        equity = float(eligible.iloc[-1]) if not eligible.empty else previous
        value = equity / previous - 1
        rows.append(
            {
                "month": (boundary - pd.offsets.Day(1)).strftime("%Y-%m"),
                "return": value,
                "ending_equity": equity,
            }
        )
        returns.append(value)
        previous = equity
    return {
        "complete_months": len(returns),
        "geometric_monthly_return": prod(1 + value for value in returns) ** (1 / len(returns)) - 1
        if returns
        else None,
        "median_monthly_return": median(returns) if returns else None,
        "positive_month_ratio": sum(value > 0 for value in returns) / len(returns)
        if returns
        else None,
        "best_month": max(returns) if returns else None,
        "worst_month": min(returns) if returns else None,
        "series": rows,
    }


def _bootstrap_probability_of_loss(
    observations: list[tuple[pd.Timestamp, float]],
    *,
    start_at: pd.Timestamp,
    end_at: pd.Timestamp,
    block_days: int = 30,
    simulations: int = 2000,
) -> float:
    series = pd.Series(
        [value for _, value in observations],
        index=pd.DatetimeIndex([timestamp for timestamp, _ in observations]),
        dtype=float,
    ).sort_index()
    series = series[~series.index.duplicated(keep="last")]
    days = pd.date_range(start_at.normalize(), end_at.normalize(), freq="1D", inclusive="left")
    daily_equity = series.reindex(series.index.union(days)).sort_index().ffill().reindex(days)
    returns = daily_equity.pct_change().fillna(0.0).to_numpy(dtype=float)
    if len(returns) < block_days:
        return 1.0
    blocks = [returns[index : index + block_days] for index in range(len(returns) - block_days + 1)]
    rng = np.random.default_rng(120012)
    losses = 0
    for _ in range(simulations):
        sampled: list[float] = []
        while len(sampled) < len(returns):
            sampled.extend(blocks[int(rng.integers(0, len(blocks)))])
        terminal = float(np.prod(1 + np.asarray(sampled[: len(returns)])))
        losses += terminal <= 1.0
    return losses / simulations


def simulate_spot_perp_carry(
    data_dir: Path,
    *,
    start: str,
    end: str,
    config: SpotPerpCarryConfig | None = None,
) -> dict[str, Any]:
    config = config or SpotPerpCarryConfig()
    start_at = pd.Timestamp(start)
    end_at = pd.Timestamp(end)
    if start_at.tzinfo is None:
        start_at = start_at.tz_localize("UTC")
    else:
        start_at = start_at.tz_convert("UTC")
    if end_at.tzinfo is None:
        end_at = end_at.tz_localize("UTC")
    else:
        end_at = end_at.tz_convert("UTC")
    if end_at <= start_at:
        raise ValueError("simulation end must follow start")
    markets = load_markets(data_dir)

    equity = config.starting_equity
    peak = equity
    maximum_drawdown = 0.0
    trades: list[dict[str, Any]] = []
    observations: list[tuple[pd.Timestamp, float]] = [(start_at, equity)]
    decisions = pd.date_range(start_at, end_at, freq="1D", inclusive="left")
    next_available = start_at
    for decision_at in decisions:
        if decision_at < next_available:
            continue
        if decision_at + pd.Timedelta(hours=1, days=config.maximum_holding_days) >= end_at:
            break
        eligible: list[tuple[float, str]] = []
        for symbol in SYMBOLS:
            market = markets[symbol]
            score = _funding_score(market, decision_at, config.lookback_days)
            spot_open = _at(market["spot"], decision_at, "open")
            perpetual_open = _at(market["perpetual"], decision_at, "open")
            basis = perpetual_open / spot_open - 1
            if score >= config.minimum_funding_score and basis >= 0:
                eligible.append((score, symbol))
        if not eligible:
            continue
        funding_score, symbol = sorted(eligible, key=lambda item: (-item[0], item[1]))[0]
        trade, mtm = _simulate_spread(
            markets[symbol],
            symbol=symbol,
            decision_at=decision_at,
            funding_score=funding_score,
            equity=equity,
            config=config,
        )
        trades.append(trade)
        for timestamp, value in mtm:
            if value <= 0:
                raise ValueError("non-positive mark-to-market equity")
            peak = max(peak, value)
            maximum_drawdown = max(maximum_drawdown, 1 - value / peak)
            observations.append((timestamp, value))
        equity = float(trade["ending_equity"])
        if equity <= 0:
            raise ValueError("non-positive closed equity")
        next_available = pd.Timestamp(trade["exit_at"]) + pd.Timedelta(nanoseconds=1)

    values = [float(trade["net_pnl"]) for trade in trades]
    positive = [value for value in values if value > 0]
    gross_positive = sum(positive)
    monthly = _monthly_metrics(
        observations,
        start_at=start_at,
        end_at=end_at,
        starting_equity=config.starting_equity,
    )
    components = {
        "raw_basis_price_pnl": sum(float(trade["raw_basis_price_pnl"]) for trade in trades),
        "modeled_slippage": sum(float(trade["modeled_slippage"]) for trade in trades),
        "spot_fees": sum(float(trade["spot_fees"]) for trade in trades),
        "futures_fees": sum(float(trade["futures_fees"]) for trade in trades),
        "funding_pnl": sum(float(trade["funding_pnl"]) for trade in trades),
        "net_pnl": sum(values),
    }
    return {
        "timerange": f"{start_at.isoformat()}--{end_at.isoformat()}",
        "configuration": config.__dict__,
        "closed_spreads": len(trades),
        "liquidations": sum(bool(trade["liquidated"]) for trade in trades),
        "ending_equity": equity,
        "net_return": equity / config.starting_equity - 1,
        "profit_factor": _profit_factor(values),
        "maximum_mark_to_market_drawdown": maximum_drawdown,
        "largest_winner_profit_concentration": max(positive) / gross_positive
        if gross_positive
        else None,
        "thirty_day_block_bootstrap_probability_of_loss": _bootstrap_probability_of_loss(
            observations, start_at=start_at, end_at=end_at
        ),
        "pnl_components": components,
        "monthly": monthly,
        "by_symbol": {
            symbol: {
                "spreads": sum(trade["symbol"] == symbol for trade in trades),
                "net_pnl": sum(float(trade["net_pnl"]) for trade in trades if trade["symbol"] == symbol),
                "funding_pnl": sum(float(trade["funding_pnl"]) for trade in trades if trade["symbol"] == symbol),
                "raw_basis_price_pnl": sum(
                    float(trade["raw_basis_price_pnl"])
                    for trade in trades
                    if trade["symbol"] == symbol
                ),
            }
            for symbol in SYMBOLS
        },
        "exit_reason_distribution": dict(Counter(trade["exit_reason"] for trade in trades)),
        "trades": trades,
    }


def evaluate_development(
    data_dir: Path,
    *,
    start: str = "2022-01-01",
    end: str = "2024-01-01",
) -> dict[str, Any]:
    base_config = SpotPerpCarryConfig()
    base = simulate_spot_perp_carry(data_dir, start=start, end=end, config=base_config)
    double_fee = simulate_spot_perp_carry(
        data_dir,
        start=start,
        end=end,
        config=replace(
            base_config,
            spot_fee_per_fill=base_config.spot_fee_per_fill * 2,
            futures_fee_per_fill=base_config.futures_fee_per_fill * 2,
        ),
    )
    slippage_stress = simulate_spot_perp_carry(
        data_dir,
        start=start,
        end=end,
        config=replace(base_config, slippage_per_leg_per_fill=0.0005),
    )
    components = base["pnl_components"]
    profit_factor = base["profit_factor"]
    concentration = base["largest_winner_profit_concentration"]
    gate = {
        "natural_closed_spreads_minimum_20": base["closed_spreads"] >= 20,
        "net_return_positive": base["net_return"] > 0,
        "profit_factor_minimum_1_20": profit_factor is not None and profit_factor >= 1.20,
        "geometric_monthly_return_minimum_0_0025": (
            base["monthly"]["geometric_monthly_return"] is not None
            and base["monthly"]["geometric_monthly_return"] >= 0.0025
        ),
        "actual_funding_pnl_positive": components["funding_pnl"] > 0,
        "basis_plus_funding_exceeds_base_friction": (
            components["raw_basis_price_pnl"] + components["funding_pnl"]
            > -(components["modeled_slippage"] + components["spot_fees"] + components["futures_fees"])
        ),
        "double_fee_net_positive": double_fee["net_return"] > 0,
        "five_bps_slippage_net_positive": slippage_stress["net_return"] > 0,
        "maximum_drawdown_at_most_0_10": base["maximum_mark_to_market_drawdown"] <= 0.10,
        "zero_liquidations": base["liquidations"] == 0,
        "largest_winner_concentration_at_most_0_35": (
            concentration is not None and concentration <= 0.35
        ),
        "bootstrap_probability_of_loss_at_most_0_10": (
            base["thirty_day_block_bootstrap_probability_of_loss"] <= 0.10
        ),
    }
    return {
        "candidate": "ALPHA_012_SPOT_PERP_CASH_AND_CARRY",
        "status": "PASS_DEVELOPMENT" if all(gate.values()) else "FAIL_DEVELOPMENT",
        "validation_opened": False,
        "development": {
            "base": base,
            "double_fee": {key: value for key, value in double_fee.items() if key != "trades"},
            "five_bps_slippage": {
                key: value for key, value in slippage_stress.items() if key != "trades"
            },
        },
        "gate": gate,
        "warning": "Development uses previously seen local files and can reject only. Locked validation was not loaded or summarized.",
    }
