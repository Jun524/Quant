# Candidate screen — 2026-10-28 (v7)

This post-Alpha #16 review considered 30 sources across perpetual-futures
validation, order-flow/microstructure and return-predictor workstreams. It used no
project return data. A candidate had to be independent, causal at the decision time,
costed, and executable as Binance USDT-M futures without an unmodelled hedge.

No new return-data experiment was admitted.

| Family | Decision | Reason |
|---|---|---|
| Execution-constrained autotuning | Validation infrastructure only | It reinforces cost, funding, selection-bias and audit gates; it is not a fixed alpha and would otherwise introduce a parameter search. |
| OHLCV/funding cross-sectional screen | Reject | Recent purged walk-forward evidence is negative after costs and duplicates closed funding/cross-sectional families. |
| Binance futures time-series momentum | Retain Alpha #1 only | The long-biased downside-defense mechanism overlaps the frozen Alpha #1 trend sleeve; a variant would be duplicate-family tuning. |

The result is intentionally conservative: Alpha #16 remains `FAIL`, no new strategy
is registered, and forward evidence is collected without changing leverage, turnover,
or order authority. Live order authorization remains `CLOSED`.
