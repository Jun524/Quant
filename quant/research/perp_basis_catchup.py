"""Causal development-only simulator for Alpha #14 perpetual basis catch-up.

The implementation intentionally has no optimizer and no historical validation
entry point.  Alpha #14 is a transfer diagnostic inside the Alpha 009/013
family, not independent promotion evidence.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from math import exp, log, prod
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np
import pandas as pd


SYMBOLS = ("BTC", "ETH", "BNB", "SOL", "XRP")
ONE_HOUR = pd.to_timedelta(1, unit="h")
ONE_DAY = pd.to_timedelta(1, unit="D")


@dataclass(frozen=True)
class PerpBasisCatchupConfig:
    leg_equity_fraction: float = 0.20
    hold_hours: int = 24
    stop_fraction: float = 0.10
    liquidation_adverse_move: float = 0.80
    fee_per_fill: float = 0.0005
    slippage_per_fill: float = 0.0002
    starting_equity: float = 1000.0


def basis_score(spot_close: float, perpetual_close: float) -> float:
    """Spot-minus-perpetual log basis used for the cross-sectional rank."""
    if spot_close <= 0 or perpetual_close <= 0:
        raise ValueError("basis prices must be positive")
    return log(spot_close) - log(perpetual_close)


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
    numeric = frame.loc[:, list(required)].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise ValueError(f"{path.name} contains non-finite values")
    price_columns = [column for column in required if column != "funding_rate"]
    if price_columns and (numeric.loc[:, price_columns] <= 0).any().any():
        raise ValueError(f"{path.name} contains non-positive market values")
    return numeric


def load_markets(
    data_dir: Path,
    *,
    start: str | pd.Timestamp,
    end_exclusive: str | pd.Timestamp,
) -> dict[str, dict[str, pd.DataFrame]]:
    """Load the fixed five-symbol panel and expose only the requested interval."""
    start_at = pd.Timestamp(start)
    end_at = pd.Timestamp(end_exclusive)
    start_at = start_at.tz_localize("UTC") if start_at.tzinfo is None else start_at.tz_convert("UTC")
    end_at = end_at.tz_localize("UTC") if end_at.tzinfo is None else end_at.tz_convert("UTC")
    if end_at <= start_at:
        raise ValueError("end_exclusive must be after start")
    spot_dir = data_dir.parent
    markets: dict[str, dict[str, pd.DataFrame]] = {}
    # One signal candle before the first decision is the only lookback needed.
    slice_start = start_at - ONE_HOUR
    for symbol in SYMBOLS:
        prefix = f"{symbol}_USDT_USDT"
        components = {
            "spot": _load_frame(
                spot_dir / f"{symbol}_USDT-1h.feather",
                required=("open", "high", "low", "close"),
            ),
            "perpetual": _load_frame(
                data_dir / f"{prefix}-1h-futures.feather",
                required=("open", "high", "low", "close"),
            ),
            "mark": _load_frame(
                data_dir / f"{prefix}-1h-mark.feather",
                required=("open", "high", "low", "close"),
            ),
            "funding": _load_frame(
                data_dir / f"{prefix}-1h-funding_rate.feather",
                required=("funding_rate",),
            ),
        }
        markets[symbol] = {
            name: frame[(frame.index >= slice_start) & (frame.index < end_at)]
            for name, frame in components.items()
        }
    return markets


def _at(frame: pd.DataFrame, timestamp: pd.Timestamp, column: str) -> float:
    if timestamp not in frame.index:
        raise ValueError(f"missing {column} at {timestamp.isoformat()}")
    return float(frame.at[timestamp, column])


def _funding_pnl(
    market: dict[str, pd.DataFrame],
    *,
    direction: float,
    quantity: float,
    after: pd.Timestamp,
    through: pd.Timestamp,
) -> tuple[float, int]:
    events = market["funding"]
    selected = events[(events.index > after) & (events.index <= through)]
    total = 0.0
    for timestamp, row in selected.iterrows():
        mark = _at(market["mark"], timestamp, "open")
        total += -direction * quantity * mark * float(row["funding_rate"])
    return total, len(selected)


def _simulate_leg(
    market: dict[str, pd.DataFrame],
    *,
    symbol: str,
    side: str,
    entry_at: pd.Timestamp,
    planned_exit_at: pd.Timestamp,
    notional: float,
    config: PerpBasisCatchupConfig,
) -> dict[str, Any]:
    perpetual = market["perpetual"]
    direction = 1.0 if side == "long" else -1.0
    raw_entry = _at(perpetual, entry_at, "open")
    executed_entry = raw_entry * (1 + direction * config.slippage_per_fill)
    quantity = notional / executed_entry
    stop_price = executed_entry * (
        1 - config.stop_fraction if side == "long" else 1 + config.stop_fraction
    )
    liquidation_price = executed_entry * (
        1 - config.liquidation_adverse_move
        if side == "long"
        else 1 + config.liquidation_adverse_move
    )
    window = perpetual.loc[entry_at:planned_exit_at]
    expected_hours = config.hold_hours + 1
    expected_index = pd.date_range(entry_at, planned_exit_at, freq="1h")
    if len(window) != expected_hours or len(window.index.symmetric_difference(expected_index)):
        raise ValueError(f"incomplete holding window for {symbol} at {entry_at.isoformat()}")
    breaches = (
        window[window["low"] <= stop_price]
        if side == "long"
        else window[window["high"] >= stop_price]
    )
    stopped = not breaches.empty
    exit_at = breaches.index[0] if stopped else planned_exit_at
    if stopped:
        candle_open = _at(perpetual, exit_at, "open")
        raw_exit = min(stop_price, candle_open) if side == "long" else max(stop_price, candle_open)
    else:
        raw_exit = _at(perpetual, exit_at, "open")
    observed = perpetual.loc[entry_at:exit_at]
    liquidated = bool(
        (side == "long" and (observed["open"] <= liquidation_price).any())
        or (side == "short" and (observed["open"] >= liquidation_price).any())
    )
    executed_exit = raw_exit * (1 - direction * config.slippage_per_fill)
    raw_price_pnl = direction * quantity * (raw_exit - raw_entry)
    executed_price_pnl = direction * quantity * (executed_exit - executed_entry)
    modeled_slippage = executed_price_pnl - raw_price_pnl
    fees = -config.fee_per_fill * quantity * (executed_entry + executed_exit)
    funding, funding_events = _funding_pnl(
        market,
        direction=direction,
        quantity=quantity,
        after=entry_at,
        through=exit_at,
    )
    favorable = (
        float(observed["high"].max()) / raw_entry - 1
        if side == "long"
        else 1 - float(observed["low"].min()) / raw_entry
    )
    adverse = (
        float(observed["low"].min()) / raw_entry - 1
        if side == "long"
        else 1 - float(observed["high"].max()) / raw_entry
    )
    return {
        "symbol": symbol,
        "side": side,
        "direction": direction,
        "entry_at": entry_at,
        "exit_at": exit_at,
        "planned_exit_at": planned_exit_at,
        "notional": notional,
        "quantity": quantity,
        "raw_entry": raw_entry,
        "executed_entry": executed_entry,
        "raw_exit": raw_exit,
        "executed_exit": executed_exit,
        "stopped": stopped,
        "liquidated": liquidated,
        "mfe_fraction": favorable,
        "mae_fraction": adverse,
        "raw_price_pnl": raw_price_pnl,
        "modeled_slippage": modeled_slippage,
        "trading_fees": fees,
        "funding_pnl": funding,
        "funding_events": funding_events,
        "net_pnl": raw_price_pnl + modeled_slippage + fees + funding,
    }


def _leg_marked_pnl(
    leg: dict[str, Any],
    market: dict[str, pd.DataFrame],
    timestamp: pd.Timestamp,
    config: PerpBasisCatchupConfig,
) -> float:
    if timestamp >= leg["exit_at"]:
        return float(leg["net_pnl"])
    direction = float(leg["direction"])
    current = _at(market["perpetual"], timestamp, "close")
    executed_exit = current * (1 - direction * config.slippage_per_fill)
    price_pnl = direction * float(leg["quantity"]) * (
        executed_exit - float(leg["executed_entry"])
    )
    fees = -config.fee_per_fill * float(leg["quantity"]) * (
        float(leg["executed_entry"]) + executed_exit
    )
    funding, _ = _funding_pnl(
        market,
        direction=direction,
        quantity=float(leg["quantity"]),
        after=leg["entry_at"],
        through=timestamp,
    )
    return price_pnl + fees + funding


def _profit_factor(values: list[float]) -> float | None:
    wins = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return wins / losses if losses else None


def _monthly_metrics(days: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[float]] = {}
    for day in days:
        month = pd.Timestamp(day["close_at"]).strftime("%Y-%m")
        grouped.setdefault(month, []).append(float(day["return"]))
    series = [
        {"month": month, "return": prod(1 + value for value in values) - 1}
        for month, values in sorted(grouped.items())
    ]
    returns = [float(item["return"]) for item in series]
    geometric = exp(sum(log(1 + value) for value in returns) / len(returns)) - 1 if returns else None
    return {
        "complete_months": len(series),
        "geometric_monthly_return": geometric,
        "positive_month_ratio": (
            sum(value > 0 for value in returns) / len(returns) if returns else None
        ),
        "worst_month": min(returns) if returns else None,
        "best_month": max(returns) if returns else None,
        "series": series,
    }


def simulate_development(
    markets: dict[str, dict[str, pd.DataFrame]],
    *,
    start: str | pd.Timestamp,
    end_exclusive: str | pd.Timestamp,
    first_decision: str | pd.Timestamp | None = None,
    config: PerpBasisCatchupConfig | None = None,
) -> dict[str, Any]:
    config = config or PerpBasisCatchupConfig()
    decision = pd.Timestamp(first_decision if first_decision is not None else start)
    end_at = pd.Timestamp(end_exclusive)
    decision = decision.tz_localize("UTC") if decision.tzinfo is None else decision.tz_convert("UTC")
    end_at = end_at.tz_localize("UTC") if end_at.tzinfo is None else end_at.tz_convert("UTC")
    equity = peak = config.starting_equity
    maximum_drawdown = 0.0
    days: list[dict[str, Any]] = []
    legs_out: list[dict[str, Any]] = []
    selection_counts: Counter[str] = Counter()
    while True:
        signal_at = decision - ONE_HOUR
        entry_at = decision + ONE_HOUR
        planned_exit_at = entry_at + pd.to_timedelta(config.hold_hours, unit="h")
        if planned_exit_at >= end_at:
            break
        scores = {
            symbol: basis_score(
                _at(markets[symbol]["spot"], signal_at, "close"),
                _at(markets[symbol]["perpetual"], signal_at, "close"),
            )
            for symbol in SYMBOLS
        }
        long_symbol = min(SYMBOLS, key=lambda symbol: (-scores[symbol], symbol))
        short_symbol = min(SYMBOLS, key=lambda symbol: (scores[symbol], symbol))
        if long_symbol == short_symbol:
            raise ValueError("basis ranking did not produce distinct legs")
        start_equity = equity
        notional = start_equity * config.leg_equity_fraction
        legs = [
            _simulate_leg(
                markets[long_symbol], symbol=long_symbol, side="long",
                entry_at=entry_at, planned_exit_at=planned_exit_at,
                notional=notional, config=config,
            ),
            _simulate_leg(
                markets[short_symbol], symbol=short_symbol, side="short",
                entry_at=entry_at, planned_exit_at=planned_exit_at,
                notional=notional, config=config,
            ),
        ]
        for timestamp in pd.date_range(entry_at, planned_exit_at, freq="1h"):
            marked = start_equity + sum(
                _leg_marked_pnl(leg, markets[leg["symbol"]], timestamp, config)
                for leg in legs
            )
            if marked <= 0:
                maximum_drawdown = 1.0
            peak = max(peak, marked)
            maximum_drawdown = max(maximum_drawdown, 1 - marked / peak)
        net = sum(float(leg["net_pnl"]) for leg in legs)
        equity += net
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, 1 - equity / peak)
        if equity <= 0:
            maximum_drawdown = 1.0
        spread = scores[long_symbol] - scores[short_symbol]
        close_at = max(leg["exit_at"] for leg in legs)
        for leg in legs:
            serial = {
                key: (value.isoformat() if isinstance(value, pd.Timestamp) else value)
                for key, value in leg.items()
                if key != "direction"
            }
            serial["decision_at"] = decision.isoformat()
            serial["signal_at"] = signal_at.isoformat()
            serial["basis_score"] = scores[leg["symbol"]]
            serial["basis_spread"] = spread
            legs_out.append(serial)
            selection_counts[f"{leg['side']}:{leg['symbol']}"] += 1
        days.append(
            {
                "decision_at": decision.isoformat(),
                "signal_at": signal_at.isoformat(),
                "entry_at": entry_at.isoformat(),
                "close_at": close_at.isoformat(),
                "long_symbol": long_symbol,
                "short_symbol": short_symbol,
                "long_basis": scores[long_symbol],
                "short_basis": scores[short_symbol],
                "basis_spread": spread,
                "start_equity": start_equity,
                "raw_price_pnl": sum(float(leg["raw_price_pnl"]) for leg in legs),
                "net_pnl": net,
                "return": net / start_equity,
                "end_equity": equity,
            }
        )
        decision += ONE_DAY

    day_pnl = [float(day["net_pnl"]) for day in days]
    raw_price = sum(float(leg["raw_price_pnl"]) for leg in legs_out)
    slippage = sum(float(leg["modeled_slippage"]) for leg in legs_out)
    fees = sum(float(leg["trading_fees"]) for leg in legs_out)
    funding = sum(float(leg["funding_pnl"]) for leg in legs_out)
    positive = [value for value in day_pnl if value > 0]
    midpoint = len(day_pnl) // 2
    return {
        "closed_daily_spreads": len(days),
        "closed_legs": len(legs_out),
        "ending_equity": equity,
        "net_return": equity / config.starting_equity - 1,
        "profit_factor_by_day": _profit_factor(day_pnl),
        "maximum_mark_to_market_drawdown": maximum_drawdown,
        "liquidations": sum(bool(leg["liquidated"]) for leg in legs_out),
        "stopped_legs": sum(bool(leg["stopped"]) for leg in legs_out),
        "pnl_components": {
            "raw_relative_price_pnl": raw_price,
            "modeled_slippage": slippage,
            "trading_fees": fees,
            "funding_pnl": funding,
            "price_after_fees_and_slippage": raw_price + slippage + fees,
            "net_pnl": raw_price + slippage + fees + funding,
        },
        "first_half_net_pnl": sum(day_pnl[:midpoint]) if midpoint else None,
        "second_half_net_pnl": sum(day_pnl[midpoint:]) if midpoint else None,
        "largest_winning_day_concentration": (
            max(positive) / sum(positive) if positive else None
        ),
        "median_basis_spread": median(
            [float(day["basis_spread"]) for day in days]
        ) if days else None,
        "median_leg_mfe": median(
            [float(leg["mfe_fraction"]) for leg in legs_out]
        ) if legs_out else None,
        "median_leg_mae": median(
            [float(leg["mae_fraction"]) for leg in legs_out]
        ) if legs_out else None,
        "monthly": _monthly_metrics(days),
        "selection_counts": dict(sorted(selection_counts.items())),
        "configuration": config.__dict__,
        "days": days,
        "legs": legs_out,
    }


def evaluate_development(
    data_dir: Path,
    *,
    start: str = "2022-01-01T00:00:00Z",
    end_exclusive: str = "2024-01-01T00:00:00Z",
    config: PerpBasisCatchupConfig | None = None,
) -> dict[str, Any]:
    config = config or PerpBasisCatchupConfig()
    markets = load_markets(data_dir, start=start, end_exclusive=end_exclusive)
    base = simulate_development(
        markets,
        start=start,
        end_exclusive=end_exclusive,
        first_decision=pd.Timestamp(start) + ONE_DAY,
        config=config,
    )
    double_fee = simulate_development(
        markets,
        start=start,
        end_exclusive=end_exclusive,
        first_decision=pd.Timestamp(start) + ONE_DAY,
        config=replace(config, fee_per_fill=0.001),
    )
    slippage_stress = simulate_development(
        markets,
        start=start,
        end_exclusive=end_exclusive,
        first_decision=pd.Timestamp(start) + ONE_DAY,
        config=replace(config, slippage_per_fill=0.0005),
    )
    pnl = base["pnl_components"]
    monthly = base["monthly"]
    pf = base["profit_factor_by_day"]
    concentration = base["largest_winning_day_concentration"]
    checks = {
        "closed_daily_spreads_minimum_700": base["closed_daily_spreads"] >= 700,
        "closed_legs_minimum_1400": base["closed_legs"] >= 1400,
        "net_return_positive": base["net_return"] > 0,
        "raw_relative_price_pnl_positive": pnl["raw_relative_price_pnl"] > 0,
        "price_after_base_fees_and_slippage_positive": pnl["price_after_fees_and_slippage"] > 0,
        "profit_factor_minimum_1_20": pf is not None and pf >= 1.20,
        "geometric_monthly_return_minimum_0_005": monthly["geometric_monthly_return"] is not None and monthly["geometric_monthly_return"] >= 0.005,
        "positive_month_ratio_minimum_0_60": monthly["positive_month_ratio"] is not None and monthly["positive_month_ratio"] >= 0.60,
        "double_fee_net_positive": double_fee["net_return"] > 0,
        "five_bps_slippage_net_positive": slippage_stress["net_return"] > 0,
        "first_half_net_positive": base["first_half_net_pnl"] is not None and base["first_half_net_pnl"] > 0,
        "second_half_net_positive": base["second_half_net_pnl"] is not None and base["second_half_net_pnl"] > 0,
        "maximum_mark_to_market_drawdown_at_most_0_15": base["maximum_mark_to_market_drawdown"] <= 0.15,
        "largest_winning_day_concentration_at_most_0_10": concentration is not None and concentration <= 0.10,
        "zero_liquidations": base["liquidations"] == 0,
    }
    return {
        "candidate": "ALPHA_014_CROSS_SECTIONAL_PERP_BASIS_CATCHUP",
        "status": "PASS_DEVELOPMENT_TRANSFER_ONLY" if all(checks.values()) else "FAIL_DEVELOPMENT",
        "sample_role": "historical_transfer_diagnostic_only",
        "live_promotion_eligible": False,
        "historical_validation_exists": False,
        "development": {
            "base": base,
            "double_fee": double_fee,
            "five_bps_slippage": slippage_stress,
        },
        "gate": checks,
        "warning": "Family trial 3 uses a fixed surviving-symbol universe. A pass can only authorize a fresh point-in-time forward protocol.",
    }
