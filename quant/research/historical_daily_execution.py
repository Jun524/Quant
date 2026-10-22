"""Conservative one-day futures execution accounting from verified daily OHLC.

Daily candles cannot prove tick-level liquidation or fill quality.  They can,
however, reject optimistic stop handling: a triggered stop is filled at the
worse of the stop threshold and the session open, then charged adverse exit
slippage.  The result is explicitly *before* funding.
"""

from __future__ import annotations

from math import isfinite
from typing import Literal


Side = Literal["long", "short"]


class HistoricalDailyExecutionError(ValueError):
    """Daily OHLC execution inputs are malformed or economically unsafe."""


def _positive(name: str, value: float) -> float:
    number = float(value)
    if not isfinite(number) or number <= 0:
        raise HistoricalDailyExecutionError(f"{name} must be positive and finite")
    return number


def simulate_daily_leg(
    *,
    side: Side,
    entry_open: float,
    session_high: float,
    session_low: float,
    scheduled_exit_open: float,
    fee_per_fill: float,
    slippage_per_fill: float,
    stop_loss_fraction: float | None,
) -> dict:
    """Simulate one next-open to next-open leg without funding.

    ``session_high`` and ``session_low`` belong to the fully elapsed holding
    day after entry.  When a stop is crossed, the daily bar cannot identify
    the exact path; using the worse of the known opening gap and stop level is
    deliberately conservative and deterministic.
    """
    if side not in {"long", "short"}:
        raise HistoricalDailyExecutionError("side must be long or short")
    entry = _positive("entry_open", entry_open)
    high = _positive("session_high", session_high)
    low = _positive("session_low", session_low)
    scheduled_exit = _positive("scheduled_exit_open", scheduled_exit_open)
    fee = float(fee_per_fill)
    slippage = float(slippage_per_fill)
    if not isfinite(fee) or not isfinite(slippage) or fee < 0 or slippage < 0:
        raise HistoricalDailyExecutionError("fee and slippage must be finite and non-negative")
    if low > high or low > entry or high < entry:
        raise HistoricalDailyExecutionError("holding-day OHLC does not contain the entry open")
    if stop_loss_fraction is not None:
        stop = float(stop_loss_fraction)
        if not isfinite(stop) or not 0 < stop < 1:
            raise HistoricalDailyExecutionError("stop loss fraction must be in (0, 1)")
    else:
        stop = None

    if side == "long":
        stop_reference = entry * (1.0 - stop) if stop is not None else None
        stopped = stop_reference is not None and low <= stop_reference
        reference_exit = min(entry, stop_reference) if stopped else scheduled_exit
        # Buys pay up, sells receive less.
        effective_entry = entry * (1.0 + slippage)
        effective_exit = reference_exit * (1.0 - slippage)
        gross_price_return = reference_exit / entry - 1.0
        mae = low / entry - 1.0
        mfe = high / entry - 1.0
    else:
        stop_reference = entry * (1.0 + stop) if stop is not None else None
        stopped = stop_reference is not None and high >= stop_reference
        reference_exit = max(entry, stop_reference) if stopped else scheduled_exit
        # Short sale receives less, buy-to-cover pays up.
        effective_entry = entry * (1.0 - slippage)
        effective_exit = reference_exit * (1.0 + slippage)
        gross_price_return = entry / reference_exit - 1.0
        mae = entry / high - 1.0
        mfe = entry / low - 1.0
    execution_price_return = (
        effective_exit / effective_entry - 1.0
        if side == "long"
        else effective_entry / effective_exit - 1.0
    )
    fee_return = 2.0 * fee
    return {
        "side": side,
        "stopped": bool(stopped),
        "entry_reference_price": entry,
        "exit_reference_price": reference_exit,
        "effective_entry_price": effective_entry,
        "effective_exit_price": effective_exit,
        "gross_price_return": gross_price_return,
        "execution_price_return": execution_price_return,
        "fee_return": fee_return,
        "net_return_before_funding": execution_price_return - fee_return,
        "mae": mae,
        "mfe": mfe,
        "funding_return": None,
        "liquidation_claim": "FORBIDDEN_FROM_DAILY_OHLC_ONLY",
    }


def attach_verified_funding(leg: dict, *, funding_return: float, funding_source: str) -> dict:
    """Attach a separately replayed funding result to a daily-OHLC leg.

    Funding is intentionally mandatory at this stage: callers must obtain it
    from a checksum-verified event ledger instead of treating an absent value
    as zero.  The function is pure so a portfolio evaluator can retain every
    price, fee, slippage, and funding component independently.
    """
    if not isinstance(leg, dict) or leg.get("funding_return") is not None:
        raise HistoricalDailyExecutionError("daily leg must be an unfunded simulation result")
    before = leg.get("net_return_before_funding")
    if not isinstance(before, (int, float)) or not isfinite(float(before)):
        raise HistoricalDailyExecutionError("daily leg lacks a finite pre-funding return")
    funding = float(funding_return)
    if not isfinite(funding) or not isinstance(funding_source, str) or not funding_source:
        raise HistoricalDailyExecutionError("funding must be finite and source bound")
    return {
        **leg,
        "funding_return": funding,
        "funding_source": funding_source,
        "net_return": float(before) + funding,
        "pnl_components": {
            "gross_price_return": leg["gross_price_return"],
            "execution_price_return": leg["execution_price_return"],
            "trading_fee_return": -leg["fee_return"],
            "funding_return": funding,
            "net_return": float(before) + funding,
        },
    }
