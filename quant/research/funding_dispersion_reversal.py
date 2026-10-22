"""Forward-only simulator for Alpha #13 funding-dispersion price reversal."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import prod
from pathlib import Path
from statistics import median
from typing import Any
from zipfile import ZipFile

import pandas as pd


SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")


@dataclass(frozen=True)
class FundingDispersionConfig:
    lookback_days: int = 7
    minimum_events: int = 21
    minimum_spread: float = 0.0025
    leg_equity_fraction: float = 0.20
    hold_days: int = 7
    stop_fraction: float = 0.10
    liquidation_adverse_move: float = 0.80
    fee_per_fill: float = 0.0005
    slippage_per_fill: float = 0.0002
    starting_equity: float = 1000.0
    maximum_development_weeks: int = 30


def _read_hourly_archive(path: Path) -> pd.DataFrame:
    with ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != 1:
            raise ValueError(f"expected one CSV in {path}")
        with archive.open(names[0]) as handle:
            frame = pd.read_csv(handle)
    frame["date"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    frame = frame.set_index("date").sort_index()
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    if frame.index.has_duplicates:
        raise ValueError(f"duplicate hourly timestamps in {path}")
    return frame


def load_sealed_markets(raw_root: Path) -> tuple[dict[str, dict[str, pd.DataFrame]], dict[str, Any]]:
    """Load only day directories that pass the collector's current protocol audit."""
    from scripts.download_alpha013_day import (
        manifest_inventory_digest,
        protocol_digests,
        validate_existing_day,
    )

    day_dirs = sorted(path for path in raw_root.glob("????-??-??") if path.is_dir())
    markets: dict[str, dict[str, list[pd.DataFrame]]] = {
        symbol: {"price": [], "mark": [], "funding": []} for symbol in SYMBOLS
    }
    manifests: list[dict[str, Any]] = []
    day_manifests: list[dict[str, str]] = []
    for day_dir in day_dirs:
        manifests.append(validate_existing_day(day_dir))
        day_manifests.append(
            {
                "day": day_dir.name,
                "manifest_sha256": sha256(
                    (day_dir / "manifest.json").read_bytes()
                ).hexdigest(),
            }
        )
        for symbol in SYMBOLS:
            symbol_dir = day_dir / symbol
            markets[symbol]["price"].append(_read_hourly_archive(symbol_dir / "klines.zip"))
            markets[symbol]["mark"].append(
                _read_hourly_archive(symbol_dir / "mark_price_klines.zip")
            )
            funding_payload = json.loads(
                (symbol_dir / "funding.json").read_text(encoding="utf-8")
            )
            rows = funding_payload.get("rows") or []
            funding = pd.DataFrame(rows)
            if funding.empty:
                funding = pd.DataFrame(columns=["fundingTime", "fundingRate"])
            funding["date"] = pd.to_datetime(funding["fundingTime"], unit="ms", utc=True)
            funding["funding_rate"] = pd.to_numeric(
                funding["fundingRate"], errors="raise"
            )
            markets[symbol]["funding"].append(
                funding.set_index("date")[["funding_rate"]].sort_index()
            )
    combined: dict[str, dict[str, pd.DataFrame]] = {}
    for symbol, components in markets.items():
        combined[symbol] = {}
        for name, frames in components.items():
            if frames:
                frame = pd.concat(frames).sort_index()
                if frame.index.has_duplicates:
                    raise ValueError(f"duplicate {name} timestamps for {symbol}")
            else:
                columns = ["funding_rate"] if name == "funding" else ["open", "high", "low", "close"]
                frame = pd.DataFrame(columns=columns, index=pd.DatetimeIndex([], tz="UTC"))
            combined[symbol][name] = frame
    return combined, {
        "sealed_days": len(day_dirs),
        "first_day": day_dirs[0].name if day_dirs else None,
        "last_day": day_dirs[-1].name if day_dirs else None,
        "day_manifests": day_manifests,
        "source_inventory_sha256": manifest_inventory_digest(day_manifests),
        "protocol_sha256": protocol_digests(),
        "funding_reconciled": all(
            manifest.get("funding", {}).get("status") == "RECONCILED"
            for manifest in manifests
        ) if manifests else False,
    }


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
        if timestamp not in market["mark"].index:
            raise ValueError(f"missing mark candle for funding event {timestamp}")
        notional = quantity * float(market["mark"].at[timestamp, "open"])
        total += -direction * notional * float(row["funding_rate"])
    return total, len(selected)


