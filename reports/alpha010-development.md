# Alpha #10 development report

- Verdict: **FAIL_DEVELOPMENT**
- Natural exits: 95
- Compounded net return on 1x trade notional: -46.4326%
- Profit factor: 0.4210824697143954
- Maximum drawdown: 0.49409988944794425
- 3% crash precision lift: 1.1386212008895478
- Locked OOS opened: **no**

## Development gate

- PASS — natural_closed_trades_minimum
- FAIL — net_return_positive
- FAIL — profit_factor_minimum
- FAIL — double_fee_net_positive
- FAIL — five_bps_per_side_slippage_net_positive
- FAIL — maximum_drawdown_maximum
- PASS — top_three_winner_profit_concentration_maximum
- FAIL — three_percent_crash_precision_lift_minimum

A development pass would only authorize a one-time locked-OOS evaluation. It is not a live-trading approval.
