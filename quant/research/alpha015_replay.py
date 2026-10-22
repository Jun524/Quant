"""Source-bound daily replay for the preregistered Alpha 015 protocol.

The entry point accepts only a phase bundle that has already replayed its
publisher checksums and phase boundary.  It forms every same-day portfolio
from causal one-minute returns, selects boundary prices from aggregate trades,
and hands the result to the shared turnover/funding/MTM simulator.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from quant.research.alpha015_data_guard import (
    Alpha015DataError,
    assert_manifest_phase_access,
    binance_archive_symbol,
    iter_reverified_archive_rows,
    load_reverified_funding_events,
    validate_aggtrade_day_chain,
    validate_phase_archive_inventory,
)
from quant.research.alpha015_simulation import (
    returns_from_closed_klines,
    simulate_daily_path,
)
from quant.research.cross_crypto_predictor import (
    AdaptiveLassoKernelConfig,
    execution_window,
    forecast_cross_section,
    quintile_target_weights,
)


class Alpha015ReplayError(RuntimeError):
    pass


def _utc_timestamp(value: str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return (
        timestamp.tz_localize("UTC")
        if timestamp.tzinfo is None
        else timestamp.tz_convert("UTC")
    )


def _date_timestamp(value: str | date) -> pd.Timestamp:
    day = date.fromisoformat(value) if isinstance(value, str) else value
    return pd.Timestamp(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc))


def aggregate_trade_boundary_prices(
    events: Iterable[Mapping[str, Any]],
    *,
    boundaries: Iterable[str | pd.Timestamp],
    maximum_delay_ms: int = 60_000,
) -> dict[pd.Timestamp, dict[str, Any]]:
    """Select the first chronological aggregate trade at each boundary.

    This single streaming pass is intentionally used instead of searching a
    preloaded aggTrades day.  A late or absent reference rejects the whole day.
    """
    if maximum_delay_ms < 0:
        raise Alpha015ReplayError("aggregate-trade delay ceiling is invalid")
    ordered = sorted({_utc_timestamp(value) for value in boundaries})
    if not ordered:
        raise Alpha015ReplayError("aggregate-trade boundaries are empty")
    results: dict[pd.Timestamp, dict[str, Any]] = {}
    next_index = 0
    previous_time: int | None = None
    for event in events:
        try:
            event_time = int(event["transact_time"])
            price = float(event["price"])
            aggregate_id = int(event["agg_trade_id"])
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise Alpha015ReplayError("aggregate-trade replay row is invalid") from error
        if (
            previous_time is not None
            and event_time < previous_time
        ) or not np.isfinite(price) or price <= 0:
            raise Alpha015ReplayError("aggregate-trade replay is not chronological")
        previous_time = event_time
        while next_index < len(ordered):
            boundary = ordered[next_index]
            boundary_ms = int(boundary.timestamp() * 1000)
            if event_time < boundary_ms:
                break
            if event_time - boundary_ms > maximum_delay_ms:
                raise Alpha015ReplayError(
                    "no aggregate trade inside the execution-delay ceiling"
                )
            results[boundary] = {
                "boundary_at": boundary.isoformat(),
                "trade_at": pd.to_datetime(event_time, unit="ms", utc=True).isoformat(),
                "delay_ms": event_time - boundary_ms,
                "price": price,
                "agg_trade_id": aggregate_id,
            }
            next_index += 1
    if next_index != len(ordered):
        raise Alpha015ReplayError("aggregate-trade archive ended before an execution boundary")
    return results


def forecast_future_row_invariance(
    returns: pd.DataFrame,
    *,
    decision_at: str | pd.Timestamp,
    config: AdaptiveLassoKernelConfig | None = None,
    penalty: float | None = None,
) -> bool:
    """Prove one forecast is unchanged when every strictly future row changes."""
    config = config or AdaptiveLassoKernelConfig()
    decision = _utc_timestamp(decision_at)
    if not isinstance(returns.index, pd.DatetimeIndex) or decision not in returns.index:
        raise Alpha015ReplayError("future-row invariance decision is not in the panel")
    future = returns.index > decision
    if not future.any():
        raise Alpha015ReplayError("future-row invariance requires rows after the decision")
    baseline = forecast_cross_section(
        returns, decision_at=decision, penalty=penalty, config=config
    )
    changed = returns.copy()
    values = changed.loc[future].to_numpy(dtype=float)
    # This deterministic, non-affine perturbation changes no row available at
    # the decision boundary and makes accidental future access observable.
    changed.loc[future] = values * -1.731 + 0.000913
    replay = forecast_cross_section(
        changed, decision_at=decision, penalty=penalty, config=config
    )
    return (
        baseline["forecasts"] == replay["forecasts"]
        and baseline["selected_predictors"] == replay["selected_predictors"]
        and baseline["penalties_by_target"] == replay["penalties_by_target"]
        and baseline["penalty_selection_receipts"] == replay["penalty_selection_receipts"]
        and baseline["receipt"] == replay["receipt"]
    )


def prepare_alpha015_phase_bundle(
    *,
    manifest: dict[str, Any],
    activation_lock: dict[str, Any],
    archive_root: Path,
    phase: str,
    expected_preregistration_sha256: str,
    validation_unlock: dict[str, Any] | None = None,
    expected_development_report_sha256: str | None = None,
) -> dict[str, Any]:
    """Reverify and index the exact files available to one research phase."""
    if phase not in {"development", "locked_validation"}:
        raise Alpha015ReplayError("Alpha 015 replay phase is invalid")
    if (
        manifest.get("phase") != phase
        or manifest.get("status") != "PASS"
        or manifest.get("activation_lock_sha256") != activation_lock.get("lock_sha256")
        or manifest.get("preregistration_sha256") != expected_preregistration_sha256
    ):
        raise Alpha015ReplayError("phase manifest is not bound to the preregistration")
    try:
        inventory = validate_phase_archive_inventory(
            manifest, activation_lock, phase=phase
        )
        verified = assert_manifest_phase_access(
            manifest,
            activation_lock,
            archive_root=archive_root,
            phase=phase,
            validation_unlock=validation_unlock,
            expected_preregistration_sha256=expected_preregistration_sha256,
            expected_development_report_sha256=expected_development_report_sha256,
        )
        chain = validate_aggtrade_day_chain(verified)
    except Alpha015DataError as error:
        raise Alpha015ReplayError(f"phase bundle failed source replay: {error}") from error
    indexed: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in verified:
        key = (str(item["dataset"]), str(item["symbol"]), str(item["date"]))
        if key in indexed:
            raise Alpha015ReplayError("phase bundle has duplicate verified files")
        indexed[key] = item
    return {
        "phase": phase,
        "activation_lock_sha256": activation_lock["lock_sha256"],
        "preregistration_sha256": expected_preregistration_sha256,
        "archive_root": str(archive_root.resolve()),
        "records": indexed,
        "source_validation": {
            "inventory": inventory,
            "aggtrade_chain": chain,
            "publisher_files_reverified": len(verified),
        },
    }


def _binding_for_day(activation_lock: Mapping[str, Any], day: date) -> dict[str, Any]:
    matches = [
        item
        for item in activation_lock.get("bindings") or []
        if str(item.get("evaluation_date") or "") == day.isoformat()
    ]
    if len(matches) != 1:
        raise Alpha015ReplayError("evaluation day has no unique prior-day universe binding")
    binding = matches[0]
    symbols = list(map(str, binding.get("eligible_symbols") or []))
    if len(symbols) < 30 or len(symbols) != len(set(symbols)):
        raise Alpha015ReplayError("evaluation day has an invalid eligible universe")
    membership = date.fromisoformat(str(binding.get("membership_snapshot_date") or ""))
    if day != membership + timedelta(days=1):
        raise Alpha015ReplayError("evaluation membership binding is not one UTC day lagged")
    return binding


def _record(
    bundle: Mapping[str, Any], dataset: str, symbol: str, day: date
) -> dict[str, Any]:
    item = (bundle.get("records") or {}).get((dataset, symbol, day.isoformat()))
    if not isinstance(item, dict):
        raise Alpha015ReplayError(
            f"authorized source file is missing: {dataset}|{symbol}|{day.isoformat()}"
        )
    return item


def _kline_inputs(
    bundle: Mapping[str, Any], *, binding: Mapping[str, Any], day: date
) -> tuple[pd.DataFrame, dict[pd.Timestamp, dict[str, float]], tuple[str, ...]]:
    membership_day = date.fromisoformat(str(binding["membership_snapshot_date"]))
    archive_root = Path(str(bundle["archive_root"]))
    symbols = tuple(sorted(binance_archive_symbol(str(value)) for value in binding["eligible_symbols"]))
    klines: dict[str, list[dict[str, Any]]] = {}
    marks: dict[pd.Timestamp, dict[str, float]] = {}
    for symbol in symbols:
        history: list[dict[str, Any]] = []
        for source_day in (membership_day, day):
            record = _record(bundle, "klines_1m", symbol, source_day)
            rows = list(iter_reverified_archive_rows(record, archive_root=archive_root))
            history.extend(
                {"open_time": int(row["open_time"]), "close": float(row["close"])}
                for row in rows
            )
            if source_day == day:
                for row in rows:
                    timestamp = pd.to_datetime(int(row["open_time"]) + 60_000, unit="ms", utc=True)
                    marks.setdefault(timestamp, {})[symbol] = float(row["close"])
        klines[symbol] = history
    if any(set(prices) != set(symbols) for prices in marks.values()):
        raise Alpha015ReplayError("current-day closed-minute mark panel is incomplete")
    return returns_from_closed_klines(klines), marks, symbols


def _execution_and_funding(
    bundle: Mapping[str, Any], *, symbols: tuple[str, ...], day: date,
    boundaries: list[pd.Timestamp],
) -> tuple[dict[str, dict[pd.Timestamp, dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    archive_root = Path(str(bundle["archive_root"]))
    executions: dict[str, dict[pd.Timestamp, dict[str, Any]]] = {}
    funding: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        aggregate = _record(bundle, "aggTrades", symbol, day)
        executions[symbol] = aggregate_trade_boundary_prices(
            iter_reverified_archive_rows(aggregate, archive_root=archive_root),
            boundaries=boundaries,
        )
        funding_record = _record(bundle, "funding", symbol, day)
        funding[symbol] = load_reverified_funding_events(
            funding_record, archive_root=archive_root
        )
    return executions, funding


def replay_alpha015_day(
    bundle: Mapping[str, Any], *, activation_lock: Mapping[str, Any], evaluation_day: str | date,
    config: AdaptiveLassoKernelConfig | None = None,
    verify_future_row_invariance: bool = False,
) -> dict[str, Any]:
    """Replay one complete authorized day with no rows after each decision."""
    config = config or AdaptiveLassoKernelConfig()
    day = date.fromisoformat(evaluation_day) if isinstance(evaluation_day, str) else evaluation_day
    binding = _binding_for_day(activation_lock, day)
    returns, closed_marks, symbols = _kline_inputs(bundle, binding=binding, day=day)
    midnight = _date_timestamp(day)
    decision_times = [midnight + pd.to_timedelta(offset * 10, unit="min") for offset in range(143)]
    windows = [execution_window(value, config=config) for value in decision_times]
    boundaries = sorted(
        {
            _utc_timestamp(window[key])
            for window in windows
            for key in ("earliest_entry_at", "exit_at")
        }
    )
    executions, funding = _execution_and_funding(
        bundle, symbols=symbols, day=day, boundaries=boundaries
    )
    decisions: list[dict[str, Any]] = []
    interior_marks: dict[pd.Timestamp, dict[str, float]] = {}
    decision_receipts: list[dict[str, Any]] = []
    for decision_at, window in zip(decision_times, windows):
        forecast = forecast_cross_section(returns, decision_at=decision_at, penalty=None, config=config)
        weights = quintile_target_weights(forecast["forecasts"], config=config)
        entry_at = _utc_timestamp(window["earliest_entry_at"])
        exit_at = _utc_timestamp(window["exit_at"])
        decisions.append(
            {
                "decision_at": decision_at,
                "universe_symbols": symbols,
                "target_weights": weights,
                "entry_prices": {symbol: executions[symbol][entry_at]["price"] for symbol in weights},
                "exit_prices": {symbol: executions[symbol][exit_at]["price"] for symbol in weights},
            }
        )
        for offset in range(1, config.holding_minutes):
            mark_at = entry_at + pd.to_timedelta(offset, unit="min")
            try:
                interior_marks[mark_at] = {
                    symbol: closed_marks[mark_at][symbol] for symbol in weights
                }
            except KeyError as error:
                raise Alpha015ReplayError("required closed-minute mark is unavailable") from error
        decision_receipts.append(
            {
                "decision_at": decision_at.isoformat(),
                "future_rows_read": int((forecast["receipt"] or {})["future_rows_read"]),
                "feature_end_at": (forecast["receipt"] or {})["feature_end_at"],
                "target_end_at": (forecast["receipt"] or {})["target_end_at"],
                "positions": len(weights),
                "future_row_invariant": (
                    forecast_future_row_invariance(
                        returns, decision_at=decision_at, config=config
                    )
                    if verify_future_row_invariance
                    else None
                ),
            }
        )
    simulation = simulate_daily_path(
        decisions,
        funding_events=funding,
        interior_mark_prices=interior_marks,
        config=config,
    )
    if any(item["future_rows_read"] != 0 for item in decision_receipts):
        raise Alpha015ReplayError("forecast receipt reports a future-row read")
    if verify_future_row_invariance and not all(
        item["future_row_invariant"] is True for item in decision_receipts
    ):
        raise Alpha015ReplayError("future-row invariance failed during daily replay")
    return {
        "status": "PASS",
        "phase": bundle["phase"],
        "evaluation_date": day.isoformat(),
        "universe_symbols": list(symbols),
        "decision_receipts": decision_receipts,
        "simulation": simulation,
        "source_validation": bundle["source_validation"],
    }
