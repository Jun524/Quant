"""Conservative, explicit cost arithmetic for perpetual-futures research."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CostInputs:
    entry_notional: float
    exit_notional: float
    entry_fee_rate: float
    exit_fee_rate: float
    entry_slippage_bps: float = 0.0
    exit_slippage_bps: float = 0.0
    spread_bps: float = 0.0
    funding_paid: float = 0.0
    partial_fill_cost: float = 0.0


@dataclass(frozen=True)
class CostEstimate:
    entry_fee: float
    exit_fee: float
    slippage: float
    spread: float
    funding_paid: float
    partial_fill_cost: float
    total_cost: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


def estimate_costs(inputs: CostInputs) -> CostEstimate:
    """Return costs in quote currency without hiding any component.

    ``spread_bps`` is the expected *round-trip* spread cost. Slippage is
    charged independently on each side. Funding must be positive only when it
    is a payment; received funding should be recorded separately as PnL rather
    than used to make an assumed cost negative.
    """
    values = asdict(inputs)
    if any(value < 0 for key, value in values.items() if key != "funding_paid"):
        raise ValueError("cost inputs other than funding_paid must be non-negative")
    if inputs.funding_paid < 0:
        raise ValueError("funding_paid must be a non-negative payment amount")

    entry_fee = inputs.entry_notional * inputs.entry_fee_rate
    exit_fee = inputs.exit_notional * inputs.exit_fee_rate
    slippage = (
        inputs.entry_notional * inputs.entry_slippage_bps
        + inputs.exit_notional * inputs.exit_slippage_bps
    ) / 10_000
    spread = (inputs.entry_notional + inputs.exit_notional) * inputs.spread_bps / 20_000
    total = entry_fee + exit_fee + slippage + spread + inputs.funding_paid + inputs.partial_fill_cost
    return CostEstimate(
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        slippage=slippage,
        spread=spread,
        funding_paid=inputs.funding_paid,
        partial_fill_cost=inputs.partial_fill_cost,
        total_cost=total,
    )
