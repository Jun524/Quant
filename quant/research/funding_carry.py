"""Causal development simulator for the preregistered Alpha #9 carry spread."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from math import prod
from pathlib import Path
from statistics import median

import pandas as pd


SYMBOLS = ("BTC", "ETH", "BNB", "SOL", "XRP")


@dataclass(frozen=True)
class CarryConfig:
    lookback_days: int = 7
    minimum_spread: float = 0.0025
    leg_equity_fraction: float = 0.20
    hold_days: int = 7
    stop_fraction: float = 0.10
    fee_per_fill: float = 0.0005
    slippage_per_fill: float = 0.0002
    starting_balance: float = 1000.0


def _load(data_dir: Path, symbol: str) -> dict[str, pd.DataFrame]:
    prefix = f"{symbol}_USDT_USDT"
    result = {
        "price": pd.read_feather(data_dir / f"{prefix}-1h-futures.feather").set_index("date").sort_index(),
        "mark": pd.read_feather(data_dir / f"{prefix}-1h-mark.feather").set_index("date").sort_index(),
        "funding": pd.read_feather(data_dir / f"{prefix}-1h-funding_rate.feather").set_index("date").sort_index(),
    }
    return result


def _simulate_leg(
    market: dict[str, pd.DataFrame],
    *,
    symbol: str,
    side: str,
    entry_at: pd.Timestamp,
    planned_exit_at: pd.Timestamp,
    notional: float,
    config: CarryConfig,
) -> dict:
    price = market["price"]
    if entry_at not in price.index or planned_exit_at not in price.index:
        raise ValueError(f"missing entry/exit candle for {symbol} at {entry_at}/{planned_exit_at}")
    raw_entry = float(price.at[entry_at, "open"])
    direction = 1.0 if side == "long" else -1.0
    entry = raw_entry * (1 + config.slippage_per_fill * direction)
    quantity = notional / entry
    stop_price = entry * (1 - config.stop_fraction if side == "long" else 1 + config.stop_fraction)
    window = price.loc[entry_at:planned_exit_at]
    if side == "long":
        breaches = window[window["low"] <= stop_price]
    else:
        breaches = window[window["high"] >= stop_price]
    stopped = not breaches.empty
    exit_at = breaches.index[0] if stopped else planned_exit_at
    if stopped:
        candle_open = float(price.at[exit_at, "open"])
        raw_exit = min(stop_price, candle_open) if side == "long" else max(stop_price, candle_open)
    else:
        raw_exit = float(price.at[exit_at, "open"])
    exit_price = raw_exit * (1 - config.slippage_per_fill * direction)

    raw_price_pnl = direction * quantity * (raw_exit - raw_entry)
    executed_price_pnl = direction * quantity * (exit_price - entry)
    modeled_slippage = executed_price_pnl - raw_price_pnl
    fees = -config.fee_per_fill * quantity * (entry + exit_price)
    funding_pnl = 0.0
    funding = market["funding"]
    events = funding[(funding.index > entry_at) & (funding.index <= exit_at)]
    mark = market["mark"]
    for timestamp, row in events.iterrows():
        if timestamp not in mark.index:
            raise ValueError(f"missing mark candle for {symbol} funding event {timestamp}")
        # A candle stamped at the funding instant has an immediately available
        # open, while its close contains the following hour and would leak.
        payment_notional = quantity * float(mark.at[timestamp, "open"])
        funding_pnl += (-direction) * payment_notional * float(row["funding_rate"])
    net = raw_price_pnl + modeled_slippage + fees + funding_pnl
    return {
        "symbol": symbol,
        "side": side,
        "entry_at": entry_at.isoformat(),
        "exit_at": exit_at.isoformat(),
        "stopped": stopped,
        "notional": notional,
        "raw_price_pnl": raw_price_pnl,
        "modeled_slippage": modeled_slippage,
        "fees": fees,
        "funding_pnl": funding_pnl,
        "net_pnl": net,
    }


def _profit_factor(values: list[float]) -> float | None:
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = -sum(value for value in values if value < 0)
    return gross_profit / gross_loss if gross_loss else None


def simulate_funding_carry(
    data_dir: Path,
    *,
    start: str,
    end: str,
    config: CarryConfig | None = None,
) -> dict:
    config = config or CarryConfig()
    start_at = pd.Timestamp(start, tz="UTC")
    end_at = pd.Timestamp(end, tz="UTC")
    markets = {symbol: _load(data_dir, symbol) for symbol in SYMBOLS}
    first_monday = start_at.normalize() + pd.offsets.Week(weekday=0)
    if first_monday < start_at:
        first_monday += pd.Timedelta(days=7)
    decisions = pd.date_range(first_monday + pd.Timedelta(hours=4), end_at, freq="7D", inclusive="left")
    equity = peak = config.starting_balance
    maximum_drawdown = 0.0
    trades: list[dict] = []
    weekly: list[dict] = []
    for decision_at in decisions:
        entry_at = decision_at + pd.Timedelta(hours=1)
        planned_exit_at = entry_at + pd.Timedelta(days=config.hold_days)
        if planned_exit_at >= end_at:
            continue
        scores: dict[str, float] = {}
        enough_history = True
        for symbol, market in markets.items():
            events = market["funding"]
            selected = events[(events.index > decision_at - pd.Timedelta(days=config.lookback_days)) & (events.index <= decision_at)]
            if len(selected) < config.lookback_days * 3:
                enough_history = False
                break
            scores[symbol] = float(selected["funding_rate"].sum())
        if not enough_history:
            continue
        low = min(scores, key=lambda symbol: (scores[symbol], symbol))
        high = max(scores, key=lambda symbol: (scores[symbol], symbol))
        spread = scores[high] - scores[low]
        if low == high or spread < config.minimum_spread:
            continue
        starting_equity = equity
        notional = starting_equity * config.leg_equity_fraction
        legs = [
            _simulate_leg(markets[low], symbol=low, side="long", entry_at=entry_at, planned_exit_at=planned_exit_at, notional=notional, config=config),
            _simulate_leg(markets[high], symbol=high, side="short", entry_at=entry_at, planned_exit_at=planned_exit_at, notional=notional, config=config),
        ]
        week_pnl = sum(leg["net_pnl"] for leg in legs)
        equity += week_pnl
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, 1 - equity / peak)
        for leg in legs:
            leg["decision_at"] = decision_at.isoformat()
            leg["score_spread"] = spread
            leg["low_score"] = scores[low]
            leg["high_score"] = scores[high]
            trades.append(leg)
        weekly.append(
            {
                "decision_at": decision_at.isoformat(),
                "close_at": max(pd.Timestamp(leg["exit_at"]) for leg in legs).isoformat(),
                "long_symbol": low,
                "short_symbol": high,
                "score_spread": spread,
                "start_equity": starting_equity,
                "net_pnl": week_pnl,
                "return": week_pnl / starting_equity,
                "end_equity": equity,
            }
        )
    values = [float(trade["net_pnl"]) for trade in trades]
    raw_price = sum(float(trade["raw_price_pnl"]) for trade in trades)
    slippage = sum(float(trade["modeled_slippage"]) for trade in trades)
    fees = sum(float(trade["fees"]) for trade in trades)
    funding = sum(float(trade["funding_pnl"]) for trade in trades)
    monthly_profit: dict[str, float] = defaultdict(float)
    for item in weekly:
        key = str(item["close_at"])[:7]
        monthly_profit[key] += float(item["net_pnl"])
    monthly_returns: list[float] = []
    monthly_rows: list[dict] = []
    month_equity = config.starting_balance
    cursor = date.fromisoformat(start[:10]).replace(day=1)
    final_month = date.fromisoformat(end[:10]).replace(day=1)
    while cursor < final_month:
        key = cursor.strftime("%Y-%m")
        pnl = monthly_profit.get(key, 0.0)
        value = pnl / month_equity
        monthly_rows.append({"month": key, "return": value, "profit": pnl, "start_equity": month_equity})
        monthly_returns.append(value)
        month_equity += pnl
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    gate = {
        "eligible_weeks_minimum_30": len(weekly) >= 30,
        "closed_legs_minimum_60": len(trades) >= 60,
        "net_return_positive": equity > config.starting_balance,
        "profit_factor_minimum_1_10": _profit_factor(values) is not None and float(_profit_factor(values)) >= 1.10,
        "funding_pnl_positive": funding > 0,
        "funding_exceeds_fees_and_slippage": funding > -(fees + slippage),
        "maximum_drawdown_at_most_0_20": maximum_drawdown <= 0.20,
    }
    by_symbol: dict[str, dict] = {}
    for symbol in SYMBOLS:
        selected = [trade for trade in trades if trade["symbol"] == symbol]
        by_symbol[symbol] = {
            "legs": len(selected),
            "raw_price_pnl": sum(float(trade["raw_price_pnl"]) for trade in selected),
            "funding_pnl": sum(float(trade["funding_pnl"]) for trade in selected),
            "net_pnl": sum(float(trade["net_pnl"]) for trade in selected),
        }
    by_side: dict[str, dict] = {}
    for side in ("long", "short"):
        selected = [trade for trade in trades if trade["side"] == side]
        by_side[side] = {
            "legs": len(selected),
            "raw_price_pnl": sum(float(trade["raw_price_pnl"]) for trade in selected),
            "funding_pnl": sum(float(trade["funding_pnl"]) for trade in selected),
            "net_pnl": sum(float(trade["net_pnl"]) for trade in selected),
        }
    positive_weeks = [float(item["net_pnl"]) for item in weekly if float(item["net_pnl"]) > 0]
    gross_positive_weeks = sum(positive_weeks)
    return {
        "candidate": "ALPHA_009_CROSS_SECTIONAL_FUNDING_CARRY",
        "status": "PASS_DEVELOPMENT" if all(gate.values()) else "FAIL_DEVELOPMENT",
        "timerange": f"{start}-{end}",
        "configuration": config.__dict__,
        "basis": "causal weekly decisions; next-hour execution; historical funding events on funding-time mark-candle open notional; 1h stop replay",
        "eligible_weeks": len(weekly),
        "closed_legs": len(trades),
        "stopped_legs": sum(bool(trade["stopped"]) for trade in trades),
        "ending_equity": equity,
        "net_return": equity / config.starting_balance - 1,
        "profit_factor": _profit_factor(values),
        "maximum_closed_weekly_drawdown": maximum_drawdown,
        "pnl_components": {
            "raw_price_pnl": raw_price,
            "modeled_slippage": slippage,
            "trading_fees": fees,
            "funding_pnl": funding,
            "net_pnl": raw_price + slippage + fees + funding,
            "carry_surplus_after_fees_and_slippage": funding + slippage + fees,
        },
        "diagnostics": {
            "by_symbol": by_symbol,
            "by_side": by_side,
            "largest_winning_week_concentration": max(positive_weeks) / gross_positive_weeks if gross_positive_weeks else None,
            "median_eligible_score_spread": median([float(item["score_spread"]) for item in weekly]) if weekly else None,
            "maximum_eligible_score_spread": max((float(item["score_spread"]) for item in weekly), default=None),
        },
        "monthly": {
            "complete_months": len(monthly_returns),
            "geometric_monthly_return": prod(1 + value for value in monthly_returns) ** (1 / len(monthly_returns)) - 1 if monthly_returns else None,
            "median_monthly_return": median(monthly_returns) if monthly_returns else None,
            "positive_month_ratio": sum(value > 0 for value in monthly_returns) / len(monthly_returns) if monthly_returns else None,
            "best_month": max(monthly_returns) if monthly_returns else None,
            "worst_month": min(monthly_returns) if monthly_returns else None,
            "series": monthly_rows,
        },
        "gate": gate,
        "weekly": weekly,
        "trades": trades,
        "warning": "Static survivor-biased universe and closed-weekly drawdown make this development evidence only.",
    }
