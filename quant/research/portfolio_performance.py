"""Recompute portfolio monthly, trade-risk, leverage, and cost-stress evidence."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Any

from quant.research.monthly_target import MonthlyTargetPolicy, evaluate_monthly_target


PORTFOLIO_PERFORMANCE_PROTOCOL_FILES = (
    "quant/research/equity.py",
    "quant/research/monthly_target.py",
    "quant/research/portfolio_performance.py",
    "scripts/portfolio_promotion_report.py",
)

PORTFOLIO_PERFORMANCE_MONTHLY_POLICY = MonthlyTargetPolicy(
    maximum_trade_adverse_excursion=0.20,
    require_slippage_stress_positive=True,
    require_funding_stress_positive=True,
)

TRADE_HEADER = [
    "trade_id",
    "candidate",
    "symbol",
    "position_side",
    "opened_at",
    "closed_at",
    "gross_price_pnl",
    "trading_fees",
    "funding_pnl",
    "modeled_slippage",
    "entry_notional",
    "exit_notional",
    "maximum_leverage",
    "maximum_adverse_excursion",
    "exit_order_role",
    "exit_reason",
]
EXPOSURE_HEADER = [
    "timestamp",
    "maximum_gross_leverage",
    "maximum_absolute_net_leverage",
]
ACCOUNTING_HEADER = [
    "timestamp",
    "gross_price_pnl",
    "trading_fees",
    "funding_pnl",
    "modeled_slippage",
]
MAX_ACCOUNTING_RECONCILIATION_ERROR = 1e-9
MAX_SIMULTANEOUS_STRESS_DRAWDOWN = 0.20
MINIMUM_DAILY_MTM_PROFIT_FACTOR = 1.20


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("portfolio performance timestamps must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _rule_violation_evidence(
    *,
    trades: list[dict[str, Any]],
    exposure: list[dict[str, Any]],
    maximum_gross_leverage: float,
    maximum_absolute_net_leverage: float,
    maximum_trade_leverage_adjusted_mae: float,
) -> dict[str, list[str]]:
    """Count every source-ledger breach of the preservation execution envelope.

    A single evidence row can breach more than one rule. Those are retained as
    separate violations so a later audit has a source-addressable explanation
    instead of a hand-maintained aggregate counter.
    """
    return {
        "trade_maximum_leverage": sorted(
            item["trade_id"]
            for item in trades
            if float(item["maximum_leverage"]) > maximum_gross_leverage
        ),
        "trade_leverage_adjusted_mae": sorted(
            item["trade_id"]
            for item in trades
            if float(item["maximum_leverage"])
            * float(item["maximum_adverse_excursion"])
            > maximum_trade_leverage_adjusted_mae
        ),
        "daily_gross_leverage": [
            item["timestamp"].isoformat()
            for item in exposure
            if float(item["maximum_gross_leverage"]) > maximum_gross_leverage
        ],
        "daily_absolute_net_leverage": [
            item["timestamp"].isoformat()
            for item in exposure
            if float(item["maximum_absolute_net_leverage"])
            > maximum_absolute_net_leverage
        ],
        "liquidation_exit": sorted(
            item["trade_id"]
            for item in trades
            if item["exit_reason"] == "liquidation"
        ),
    }


def load_portfolio_trades(path: Path, candidates: list[str]) -> list[dict[str, Any]]:
    allowed_candidates = set(candidates)
    trades: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != TRADE_HEADER:
            raise ValueError("portfolio trade CSV header does not match the locked schema")
        for line_number, row in enumerate(reader, start=2):
            trade_id = str(row.get("trade_id") or "")
            candidate = str(row.get("candidate") or "")
            symbol = str(row.get("symbol") or "").upper()
            position_side = str(row.get("position_side") or "").lower()
            exit_reason = str(row.get("exit_reason") or "")
            exit_order_role = str(row.get("exit_order_role") or "").lower()
            if not trade_id or trade_id in seen_ids:
                raise ValueError(f"row {line_number} trade_id is missing or duplicated")
            if candidate not in allowed_candidates:
                raise ValueError(f"row {line_number} candidate is not in the manifest")
            if not symbol or position_side not in {"long", "short"} or not exit_reason:
                raise ValueError(f"row {line_number} trade identity is invalid")
            opened_at = _parse_utc(str(row.get("opened_at") or ""))
            closed_at = _parse_utc(str(row.get("closed_at") or ""))
            if opened_at >= closed_at:
                raise ValueError(f"row {line_number} trade close must follow open")
            numeric: dict[str, float] = {}
            for name in (
                "gross_price_pnl",
                "trading_fees",
                "funding_pnl",
                "modeled_slippage",
                "entry_notional",
                "exit_notional",
                "maximum_leverage",
                "maximum_adverse_excursion",
            ):
                try:
                    value = float(row.get(name) or "")
                except ValueError as exc:
                    raise ValueError(f"row {line_number} {name} is invalid") from exc
                if not isfinite(value):
                    raise ValueError(f"row {line_number} {name} must be finite")
                numeric[name] = value
            if numeric["trading_fees"] < 0 or numeric["modeled_slippage"] < 0:
                raise ValueError(f"row {line_number} fees and slippage must be non-negative")
            if numeric["entry_notional"] <= 0 or numeric["exit_notional"] <= 0:
                raise ValueError(f"row {line_number} entry and exit notional must be positive")
            if numeric["maximum_leverage"] <= 0:
                raise ValueError(f"row {line_number} maximum leverage must be positive")
            if not 0 <= numeric["maximum_adverse_excursion"] <= 1:
                raise ValueError(f"row {line_number} MAE must be between zero and one")
            protective_exit_reasons = {
                "stop_loss",
                "stoploss_on_exchange",
                "trailing_stop_loss",
                "liquidation",
            }
            expected_exit_role = (
                "protective_stop" if exit_reason in protective_exit_reasons else "exit"
            )
            if exit_order_role != expected_exit_role:
                raise ValueError(
                    f"row {line_number} exit_order_role does not match exit_reason"
                )
            seen_ids.add(trade_id)
            trades.append(
                {
                    "trade_id": trade_id,
                    "candidate": candidate,
                    "symbol": symbol,
                    "position_side": position_side,
                    "opened_at": opened_at,
                    "closed_at": closed_at,
                    "exit_reason": exit_reason,
                    "exit_order_role": exit_order_role,
                    **numeric,
                }
            )
    return trades


def load_portfolio_exposure(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != EXPOSURE_HEADER:
            raise ValueError("portfolio exposure CSV header does not match the locked schema")
        for line_number, row in enumerate(reader, start=2):
            timestamp = _parse_utc(str(row.get("timestamp") or ""))
            if timestamp.time() != datetime.min.time():
                raise ValueError(f"row {line_number} exposure timestamp must be 00:00 UTC")
            try:
                gross = float(row.get("maximum_gross_leverage") or "")
                net = float(row.get("maximum_absolute_net_leverage") or "")
            except ValueError as exc:
                raise ValueError(f"row {line_number} exposure value is invalid") from exc
            if (
                not isfinite(gross)
                or not isfinite(net)
                or gross < 0
                or net < 0
                or net > gross + 1e-12
            ):
                raise ValueError(f"row {line_number} exposure values are inconsistent")
            rows.append(
                {
                    "timestamp": timestamp,
                    "maximum_gross_leverage": gross,
                    "maximum_absolute_net_leverage": net,
                }
            )
    if not rows:
        raise ValueError("portfolio exposure CSV is empty")
    for previous, current in zip(rows, rows[1:]):
        if current["timestamp"] - previous["timestamp"] != timedelta(days=1):
            raise ValueError("portfolio exposure must contain every consecutive UTC day")
    return rows


def load_portfolio_accounting(path: Path) -> list[dict[str, Any]]:
    """Load daily PnL components for every MTM return interval.

    Values are portfolio-currency amounts for the interval ending at
    ``timestamp``.  Fees and modeled slippage are positive cost magnitudes;
    funding is signed from the portfolio's perspective.
    """
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ACCOUNTING_HEADER:
            raise ValueError("portfolio accounting CSV header does not match the locked schema")
        for line_number, row in enumerate(reader, start=2):
            timestamp = _parse_utc(str(row.get("timestamp") or ""))
            if timestamp.time() != datetime.min.time():
                raise ValueError(f"row {line_number} accounting timestamp must be 00:00 UTC")
            values: dict[str, float] = {}
            for name in ACCOUNTING_HEADER[1:]:
                try:
                    value = float(row.get(name) or "")
                except ValueError as exc:
                    raise ValueError(f"row {line_number} {name} is invalid") from exc
                if not isfinite(value):
                    raise ValueError(f"row {line_number} {name} must be finite")
                values[name] = value
            if values["trading_fees"] < 0 or values["modeled_slippage"] < 0:
                raise ValueError(
                    f"row {line_number} accounting fees and slippage must be non-negative"
                )
            rows.append({"timestamp": timestamp, **values})
    if not rows:
        raise ValueError("portfolio accounting CSV is empty")
    for previous, current in zip(rows, rows[1:]):
        if current["timestamp"] - previous["timestamp"] != timedelta(days=1):
            raise ValueError("portfolio accounting must contain every consecutive UTC day")
    return rows


def _profit_factor(values: list[float]) -> float | None:
    wins = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return wins / losses if losses > 0 else None


def _stressed_equity_path(
    accounting: list[dict[str, Any]],
    *,
    initial_equity: float,
    fee_multiplier: float,
    slippage_multiplier: float,
    adverse_funding: bool,
) -> dict[str, Any]:
    """Replay a fixed-position daily PnL ledger under an adverse cost scenario.

    The positions and gross price PnL remain those actually observed.  Extra
    costs are applied on their original dates so a positive terminal result
    cannot hide an interim insolvency or an unacceptable stressed drawdown.
    """
    equity = peak = float(initial_equity)
    minimum_equity = equity
    maximum_drawdown = 0.0
    ruined = False
    for item in accounting:
        funding = float(item["funding_pnl"])
        stressed_funding = funding - abs(funding) if adverse_funding else funding
        pnl = (
            float(item["gross_price_pnl"])
            - fee_multiplier * float(item["trading_fees"])
            + stressed_funding
            - slippage_multiplier * float(item["modeled_slippage"])
        )
        equity += pnl
        minimum_equity = min(minimum_equity, equity)
        if equity <= 0.0:
            ruined = True
            maximum_drawdown = 1.0
        else:
            peak = max(peak, equity)
            maximum_drawdown = max(maximum_drawdown, 1.0 - equity / peak)
    return {
        "terminal_return": equity / initial_equity - 1.0,
        "maximum_drawdown": maximum_drawdown,
        "minimum_equity_fraction": minimum_equity / initial_equity,
        "ruined": ruined,
    }


def _reconstruct_exposure_lower_bound(
    *,
    trades: list[dict[str, Any]],
    exposure: list[dict[str, Any]],
    daily_series: list[dict[str, Any]],
    initial_equity: float,
) -> dict[str, Any]:
    """Reconstruct a conservative daily leverage floor from overlapping trades."""
    prior_equity = float(initial_equity)
    maximum_gross = 0.0
    maximum_net = 0.0
    maximum_gross_understatement = 0.0
    maximum_net_understatement = 0.0
    rows_evaluated = 0
    for exposure_row, daily_item in zip(exposure, daily_series):
        interval_end = exposure_row["timestamp"]
        interval_start = interval_end - timedelta(days=1)
        ending_equity = prior_equity * (1.0 + float(daily_item.get("return") or 0.0))
        conservative_equity = min(prior_equity, ending_equity)
        active_gross = 0.0
        active_net = 0.0
        events: list[tuple[datetime, int, float, float]] = []
        for trade in trades:
            notional = max(
                float(trade["entry_notional"]), float(trade["exit_notional"])
            )
            signed_notional = notional if trade["position_side"] == "long" else -notional
            if trade["opened_at"] < interval_start and trade["closed_at"] > interval_start:
                active_gross += notional
                active_net += signed_notional
            if interval_start <= trade["opened_at"] < interval_end:
                events.append((trade["opened_at"], 1, notional, signed_notional))
            if interval_start < trade["closed_at"] < interval_end:
                events.append((trade["closed_at"], 0, -notional, -signed_notional))
        day_maximum_gross = active_gross
        day_maximum_net = abs(active_net)
        for _, _, gross_delta, net_delta in sorted(
            events, key=lambda item: (item[0], item[1])
        ):
            active_gross += gross_delta
            active_net += net_delta
            day_maximum_gross = max(day_maximum_gross, active_gross)
            day_maximum_net = max(day_maximum_net, abs(active_net))
        gross_leverage = day_maximum_gross / conservative_equity
        net_leverage = day_maximum_net / conservative_equity
        maximum_gross = max(maximum_gross, gross_leverage)
        maximum_net = max(maximum_net, net_leverage)
        maximum_gross_understatement = max(
            maximum_gross_understatement,
            gross_leverage - float(exposure_row["maximum_gross_leverage"]),
        )
        maximum_net_understatement = max(
            maximum_net_understatement,
            net_leverage - float(exposure_row["maximum_absolute_net_leverage"]),
        )
        prior_equity = ending_equity
        rows_evaluated += 1
    return {
        "rows_evaluated": rows_evaluated,
        "reported_rows": len(exposure),
        "reconstructed_maximum_gross_leverage": maximum_gross,
        "reconstructed_maximum_absolute_net_leverage": maximum_net,
        "maximum_gross_leverage_understatement": max(
            0.0, maximum_gross_understatement
        ),
        "maximum_absolute_net_leverage_understatement": max(
            0.0, maximum_net_understatement
        ),
        "notional_convention": "max(entry_notional, exit_notional) held for the full trade interval",
        "equity_denominator": "min(start_of_day_mtm_equity, end_of_day_mtm_equity)",
    }


def evaluate_portfolio_performance(
    *,
    trades: list[dict[str, Any]],
    exposure: list[dict[str, Any]],
    accounting: list[dict[str, Any]],
    equity: dict[str, Any],
    portfolio_risk_audit: dict[str, Any],
    evidence: dict[str, Any],
    candidates: list[str],
    execution_scopes: list[dict[str, Any]] | None = None,
    maximum_simultaneous_stress_drawdown: float = MAX_SIMULTANEOUS_STRESS_DRAWDOWN,
    minimum_daily_mtm_profit_factor: float = MINIMUM_DAILY_MTM_PROFIT_FACTOR,
    maximum_gross_leverage: float = 1.0,
    maximum_absolute_net_leverage: float = 1.0,
    maximum_trade_leverage_adjusted_mae: float = 0.20,
    maximum_rule_violations: int = 0,
) -> dict[str, Any]:
    if (
        not all(
            isinstance(value, (int, float))
            and isfinite(float(value))
            and float(value) >= 0.0
            for value in (
                maximum_gross_leverage,
                maximum_absolute_net_leverage,
                maximum_trade_leverage_adjusted_mae,
            )
        )
        or not isinstance(maximum_rule_violations, int)
        or isinstance(maximum_rule_violations, bool)
        or maximum_rule_violations < 0
    ):
        raise ValueError("portfolio rule-violation policy is invalid")
    daily = equity.get("daily") or {}
    monthly = equity.get("monthly") or {}
    daily_series = daily.get("series") or []
    expected_dates = [str(item.get("date") or "") for item in daily_series]
    exposure_dates = [item["timestamp"].date().isoformat() for item in exposure]
    exposure_aligned = exposure_dates == expected_dates
    accounting_dates = [item["timestamp"].date().isoformat() for item in accounting]
    accounting_aligned = accounting_dates == expected_dates
    coverage_start = datetime.fromisoformat(
        str(equity.get("coverage_start") or "") + "T00:00:00+00:00"
    )
    coverage_end = datetime.fromisoformat(
        str(equity.get("coverage_end") or "") + "T00:00:00+00:00"
    )
    trades_aligned = bool(trades) and all(
        coverage_start <= item["opened_at"] < item["closed_at"] <= coverage_end
        for item in trades
    )
    force_exits = [item for item in trades if item["exit_reason"] == "force_exit"]
    invalid_force_exit_ids = sorted(
        item["trade_id"] for item in force_exits if item["closed_at"] != coverage_end
    )
    boundary_force_exit_audit = {
        "force_exit_trades": len(force_exits),
        "invalid_force_exit_trade_ids": invalid_force_exit_ids,
        "required_close_at_utc": coverage_end.isoformat(),
    }
    candidate_inventory = sorted({item["candidate"] for item in trades})
    organic = [
        item
        for item in trades
        if item["exit_reason"] not in {"force_exit", "liquidation"}
    ]
    organic_net = [
        item["gross_price_pnl"]
        - item["trading_fees"]
        + item["funding_pnl"]
        - item["modeled_slippage"]
        for item in organic
    ]
    all_trade_net = sum(
        item["gross_price_pnl"]
        - item["trading_fees"]
        + item["funding_pnl"]
        - item["modeled_slippage"]
        for item in trades
    )
    initial_equity = equity.get("initial_equity")
    if not isinstance(initial_equity, (int, float)) or not isfinite(initial_equity) or initial_equity <= 0:
        raise ValueError("portfolio MTM evidence must include positive initial_equity")
    final_equity = equity.get("final_equity")
    if not isinstance(final_equity, (int, float)) or not isfinite(final_equity) or final_equity <= 0:
        raise ValueError("portfolio MTM evidence must include positive final_equity")
    trade_mtm_reconciliation_error = abs(
        all_trade_net - (float(final_equity) - float(initial_equity))
    ) / float(initial_equity)

    trade_component_totals = {
        name: sum(float(item[name]) for item in trades)
        for name in (
            "gross_price_pnl",
            "trading_fees",
            "funding_pnl",
            "modeled_slippage",
        )
    }
    accounting_component_totals = {
        name: sum(float(item[name]) for item in accounting)
        for name in trade_component_totals
    }
    component_reconciliation_errors = {
        name: abs(trade_component_totals[name] - accounting_component_totals[name])
        / float(initial_equity)
        for name in trade_component_totals
    }

    scope_by_role: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    duplicate_cost_scopes = False
    for scope in execution_scopes or []:
        key = (
            str(scope.get("candidate") or ""),
            str(scope.get("symbol") or "").upper(),
            str(scope.get("position_side") or "").lower(),
            str(scope.get("order_role") or "").lower(),
        )
        if key in scope_by_role:
            duplicate_cost_scopes = True
        scope_by_role[key] = scope
    fee_floor_total = 0.0
    all_in_floor_total = 0.0
    maximum_fee_shortfall = 0.0
    maximum_all_in_shortfall = 0.0
    covered_cost_trades = 0
    for trade in trades:
        identity = (
            trade["candidate"],
            trade["symbol"],
            trade["position_side"],
        )
        entry_scope = scope_by_role.get((*identity, "entry"))
        exit_scope = scope_by_role.get((*identity, trade["exit_order_role"]))
        if entry_scope is None or exit_scope is None:
            continue
        values = (
            entry_scope.get("fee_bps"),
            entry_scope.get("maximum_all_in_drag_bps"),
            exit_scope.get("fee_bps"),
            exit_scope.get("maximum_all_in_drag_bps"),
        )
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not isfinite(float(value))
            or float(value) < 0.0
            for value in values
        ):
            continue
        entry_notional = float(trade["entry_notional"])
        exit_notional = float(trade["exit_notional"])
        fee_floor = (
            entry_notional * float(entry_scope["fee_bps"])
            + exit_notional * float(exit_scope["fee_bps"])
        ) / 10_000.0
        all_in_floor = (
            entry_notional * float(entry_scope["maximum_all_in_drag_bps"])
            + exit_notional * float(exit_scope["maximum_all_in_drag_bps"])
        ) / 10_000.0
        fee_floor_total += fee_floor
        all_in_floor_total += all_in_floor
        maximum_fee_shortfall = max(
            maximum_fee_shortfall, fee_floor - float(trade["trading_fees"])
        )
        maximum_all_in_shortfall = max(
            maximum_all_in_shortfall,
            all_in_floor
            - float(trade["trading_fees"])
            - float(trade["modeled_slippage"]),
        )
        covered_cost_trades += 1
    execution_cost_floor = {
        "trades_evaluated": covered_cost_trades,
        "total_trades": len(trades),
        "duplicate_scope_roles": duplicate_cost_scopes,
        "minimum_total_trading_fees": fee_floor_total,
        "minimum_total_all_in_cost": all_in_floor_total,
        "recorded_total_trading_fees": trade_component_totals["trading_fees"],
        "recorded_total_modeled_slippage": trade_component_totals["modeled_slippage"],
        "maximum_trade_fee_shortfall": max(0.0, maximum_fee_shortfall),
        "maximum_trade_all_in_shortfall": max(0.0, maximum_all_in_shortfall),
    }

    prior_equity = float(initial_equity)
    accounting_errors: list[float] = []
    daily_accounting_net: list[float] = []
    for item, daily_item in zip(accounting, daily_series):
        expected_change = prior_equity * float(daily_item.get("return") or 0.0)
        accounted_change = (
            item["gross_price_pnl"]
            - item["trading_fees"]
            + item["funding_pnl"]
            - item["modeled_slippage"]
        )
        accounting_errors.append(abs(accounted_change - expected_change) / prior_equity)
        daily_accounting_net.append(accounted_change)
        prior_equity += expected_change
    maximum_accounting_error = (
        max(accounting_errors, default=None) if accounting_aligned else None
    )
    exposure_reconciliation = _reconstruct_exposure_lower_bound(
        trades=trades,
        exposure=exposure,
        daily_series=daily_series,
        initial_equity=float(initial_equity),
    )
    rule_violation_evidence = _rule_violation_evidence(
        trades=trades,
        exposure=exposure,
        maximum_gross_leverage=float(maximum_gross_leverage),
        maximum_absolute_net_leverage=float(maximum_absolute_net_leverage),
        maximum_trade_leverage_adjusted_mae=float(
            maximum_trade_leverage_adjusted_mae
        ),
    )
    rule_violations = sum(len(values) for values in rule_violation_evidence.values())
    daily_gross_profit = sum(value for value in daily_accounting_net if value > 0.0)
    daily_gross_loss = -sum(value for value in daily_accounting_net if value < 0.0)
    daily_mtm_profit_factor = _profit_factor(daily_accounting_net)

    base = {
        "monthly": monthly,
        "max_drawdown": equity.get("max_drawdown"),
        "equity_basis": equity.get("equity_basis"),
        "organic": {
            "trades": len(organic),
            "profit_factor": _profit_factor(organic_net),
            "net_return": sum(organic_net) / initial_equity,
        },
        "trade_risk": {
            "excursion_coverage_ratio": 1.0 if trades else 0.0,
            "slippage_coverage_ratio": 1.0 if trades else 0.0,
            "funding_coverage_ratio": 1.0 if trades else 0.0,
            "maximum_leverage": max(
                (item["maximum_leverage"] for item in trades), default=None
            ),
            "maximum_leverage_adjusted_mae": max(
                (
                    item["maximum_leverage"]
                    * item["maximum_adverse_excursion"]
                    for item in trades
                ),
                default=None,
            ),
            "liquidation_exits": sum(
                item["exit_reason"] == "liquidation" for item in trades
            ),
        },
    }
    stress_specifications = {
        "double_fee": (2.0, 1.0, False),
        "double_slippage": (1.0, 2.0, False),
        "adverse_funding": (1.0, 1.0, True),
        "simultaneous": (2.0, 2.0, True),
    }
    stress_paths = {
        name: _stressed_equity_path(
            accounting,
            initial_equity=float(initial_equity),
            fee_multiplier=fee_multiplier,
            slippage_multiplier=slippage_multiplier,
            adverse_funding=adverse_funding,
        )
        for name, (fee_multiplier, slippage_multiplier, adverse_funding) in stress_specifications.items()
    }
    double_cost_return = stress_paths["double_fee"]["terminal_return"]
    slippage_stress_return = stress_paths["double_slippage"]["terminal_return"]
    funding_stress_return = stress_paths["adverse_funding"]["terminal_return"]
    simultaneous_path = stress_paths["simultaneous"]
    simultaneous_return = simultaneous_path["terminal_return"]
    aggregate_risk = portfolio_risk_audit.get("aggregate") or {}
    risk = {
        "probability_of_ruin": aggregate_risk.get(
            "probability_of_ruin_upper_confidence"
        ),
        "probability_terminal_loss": aggregate_risk.get(
            "probability_terminal_loss_upper_confidence"
        ),
        "probability_reaching_annual_target": aggregate_risk.get(
            "probability_reaching_annual_target_lower_confidence"
        ),
        "maximum_drawdown_p95": aggregate_risk.get("maximum_drawdown_p95"),
    }
    monthly_gate = evaluate_monthly_target(
        base,
        double_cost={"organic": {"net_return": double_cost_return}},
        slippage_stress={"organic": {"net_return": slippage_stress_return}},
        funding_stress={"organic": {"net_return": funding_stress_return}},
        risk=risk,
        evidence=evidence,
        policy=PORTFOLIO_PERFORMANCE_MONTHLY_POLICY,
    )
    checks = [
        {
            "name": "candidate_trade_inventory",
            "passed": set(candidate_inventory) == set(candidates),
            "actual": candidate_inventory,
            "required": candidates,
        },
        {
            "name": "daily_exposure_alignment",
            "passed": exposure_aligned,
            "actual": {
                "first": exposure_dates[0] if exposure_dates else None,
                "last": exposure_dates[-1] if exposure_dates else None,
                "rows": len(exposure_dates),
            },
            "required": "exactly one row for every portfolio MTM return date",
        },
        {
            "name": "intraday_exposure_reconciliation",
            "passed": exposure_aligned
            and exposure_reconciliation["rows_evaluated"] == len(exposure)
            and exposure_reconciliation["maximum_gross_leverage_understatement"]
            <= 1e-12
            and exposure_reconciliation[
                "maximum_absolute_net_leverage_understatement"
            ]
            <= 1e-12,
            "actual": exposure_reconciliation,
            "required": "reported daily maxima must cover the conservative overlap-derived gross and absolute-net leverage lower bounds",
        },
        {
            "name": "daily_accounting_alignment",
            "passed": accounting_aligned,
            "actual": {
                "first": accounting_dates[0] if accounting_dates else None,
                "last": accounting_dates[-1] if accounting_dates else None,
                "rows": len(accounting_dates),
            },
            "required": "exactly one row for every portfolio MTM return date",
        },
        {
            "name": "daily_accounting_mtm_reconciliation",
            "passed": maximum_accounting_error is not None
            and maximum_accounting_error <= MAX_ACCOUNTING_RECONCILIATION_ERROR,
            "actual": maximum_accounting_error,
            "required": f"<= {MAX_ACCOUNTING_RECONCILIATION_ERROR}",
        },
        {
            "name": "trade_sample_alignment",
            "passed": trades_aligned,
            "actual": {
                "earliest_open": min(
                    (item["opened_at"].isoformat() for item in trades), default=None
                ),
                "latest_close": max(
                    (item["closed_at"].isoformat() for item in trades), default=None
                ),
            },
            "required": f"all trades within {coverage_start.isoformat()}..{coverage_end.isoformat()}",
        },
        {
            "name": "boundary_force_exit_integrity",
            "passed": not invalid_force_exit_ids,
            "actual": boundary_force_exit_audit,
            "required": "every force_exit closes exactly at the locked MTM sample end",
        },
        {
            "name": "closed_trade_mtm_total_reconciliation",
            "passed": trade_mtm_reconciliation_error
            <= MAX_ACCOUNTING_RECONCILIATION_ERROR,
            "actual": trade_mtm_reconciliation_error,
            "required": f"<= {MAX_ACCOUNTING_RECONCILIATION_ERROR}",
        },
        {
            "name": "closed_trade_mtm_component_reconciliation",
            "passed": all(
                value <= MAX_ACCOUNTING_RECONCILIATION_ERROR
                for value in component_reconciliation_errors.values()
            ),
            "actual": component_reconciliation_errors,
            "required": f"every PnL component <= {MAX_ACCOUNTING_RECONCILIATION_ERROR}",
        },
        {
            "name": "preregistered_execution_cost_floor",
            "passed": bool(trades)
            and not duplicate_cost_scopes
            and covered_cost_trades == len(trades)
            and execution_cost_floor["maximum_trade_fee_shortfall"] <= 1e-12
            and execution_cost_floor["maximum_trade_all_in_shortfall"] <= 1e-12,
            "actual": execution_cost_floor,
            "required": "every trade charges at least its preregistered entry/exit fee and all-in drag budgets",
        },
        {
            "name": "capital_preservation_rule_violations",
            "passed": rule_violations <= maximum_rule_violations,
            "actual": {
                "count": rule_violations,
                "evidence": rule_violation_evidence,
            },
            "required": f"<= {maximum_rule_violations} source-ledger risk-envelope violations",
        },
        {
            "name": "monthly_target_gate",
            "passed": monthly_gate["status"] == "PASS",
            "actual": monthly_gate["status"],
            "required": "PASS",
        },
        {
            "name": "daily_mtm_profit_factor",
            "passed": daily_gross_profit > 0.0
            and (
                daily_gross_loss == 0.0
                or (
                    daily_mtm_profit_factor is not None
                    and daily_mtm_profit_factor >= minimum_daily_mtm_profit_factor
                )
            ),
            "actual": {
                "profit_factor": daily_mtm_profit_factor,
                "gross_profit": daily_gross_profit,
                "gross_loss": daily_gross_loss,
            },
            "required": f">= {minimum_daily_mtm_profit_factor}, or positive gross profit with zero losing days",
        },
        {
            "name": "simultaneous_cost_funding_stress",
            "passed": simultaneous_return > 0.0 and not simultaneous_path["ruined"],
            "actual": simultaneous_return,
            "required": "> 0 and no interim insolvency",
        },
        {
            "name": "simultaneous_cost_funding_stress_drawdown",
            "passed": not simultaneous_path["ruined"]
            and simultaneous_path["maximum_drawdown"]
            <= maximum_simultaneous_stress_drawdown,
            "actual": simultaneous_path["maximum_drawdown"],
            "required": f"<= {maximum_simultaneous_stress_drawdown}",
        },
    ]
    return {
        "schema_version": 1,
        "status": "PASS" if all(item["passed"] for item in checks) else "FAIL",
        "candidate_order": candidates,
        "trade_rows": len(trades),
        "organic_trades": len(organic),
        "accounting_rows": len(accounting),
        "maximum_daily_accounting_reconciliation_error": maximum_accounting_error,
        "closed_trade_mtm_total_reconciliation_error": trade_mtm_reconciliation_error,
        "closed_trade_mtm_component_reconciliation_errors": component_reconciliation_errors,
        "execution_cost_floor": execution_cost_floor,
        "rule_violations": rule_violations,
        "rule_violation_evidence": rule_violation_evidence,
        "exposure_reconciliation": exposure_reconciliation,
        "boundary_force_exit_audit": boundary_force_exit_audit,
        "monthly_target_gate": monthly_gate,
        "daily_mtm_profit_factor": daily_mtm_profit_factor,
        "daily_mtm_gross_profit": daily_gross_profit,
        "daily_mtm_gross_loss": daily_gross_loss,
        "maximum_gross_leverage": max(
            (item["maximum_gross_leverage"] for item in exposure), default=None
        ),
        "maximum_absolute_net_leverage": max(
            (item["maximum_absolute_net_leverage"] for item in exposure), default=None
        ),
        "maximum_trade_leverage_adjusted_mae": base["trade_risk"][
            "maximum_leverage_adjusted_mae"
        ],
        "liquidations": base["trade_risk"]["liquidation_exits"],
        "stress_returns": {
            "double_fee": double_cost_return,
            "double_slippage": slippage_stress_return,
            "adverse_funding": funding_stress_return,
            "simultaneous": simultaneous_return,
        },
        "stress_paths": stress_paths,
        "checks": checks,
        "definitions": {
            "net_trade_pnl": "gross_price_pnl - trading_fees + funding_pnl - modeled_slippage",
            "daily_accounting_pnl": "gross_price_pnl - trading_fees + funding_pnl - modeled_slippage equals the corresponding MTM equity change",
            "adverse_funding": "funding_pnl - abs(funding_pnl)",
            "simultaneous": "2x fees + 2x modeled slippage + adverse funding",
            "organic": "all closed trades except force_exit and liquidation",
            "stress_source": "daily accounting ledger including realized and unrealized mark-to-market PnL",
            "closed_trade_boundary": "the complete ledger, including deterministic boundary force exits, must reconcile to final minus initial MTM equity; force exits remain excluded from organic trade count and profit factor",
            "rule_violations": "each source-ledger breach of the gross leverage, absolute-net leverage, leverage-adjusted MAE, or no-liquidation execution envelope; multiple breached rules on one row count separately",
        },
    }
