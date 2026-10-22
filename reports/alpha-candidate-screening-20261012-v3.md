# Candidate screen — 2026-10-12

This screen reviewed 50 search results across five derivatives-research workstreams before
opening any new return-data experiment. The filters were: independent from a sealed failed
family, Binance USDT-M-futures-only execution, causal decision-time inputs, and a reproducible
post-cost implementation.

No candidate passed.

| Family | Decision | Reason |
|---|---|---|
| Random-maturity spot/perpetual basis arbitrage | Reject | Needs a spot hedge and overlaps Alpha 012 cash-and-carry. |
| Spot/perpetual funding carry | Reject | Needs a spot hedge and overlaps Alpha 012. |
| Cross-sectional futures basis | Reject | Duplicates the Alpha 009/014 basis/funding family, which is sealed failed. |
| Intraday ML directional perpetuals | Reject | No source-replayable causal feature, funding, execution, or multiple-testing protocol. |
| Day-of-week effect | Reject | Spot-only calendar study has no stable cost-aware futures return evidence. |
| Scheduled macro events | Reject as alpha | Evidence supports a risk/execution diagnostic, not a fixed signed futures trade. |

The decision is **not** to alter thresholds, directions, costs, holding periods, or leverage in
the failed studies. The monthly 10% figure remains aspirational; a candidate first needs a
verifiable cost-after edge inside the capital-preservation limits.

Machine-readable record: `research/alpha-candidate-screening-20261012-v3.json`.