def _simulate_leg(
    market: dict[str, pd.DataFrame],
    *,
    symbol: str,
    side: str,
    entry_at: pd.Timestamp,
    planned_exit_at: pd.Timestamp,
    notional: float,
    config: FundingDispersionConfig,
) -> dict[str, Any]:
    price = market["price"]
    if entry_at not in price.index or planned_exit_at not in price.index:
        raise ValueError(f"missing entry/exit candle for {symbol}")
    direction = 1.0 if side == "long" else -1.0
    raw_entry = float(price.at[entry_at, "open"])
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
    window = price.loc[entry_at:planned_exit_at]
    breaches = (
        window[window["low"] <= stop_price]
        if side == "long"
        else window[window["high"] >= stop_price]
    )
    stopped = not breaches.empty
    exit_at = breaches.index[0] if stopped else planned_exit_at
    if stopped:
        candle_open = float(price.at[exit_at, "open"])
        raw_exit = (
            min(stop_price, candle_open)
            if side == "long"
            else max(stop_price, candle_open)
        )
    else:
        raw_exit = float(price.at[exit_at, "open"])
    executed_exit = raw_exit * (1 - direction * config.slippage_per_fill)
    observed_to_exit = price.loc[entry_at:exit_at]
    liquidated = bool(
        (side == "long" and (observed_to_exit["open"] <= liquidation_price).any())
        or (side == "short" and (observed_to_exit["open"] >= liquidation_price).any())
    )
    raw_price_pnl = direction * quantity * (raw_exit - raw_entry)
    executed_price_pnl = direction * quantity * (executed_exit - executed_entry)
    modeled_slippage = executed_price_pnl - raw_price_pnl
    fees = -config.fee_per_fill * quantity * (executed_entry + executed_exit)
    funding_pnl, funding_events = _funding_pnl(
        market,
        direction=direction,
        quantity=quantity,
        after=entry_at,
        through=exit_at,
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
        "raw_price_pnl": raw_price_pnl,
        "modeled_slippage": modeled_slippage,
        "fees": fees,
        "funding_pnl": funding_pnl,
        "funding_events": funding_events,
        "net_pnl": raw_price_pnl + modeled_slippage + fees + funding_pnl,
    }


def _leg_mtm(
    leg: dict[str, Any],
    market: dict[str, pd.DataFrame],
    timestamp: pd.Timestamp,
    config: FundingDispersionConfig,
) -> float:
    if timestamp >= leg["exit_at"]:
        return float(leg["net_pnl"])
    direction = float(leg["direction"])
    current = float(market["price"].at[timestamp, "close"])
    executed_exit = current * (1 - direction * config.slippage_per_fill)
    executed_price_pnl = direction * float(leg["quantity"]) * (
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
        through=timestamp + timedelta(hours=1) - timedelta(milliseconds=1),
    )
    return executed_price_pnl + fees + funding


def _profit_factor(values: list[float]) -> float | None:
    wins = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return wins / losses if losses else None


