# Alpha 014 development verdict

Verdict: **FAIL**. This is family trial 3 of `cross_sectional_funding_dispersion`, not an independent alpha.

The fixed 2022-01-02 through 2023-12-30 development run closed 728 daily, two-leg spreads. It lost 48.22% net, produced a 0.68 daily-spread profit factor, and reached a 53.52% hourly mark-to-market drawdown. The geometric monthly return was -2.70%; only 5 of 24 months were positive.

| PnL component | USDT |
|---|---:|
| Raw relative price PnL | -241.02 |
| Modeled slippage | -80.29 |
| Trading fees | -200.74 |
| Actual funding | +39.87 |
| Net PnL | -482.19 |

The raw price component was already negative in both sample halves. Funding softened the loss but did not establish the hypothesized perpetual-futures catch-up. Double-fee return was -61.31%, and five-basis-point-per-fill slippage return was -56.55%.

No threshold, direction, holding period, universe, stop, cost, or sizing parameter will be changed. There is no historical validation stage and this result is ineligible for live promotion. Full machine-readable evidence and all 728 spreads are in `reports/alpha014-development.json`.
