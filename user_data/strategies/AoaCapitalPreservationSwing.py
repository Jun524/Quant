"""Research-only capital-preserving swing translation of AOA observations.

This is deliberately *not* presented as a reconstruction of another trader's
private decision process.  The public archive supports execution and risk
discipline observations, but not the trader's collateral, leverage, cancelled
orders, or discretionary context.  The signal therefore remains the existing
slow BTC-regime breakout; this class tests a fixed lower-risk implementation
of that signal without adverse averaging.
"""

from FuturesBtcRegimeStrategy import FuturesBtcRegimeRiskBudgetLong


class AoaCapitalPreservationSwing(FuturesBtcRegimeRiskBudgetLong):
    """One-times long-only swing with a fixed 0.25% account-risk budget.

    The 40/20-day channel, BTC BULL regime, -8% emergency stop, and one-times
    leverage are inherited unchanged.  This candidate changes only the risk
    budget and portfolio circuit breaker.  It has no optimizer, no position
    adjustment, and no live configuration.
    """

    account_risk_fraction = 0.0025

    @property
    def protections(self):
        protections = super().protections
        protections[-1] = {
            "method": "MaxDrawdown",
            "lookback_period_candles": 252,
            "trade_limit": 10,
            "stop_duration_candles": 42,
            "max_allowed_drawdown": 0.03,
        }
        return protections
