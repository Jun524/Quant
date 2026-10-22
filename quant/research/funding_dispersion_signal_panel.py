"""Source-bound, non-promotional signal panel for Alpha #13 diagnostics.

The Alpha #13 forward simulator remains the sole strategy evaluator.  This
module only derives a Qlib-style cross-sectional signal/label panel from the
same sealed sources so predictive diagnostics never depend on hand-entered
forward returns.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from quant.research.funding_dispersion_reversal import (
    SYMBOLS,
    FundingDispersionConfig,
)
from quant.research.signal_diagnostics import SignalObservation


def _timestamp(value: str | datetime | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return (
        timestamp.tz_localize("UTC")
        if timestamp.tzinfo is None
        else timestamp.tz_convert("UTC")
    )


def assert_alpha013_signal_contract(preregistration: dict[str, Any]) -> None:
    """Reject a panel build if the preregistered signal is not this signal."""

    fixed = preregistration.get("fixed_signal_and_execution")
    fresh = preregistration.get("fresh_sample")
    if not isinstance(fixed, dict) or not isinstance(fresh, dict):
        raise ValueError("Alpha #13 preregistration is missing signal metadata")
    expected = {
        "universe": list(SYMBOLS),
        "decision_time": "Monday 04:00 UTC",
        "funding_lookback_days": 7,
        "score": "sum of actual funding events with timestamp in (decision - 7 days, decision]",
        "minimum_funding_events_per_symbol": 21,
        "minimum_score_spread": 0.0025,
        "entry": "next 1h candle open at Monday 05:00 UTC",
        "holding_days": 7,
    }
    if any(fixed.get(key) != value for key, value in expected.items()):
        raise ValueError("Alpha #13 preregistration does not match the fixed signal contract")
    first_decision = fresh.get("first_eligible_decision")
    if not first_decision:
        raise ValueError("Alpha #13 preregistration is missing first_eligible_decision")
    decision = _timestamp(first_decision)
    registered_at = preregistration.get("registered_at_utc")
    if not registered_at:
        raise ValueError("Alpha #13 preregistration is missing registered_at_utc")
    if decision <= _timestamp(registered_at):
        raise ValueError("Alpha #13 first_eligible_decision must follow preregistration")
    if decision.weekday() != 0 or (decision.hour, decision.minute) != (4, 0):
        raise ValueError("Alpha #13 first_eligible_decision violates the Monday 04:00 UTC contract")


def build_funding_dispersion_signal_panel(
    markets: dict[str, dict[str, pd.DataFrame]],
    *,
    first_decision: str | datetime | pd.Timestamp,
    config: FundingDispersionConfig | None = None,
) -> tuple[list[SignalObservation], dict[str, int | str | None]]:
    """Build completed, threshold-eligible gross-return labels only.

    A signal is the negative trailing funding sum, so a higher value expresses
    the preregistered belief that a lower-funded perpetual will outperform.
    Labels use the next completed 1h open through the fixed seven-day exit
    open.  They are deliberately unhedged and before costs: the result is a
    signal diagnostic, not Alpha #13 strategy PnL or a promotion artifact.
    """

    config = config or FundingDispersionConfig()
    decision = _timestamp(first_decision)
    observations: list[SignalObservation] = []
    completed_decisions = 0
    threshold_eligible_decisions = 0
    incomplete_decisions = 0

    while True:
        entry_at = decision + timedelta(hours=1)
        exit_at = entry_at + timedelta(days=config.hold_days)
        if any(
            entry_at not in markets[symbol]["price"].index
            or exit_at not in markets[symbol]["price"].index
            for symbol in SYMBOLS
        ):
            # Sources are day-sealed and contiguous.  There can be no later
            # completed decision once this next fixed label is incomplete.
            break
        completed_decisions += 1
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
        if not scores:
            incomplete_decisions += 1
            decision += timedelta(days=7)
            continue
        score_spread = max(scores.values()) - min(scores.values())
        if score_spread < config.minimum_spread:
            decision += timedelta(days=7)
            continue
        threshold_eligible_decisions += 1
        for symbol in sorted(SYMBOLS):
            price = markets[symbol]["price"]
            entry_price = float(price.at[entry_at, "open"])
            exit_price = float(price.at[exit_at, "open"])
            observations.append(
                SignalObservation(
                    decision_at=decision.to_pydatetime(),
                    forward_start_at=entry_at.to_pydatetime(),
                    forward_end_at=exit_at.to_pydatetime(),
                    symbol=symbol,
                    signal=-scores[symbol],
                    forward_return=exit_price / entry_price - 1.0,
                )
            )
        decision += timedelta(days=7)

    return observations, {
        "completed_decisions": completed_decisions,
        "threshold_eligible_decisions": threshold_eligible_decisions,
        "incomplete_funding_decisions": incomplete_decisions,
        "rows": len(observations),
        "first_completed_decision_at": (
            observations[0].decision_at.isoformat() if observations else None
        ),
        "last_completed_decision_at": (
            observations[-1].decision_at.isoformat() if observations else None
        ),
    }