def simulate_forward(
    markets: dict[str, dict[str, pd.DataFrame]],
    *,
    first_decision: str | datetime | pd.Timestamp,
    config: FundingDispersionConfig | None = None,
) -> dict[str, Any]:
    config = config or FundingDispersionConfig()
    decision = pd.Timestamp(first_decision)
    if decision.tzinfo is None:
        decision = decision.tz_localize("UTC")
    else:
        decision = decision.tz_convert("UTC")
    equity = peak = config.starting_equity
    maximum_drawdown = 0.0
    weeks: list[dict[str, Any]] = []
    legs_out: list[dict[str, Any]] = []
    while len(weeks) < config.maximum_development_weeks:
        entry_at = decision + timedelta(hours=1)
        exit_at = entry_at + timedelta(days=config.hold_days)
        if any(
            entry_at not in markets[symbol]["price"].index
            or exit_at not in markets[symbol]["price"].index
            for symbol in SYMBOLS
        ):
            break
        scores: dict[str, float] = {}
        for symbol in SYMBOLS:
            funding = markets[symbol]["funding"]
            selected = funding[
                (funding.index > decision - timedelta(days=config.lookback_days))
                & (funding.index <= decision)
            ]
            if len(selected) < config.minimum_events:
                scores = {}
                break
            scores[symbol] = float(selected["funding_rate"].sum())
        if scores:
            low = min(SYMBOLS, key=lambda symbol: (scores[symbol], symbol))
            high = min(SYMBOLS, key=lambda symbol: (-scores[symbol], symbol))
            spread = scores[high] - scores[low]
            if low != high and spread >= config.minimum_spread:
                starting_equity = equity
                notional = starting_equity * config.leg_equity_fraction
                legs = [
                    _simulate_leg(
                        markets[low], symbol=low, side="long", entry_at=entry_at,
                        planned_exit_at=exit_at, notional=notional, config=config,
                    ),
                    _simulate_leg(
                        markets[high], symbol=high, side="short", entry_at=entry_at,
                        planned_exit_at=exit_at, notional=notional, config=config,
                    ),
                ]
                latest_exit = max(leg["exit_at"] for leg in legs)
                timeline = markets[low]["price"].loc[entry_at:latest_exit].index
                for timestamp in timeline:
                    marked = starting_equity + sum(
                        _leg_mtm(leg, markets[leg["symbol"]], timestamp, config)
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
                for leg in legs:
                    serial = {
                        key: (value.isoformat() if isinstance(value, pd.Timestamp) else value)
                        for key, value in leg.items()
                        if key != "direction"
                    }
                    serial["decision_at"] = decision.isoformat()
                    serial["score_spread"] = spread
                    legs_out.append(serial)
                weeks.append(
                    {
                        "decision_at": decision.isoformat(),
                        "entry_at": entry_at.isoformat(),
                        "close_at": latest_exit.isoformat(),
                        "long_symbol": low,
                        "short_symbol": high,
                        "low_score": scores[low],
                        "high_score": scores[high],
                        "score_spread": spread,
                        "start_equity": starting_equity,
                        "raw_price_pnl": sum(float(leg["raw_price_pnl"]) for leg in legs),
                        "net_pnl": net,
                        "return": net / starting_equity,
                        "end_equity": equity,
                    }
                )
        decision += timedelta(days=7)
    week_values = [float(item["net_pnl"]) for item in weeks]
    raw_price = sum(float(item["raw_price_pnl"]) for item in legs_out)
    slippage = sum(float(item["modeled_slippage"]) for item in legs_out)
    fees = sum(float(item["fees"]) for item in legs_out)
    funding = sum(float(item["funding_pnl"]) for item in legs_out)
    positive = [value for value in week_values if value > 0]
    midpoint = len(week_values) // 2
    return {
        "eligible_weeks": len(weeks),
        "closed_legs": len(legs_out),
        "ending_equity": equity,
        "net_return": equity / config.starting_equity - 1,
        "profit_factor_by_week": _profit_factor(week_values),
        "maximum_mark_to_market_drawdown": maximum_drawdown,
        "liquidations": sum(bool(leg["liquidated"]) for leg in legs_out),
        "pnl_components": {
            "raw_relative_price_pnl": raw_price,
            "modeled_slippage": slippage,
            "trading_fees": fees,
            "funding_pnl": funding,
            "price_after_fees_and_slippage": raw_price + slippage + fees,
            "net_pnl": raw_price + slippage + fees + funding,
        },
        "first_half_net_pnl": sum(week_values[:midpoint]) if midpoint else None,
        "second_half_net_pnl": sum(week_values[midpoint:]) if midpoint else None,
        "largest_winning_week_concentration": (
            max(positive) / sum(positive) if positive else None
        ),
        "median_score_spread": median(
            [float(item["score_spread"]) for item in weeks]
        ) if weeks else None,
        "configuration": config.__dict__,
        "weeks": weeks,
        "legs": legs_out,
    }


def evaluate_development(
    markets: dict[str, dict[str, pd.DataFrame]],
    *,
    first_decision: str,
    funding_reconciled: bool,
    config: FundingDispersionConfig | None = None,
) -> dict[str, Any]:
    config = config or FundingDispersionConfig()
    base = simulate_forward(markets, first_decision=first_decision, config=config)
    double_fee = simulate_forward(
        markets,
        first_decision=first_decision,
        config=replace(config, fee_per_fill=0.001),
    )
    slippage_stress = simulate_forward(
        markets,
        first_decision=first_decision,
        config=replace(config, slippage_per_fill=0.0005),
    )
    complete = base["eligible_weeks"] >= config.maximum_development_weeks
    pnl = base["pnl_components"]
    pf = base["profit_factor_by_week"]
    concentration = base["largest_winning_week_concentration"]
    checks = {
        "eligible_weeks_minimum_30": base["eligible_weeks"] >= 30,
        "closed_legs_minimum_60": base["closed_legs"] >= 60,
        "net_return_positive": base["net_return"] > 0,
        "raw_relative_price_pnl_positive": pnl["raw_relative_price_pnl"] > 0,
        "price_after_base_fees_and_slippage_positive": pnl["price_after_fees_and_slippage"] > 0,
        "profit_factor_minimum_1_20": pf is not None and pf >= 1.20,
        "double_fee_net_positive": double_fee["net_return"] > 0,
        "five_bps_slippage_net_positive": slippage_stress["net_return"] > 0,
        "first_half_net_positive": base["first_half_net_pnl"] is not None and base["first_half_net_pnl"] > 0,
        "second_half_net_positive": base["second_half_net_pnl"] is not None and base["second_half_net_pnl"] > 0,
        "maximum_mark_to_market_drawdown_at_most_0_15": base["maximum_mark_to_market_drawdown"] <= 0.15,
        "largest_winning_week_concentration_at_most_0_25": concentration is not None and concentration <= 0.25,
        "zero_liquidations": base["liquidations"] == 0,
        "all_funding_rows_reconciled": funding_reconciled,
        "locked_validation_remains_unopened": True,
    }
    status = "ACCUMULATING"
    if complete:
        status = "PASS_DEVELOPMENT" if all(checks.values()) else "FAIL_DEVELOPMENT"
    return {
        "candidate": "ALPHA_013_FUNDING_DISPERSION_REVERSAL",
        "status": status,
        "development": {
            "base": base,
            "double_fee": double_fee,
            "five_bps_slippage": slippage_stress,
        },
        "gate": checks,
        "locked_validation_opened": False,
        "warning": "Alpha #13 is a post-hoc child of Alpha #9 and only fresh post-registration weeks count.",
    }
