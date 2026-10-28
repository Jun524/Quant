# Candidate screen — 2026-10-28 (v8)

This post-policy screen reviewed 30 sources across three workstreams without
inspecting project return data. It required a causal Binance USDT-M-only
implementation with no more than two concurrent futures positions, one-times
planned leverage, complete cost/funding treatment, and independence from failed
or still-accumulating families.

No new return-data experiment was admitted.

| Family | Decision | Reason |
|---|---|---|
| Alpha 015 minute cross-crypto portfolio | Close before data acquisition | Its fixed top/bottom-quintile construction needs at least 12 concurrent futures positions, exceeding the active two-position limit. |
| Binance USDT-M cointegration/copula pair | Reject | A 2026 Binance USDT-perpetual study reports its market-neutral variants are negative after fee, spread and funding treatment; the mechanism also overlaps the sealed mean-reversion family. |
| Spot–perpetual random-maturity arbitrage | Reject | Its documented implementation needs a spot hedge and therefore falls outside the Binance USDT-M-only scope. |

The outcome is a deliberate non-selection: Alpha 015 cannot access return data,
no failed family is retuned, and no new strategy is registered. The active work
remains sealed forward and execution-quality evidence for Alpha 001, Alpha 007,
and Alpha 011. Live order authorization remains `CLOSED`.

Sources: [Binance USDT perpetual pairs study](https://aimspress.com/article/doi/10.3934/QFE.2026016), [perpetual-futures fundamentals and random-maturity arbitrage](https://arxiv.org/html/2212.06888v7).
